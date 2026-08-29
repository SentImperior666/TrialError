"""M10 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m4_acceptance.py`` convention: this file IS the
acceptance-criteria mapping, each test here re-running (not replacing) a
narrower assertion that already lives in its dedicated module.

    | Acceptance criterion (design Section 12, M10 row)                          | Test |
    |------------------------------------------------------------------------------|------|
    | illegal transition refused (property-based test over state machine)          | test_illegal_transition_refused_property_based (see test_artifacts_state_machine.py for the full 36-pair enumeration) |
    | register-before-gate refused for gated template types                        | test_register_before_gate_refused_for_gated_template_types (see test_artifacts_registry.py) |
    | blocking-edit-unverified blocks registration                                 | test_blocking_edit_unverified_blocks_registration (see test_artifacts_gates.py) |
    | reproduction mismatch blocks (fixture-planted reproduction_status rows)      | test_reproduction_mismatch_blocks_fixture_planted (see test_artifacts_gates.py) |
"""

from __future__ import annotations

import itertools
import json

import pytest

from trialerror.artifacts.errors import GateEntryConditionError, IllegalTransitionError, RegistrationRefusedError
from trialerror.artifacts.gates import apply_union, get_gate, open_gate, record_verdict, submit_gate, verify_edit
from trialerror.artifacts.registry import create_artifact, get_artifact, register_artifact
from trialerror.artifacts.state_machine import STATES, assert_legal_transition, is_legal_transition
from trialerror.stores import insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

pytestmark = pytest.mark.acceptance


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


def test_illegal_transition_refused_property_based(store):
    """Exhaustive over all 6x6 (from_state, to_state) pairs: every pair NOT
    in the legal set raises, every pair IN it does not — see
    ``test_artifacts_state_machine.py::test_exhaustive_36_pair_matrix_agrees_with_the_legal_set``
    for the full enumeration this re-confirms at the acceptance level,
    plus one live-store instance of the refusal via the actual service
    call (not just the pure graph)."""
    legal_pairs = set()
    for from_state, to_state in itertools.product(STATES, STATES):
        if is_legal_transition(from_state, to_state):
            legal_pairs.add((from_state, to_state))
        else:
            with pytest.raises(IllegalTransitionError):
                assert_legal_transition(from_state, to_state)
    assert legal_pairs  # non-vacuous: at least the real pipeline edges exist

    # Live instance: a freshly opened gate (state='draft') refuses an
    # advance straight to 'registered' through the actual service call.
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])

    from trialerror.artifacts.gates import advance_gate

    with pytest.raises(IllegalTransitionError):
        advance_gate(store, gate_id=gate["gate_id"], to_state="registered", by_launch=launch_id)
    assert get_gate(store, gate["gate_id"])["state"] == "draft"


def test_register_before_gate_refused_for_gated_template_types(store):
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)

    # No gate opened at all.
    with pytest.raises(RegistrationRefusedError):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    # A gate opened but not driven to union_applied.
    open_gate(store, artifact_id=artifact["artifact_id"])
    with pytest.raises(RegistrationRefusedError):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    assert get_artifact(store, artifact["artifact_id"])["status"] == "in_gate"

    # An UNGATED type, by contrast, registers with no gate at all.
    _add_template(store, "note", gated=False)
    ungated = create_artifact(store, type_key="note", title="n", path="p2", sha256="1" * 64, by_launch=launch_id)
    registered = register_artifact(store, artifact_id=ungated["artifact_id"], by_launch=launch_id)
    assert registered["status"] == "registered"


def test_blocking_edit_unverified_blocks_registration(store):
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    record_verdict(
        store, gate_id=gate["gate_id"], verdict="PASS_WITH_EDITS", critic_launch=launch_id,
        edits=[{"text": "must fix this before it ships", "blocking": True}],
    )

    # apply-union itself refuses (the transition-in entry check)...
    with pytest.raises(GateEntryConditionError):
        apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)

    # ...and so, transitively, does registration: the gate never reached
    # union_applied, so `register_artifact`'s own gate-state check refuses too.
    with pytest.raises(RegistrationRefusedError):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)
    assert get_artifact(store, artifact["artifact_id"])["status"] == "in_gate"

    # Verify the blocking edit, then both paths succeed.
    edit_id = json.loads(get_gate(store, gate["gate_id"])["edits"])[0]["edit_id"]
    verify_edit(store, gate_id=gate["gate_id"], edit_id=edit_id, by_launch=launch_id)
    apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
    registered = register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)
    assert registered["status"] == "registered"


def test_reproduction_mismatch_blocks_fixture_planted(store):
    """"fixture-planted reproduction_status rows — the runner ships in M9":
    this test plants the column exactly the way M9's real reproduce runner
    will (a plain ``trialerror.stores.update`` on the gate row) — no reproduce
    script is ever invoked here."""
    launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", critic_launch=launch_id)

    # Fixture-plant a mismatch (as M9's runner would write it for real).
    update(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"], changes={"reproduction_status": "mismatch"})

    with pytest.raises(GateEntryConditionError, match="reproduction_status"):
        apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
    assert get_gate(store, gate["gate_id"])["state"] == "gated"

    # Fixture-plant a 'match' instead: the same gate now proceeds.
    update(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"], changes={"reproduction_status": "match"})
    updated = apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
    assert updated["state"] == "union_applied"
