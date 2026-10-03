"""``trialerror gate fail-reproduction`` (``fail_on_reproduction``) and the
``register --as-failed`` it opens: the operator's decision that a gate the
critic passed, but whose gate-suite reproduction read ``mismatch``, is a
failed result.

The invariants: the critic's verdict is never rewritten; the path only moves a
gate toward ``failed`` (never toward a pass, never to a registration as
passed); it refuses without a decision, without a recorded ``mismatch``, and
from every state but ``gated``; each write is append-only and one
transaction.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from tests.test_artifacts_register_dispositions import DEC, FAILURE_TEXT, World, registered_bytes_evidence
from trialerror.artifacts import gates as gates_mod
from trialerror.artifacts.checks import check_gate_illegal_transition_history, check_registration_disposition_consistent
from trialerror.artifacts.errors import (
    GateEntryConditionError,
    IllegalTransitionError,
    OperatorFailRefusedError,
    RegistrationRefusedError,
)
from trialerror.artifacts.gates import (
    OPERATOR_FAIL_PATH,
    advance_gate,
    apply_union,
    fail_on_reproduction,
    get_gate,
    open_gate,
    record_verdict,
    register_failed,
    register_with_deviation,
    submit_gate,
)
from trialerror.artifacts.registry import get_artifact, register_artifact
from trialerror.cli import main
from trialerror.stores import update
from trialerror.util.doctor import DoctorContext

REASON = "the round's gate suite could not reproduce its admission order"
FAIL_DEC = "DECISION-TEST-2"


@pytest.fixture()
def world(store, program_root):
    return World(store, program_root)


def transitions(store, gid):
    return [dict(r) for r in store.ops.execute("SELECT * FROM gate_transition WHERE gate_id=? ORDER BY id", (gid,))]


def fail(world, gid, **kw):
    args = {"decided_by": FAIL_DEC, "reason": REASON, "by_launch": world.launch}
    args.update(kw)
    return fail_on_reproduction(world.store, gate_id=gid, **args)


def snapshot(world, gid):
    return get_gate(world.store, gid), transitions(world.store, gid)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_fail_moves_a_mismatched_gate_to_failed_and_keeps_the_critic_verdict(world):
    _aid, gid = world.gated()
    before, before_transitions = snapshot(world, gid)
    assert (before["state"], before["verdict"], before["reproduction_status"]) == ("gated", "PASS_WITH_EDITS", "mismatch")
    row = fail(world, gid)
    assert row["state"] == "failed"
    # only the state moved: the verdict, the edits, the critic and the reproduction are as they were
    assert {k: v for k, v in row.items() if k != "state"} == {k: v for k, v in before.items() if k != "state"}
    after_transitions = transitions(world.store, gid)
    assert after_transitions[: len(before_transitions)] == before_transitions
    assert len(after_transitions) == len(before_transitions) + 1
    last = after_transitions[-1]
    assert (last["from_state"], last["to_state"], last["by_launch"]) == ("gated", "failed", world.launch)
    assert json.loads(last["evidence"]) == {
        "path": OPERATOR_FAIL_PATH,
        "decided_by": FAIL_DEC,
        "reason": REASON,
        "reproduction_status": "mismatch",
        "reproduction_ref": before["reproduction_ref"],
    }


def test_register_as_failed_accepts_a_gate_failed_this_way(world):
    aid, gid = world.gated()
    fail(world, gid)
    before_transitions = transitions(world.store, gid)
    artifact = register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
    assert artifact["status"] == "registered" and artifact["disposition"] == "registered_failed"
    gate = get_gate(world.store, gid)
    assert gate["state"] == "registered" and gate["disposition"] == "failure_registered"
    assert gate["verdict"] == "PASS_WITH_EDITS"  # never rewritten
    after = transitions(world.store, gid)
    assert after[: len(before_transitions)] == before_transitions
    last = after[-1]
    assert (last["from_state"], last["to_state"]) == ("failed", "registered")
    assert json.loads(last["evidence"]) == {
        "path": "register_failed", "decided_by": DEC, "failure_ref": FAILURE_TEXT,
        "basis": OPERATOR_FAIL_PATH, "failed_by": FAIL_DEC,
        "failure_basis": {
            "kind": OPERATOR_FAIL_PATH,
            "decided_by": FAIL_DEC,
            "failing_checks": [{"name": "admission_order_hash_matches", "message": "admission_order_hash_matches failed"}],
        },
        **registered_bytes_evidence(world, aid),
    }
    # a failed result on the record is closed: no new gate on it
    with pytest.raises(ValueError):
        open_gate(world.store, artifact_id=aid)


def test_register_as_failed_keeps_its_other_checks_on_this_path(world):
    aid, gid = world.gated()
    fail(world, gid)

    def refused(**kw):
        args = {"failure_ref": FAILURE_TEXT, "decided_by": DEC}
        args.update(kw)
        with pytest.raises(RegistrationRefusedError) as exc:
            register_failed(world.store, gate_id=gid, by_launch=world.launch, **args)
        return str(exc.value)

    assert "does not contain" in refused(failure_ref="a statement the artifact never makes")
    # on the operator's path the ref is optional, so an explicit empty one is "empty", not "required"
    assert "failure_ref is empty" in refused(failure_ref="")
    assert "decided_by" in refused(decided_by=" ")
    assert get_gate(world.store, gid)["state"] == "failed"
    assert get_artifact(world.store, aid)["status"] == "in_gate"


def test_the_doctor_checks_pass_after_this_path(world, program_root):
    _aid, gid = world.gated()
    fail(world, gid)
    register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
    ctx = DoctorContext(program_root=program_root)
    assert check_gate_illegal_transition_history(ctx).status == "pass"
    result = check_registration_disposition_consistent(ctx)
    assert result.status == "pass", result.details


# ---------------------------------------------------------------------------
# refusals: nothing is written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [("decided_by", ""), ("decided_by", "   "), ("reason", ""), ("reason", "  ")])
def test_refused_without_a_decision_or_a_reason(world, field, value):
    _aid, gid = world.gated()
    before = snapshot(world, gid)
    with pytest.raises(OperatorFailRefusedError, match=field):
        fail(world, gid, **{field: value})
    assert snapshot(world, gid) == before


@pytest.mark.parametrize("status", ["match", "unrun", None])
def test_refused_without_a_recorded_mismatch(world, status):
    _aid, gid = world.gated(reproduction_status=status)
    before = snapshot(world, gid)
    with pytest.raises(OperatorFailRefusedError, match="mismatch"):
        fail(world, gid)
    assert snapshot(world, gid) == before


def _gate_at(world, state):
    if state == "draft":
        return open_gate(world.store, artifact_id=world.artifact()["artifact_id"])["gate_id"]
    if state == "submitted":
        gid = open_gate(world.store, artifact_id=world.artifact()["artifact_id"])["gate_id"]
        submit_gate(world.store, gate_id=gid, by_launch=world.launch)
        return gid
    if state == "union_applied":
        gid = open_gate(world.store, artifact_id=world.artifact()["artifact_id"])["gate_id"]
        submit_gate(world.store, gate_id=gid, by_launch=world.launch)
        record_verdict(world.store, gate_id=gid, verdict="PASS", critic_launch=world.launch, reproduction_status="match")
        apply_union(world.store, gate_id=gid, by_launch=world.launch)
        return gid
    if state == "failed":
        return world.failed()[1]
    if state == "failed_by_operator":
        gid = world.gated()[1]
        fail(world, gid)
        return gid
    if state == "registered":
        gid = world.failed()[1]
        register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
        return gid
    raise AssertionError(state)


@pytest.mark.parametrize("state", ["draft", "submitted", "union_applied", "failed", "failed_by_operator", "registered"])
def test_refused_from_every_state_but_gated(world, state):
    gid = _gate_at(world, state)
    before = snapshot(world, gid)
    with pytest.raises(OperatorFailRefusedError, match="needs the gate at 'gated'"):
        fail(world, gid)
    assert snapshot(world, gid) == before


def test_refused_for_an_unknown_gate_or_launch(world):
    with pytest.raises(ValueError, match="no such gate"):
        fail(world, "CR-999")
    _aid, gid = world.gated()
    before = snapshot(world, gid)
    with pytest.raises(Exception, match="XID"):
        fail(world, gid, by_launch="LNCH-TEST-MISSING")
    assert snapshot(world, gid) == before


def test_one_transaction_a_failed_write_leaves_nothing(world, monkeypatch):
    _aid, gid = world.gated()
    before = snapshot(world, gid)
    real_insert = gates_mod.raw_insert

    def failing_insert(conn, table, row):
        if table == "gate_transition":
            raise RuntimeError("disk full")
        return real_insert(conn, table, row)

    monkeypatch.setattr(gates_mod, "raw_insert", failing_insert)
    with pytest.raises(RuntimeError):
        fail(world, gid)
    monkeypatch.undo()
    assert snapshot(world, gid) == before


# ---------------------------------------------------------------------------
# only toward failed; the generic path cannot pass for it
# ---------------------------------------------------------------------------


def test_a_gate_failed_this_way_never_reaches_a_pass(world):
    aid, gid = world.gated()
    fail(world, gid)
    with pytest.raises(IllegalTransitionError):
        apply_union(world.store, gate_id=gid, by_launch=world.launch)
    with pytest.raises(IllegalTransitionError):
        advance_gate(world.store, gate_id=gid, to_state="gated", by_launch=world.launch)
    with pytest.raises(IllegalTransitionError):
        advance_gate(world.store, gate_id=gid, to_state="registered", by_launch=world.launch)
    with pytest.raises(RegistrationRefusedError):
        register_artifact(world.store, artifact_id=aid, by_launch=world.launch)
    with pytest.raises(RegistrationRefusedError):
        register_with_deviation(
            world.store, gate_id=gid, deviations=[world.deviation()], decided_by=DEC, by_launch=world.launch
        )
    assert get_gate(world.store, gid)["state"] == "failed"
    assert get_artifact(world.store, aid)["status"] == "in_gate"


def test_a_generic_advance_to_failed_is_not_accepted_as_an_operator_failure(world):
    _aid, gid = world.gated()
    advance_gate(world.store, gate_id=gid, to_state="failed", by_launch=world.launch, evidence={"note": "abandoned"})
    with pytest.raises(RegistrationRefusedError, match="fail-reproduction"):
        register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)


@pytest.mark.parametrize("path", [OPERATOR_FAIL_PATH, "register_failed", "register_with_deviation"])
def test_a_generic_advance_cannot_claim_a_reserved_evidence_path(world, path):
    _aid, gid = world.gated()
    before = snapshot(world, gid)
    with pytest.raises(IllegalTransitionError, match="only by its own verb"):
        advance_gate(
            world.store, gate_id=gid, to_state="failed", by_launch=world.launch,
            evidence={"path": path, "decided_by": "forged"},
        )
    assert snapshot(world, gid) == before


def test_the_basis_is_the_frozen_evidence_not_the_live_column(world):
    """The decision that failed the gate froze its basis in the transition's
    evidence. A live column that was later rewritten by hand does not change
    what the registration reads: the failing checks come from the evidence."""
    aid, gid = world.gated()
    fail(world, gid)
    update(world.store, "gate", pk_column="gate_id", pk_value=gid, changes={"reproduction_status": "match", "reproduction_ref": "{}"})
    register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
    evidence = json.loads(transitions(world.store, gid)[-1]["evidence"])
    assert [c["name"] for c in evidence["failure_basis"]["failing_checks"]] == ["admission_order_hash_matches"]


def test_the_critic_fail_path_is_unchanged(world):
    aid, gid = world.failed()
    register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
    evidence = json.loads(transitions(world.store, gid)[-1]["evidence"])
    assert evidence == {
        "path": "register_failed", "decided_by": DEC, "failure_ref": FAILURE_TEXT,
        **registered_bytes_evidence(world, aid),
    }
    # the critic's path still needs a failure_ref: there is no operator decision to state the failure
    _aid2, gid2 = world.failed()
    with pytest.raises(RegistrationRefusedError, match="failure_ref is required"):
        register_failed(world.store, gate_id=gid2, decided_by=DEC, by_launch=world.launch)


def test_the_normal_mismatch_block_is_unchanged(world):
    _aid, gid = world.gated()
    with pytest.raises(GateEntryConditionError):
        apply_union(world.store, gate_id=gid, by_launch=world.launch)


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


def _fail_cli(world, gid, *extra):
    return _run_cli(["gate", "fail-reproduction", "--program-root", str(world.root), "--id", gid, *extra])


def test_cli_fail_then_register_as_failed(world):
    aid, gid = world.gated()
    rc, env = _fail_cli(world, gid, "--reason", REASON, "--decided-by", FAIL_DEC, "--by-launch", world.launch)
    assert rc == 0, env
    assert env["result"]["state"] == "failed"
    assert env["result"]["verdict"] == "PASS_WITH_EDITS"
    assert env["nextActions"] == []  # the failure ref is the operator's to give: no placeholder action
    rc, env = _run_cli([
        "artifact", "register", "--program-root", str(world.root), "--id", aid, "--by-launch", world.launch,
        "--as-failed", "--failure-ref", FAILURE_TEXT, "--decided-by", DEC,
    ])
    assert rc == 0, env
    assert env["result"]["disposition"] == "registered_failed"


def test_cli_fail_refusal_carries_its_code(world):
    _aid, gid = world.gated(reproduction_status="match")
    rc, env = _fail_cli(world, gid, "--reason", REASON, "--decided-by", FAIL_DEC, "--by-launch", world.launch)
    assert rc == 1 and env["error"]["code"] == "fail_refused"
    assert get_gate(world.store, gid)["state"] == "gated"


@pytest.mark.parametrize("drop", ["--decided-by", "--reason", "--by-launch"])
def test_cli_fail_requires_its_flags(world, drop):
    _aid, gid = world.gated()
    flags = {"--reason": REASON, "--decided-by": FAIL_DEC, "--by-launch": world.launch}
    flags.pop(drop)
    argv = [x for kv in flags.items() for x in kv]
    with pytest.raises(SystemExit) as exc:
        with redirect_stdout(io.StringIO()):
            _fail_cli(world, gid, *argv)
    assert exc.value.code == 2
    assert get_gate(world.store, gid)["state"] == "gated"


# ---------------------------------------------------------------------------
# S-3: the reserved-path guard never crashes on an unhashable path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [[OPERATOR_FAIL_PATH], {"a": 1}], ids=["list_path", "object_path"],
)
def test_advance_with_an_unhashable_evidence_path_gets_the_normal_envelope(world, path):
    """A ``path`` that is a list or an object is not a reserved path (those are strings): the guard lets the
    generic transition through as it did before the guard existed, with an envelope, never a traceback. And
    the row it writes still does not pass for the operator's decision."""
    _aid, gid = world.gated()
    rc, env = _run_cli([
        "gate", "advance", "--program-root", str(world.root), "--id", gid, "--to", "failed",
        "--by-launch", world.launch, "--evidence", json.dumps({"path": path}),
    ])
    assert rc == 0, env
    assert env["ok"] is True and env["result"]["state"] == "failed"
    with pytest.raises(RegistrationRefusedError, match="fail-reproduction"):
        register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
