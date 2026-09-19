"""``trialerror.verify.verdicts`` — the generic ``knowledge.verdict`` writer."""

from __future__ import annotations

import json

import pytest

from trialerror.stores.errors import XidTargetMissingError
from trialerror.verify.errors import InvalidProcedureError, InvalidSubjectKindError
from trialerror.verify.verdicts import PROCEDURES, SUBJECT_KINDS, record_verdict

from tests._verify_fixtures import bootstrap_launch


def test_record_verdict_writes_the_row_with_json_encoded_evidence(store):
    launch_id = bootstrap_launch(store)
    row = record_verdict(
        store, subject_kind="claim", subject_id="CLM-fixture", procedure="citecheck",
        procedure_version="1", label="PASS", evidence=[{"anchor_id": "ANC-1", "stance": "supports"}],
        issued_by_launch=launch_id,
    )
    assert row["subject_kind"] == "claim"
    assert row["label"] == "PASS"
    assert json.loads(row["evidence"])[0]["anchor_id"] == "ANC-1"
    assert row["prereg_compliant"] is None
    assert row["verdict_id"].startswith("VRD-")


def test_record_verdict_defaults_evidence_to_empty_json_array_when_omitted(store):
    launch_id = bootstrap_launch(store)
    row = record_verdict(
        store, subject_kind="artifact", subject_id="ART-1", procedure="gate",
        procedure_version="1", label="PASS", issued_by_launch=launch_id,
    )
    assert json.loads(row["evidence"]) == []


def test_record_verdict_prereg_compliant_true_encodes_as_1(store):
    launch_id = bootstrap_launch(store)
    row = record_verdict(
        store, subject_kind="hypothesis", subject_id="HYP-1", procedure="contracrow",
        procedure_version="1", label="supported", prereg_compliant=True, issued_by_launch=launch_id,
    )
    assert row["prereg_compliant"] == 1


def test_record_verdict_prereg_compliant_false_encodes_as_0(store):
    launch_id = bootstrap_launch(store)
    row = record_verdict(
        store, subject_kind="hypothesis", subject_id="HYP-1", procedure="contracrow",
        procedure_version="1", label="supported", prereg_compliant=False, issued_by_launch=launch_id,
    )
    assert row["prereg_compliant"] == 0


def test_record_verdict_bad_subject_kind_raises_before_any_write(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(InvalidSubjectKindError):
        record_verdict(
            store, subject_kind="not-a-real-kind", subject_id="X", procedure="citecheck",
            procedure_version="1", label="PASS", issued_by_launch=launch_id,
        )


def test_record_verdict_bad_procedure_raises_before_any_write(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(InvalidProcedureError):
        record_verdict(
            store, subject_kind="claim", subject_id="X", procedure="not-a-real-procedure",
            procedure_version="1", label="PASS", issued_by_launch=launch_id,
        )


def test_record_verdict_unknown_launch_raises_xid_target_missing(store):
    with pytest.raises(XidTargetMissingError):
        record_verdict(
            store, subject_kind="claim", subject_id="X", procedure="citecheck",
            procedure_version="1", label="PASS", issued_by_launch="LNCH-does-not-exist",
        )


def test_subject_kinds_and_procedures_match_the_ddl_check_constraints():
    assert SUBJECT_KINDS == {"hypothesis", "claim", "citation", "artifact"}
    assert PROCEDURES == {"citecheck", "contracrow", "gate", "reproduction", "custom"}
