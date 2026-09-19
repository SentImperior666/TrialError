"""``trialerror.rooms.checks`` — the rooms-subsystem doctor checks. Mirrors
``tests/test_artifacts_checks.py``'s style: auto-discovery + planted-
fixture adversarial cases for each check.
"""

from __future__ import annotations

from trialerror.rooms.api import converge_room, create_room, freeze_room, register_room_deliverable, score_dp
from trialerror.stores import insert, update
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks

from tests._rooms_fixtures import bootstrap_launch, seed_template


def _run(names, program_root, platform_root=None):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root, platform_root=platform_root)
    results = run_checks(ctx, only=names)
    return {r.name: r for r in results}


def test_checks_are_auto_discovered_without_import():
    clear_registry()
    discover_and_register_checks()
    from trialerror.util.doctor import registered_checks

    names = set(registered_checks())
    assert {"rooms_stuck", "rooms_unregistered_deliverables"} <= names


def test_both_checks_skip_when_ops_db_absent(tmp_path, platform_root):
    empty_program = tmp_path / "never_initialized"
    results = _run(["rooms_stuck", "rooms_unregistered_deliverables"], empty_program, platform_root)
    assert results["rooms_stuck"].status == "skip"
    assert results["rooms_unregistered_deliverables"].status == "skip"


def test_rooms_stuck_passes_with_no_open_rooms(store, program_root, platform_root):
    results = _run(["rooms_stuck"], program_root, platform_root)
    assert results["rooms_stuck"].status == "pass"


def test_rooms_stuck_passes_for_a_freshly_created_room(store, program_root, platform_root):
    create_room(store, topic="t", discussion_points=[{"prompt": "p"}], participants=["P1", "P2"])
    results = _run(["rooms_stuck"], program_root, platform_root)
    assert results["rooms_stuck"].status == "pass"


def test_rooms_stuck_warns_on_stale_activity(store, program_root, platform_root):
    room = create_room(store, topic="t", discussion_points=[{"prompt": "p"}], participants=["P1", "P2"])
    # Backdate the room's only activity event well past the staleness
    # window, via the validated writer (never raw SQL) -- same convention
    # every other adversarial-fixture test in this suite uses.
    event_row = store.ops.execute("SELECT event_id FROM event WHERE type = 'room_created'").fetchone()
    update(store, "event", pk_column="event_id", pk_value=event_row["event_id"], changes={"ts": "2000-01-01T00:00:00.000Z"})
    results = _run(["rooms_stuck"], program_root, platform_root)
    assert results["rooms_stuck"].status == "warn"
    offenders = results["rooms_stuck"].details["rooms"]
    assert offenders and offenders[0]["room_id"] == room["room_id"]


def test_rooms_stuck_flags_a_room_with_no_activity_event_at_all(store, program_root, platform_root):
    # Direct write bypassing trialerror.rooms.api.create_room -- no companion
    # 'room_created' event lands, which is exactly the adversarial case
    # this check treats as maximally stale rather than silently skipped.
    insert(store, "room", {"room_id": "ROOM-direct", "topic": "t", "dps": "[]", "state": "open"})
    results = _run(["rooms_stuck"], program_root, platform_root)
    assert results["rooms_stuck"].status == "warn"
    offenders = results["rooms_stuck"].details["rooms"]
    assert any(r["room_id"] == "ROOM-direct" and r["last_activity_ts"] is None for r in offenders)


def test_rooms_unregistered_deliverables_passes_with_no_converged_rooms(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    room = create_room(store, topic="t", discussion_points=[{"prompt": "p"}], participants=["P1", "P2"])
    freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="r")
    results = _run(["rooms_unregistered_deliverables"], program_root, platform_root)
    assert results["rooms_unregistered_deliverables"].status == "pass"


def test_rooms_unregistered_deliverables_fails_for_a_converged_room_missing_its_artifact(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    room = create_room(store, topic="t", discussion_points=[{"prompt": "p"}], participants=["P1", "P2"])
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    converge_room(store, room_id=room["room_id"], by_launch=launch_id)

    results = _run(["rooms_unregistered_deliverables"], program_root, platform_root)
    assert results["rooms_unregistered_deliverables"].status == "fail"
    offenders = results["rooms_unregistered_deliverables"].details["offenders"]
    assert offenders == [{"room_id": room["room_id"], "topic": "t"}]

    seed_template(store)
    register_room_deliverable(
        store, room_id=room["room_id"], type_key="room_theory_doc", title="t", path="artifacts/t.md",
        sha256="0" * 64, by_launch=launch_id,
    )
    results_after = _run(["rooms_unregistered_deliverables"], program_root, platform_root)
    assert results_after["rooms_unregistered_deliverables"].status == "pass"
