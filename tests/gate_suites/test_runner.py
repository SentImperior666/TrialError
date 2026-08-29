"""Integration tests for ``trialerror.eval.gate_suites.run_gate_suite``/
``run_gate_suite_for_gate`` -- a REAL pytest subprocess run (module
docstring: "spawning pytest programmatically"), against a fixture artifact
that includes one deliberately failing criterion (deliverable requirement,
verbatim), plus the ``reproduction_ref`` gate-row wiring and its
downstream effect on ``apply_union``.
"""

from __future__ import annotations

import json

import pytest

from trialerror.artifacts.errors import GateEntryConditionError
from trialerror.artifacts.gates import apply_union, open_gate
from trialerror.artifacts.gates import record_verdict as gate_record_verdict
from trialerror.artifacts.gates import submit_gate
from trialerror.eval.errors import GateSuiteRunnerError, UnknownGateSuiteError
from trialerror.eval.gate_suites import REVIEW_VERDICT_SUITE_ID, run_gate_suite, run_gate_suite_for_gate
from trialerror.stores.writer import get, insert
from trialerror.util.ids import new_id

from tests._verify_fixtures import bootstrap_launch


def _open_gated_artifact(store, *, launch_id: str) -> dict:
    insert(store, "template", {"type_key": "eval-fixture-note", "title": "Note", "version": "1", "path": "templates/note.md", "gated": 1})
    artifact_id = new_id("ART")
    insert(
        store, "artifact",
        {
            "artifact_id": artifact_id, "type": "eval-fixture-note", "title": "t", "path": "x.md", "sha256": "0" * 64,
            "status": "draft", "registered_ts": None, "registered_by_launch": launch_id,
        },
    )
    gate = open_gate(store, artifact_id=artifact_id)
    gate = submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    gate_record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", by_launch=launch_id)
    return gate


# ---------------------------------------------------------------------------
# run_gate_suite: a real pytest subprocess, one deliberately failing
# criterion among otherwise-passing ones.
# ---------------------------------------------------------------------------


def test_run_gate_suite_all_pass_returns_pass_with_full_check_breakdown():
    subject = {
        "citecheck_summary": {"total_pairs": 2, "mechanical_pass": 2, "llm_pass": 0},
        "faithfulness": {"score": 0.9},
        "gate": {"reproduction_status": "match"},
        "fenced_chunks": [],
    }
    result = run_gate_suite("citation-grounded", subject, timeout=30)
    assert result["overall"] == "PASS"
    assert result["returncode"] == 0
    assert len(result["checks"]) == 4
    assert all(c["passed"] for c in result["checks"])


def test_run_gate_suite_one_deliberately_failing_criterion_yields_fail_with_named_offender():
    """Every check passes EXCEPT reproduction_status (deliberately planted
    'mismatch') -- the deliverable's own "incl. one deliberately failing
    criterion" fixture."""
    subject = {
        "citecheck_summary": {"total_pairs": 2, "mechanical_pass": 2, "llm_pass": 0},
        "faithfulness": {"score": 0.9},
        "gate": {"reproduction_status": "mismatch"},  # <- the planted failure
        "fenced_chunks": [],
    }
    result = run_gate_suite("citation-grounded", subject, timeout=30)
    assert result["overall"] == "FAIL"
    assert result["returncode"] == 1

    by_name = {c["name"]: c for c in result["checks"]}
    assert len(by_name) == 4
    assert by_name["reproduction_status"]["passed"] is False
    assert "mismatch" in by_name["reproduction_status"]["message"]
    # every OTHER check still ran and still reports its own real result --
    # one failure never short-circuits the rest of the suite.
    assert by_name["citation_coverage"]["passed"] is True
    assert by_name["faithfulness_threshold"]["passed"] is True
    assert by_name["fence_compliance"]["passed"] is True


def test_run_gate_suite_review_verdict_suite_catches_a_finding_missing_disposition():
    subject = {
        "findings": [
            {"finding": "NB-1", "disposition": "FIXED (tier3)"},
            {"finding": "EP-9", "disposition": ""},  # <- the planted failure
        ],
        "gate": {"reproduction_status": "unrun"},
    }
    result = run_gate_suite(REVIEW_VERDICT_SUITE_ID, subject, timeout=30)
    assert result["overall"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["consolidation_completeness"]["passed"] is False
    assert "EP-9" in by_name["consolidation_completeness"]["message"]
    assert by_name["reproduction_status"]["passed"] is True


def test_run_gate_suite_unknown_suite_id_raises_before_spawning_a_subprocess():
    with pytest.raises(UnknownGateSuiteError):
        run_gate_suite("definitely-not-a-registered-suite", {})


# ---------------------------------------------------------------------------
# run_gate_suite_for_gate: the reproduction_ref pattern, and its effect on
# the real gate state machine (apply_union blocked / allowed).
# ---------------------------------------------------------------------------


def test_run_gate_suite_for_gate_failing_suite_writes_mismatch_and_blocks_apply_union(store):
    launch_id = bootstrap_launch(store)
    gate = _open_gated_artifact(store, launch_id=launch_id)

    subject = {
        "findings": [{"finding": "X", "disposition": "FIXED"}, {"finding": "Y", "disposition": None}],
        "gate": {"reproduction_status": "unrun"},
    }
    result = run_gate_suite_for_gate(
        store, gate_id=gate["gate_id"], suite_id=REVIEW_VERDICT_SUITE_ID, subject=subject, issued_by_launch=launch_id,
    )
    assert result["overall"] == "FAIL"
    assert result["reproduction_status"] == "mismatch"

    refreshed = get(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"])
    assert refreshed["reproduction_status"] == "mismatch"
    ref = json.loads(refreshed["reproduction_ref"])
    assert ref["kind"] == "gate_suite"
    assert ref["suite_id"] == REVIEW_VERDICT_SUITE_ID
    assert any(not c["passed"] for c in ref["checks"])

    assert result["verdict"]["procedure"] == "gate"
    assert result["verdict"]["subject_kind"] == "artifact"
    assert result["verdict"]["label"] == "FAIL"

    with pytest.raises(GateEntryConditionError):
        apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)


def test_run_gate_suite_for_gate_passing_suite_writes_match_and_allows_apply_union(store):
    launch_id = bootstrap_launch(store)
    gate = _open_gated_artifact(store, launch_id=launch_id)

    subject = {
        "findings": [{"finding": "X", "disposition": "FIXED"}],
        "gate": {"reproduction_status": "unrun"},
    }
    result = run_gate_suite_for_gate(
        store, gate_id=gate["gate_id"], suite_id=REVIEW_VERDICT_SUITE_ID, subject=subject, issued_by_launch=launch_id,
    )
    assert result["overall"] == "PASS"
    assert result["reproduction_status"] == "match"

    unioned = apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
    assert unioned["state"] == "union_applied"


def test_run_gate_suite_for_gate_unknown_gate_id_raises(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(GateSuiteRunnerError):
        run_gate_suite_for_gate(
            store, gate_id="GATE-does-not-exist", suite_id=REVIEW_VERDICT_SUITE_ID,
            subject={"findings": [], "gate": {}}, issued_by_launch=launch_id,
        )
