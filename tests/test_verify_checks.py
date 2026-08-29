"""``trialerror.verify.checks`` — M9's doctor checks (read-only, sample-bounded).
Calls the check functions directly (the same convention
``tests/test_retrieval_checks.py`` follows for M8's own checks) rather than
going through the global doctor registry, so this file never risks
polluting/depending on registry state another test module also mutates."""

from __future__ import annotations

import json
from pathlib import Path

from trialerror.stores.writer import update
from trialerror.util.doctor import DoctorContext
from trialerror.verify.checks import check_prereg_escrow_integrity, check_verdict_evidence_anchors
from trialerror.verify.prereg import commit_prereg
from trialerror.verify.verdicts import record_verdict

from tests._verify_fixtures import bootstrap_launch, build_small_corpus

# ---------------------------------------------------------------------------
# verdict_evidence_anchors
# ---------------------------------------------------------------------------


def test_verdict_evidence_anchors_skips_when_no_program_root():
    result = check_verdict_evidence_anchors(DoctorContext(program_root=None))
    assert result.status == "skip"


def test_verdict_evidence_anchors_skips_when_no_verdicts_yet(store):
    result = check_verdict_evidence_anchors(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"


def test_verdict_evidence_anchors_pass_when_every_cited_anchor_resolves(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor_row = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)).fetchone())
    record_verdict(
        store, subject_kind="citation", subject_id=anchor_row["anchor_id"], procedure="citecheck",
        procedure_version="1", label="PASS", evidence=[{"anchor_id": anchor_row["anchor_id"]}],
        issued_by_launch=launch_id,
    )
    result = check_verdict_evidence_anchors(DoctorContext(program_root=store.program_root))
    assert result.status == "pass"
    assert result.details["anchors_checked"] == 1


def test_verdict_evidence_anchors_fail_when_a_cited_anchor_is_dangling(store):
    launch_id = bootstrap_launch(store)
    record_verdict(
        store, subject_kind="citation", subject_id="ANC-ghost", procedure="citecheck",
        procedure_version="1", label="FAIL", evidence=[{"anchor_id": "ANC-ghost-does-not-exist"}],
        issued_by_launch=launch_id,
    )
    result = check_verdict_evidence_anchors(DoctorContext(program_root=store.program_root))
    assert result.status == "fail"
    assert result.details["offenders"][0]["anchor_id"] == "ANC-ghost-does-not-exist"


def test_verdict_evidence_anchors_ignores_evidence_items_with_no_anchor_id(store):
    launch_id = bootstrap_launch(store)
    record_verdict(
        store, subject_kind="artifact", subject_id="ART-1", procedure="citecheck",
        procedure_version="1", label="PASS", evidence=[{"note": "no anchor cited here"}],
        issued_by_launch=launch_id,
    )
    result = check_verdict_evidence_anchors(DoctorContext(program_root=store.program_root))
    assert result.status == "pass"
    assert result.details["anchors_checked"] == 0


# ---------------------------------------------------------------------------
# prereg_escrow_integrity
# ---------------------------------------------------------------------------


def test_prereg_escrow_integrity_skips_when_no_program_root():
    result = check_prereg_escrow_integrity(DoctorContext(program_root=None))
    assert result.status == "skip"


def test_prereg_escrow_integrity_skips_when_no_preregs_yet(store):
    result = check_prereg_escrow_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"


def test_prereg_escrow_integrity_pass_when_escrow_is_intact(store):
    commit_prereg(store, title="t", procedure="a real procedure", params={"x": 1})
    result = check_prereg_escrow_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "pass"


def test_prereg_escrow_integrity_fail_when_escrow_file_missing(store):
    row = commit_prereg(store, title="t", procedure="proc")
    Path(row["escrow_path"]).unlink()
    result = check_prereg_escrow_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "fail"
    assert "missing" in result.details["offenders"][0]["reason"]


def test_prereg_escrow_integrity_fail_when_escrow_tampered(store):
    row = commit_prereg(store, title="t", procedure="proc")
    escrow_path = Path(row["escrow_path"])
    content = json.loads(escrow_path.read_text(encoding="utf-8"))
    content["procedure"] = "swapped after commit"
    escrow_path.write_text(json.dumps(content), encoding="utf-8")
    result = check_prereg_escrow_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "fail"


def test_prereg_escrow_integrity_never_samples_voided_rows(store):
    row = commit_prereg(store, title="t", procedure="proc")
    update(store, "prereg", pk_column="prereg_id", pk_value=row["prereg_id"], changes={"status": "voided"})
    Path(row["escrow_path"]).unlink()  # would fail the check if this row were sampled
    result = check_prereg_escrow_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"  # only non-voided rows are sampled, and none remain
