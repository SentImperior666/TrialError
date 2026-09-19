"""Tests for :mod:`trialerror.ingest.extract` -- the KG extraction pipeline +
merge-review queue (design Section 11 v1 deliverable 1), driven with
deterministic fake judges (the ``trialerror.verify.hypothesis`` pattern --
this module never calls an LLM itself, see its own module docstring).

Reuses ``tests._retrieve_fixtures.build_small_corpus`` (a landed, non-
concurrent-lane fixture builder -- the same precedent
``tests/_verify_fixtures.py``'s own docstring states for reusing a
DIFFERENT build's fixture module once it has landed, as opposed to a
CONCURRENTLY-being-built one) rather than re-deriving a second copy of the
same chunker/anchor-building plumbing.
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest import extract as extract_api
from trialerror.ingest.errors import (
    CandidateNotFoundError,
    CandidateNotPendingError,
    ChunkNotFoundError,
    ExtractError,
    GroundingError,
    UnresolvedEntityReferenceError,
)
from trialerror.retrieve import engine
from trialerror.stores.bitemporal import as_of
from trialerror.stores.writer import get as store_get

from tests._retrieve_fixtures import build_small_corpus

# ---------------------------------------------------------------------------
# fixture-derived, VERBATIM quotes -- build_small_corpus's own fixed prose,
# copied here so every "quote" this file's fake judges return is an exact
# substring of the real fixture chunk text (grounding-correct by
# construction, not by luck).
# ---------------------------------------------------------------------------

_OPEN_SENTENCE_1 = "Distributed schedulers use retry budgets to bound tail latency during failover."
_OPEN_SENTENCE_2 = "A coordinator arbitrates lock conflicts and records the consequences of worker actions."
_RESTRICTED_LONG_PREFIX = (
    "This paragraph belongs to a commercial rulebook and is intentionally long so that fencing it "
    "down to twenty words is a meaningful, observable transformation rather than a no-op"
)
assert len(_RESTRICTED_LONG_PREFIX.split()) > 20  # the fixture test is meaningless otherwise


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


def _open_chunk_id(corpus):
    return corpus["open_chunk_ids"][0]


def _restricted_chunk_id(corpus):
    return corpus["restricted_chunk_ids"][0]


def _fake_judge(corpus, *, calls: list[str] | None = None):
    """A deterministic fake judge: OPEN chunk -> 2 entities/1 relation/1
    claim; RESTRICTED chunk -> 2 entities/1 relation, every quote a real
    verbatim substring of the fixture text. ``calls`` (if given) records
    every ``chunk_id`` the judge is actually invoked for -- the idempotency
    tests use this to prove a re-run skips already-processed chunks."""
    open_id = _open_chunk_id(corpus)
    restricted_id = _restricted_chunk_id(corpus)

    def judge(envelope):
        chunk_id = envelope["chunk_id"]
        if calls is not None:
            calls.append(chunk_id)
        if chunk_id == open_id:
            return {
                "entities": [
                    {"name": "Coordinator", "entity_type": "role", "confidence": 0.9},
                    {"name": "Retry Budget", "entity_type": "mechanism", "confidence": 0.8},
                ],
                "relations": [
                    {
                        "src": "Coordinator", "dst": "Retry Budget", "rel_type": "uses",
                        "fact_text": "A coordinator oversees retry budget resolution.",
                        "quote": _OPEN_SENTENCE_2, "confidence": 0.7,
                    }
                ],
                "claims": [
                    {"text": "Retry budgets bound tail latency.", "kind": "mechanism", "quote": _OPEN_SENTENCE_1, "confidence": 0.6}
                ],
            }
        if chunk_id == restricted_id:
            return {
                "entities": [
                    {"name": "Epoch Counter", "entity_type": "mechanism"},
                    {"name": "Quorum Reconfiguration", "entity_type": "mechanism"},
                ],
                "relations": [
                    {
                        "src": "Epoch Counter", "dst": "Quorum Reconfiguration", "rel_type": "part_of",
                        "fact_text": "An epoch counter is invoked during quorum reconfiguration.",
                        "quote": _RESTRICTED_LONG_PREFIX, "confidence": 0.5,
                    }
                ],
                "claims": [],
            }
        raise AssertionError(f"unexpected chunk_id {chunk_id!r}")

    return judge


# ---------------------------------------------------------------------------
# build_extraction_judgment_envelope
# ---------------------------------------------------------------------------


def test_envelope_wraps_untrusted_chunk_text_and_carries_chunk_id(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    chunk = store_get(store, "chunk", pk_column="chunk_id", pk_value=_open_chunk_id(corpus))
    envelope = extract_api.build_extraction_judgment_envelope(chunk, doc_title="Fixture Doc")
    assert envelope["kind"] == "kg_extract"
    assert envelope["chunk_id"] == chunk["chunk_id"]
    assert envelope["doc_title"] == "Fixture Doc"
    assert envelope["text"].startswith(UNTRUSTED_OPEN)
    assert envelope["text"].endswith(UNTRUSTED_CLOSE)
    assert _OPEN_SENTENCE_1 in envelope["text"]
    assert "instructions" in envelope


# ---------------------------------------------------------------------------
# run_extract_chunk -- envelope round trip w/ fake judge
# ---------------------------------------------------------------------------


def test_run_extract_chunk_queues_entities_relations_claims_as_pending(store, corpus):
    judge = _fake_judge(corpus)
    result = extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=judge, created_by_launch=corpus["launch_id"])

    assert result["entities_queued"] == 2
    assert result["relations_queued"] == 1
    assert result["claims_queued"] == 1
    assert len(result["record_ids"]["entities"]) == 2
    assert len(result["record_ids"]["relations"]) == 1
    assert len(result["record_ids"]["claims"]) == 1

    # nothing landed in entity/relation/claim yet -- pending only
    assert store.knowledge.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0
    assert store.knowledge.execute("SELECT COUNT(*) FROM relation").fetchone()[0] == 0
    assert store.knowledge.execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 0

    for rid in result["record_ids"]["entities"] + result["record_ids"]["relations"] + result["record_ids"]["claims"]:
        candidate = extract_api.get_candidate(store, rid)
        assert candidate is not None
        assert candidate["payload"]["status"] == "pending"
        assert candidate["register_key"] == extract_api.EXTRACT_REGISTER_KEY

    # a kg_extract_chunk_processed event was emitted (idempotency marker)
    events = [dict(r) for r in store.ops.execute("SELECT * FROM event WHERE type='kg_extract_chunk_processed'")]
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["chunk_id"] == _open_chunk_id(corpus)


def test_run_extract_chunk_no_such_chunk_raises(store, corpus):
    with pytest.raises(ChunkNotFoundError):
        extract_api.run_extract_chunk(store, "CHK-does-not-exist", judge=lambda e: {}, created_by_launch=corpus["launch_id"])


def test_run_extract_chunk_bad_judge_response_shape_raises(store, corpus):
    with pytest.raises(ExtractError):
        extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=lambda e: "not a mapping", created_by_launch=corpus["launch_id"])


def test_run_extract_chunk_ungrounded_quote_raises_grounding_error(store, corpus):
    def judge(envelope):
        return {
            "entities": [{"name": "X", "entity_type": "y"}],
            "relations": [{"src": "X", "dst": "X", "rel_type": "r", "fact_text": "f", "quote": "this text never appears in the chunk"}],
            "claims": [],
        }

    with pytest.raises(GroundingError):
        extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=judge, created_by_launch=corpus["launch_id"])


def test_run_extract_chunk_missing_quote_raises_grounding_error(store, corpus):
    def judge(envelope):
        return {"entities": [], "relations": [], "claims": [{"text": "t", "kind": "finding", "quote": ""}]}

    with pytest.raises(GroundingError):
        extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=judge, created_by_launch=corpus["launch_id"])


def test_run_extract_chunk_bad_claim_kind_raises(store, corpus):
    def judge(envelope):
        return {"entities": [], "relations": [], "claims": [{"text": "t", "kind": "not_a_real_kind", "quote": _OPEN_SENTENCE_1}]}

    with pytest.raises(ExtractError):
        extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=judge, created_by_launch=corpus["launch_id"])


# ---------------------------------------------------------------------------
# run_extract_document -- idempotency (resumability)
# ---------------------------------------------------------------------------


def test_run_extract_document_processes_every_chunk_once(store, corpus):
    calls: list[str] = []
    judge = _fake_judge(corpus, calls=calls)

    result_open = extract_api.run_extract_document(store, corpus["open_doc_id"], judge=judge, created_by_launch=corpus["launch_id"])
    assert result_open["chunks_processed"] == 1
    assert result_open["chunks_skipped"] == 0
    assert calls == [_open_chunk_id(corpus)]

    # re-running the SAME document skips the already-processed chunk --
    # the judge must not be called again.
    calls.clear()
    result_again = extract_api.run_extract_document(store, corpus["open_doc_id"], judge=judge, created_by_launch=corpus["launch_id"])
    assert result_again["chunks_processed"] == 0
    assert result_again["chunks_skipped"] == 1
    assert calls == []


def test_run_extract_document_on_chunk_callback_fires_per_chunk(store, corpus):
    seen = []
    extract_api.run_extract_document(
        store, corpus["open_doc_id"], judge=_fake_judge(corpus), created_by_launch=corpus["launch_id"], on_chunk=lambda totals: seen.append(dict(totals))
    )
    assert len(seen) == 1
    assert seen[0]["chunks_processed"] == 1


# ---------------------------------------------------------------------------
# merge-review queue -- list / accept / reject
# ---------------------------------------------------------------------------


def _queue_open_chunk(store, corpus):
    return extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=_fake_judge(corpus), created_by_launch=corpus["launch_id"])


def test_list_pending_and_status_reflect_queued_candidates(store, corpus):
    _queue_open_chunk(store, corpus)
    pending = extract_api.list_pending(store)
    assert len(pending["candidates"]) == 4  # 2 entities + 1 relation + 1 claim
    kinds = sorted(c["payload"]["kind"] for c in pending["candidates"])
    assert kinds == ["claim", "entity", "entity", "relation"]

    st = extract_api.status(store)
    assert st["candidates"]["entity"]["pending"] == 2
    assert st["candidates"]["relation"]["pending"] == 1
    assert st["candidates"]["claim"]["pending"] == 1


def test_accept_candidate_unknown_id_raises(store, corpus):
    with pytest.raises(CandidateNotFoundError):
        extract_api.accept_candidate(store, "RCD-does-not-exist", by_launch=corpus["launch_id"])


def test_accept_entity_candidate_writes_confirmed_entity_no_dedup(store, corpus):
    result = _queue_open_chunk(store, corpus)
    entity_record_id = result["record_ids"]["entities"][0]
    out = extract_api.accept_candidate(store, entity_record_id, by_launch=corpus["launch_id"])
    assert out["kind"] == "entity"
    assert out["resolution"] == "confirmed"
    assert out["merge_proposal_id"] is None

    row = store_get(store, "entity", pk_column="entity_id", pk_value=out["entity_id"])
    assert row is not None
    assert row["resolution"] == "confirmed"

    # re-accepting the SAME (now-accepted) candidate is refused
    with pytest.raises(CandidateNotPendingError):
        extract_api.accept_candidate(store, entity_record_id, by_launch=corpus["launch_id"])

    candidate = extract_api.get_candidate(store, entity_record_id)
    assert candidate["payload"]["status"] == "accepted"
    assert candidate["payload"]["resolved_entity_id"] == out["entity_id"]


def test_accept_entity_candidate_with_dedup_lands_draft_plus_merge_proposal(store, corpus):
    result = _queue_open_chunk(store, corpus)
    gm_record_id = result["record_ids"]["entities"][0]  # "Coordinator"
    first = extract_api.accept_candidate(store, gm_record_id, by_launch=corpus["launch_id"])
    assert first["resolution"] == "confirmed"

    # queue a SECOND extraction that proposes the same (name, entity_type)
    # again -- this time the dedup check should find the just-confirmed row.
    def judge_dup(envelope):
        return {"entities": [{"name": "Coordinator", "entity_type": "role"}], "relations": [], "claims": []}

    dup_result = extract_api.run_extract_chunk(store, _restricted_chunk_id(corpus), judge=judge_dup, created_by_launch=corpus["launch_id"])
    dup_record_id = dup_result["record_ids"]["entities"][0]
    dup_candidate = extract_api.get_candidate(store, dup_record_id)
    assert dup_candidate["payload"]["dedup_of_entity_id"] == first["entity_id"]

    accepted = extract_api.accept_candidate(store, dup_record_id, by_launch=corpus["launch_id"])
    assert accepted["resolution"] == "draft"
    prop_id = accepted["merge_proposal_id"]
    assert prop_id is not None and prop_id.startswith("PROP-")

    prop = store_get(store, "merge_proposal", pk_column="prop_id", pk_value=prop_id)
    assert prop["status"] == "draft"
    assert prop["canonical_entity"] == first["entity_id"]

    # the draft entity itself is not yet confirmed
    new_entity = store_get(store, "entity", pk_column="entity_id", pk_value=accepted["entity_id"])
    assert new_entity["resolution"] == "draft"


def test_accept_merge_proposal_confirms_member_entities(store, corpus):
    result = _queue_open_chunk(store, corpus)
    gm_record_id = result["record_ids"]["entities"][0]
    first = extract_api.accept_candidate(store, gm_record_id, by_launch=corpus["launch_id"])

    def judge_dup(envelope):
        return {"entities": [{"name": "Coordinator", "entity_type": "role"}], "relations": [], "claims": []}

    dup_result = extract_api.run_extract_chunk(store, _restricted_chunk_id(corpus), judge=judge_dup, created_by_launch=corpus["launch_id"])
    dup_record_id = dup_result["record_ids"]["entities"][0]
    accepted = extract_api.accept_candidate(store, dup_record_id, by_launch=corpus["launch_id"])
    prop_id = accepted["merge_proposal_id"]

    out = extract_api.accept_merge_proposal(store, prop_id, by_launch=corpus["launch_id"])
    assert out["status"] == "confirmed"
    member = store_get(store, "entity", pk_column="entity_id", pk_value=accepted["entity_id"])
    assert member["resolution"] == "confirmed"
    assert member["merge_group"] == first["entity_id"]

    with pytest.raises(CandidateNotPendingError):
        extract_api.accept_merge_proposal(store, prop_id, by_launch=corpus["launch_id"])


def test_reject_merge_proposal_confirms_member_as_distinct_no_merge_group(store, corpus):
    result = _queue_open_chunk(store, corpus)
    gm_record_id = result["record_ids"]["entities"][0]
    extract_api.accept_candidate(store, gm_record_id, by_launch=corpus["launch_id"])

    def judge_dup(envelope):
        return {"entities": [{"name": "Coordinator", "entity_type": "role"}], "relations": [], "claims": []}

    dup_result = extract_api.run_extract_chunk(store, _restricted_chunk_id(corpus), judge=judge_dup, created_by_launch=corpus["launch_id"])
    dup_record_id = dup_result["record_ids"]["entities"][0]
    accepted = extract_api.accept_candidate(store, dup_record_id, by_launch=corpus["launch_id"])
    prop_id = accepted["merge_proposal_id"]

    out = extract_api.reject_merge_proposal(store, prop_id, by_launch=corpus["launch_id"])
    assert out["status"] == "rejected"
    member = store_get(store, "entity", pk_column="entity_id", pk_value=accepted["entity_id"])
    assert member["resolution"] == "confirmed"
    assert member["merge_group"] is None


def test_reject_candidate_writes_nothing_and_is_not_reacceptable(store, corpus):
    result = _queue_open_chunk(store, corpus)
    claim_record_id = result["record_ids"]["claims"][0]
    out = extract_api.reject_candidate(store, claim_record_id, by_launch=corpus["launch_id"], reason="not interesting")
    assert out["status"] == "rejected"
    assert store.knowledge.execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 0

    candidate = extract_api.get_candidate(store, claim_record_id)
    assert candidate["payload"]["status"] == "rejected"
    assert candidate["payload"]["reject_reason"] == "not interesting"

    with pytest.raises(CandidateNotPendingError):
        extract_api.reject_candidate(store, claim_record_id, by_launch=corpus["launch_id"])

    with pytest.raises(CandidateNotPendingError):
        extract_api.accept_candidate(store, claim_record_id, by_launch=corpus["launch_id"])


# ---------------------------------------------------------------------------
# accept()/reject() -- single --id surface dispatching on typed prefix
# ---------------------------------------------------------------------------


def test_accept_reject_dispatch_by_id_prefix(store, corpus):
    result = _queue_open_chunk(store, corpus)
    entity_record_id = result["record_ids"]["entities"][1]  # "Retry Budget" -- no dedup
    out = extract_api.accept(store, entity_record_id, by_launch=corpus["launch_id"])
    assert out["kind"] == "entity"

    claim_record_id = result["record_ids"]["claims"][0]
    out2 = extract_api.reject(store, claim_record_id, by_launch=corpus["launch_id"])
    assert out2["status"] == "rejected"


def test_accept_unrecognized_id_prefix_raises(store, corpus):
    with pytest.raises(ExtractError):
        extract_api.accept(store, "QRY-not-a-candidate-or-proposal", by_launch=corpus["launch_id"])


# ---------------------------------------------------------------------------
# relation acceptance ordering -- UnresolvedEntityReferenceError
# ---------------------------------------------------------------------------


def test_accept_relation_before_its_entities_are_accepted_raises(store, corpus):
    result = _queue_open_chunk(store, corpus)
    relation_record_id = result["record_ids"]["relations"][0]
    with pytest.raises(UnresolvedEntityReferenceError):
        extract_api.accept_candidate(store, relation_record_id, by_launch=corpus["launch_id"])


def test_accept_relation_after_its_entities_succeeds_and_is_bitemporal(store, corpus):
    result = _queue_open_chunk(store, corpus)
    for rid in result["record_ids"]["entities"]:
        extract_api.accept_candidate(store, rid, by_launch=corpus["launch_id"])

    relation_record_id = result["record_ids"]["relations"][0]
    out = extract_api.accept_candidate(store, relation_record_id, by_launch=corpus["launch_id"])
    assert out["kind"] == "relation"
    rel_id = out["rel_id"]

    row = store_get(store, "relation", pk_column="rel_id", pk_value=rel_id)
    assert row["src_entity"] is not None and row["dst_entity"] is not None
    assert row["fact_text"] == "A coordinator oversees retry budget resolution."

    # bi-temporal correctness (design Section 11 deliverable 1: "accepted
    # rows written bi-temporally (assert_fact) with anchors")
    assert row["created_at"] is not None
    assert row["expired_at"] is None
    assert row["valid_at"] == row["created_at"]
    assert row["invalid_at"] is None

    live_rows = as_of(store, "relation", where="rel_id = ?", params=(rel_id,))
    assert len(live_rows) == 1
    assert live_rows[0]["rel_id"] == rel_id

    # its evidence anchor is a REAL, freshly-minted quote_anchor row
    anchor = store_get(store, "quote_anchor", pk_column="anchor_id", pk_value=row["evidence_anchor"])
    assert anchor is not None
    assert anchor["quote_text"] == _OPEN_SENTENCE_2
    assert anchor["created_by_launch"] == corpus["launch_id"]


def test_accept_claim_candidate_is_bitemporal_with_real_anchor(store, corpus):
    result = _queue_open_chunk(store, corpus)
    claim_record_id = result["record_ids"]["claims"][0]
    out = extract_api.accept_candidate(store, claim_record_id, by_launch=corpus["launch_id"])
    claim_id = out["claim_id"]

    row = store_get(store, "claim", pk_column="claim_id", pk_value=claim_id)
    assert row["expired_at"] is None
    assert row["invalid_at"] is None
    assert row["created_at"] == row["valid_at"]

    live_rows = as_of(store, "claim", where="claim_id = ?", params=(claim_id,))
    assert len(live_rows) == 1

    anchor = store_get(store, "quote_anchor", pk_column="anchor_id", pk_value=row["anchor_id"])
    assert anchor["quote_text"] == _OPEN_SENTENCE_1


# ---------------------------------------------------------------------------
# FX-4 fence regression on REAL, pipeline-written relation rows (not
# manually planted -- the mission's own explicit ask: "fence regression on
# populated relation rows ... now with REAL extraction-written rows").
# ---------------------------------------------------------------------------


def test_fence_applies_to_a_relation_the_extraction_pipeline_actually_wrote(store, corpus):
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    result = extract_api.run_extract_chunk(store, _restricted_chunk_id(corpus), judge=_fake_judge(corpus), created_by_launch=corpus["launch_id"])
    for rid in result["record_ids"]["entities"]:
        extract_api.accept_candidate(store, rid, by_launch=corpus["launch_id"])
    relation_record_id = result["record_ids"]["relations"][0]
    out = extract_api.accept_candidate(store, relation_record_id, by_launch=corpus["launch_id"])

    neighbors = engine.graph_neighbors(store, out["relation"]["src_entity"])
    assert neighbors["count"] == 1
    edge = neighbors["edges"][0]
    assert edge["fenced"] is True
    assert edge["fact_text"].startswith(UNTRUSTED_OPEN)
    assert edge["fact_text"].endswith(UNTRUSTED_CLOSE)

    # the underlying quote_anchor DOES carry the full >20-word quote (real,
    # precise evidence) -- but the SERVED edge never exceeds the D-COC-1 cap.
    anchor = store_get(store, "quote_anchor", pk_column="anchor_id", pk_value=out["relation"]["evidence_anchor"])
    assert anchor["quote_text"] == _RESTRICTED_LONG_PREFIX
    assert len(anchor["quote_text"].split()) > 20

    k_hop = engine.k_hop_neighbors(store, out["relation"]["src_entity"])
    assert k_hop["count"] == 1
    assert k_hop["edges"][0]["fenced"] is True
