"""lane-b-translator: the fail-closed faithfulness guard
(:mod:`trialerror.feed_translate.gate`) -- Section 4.3 of
``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md``.

The property under test throughout is the one the design states in its own
words -- "a wrong translation of a gate verdict or a booking id is worse
than no translation" -- expressed three ways:

- tier 1 (deterministic fidelity) runs with no judges, no store, no
  network, so there is no configuration under which a translation reaches
  the dashboard unchecked;
- tier 2 (judged) can only ever make the verdict STRICTER, never rescue a
  translation tier 1 rejected;
- a failing verdict carries readable reasons, because a withheld
  translation the operator cannot diagnose is a dead end.

Both judges are deterministic fakes (the house convention -- see
``trialerror/verify/__init__.py``'s LLM-judgment-boundary note). No test in
this file touches a network.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from trialerror.feed_translate.gate import DEFAULT_FAITHFULNESS_MIN_SCORE, build_claim_envelope, run_translation_gate
from trialerror.verify.citecheck import CITECHECK_LABELS

from tests._feed_translate_fixtures import (
    DENSE_POST,
    FAITHFUL_TRANSLATION,
    UNFAITHFUL_TRANSLATION,
    build_feed,
)


def _sentence_decomposer(envelope: Mapping[str, Any]) -> list[str]:
    """A judge that treats each sentence as exactly one atomic claim --
    the minimum honest decomposition, and enough to exercise the
    supported-ratio arithmetic deterministically."""
    return [envelope["sentence"]]


def _always(label: str):
    def judge(_envelope: Mapping[str, Any]) -> str:
        return label

    return judge


def _support_first_only():
    """Supports the first claim it sees and rejects the rest -- gives a
    score strictly between 0 and 1 without depending on sentence counts
    being stable across edits."""
    seen: list[str] = []

    def judge(envelope: Mapping[str, Any]) -> str:
        seen.append(envelope["pair_id"])
        return "supported" if len(seen) == 1 else "unsupported"

    return judge


# ---------------------------------------------------------------------------
# tier 1 -- always on
# ---------------------------------------------------------------------------


def test_a_faithful_translation_passes_with_no_judges_and_no_store():
    gate = run_translation_gate(
        None, post_id="POST-x", original_body=DENSE_POST, translation_body=FAITHFUL_TRANSLATION
    )
    assert gate.passed
    assert gate.gate_status == "pass"
    assert gate.score is None  # tier 2 never ran
    assert gate.reasons == []


def test_an_unfaithful_translation_fails_closed_with_no_judges_and_no_store():
    gate = run_translation_gate(
        None, post_id="POST-x", original_body=DENSE_POST, translation_body=UNFAITHFUL_TRANSLATION
    )
    assert not gate.passed
    assert gate.gate_status == "fail"
    # every one of the three breakages is named, not just the first
    joined = " | ".join(gate.reasons)
    assert "LNCH-01JXYZ4" in joined      # dropped id
    assert "'5'" in joined               # invented number
    assert "deferred" in joined          # promoted hedge


def test_the_failure_reasons_survive_into_the_stored_row_shape():
    gate = run_translation_gate(
        None, post_id="POST-x", original_body=DENSE_POST, translation_body=UNFAITHFUL_TRANSLATION
    )
    row = gate.as_row()
    assert row["gate_status"] == "fail"
    assert row["faithfulness_score"] is None
    assert row["faithfulness_verdict_id"] is None
    import json

    reasons = json.loads(row["gate_reasons"])
    assert reasons["passed"] is False
    assert reasons["judged"] is False
    assert reasons["reasons"]
    # both load-bearing hedges in DENSE_POST were promoted to facts
    assert reasons["style"]["hedges_lost"] == ["deferred", "pending"]


def test_register_violations_are_advisory_by_default_and_fatal_under_strict_style():
    original = "The job completed."
    translation = "The job completed; it was a pivotal moment."
    lenient = run_translation_gate(None, post_id="P", original_body=original, translation_body=translation)
    assert lenient.passed
    assert lenient.style.register_violations

    strict = run_translation_gate(
        None, post_id="P", original_body=original, translation_body=translation, strict_style=True
    )
    assert not strict.passed
    assert any(r.startswith("[strict_style]") for r in strict.reasons)


def test_require_faithfulness_score_withholds_anything_never_judged():
    gate = run_translation_gate(
        None,
        post_id="POST-x",
        original_body=DENSE_POST,
        translation_body=FAITHFUL_TRANSLATION,
        require_faithfulness_score=True,
    )
    assert not gate.passed
    assert any("faithfulness" in r for r in gate.reasons)


# ---------------------------------------------------------------------------
# tier 2 -- judged
# ---------------------------------------------------------------------------


def test_the_claim_envelope_anchors_on_the_post_body_not_a_quote_anchor_row():
    envelope = build_claim_envelope(
        post_id="POST-1", claim_id="POST-1::S-1::CLM-1", claim="the booking is pending", original_body=DENSE_POST
    )
    assert envelope["anchor_quote"] == DENSE_POST
    assert envelope["anchor_id"] is None  # deliberately not a knowledge.quote_anchor
    assert envelope["labels"] == list(CITECHECK_LABELS)


def test_a_fully_supported_translation_scores_one_and_still_passes(store):
    feed = build_feed(store)
    gate = run_translation_gate(
        store,
        post_id=feed["post_ids"][0],
        original_body=DENSE_POST,
        translation_body=FAITHFUL_TRANSLATION,
        decompose_judge=_sentence_decomposer,
        verify_judge=_always("supported"),
        issued_by_launch=feed["launch_id"],
    )
    assert gate.score == 1.0
    assert gate.passed
    assert gate.verdict_id is not None
    verdict = store.knowledge.execute(
        "SELECT * FROM verdict WHERE verdict_id = ?", (gate.verdict_id,)
    ).fetchone()
    assert verdict["procedure"] == "custom"
    assert verdict["label"] == "1.0000"
    assert verdict["subject_id"] == f"feed_translate::{feed['post_ids'][0]}"


def test_a_low_judged_score_fails_a_translation_tier_one_accepted(store):
    feed = build_feed(store)
    gate = run_translation_gate(
        store,
        post_id=feed["post_ids"][0],
        original_body=DENSE_POST,
        translation_body=FAITHFUL_TRANSLATION,
        decompose_judge=_sentence_decomposer,
        verify_judge=_support_first_only(),
        issued_by_launch=feed["launch_id"],
    )
    assert 0.0 < gate.score < DEFAULT_FAITHFULNESS_MIN_SCORE
    assert not gate.passed
    assert gate.as_row()["faithfulness_score"] == gate.score
    assert gate.as_row()["faithfulness_verdict_id"] == gate.verdict_id


def test_tier_two_cannot_rescue_a_tier_one_failure(store):
    feed = build_feed(store)
    gate = run_translation_gate(
        store,
        post_id=feed["post_ids"][0],
        original_body=DENSE_POST,
        translation_body=UNFAITHFUL_TRANSLATION,
        decompose_judge=_sentence_decomposer,
        verify_judge=_always("supported"),
        issued_by_launch=feed["launch_id"],
    )
    assert gate.score == 1.0  # the judge said every claim was fine...
    assert not gate.passed  # ...and the deterministic tier still withholds it


def test_no_verdict_row_is_written_for_an_orchestrator_identity_translation(store):
    """``knowledge.verdict.issued_by_launch`` is NOT NULL and XID-checked,
    so a translation produced under the orchestrator's no-launch identity
    (the design's zero-cost path) gets a SCORE but no verdict row -- and
    must not crash trying to invent one."""
    feed = build_feed(store)
    before = store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"]
    gate = run_translation_gate(
        store,
        post_id=feed["post_ids"][0],
        original_body=DENSE_POST,
        translation_body=FAITHFUL_TRANSLATION,
        decompose_judge=_sentence_decomposer,
        verify_judge=_always("supported"),
        issued_by_launch=None,
    )
    assert gate.score == 1.0
    assert gate.verdict_id is None
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"] == before


def test_an_empty_translation_decomposes_to_no_claims_and_scores_none(store):
    feed = build_feed(store)
    gate = run_translation_gate(
        store,
        post_id=feed["post_ids"][0],
        original_body=DENSE_POST,
        translation_body="   ",
        decompose_judge=_sentence_decomposer,
        verify_judge=_always("supported"),
        issued_by_launch=feed["launch_id"],
    )
    assert gate.score is None  # an undefined ratio, never a spurious 0.0 or 1.0
    assert not gate.passed  # tier 1 still catches the dropped ids


@pytest.mark.parametrize("label", ["unsupported", "uncertain"])
def test_any_non_supported_label_counts_against_the_score(store, label):
    feed = build_feed(store)
    gate = run_translation_gate(
        store,
        post_id=feed["post_ids"][0],
        original_body=DENSE_POST,
        translation_body=FAITHFUL_TRANSLATION,
        decompose_judge=_sentence_decomposer,
        verify_judge=_always(label),
        issued_by_launch=feed["launch_id"],
    )
    assert gate.score == 0.0
    assert not gate.passed
