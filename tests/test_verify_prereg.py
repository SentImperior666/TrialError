"""``trialerror.verify.prereg`` — blind pre-registration: commit, reveal,
compliance recomputation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trialerror.stores.writer import get, update
from trialerror.verify.errors import InvalidProcedureError, PreregNotFoundError, PreregTamperedError, PreregVoidedError
from trialerror.verify.prereg import canonical_json, check_prereg_compliance, commit_prereg, prereg_status, reveal_prereg, sha256_hex


def test_commit_prereg_hashes_and_escrows_outside_program_repo(store, program_root, platform_root):
    row = commit_prereg(store, title="blind test", procedure="do the thing exactly this way", params={"n": 3, "a": "x"})
    assert row["status"] == "committed"
    assert row["revealed_ts"] is None
    assert len(row["procedure_sha256"]) == 64
    assert len(row["params_sha256"]) == 64
    assert str(platform_root) in row["escrow_path"]
    assert str(program_root) not in row["escrow_path"]  # the blind is OUTSIDE the program repo (design Sec 4.2)

    escrowed = json.loads(Path(row["escrow_path"]).read_text(encoding="utf-8"))
    assert escrowed["procedure"] == "do the thing exactly this way"
    assert escrowed["params"] == {"n": 3, "a": "x"}

    db_row = get(store, "prereg", pk_column="prereg_id", pk_value=row["prereg_id"])
    assert db_row is not None and db_row["status"] == "committed"


def test_commit_prereg_blank_procedure_raises_invalid_procedure_error(store):
    with pytest.raises(InvalidProcedureError):
        commit_prereg(store, title="t", procedure="   ")


def test_commit_prereg_defaults_params_to_empty_dict(store):
    row = commit_prereg(store, title="t", procedure="proc text")
    assert row["params_sha256"] == sha256_hex(canonical_json({}))


def test_commit_prereg_params_hash_is_order_independent(store):
    row_a = commit_prereg(store, title="t", procedure="proc", params={"a": 1, "b": 2})
    row_b = commit_prereg(store, title="t", procedure="proc", params={"b": 2, "a": 1})
    assert row_a["params_sha256"] == row_b["params_sha256"]


def test_prereg_status_returns_the_row(store):
    row = commit_prereg(store, title="t", procedure="proc")
    fetched = prereg_status(store, prereg_id=row["prereg_id"])
    assert fetched["prereg_id"] == row["prereg_id"]


def test_prereg_status_not_found_raises(store):
    with pytest.raises(PreregNotFoundError):
        prereg_status(store, prereg_id="PREG-does-not-exist")


def test_reveal_prereg_copies_content_into_program_tree_and_marks_revealed(store, program_root):
    committed = commit_prereg(store, title="t", procedure="the real procedure", params={"k": 1})
    revealed = reveal_prereg(store, prereg_id=committed["prereg_id"])
    assert revealed["status"] == "revealed"
    assert revealed["procedure"] == "the real procedure"
    assert revealed["params"] == {"k": 1}
    assert str(program_root) in revealed["revealed_path"]
    assert Path(revealed["revealed_path"]).is_file()

    db_row = get(store, "prereg", pk_column="prereg_id", pk_value=committed["prereg_id"])
    assert db_row["status"] == "revealed"
    assert db_row["revealed_ts"] is not None


def test_reveal_prereg_with_tampered_escrow_is_refused_and_voids_the_row(store):
    committed = commit_prereg(store, title="t", procedure="original procedure", params={})
    # tamper with the escrowed content directly on disk
    escrow_path = Path(committed["escrow_path"])
    tampered = json.loads(escrow_path.read_text(encoding="utf-8"))
    tampered["procedure"] = "a DIFFERENT procedure, swapped in after commit"
    escrow_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(PreregTamperedError):
        reveal_prereg(store, prereg_id=committed["prereg_id"])

    db_row = get(store, "prereg", pk_column="prereg_id", pk_value=committed["prereg_id"])
    assert db_row["status"] == "voided"


def test_reveal_prereg_missing_escrow_file_is_tampered_and_voids(store):
    committed = commit_prereg(store, title="t", procedure="proc")
    Path(committed["escrow_path"]).unlink()
    with pytest.raises(PreregTamperedError):
        reveal_prereg(store, prereg_id=committed["prereg_id"])
    db_row = get(store, "prereg", pk_column="prereg_id", pk_value=committed["prereg_id"])
    assert db_row["status"] == "voided"


def test_reveal_prereg_already_voided_is_refused(store):
    committed = commit_prereg(store, title="t", procedure="proc")
    update(store, "prereg", pk_column="prereg_id", pk_value=committed["prereg_id"], changes={"status": "voided"})
    with pytest.raises(PreregVoidedError):
        reveal_prereg(store, prereg_id=committed["prereg_id"])


def test_check_prereg_compliance_true_when_executed_matches_committed(store):
    committed = commit_prereg(store, title="t", procedure="proc-v1", params={"q": "dice"})
    assert check_prereg_compliance(store, prereg_id=committed["prereg_id"], executed_procedure="proc-v1", executed_params={"q": "dice"}) is True


def test_check_prereg_compliance_false_on_any_mismatch(store):
    committed = commit_prereg(store, title="t", procedure="proc-v1", params={"q": "dice"})
    assert check_prereg_compliance(store, prereg_id=committed["prereg_id"], executed_procedure="proc-v2", executed_params={"q": "dice"}) is False
    assert check_prereg_compliance(store, prereg_id=committed["prereg_id"], executed_procedure="proc-v1", executed_params={"q": "different"}) is False


def test_check_prereg_compliance_does_not_mutate_the_row(store):
    committed = commit_prereg(store, title="t", procedure="proc-v1")
    check_prereg_compliance(store, prereg_id=committed["prereg_id"], executed_procedure="proc-v1")
    db_row = get(store, "prereg", pk_column="prereg_id", pk_value=committed["prereg_id"])
    assert db_row["status"] == "committed"  # unchanged -- a pure check, not a reveal


def test_check_prereg_compliance_on_voided_prereg_raises(store):
    committed = commit_prereg(store, title="t", procedure="proc")
    update(store, "prereg", pk_column="prereg_id", pk_value=committed["prereg_id"], changes={"status": "voided"})
    with pytest.raises(PreregVoidedError):
        check_prereg_compliance(store, prereg_id=committed["prereg_id"], executed_procedure="proc")


def test_check_prereg_compliance_not_found_raises(store):
    with pytest.raises(PreregNotFoundError):
        check_prereg_compliance(store, prereg_id="PREG-does-not-exist", executed_procedure="proc")
