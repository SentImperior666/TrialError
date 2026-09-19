"""``trialerror.verify.faithfulness`` — the Ragas statement-decomposition port
(design Section 11: "hypothesis pipeline hardening ... Ragas faithfulness
port")."""

from __future__ import annotations

import json

import pytest

from trialerror.verify.errors import VerifyError
from trialerror.verify.faithfulness import (
    CLEAN_PASS_STATUSES,
    build_decomposition_envelope,
    run_citecheck_with_faithfulness,
    run_faithfulness,
)
from trialerror.verify.verdicts import record_verdict

from tests._verify_fixtures import anchor_for_chunk, bootstrap_launch, build_small_corpus

_OPEN_SENTENCE = "Distributed schedulers use retry budgets to bound tail latency during failover."


def _text_with_marker(anchor_id: str, sentence: str) -> str:
    return f"{sentence} [[cite:{anchor_id}]]"


# ---------------------------------------------------------------------------
# build_decomposition_envelope
# ---------------------------------------------------------------------------


def test_build_decomposition_envelope_carries_sentence_and_anchor():
    pair = {"pair_id": "CPR-1", "sentence": "Retry budgets bound latency.", "anchor_id": "ANC-x"}
    envelope = build_decomposition_envelope(pair)
    assert envelope["kind"] == "faithfulness_decompose"
    assert envelope["pair_id"] == "CPR-1"
    assert envelope["sentence"] == "Retry budgets bound latency."
    assert envelope["anchor_id"] == "ANC-x"
    assert "instruction" in envelope


# ---------------------------------------------------------------------------
# run_faithfulness: round trip, planted supported/unsupported claims
# ---------------------------------------------------------------------------


def _decompose_into_two(_envelope):
    """A deterministic fake decomposer: every sentence splits into exactly
    two atomic claims -- one that will mechanically match the anchor's own
    quote (planted verbatim substring), one that plainly does not."""
    return {"claims": [_OPEN_SENTENCE, "This is an entirely unrelated claim about coastal erosion patterns."]}


def _verify_supported_only_first(envelope):
    """A deterministic fake verifier: only the FIRST claim (the one that
    should mechanically match anyway) is asked about; this fake is only
    reached for pairs the mechanical pass could NOT settle on its own."""
    return "supported"


def test_run_faithfulness_round_trip_scores_planted_supported_and_unsupported_claims(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = _text_with_marker(anchor["anchor_id"], _OPEN_SENTENCE)

    def verify_judge(envelope):
        # the claim identical to the anchor's own cited quote mechanically
        # passes before this judge is ever consulted; only the unrelated
        # decomposed claim reaches here, and it is planted UNSUPPORTED.
        return "unsupported"

    result = run_faithfulness(
        store, subject_id="ART-faithfulness-1", text=text, decompose_judge=_decompose_into_two,
        verify_judge=verify_judge, issued_by_launch=launch_id,
    )

    assert result["total_claims"] == 2
    assert result["supported_claims"] == 1
    assert result["score"] == pytest.approx(0.5)
    statuses = {b["claim"]: b["status"] for b in result["breakdown"]}
    assert statuses[_OPEN_SENTENCE] == "mechanical_pass"
    assert statuses["This is an entirely unrelated claim about coastal erosion patterns."] == "llm_fail"

    verdict = result["verdict"]
    assert verdict["subject_kind"] == "artifact"
    assert verdict["procedure"] == "custom"
    assert verdict["label"] == "0.5000"
    evidence = json.loads(verdict["evidence"])
    assert len(evidence) == 2


def test_run_faithfulness_all_claims_supported_scores_one(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = _text_with_marker(anchor["anchor_id"], _OPEN_SENTENCE)

    def decompose_single(_envelope):
        return [_OPEN_SENTENCE]  # bare list form -- also accepted

    result = run_faithfulness(
        store, subject_id="ART-faithfulness-2", text=text, decompose_judge=decompose_single,
        verify_judge=lambda env: "supported", issued_by_launch=launch_id,
    )
    assert result["score"] == 1.0
    assert result["supported_claims"] == result["total_claims"] == 1
    assert all(b["status"] in CLEAN_PASS_STATUSES for b in result["breakdown"])


def test_run_faithfulness_zero_source_pairs_yields_none_score_and_records_no_claims_verdict(store):
    launch_id = bootstrap_launch(store)
    result = run_faithfulness(
        store, subject_id="ART-empty", text="no citation markers in this text at all.",
        decompose_judge=lambda env: {"claims": ["should never be called"]},
        verify_judge=lambda env: "supported", issued_by_launch=launch_id,
    )
    assert result["total_claims"] == 0
    assert result["score"] is None
    assert result["citecheck_result"] is None
    assert result["verdict"]["label"] == "no_claims"


def test_run_faithfulness_undecomposable_reply_falls_back_to_original_sentence(store):
    """A decomposer that returns an empty claims list must not silently
    drop the sentence's own evidence obligation -- it falls back to
    treating the sentence itself as its one atomic claim."""
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = _text_with_marker(anchor["anchor_id"], _OPEN_SENTENCE)

    result = run_faithfulness(
        store, subject_id="ART-fallback", text=text, decompose_judge=lambda env: {"claims": []},
        verify_judge=lambda env: "supported", issued_by_launch=launch_id,
    )
    assert result["total_claims"] == 1
    assert result["breakdown"][0]["claim"] == _OPEN_SENTENCE


def test_run_faithfulness_requires_exactly_one_of_text_or_pairs(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(VerifyError):
        run_faithfulness(store, subject_id="S", decompose_judge=lambda e: [], verify_judge=lambda e: "supported", issued_by_launch=launch_id)
    with pytest.raises(VerifyError):
        run_faithfulness(
            store, subject_id="S", text="x", pairs=[], decompose_judge=lambda e: [], verify_judge=lambda e: "supported",
            issued_by_launch=launch_id,
        )


# ---------------------------------------------------------------------------
# run_citecheck_with_faithfulness: the optional escalation stage
# ---------------------------------------------------------------------------


def test_run_citecheck_with_faithfulness_skips_escalation_when_everything_mechanically_passes(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = _text_with_marker(anchor["anchor_id"], _OPEN_SENTENCE)

    result = run_citecheck_with_faithfulness(
        store, subject_id="ART-escalation-skip", text=text,
        decompose_judge=lambda env: pytest.fail("decompose_judge must not be called when nothing needs escalation"),
        verify_judge=lambda env: pytest.fail("verify_judge must not be called when nothing needs escalation"),
        issued_by_launch=launch_id,
    )
    assert result["summary"]["mechanical_pass"] == 1
    assert result["faithfulness"] is None


def test_run_citecheck_with_faithfulness_escalates_a_broken_citation_to_atomic_claim_check(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    broken_sentence = "This sentence cites an anchor that plainly does not exist anywhere."
    text = f"{broken_sentence} [[cite:ANC-0000000000000000000BROKEN]]"

    def decompose_judge(envelope):
        assert envelope["sentence"] == broken_sentence
        return {"claims": [broken_sentence]}

    result = run_citecheck_with_faithfulness(
        store, subject_id="ART-escalation-run", text=text, decompose_judge=decompose_judge,
        verify_judge=lambda env: "unsupported", issued_by_launch=launch_id,
    )
    assert result["summary"]["anchor_not_found"] == 1
    assert result["faithfulness"] is not None
    assert result["faithfulness"]["total_claims"] == 1
    # the escalated claim inherits the SAME broken anchor -- it too fails
    # to resolve (anchor_not_found), which is not a CLEAN_PASS status.
    assert result["faithfulness"]["supported_claims"] == 0


def test_run_citecheck_with_faithfulness_escalate_statuses_narrows_which_pairs_escalate(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    # A sentence with no numbers/6-word shingle overlap with the anchor's
    # quote never mechanically passes -- it becomes an escalation
    # candidate that (with judge=None passed as citecheck_judge) is never
    # LLM-judged either, landing at "escalation_selected"/"escalation_not_sampled".
    text = f"Short unrelated claim. [[cite:{anchor['anchor_id']}]]"

    result = run_citecheck_with_faithfulness(
        store, subject_id="ART-narrow", text=text, citecheck_judge=None,
        decompose_judge=lambda env: pytest.fail("must not escalate a status excluded by escalate_statuses"),
        verify_judge=lambda env: pytest.fail("must not escalate a status excluded by escalate_statuses"),
        issued_by_launch=launch_id, escalate_statuses=["llm_fail"],  # excludes escalation_selected/escalation_not_sampled
    )
    assert result["faithfulness"] is None
