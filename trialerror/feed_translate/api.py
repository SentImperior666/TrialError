"""The AI-Speak -> plain-English Feed translator's core API.
``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 5 step 2, modelled
line-for-line on :mod:`trialerror.summarize.api` (that design doc names
``trialerror.summarize`` as "the load-bearing precedent" and this package is
the same architecture retargeted from ``knowledge.document`` onto
``ops.feed_post``).

What this module owns:

- :func:`build_translation_envelope` -- the judgment-request envelope a
  translator backend fills: the original body, the style contract
  (:data:`TRANSLATION_INSTRUCTION`, §4.5 verbatim), the mode, and the
  staleness key.
- :func:`store_translation` -- durable, VERSIONED storage of whatever
  plain text came back, with the gate verdict attached. Writes a new row
  and supersedes the old one; never an ``UPDATE`` of an existing
  translation, and never, under any circumstance, a write to
  ``feed_post``.
- :func:`get_translation` / :func:`list_translations` /
  :func:`find_untranslated_posts` / :func:`count_gate_failures` -- the
  read side the CLI, the dashboard panel and the doctor checks share.

**The append-only original is the whole reason this table exists.**
``ops.feed_post`` is append-only and doctor-audited for it
(``trialerror.events.checks.check_feed_author_integrity``); ``author`` is
server-derived by ``trialerror.events.api._derive_author`` and is not
settable by any caller. Nothing in this package writes, updates or
deletes a ``feed_post`` row -- the translation is a SIDECAR, and
:func:`store_translation` touches exactly one table.

**LLM-judgment boundary (the house pattern, restated).** This module
never calls an LLM. It builds an envelope and stores whatever text a
caller hands back -- a backend (:mod:`trialerror.feed_translate.backends`), a
live agent session filling a parked envelope, or a deterministic fake in
tests.

**Staleness.** ``feed_post`` is append-only, so a translation cannot go
stale against an edited original the way a ``summary`` can against a
re-normalized document. ``original_sha256`` is still recorded and still
re-checked, because it costs nothing and catches the
impossible-but-cheap-to-guard case of a ``post_id`` resolving to different
body text (design Section 4.2's own comment on that column). The real
staleness driver is a deliberate :data:`CURRENT_TRANSLATOR_VERSION` bump --
an explicit operator action after a style-contract fix -- which
``translations_stale`` (:mod:`trialerror.feed_translate.checks`) reports.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from trialerror.feed_translate.errors import InvalidStyleModeError, PostNotFoundError
from trialerror.feed_translate.style import DEFAULT_STYLE_MODE, STYLE_MODES
from trialerror.ingest.anchors import sha256_hex
from trialerror.stores.store import Store
from trialerror.stores.writer import get, insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "CURRENT_TRANSLATOR_VERSION",
    "TRANSLATION_INSTRUCTION",
    "compute_original_sha256",
    "build_translation_envelope",
    "store_translation",
    "get_translation",
    "get_translation_by_id",
    "list_translations",
    "find_untranslated_posts",
    "count_gate_failures",
]

#: The style contract's version. Bumping this is the ONE way to make every
#: existing translation stale on purpose (module docstring); it is a
#: string, not an int, because it is stored in a ``TEXT`` column and
#: compared for equality, never ordered.
CURRENT_TRANSLATOR_VERSION = "1"

#: ``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 4.5, carried into
#: the envelope verbatim (that design's own step 7: "Copy this doc's §4.5
#: into ... as ``TRANSLATION_INSTRUCTION``"). Every rule here has a
#: counterpart in :mod:`trialerror.feed_translate.style`, which checks the
#: result -- writer-side prose and checker-side code are two readings of
#: one list, and the rule numbers match.
TRANSLATION_INSTRUCTION = """\
Rewrite this Feed post in plain English. It is a TRANSLATION, not a summary: keep
every claim the post makes. It may come out shorter only because plain sentences are
shorter than jargon-dense ones, never because you cut content.

SENTENCE AND STRUCTURE
1. 25 words per sentence at most. Split anything longer.
2. One idea per sentence. No "and then", no comma-spliced compound claims.
3. Active voice. Name the actor ("the harness reclaimed the job"), not the passive
   ("the job was reclaimed").
4. No semicolons. Split into two sentences instead.
5. No phrasal verbs where a single plain verb exists: "start", not "spin up"; "read",
   not "dive into".
6. Noun clusters of 3 words at most. Break a longer noun stack into a short phrase.
7. Use a numbered or bulleted list for any sequence of 3 or more steps or conditions
   buried in prose.

FIDELITY (non-negotiable; a failure here means the translation is withheld)
8. Every id, number, date and named entity from the original -- booking ids, launch
   ids, gate and verdict labels, register and session ids, campaign codes, counts --
   appears in the translation verbatim and unchanged. Never paraphrase, round or drop
   an id. Never introduce a number the original does not contain. Write every number
   as a numeral (3, not "three") -- the checker compares digit strings, and spelling
   a number out reads as a dropped number even when the value is right.
9. Never promote a hedge to a fact. "Provisionally", "pending", "deferred", "may
   have", "unconfirmed" in the original must leave a corresponding hedge in the
   translation. Collapsing "DEFERRED, not FAILED" into "failed" is a fidelity break,
   not a simplification.
10. Add no fact, cause or mechanism the original did not state. If the plain version
    needs a missing detail to read naturally, leave the gap rather than invent a
    plausible-sounding fill.
11. Keep a real trade-off, exception or alternative the original actually discusses.
    Do not invent a rejected-alternative sentence that was not there.

REGISTER
12. No inflated-importance framing and no sales language. Do not add "pivotal",
    "robust", "seamless". Replace such a claim with the concrete fact that earns it,
    if any.
13. No chatbot filler. No "hope this helps", no "let me know if", no praise or
    agreement preamble. This is a rendering of the post, not a reply to it.
14. No em or en dashes unless the original uses them at the same rate. Split into two
    sentences instead.
15. Define a domain term inline in 6 words or fewer the first time it appears, and
    only if the term has no glossary entry.

Return the plain-English text only. No preamble, no headings you invented, no notes
about what you changed.\
"""


def compute_original_sha256(body: str) -> str:
    """The staleness key: ``sha256`` of the post body exactly as stored.
    One function so the write path and the doctor check cannot drift on
    what "the same original" means (the same reason
    :func:`trialerror.summarize.api.compute_subject_sha256` exists)."""
    return sha256_hex(body or "")


def _post_row(store: Store, post_id: str) -> dict[str, Any]:
    post = get(store, "feed_post", pk_column="post_id", pk_value=post_id)
    if post is None:
        raise PostNotFoundError(f"no such feed post: {post_id!r}")
    if not (post.get("body") or "").strip():
        raise PostNotFoundError(f"feed post {post_id!r} has an empty body -- nothing to translate")
    return post


def build_translation_envelope(
    store: Store,
    *,
    post_id: str,
    style_mode: str = DEFAULT_STYLE_MODE,
    translator_version: str = CURRENT_TRANSLATOR_VERSION,
    glossary_hint_terms: Sequence[str] | None = None,
) -> dict[str, Any]:
    """One judgment-request envelope for one post (design Section 4.1
    step 2). Shape::

        {"kind": "feed_translate", "post_id", "thread_id", "author",
         "original_body", "original_sha256", "style_mode",
         "translator_version", "glossary_hint_terms", "instruction"}

    ``glossary_hint_terms`` is the design's optional, deferred-friendly
    glossary hook (Section 4.3.4): there is no glossary table in this
    codebase yet, so callers pass nothing and the field is ``[]``. When one
    lands, populating this field is the entire integration -- the envelope
    shape, the storage and the gate do not change.
    """
    if style_mode not in STYLE_MODES:
        raise InvalidStyleModeError(
            f"build_translation_envelope: style_mode must be one of {sorted(STYLE_MODES)!r}, got {style_mode!r}"
        )
    post = _post_row(store, post_id)
    body = post["body"]
    return {
        "kind": "feed_translate",
        "post_id": post_id,
        "thread_id": post["thread_id"],
        "author": post["author"],
        "original_body": body,
        "original_sha256": compute_original_sha256(body),
        "style_mode": style_mode,
        "translator_version": translator_version,
        "glossary_hint_terms": list(glossary_hint_terms or []),
        "instruction": TRANSLATION_INSTRUCTION,
    }


def store_translation(
    store: Store,
    *,
    envelope: Mapping[str, Any],
    body: str,
    gate: Mapping[str, Any] | None = None,
    created_by_launch: str | None = None,
    glossary_links: Sequence[Mapping[str, Any]] | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Durably record one translation. Versioned exactly like
    :func:`trialerror.summarize.api.store_summary`: the NEW row is written
    first (as ``current``, ``supersedes`` pointing at the old row), then the
    old row is flipped to ``superseded``. That order is deliberate and is
    copied for the same reason -- a crash between the two steps leaves a
    harmless transient DUPLICATE ``current`` row (which
    :func:`get_translation` resolves by picking the most recent) rather
    than a window with NO current translation at all.

    ``gate`` is a :meth:`trialerror.feed_translate.gate.GateResult.as_row`
    mapping (``gate_status``/``gate_reasons``/``faithfulness_score``/
    ``faithfulness_verdict_id``). Omitting it stores the row ``'ungated'``
    -- legal, but it means the dashboard will render it with an explicit
    "not gated" flag, and no code path inside this package omits it.
    A ``gate_status='fail'`` row is STORED, not refused: design Section
    4.3.3 wants the failure recorded and countable
    (``feed_translation_failures``), just never served.

    ``created_by_launch`` is nullable, mirroring ``feed_post.launch_id``
    itself: a translation the orchestrator produced live in its own
    session carries no launch id, exactly like an orchestrator feed post;
    a batch-booked subagent's translation carries that subagent's
    ``launch_id``, XID-checked against ``platform.launch`` by
    ``trialerror.stores.insert``.
    """
    post_id = envelope["post_id"]
    style_mode = envelope.get("style_mode", DEFAULT_STYLE_MODE)
    if style_mode not in STYLE_MODES:
        raise InvalidStyleModeError(
            f"store_translation: envelope style_mode must be one of {sorted(STYLE_MODES)!r}, got {style_mode!r}"
        )
    if not body or not body.strip():
        raise ValueError("store_translation: body must not be empty")

    translator_version = str(envelope.get("translator_version", CURRENT_TRANSLATOR_VERSION))
    # Recomputed from the CURRENT post row rather than trusted from the
    # envelope, which could have been built long before this call --
    # trialerror.summarize.store_summary re-derives `fenced` for the
    # identical reason.
    original_sha256 = compute_original_sha256(_post_row(store, post_id)["body"])

    existing = get_translation(store, post_id=post_id, translator_version=translator_version)
    gate_row = dict(gate or {})

    row = {
        "translation_id": new_id("XLAT"),
        "post_id": post_id,
        "translator_version": translator_version,
        "style_mode": style_mode,
        "body": body,
        "original_sha256": original_sha256,
        "faithfulness_score": gate_row.get("faithfulness_score"),
        "faithfulness_verdict_id": gate_row.get("faithfulness_verdict_id"),
        "glossary_links": json.dumps(list(glossary_links), ensure_ascii=False) if glossary_links else None,
        "status": "current",
        "supersedes": existing["translation_id"] if existing is not None else None,
        "created_by_launch": created_by_launch,
        "created_ts": ts or now(),
        "gate_status": gate_row.get("gate_status", "ungated"),
        "gate_reasons": gate_row.get("gate_reasons"),
    }
    written = insert(store, "feed_post_translation", row)

    if existing is not None:
        update(
            store,
            "feed_post_translation",
            pk_column="translation_id",
            pk_value=existing["translation_id"],
            changes={"status": "superseded"},
        )
    return written


def get_translation(
    store: Store, *, post_id: str, translator_version: str | None = None
) -> dict[str, Any] | None:
    """The current (``status='current'``) translation for ``post_id``, or
    ``None``. ``translator_version=None`` means "whatever version is
    current"; passing one narrows to that version (what
    :func:`store_translation` does, so bumping the version supersedes
    within its own chain rather than across chains). Picks the most
    recently created row when (rarely, transiently) more than one is
    marked ``current`` -- see :func:`store_translation` for why that
    window can exist.
    """
    clauses = ["post_id = ?", "status = 'current'"]
    params: list[Any] = [post_id]
    if translator_version is not None:
        clauses.append("translator_version = ?")
        params.append(translator_version)
    row = store.ops.execute(
        f"SELECT * FROM feed_post_translation WHERE {' AND '.join(clauses)} "
        "ORDER BY created_ts DESC, rowid DESC LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row is not None else None


def get_translation_by_id(store: Store, translation_id: str) -> dict[str, Any] | None:
    return get(store, "feed_post_translation", pk_column="translation_id", pk_value=translation_id)


def list_translations(
    store: Store,
    *,
    post_id: str | None = None,
    status: str | None = None,
    gate_status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Filtered read, newest-first (``rowid`` ordering, the convention
    :func:`trialerror.summarize.api.list_summaries` and
    ``trialerror.artifacts.registry.list_artifacts`` already share)."""
    clauses: list[str] = []
    params: list[Any] = []
    if post_id is not None:
        clauses.append("post_id = ?")
        params.append(post_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if gate_status is not None:
        clauses.append("gate_status = ?")
        params.append(gate_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.ops.execute(
        f"SELECT *, rowid AS _rowid FROM feed_post_translation {where} ORDER BY _rowid DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]


def find_untranslated_posts(
    store: Store,
    *,
    thread_id: str | None = None,
    translator_version: str = CURRENT_TRANSLATOR_VERSION,
    limit: int | None = None,
) -> list[str]:
    """Post ids with no ``status='current'`` translation at
    ``translator_version`` -- the ``--pending`` target set, and the job
    handler's auto-discovery.

    A post whose only current translation FAILED the gate counts as
    translated for this purpose, deliberately: re-running the same
    backend over the same body would produce the same failing text and
    burn the same budget. Re-translating a failure is an explicit act
    (name the post, or bump :data:`CURRENT_TRANSLATOR_VERSION` after
    fixing the contract), never something a ``--pending`` sweep does on
    its own.
    """
    clauses = ["1 = 1"]
    params: list[Any] = []
    if thread_id is not None:
        clauses.append("p.thread_id = ?")
        params.append(thread_id)
    sql = f"""
        SELECT p.post_id FROM feed_post p
        WHERE {' AND '.join(clauses)}
          AND NOT EXISTS (
            SELECT 1 FROM feed_post_translation t
            WHERE t.post_id = p.post_id AND t.status = 'current' AND t.translator_version = ?
          )
        ORDER BY p.ts ASC, p.rowid ASC
    """
    params.append(translator_version)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [r["post_id"] for r in store.ops.execute(sql, params).fetchall()]


def count_gate_failures(store: Store) -> int:
    """How many CURRENT translations were withheld by the gate. The
    predicate behind the ``feed_translation_failures`` doctor check --
    spelled out here and re-stated as raw SQL there, the same deliberate
    two-implementations-one-prose-contract arrangement
    :func:`trialerror.summarize.api.find_stale_or_missing_document_summaries`
    documents (a doctor check opens one read-only connection, never a full
    four-DB :class:`Store`)."""
    row = store.ops.execute(
        "SELECT COUNT(*) AS n FROM feed_post_translation WHERE status = 'current' AND gate_status = 'fail'"
    ).fetchone()
    return int(row["n"])
