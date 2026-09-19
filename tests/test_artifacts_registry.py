"""``trialerror.artifacts.registry`` — the typed-artifact registry, and the
one-transaction register-then-close-gate proof. Design Section 12 (M10
row): "register-before-gate refused for gated template types" + "register-
then-close-gate ordering in ONE transaction" (build brief)."""

from __future__ import annotations

import json

import pytest

import trialerror.artifacts.registry as registry_module
from trialerror.artifacts.errors import RegistrationRefusedError
from trialerror.artifacts.gates import apply_union, open_gate, record_verdict, submit_gate
from trialerror.artifacts.registry import create_artifact, get_artifact, list_artifacts, register_artifact
from trialerror.stores import insert
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


def _to_union_applied(store, launch_id: str, artifact_id: str) -> str:
    """Drive a fresh artifact's gate all the way to union_applied (PASS,
    zero edits) and return the gate_id."""
    gate = open_gate(store, artifact_id=artifact_id)
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", critic_launch=launch_id)
    apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
    return gate["gate_id"]


# ---- create_artifact --------------------------------------------------------


def test_create_artifact_lands_in_draft_status(store):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)

    artifact = create_artifact(
        store, type_key="keystone", title="My Artifact", path="artifacts/a.md", sha256="a" * 64,
        by_launch=launch_id, purpose="testing", domains=["ingest", "verify"], attrs={"k": "v"},
    )

    assert artifact["status"] == "draft"
    assert artifact["gate_id"] is None
    assert artifact["registered_ts"] is None
    assert artifact["registered_by_launch"] == launch_id
    assert json.loads(artifact["domains"]) == ["ingest", "verify"]
    assert json.loads(artifact["attrs"]) == {"k": "v"}


def test_create_artifact_refuses_unknown_type(store):
    _account, _session, launch_id = _seed_launch(store)
    with pytest.raises(ValidationError):
        create_artifact(store, type_key="no-such-type", title="t", path="p", sha256="0" * 64, by_launch=launch_id)


def test_create_artifact_refuses_unknown_launch(store):
    _add_template(store, "keystone", gated=True)
    with pytest.raises(XidTargetMissingError):
        create_artifact(store, type_key="keystone", title="t", path="p", sha256="0" * 64, by_launch="LNCH-nope")


def test_list_artifacts_filters_by_type_and_status(store):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    _add_template(store, "note", gated=False)
    create_artifact(store, type_key="keystone", title="a", path="p1", sha256="1" * 64, by_launch=launch_id)
    create_artifact(store, type_key="note", title="b", path="p2", sha256="2" * 64, by_launch=launch_id)

    keystones = list_artifacts(store, type_key="keystone")
    assert len(keystones) == 1
    assert keystones[0]["title"] == "a"

    drafts = list_artifacts(store, status="draft")
    assert len(drafts) == 2


# ---- register_artifact: ungated types --------------------------------------


def test_register_ungated_artifact_needs_no_gate(store):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "note", gated=False)
    artifact = create_artifact(store, type_key="note", title="n", path="p", sha256="0" * 64, by_launch=launch_id)

    registered = register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    assert registered["status"] == "registered"
    assert registered["registered_ts"] is not None
    assert registered["gate_id"] is None


# ---- register_artifact: gated types (F10 registration ordering) -----------


def test_register_gated_artifact_refused_with_no_gate_at_all(store):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)

    with pytest.raises(RegistrationRefusedError, match="no gate has been opened"):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)
    assert get_artifact(store, artifact["artifact_id"])["status"] == "draft"


@pytest.mark.parametrize("stop_at", ["draft", "submitted", "gated"])
def test_register_gated_artifact_refused_before_union_applied(store, stop_at):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    if stop_at in ("submitted", "gated"):
        submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    if stop_at == "gated":
        record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", critic_launch=launch_id)

    with pytest.raises(RegistrationRefusedError, match="union_applied"):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)
    assert get_artifact(store, artifact["artifact_id"])["status"] == "in_gate"


def test_register_gated_artifact_succeeds_at_union_applied_and_closes_gate_same_transaction(store):
    """THE ordering proof: on success, the gate's own state row shows
    'registered' AND a fresh union_applied -> registered gate_transition
    row exists AND the artifact's status flipped — all readable
    immediately after one ``register_artifact`` call with no intermediate
    state ever externally observable (SQLite's write lock serializes any
    concurrent reader out until COMMIT)."""
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate_id = _to_union_applied(store, launch_id, artifact["artifact_id"])

    registered = register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    assert registered["status"] == "registered"
    assert registered["registered_ts"] is not None

    gate_row = store.ops.execute("SELECT state FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
    assert gate_row["state"] == "registered"

    transition = store.ops.execute(
        "SELECT from_state, to_state, by_launch FROM gate_transition WHERE gate_id = ? ORDER BY id DESC LIMIT 1",
        (gate_id,),
    ).fetchone()
    assert (transition["from_state"], transition["to_state"]) == ("union_applied", "registered")
    assert transition["by_launch"] == launch_id


def test_register_refuses_when_gate_state_changed_after_the_precheck_toctou(store, monkeypatch):
    """OB-2 (C-0064 fix-tier3): register_artifact's PRE-transaction check
    reads the gate via ``store_get`` BEFORE ``BEGIN IMMEDIATE`` -- it can
    only prove "union_applied a moment ago", not "union_applied right
    now". The real race (two concurrent ``register_artifact`` calls for
    the SAME gate) is not practically reproducible deterministically in a
    unit test, so it's simulated directly: the pre-check is made to see a
    STALE ``union_applied`` snapshot (monkeypatched) while the REAL row has
    already moved past it (a raw write, standing in for a concurrent
    writer's commit) -- proving the in-transaction re-fetch, not the stale
    snapshot, is what actually governs the write, the same pattern
    ``trialerror.artifacts.gates.advance_gate`` uses at its own
    ``fresh = conn.execute(...)`` re-fetch."""
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate_id = _to_union_applied(store, launch_id, artifact["artifact_id"])

    stale_gate_snapshot = dict(store.ops.execute("SELECT * FROM gate WHERE gate_id = ?", (gate_id,)).fetchone())
    assert stale_gate_snapshot["state"] == "union_applied"

    # Stand-in for a concurrent writer's commit landing in the window
    # between the pre-check and this call's own BEGIN IMMEDIATE.
    with store.ops:
        store.ops.execute("UPDATE gate SET state = 'failed' WHERE gate_id = ?", (gate_id,))

    real_store_get = registry_module.store_get

    def _stale_get(s, table, *, pk_column, pk_value):
        if table == "gate" and pk_value == gate_id:
            return stale_gate_snapshot
        return real_store_get(s, table, pk_column=pk_column, pk_value=pk_value)

    monkeypatch.setattr(registry_module, "store_get", _stale_get)

    with pytest.raises(RegistrationRefusedError, match="union_applied"):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    # Refused before any write landed -- artifact still un-registered, and
    # the gate is still 'failed' (not clobbered back to 'registered').
    assert get_artifact(store, artifact["artifact_id"])["status"] == "in_gate"
    gate_row = store.ops.execute("SELECT state FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
    assert gate_row["state"] == "failed"


def test_register_refuses_already_registered_artifact(store):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "note", gated=False)
    artifact = create_artifact(store, type_key="note", title="n", path="p", sha256="0" * 64, by_launch=launch_id)
    register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)

    with pytest.raises(ValueError, match="already 'registered'"):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)


def test_register_refuses_unknown_artifact(store):
    _account, _session, launch_id = _seed_launch(store)
    with pytest.raises(ValueError, match="no such artifact"):
        register_artifact(store, artifact_id="ART-nope", by_launch=launch_id)


def test_register_refuses_unknown_launch(store):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "note", gated=False)
    artifact = create_artifact(store, type_key="note", title="n", path="p", sha256="0" * 64, by_launch=launch_id)

    with pytest.raises(XidTargetMissingError):
        register_artifact(store, artifact_id=artifact["artifact_id"], by_launch="LNCH-nope")


def test_register_with_supersedes_flips_prior_artifact(store):
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "note", gated=False)
    old = create_artifact(store, type_key="note", title="v1", path="p1", sha256="1" * 64, by_launch=launch_id)
    register_artifact(store, artifact_id=old["artifact_id"], by_launch=launch_id)
    new = create_artifact(store, type_key="note", title="v2", path="p2", sha256="2" * 64, by_launch=launch_id)

    register_artifact(store, artifact_id=new["artifact_id"], by_launch=launch_id, supersedes=old["artifact_id"])

    assert get_artifact(store, old["artifact_id"])["status"] == "superseded"
    updated_new = get_artifact(store, new["artifact_id"])
    assert updated_new["status"] == "registered"
    assert updated_new["supersedes"] == old["artifact_id"]


def test_register_bad_supersedes_target_rolls_back_the_whole_transaction(store):
    """One-transaction proof, negative case: an invalid ``supersedes``
    target must leave EVERYTHING untouched — not just the artifact's own
    row, but the gate it was about to close too."""
    _account, _session, launch_id = _seed_launch(store)
    _add_template(store, "keystone", gated=True)
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate_id = _to_union_applied(store, launch_id, artifact["artifact_id"])

    with pytest.raises(ValidationError, match="supersedes"):
        register_artifact(
            store, artifact_id=artifact["artifact_id"], by_launch=launch_id, supersedes="ART-does-not-exist",
        )

    # Nothing landed: artifact still un-registered, gate still union_applied.
    assert get_artifact(store, artifact["artifact_id"])["status"] == "in_gate"
    gate_row = store.ops.execute("SELECT state FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
    assert gate_row["state"] == "union_applied"
    n_transitions = store.ops.execute(
        "SELECT COUNT(*) FROM gate_transition WHERE gate_id = ? AND to_state = 'registered'", (gate_id,)
    ).fetchone()[0]
    assert n_transitions == 0
