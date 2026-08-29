"""The L1 summary tier. Design Section 11 ("summary tier (L1 overviews)")
+ Section 7 pipeline step 5 ("optional summary tier (L1 per-document
overviews as a coarse index"), applying the OpenViking L0/L1/L2
progressive-loading pattern (``docs/mining/G01-memory-1__OpenViking.md``)
to the knowledge corpus: per-document and per-collection overview
summaries, generated via judgment envelopes, stored durably, and served as
a retrieval tier (:mod:`trialerror.retrieve.engine`'s ``mode="summary"`` path)
so cheap overview context precedes expensive chunk retrieval.

**LLM-judgment boundary (the house ``trialerror/verify`` pattern, applied
here):** this module never calls an LLM itself. :func:`build_summary_envelope`
builds a "judgment request" envelope (plain dict: the subject's context,
the word cap, the fence instruction when applicable) that a real agent
fills at runtime — the AGENT does the writing — or a deterministic fake
fills in tests; :func:`store_summary` takes that agent-authored ``body``
text and durably records it. This module shapes the work (context
assembly, staleness keys, the D-COC-1 embedded-quote fence, versioned
supersession); it never authors summary text itself.

Two subjects (mirrors ``verdict``'s polymorphic ``subject_kind``/
``subject_id`` shape rather than inventing a parallel one — see the
``knowledge_v3_summary_table`` migration's own comment for why):

- ``document`` — ``subject_id`` is a real ``doc_id``; the summary
  overviews that one document.
- ``collection`` — ``subject_id`` is a caller-chosen grouping key (a
  ``source_id`` when every document under one source is being summarized
  together, or a free-form label paired with an explicit ``doc_ids`` list);
  the summary overviews the whole set, built bottom-up from each member's
  OWN current L1 summary when one already exists (the OpenViking pattern:
  "aggregating child L0s into parent L1s recursively"), falling back to a
  raw excerpt for a member that hasn't been summarized yet.

**Versioning:** a re-summarize supersedes, never overwrites — see the
``knowledge_v3_summary_table`` migration's own comment for the full
rationale (a same-table versioned-row chain, the ``artifact``/
``source.dedup_of`` ``status``+``supersedes`` convention, not
``trialerror.stores.bitemporal``'s four-timestamp shape, which has no
independent event-time axis to offer a summary).
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from trialerror.ingest.anchors import sha256_hex
from trialerror.ingest.stream import stream_v1
from trialerror.retrieve.fence import MAX_FENCED_EXCERPT_WORDS, excerpt_words, is_fenced_license, source_license_tier
from trialerror.stores.store import Store
from trialerror.stores.writer import get, insert, update
from trialerror.summarize.errors import InvalidSubjectKindError, SubjectNotFoundError, SummarizeError, SummaryFenceViolationError
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "SUBJECT_KINDS",
    "DEFAULT_WORD_CAP",
    "MAX_EMBEDDED_QUOTE_WORDS",
    "compute_subject_sha256",
    "build_summary_envelope",
    "store_summary",
    "get_summary",
    "get_summary_by_id",
    "list_summaries",
    "find_stale_or_missing_document_summaries",
]

#: Matches ``summary.subject_kind``'s CHECK constraint
#: (``trialerror/stores/schema/knowledge.py``).
SUBJECT_KINDS: frozenset[str] = frozenset({"document", "collection"})

#: The L1 overview's default word budget — sent to the agent as
#: ``envelope["word_cap"]``, recorded alongside the actual ``word_count``
#: at write time, never itself enforced as a hard truncation (an agent's
#: genuine output being a little over/under is not a fence violation —
#: only an embedded VERBATIM quote from a restricted source is, see
#: :data:`MAX_EMBEDDED_QUOTE_WORDS`).
DEFAULT_WORD_CAP = 150

#: D-COC-1's cap, reused verbatim (:data:`trialerror.retrieve.fence.
#: MAX_FENCED_EXCERPT_WORDS`) — an L1 overview of a restricted source is
#: EXTRACTION, not verbatim reproduction, so the overview BODY serves in
#: full at any length; but any literal quoted excerpt embedded inside it
#: is held to the exact same word cap every other verbatim excerpt in this
#: codebase is held to (:mod:`trialerror.retrieve.fence`).
MAX_EMBEDDED_QUOTE_WORDS = MAX_FENCED_EXCERPT_WORDS

#: How much of a document's canonical ``stream_v1`` text an envelope shows
#: the agent for a ``document``-kind summary.
_DEFAULT_DOC_EXCERPT_CHARS = 4000

#: Per-member excerpt budget for a ``collection``-kind summary's context
#: block (kept far smaller than a document's own budget, since a
#: collection context concatenates one such block per member).
_DEFAULT_COLLECTION_MEMBER_CHARS = 800

#: Straight ``"..."`` and curly ``“...”`` quoted spans — the two quoting
#: conventions an agent's generated prose plausibly uses. Bounded
#: (``{1,4000}``) so a pathological unterminated-quote input can't make
#: this run away; a summary body is never that long in practice (v0 has no
#: enforced hard cap on ``body`` length, but this regex is a defensive
#: bound, not a content policy).
_QUOTE_SPAN_RE = re.compile(r'"([^"]{1,4000})"|“([^”]{1,4000})”')


def _quoted_spans(text: str | None) -> list[str]:
    spans: list[str] = []
    for m in _QUOTE_SPAN_RE.finditer(text or ""):
        spans.append(m.group(1) if m.group(1) is not None else m.group(2))
    return spans


def _embedded_quote_violations(body: str, *, max_words: int = MAX_EMBEDDED_QUOTE_WORDS) -> list[str]:
    """Every quoted span in ``body`` longer than ``max_words`` words —
    empty when none violate."""
    return [q for q in _quoted_spans(body) if len(q.split()) > max_words]


def _document_row(store: Store, doc_id: str) -> dict[str, Any]:
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise SubjectNotFoundError(f"no such document: {doc_id!r}")
    return doc


def _load_elements(store: Store, doc_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in store.knowledge.execute("SELECT * FROM element WHERE doc_id = ? ORDER BY seq", (doc_id,)).fetchall()]


def compute_subject_sha256(store: Store, subject_kind: str, member_doc_ids: Sequence[str]) -> str:
    """The ONE staleness-key computation both the write path
    (:func:`build_summary_envelope`) and the doctor check
    (:mod:`trialerror.summarize.checks`) call, so the two can never
    independently drift on what "stale" means (design's own
    ``anchors_dangling`` doc-level check does the identical
    ``document.sha256``-comparison trick for the exact same reason).

    ``document`` subject: literally that one document's current
    ``sha256`` — a missing document hashes to a sentinel string (never
    silently ``None``) so "the document was deleted" is still a real,
    non-matching sha rather than a crash.

    ``collection`` subject: a combined hash over every member's
    ``(doc_id, sha256)`` pair, sorted (order-independent — two calls with
    the same membership in a different order hash identically) — changing
    ANY member's content, or the membership set itself, changes this hash.
    """
    if subject_kind == "document":
        if len(member_doc_ids) != 1:
            raise InvalidSubjectKindError(
                f"compute_subject_sha256: a 'document' subject must have exactly one member doc_id, got {len(member_doc_ids)}"
            )
        doc = get(store, "document", pk_column="doc_id", pk_value=member_doc_ids[0])
        return doc["sha256"] if doc is not None else sha256_hex(f"__missing_document__:{member_doc_ids[0]}")
    if subject_kind == "collection":
        parts = []
        for doc_id in member_doc_ids:
            doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
            doc_sha = doc["sha256"] if doc is not None else "__missing__"
            parts.append(f"{doc_id}:{doc_sha}")
        return sha256_hex("|".join(sorted(parts)))
    raise InvalidSubjectKindError(f"subject_kind must be one of {sorted(SUBJECT_KINDS)!r}, got {subject_kind!r}")


def _any_fenced_source(store: Store, doc_ids: Sequence[str]) -> bool:
    """Whether ANY of ``doc_ids``' sources requires the D-COC-1 fence —
    recomputed fresh from CURRENT ``document``/``source`` rows every call
    (never trusted from a possibly-stale cached flag), matching
    :mod:`trialerror.retrieve.engine`'s own "fencing is always decided fresh at
    serve/write time" convention."""
    for doc_id in doc_ids:
        doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
        if doc is None:
            continue
        source = get(store, "source", pk_column="source_id", pk_value=doc["source_id"])
        if is_fenced_license(source_license_tier(source)):
            return True
    return False


def _resolve_collection_doc_ids(store: Store, subject_id: str, doc_ids: Sequence[str] | None) -> list[str]:
    if doc_ids:
        deduped: list[str] = []
        for d in doc_ids:
            if d not in deduped:
                deduped.append(d)
        return deduped
    rows = store.knowledge.execute(
        "SELECT doc_id FROM document WHERE source_id = ? ORDER BY rel_path", (subject_id,)
    ).fetchall()
    if not rows:
        raise SubjectNotFoundError(
            f"collection {subject_id!r}: no doc_ids given, and it does not match an existing "
            "source_id with any documents under it"
        )
    return [r["doc_id"] for r in rows]


def _document_context_block(store: Store, doc_id: str, *, max_chars: int = _DEFAULT_DOC_EXCERPT_CHARS) -> str:
    doc = _document_row(store, doc_id)
    source = get(store, "source", pk_column="source_id", pk_value=doc["source_id"])
    elements = _load_elements(store, doc_id)
    if not elements:
        raise SubjectNotFoundError(f"document {doc_id!r} has no element rows yet (not normalized) -- nothing to summarize")
    full_text = stream_v1(elements)
    if not full_text.strip():
        raise SubjectNotFoundError(f"document {doc_id!r}'s elements carry no text -- nothing to summarize")
    excerpt = full_text[:max_chars]
    truncated_note = " [excerpt truncated]" if len(full_text) > max_chars else ""
    title = source["title"] if source else doc_id
    return f"Document: {title} ({doc_id}){truncated_note}\n\n{excerpt}"


def _collection_context_block(store: Store, member_doc_ids: Sequence[str], *, per_doc_chars: int = _DEFAULT_COLLECTION_MEMBER_CHARS) -> str:
    """Built bottom-up (the OpenViking pattern, module docstring): a member
    with an existing current L1 summary contributes THAT (cheap, already
    reviewed context); a member with none yet contributes a raw excerpt
    instead — never a hard failure, so one un-normalized member doesn't
    block summarizing the rest of a collection."""
    parts: list[str] = []
    for doc_id in member_doc_ids:
        doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
        if doc is None:
            parts.append(f"### {doc_id} [document not found -- skipped]")
            continue
        source = get(store, "source", pk_column="source_id", pk_value=doc["source_id"])
        title = source["title"] if source else doc_id
        existing = get_summary(store, subject_kind="document", subject_id=doc_id)
        if existing is not None:
            snippet = existing["body"]
            origin = "existing L1 summary"
        else:
            elements = _load_elements(store, doc_id)
            snippet = stream_v1(elements)[:per_doc_chars] if elements else "(no content yet)"
            origin = "raw excerpt, no summary yet"
        parts.append(f"### {title} ({doc_id}) [{origin}]\n{snippet}")
    return "\n\n".join(parts)


def build_summary_envelope(
    store: Store,
    *,
    subject_kind: str,
    subject_id: str,
    doc_ids: Sequence[str] | None = None,
    word_cap: int = DEFAULT_WORD_CAP,
) -> dict[str, Any]:
    """Build one judgment-request envelope: the agent-facing context block
    plus the metadata :func:`store_summary` needs to record the result
    (``word_cap``, ``source_doc_ids`` — "citation of source doc ids" per
    the build brief — and the staleness key ``subject_sha256``).

    ``subject_kind='document'``: ``subject_id`` must be an existing
    ``doc_id`` with at least one ``element`` row (normalized); ``doc_ids``
    must be omitted or exactly ``[subject_id]``.

    ``subject_kind='collection'``: ``doc_ids`` names the member documents
    explicitly, or — when omitted — every document under the source whose
    ``source_id == subject_id`` (a convenience default for "summarize this
    whole source"). Refuses (:class:`~trialerror.summarize.errors.
    SubjectNotFoundError`) when that resolves to zero members.
    """
    if subject_kind not in SUBJECT_KINDS:
        raise InvalidSubjectKindError(f"build_summary_envelope: subject_kind must be one of {sorted(SUBJECT_KINDS)!r}, got {subject_kind!r}")
    if word_cap < 1:
        raise SummarizeError("build_summary_envelope: word_cap must be >= 1")

    if subject_kind == "document":
        if doc_ids not in (None, [subject_id]):
            raise SummarizeError("build_summary_envelope: subject_kind='document' requires doc_ids omitted or exactly [subject_id]")
        member_doc_ids = [subject_id]
        context = _document_context_block(store, subject_id)
    else:
        member_doc_ids = _resolve_collection_doc_ids(store, subject_id, doc_ids)
        context = _collection_context_block(store, member_doc_ids)

    subject_sha256 = compute_subject_sha256(store, subject_kind, member_doc_ids)
    fenced = _any_fenced_source(store, member_doc_ids)

    instruction = (
        f"Write a plain-language {subject_kind} overview, no more than {word_cap} words. "
        "Ground every claim in the material shown below; do not invent facts not present in it."
    )
    if fenced:
        instruction += (
            " At least one cited source is commercial_restricted: an overview like this one is "
            "an EXTRACTION/SUMMARY, which is always allowed at any length -- but any literal "
            f"quoted excerpt you embed from the source text must be {MAX_EMBEDDED_QUOTE_WORDS} "
            "words or fewer (D-COC-1). Paraphrase instead of quoting wherever you can."
        )

    return {
        "kind": "summary",
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "source_doc_ids": member_doc_ids,
        "word_cap": word_cap,
        "fenced": fenced,
        "subject_sha256": subject_sha256,
        "context": context,
        "instruction": instruction,
    }


def store_summary(
    store: Store,
    *,
    envelope: Mapping[str, Any],
    body: str,
    issued_by_launch: str,
    procedure_version: str = "1",
    ts: str | None = None,
) -> dict[str, Any]:
    """Durably record an agent-authored ``body`` for the subject
    :func:`build_summary_envelope` described in ``envelope``. Versioned:
    if a ``status='current'`` row already exists for this
    ``(subject_kind, subject_id)``, the NEW row is written first (as
    ``current``, ``supersedes`` pointing at the old row), then the old row
    is flipped to ``superseded`` -- this order (not the reverse) is
    deliberate: a crash between the two steps leaves a harmless transient
    DUPLICATE ``current`` row (which :func:`get_summary` resolves by
    picking the most recent) rather than a window with NO current summary
    at all. Mirrors ``trialerror.stores.bitemporal.supersede_fact``'s own
    assert-then-expire ordering for the identical reason.

    Refuses (:class:`~trialerror.summarize.errors.SummaryFenceViolationError`,
    raised BEFORE any write) when ``envelope``'s cited sources include a
    ``commercial_restricted`` one AND ``body`` embeds a quoted run longer
    than :data:`MAX_EMBEDDED_QUOTE_WORDS` words -- the D-COC-1 fence,
    enforced here rather than trusted to have been honored by whatever
    produced ``body``. ``fenced`` is recomputed fresh from the CURRENT
    ``document``/``source`` rows (never taken from ``envelope["fenced"]``,
    which could be stale if this call runs long after the envelope was
    built) and stored on the row purely for audit/display convenience.
    """
    subject_kind = envelope["subject_kind"]
    subject_id = envelope["subject_id"]
    if subject_kind not in SUBJECT_KINDS:
        raise InvalidSubjectKindError(f"store_summary: envelope subject_kind must be one of {sorted(SUBJECT_KINDS)!r}, got {subject_kind!r}")
    if not body or not body.strip():
        raise SummarizeError("store_summary: body must not be empty")

    source_doc_ids = list(envelope["source_doc_ids"])
    fenced = _any_fenced_source(store, source_doc_ids)

    if fenced:
        offenders = _embedded_quote_violations(body)
        if offenders:
            worst = max(offenders, key=lambda q: len(q.split()))
            raise SummaryFenceViolationError(
                f"summary for {subject_kind}:{subject_id} cites a commercial_restricted source and "
                f"embeds a quoted run of {len(worst.split())} words (> {MAX_EMBEDDED_QUOTE_WORDS}-word "
                f'D-COC-1 cap): "{excerpt_words(worst, MAX_EMBEDDED_QUOTE_WORDS)}..."'
            )

    subject_sha256 = envelope.get("subject_sha256") or compute_subject_sha256(store, subject_kind, source_doc_ids)
    existing = get_summary(store, subject_kind=subject_kind, subject_id=subject_id)

    row = {
        "summary_id": new_id("SUM"),
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "tier": "L1",
        "body": body,
        "word_count": len(body.split()),
        "word_cap": int(envelope.get("word_cap", DEFAULT_WORD_CAP)),
        "source_doc_ids": json.dumps(source_doc_ids, ensure_ascii=False),
        "subject_sha256": subject_sha256,
        "fenced": 1 if fenced else 0,
        "status": "current",
        "supersedes": existing["summary_id"] if existing is not None else None,
        "procedure_version": procedure_version,
        "created_by_launch": issued_by_launch,
        "created_ts": ts or now(),
    }
    written = insert(store, "summary", row)

    if existing is not None:
        update(store, "summary", pk_column="summary_id", pk_value=existing["summary_id"], changes={"status": "superseded"})

    return written


def get_summary(store: Store, *, subject_kind: str, subject_id: str) -> dict[str, Any] | None:
    """The current (``status='current'``) summary for ``(subject_kind,
    subject_id)``, or ``None``. Picks the most recently created row when
    (rarely, transiently) more than one is marked ``current`` -- see
    :func:`store_summary`'s own docstring for why that window can exist."""
    row = store.knowledge.execute(
        "SELECT * FROM summary WHERE subject_kind = ? AND subject_id = ? AND status = 'current' "
        "ORDER BY created_ts DESC, rowid DESC LIMIT 1",
        (subject_kind, subject_id),
    ).fetchone()
    return dict(row) if row is not None else None


def get_summary_by_id(store: Store, summary_id: str) -> dict[str, Any] | None:
    return get(store, "summary", pk_column="summary_id", pk_value=summary_id)


def list_summaries(
    store: Store,
    *,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """``trialerror summarize list`` -- filtered read, newest-first (mirrors
    ``trialerror.artifacts.registry.list_artifacts``'s own ``rowid``-ordering
    convention)."""
    clauses: list[str] = []
    params: list[Any] = []
    if subject_kind is not None:
        clauses.append("subject_kind = ?")
        params.append(subject_kind)
    if subject_id is not None:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.knowledge.execute(
        f"SELECT *, rowid AS _rowid FROM summary {where} ORDER BY _rowid DESC LIMIT ?", params + [limit]
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]


def find_stale_or_missing_document_summaries(store: Store) -> dict[str, list[str]]:
    """Every ``document`` partitioned into ``missing`` (no current summary
    at all) and ``stale`` (a current summary exists, but its
    ``subject_sha256`` no longer matches the document's CURRENT
    ``sha256``). Used by :mod:`trialerror.summarize.handlers`'s batch
    job-discovery, which always has a full :class:`Store` (``ctx.store``).

    TRIALERROR-DEV-NOTE: :mod:`trialerror.summarize.checks`'s ``summaries_stale``
    doctor check reimplements this exact predicate as a single raw SQL
    query against a read-only ``sqlite3.Connection`` instead of calling
    this function -- matching every other doctor check in this codebase
    (``trialerror.stores.checks.check_anchors_dangling`` et al.), none of which
    open a full four-DB :class:`Store` just to run one query. Both
    predicates are spelled out in prose (this docstring) precisely so the
    two implementations cannot drift silently: 'missing' = no ``summary``
    row with ``status='current'`` for this ``doc_id``; 'stale' = a current
    summary exists but its ``subject_sha256`` != the document's CURRENT
    ``sha256``."""
    docs = [dict(r) for r in store.knowledge.execute("SELECT doc_id, sha256 FROM document ORDER BY doc_id").fetchall()]
    missing: list[str] = []
    stale: list[str] = []
    for doc in docs:
        current = get_summary(store, subject_kind="document", subject_id=doc["doc_id"])
        if current is None:
            missing.append(doc["doc_id"])
        elif current["subject_sha256"] != doc["sha256"]:
            stale.append(doc["doc_id"])
    return {"missing": missing, "stale": stale}
