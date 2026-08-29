"""The KG extraction stage (design Section 6 stage 8) + the merge-review
queue (design Section 11 v1 deliverable: "full entity/relation extraction +
merge review + graph retrieval tier").

**LLM-judgment boundary** (:mod:`trialerror.verify.hypothesis` / :mod:`trialerror.rooms.api`'s
own pattern, restated for extraction -- stated once per subsystem, applies
here too): this module never calls an LLM. Real per-chunk entity/relation/
claim extraction is a judgment call executed by an agent at runtime; this
module SHAPES that work (:func:`build_extraction_judgment_envelope`) and
accepts a ``judge`` callable (``judge(envelope) -> {"entities": [...],
"relations": [...], "claims": [...]}``), exactly the
``run_hypothesis_verification(..., judge=...)`` shape -- a deterministic
fake fills it in tests, a real subagent fills it at runtime via ``trialerror
extract run --judgments-file`` (disk-to-disk, C-0007: "page text never
transits the orchestrator's context") or the ``extract`` job handler's
``judgments_path`` payload key (:func:`trialerror.ingest.handlers.run_extract`).

**Merge-review queue -- never silent auto-merge** (the cognee/sift-kg
lesson, mission brief verbatim): extraction never writes directly into
``entity``/``relation``/``claim``. Every candidate lands as one PENDING
``record`` row (``register_key=EXTRACT_REGISTER_KEY``) -- design Section
4.1 already names ``record`` as the schema-now landing zone for exactly
this shape ("structured landing ... row-per-row with column structure
preserved"), so this reuses it rather than adding a table (schema is out
of this lane's ownership -- ``trialerror/stores/schema/`` is the schemav2
lane's file). :func:`accept_candidate`/:func:`reject_candidate` (and the
CLI's ``trialerror extract accept/reject``) are the ONLY path from a pending
``record`` row to a real ``entity``/``relation``/``claim`` row; accept
always calls :func:`trialerror.stores.bitemporal.assert_fact` for
relation/claim (bi-temporal, per the mission: "accepted rows written
bi-temporally (assert_fact) with anchors") and mints a real, freshly-
written ``quote_anchor`` row at ACCEPT time -- never at extraction/queue
time, so a rejected candidate never litters the anchor table.

**Entity dedup**: at QUEUE time (:func:`run_extract_chunk`), a candidate
entity is checked by exact ``(name, entity_type)`` match against existing
CONFIRMED entities; a hit is recorded on the pending record as
``dedup_of_entity_id`` (informational -- accept still decides, never
auto-applied). At ACCEPT time, a dedup hit produces the candidate entity
at ``resolution='draft'`` plus one ``merge_proposal`` row
(``status='draft'``) naming the suspected canonical entity --
:func:`accept_merge_proposal`/:func:`reject_merge_proposal` are this
module's explicit accept/reject surface over THAT decision too (never
auto-applied; ``trialerror.stores.schema.knowledge``'s own comment: "apply-
merges touches only confirmed rows"). No dedup hit -> the candidate entity
is inserted directly at ``resolution='confirmed'`` (the accept action IS
the confirmation). The CLI's single ``--id`` accept/reject surface
dispatches on the id's typed prefix (``RCD-`` -> a candidate,
``PROP-`` -> a merge proposal) so both queues share one command.

**Relation acceptance ordering**: a relation candidate's ``src``/``dst``
are entity NAMES, resolved against EXISTING ``entity`` rows only at ACCEPT
time (never auto-created) -- accepting a relation whose endpoint entity
candidate(s) have not themselves been accepted yet raises
:class:`~trialerror.ingest.errors.UnresolvedEntityReferenceError` naming the
missing entity, rather than silently creating a placeholder entity or
resolving to the wrong same-named row.

**TRIALERROR-DEV-NOTE (partial-chunk-failure tolerance, a deliberate v1
tradeoff)**: :func:`run_extract_chunk` inserts one candidate ``record`` row
per entity/relation/claim as it validates each item, not inside one
whole-chunk transaction. If the judge's response is malformed partway
through (e.g. the 2nd relation's quote fails to ground), candidates already
queued for earlier items in that SAME chunk remain queued, but the
chunk-level "processed" event never fires -- a retry re-runs the whole
chunk and may re-queue duplicate pending candidates for the surviving
items. This is a visible, reviewable/rejectable duplication, never a
silent auto-merge or silently-wrong write (the same bar this module holds
everywhere else) -- not a full multi-row transaction, matching how
``trialerror.ingest.handlers.run_chunk`` also commits its chunk rows one at a
time rather than all-or-nothing per document.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from trialerror.events.api import append_event
from trialerror.ingest.anchors import sha256_hex
from trialerror.ingest.errors import (
    CandidateNotFoundError,
    CandidateNotPendingError,
    ChunkNotFoundError,
    ExtractError,
    GroundingError,
    UnresolvedEntityReferenceError,
)
from trialerror.retrieve.wrap import untrusted_wrap
from trialerror.stores.bitemporal import assert_fact
from trialerror.stores.store import Store
from trialerror.stores.writer import get as store_get
from trialerror.stores.writer import insert, update
from trialerror.util.ids import new_id, split_id
from trialerror.util.timeutil import now

__all__ = [
    "EXTRACT_REGISTER_KEY",
    "CLAIM_KINDS",
    "build_extraction_judgment_envelope",
    "run_extract_chunk",
    "run_extract_document",
    "list_pending",
    "get_candidate",
    "accept_candidate",
    "reject_candidate",
    "accept_merge_proposal",
    "reject_merge_proposal",
    "accept",
    "reject",
    "status",
]

#: The ``record.register_key`` this module's merge-review queue rows live
#: under -- design Section 4.1's ``record`` table, unused by any other
#: writer in this codebase (confirmed: it is a schema-now landing zone with
#: no prior reader/writer), so this claims it outright rather than sharing
#: a namespace convention with a future register-import writer.
EXTRACT_REGISTER_KEY = "kg_extract_pending"

#: Mirrors ``claim.kind``'s DDL CHECK domain (design Section 4.1) --
#: duplicated here (not imported from the schema module) the same way
#: ``trialerror.retrieve.engine``'s ``_REQUEST_STATES`` duplicates
#: ``source.request_state``'s domain: both are read-only self-containment
#: choices, pinned to the same constraint so they cannot silently drift.
CLAIM_KINDS: frozenset[str] = frozenset({"finding", "definition", "number", "mechanism", "opinion"})

_CANDIDATE_ID_PREFIX = "RCD"
_MERGE_PROPOSAL_ID_PREFIX = "PROP"


# ---------------------------------------------------------------------------
# judgment envelope
# ---------------------------------------------------------------------------


def build_extraction_judgment_envelope(chunk: Mapping[str, Any], *, doc_title: str | None = None) -> dict[str, Any]:
    """One judgment-request envelope for one chunk -- the
    ``trialerror.verify.hypothesis.build_hypothesis_judgment_envelope`` pattern
    (module docstring's LLM-judgment-boundary note). ``chunk["text"]`` is
    untrusted-wrapped before it reaches the prompt (the same defensive
    posture :func:`trialerror.retrieve.engine.search` already applies to corpus
    text served to any downstream reader, judge included).

    Expected judge response shape: ``{"entities": [{"name", "entity_type",
    "aliases"?, "summary"?, "attributes"?, "confidence"?}, ...], "relations":
    [{"src", "dst", "rel_type", "fact_text", "quote", "confidence"?}, ...],
    "claims": [{"text", "kind", "quote", "confidence"?}, ...]}`` --
    ``src``/``dst`` name an entity from THIS SAME ``entities`` list (or an
    existing entity's name); every relation/claim's ``quote`` MUST be an
    EXACT verbatim substring of ``chunk["text"]`` (:func:`_resolve_quote_anchor_draft`
    refuses otherwise, per :class:`~trialerror.ingest.errors.GroundingError`)."""
    return {
        "kind": "kg_extract",
        "chunk_id": chunk["chunk_id"],
        "doc_title": doc_title,
        "text": untrusted_wrap(chunk["text"]),
        "instructions": (
            "Extract entities, typed relations, and standalone claims grounded ONLY in the "
            "text above. Every relation and claim MUST carry a 'quote' field that is an EXACT "
            "verbatim substring of the text above (word-for-word, same punctuation) -- this is "
            "its evidence anchor; a quote that does not appear verbatim will be refused. Return "
            "{\"entities\": [{\"name\",\"entity_type\",\"aliases\"?,\"summary\"?,\"attributes\"?,"
            "\"confidence\"?}, ...], \"relations\": [{\"src\",\"dst\",\"rel_type\",\"fact_text\","
            "\"quote\",\"confidence\"?}, ...], \"claims\": [{\"text\",\"kind\",\"quote\","
            "\"confidence\"?}, ...]} -- 'src'/'dst' must each name an entity from THIS SAME "
            "'entities' list, or an already-known entity by exact name. 'kind' for a claim must "
            f"be one of {sorted(CLAIM_KINDS)!r}."
        ),
    }


# ---------------------------------------------------------------------------
# extraction -- run_extract_chunk / run_extract_document
# ---------------------------------------------------------------------------


def _primary_anchor(store: Store, chunk_id: str) -> dict[str, Any] | None:
    row = store.knowledge.execute(
        "SELECT * FROM quote_anchor WHERE chunk_id = ? ORDER BY created_ts ASC LIMIT 1", (chunk_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _resolve_quote_anchor_draft(chunk: Mapping[str, Any], chunk_anchor: Mapping[str, Any], quote: str | None) -> dict[str, Any]:
    """A precise ``quote_anchor`` draft for ``quote`` -- an EXACT verbatim
    substring of ``chunk["text"]`` -- by locating it within the chunk's own
    text and offsetting into ``chunk_anchor``'s already-resolved
    ``[char_start, char_end)`` span in the document's canonical
    ``stream_v1`` text (design Section 4.1). Raises
    :class:`~trialerror.ingest.errors.GroundingError` when ``quote`` is missing
    or is not found verbatim -- an extraction can never be evidence-
    anchored to a quote nobody actually wrote."""
    if not quote or not quote.strip():
        raise GroundingError("extraction candidate is missing a 'quote' -- every relation/claim must carry a verbatim quote")
    chunk_text = chunk["text"]
    local_offset = chunk_text.find(quote)
    if local_offset < 0:
        raise GroundingError(
            f"quote {quote[:80]!r}{'...' if len(quote) > 80 else ''} is not a verbatim substring of "
            f"chunk {chunk['chunk_id']!r} -- refused (ungrounded extraction)"
        )
    char_start = chunk_anchor["char_start"] + local_offset
    char_end = char_start + len(quote)
    return {
        "doc_id": chunk["doc_id"],
        "chunk_id": chunk["chunk_id"],
        "page_number": chunk.get("page_start"),
        "char_start": char_start,
        "char_end": char_end,
        "stream_fn": chunk_anchor["stream_fn"],
        "doc_sha256": chunk_anchor["doc_sha256"],
        "quote_sha256": sha256_hex(quote),
        "quote_text": quote,
    }


def _find_confirmed_entity(store: Store, *, name: str, entity_type: str) -> dict[str, Any] | None:
    row = store.knowledge.execute(
        "SELECT * FROM entity WHERE name = ? AND entity_type = ? AND resolution = 'confirmed' LIMIT 1",
        (name, entity_type),
    ).fetchone()
    return dict(row) if row is not None else None


def _next_seq(store: Store, register_key: str) -> int:
    row = store.knowledge.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM record WHERE register_key = ?", (register_key,)).fetchone()
    return int(row["n"])


def _queue_candidate(store: Store, payload: dict[str, Any], *, by_launch: str) -> dict[str, Any]:
    row = {
        "record_id": new_id(_CANDIDATE_ID_PREFIX),
        "register_key": EXTRACT_REGISTER_KEY,
        "artifact_id": None,
        "seq": _next_seq(store, EXTRACT_REGISTER_KEY),
        "payload": json.dumps(payload, ensure_ascii=False),
        "anchors": None,
        "created_ts": now(),
    }
    written = insert(store, "record", row)
    append_event(
        store,
        event_type="kg_candidate_queued",
        payload={"record_id": written["record_id"], "kind": payload["kind"], "chunk_id": payload.get("chunk_id")},
        launch_id=by_launch,
    )
    return written


def _validate_extraction_response(response: Any) -> dict[str, list[Any]]:
    if not isinstance(response, Mapping):
        raise ExtractError(f"judge response for kg_extract must be a mapping, got {type(response).__name__}")
    out: dict[str, list[Any]] = {}
    for key in ("entities", "relations", "claims"):
        value = response.get(key, [])
        if not isinstance(value, list):
            raise ExtractError(f"judge response {key!r} must be a list, got {type(value).__name__}")
        out[key] = value
    return out


def run_extract_chunk(store: Store, chunk_id: str, *, judge: Callable[[Mapping[str, Any]], Any], created_by_launch: str) -> dict[str, Any]:
    """Extract one chunk: build its judgment envelope, call ``judge``,
    validate + ground every candidate's quote, and queue each as one
    PENDING ``record`` row (module docstring). Returns
    ``{"chunk_id", "entities_queued", "relations_queued", "claims_queued",
    "record_ids": {"entities": [...], "relations": [...], "claims": [...]}}``.

    Emits ONE ``kg_extract_chunk_processed`` event on success (payload
    carries the chunk id + the three queued counts) -- this is what makes
    :func:`run_extract_document`'s re-run idempotent (design Section 6:
    "each idempotent ... resumable"): a chunk that already has this event
    is skipped on a subsequent call, the SAME "re-derive what's already
    durably written from the store itself" convention
    ``trialerror.ingest.handlers``'s own module docstring documents."""
    chunk = store_get(store, "chunk", pk_column="chunk_id", pk_value=chunk_id)
    if chunk is None:
        raise ChunkNotFoundError(f"run_extract_chunk: no such chunk {chunk_id!r}")
    anchor = _primary_anchor(store, chunk_id)
    if anchor is None:
        raise ExtractError(f"run_extract_chunk: chunk {chunk_id!r} has no quote_anchor yet -- run ingest through the 'chunk' stage first")

    doc = store_get(store, "document", pk_column="doc_id", pk_value=chunk["doc_id"])
    source = store_get(store, "source", pk_column="source_id", pk_value=doc["source_id"]) if doc else None
    envelope = build_extraction_judgment_envelope(chunk, doc_title=(source["title"] if source else None))
    response = judge(envelope)
    parsed = _validate_extraction_response(response)

    record_ids: dict[str, list[str]] = {"entities": [], "relations": [], "claims": []}

    for ent in parsed["entities"]:
        name = str(ent.get("name") or "").strip()
        if not name:
            raise ExtractError(f"run_extract_chunk: an entity candidate for chunk {chunk_id!r} is missing a non-empty 'name'")
        entity_type = str(ent.get("entity_type") or "unknown").strip() or "unknown"
        dedup_match = _find_confirmed_entity(store, name=name, entity_type=entity_type)
        payload = {
            "kind": "entity",
            "status": "pending",
            "chunk_id": chunk_id,
            "doc_id": chunk["doc_id"],
            "name": name,
            "entity_type": entity_type,
            "aliases": ent.get("aliases"),
            "summary": ent.get("summary"),
            "attributes": ent.get("attributes"),
            "confidence": ent.get("confidence"),
            "dedup_of_entity_id": dedup_match["entity_id"] if dedup_match else None,
        }
        record_ids["entities"].append(_queue_candidate(store, payload, by_launch=created_by_launch)["record_id"])

    for rel in parsed["relations"]:
        for field in ("src", "dst", "rel_type", "fact_text"):
            if not rel.get(field):
                raise ExtractError(f"run_extract_chunk: a relation candidate for chunk {chunk_id!r} is missing required field {field!r}")
        anchor_draft = _resolve_quote_anchor_draft(chunk, anchor, rel.get("quote"))
        payload = {
            "kind": "relation",
            "status": "pending",
            "chunk_id": chunk_id,
            "doc_id": chunk["doc_id"],
            "src_name": rel["src"],
            "dst_name": rel["dst"],
            "rel_type": rel["rel_type"],
            "fact_text": rel["fact_text"],
            "confidence": rel.get("confidence"),
            "anchor_draft": anchor_draft,
        }
        record_ids["relations"].append(_queue_candidate(store, payload, by_launch=created_by_launch)["record_id"])

    for cl in parsed["claims"]:
        text = cl.get("text")
        if not text:
            raise ExtractError(f"run_extract_chunk: a claim candidate for chunk {chunk_id!r} is missing required field 'text'")
        kind = cl.get("kind", "finding")
        if kind not in CLAIM_KINDS:
            raise ExtractError(f"run_extract_chunk: claim kind {kind!r} is not one of {sorted(CLAIM_KINDS)!r}")
        anchor_draft = _resolve_quote_anchor_draft(chunk, anchor, cl.get("quote"))
        payload = {
            "kind": "claim",
            "status": "pending",
            "chunk_id": chunk_id,
            "doc_id": chunk["doc_id"],
            "text": text,
            "claim_kind": kind,
            "confidence": cl.get("confidence"),
            "anchor_draft": anchor_draft,
        }
        record_ids["claims"].append(_queue_candidate(store, payload, by_launch=created_by_launch)["record_id"])

    totals = {
        "chunk_id": chunk_id,
        "entities_queued": len(record_ids["entities"]),
        "relations_queued": len(record_ids["relations"]),
        "claims_queued": len(record_ids["claims"]),
        "record_ids": record_ids,
    }
    append_event(
        store,
        event_type="kg_extract_chunk_processed",
        payload={
            "chunk_id": chunk_id,
            "doc_id": chunk["doc_id"],
            "entities_queued": totals["entities_queued"],
            "relations_queued": totals["relations_queued"],
            "claims_queued": totals["claims_queued"],
        },
        launch_id=created_by_launch,
    )
    return totals


def _already_processed_chunk_ids(store: Store) -> set[str]:
    out: set[str] = set()
    for r in store.ops.execute("SELECT payload FROM event WHERE type = 'kg_extract_chunk_processed'"):
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        cid = payload.get("chunk_id") if isinstance(payload, Mapping) else None
        if cid:
            out.add(cid)
    return out


def run_extract_document(
    store: Store,
    doc_id: str,
    *,
    judge: Callable[[Mapping[str, Any]], Any],
    created_by_launch: str,
    on_chunk: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Extract every not-yet-processed chunk of ``doc_id`` (design Section
    6: "resumable via the jobs ledger" -- a chunk already carrying a
    ``kg_extract_chunk_processed`` event, per :func:`_already_processed_chunk_ids`,
    is skipped, so a resumed/re-run call never re-judges the same chunk
    twice). ``on_chunk(totals)`` (if given) is called after each chunk with
    the running totals so far -- :func:`trialerror.ingest.handlers.run_extract`
    uses this to call ``ctx.set_checkpoint`` per chunk."""
    chunk_ids = [r["chunk_id"] for r in store.knowledge.execute("SELECT chunk_id FROM chunk WHERE doc_id = ? ORDER BY seq", (doc_id,))]
    already = _already_processed_chunk_ids(store)
    totals = {"chunks_processed": 0, "chunks_skipped": 0, "entities_queued": 0, "relations_queued": 0, "claims_queued": 0}
    per_chunk: list[dict[str, Any]] = []
    for chunk_id in chunk_ids:
        if chunk_id in already:
            totals["chunks_skipped"] += 1
            continue
        result = run_extract_chunk(store, chunk_id, judge=judge, created_by_launch=created_by_launch)
        per_chunk.append(result)
        totals["chunks_processed"] += 1
        totals["entities_queued"] += result["entities_queued"]
        totals["relations_queued"] += result["relations_queued"]
        totals["claims_queued"] += result["claims_queued"]
        if on_chunk is not None:
            on_chunk(dict(totals))
    return {"doc_id": doc_id, **totals, "per_chunk": per_chunk}


# ---------------------------------------------------------------------------
# merge-review queue -- list / accept / reject
# ---------------------------------------------------------------------------


def _load_candidate(store: Store, record_id: str) -> dict[str, Any] | None:
    row = store_get(store, "record", pk_column="record_id", pk_value=record_id)
    if row is None or row.get("register_key") != EXTRACT_REGISTER_KEY:
        return None
    return {**row, "payload": json.loads(row["payload"])}


def list_pending(store: Store, *, kind: str | None = None, doc_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Every PENDING extraction candidate (optionally filtered by
    ``kind``/``doc_id``) plus every DRAFT merge proposal -- the full
    merge-review queue :func:`trialerror.cli.extract._cmd_review` renders."""
    candidates: list[dict[str, Any]] = []
    rows = store.knowledge.execute(
        "SELECT * FROM record WHERE register_key = ? ORDER BY seq", (EXTRACT_REGISTER_KEY,)
    ).fetchall()
    for r in rows:
        payload = json.loads(r["payload"])
        if payload.get("status") != "pending":
            continue
        if kind is not None and payload.get("kind") != kind:
            continue
        if doc_id is not None and payload.get("doc_id") != doc_id:
            continue
        candidates.append({**dict(r), "payload": payload})
        if len(candidates) >= limit:
            break
    proposals = [dict(r) for r in store.knowledge.execute("SELECT * FROM merge_proposal WHERE status = 'draft' ORDER BY prop_id")]
    return {"candidates": candidates, "merge_proposals": proposals}


def get_candidate(store: Store, record_id: str) -> dict[str, Any] | None:
    return _load_candidate(store, record_id)


def _mint_anchor(store: Store, anchor_draft: Mapping[str, Any], *, by_launch: str) -> dict[str, Any]:
    return insert(
        store, "quote_anchor",
        {"anchor_id": new_id("ANC"), **anchor_draft, "created_by_launch": by_launch, "created_ts": now()},
    )


def _resolve_entity_by_name(store: Store, name: str) -> dict[str, Any] | None:
    row = store.knowledge.execute("SELECT * FROM entity WHERE name = ? ORDER BY created_at ASC LIMIT 1", (name,)).fetchone()
    return dict(row) if row is not None else None


def _accept_entity_candidate(store: Store, payload: Mapping[str, Any], *, by_launch: str) -> dict[str, Any]:
    dedup_id = payload.get("dedup_of_entity_id")
    entity_id = new_id("ENT")
    insert(
        store, "entity",
        {
            "entity_id": entity_id,
            "name": payload["name"],
            "entity_type": payload["entity_type"],
            "aliases": json.dumps(payload["aliases"], ensure_ascii=False) if payload.get("aliases") else None,
            "summary": payload.get("summary"),
            "attributes": json.dumps(payload["attributes"], ensure_ascii=False) if payload.get("attributes") else None,
            "resolution": "draft" if dedup_id else "confirmed",
            "merge_group": None,
            "created_by_launch": by_launch,
            "created_at": now(),
        },
    )
    prop_id = None
    if dedup_id:
        prop_id = new_id(_MERGE_PROPOSAL_ID_PREFIX)
        insert(
            store, "merge_proposal",
            {
                "prop_id": prop_id,
                "canonical_entity": dedup_id,
                "members": json.dumps([entity_id], ensure_ascii=False),
                "reason": f"extraction dedup: candidate {payload['name']!r} ({payload['entity_type']!r}) exact-matches confirmed entity {dedup_id}",
                "status": "draft",
                "proposed_by_launch": by_launch,
                "decided_by": None,
                "decided_ts": None,
            },
        )
    return {
        "record_stamp": {"resolved_entity_id": entity_id, "merge_proposal_id": prop_id},
        "event_extra": {"entity_id": entity_id, "merge_proposal_id": prop_id},
        "public": {"entity_id": entity_id, "resolution": "draft" if dedup_id else "confirmed", "merge_proposal_id": prop_id},
    }


def _accept_relation_candidate(store: Store, payload: Mapping[str, Any], *, by_launch: str) -> dict[str, Any]:
    src_entity = _resolve_entity_by_name(store, payload["src_name"])
    dst_entity = _resolve_entity_by_name(store, payload["dst_name"])
    if src_entity is None or dst_entity is None:
        missing = payload["src_name"] if src_entity is None else payload["dst_name"]
        raise UnresolvedEntityReferenceError(
            f"relation candidate references entity {missing!r}, which has no accepted entity row yet -- "
            "accept the referenced entity candidate(s) first"
        )
    anchor_row = _mint_anchor(store, payload["anchor_draft"], by_launch=by_launch)
    rel_id = new_id("REL")
    written = assert_fact(
        store, "relation",
        {
            "rel_id": rel_id,
            "src_entity": src_entity["entity_id"],
            "dst_entity": dst_entity["entity_id"],
            "rel_type": payload["rel_type"],
            "fact_text": payload["fact_text"],
            "evidence_anchor": anchor_row["anchor_id"],
            "extra_anchors": None,
            "confidence": payload.get("confidence"),
        },
    )
    return {
        "record_stamp": {"resolved_rel_id": rel_id, "evidence_anchor": anchor_row["anchor_id"]},
        "event_extra": {"rel_id": rel_id},
        "public": {"rel_id": rel_id, "relation": written},
    }


def _accept_claim_candidate(store: Store, payload: Mapping[str, Any], *, by_launch: str) -> dict[str, Any]:
    anchor_row = _mint_anchor(store, payload["anchor_draft"], by_launch=by_launch)
    claim_id = new_id("CLM")
    written = assert_fact(
        store, "claim",
        {
            "claim_id": claim_id,
            "text": payload["text"],
            "kind": payload["claim_kind"],
            "confidence": payload.get("confidence"),
            "anchor_id": anchor_row["anchor_id"],
            "extra_anchors": None,
            "created_by_launch": by_launch,
        },
    )
    return {
        "record_stamp": {"resolved_claim_id": claim_id, "evidence_anchor": anchor_row["anchor_id"]},
        "event_extra": {"claim_id": claim_id},
        "public": {"claim_id": claim_id, "claim": written},
    }


def accept_candidate(store: Store, record_id: str, *, by_launch: str) -> dict[str, Any]:
    """Promote one PENDING candidate to a real ``entity``/``relation``/
    ``claim`` row (module docstring). Refuses
    (:class:`~trialerror.ingest.errors.CandidateNotFoundError`/
    :class:`~trialerror.ingest.errors.CandidateNotPendingError`) for an unknown
    or already-decided candidate."""
    candidate = _load_candidate(store, record_id)
    if candidate is None:
        raise CandidateNotFoundError(f"no such extraction candidate: {record_id!r}")
    payload = candidate["payload"]
    if payload.get("status") != "pending":
        raise CandidateNotPendingError(f"candidate {record_id!r} is not pending (status={payload.get('status')!r})")

    kind = payload["kind"]
    if kind == "entity":
        result = _accept_entity_candidate(store, payload, by_launch=by_launch)
    elif kind == "relation":
        result = _accept_relation_candidate(store, payload, by_launch=by_launch)
    elif kind == "claim":
        result = _accept_claim_candidate(store, payload, by_launch=by_launch)
    else:
        raise ExtractError(f"accept_candidate: unknown candidate kind {kind!r}")

    new_payload = {**payload, "status": "accepted", **result["record_stamp"]}
    update(store, "record", pk_column="record_id", pk_value=record_id, changes={"payload": json.dumps(new_payload, ensure_ascii=False)})
    append_event(
        store, event_type="kg_candidate_accepted",
        payload={"record_id": record_id, "kind": kind, **result["event_extra"]}, launch_id=by_launch,
    )
    return {"record_id": record_id, "kind": kind, **result["public"]}


def reject_candidate(store: Store, record_id: str, *, by_launch: str, reason: str | None = None) -> dict[str, Any]:
    """Mark one PENDING candidate ``rejected`` -- nothing is written to
    ``entity``/``relation``/``claim``."""
    candidate = _load_candidate(store, record_id)
    if candidate is None:
        raise CandidateNotFoundError(f"no such extraction candidate: {record_id!r}")
    payload = candidate["payload"]
    if payload.get("status") != "pending":
        raise CandidateNotPendingError(f"candidate {record_id!r} is not pending (status={payload.get('status')!r})")
    new_payload = {**payload, "status": "rejected", "reject_reason": reason}
    update(store, "record", pk_column="record_id", pk_value=record_id, changes={"payload": json.dumps(new_payload, ensure_ascii=False)})
    append_event(
        store, event_type="kg_candidate_rejected",
        payload={"record_id": record_id, "kind": payload["kind"], "reason": reason}, launch_id=by_launch,
    )
    return {"record_id": record_id, "kind": payload["kind"], "status": "rejected"}


# ---------------------------------------------------------------------------
# merge-review queue -- accept/reject a merge_proposal directly
# ---------------------------------------------------------------------------


def accept_merge_proposal(store: Store, prop_id: str, *, by_launch: str) -> dict[str, Any]:
    """Confirm a DRAFT merge proposal: the proposal's ``status`` becomes
    ``confirmed`` and every member entity's ``resolution`` becomes
    ``confirmed`` with ``merge_group`` set to the canonical entity (design
    Section 4.1: "apply-merges touches only confirmed rows")."""
    prop = store_get(store, "merge_proposal", pk_column="prop_id", pk_value=prop_id)
    if prop is None:
        raise CandidateNotFoundError(f"no such merge_proposal: {prop_id!r}")
    if prop["status"] != "draft":
        raise CandidateNotPendingError(f"merge_proposal {prop_id!r} is not draft (status={prop['status']!r})")
    members = json.loads(prop["members"])
    update(store, "merge_proposal", pk_column="prop_id", pk_value=prop_id, changes={"status": "confirmed", "decided_by": by_launch, "decided_ts": now()})
    for member_id in members:
        update(store, "entity", pk_column="entity_id", pk_value=member_id, changes={"resolution": "confirmed", "merge_group": prop["canonical_entity"]})
    append_event(
        store, event_type="merge_proposal_accepted",
        payload={"prop_id": prop_id, "canonical_entity": prop["canonical_entity"], "members": members}, launch_id=by_launch,
    )
    return {"prop_id": prop_id, "status": "confirmed", "canonical_entity": prop["canonical_entity"], "members": members}


def reject_merge_proposal(store: Store, prop_id: str, *, by_launch: str) -> dict[str, Any]:
    """Decline a DRAFT merge proposal: the proposal's ``status`` becomes
    ``rejected``; every member entity's ``resolution`` becomes
    ``confirmed`` WITHOUT a ``merge_group`` (the entity is determined to be
    genuinely distinct, not invalid -- ``resolution='rejected'`` would
    instead mean "this entity itself shouldn't exist", which a declined
    merge suggestion does not imply)."""
    prop = store_get(store, "merge_proposal", pk_column="prop_id", pk_value=prop_id)
    if prop is None:
        raise CandidateNotFoundError(f"no such merge_proposal: {prop_id!r}")
    if prop["status"] != "draft":
        raise CandidateNotPendingError(f"merge_proposal {prop_id!r} is not draft (status={prop['status']!r})")
    members = json.loads(prop["members"])
    update(store, "merge_proposal", pk_column="prop_id", pk_value=prop_id, changes={"status": "rejected", "decided_by": by_launch, "decided_ts": now()})
    for member_id in members:
        update(store, "entity", pk_column="entity_id", pk_value=member_id, changes={"resolution": "confirmed"})
    append_event(store, event_type="merge_proposal_rejected", payload={"prop_id": prop_id, "members": members}, launch_id=by_launch)
    return {"prop_id": prop_id, "status": "rejected", "members": members}


# ---------------------------------------------------------------------------
# accept / reject -- ONE surface dispatching on the id's typed prefix
# (RCD-... -> a candidate, PROP-... -> a merge proposal), the CLI's own
# `--id` argument routes straight through here.
# ---------------------------------------------------------------------------


def accept(store: Store, item_id: str, *, by_launch: str) -> dict[str, Any]:
    prefix, _ = split_id(item_id)
    if prefix == _CANDIDATE_ID_PREFIX:
        return accept_candidate(store, item_id, by_launch=by_launch)
    if prefix == _MERGE_PROPOSAL_ID_PREFIX:
        return accept_merge_proposal(store, item_id, by_launch=by_launch)
    raise ExtractError(f"accept: {item_id!r} is neither a candidate ({_CANDIDATE_ID_PREFIX}-...) nor a merge_proposal ({_MERGE_PROPOSAL_ID_PREFIX}-...)")


def reject(store: Store, item_id: str, *, by_launch: str, reason: str | None = None) -> dict[str, Any]:
    prefix, _ = split_id(item_id)
    if prefix == _CANDIDATE_ID_PREFIX:
        return reject_candidate(store, item_id, by_launch=by_launch, reason=reason)
    if prefix == _MERGE_PROPOSAL_ID_PREFIX:
        return reject_merge_proposal(store, item_id, by_launch=by_launch)
    raise ExtractError(f"reject: {item_id!r} is neither a candidate ({_CANDIDATE_ID_PREFIX}-...) nor a merge_proposal ({_MERGE_PROPOSAL_ID_PREFIX}-...)")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status(store: Store) -> dict[str, Any]:
    """Summary counts for ``trialerror extract status`` / the
    ``extract_pending_backlog`` doctor check's own reasoning."""
    counts: dict[str, dict[str, int]] = {
        "entity": {"pending": 0, "accepted": 0, "rejected": 0},
        "relation": {"pending": 0, "accepted": 0, "rejected": 0},
        "claim": {"pending": 0, "accepted": 0, "rejected": 0},
    }
    for r in store.knowledge.execute("SELECT payload FROM record WHERE register_key = ?", (EXTRACT_REGISTER_KEY,)):
        payload = json.loads(r["payload"])
        k, s = payload.get("kind"), payload.get("status")
        if k in counts and s in counts[k]:
            counts[k][s] += 1
    proposal_counts = {"draft": 0, "confirmed": 0, "rejected": 0}
    for r in store.knowledge.execute("SELECT status FROM merge_proposal"):
        if r["status"] in proposal_counts:
            proposal_counts[r["status"]] += 1
    return {"candidates": counts, "merge_proposals": proposal_counts}
