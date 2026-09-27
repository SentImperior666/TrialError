"""``register_with_deviation`` / ``register_failed``: the two registration end
states for a result whose gate did not pass cleanly."""

from __future__ import annotations

import hashlib
import json

import pytest

from trialerror.artifacts.checks import check_gate_illegal_transition_history, check_registration_disposition_consistent
from trialerror.artifacts.errors import IllegalTransitionError, RegistrationRefusedError
from trialerror.artifacts.gates import (
    advance_gate,
    apply_union,
    get_gate,
    open_gate,
    record_verdict,
    register_failed,
    register_with_deviation,
    submit_gate,
    verify_edit,
)
from trialerror.artifacts.registry import create_artifact, get_artifact, register_artifact
from trialerror.stores import insert, update
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

DEVIATION_TEXT = "deviation 22: no admission-order escrow"
FAILURE_TEXT = "the run failed: the control arm was never seated"
CHECK = "admission_order_hash_matches"
DEC = "DEC-2"


def _seed_launch(store) -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store, "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-test",
            "session_id": session_id, "agent_kind": "tester", "model_class": "top", "model": "sonnet",
            "purpose": "fixture", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
        },
    )
    return launch_id


def _suite_record(failing=(CHECK,)):
    """A gate-suite record shaped like ``run_gate_suite_for_gate``'s: 14 checks."""
    checks = [{"name": f"check_{i}", "passed": True, "score": 1.0, "message": "ok"} for i in range(14)]
    for i, name in enumerate(failing):
        checks[i] = {"name": name, "passed": False, "score": 0.0, "message": f"{name} failed"}
    checks[13] = {"name": "not_here", "passed": True, "status": "not_applicable", "score": None, "message": "n/a"}
    return json.dumps({"kind": "gate_suite", "suite_id": "aiif_round", "returncode": 1, "checks": checks})


class World:
    """One store, one launch, one gated template, and helpers that build an
    artifact file + gate at a chosen point."""

    def __init__(self, store, program_root):
        self.store = store
        self.root = program_root
        self.launch = _seed_launch(store)
        insert(
            store, "template",
            {"type_key": "report", "title": "report", "version": "1", "path": "templates/report.md", "gated": 1},
        )

    def artifact(self, text=None, *, sha_of=None):
        text = DEVIATION_TEXT + "\n" + FAILURE_TEXT + "\n" if text is None else text
        path = self.root / "artifacts" / f"{new_id('F')}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # bytes: no newline translation, so the sha is exact
        sha = hashlib.sha256((sha_of if sha_of is not None else text).encode("utf-8")).hexdigest()
        return create_artifact(
            self.store, type_key="report", title="r", path=str(path.relative_to(self.root)),
            sha256=sha, by_launch=self.launch,
        )

    def gated(self, *, reproduction_status="mismatch", reproduction_ref=None, verdict="PASS_WITH_EDITS",
              edits=None, verify=True, text=None, sha_of=None):
        artifact = self.artifact(text, sha_of=sha_of)
        gate = open_gate(self.store, artifact_id=artifact["artifact_id"])
        gid = gate["gate_id"]
        submit_gate(self.store, gate_id=gid, by_launch=self.launch)
        if edits is None and verdict == "PASS_WITH_EDITS":
            edits = [{"text": "fix a heading", "blocking": True}]
        record_verdict(
            self.store, gate_id=gid, verdict=verdict, critic_launch=self.launch, edits=edits,
            reproduction_ref=_suite_record() if reproduction_ref is None else reproduction_ref,
            reproduction_status=reproduction_status,
        )
        if verify and edits:
            for e in json.loads(get_gate(self.store, gid)["edits"]):
                verify_edit(self.store, gate_id=gid, edit_id=e["edit_id"], by_launch=self.launch)
        return artifact["artifact_id"], gid

    def failed(self, *, verdict="FAIL", text=None, sha_of=None):
        artifact = self.artifact(text, sha_of=sha_of)
        gate = open_gate(self.store, artifact_id=artifact["artifact_id"])
        gid = gate["gate_id"]
        submit_gate(self.store, gate_id=gid, by_launch=self.launch)
        if verdict is not None:
            record_verdict(self.store, gate_id=gid, verdict=verdict, critic_launch=self.launch)
        else:
            advance_gate(self.store, gate_id=gid, to_state="failed", by_launch=self.launch)
        return artifact["artifact_id"], gid

    def deviation(self, **changes):
        d = {"check": CHECK, "reason": "the round ran no rooms before the decision", "report_ref": DEVIATION_TEXT}
        d.update(changes)
        return d


@pytest.fixture()
def world(store, program_root):
    return World(store, program_root)


def register_dev(w, gid, deviations=None, decided_by=DEC, **kw):
    return register_with_deviation(
        w.store, gate_id=gid, deviations=[w.deviation()] if deviations is None else deviations,
        decided_by=decided_by, by_launch=w.launch, **kw,
    )


def transitions(store, gid):
    return [dict(r) for r in store.ops.execute("SELECT * FROM gate_transition WHERE gate_id=? ORDER BY id", (gid,))]


# --- 18: the graph, through the service ------------------------------------


def test_advance_gate_refuses_the_two_guarded_edges(world):
    _aid, gated_gid = world.gated()
    with pytest.raises(IllegalTransitionError) as exc:
        advance_gate(world.store, gate_id=gated_gid, to_state="registered", by_launch=world.launch)
    assert "register_with_deviation" in str(exc.value) and "register_failed" in str(exc.value)
    assert "artifact register --with-deviation" in str(exc.value) and "--as-failed" in str(exc.value)
    _aid, failed_gid = world.failed()
    with pytest.raises(IllegalTransitionError) as exc:
        advance_gate(world.store, gate_id=failed_gid, to_state="registered", by_launch=world.launch)
    assert "register_failed" in str(exc.value)
    assert get_gate(world.store, gated_gid)["state"] == "gated"
    assert get_gate(world.store, failed_gid)["state"] == "failed"


def test_doctor_transition_history_check_passes_after_both_paths(world, program_root):
    aid1, gid1 = world.gated()
    register_dev(world, gid1)
    aid2, gid2 = world.failed()
    register_failed(world.store, gate_id=gid2, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
    result = check_gate_illegal_transition_history(DoctorContext(program_root=program_root))
    assert result.status == "pass", result.details


# --- 19: register_with_deviation, happy ------------------------------------


def test_register_with_deviation_happy(world):
    aid, gid = world.gated()
    assert get_gate(world.store, gid)["reproduction_status"] == "mismatch"
    artifact = register_dev(world, gid)
    assert artifact["status"] == "registered" and artifact["disposition"] == "registered_with_deviation"
    assert artifact["registered_by_launch"] == world.launch and artifact["registered_ts"]
    gate = get_gate(world.store, gid)
    assert gate["state"] == "registered" and gate["disposition"] == "deviation_disclosed"
    recorded = json.loads(gate["deviation_ref"])
    assert recorded[0]["check"] == CHECK and recorded[0]["report_ref"] == DEVIATION_TEXT
    last = transitions(world.store, gid)[-1]
    assert (last["from_state"], last["to_state"]) == ("gated", "registered")
    evidence = json.loads(last["evidence"])
    assert evidence["path"] == "register_with_deviation" and evidence["decided_by"] == DEC
    assert evidence["deviations"][0]["check"] == CHECK


def test_register_with_deviation_supersedes(world):
    first_aid, first_gid = world.gated()
    register_dev(world, first_gid)
    aid, gid = world.gated()
    register_dev(world, gid, supersedes=first_aid)
    assert get_artifact(world.store, first_aid)["status"] == "superseded"
    assert get_artifact(world.store, aid)["supersedes"] == first_aid


# --- 20: register_with_deviation, refusals ---------------------------------


def refused(world, gid, **kw):
    with pytest.raises(RegistrationRefusedError) as exc:
        register_dev(world, gid, **kw)
    return str(exc.value)


def assert_untouched(world, aid, gid, state="gated"):
    assert get_gate(world.store, gid)["state"] == state
    assert get_gate(world.store, gid)["disposition"] is None
    assert get_artifact(world.store, aid)["status"] == "in_gate"
    assert get_artifact(world.store, aid)["disposition"] is None


def test_refused_when_the_suite_matched(world):
    aid, gid = world.gated(reproduction_status="match")
    assert "normal path" in refused(world, gid)
    assert_untouched(world, aid, gid)


def test_refused_when_unrun(world):
    aid, gid = world.gated(reproduction_status="unrun")
    assert "unrun" in refused(world, gid)
    assert_untouched(world, aid, gid)


def test_refused_when_reproduction_ref_is_not_a_gate_suite_record(world):
    ref = json.dumps({"kind": "byte_compare", "checks": []})
    aid, gid = world.gated(reproduction_ref=ref)
    assert "not a gate-suite record" in refused(world, gid)
    aid2, gid2 = world.gated(reproduction_ref="not json at all")
    assert "not a gate-suite record" in refused(world, gid2)
    assert_untouched(world, aid, gid)


def test_refused_when_a_failing_check_is_not_covered(world):
    aid, gid = world.gated(reproduction_ref=_suite_record(failing=(CHECK, "card_cells_ge_2")))
    message = refused(world, gid)
    assert "card_cells_ge_2" in message and "not covered" in message
    assert_untouched(world, aid, gid)


def test_refused_when_a_deviation_names_a_passing_check(world):
    aid, gid = world.gated()
    message = refused(world, gid, deviations=[world.deviation(), world.deviation(check="check_3")])
    assert "check_3" in message
    assert_untouched(world, aid, gid)


def test_a_not_applicable_check_is_not_a_failing_check(world):
    aid, gid = world.gated()
    assert "not_here" in refused(world, gid, deviations=[world.deviation(), world.deviation(check="not_here")])


def test_refused_when_report_ref_is_not_in_the_file(world):
    aid, gid = world.gated()
    message = refused(world, gid, deviations=[world.deviation(report_ref="deviation 99: something else")])
    assert "does not contain" in message
    assert_untouched(world, aid, gid)


def test_refused_when_the_file_changed_since_submission(world):
    aid, gid = world.gated(sha_of="the text that was submitted")
    assert "sha256" in refused(world, gid)
    assert_untouched(world, aid, gid)


def test_refused_when_the_file_is_missing(world, program_root):
    aid, gid = world.gated()
    (program_root / get_artifact(world.store, aid)["path"]).unlink()
    assert "missing" in refused(world, gid)


def test_refused_when_a_blocking_edit_is_not_verified(world):
    aid, gid = world.gated(verify=False)
    assert "not yet verified" in refused(world, gid)
    assert_untouched(world, aid, gid)


def test_refused_when_the_verdict_is_not_a_pass(world):
    # a FAIL verdict lands in `failed`, so there is no `gated` gate to register
    aid, gid = world.failed()
    assert "'gated'" in refused(world, gid)


def test_refused_with_empty_decided_by(world):
    aid, gid = world.gated()
    assert "decided_by" in refused(world, gid, decided_by="  ")
    assert_untouched(world, aid, gid)


def test_refused_when_the_gate_is_not_gated(world):
    aid, gid = world.gated()
    # drive a second gate to union_applied through the normal path
    aid2 = world.artifact()["artifact_id"]
    gate2 = open_gate(world.store, artifact_id=aid2)["gate_id"]
    submit_gate(world.store, gate_id=gate2, by_launch=world.launch)
    record_verdict(world.store, gate_id=gate2, verdict="PASS", critic_launch=world.launch, reproduction_status="match")
    apply_union(world.store, gate_id=gate2, by_launch=world.launch)
    assert "'union_applied'" in refused(world, gate2)
    draft = open_gate(world.store, artifact_id=world.artifact()["artifact_id"])["gate_id"]
    assert "'draft'" in refused(world, draft)
    assert_untouched(world, aid, gid)


def test_refused_with_no_deviations(world):
    aid, gid = world.gated()
    assert "no deviation" in refused(world, gid, deviations=[])
    assert "needs a check, a reason and a report_ref" in refused(world, gid, deviations=[world.deviation(reason="")])


# --- 21 / 22: register_failed ------------------------------------------------


def test_register_failed_happy(world):
    aid, gid = world.failed()
    assert get_gate(world.store, gid)["verdict"] == "FAIL"
    artifact = register_failed(world.store, gate_id=gid, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
    assert artifact["status"] == "registered" and artifact["disposition"] == "registered_failed"
    gate = get_gate(world.store, gid)
    assert gate["state"] == "registered" and gate["disposition"] == "failure_registered"
    last = transitions(world.store, gid)[-1]
    assert (last["from_state"], last["to_state"]) == ("failed", "registered")
    assert json.loads(last["evidence"]) == {"path": "register_failed", "decided_by": DEC, "failure_ref": FAILURE_TEXT}
    # a failed result on the record is closed: no new gate on it
    with pytest.raises(ValueError):
        open_gate(world.store, artifact_id=aid)


def failed_refused(world, gid, **kw):
    args = {"failure_ref": FAILURE_TEXT, "decided_by": DEC}
    args.update(kw)
    with pytest.raises(RegistrationRefusedError) as exc:
        register_failed(world.store, gate_id=gid, by_launch=world.launch, **args)
    return str(exc.value)


def test_register_failed_refusals(world):
    aid, gid = world.failed(verdict=None)  # abandoned: failed with no verdict
    assert get_gate(world.store, gid)["verdict"] is None
    assert "abandoned" in failed_refused(world, gid)

    aid, gid = world.failed()
    assert "does not contain" in failed_refused(world, gid, failure_ref="a statement the artifact never makes")
    assert "failure_ref is required" in failed_refused(world, gid, failure_ref="")
    assert "decided_by" in failed_refused(world, gid, decided_by="")
    assert get_gate(world.store, gid)["state"] == "failed"
    assert get_artifact(world.store, aid)["status"] == "in_gate"

    aid, gid = world.failed(sha_of="another text")
    assert "sha256" in failed_refused(world, gid)

    _aid, gated_gid = world.gated()
    assert "'gated'" in failed_refused(world, gated_gid)


def test_a_reopened_artifact_cannot_be_registered_from_its_old_failed_gate(world):
    aid, old_gid = world.failed()
    open_gate(world.store, artifact_id=aid)  # a fresh review attempt after the failure
    assert "current gate" in failed_refused(world, old_gid)


# --- 23: the normal path is unchanged ----------------------------------------


def test_normal_registration_unchanged(world):
    artifact = world.artifact()
    gid = open_gate(world.store, artifact_id=artifact["artifact_id"])["gate_id"]
    submit_gate(world.store, gate_id=gid, by_launch=world.launch)
    record_verdict(world.store, gate_id=gid, verdict="PASS", critic_launch=world.launch, reproduction_status="match")
    apply_union(world.store, gate_id=gid, by_launch=world.launch)
    registered = register_artifact(world.store, artifact_id=artifact["artifact_id"], by_launch=world.launch)
    assert registered["status"] == "registered" and registered["disposition"] is None
    gate = get_gate(world.store, gid)
    assert gate["state"] == "registered" and gate["disposition"] is None and gate["deviation_ref"] is None
    assert json.loads(transitions(world.store, gid)[-1]["evidence"] or "null") is None


def test_mismatch_still_blocks_the_normal_path(world):
    aid, gid = world.gated()
    from trialerror.artifacts.errors import GateEntryConditionError

    with pytest.raises(GateEntryConditionError):
        apply_union(world.store, gate_id=gid, by_launch=world.launch)


# --- 25: the doctor check ------------------------------------------------------


def _both_registered(world):
    aid1, gid1 = world.gated()
    register_dev(world, gid1)
    aid2, gid2 = world.failed()
    register_failed(world.store, gate_id=gid2, failure_ref=FAILURE_TEXT, decided_by=DEC, by_launch=world.launch)
    return (aid1, gid1), (aid2, gid2)


def test_disposition_check_passes_on_consistent_rows(world, program_root):
    _both_registered(world)
    result = check_registration_disposition_consistent(DoctorContext(program_root=program_root))
    assert result.status == "pass", result.details
    assert result.details["checked"] == 2


def test_disposition_check_skips_without_a_store(tmp_path, platform_root):
    result = check_registration_disposition_consistent(DoctorContext(program_root=tmp_path / "none"))
    assert result.status == "skip"


def test_disposition_check_ignores_normal_registrations(world, program_root):
    artifact = world.artifact()
    gid = open_gate(world.store, artifact_id=artifact["artifact_id"])["gate_id"]
    submit_gate(world.store, gate_id=gid, by_launch=world.launch)
    record_verdict(world.store, gate_id=gid, verdict="PASS", critic_launch=world.launch, reproduction_status="match")
    apply_union(world.store, gate_id=gid, by_launch=world.launch)
    register_artifact(world.store, artifact_id=artifact["artifact_id"], by_launch=world.launch)
    result = check_registration_disposition_consistent(DoctorContext(program_root=program_root))
    assert result.status == "pass" and result.details["checked"] == 0


@pytest.mark.parametrize(
    "corrupt",
    [
        ("gate", {"disposition": "failure_registered"}, "gate disposition"),  # gate disagrees with the artifact
        ("artifact", {"disposition": "registered_failed"}, "artifact disposition"),  # artifact disagrees with the gate
        ("gate", {"disposition": None}, "gate disposition"),  # the gate lost it
        ("artifact", {"disposition": None}, "no disposition"),  # the artifact lost it
    ],
)
def test_disposition_check_reports_a_corrupted_row(world, program_root, corrupt):
    (aid1, gid1), _other = _both_registered(world)
    table, changes, needle = corrupt
    pk = ("gate_id", gid1) if table == "gate" else ("artifact_id", aid1)
    update(world.store, table, pk_column=pk[0], pk_value=pk[1], changes=changes)
    result = check_registration_disposition_consistent(DoctorContext(program_root=program_root))
    assert result.status == "fail", result.details
    offender = result.details["offenders"][0]
    assert offender["artifact_id"] == aid1
    assert any(needle in problem for problem in offender["problems"]), offender


def test_disposition_check_reports_a_transition_with_the_wrong_path(world, program_root):
    (aid1, gid1), _other = _both_registered(world)
    world.store.ops.execute(
        "UPDATE gate_transition SET evidence = ? WHERE gate_id = ? AND to_state = 'registered'",
        (json.dumps({"path": "register_failed"}), gid1),
    )
    world.store.ops.commit()
    result = check_registration_disposition_consistent(DoctorContext(program_root=program_root))
    assert result.status == "fail"
    assert "last transition" in result.details["offenders"][0]["problems"][0]
