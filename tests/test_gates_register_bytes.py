"""Registration binds to bytes the gate holds.

* A registration reads the artifact's file, or ``--file``, and refuses unless
  its sha256 is one the gate holds: the submitted hash on the artifact, or the
  corrected hash the gate itself recorded when its last blocking edit was
  verified (``gate.post_edit_sha256``).
* An operator-failed gate registers as failed on its own record: the decision
  and the failing checks, frozen as they stood.
* A decided gate's basis cannot be rewritten by a later suite run.
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout

import pytest

from tests.test_artifacts_register_dispositions import (
    CHECK,
    DEC,
    DEVIATION_TEXT,
    FAILURE_TEXT,
    World,
    _suite_record,
)
from trialerror.artifacts.checks import check_registration_disposition_consistent
from trialerror.artifacts.errors import RegistrationRefusedError
from trialerror.artifacts.gates import (
    OPERATOR_FAIL_PATH,
    advance_gate,
    fail_on_reproduction,
    get_gate,
    register_failed,
    register_with_deviation,
    verify_edit,
)
from trialerror.artifacts.registry import get_artifact
from trialerror.cli import main
from trialerror.events.api import append_event
from trialerror.util.doctor import DoctorContext

FAIL_DEC = "DECISION-TEST-9"
REASON = "the round's gate suite could not reproduce its admission order"


@pytest.fixture()
def world(store, program_root):
    return World(store, program_root)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_of(world, aid):
    return world.root / get_artifact(world.store, aid)["path"]


def transitions(store, gid):
    return [dict(r) for r in store.ops.execute("SELECT * FROM gate_transition WHERE gate_id=? ORDER BY id", (gid,))]


def last_evidence(world, gid):
    return json.loads(transitions(world.store, gid)[-1]["evidence"])


def events(world, event_type):
    rows = world.store.ops.execute("SELECT payload FROM event WHERE type = ?", (event_type,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def corrected_file_world(world, **gated_kwargs):
    """A gate whose live file has moved on from what was submitted (the shape
    of a report corrected after review), with a frozen copy of the submitted
    bytes kept elsewhere, and a gate that recorded no corrected hash (the edits
    were verified before the gate recorded corrected hashes)."""
    aid, gid = world.gated(verify=False, **gated_kwargs)
    live = file_of(world, aid)
    submitted = live.read_bytes()
    frozen = world.root / "gate_folder" / "frozen_submitted.md"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_bytes(submitted)
    live.write_bytes(submitted + b"\nstatus: corrected after review\n")
    return aid, gid, frozen, submitted


# ---------------------------------------------------------------------------
# register_failed on the operator's path
# ---------------------------------------------------------------------------


def test_operator_failed_gate_registers_with_a_copy_of_the_submitted_bytes_and_no_failure_ref(world, program_root):
    aid, gid, frozen, submitted = corrected_file_world(world)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)

    artifact = register_failed(
        world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file=str(frozen),
        note="the corrected report states the failure in its own text",
    )
    assert artifact["status"] == "registered" and artifact["disposition"] == "registered_failed"
    evidence = last_evidence(world, gid)
    assert "failure_ref" not in evidence
    assert evidence["path"] == "register_failed" and evidence["decided_by"] == DEC
    assert evidence["failure_basis"] == {
        "kind": OPERATOR_FAIL_PATH,
        "decided_by": FAIL_DEC,
        "failing_checks": [{"name": CHECK, "message": f"{CHECK} failed"}],
    }
    assert evidence["registered_bytes"] == "submitted"
    assert evidence["registered_sha256"] == sha(submitted) == get_artifact(world.store, aid)["sha256"]
    assert evidence["registered_path"] == str(frozen.resolve())
    assert evidence["note"] == "the corrected report states the failure in its own text"
    result = check_registration_disposition_consistent(DoctorContext(program_root=program_root))
    assert result.status == "pass", result.details


def test_the_live_file_alone_is_refused_when_it_matches_neither_hash_on_record(world):
    aid, gid, frozen, submitted = corrected_file_world(world)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)
    live_hash = sha(file_of(world, aid).read_bytes())
    with pytest.raises(RegistrationRefusedError) as exc:
        register_failed(world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch)
    message = str(exc.value)
    assert live_hash in message and sha(submitted) in message and "corrected hash none" in message
    assert "--file" in message
    assert get_gate(world.store, gid)["state"] == "failed"
    assert get_artifact(world.store, aid)["status"] == "in_gate"


def test_a_file_whose_hash_is_neither_on_record_is_refused_with_both_hashes_named(world):
    aid, gid, frozen, submitted = corrected_file_world(world)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)
    other = world.root / "gate_folder" / "another_copy.md"
    other.write_bytes(submitted + b"one more line\n")
    with pytest.raises(RegistrationRefusedError) as exc:
        register_failed(world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file=str(other))
    message = str(exc.value)
    assert sha(other.read_bytes()) in message and sha(submitted) in message


def test_a_missing_file_is_refused(world):
    aid, gid, frozen, submitted = corrected_file_world(world)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)
    with pytest.raises(RegistrationRefusedError, match="is missing"):
        register_failed(world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file="no/such/copy.md")


def test_a_reproduction_ref_that_is_not_a_gate_suite_record_is_refused(world):
    ref = json.dumps({"kind": "byte_compare", "checks": []})
    aid, gid, frozen, _submitted = corrected_file_world(world, reproduction_ref=ref)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)
    with pytest.raises(RegistrationRefusedError, match="failed checks cannot be read from the gate's record"):
        register_failed(world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file=str(frozen))
    assert get_gate(world.store, gid)["state"] == "failed"


def test_a_failure_ref_given_on_the_operator_path_must_still_be_in_the_registered_bytes(world):
    aid, gid, frozen, submitted = corrected_file_world(world)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)
    with pytest.raises(RegistrationRefusedError, match="does not contain"):
        register_failed(
            world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file=str(frozen),
            failure_ref="status: corrected after review",  # only in the corrected live file
        )
    register_failed(
        world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file=str(frozen), failure_ref=FAILURE_TEXT
    )
    evidence = last_evidence(world, gid)
    assert evidence["failure_ref"] == FAILURE_TEXT and "failure_basis" in evidence


# ---------------------------------------------------------------------------
# verify_edit records the corrected bytes' hash, exactly when the last blocking edit is verified
# ---------------------------------------------------------------------------


def two_blocking_edits(world):
    aid, gid = world.gated(
        verify=False,
        edits=[{"text": "fix a heading", "blocking": True}, {"text": "fix a table", "blocking": True}],
    )
    ids = [e["edit_id"] for e in json.loads(get_gate(world.store, gid)["edits"])]
    return aid, gid, ids


def test_verify_edit_records_the_post_edit_hash_when_the_last_blocking_edit_is_verified(world):
    aid, gid, (first, second) = two_blocking_edits(world)
    live = file_of(world, aid)

    live.write_bytes(live.read_bytes() + b"\nfirst correction\n")
    row = verify_edit(world.store, gate_id=gid, edit_id=first, by_launch=world.launch, ts="2026-09-28T10:00:00.000Z")
    assert row["post_edit_sha256"] is None and row["post_edit_recorded"] is False
    entry = next(e for e in json.loads(row["edits"]) if e["edit_id"] == first)
    assert entry["verified"] is True and entry["verified_ts"] == "2026-09-28T10:00:00.000Z"

    live.write_bytes(live.read_bytes() + b"second correction\n")
    corrected = sha(live.read_bytes())
    row = verify_edit(world.store, gate_id=gid, edit_id=second, by_launch=world.launch, ts="2026-09-28T10:05:00.000Z")
    assert row["post_edit_sha256"] == corrected and row["post_edit_recorded"] is True
    assert row["post_edit_ts"] == "2026-09-28T10:05:00.000Z"
    assert row["post_edit_sha256"] != get_artifact(world.store, aid)["sha256"]

    # verifying an already-verified edit again is not a transition: the record stands
    live.write_bytes(live.read_bytes() + b"a later change\n")
    row = verify_edit(world.store, gate_id=gid, edit_id=second, by_launch=world.launch)
    assert row["post_edit_sha256"] == corrected and row["post_edit_recorded"] is False


def test_a_gate_with_only_non_blocking_edits_records_no_corrected_hash(world):
    aid, gid = world.gated(verify=False, edits=[{"text": "a note", "blocking": False}])
    edit_id = json.loads(get_gate(world.store, gid)["edits"])[0]["edit_id"]
    row = verify_edit(world.store, gate_id=gid, edit_id=edit_id, by_launch=world.launch)
    assert row["post_edit_sha256"] is None and row["post_edit_ts"] is None


def test_an_unreadable_file_leaves_the_hash_null_and_says_so(world):
    aid, gid = world.gated(verify=False)
    edit_id = json.loads(get_gate(world.store, gid)["edits"])[0]["edit_id"]
    file_of(world, aid).unlink()
    row = verify_edit(world.store, gate_id=gid, edit_id=edit_id, by_launch=world.launch)
    assert row["post_edit_sha256"] is None and row["post_edit_recorded"] is False
    assert "could not be read" in row["post_edit_note"]
    assert json.loads(row["edits"])[0]["verified"] is True  # the verification itself stands


def test_no_verb_and_no_event_can_set_the_corrected_hash(world):
    aid, gid = world.gated(verify=False)
    forged = "f" * 64
    with pytest.raises(Exception):
        advance_gate(
            world.store, gate_id=gid, to_state="union_applied", by_launch=world.launch,
            evidence={"post_edit_sha256": forged},
        )
    append_event(world.store, event_type="gate_post_edit", payload={"gate_id": gid, "sha256": forged})
    assert get_gate(world.store, gid)["post_edit_sha256"] is None


# ---------------------------------------------------------------------------
# register_with_deviation binds to the recorded corrected bytes, and only to those
# ---------------------------------------------------------------------------


def test_register_with_deviation_accepts_the_recorded_corrected_bytes(world):
    aid, gid = world.gated(verify=False)
    live = file_of(world, aid)
    submitted_hash = get_artifact(world.store, aid)["sha256"]
    live.write_bytes(live.read_bytes() + b"\nstatus: corrected\n")
    for edit in json.loads(get_gate(world.store, gid)["edits"]):
        verify_edit(world.store, gate_id=gid, edit_id=edit["edit_id"], by_launch=world.launch)
    gate = get_gate(world.store, gid)
    assert gate["post_edit_sha256"] == sha(live.read_bytes()) != submitted_hash

    artifact = register_with_deviation(
        world.store, gate_id=gid, deviations=[world.deviation()], decided_by=DEC, by_launch=world.launch,
        note="registered against the corrected file",
    )
    assert artifact["disposition"] == "registered_with_deviation"
    evidence = last_evidence(world, gid)
    assert evidence["registered_bytes"] == "post_edit"
    assert evidence["registered_sha256"] == gate["post_edit_sha256"]
    assert evidence["registered_path"] == str(live.resolve())
    assert evidence["note"] == "registered against the corrected file"


def test_a_hash_set_by_any_other_route_does_not_count(world):
    """The file changed after the gate recorded its hash; an event (which anyone
    can write) naming the file's new hash makes no difference: only the gate's
    own column counts."""
    aid, gid = world.gated()  # verify=True: the gate recorded the file as it stood
    live = file_of(world, aid)
    live.write_bytes(live.read_bytes() + b"\nedited after the gate recorded its hash\n")
    forged = sha(live.read_bytes())
    append_event(world.store, event_type="gate_post_edit", payload={"gate_id": gid, "sha256": forged})
    with pytest.raises(RegistrationRefusedError) as exc:
        register_with_deviation(
            world.store, gate_id=gid, deviations=[world.deviation()], decided_by=DEC, by_launch=world.launch
        )
    assert forged in str(exc.value)
    assert get_gate(world.store, gid)["state"] == "gated"


def test_the_submitted_bytes_still_register_through_file_when_the_live_file_moved_on(world):
    aid, gid, frozen, submitted = corrected_file_world(world)
    for edit in json.loads(get_gate(world.store, gid)["edits"]):
        verify_edit(world.store, gate_id=gid, edit_id=edit["edit_id"], by_launch=world.launch)
    assert get_gate(world.store, gid)["post_edit_sha256"] == sha(file_of(world, aid).read_bytes())
    register_with_deviation(
        world.store, gate_id=gid, deviations=[world.deviation()], decided_by=DEC, by_launch=world.launch,
        file=str(frozen),
    )
    evidence = last_evidence(world, gid)
    assert evidence["registered_bytes"] == "submitted" and evidence["registered_sha256"] == sha(submitted)
    assert evidence["registered_path"] == str(frozen.resolve())


# ---------------------------------------------------------------------------
# the failure basis is frozen
# ---------------------------------------------------------------------------


def fake_suite(monkeypatch, *, overall):
    from trialerror.eval import gate_suites

    passed = overall == "PASS"
    checks = [
        {"name": CHECK, "passed": passed, "score": 1.0 if passed else 0.0, "message": "re-run message"},
        {"name": "another_check", "passed": True, "score": 1.0, "message": "ok"},
    ]
    monkeypatch.setattr(
        gate_suites,
        "run_gate_suite",
        lambda suite_id, subject, timeout=60.0: {
            "suite_id": suite_id, "returncode": 0 if passed else 1, "overall": overall,
            "checks": checks, "stdout_tail": "",
        },
    )
    return gate_suites


def verdict_rows(world):
    return world.store.knowledge.execute("SELECT count(*) FROM verdict").fetchone()[0]


def test_a_suite_rerun_on_a_decided_gate_is_recorded_as_an_event_only(world, monkeypatch):
    aid, gid, frozen, submitted = corrected_file_world(world)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)
    before_gate = get_gate(world.store, gid)
    before_transitions = transitions(world.store, gid)
    before_verdicts = verdict_rows(world)

    gate_suites = fake_suite(monkeypatch, overall="PASS")
    result = gate_suites.run_gate_suite_for_gate(
        world.store, gate_id=gid, suite_id="aiif_round", subject={}, issued_by_launch=world.launch
    )
    assert result["frozen"] is True and result["reproduction_status"] == "match" and result["verdict"] is None

    assert get_gate(world.store, gid) == before_gate  # every gate column as it was
    assert transitions(world.store, gid) == before_transitions
    assert verdict_rows(world) == before_verdicts  # nothing written to the knowledge store
    (rerun,) = events(world, "gate_suite_rerun")
    assert rerun["gate_id"] == gid and rerun["suite_id"] == "aiif_round" and rerun["reproduction_status"] == "match"
    assert [c["name"] for c in rerun["checks"]] == [CHECK, "another_check"]

    # the registration still succeeds, on the basis the gate was decided on
    register_failed(world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file=str(frozen))
    assert last_evidence(world, gid)["failure_basis"]["failing_checks"][0]["name"] == CHECK


def test_a_suite_rerun_on_a_registered_gate_is_also_an_event_only(world, monkeypatch):
    aid, gid, frozen, submitted = corrected_file_world(world)
    fail_on_reproduction(world.store, gate_id=gid, decided_by=FAIL_DEC, reason=REASON, by_launch=world.launch)
    register_failed(world.store, gate_id=gid, decided_by=DEC, by_launch=world.launch, file=str(frozen))
    before_gate = get_gate(world.store, gid)
    before_verdicts = verdict_rows(world)
    gate_suites = fake_suite(monkeypatch, overall="PASS")
    gate_suites.run_gate_suite_for_gate(
        world.store, gate_id=gid, suite_id="aiif_round", subject={}, issued_by_launch=world.launch
    )
    assert get_gate(world.store, gid) == before_gate
    assert verdict_rows(world) == before_verdicts
    assert len(events(world, "gate_suite_rerun")) == 1


def test_a_suite_run_on_an_undecided_gate_still_writes_the_gate_and_a_verdict(world, monkeypatch):
    aid, gid = world.gated(reproduction_status="unrun", reproduction_ref="")
    before_verdicts = verdict_rows(world)
    gate_suites = fake_suite(monkeypatch, overall="FAIL")
    result = gate_suites.run_gate_suite_for_gate(
        world.store, gate_id=gid, suite_id="aiif_round", subject={}, issued_by_launch=world.launch
    )
    assert "frozen" not in result and result["verdict"] is not None
    gate = get_gate(world.store, gid)
    assert gate["reproduction_status"] == "mismatch"
    assert json.loads(gate["reproduction_ref"])["kind"] == "gate_suite"
    assert verdict_rows(world) == before_verdicts + 1
    assert events(world, "gate_suite_rerun") == []


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


def test_cli_fail_on_reproduction_then_register_as_failed_with_a_copy_of_the_submitted_file(world):
    aid, gid, frozen, submitted = corrected_file_world(world)
    rc, env = run_cli([
        "gate", "fail-reproduction", "--program-root", str(world.root), "--id", gid,
        "--reason", REASON, "--decided-by", FAIL_DEC, "--by-launch", world.launch,
    ])
    assert rc == 0, env
    # without --file: the live (corrected) file matches neither hash on record
    rc, env = run_cli([
        "artifact", "register", "--program-root", str(world.root), "--id", aid, "--by-launch", world.launch,
        "--as-failed", "--decided-by", DEC,
    ])
    assert rc == 1 and env["error"]["code"] == "registration_refused" and "--file" in env["error"]["message"]
    rc, env = run_cli([
        "artifact", "register", "--program-root", str(world.root), "--id", aid, "--by-launch", world.launch,
        "--as-failed", "--file", str(frozen), "--decided-by", DEC, "--note", "corrected report says so",
    ])
    assert rc == 0, env
    assert env["result"]["disposition"] == "registered_failed"

    rc, shown = run_cli(["artifact", "show", "--program-root", str(world.root), "--id", aid])
    assert rc == 0
    assert shown["result"]["registered_sha256"] == sha(submitted)
    assert shown["result"]["registered_bytes"] == "submitted"
    assert shown["result"]["registered_path"] == str(frozen.resolve())
    assert shown["result"]["registered_note"] == "corrected report says so"


def test_cli_gate_show_prints_the_recorded_corrected_hash(world):
    aid, gid = world.gated(verify=False)
    rc, env = run_cli(["gate", "show", "--program-root", str(world.root), "--id", gid])
    assert rc == 0 and env["result"]["post_edit_sha256"] is None and env["result"]["post_edit_ts"] is None
    edit_id = json.loads(get_gate(world.store, gid)["edits"])[0]["edit_id"]
    rc, env = run_cli([
        "gate", "verify-edit", "--program-root", str(world.root), "--id", gid, "--edit-id", edit_id,
        "--by-launch", world.launch,
    ])
    assert rc == 0 and env["result"]["post_edit_recorded"] is True
    rc, env = run_cli(["gate", "show", "--program-root", str(world.root), "--id", gid])
    assert env["result"]["post_edit_sha256"] == sha(file_of(world, aid).read_bytes())
    assert env["result"]["post_edit_ts"]
    rc, env = run_cli(["gate", "show", "--program-root", str(world.root), "--id", "CR-999"])
    assert rc == 1 and env["error"]["code"] == "not_found"
