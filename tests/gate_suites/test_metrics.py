"""Unit tests for ``trialerror.eval.gate_suites``'s pure metric functions and
suite registry -- no subprocess involved (see ``test_runner.py`` for the
real pytest-subprocess integration tests)."""

from __future__ import annotations

import pytest

from trialerror.eval.errors import UnknownGateSuiteError
from trialerror.eval.gate_suites import (
    CITATION_GROUNDED_SUITE_ID,
    REVIEW_VERDICT_SUITE_ID,
    GateSuite,
    MetricResult,
    citation_coverage,
    consolidation_completeness,
    faithfulness_threshold,
    fence_compliance,
    get_suite,
    list_suites,
    register_suite,
    reproduction_status_check,
)


def test_metric_result_to_dict_round_trips_all_fields():
    result = MetricResult(name="x", passed=True, score=0.5, message="m")
    assert result.to_dict() == {"name": "x", "passed": True, "score": 0.5, "message": "m"}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_built_in_suites_are_registered_on_import():
    suites = list_suites()
    assert CITATION_GROUNDED_SUITE_ID in suites
    assert REVIEW_VERDICT_SUITE_ID in suites
    assert set(suites[CITATION_GROUNDED_SUITE_ID]) == {
        "citation_coverage", "faithfulness_threshold", "reproduction_status", "fence_compliance",
    }
    assert "consolidation_completeness" in suites[REVIEW_VERDICT_SUITE_ID]


def test_get_suite_unknown_id_raises_typed_error():
    with pytest.raises(UnknownGateSuiteError):
        get_suite("no-such-suite-id")


def test_register_suite_is_idempotent_by_id():
    register_suite(GateSuite(suite_id="tmp-test-suite", checks={"always_pass": lambda s: MetricResult("always_pass", True, None, "ok")}))
    register_suite(GateSuite(suite_id="tmp-test-suite", checks={"always_pass": lambda s: MetricResult("always_pass", True, None, "ok")}, ))
    assert list_suites()["tmp-test-suite"] == ["always_pass"]


# ---------------------------------------------------------------------------
# citation_coverage
# ---------------------------------------------------------------------------


def test_citation_coverage_passes_at_full_ratio():
    subject = {"citecheck_summary": {"total_pairs": 4, "mechanical_pass": 3, "llm_pass": 1}}
    result = citation_coverage(subject)
    assert result.passed is True
    assert result.score == 1.0


def test_citation_coverage_fails_below_threshold():
    subject = {"citecheck_summary": {"total_pairs": 4, "mechanical_pass": 1, "llm_pass": 0}}
    result = citation_coverage(subject, min_ratio=0.9)
    assert result.passed is False
    assert result.score == 0.25


def test_citation_coverage_no_pairs_fails_with_none_score():
    result = citation_coverage({})
    assert result.passed is False
    assert result.score is None


# ---------------------------------------------------------------------------
# faithfulness_threshold
# ---------------------------------------------------------------------------


def test_faithfulness_threshold_passes_above_bar():
    result = faithfulness_threshold({"faithfulness": {"score": 0.9}}, min_score=0.8)
    assert result.passed is True


def test_faithfulness_threshold_fails_below_bar():
    result = faithfulness_threshold({"faithfulness": {"score": 0.5}}, min_score=0.8)
    assert result.passed is False


def test_faithfulness_threshold_fails_closed_when_absent():
    result = faithfulness_threshold({})
    assert result.passed is False
    assert result.score is None


# ---------------------------------------------------------------------------
# reproduction_status_check
# ---------------------------------------------------------------------------


def test_reproduction_status_check_passes_on_match():
    assert reproduction_status_check({"gate": {"reproduction_status": "match"}}).passed is True


def test_reproduction_status_check_passes_on_unrun_by_default():
    assert reproduction_status_check({"gate": {"reproduction_status": "unrun"}}).passed is True


def test_reproduction_status_check_fails_on_mismatch():
    assert reproduction_status_check({"gate": {"reproduction_status": "mismatch"}}).passed is False


# ---------------------------------------------------------------------------
# fence_compliance
# ---------------------------------------------------------------------------


def test_fence_compliance_passes_when_every_restricted_chunk_is_fenced_and_short():
    subject = {"fenced_chunks": [{"chunk_id": "C1", "license_tier": "commercial_restricted", "fenced": True, "excerpt_word_count": 20}]}
    assert fence_compliance(subject).passed is True


def test_fence_compliance_fails_on_unfenced_restricted_chunk():
    subject = {"fenced_chunks": [{"chunk_id": "C1", "license_tier": "commercial_restricted", "fenced": False}]}
    assert fence_compliance(subject).passed is False


def test_fence_compliance_fails_on_over_length_excerpt():
    subject = {"fenced_chunks": [{"chunk_id": "C1", "license_tier": "commercial_restricted", "fenced": True, "excerpt_word_count": 21}]}
    assert fence_compliance(subject).passed is False


def test_fence_compliance_ignores_open_license_chunks():
    subject = {"fenced_chunks": [{"chunk_id": "C1", "license_tier": "open", "fenced": False}]}
    assert fence_compliance(subject).passed is True


# ---------------------------------------------------------------------------
# consolidation_completeness -- the C-0066 executable check
# ---------------------------------------------------------------------------


def test_consolidation_completeness_passes_when_every_finding_has_a_valid_disposition():
    subject = {"findings": [
        {"finding": "NB-1 ...", "disposition": "FIXED (tier3 09e68d2): rewired ..."},
        {"finding": "EP-6 ...", "disposition": "ACCEPTED (design bar = ...)"},
        {"finding": "O-1 ...", "disposition": "DEFERRED-v1"},
    ]}
    result = consolidation_completeness(subject)
    assert result.passed is True


def test_consolidation_completeness_fails_on_a_missing_disposition():
    subject = {"findings": [
        {"finding": "NB-1", "disposition": "FIXED"},
        {"finding": "EP-6", "disposition": None},
    ]}
    result = consolidation_completeness(subject)
    assert result.passed is False
    assert "EP-6" in result.message


def test_consolidation_completeness_fails_on_an_out_of_vocabulary_disposition():
    subject = {"findings": [{"finding": "X", "disposition": "MAYBE LATER"}]}
    assert consolidation_completeness(subject).passed is False


def test_consolidation_completeness_fails_closed_on_zero_findings():
    assert consolidation_completeness({"findings": []}).passed is False
    assert consolidation_completeness({}).passed is False
