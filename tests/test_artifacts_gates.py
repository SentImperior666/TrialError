"""``trialerror.artifacts.gates`` — the gate mutation service. Design Section 12
(M10 row) + Section 4.2 (F10 resolution).

Seeding note: deliberately does NOT reuse ``tests._store_fixtures.
populate_one_of_everything`` — that M1 fixture plants its own artifact/gate/
gate_transition rows purely for schema round-trip coverage (its gate row
is left at ``state='draft'`` even though it also inserts a
``draft -> submitted`` ``gate_transition`` row, which is exactly the kind
of business-logic inconsistency ``trialerror.artifacts.checks`` exists to catch
— see ``tests/test_artifacts_checks.py``). Using it here would pollute
every "clean slate" assertion in this file. Instead each test seeds its
own minimal account/session/launch/template via the small local
``_seed_launch``/``_add_template`` helpers below, mirroring
``tests/test_budget_cli.py``'s own local-seeding style.
"""

from __future__ import annotations

import json

import pytest

from trialerror.artifacts.errors import GateEntryConditionError, IllegalTransitionError
from trialerror.artifacts.gates import (
    advance_gate,
    apply_union,
    get_gate,
    open_gate,
    record_verdict,
    submit_gate,
    verify_edit,
)
from trialerror.artifacts.registry import create_artifact
from trialerror.stores import get, insert, update
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _seed_launch(store) -> tuple[str, str, str]:
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
    return account_id, session_id, launch_id


def _add_template(store, type_key: str, *, gated: bool) -> str:
    insert(
        store, "template",
        {"type_key": type_key, "title": type_key, "version": "1", "path": f"templates/{type_key}.md", "gated": int(gated)},
    )
    return type_key


def _seed_artifact(store, *, gated: bool) -> tuple[str, str, str]:
    """Returns ``(launch_id, type_key, artifact_id)`` for a fresh draft
    artifact of a fresh template."""
    _account, _session, launch_id = _seed_launch(store)
    type_key = _add_template(store, "gated-type" if gated else "ungated-type", gated=gated)
    artifact = create_artifact(store, type_key=type_key, title="t", path="artifacts/t.md", sha256="0" * 64, by_launch=launch_id)
    return launch_id, type_key, artifact["artifact_id"]


# ---- open_gate ------------------------------------------------------------


def test_open_gate_creates_draft_gate_and_links_artifact(store):
    _launch, _type, artifact_id = _seed_artifact(store, gated=True)

    gate = open_gate(store, artifact_id=artifact_id)

    assert gate["state"] == "draft"
    assert gate["artifact_id"] == artifact_id
    artifact = get(store, "artifact", pk_column="artifact_id", pk_value=artifact_id)
    assert artifact["status"] == "in_gate"
    assert artifact["gate_id"] == gate["gate_id"]
    # No gate_transition row for the opening act (no from_state exists yet).
    n = store.ops.execute("SELECT COUNT(*) FROM gate_transition WHERE gate_id = ?", (gate["gate_id"],)).fetchone()[0]
    assert n == 0


def test_open_gate_refuses_unknown_artifact(store):
    with pytest.raises(ValueError, match="no such artifact"):
        open_gate(store, artifact_id="ART-does-not-exist")


def test_open_gate_refuses_when_artifact_already_registered(store):
    launch_id, type_key, artifact_id = _seed_artifact(store, gated=False)
    from trialerror.artifacts.registry import register_artifact

    register_artifact(store, artifact_id=artifact_id, by_launch=launch_id)

    with pytest.raises(ValueError, match="already 'registered'"):
        open_gate(store, artifact_id=artifact_id)


def test_open_gate_refuses_a_second_open_gate(store):
    _launch, _type, artifact_id = _seed_artifact(store, gated=True)
    open_gate(store, artifact_id=artifact_id)

    with pytest.raises(ValueError, match="already has an open gate"):
        open_gate(store, artifact_id=artifact_id)


def test_open_gate_allows_reopen_after_failed(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    first = open_gate(store, artifact_id=artifact_id)
    advance_gate(store, gate_id=first["gate_id"], to_state="failed", by_launch=launch_id)

    second = open_gate(store, artifact_id=artifact_id)

    assert second["gate_id"] != first["gate_id"]
    artifact = get(store, "artifact", pk_column="artifact_id", pk_value=artifact_id)
    assert artifact["gate_id"] == second["gate_id"]
    assert artifact["status"] == "in_gate"


# ---- submit_gate / advance_gate (illegal transitions) ---------------------


def test_submit_gate_moves_draft_to_submitted_and_logs_transition(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate = open_gate(store, artifact_id=artifact_id)

    updated = submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id, evidence={"note": "ready"})

    assert updated["state"] == "submitted"
    row = store.ops.execute(
        "SELECT * FROM gate_transition WHERE gate_id = ?", (gate["gate_id"],)
    ).fetchone()
    assert row["from_state"] == "draft"
    assert row["to_state"] == "submitted"
    assert row["by_launch"] == launch_id
    assert json.loads(row["evidence"]) == {"note": "ready"}


def test_submit_gate_twice_is_illegal(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate = open_gate(store, artifact_id=artifact_id)
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)

    with pytest.raises(IllegalTransitionError):
        submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)


def test_advance_gate_refuses_unknown_gate(store):
    _launch, _type, _artifact_id = _seed_artifact(store, gated=True)
    _account, _session, launch_id = _seed_launch(store)
    with pytest.raises(ValueError, match="no such gate"):
        advance_gate(store, gate_id="CR-nope", to_state="submitted", by_launch=launch_id)


def test_advance_gate_refuses_unknown_launch(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate = open_gate(store, artifact_id=artifact_id)
    with pytest.raises(XidTargetMissingError):
        advance_gate(store, gate_id=gate["gate_id"], to_state="submitted", by_launch="LNCH-does-not-exist")
    # refused before any write landed
    assert get_gate(store, gate["gate_id"])["state"] == "draft"


def test_advance_gate_draft_to_failed_is_a_legal_abandon_path(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate = open_gate(store, artifact_id=artifact_id)

    updated = advance_gate(store, gate_id=gate["gate_id"], to_state="failed", by_launch=launch_id)

    assert updated["state"] == "failed"


def test_advance_gate_draft_to_registered_is_illegal(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate = open_gate(store, artifact_id=artifact_id)

    with pytest.raises(IllegalTransitionError):
        advance_gate(store, gate_id=gate["gate_id"], to_state="registered", by_launch=launch_id)
    assert get_gate(store, gate["gate_id"])["state"] == "draft"


# ---- record_verdict ---------------------------------------------------------


def _to_submitted(store, launch_id: str, artifact_id: str) -> str:
    gate = open_gate(store, artifact_id=artifact_id)
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    return gate["gate_id"]


def test_record_verdict_requires_submitted_state(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate = open_gate(store, artifact_id=artifact_id)  # still draft

    with pytest.raises(ValueError, match="must be 'submitted'"):
        record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", critic_launch=launch_id)


def test_record_verdict_rejects_unknown_verdict_value(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)

    with pytest.raises(ValueError, match="verdict must be one of"):
        record_verdict(store, gate_id=gate_id, verdict="MAYBE", critic_launch=launch_id)


def test_record_verdict_pass_moves_to_gated(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)

    updated = record_verdict(store, gate_id=gate_id, verdict="PASS", critic_launch=launch_id)

    assert updated["state"] == "gated"
    assert updated["verdict"] == "PASS"
    assert updated["critic_launch"] == launch_id
    assert updated["verdict_ts"] is not None
    row = store.ops.execute(
        "SELECT from_state, to_state FROM gate_transition WHERE gate_id = ? ORDER BY id DESC LIMIT 1", (gate_id,)
    ).fetchone()
    assert (row["from_state"], row["to_state"]) == ("submitted", "gated")


def test_record_verdict_fail_moves_to_failed(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)

    updated = record_verdict(store, gate_id=gate_id, verdict="FAIL", critic_launch=launch_id)

    assert updated["state"] == "failed"
    assert updated["verdict"] == "FAIL"


def test_record_verdict_normalizes_edits_with_defaults(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)

    updated = record_verdict(
        store, gate_id=gate_id, verdict="PASS_WITH_EDITS", critic_launch=launch_id,
        edits=[{"text": "fix the typo", "blocking": True}, {"text": "nit: rename var"}],
    )

    edits = json.loads(updated["edits"])
    assert len(edits) == 2
    fix = next(e for e in edits if e["text"] == "fix the typo")
    assert fix["blocking"] is True
    assert fix["applied"] is False
    assert fix["verified"] is False
    assert fix["edit_id"]  # generated
    nit = next(e for e in edits if e["text"] == "nit: rename var")
    assert nit["blocking"] is False


def test_record_verdict_by_launch_defaults_to_critic_launch(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)

    record_verdict(store, gate_id=gate_id, verdict="PASS", critic_launch=launch_id)

    row = store.ops.execute(
        "SELECT by_launch FROM gate_transition WHERE gate_id = ? ORDER BY id DESC LIMIT 1", (gate_id,)
    ).fetchone()
    assert row["by_launch"] == launch_id


def test_record_verdict_leaves_no_trace_on_failure(store):
    """Transactional atomicity: an invalid verdict value fails BEFORE any
    row is touched — replay after the failed call shows zero new
    gate_transition rows and the gate's state unchanged."""
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    before = store.ops.execute("SELECT COUNT(*) FROM gate_transition WHERE gate_id = ?", (gate_id,)).fetchone()[0]

    with pytest.raises(ValueError):
        record_verdict(store, gate_id=gate_id, verdict="NOPE", critic_launch=launch_id)

    after = store.ops.execute("SELECT COUNT(*) FROM gate_transition WHERE gate_id = ?", (gate_id,)).fetchone()[0]
    assert after == before
    assert get_gate(store, gate_id)["state"] == "submitted"


# ---- apply_union (F10 enforcement) -----------------------------------------


def test_apply_union_pass_with_zero_edits_is_a_noop_pass(store):
    """"A PASS with zero edits still passes through union_applied as a
    no-op transition, keeping one rule" (design Section 4.2)."""
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    record_verdict(store, gate_id=gate_id, verdict="PASS", critic_launch=launch_id)

    updated = apply_union(store, gate_id=gate_id, by_launch=launch_id)

    assert updated["state"] == "union_applied"


def test_apply_union_refuses_when_verdict_is_not_a_pass_value(store):
    """apply-union is only reachable (state-graph-legally) from 'gated',
    and 'gated' is reached by ANY verdict including FAIL only via the raw
    graph... except record_verdict routes FAIL straight to 'failed', which
    has no outgoing edge — so this exercises the entry-condition check via
    a gate advanced to 'gated' generically (bypassing record_verdict) with
    no verdict ever set, proving the union check does not just trust the
    state name."""
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    advance_gate(store, gate_id=gate_id, to_state="gated", by_launch=launch_id)  # no verdict set

    with pytest.raises(GateEntryConditionError, match="verdict must be PASS"):
        apply_union(store, gate_id=gate_id, by_launch=launch_id)
    assert get_gate(store, gate_id)["state"] == "gated"  # unchanged


def test_apply_union_refuses_unverified_blocking_edit(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    record_verdict(
        store, gate_id=gate_id, verdict="PASS_WITH_EDITS", critic_launch=launch_id,
        edits=[{"text": "must fix this", "blocking": True}],
    )

    with pytest.raises(GateEntryConditionError, match="blocking edit"):
        apply_union(store, gate_id=gate_id, by_launch=launch_id)
    assert get_gate(store, gate_id)["state"] == "gated"


def test_apply_union_succeeds_once_blocking_edit_is_verified(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    verdict = record_verdict(
        store, gate_id=gate_id, verdict="PASS_WITH_EDITS", critic_launch=launch_id,
        edits=[{"text": "must fix this", "blocking": True}],
    )
    edit_id = json.loads(verdict["edits"])[0]["edit_id"]

    verify_edit(store, gate_id=gate_id, edit_id=edit_id, by_launch=launch_id, verified_note="fixed in commit x")
    updated = apply_union(store, gate_id=gate_id, by_launch=launch_id)

    assert updated["state"] == "union_applied"


def test_apply_union_refuses_reproduction_mismatch(store):
    """Reproduction acceptance via FIXTURE-PLANTED rows (design F4 / build
    brief): this module never runs a reproduction script (M9 owns that) —
    a mismatch is simulated here exactly as M9's real runner would leave
    it: a direct ``reproduction_status`` write on the gate row."""
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    record_verdict(store, gate_id=gate_id, verdict="PASS", critic_launch=launch_id)
    update(store, "gate", pk_column="gate_id", pk_value=gate_id, changes={"reproduction_status": "mismatch"})

    with pytest.raises(GateEntryConditionError, match="reproduction_status"):
        apply_union(store, gate_id=gate_id, by_launch=launch_id)
    assert get_gate(store, gate_id)["state"] == "gated"


@pytest.mark.parametrize("reproduction_status", [None, "unrun", "match"])
def test_apply_union_allows_non_mismatch_reproduction_statuses(store, reproduction_status):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    record_verdict(
        store, gate_id=gate_id, verdict="PASS", critic_launch=launch_id, reproduction_status=reproduction_status,
    )

    updated = apply_union(store, gate_id=gate_id, by_launch=launch_id)

    assert updated["state"] == "union_applied"


def test_apply_union_from_failed_is_illegal_not_a_gate_entry_condition_error(store):
    """union_applied can only be entered from 'gated' — attempting it from
    a terminal 'failed' gate is an ILLEGAL TRANSITION (wrong error type),
    not an entry-condition failure."""
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    record_verdict(store, gate_id=gate_id, verdict="FAIL", critic_launch=launch_id)

    with pytest.raises(IllegalTransitionError):
        apply_union(store, gate_id=gate_id, by_launch=launch_id)


# ---- verify_edit (not a state transition) ----------------------------------


def test_verify_edit_marks_applied_and_verified(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    verdict = record_verdict(
        store, gate_id=gate_id, verdict="PASS_WITH_EDITS", critic_launch=launch_id,
        edits=[{"text": "fix it", "blocking": True}],
    )
    edit_id = json.loads(verdict["edits"])[0]["edit_id"]

    updated = verify_edit(store, gate_id=gate_id, edit_id=edit_id, by_launch=launch_id, verified_note="done")

    edit = json.loads(updated["edits"])[0]
    assert edit["applied"] is True
    assert edit["applied_by_launch"] == launch_id
    assert edit["verified"] is True
    assert edit["verified_note"] == "done"
    # not a state transition: gate.state and gate_transition count unchanged
    assert updated["state"] == "gated"


def test_verify_edit_does_not_write_a_gate_transition_row(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    verdict = record_verdict(
        store, gate_id=gate_id, verdict="PASS_WITH_EDITS", critic_launch=launch_id,
        edits=[{"text": "fix it", "blocking": True}],
    )
    edit_id = json.loads(verdict["edits"])[0]["edit_id"]
    before = store.ops.execute("SELECT COUNT(*) FROM gate_transition WHERE gate_id = ?", (gate_id,)).fetchone()[0]

    verify_edit(store, gate_id=gate_id, edit_id=edit_id, by_launch=launch_id)

    after = store.ops.execute("SELECT COUNT(*) FROM gate_transition WHERE gate_id = ?", (gate_id,)).fetchone()[0]
    assert after == before


def test_verify_edit_refuses_outside_gated_state(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate = open_gate(store, artifact_id=artifact_id)  # draft, no edits at all

    with pytest.raises(ValueError, match="must be 'gated'"):
        verify_edit(store, gate_id=gate["gate_id"], edit_id="EDIT-x", by_launch=launch_id)


def test_verify_edit_refuses_unknown_edit_id(store):
    launch_id, _type, artifact_id = _seed_artifact(store, gated=True)
    gate_id = _to_submitted(store, launch_id, artifact_id)
    record_verdict(store, gate_id=gate_id, verdict="PASS_WITH_EDITS", critic_launch=launch_id, edits=[{"text": "x"}])

    with pytest.raises(ValueError, match="no edit"):
        verify_edit(store, gate_id=gate_id, edit_id="EDIT-nonexistent", by_launch=launch_id)
