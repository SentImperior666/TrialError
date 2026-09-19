"""``trialerror.artifacts.checks`` — the M10 doctor checks. Auto-discovery (no
import needed) plus planted-fixture adversarial cases for each, mirroring
``tests/test_stores_checks.py``'s style for M1's own checks."""

from __future__ import annotations

from trialerror.artifacts.gates import apply_union, open_gate, record_verdict, submit_gate
from trialerror.artifacts.registry import create_artifact, register_artifact
from trialerror.stores import insert, update
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _run(names, program_root):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    results = run_checks(ctx, only=names)
    return {r.name: r for r in results}


def _seed_launch(store) -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
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


def _add_template(store, type_key: str, *, gated: bool) -> str:
    insert(
        store, "template",
        {"type_key": type_key, "title": type_key, "version": "1", "path": f"templates/{type_key}.md", "gated": int(gated)},
    )
    return type_key


def test_checks_are_auto_discovered_without_import():
    clear_registry()
    discover_and_register_checks()
    from trialerror.util.doctor import registered_checks

    names = set(registered_checks())
    assert {"gated_type_without_gate", "orphan_gate_transition", "gate_illegal_transition_history"} <= names


def test_all_three_skip_when_ops_db_absent(tmp_path, platform_root):
    empty_program = tmp_path / "never_initialized"
    results = _run(
        ["gated_type_without_gate", "orphan_gate_transition", "gate_illegal_transition_history"], empty_program
    )
    for r in results.values():
        assert r.status == "skip"


def test_all_three_pass_on_a_clean_full_lifecycle(store, program_root):
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", critic_launch=launch_id)
    apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
    register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    results = _run(
        ["gated_type_without_gate", "orphan_gate_transition", "gate_illegal_transition_history"], program_root
    )
    for name, r in results.items():
        assert r.status == "pass", f"{name}: {r.message} {r.details}"


# ---- gated_type_without_gate ------------------------------------------------


def test_gated_type_without_gate_catches_bypassed_registration(store, program_root):
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    # bypass the validated write API on purpose: flip straight to
    # 'registered' with no gate at all (simulates a legacy import / a
    # direct SQL write that skipped trialerror.artifacts.registry entirely).
    update(store, "artifact", pk_column="artifact_id", pk_value=artifact["artifact_id"], changes={"status": "registered"})

    results = _run(["gated_type_without_gate"], program_root)
    r = results["gated_type_without_gate"]
    assert r.status == "fail"
    assert r.details["offenders"][0]["artifact_id"] == artifact["artifact_id"]


def test_gated_type_without_gate_ignores_ungated_registrations(store, program_root):
    launch_id = _seed_launch(store)
    _add_template(store, "note", gated=False)
    artifact = create_artifact(store, type_key="note", title="n", path="p", sha256="0" * 64, by_launch=launch_id)
    register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    results = _run(["gated_type_without_gate"], program_root)
    assert results["gated_type_without_gate"].status == "pass"


# ---- orphan_gate_transition --------------------------------------------------


def test_orphan_gate_transition_catches_a_dangling_gate_id(store, program_root):
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)

    # bypass same-file FK enforcement on purpose (foreign_keys pragma OFF
    # for this one write) -- simulates a legacy row / a gate row deleted
    # after its transition history was written.
    store.ops.execute("PRAGMA foreign_keys = OFF")
    try:
        with store.ops:
            store.ops.execute(
                "INSERT INTO gate_transition(gate_id, from_state, to_state, ts, by_launch) VALUES (?,?,?,?,?)",
                ("CR-does-not-exist", "draft", "submitted", now(), launch_id),
            )
    finally:
        store.ops.execute("PRAGMA foreign_keys = ON")

    results = _run(["orphan_gate_transition"], program_root)
    r = results["orphan_gate_transition"]
    assert r.status == "fail"
    assert any(o["gate_id"] == "CR-does-not-exist" for o in r.details["offenders"])


# ---- gate_illegal_transition_history -----------------------------------------


def test_gate_illegal_transition_history_catches_a_planted_illegal_edge(store, program_root):
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])

    insert(
        store, "gate_transition",
        {"gate_id": gate["gate_id"], "from_state": "draft", "to_state": "registered", "ts": now(), "by_launch": launch_id},
    )

    results = _run(["gate_illegal_transition_history"], program_root)
    r = results["gate_illegal_transition_history"]
    assert r.status == "fail"
    assert any(e["to_state"] == "registered" for e in r.details["illegal_edges"])


def test_gate_illegal_transition_history_catches_a_state_mismatch(store, program_root):
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    # bypass the write API: force gate.state back to 'draft' even though
    # its own transition history says the last logged move was to 'submitted'.
    update(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"], changes={"state": "draft"})

    results = _run(["gate_illegal_transition_history"], program_root)
    r = results["gate_illegal_transition_history"]
    assert r.status == "fail"
    assert any(m["gate_id"] == gate["gate_id"] for m in r.details["state_mismatches"])
