"""Tests for ``trialerror.sessions.checks`` — the M6 doctor checks."""

from __future__ import annotations

from trialerror.budget.pools import book_launch, reconcile_launch
from trialerror.events.api import append_event
from trialerror.sessions.checks import check_session_hook_alive, check_session_multiple_open, check_spawns_vs_bookings
from trialerror.stores import insert
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests.test_session_helpers import add_hook_alive, seed_launch, seed_open_session


def _ctx(program_root) -> DoctorContext:
    return DoctorContext(program_root=program_root)


def test_check_session_multiple_open_skips_missing_db(tmp_path):
    result = check_session_multiple_open(_ctx(tmp_path / "does-not-exist"))
    assert result.status == "skip"


def test_check_session_multiple_open_pass_with_zero_or_one(store):
    result = check_session_multiple_open(_ctx(store.program_root))
    assert result.status == "pass"

    seed_open_session(store)
    result = check_session_multiple_open(_ctx(store.program_root))
    assert result.status == "pass"


def test_check_session_multiple_open_fails_with_two(store):
    account_id, _ = seed_open_session(store)
    # Insert a SECOND open session directly (bypassing boot_session's own
    # single-open-session guard) -- the exact adversarial fixture this
    # check exists to catch.
    insert(
        store,
        "session",
        {"session_id": new_id("SESS"), "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    result = check_session_multiple_open(_ctx(store.program_root))
    assert result.status == "fail"
    assert len(result.details["open_session_ids"]) == 2


def test_check_session_hook_alive_skips_missing_db(tmp_path):
    result = check_session_hook_alive(_ctx(tmp_path / "does-not-exist"))
    assert result.status == "skip"


def test_check_session_hook_alive_warns_when_zero_events(store):
    seed_open_session(store)
    result = check_session_hook_alive(_ctx(store.program_root))
    assert result.status == "warn"
    assert len(result.details["offenders"]) == 1


def test_check_session_hook_alive_passes_when_recorded(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    result = check_session_hook_alive(_ctx(store.program_root))
    assert result.status == "pass"


def test_check_session_hook_alive_pass_with_no_open_sessions(store):
    result = check_session_hook_alive(_ctx(store.program_root))
    assert result.status == "pass"
    assert result.details["offenders"] == []


# ---------------------------------------------------------------------------
# spawns_vs_bookings (FX-8, C-0064 lens B EP-1 Bypass C)
# ---------------------------------------------------------------------------


def test_check_spawns_vs_bookings_skips_missing_ops_db(tmp_path):
    result = check_spawns_vs_bookings(_ctx(tmp_path / "does-not-exist"))
    assert result.status == "skip"


def test_check_spawns_vs_bookings_passes_with_nothing_recorded(store):
    seed_open_session(store)
    result = check_spawns_vs_bookings(_ctx(store.program_root))
    assert result.status == "pass"


def test_check_spawns_vs_bookings_passes_when_counts_reconcile(store):
    account_id, session_id = seed_open_session(store)
    add_hook_alive(store, session_id, hook="spawn_gate")
    booked = book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=10,
    )
    # consume the booking for real (PROVISIONAL -> RUNNING) the same way
    # the spawn gate does, then a matching subagent_return.
    from trialerror.budget.gate import evaluate_spawn_for_open_session

    evaluate_spawn_for_open_session(store, f"launch_id: {booked.launch_id}")
    append_event(
        store, event_type="subagent_return", session_id=session_id, launch_id=booked.launch_id,
        payload={"response_size_bytes": 10, "duration_ms": None},
    )

    result = check_spawns_vs_bookings(_ctx(store.program_root))
    assert result.status == "pass"
    assert result.details["mismatched_sessions"] == []
    assert result.details["bad_launch_id_events"] == []


def test_check_spawns_vs_bookings_warns_on_count_mismatch(store):
    """A consumed (RUNNING) launch with zero matching subagent_return
    events -- the spawn ran but PostToolUse:Task never fired (or vice
    versa)."""
    account_id, session_id = seed_open_session(store)
    seed_launch(store, account_id=account_id, session_id=session_id, state="RUNNING")

    result = check_spawns_vs_bookings(_ctx(store.program_root))
    assert result.status == "warn"
    assert len(result.details["mismatched_sessions"]) == 1
    assert result.details["mismatched_sessions"][0]["session_id"] == session_id
    assert result.details["mismatched_sessions"][0]["subagent_return_count"] == 0
    assert result.details["mismatched_sessions"][0]["consumed_launch_count"] == 1


def test_check_spawns_vs_bookings_warns_on_null_launch_id(store):
    _, session_id = seed_open_session(store)
    append_event(
        store, event_type="subagent_return", session_id=session_id, launch_id=None,
        payload={"response_size_bytes": 10, "duration_ms": None},
    )

    result = check_spawns_vs_bookings(_ctx(store.program_root))
    assert result.status == "warn"
    offenders = result.details["bad_launch_id_events"]
    assert len(offenders) == 1
    assert offenders[0]["reason"] == "null_launch_id"


def test_check_spawns_vs_bookings_warns_on_unknown_launch_id(store):
    """``event.launch_id`` is XID-validated by ``trialerror.stores.insert``
    (``trialerror/stores/xid.py``'s ``XID_REGISTRY``), so a dangling launch_id
    can never land through the normal API -- this scenario is only
    reachable via a raw write (a migration import, or real DB corruption),
    which is exactly why the doctor check defends against it. Inserted
    directly, bypassing the validated writer, to construct that state."""
    _, session_id = seed_open_session(store)
    with store.ops:
        store.ops.execute(
            "INSERT INTO event (event_id, ts, session_id, launch_id, workpackage, type, payload, redactions) "
            "VALUES (?, ?, ?, ?, NULL, 'subagent_return', ?, 0)",
            (new_id("EVT"), now(), session_id, "LNCH-does-not-exist", '{"response_size_bytes": 10, "duration_ms": null}'),
        )

    result = check_spawns_vs_bookings(_ctx(store.program_root))
    assert result.status == "warn"
    offenders = result.details["bad_launch_id_events"]
    assert len(offenders) == 1
    assert offenders[0]["reason"] == "unknown_launch_id"
    assert offenders[0]["launch_id"] == "LNCH-does-not-exist"


def test_check_spawns_vs_bookings_ignores_old_closed_sessions(store):
    """A session closed well outside the recent window is not scanned --
    matches ``budget_dangling_launches``'s own TTL-scoped (not
    full-history) doctor-check convention."""
    account_id, session_id = seed_open_session(store)
    from trialerror.stores import update

    update(
        store, "session", pk_column="session_id", pk_value=session_id,
        changes={"status": "closed", "closed_ts": "2000-01-01T00:00:00.000Z"},
    )
    seed_launch(store, account_id=account_id, session_id=session_id, state="RUNNING")

    result = check_spawns_vs_bookings(_ctx(store.program_root))
    assert result.status == "pass"
