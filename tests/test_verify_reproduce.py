"""``trialerror.verify.reproduce`` — the reproduction runner (design Section
8.3) and its ONE coupling into M10's gate state machine."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trialerror.artifacts.errors import GateEntryConditionError
from trialerror.artifacts.gates import apply_union, open_gate, record_verdict as gate_record_verdict, submit_gate
from trialerror.stores.writer import get, insert
from trialerror.util.ids import new_id
from trialerror.verify.errors import ReproductionRefError, VerdictNotFoundError
from trialerror.verify.reproduce import parse_reproduction_ref, reproduce_verdict
from trialerror.verify.verdicts import record_verdict

from tests._verify_fixtures import bootstrap_launch

_SCRIPT = """
import sys
sys.stdout.write("hello reproduction world")
"""

_FAILING_SCRIPT = """
import sys
sys.stderr.write("boom")
sys.exit(1)
"""


def _write_script(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _make_verdict_with_ref(store, *, launch_id: str, script_path: Path, expected_sha256: str, args=None) -> dict:
    ref = json.dumps({"script": str(script_path), "args": args or [], "expected_sha256": expected_sha256})
    return record_verdict(
        store, subject_kind="artifact", subject_id="ART-repro", procedure="citecheck",
        procedure_version="1", label="PASS", reproduction_ref=ref, issued_by_launch=launch_id,
    )


# ---------------------------------------------------------------------------
# parse_reproduction_ref
# ---------------------------------------------------------------------------


def test_parse_reproduction_ref_fills_default_empty_args():
    spec = parse_reproduction_ref(json.dumps({"script": "x.py", "expected_sha256": "a" * 64}))
    assert spec["args"] == []


def test_parse_reproduction_ref_rejects_non_json():
    with pytest.raises(ReproductionRefError):
        parse_reproduction_ref("not json at all")


def test_parse_reproduction_ref_rejects_non_object_json():
    with pytest.raises(ReproductionRefError):
        parse_reproduction_ref(json.dumps(["a", "list"]))


def test_parse_reproduction_ref_requires_script_and_expected_sha256():
    with pytest.raises(ReproductionRefError):
        parse_reproduction_ref(json.dumps({"script": "x.py"}))
    with pytest.raises(ReproductionRefError):
        parse_reproduction_ref(json.dumps({"expected_sha256": "a" * 64}))


# ---------------------------------------------------------------------------
# reproduce_verdict: match / mismatch
# ---------------------------------------------------------------------------


def test_reproduce_verdict_match_when_output_sha_agrees(store, tmp_path):
    launch_id = bootstrap_launch(store)
    script = _write_script(tmp_path, "repro_match.py", _SCRIPT)
    expected = hashlib.sha256(b"hello reproduction world").hexdigest()
    original = _make_verdict_with_ref(store, launch_id=launch_id, script_path=script, expected_sha256=expected)

    result = reproduce_verdict(store, verdict_id=original["verdict_id"], issued_by_launch=launch_id)
    assert result["status"] == "match"
    assert result["actual_sha256"] == expected
    assert result["verdict"]["procedure"] == "reproduction"
    assert result["verdict"]["label"] == "match"


def test_reproduce_verdict_mismatch_when_output_sha_disagrees(store, tmp_path):
    launch_id = bootstrap_launch(store)
    script = _write_script(tmp_path, "repro_mismatch.py", _SCRIPT)
    original = _make_verdict_with_ref(store, launch_id=launch_id, script_path=script, expected_sha256="0" * 64)

    result = reproduce_verdict(store, verdict_id=original["verdict_id"], issued_by_launch=launch_id)
    assert result["status"] == "mismatch"
    assert result["verdict"]["label"] == "mismatch"


def test_reproduce_verdict_nonzero_exit_is_mismatch_not_an_exception(store, tmp_path):
    launch_id = bootstrap_launch(store)
    script = _write_script(tmp_path, "repro_fail.py", _FAILING_SCRIPT)
    original = _make_verdict_with_ref(store, launch_id=launch_id, script_path=script, expected_sha256="0" * 64)

    result = reproduce_verdict(store, verdict_id=original["verdict_id"], issued_by_launch=launch_id)
    assert result["status"] == "mismatch"
    assert "exit code" in (result["error_note"] or "")


def test_reproduce_verdict_unknown_verdict_id_raises(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(VerdictNotFoundError):
        reproduce_verdict(store, verdict_id="VRD-does-not-exist", issued_by_launch=launch_id)


def test_reproduce_verdict_no_reproduction_ref_raises(store):
    launch_id = bootstrap_launch(store)
    row = record_verdict(
        store, subject_kind="artifact", subject_id="ART-1", procedure="citecheck",
        procedure_version="1", label="PASS", issued_by_launch=launch_id,
    )
    with pytest.raises(ReproductionRefError):
        reproduce_verdict(store, verdict_id=row["verdict_id"], issued_by_launch=launch_id)


# ---------------------------------------------------------------------------
# the M10 coupling: gate.reproduction_status / reproduction_ref
# ---------------------------------------------------------------------------


def _open_submitted_gate(store, *, launch_id: str) -> dict:
    artifact_id = new_id("ART")
    insert(
        store, "artifact",
        {
            "artifact_id": artifact_id, "type": "note", "title": "t", "path": "x.md", "sha256": "0" * 64,
            "status": "draft", "registered_ts": None, "registered_by_launch": launch_id,
        },
    )
    gate = open_gate(store, artifact_id=artifact_id)
    return submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)


def test_reproduce_verdict_with_gate_id_stamps_gate_reproduction_status_and_ref(store, tmp_path):
    launch_id = bootstrap_launch(store)
    insert(store, "template", {"type_key": "note", "title": "Note", "version": "1", "path": "templates/note.md", "gated": 1})
    gate = _open_submitted_gate(store, launch_id=launch_id)
    gate_record_verdict(store, gate_id=gate["gate_id"], verdict="PASS_WITH_EDITS", by_launch=launch_id)

    script = _write_script(tmp_path, "repro_gate.py", _SCRIPT)
    expected = hashlib.sha256(b"hello reproduction world").hexdigest()
    original = _make_verdict_with_ref(store, launch_id=launch_id, script_path=script, expected_sha256=expected)

    result = reproduce_verdict(store, verdict_id=original["verdict_id"], gate_id=gate["gate_id"], issued_by_launch=launch_id)
    assert result["status"] == "match"

    refreshed_gate = get(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"])
    assert refreshed_gate["reproduction_status"] == "match"
    assert refreshed_gate["reproduction_ref"] == original["reproduction_ref"]

    # match does not block apply_union (no blocking edits here)
    applied = apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
    assert applied["state"] == "union_applied"


def test_reproduce_verdict_mismatch_blocks_the_gate_path(store, tmp_path):
    launch_id = bootstrap_launch(store)
    insert(store, "template", {"type_key": "note2", "title": "Note", "version": "1", "path": "templates/note.md", "gated": 1})
    artifact_id = new_id("ART")
    insert(
        store, "artifact",
        {
            "artifact_id": artifact_id, "type": "note2", "title": "t", "path": "x.md", "sha256": "0" * 64,
            "status": "draft", "registered_ts": None, "registered_by_launch": launch_id,
        },
    )
    gate = open_gate(store, artifact_id=artifact_id)
    gate = submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    gate_record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", by_launch=launch_id)

    script = _write_script(tmp_path, "repro_gate_mismatch.py", _SCRIPT)
    original = _make_verdict_with_ref(store, launch_id=launch_id, script_path=script, expected_sha256="0" * 64)

    result = reproduce_verdict(store, verdict_id=original["verdict_id"], gate_id=gate["gate_id"], issued_by_launch=launch_id)
    assert result["status"] == "mismatch"

    refreshed_gate = get(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"])
    assert refreshed_gate["reproduction_status"] == "mismatch"

    # apply_union must now refuse -- design Section 4.2's F10 entry condition
    with pytest.raises(GateEntryConditionError):
        apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)


def test_reproduce_verdict_relative_script_resolves_under_program_root(store):
    launch_id = bootstrap_launch(store)
    script_rel = "scripts/repro_rel.py"
    (store.program_root / "scripts").mkdir(parents=True, exist_ok=True)
    (store.program_root / script_rel).write_text(_SCRIPT, encoding="utf-8")
    expected = hashlib.sha256(b"hello reproduction world").hexdigest()
    ref = json.dumps({"script": script_rel, "args": [], "expected_sha256": expected})
    original = record_verdict(
        store, subject_kind="artifact", subject_id="ART-rel", procedure="citecheck", procedure_version="1",
        label="PASS", reproduction_ref=ref, issued_by_launch=launch_id,
    )
    result = reproduce_verdict(store, verdict_id=original["verdict_id"], issued_by_launch=launch_id)
    assert result["status"] == "match"
