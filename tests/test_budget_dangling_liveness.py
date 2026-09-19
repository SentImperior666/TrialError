"""Lane FB-1 item F2: what the dangling-launch check knows, and what it does not.

Three surfaces, one module of truth (:mod:`trialerror.budget.dangling`):
the ``budget_dangling_launches`` doctor check, the dashboard's budget card,
and ``budget heartbeat`` -- the verb that lets a live launch clear the first
reading honestly instead of reconciling early.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.budget import dangling
from trialerror.budget.checks import check_budget_dangling_launches
from trialerror.budget.errors import BudgetError, LaunchNotOwnedError, NoOpenSessionError
from trialerror.budget.pools import book_launch, heartbeat_launch, reconcile_launch
from trialerror.dashboard.data import PANEL_BUILDERS
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.events.api import record_hook_alive_once
from trialerror.stores import insert
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._budget_fixtures import open_account_session

_PAST = "2020-01-01T00:00:00.000Z"


def _book_past_ttl(store, session_id: str, *, purpose: str = "mechanical") -> str:
    return book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose=purpose,
        est_tokens=10,
        booking_ttl_s=1,
        now_ts=_PAST,
    ).launch_id


def _closed_session(store, account_id: str) -> str:
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {
            "session_id": session_id,
            "account_id": account_id,
            "opened_ts": now(),
            "closed_ts": now(),
            "status": "closed",
        },
    )
    return session_id


def _orphan_row(store, account_id: str, session_id: str) -> str:
    """A past-TTL booking under a session that is NOT open -- a state the
    booking API refuses to create (and close refuses to leave behind), so
    the row is written directly."""
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id,
            "account_id": account_id,
            "program_id": "PROG-test",
            "session_id": session_id,
            "agent_kind": "lens",
            "model_class": "mid",
            "model": "sonnet",
            "purpose": "orphaned",
            "est_tokens": 10,
            "booked_ts": _PAST,
            "booking_ttl_s": 1,
            "state": "PROVISIONAL",
        },
    )
    return launch_id


# ---------------------------------------------------------------------------
# the message, and what it refuses to claim
# ---------------------------------------------------------------------------


def test_the_message_no_longer_asserts_a_crashed_session(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    _book_past_ttl(store, session_id)
    store.close()

    result = check_budget_dangling_launches(DoctorContext(platform_root=platform_root))
    assert result.status == "warn"
    assert "this check cannot tell which" in result.message
    assert "crashed session" not in result.message
    assert "the TTL was too short" in result.message


def test_a_clean_program_still_passes(store, platform_root):
    account_id, session_id = open_account_session(store)
    book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=10, booking_ttl_s=3600,
    )
    store.close()
    result = check_budget_dangling_launches(DoctorContext(platform_root=platform_root))
    assert result.status == "pass"
    assert result.details["offenders"] == []


# ---------------------------------------------------------------------------
# the liveness split
# ---------------------------------------------------------------------------


def test_split_degrades_to_one_list_without_a_program_root(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    record_hook_alive_once(store, session_id=session_id, hook_name="session_start")
    launch_id = _book_past_ttl(store, session_id)
    store.close()

    # No program_root -> no ops.db -> the split cannot be evaluated at all.
    result = check_budget_dangling_launches(DoctorContext(platform_root=platform_root))
    assert [o["launch_id"] for o in result.details["offenders"]] == [launch_id]
    assert result.details["past_ttl_session_alive"] is None
    assert "unavailable" in result.details["liveness_evidence"]
    assert "this check cannot tell which" in result.message


def test_the_evidence_line_names_the_absence_that_actually_fired(store, tmp_path, platform_root):
    """fix-accept V-4. ``None`` from the liveness read has three causes -- no
    program root, no ops.db, an unreadable ops.db -- and the detail string
    used to name only the first, so a run that DID pass a program root was
    told something false about its own invocation."""
    account_id, session_id = open_account_session(store)
    _book_past_ttl(store, session_id)
    store.close()

    no_root = check_budget_dangling_launches(DoctorContext(platform_root=platform_root))
    assert "no program root" in no_root.details["liveness_evidence"]

    bare = tmp_path / "program-with-no-stores"
    bare.mkdir()
    no_ops = check_budget_dangling_launches(
        DoctorContext(program_root=bare, platform_root=platform_root)
    )
    assert no_ops.details["past_ttl_session_alive"] is None
    evidence = no_ops.details["liveness_evidence"]
    assert evidence.startswith("unavailable: ")
    assert "no ops.db yet" in evidence
    assert "no program root" not in evidence


def test_a_launch_booked_in_another_program_is_named_as_such(store, tmp_path, platform_root):
    """fix-accept V-5. Bookings are shared across programs (platform.db),
    sessions and their hook_alive events are not (ops.db), so the split would
    otherwise report another program's demonstrably live launch as an offender
    with ``past_ttl_session_alive == []`` -- which under this module's own
    distinction reads "checked, none alive" rather than "could not tell"."""
    from trialerror.stores.store import open_store

    account_id, session_id = open_account_session(store)
    record_hook_alive_once(store, session_id=session_id, hook_name="session_start")
    launch_id = _book_past_ttl(store, session_id)
    store.close()

    other = tmp_path / "other-program"
    other.mkdir()
    other_store = open_store(other, platform_root=platform_root)
    other_store.close()

    here = check_budget_dangling_launches(
        DoctorContext(program_root=store.program_root, platform_root=platform_root)
    )
    assert [r["launch_id"] for r in here.details["past_ttl_session_alive"]] == [launch_id]
    assert "past_ttl_foreign_session" not in here.details
    assert here.details["liveness_evidence"].startswith("read: ")

    elsewhere = check_budget_dangling_launches(
        DoctorContext(program_root=other, platform_root=platform_root)
    )
    assert [r["launch_id"] for r in elsewhere.details["offenders"]] == [launch_id]
    assert elsewhere.details["past_ttl_foreign_session"] == [launch_id]
    evidence = elsewhere.details["liveness_evidence"]
    assert evidence.startswith("partial: ")
    assert "does not know" in evidence


def test_a_live_session_moves_its_booking_out_of_the_offender_list(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    record_hook_alive_once(store, session_id=session_id, hook_name="session_start")
    launch_id = _book_past_ttl(store, session_id)
    store.close()

    result = check_budget_dangling_launches(
        DoctorContext(program_root=program_root, platform_root=platform_root)
    )
    assert result.status == "warn"  # still visible -- a short TTL is worth knowing
    assert result.details["offenders"] == []
    assert [o["launch_id"] for o in result.details["past_ttl_session_alive"]] == [launch_id]
    assert "still open and recording hook liveness" in result.message


def test_an_open_session_with_no_hook_events_is_not_evidence_of_life(store, program_root, platform_root):
    """A session can be OPEN with hooks never armed -- the state
    ``session_hook_alive`` warns about. It proves nothing about liveness, so
    its bookings stay in the offender list."""
    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    store.close()

    result = check_budget_dangling_launches(
        DoctorContext(program_root=program_root, platform_root=platform_root)
    )
    assert [o["launch_id"] for o in result.details["offenders"]] == [launch_id]
    assert result.details["past_ttl_session_alive"] == []


def test_a_closed_session_is_never_alive_however_many_hooks_it_fired(store, program_root, platform_root):
    account_id, open_session = open_account_session(store)
    closed = _closed_session(store, account_id)
    record_hook_alive_once(store, session_id=closed, hook_name="session_start")
    record_hook_alive_once(store, session_id=open_session, hook_name="session_start")
    orphan = _orphan_row(store, account_id, closed)
    live = _book_past_ttl(store, open_session)
    store.close()

    result = check_budget_dangling_launches(
        DoctorContext(program_root=program_root, platform_root=platform_root)
    )
    assert [o["launch_id"] for o in result.details["offenders"]] == [orphan]
    assert [o["launch_id"] for o in result.details["past_ttl_session_alive"]] == [live]
    assert "no evidence of life" in result.message
    assert "1 more past TTL" in result.message


# ---------------------------------------------------------------------------
# the dashboard mirror
# ---------------------------------------------------------------------------


def test_the_card_and_the_doctor_agree_on_the_same_fixture(store, program_root, platform_root):
    account_id, open_session = open_account_session(store)
    closed = _closed_session(store, account_id)
    record_hook_alive_once(store, session_id=open_session, hook_name="stop_check")
    orphan = _orphan_row(store, account_id, closed)
    live = _book_past_ttl(store, open_session)
    store.close()

    check = check_budget_dangling_launches(
        DoctorContext(program_root=program_root, platform_root=platform_root)
    )
    with open_store_ro(program_root, platform_root=platform_root) as rostore:
        panel = PANEL_BUILDERS["budget"](rostore)

    assert [o["launch_id"] for o in panel["dangling_bookings"]] == [orphan]
    assert [o["launch_id"] for o in panel["past_ttl_session_alive"]] == [live]
    assert [o["launch_id"] for o in check.details["offenders"]] == [
        o["launch_id"] for o in panel["dangling_bookings"]
    ]
    assert [o["launch_id"] for o in check.details["past_ttl_session_alive"]] == [
        o["launch_id"] for o in panel["past_ttl_session_alive"]
    ]
    assert panel["past_ttl_message"] == check.message


def test_the_card_and_the_doctor_agree_on_the_SEVERITY_WORD_too(store, program_root, platform_root):
    """FB-1 V-11, closed (FB-1b item 5). The fixture that raised it: ONE
    booking past its TTL whose session is demonstrably alive, so the doctor
    reports `warn` while the card's DANGLING chip -- which counts the doctor's
    own OFFENDER list -- truthfully reads 0. The two words were computed in two
    places, which is what let a reader see them as a disagreement. The card now
    carries the doctor's own word, from the same function."""
    account_id, open_session = open_account_session(store)
    record_hook_alive_once(store, session_id=open_session, hook_name="stop_check")
    live = _book_past_ttl(store, open_session)
    store.close()

    check = check_budget_dangling_launches(
        DoctorContext(program_root=program_root, platform_root=platform_root)
    )
    with open_store_ro(program_root, platform_root=platform_root) as rostore:
        panel = PANEL_BUILDERS["budget"](rostore)

    # the V-11 shape itself: doctor warn, zero offenders, one alive row
    assert check.status == "warn"
    assert panel["dangling_bookings"] == []
    assert [o["launch_id"] for o in panel["past_ttl_session_alive"]] == [live]
    # ...and the word the card prints is the doctor's, not one derived from
    # the length of the list beside it
    assert panel["past_ttl_status"] == check.status == "warn"
    assert panel["past_ttl_message"] == check.message


def test_the_severity_word_is_pass_only_when_nothing_is_past_its_ttl(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=10, booking_ttl_s=3600,
    )
    store.close()
    check = check_budget_dangling_launches(
        DoctorContext(program_root=program_root, platform_root=platform_root)
    )
    with open_store_ro(program_root, platform_root=platform_root) as rostore:
        panel = PANEL_BUILDERS["budget"](rostore)
    assert panel["past_ttl_status"] == check.status == "pass"


def test_the_severity_word_does_not_depend_on_the_split_being_evaluable():
    """A program with no ops.db cannot split the rows, and must not report the
    absence of evidence as the absence of a finding."""
    rows = [{"launch_id": "LNCH-1", "session_id": "SESS-1"}]
    assert dangling.past_ttl_status(rows) == "warn"
    assert dangling.past_ttl_status([]) == "pass"
    offenders, alive = dangling.split_by_liveness(rows, None)
    assert alive is None and offenders == rows
    assert dangling.past_ttl_status(rows) == "warn"


def test_the_card_keeps_dangling_bookings_a_list_of_launch_rows(store, program_root, platform_root):
    """The ribbon and the console card both do ``(dangling_bookings || []).length``
    and render per-launch fields; the split must not turn that key into a dict."""
    account_id, session_id = open_account_session(store)
    closed = _closed_session(store, account_id)
    _orphan_row(store, account_id, closed)
    store.close()
    with open_store_ro(program_root, platform_root=platform_root) as rostore:
        panel = PANEL_BUILDERS["budget"](rostore)
    assert isinstance(panel["dangling_bookings"], list)
    row = panel["dangling_bookings"][0]
    for key in ("launch_id", "agent_kind", "purpose", "booked_ts", "booking_ttl_s", "state"):
        assert key in row


# ---------------------------------------------------------------------------
# the Stop hook as a liveness source (reconciliation follow-up (f))
# ---------------------------------------------------------------------------

_STOP_CHECK_SCRIPT = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "stop_check.py"


def _run_stop_hook(program_root: Path, platform_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(_STOP_CHECK_SCRIPT)],
        input=json.dumps({"hook_event_name": "Stop", "cwd": str(program_root)}),
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_the_stop_hook_records_liveness_for_a_session_that_spawned_nothing(
    store, program_root, platform_root
):
    account_id, session_id = open_account_session(store)
    store.close()
    (program_root / "trialerror.toml").write_text('[program]\nid = "fb1-fixture"\n', encoding="utf-8")

    proc = _run_stop_hook(program_root, platform_root)
    assert proc.returncode == 0, proc.stderr

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        rows = reopened.ops.execute(
            "SELECT payload FROM event WHERE session_id = ? AND type = 'hook_alive'", (session_id,)
        ).fetchall()
    finally:
        reopened.close()
    hooks = {json.loads(r["payload"]).get("hook") for r in rows}
    assert "stop_check" in hooks


def test_the_stop_hook_writes_its_marker_at_most_once(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    store.close()
    (program_root / "trialerror.toml").write_text('[program]\nid = "fb1-fixture"\n', encoding="utf-8")

    _run_stop_hook(program_root, platform_root)
    _run_stop_hook(program_root, platform_root)

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        rows = reopened.ops.execute(
            "SELECT payload FROM event WHERE session_id = ? AND type = 'hook_alive'", (session_id,)
        ).fetchall()
    finally:
        reopened.close()
    markers = [r for r in rows if json.loads(r["payload"]).get("hook") == "stop_check"]
    assert len(markers) == 1


def test_the_marker_is_written_even_on_a_blocking_stop(store, program_root, platform_root):
    """Liveness is recorded whatever the verdict: a session whose stop is
    blocked is precisely a session that is still alive, and the blocked turn
    must not be the one turn that leaves no trace of that."""
    account_id, session_id = open_account_session(store)
    _book_past_ttl(store, session_id)
    store.close()
    (program_root / "trialerror.toml").write_text('[program]\nid = "fb1-fixture"\n', encoding="utf-8")

    first = _run_stop_hook(program_root, platform_root)
    assert first.returncode == 2
    assert "STOP CHECKLIST" in first.stderr

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        rows = reopened.ops.execute(
            "SELECT payload FROM event WHERE session_id = ? AND type = 'hook_alive'", (session_id,)
        ).fetchall()
    finally:
        reopened.close()
    assert "stop_check" in {json.loads(r["payload"]).get("hook") for r in rows}


def test_the_marker_cannot_change_the_verdict_it_is_written_after(store, program_root, platform_root):
    """The Stop hook records its marker AFTER reading readiness. Pinned here
    because the close ladder's own ``hooks_disabled`` rung counts exactly
    these events: whatever `session close` decides, it must decide on
    evidence the Stop hook did not manufacture within the same read."""
    from trialerror.sessions.lifecycle import evaluate_close_readiness

    account_id, session_id = open_account_session(store)
    before = evaluate_close_readiness(store, session_id).problems
    record_hook_alive_once(store, session_id=session_id, hook_name="stop_check")
    after = evaluate_close_readiness(store, session_id).problems
    assert before == after == []


# ---------------------------------------------------------------------------
# budget heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_refreshes_booked_ts_and_nothing_else(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    before = store.platform.execute(
        "SELECT * FROM launch WHERE launch_id = ?", (launch_id,)
    ).fetchone()

    result = heartbeat_launch(store, launch_id=launch_id)
    after = dict(store.platform.execute("SELECT * FROM launch WHERE launch_id = ?", (launch_id,)).fetchone())

    assert result["booked_ts_before"] == _PAST
    assert after["booked_ts"] != _PAST
    for column in before.keys():
        if column == "booked_ts":
            continue
        assert after[column] == before[column], column


def test_heartbeat_clears_the_past_ttl_reading(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    heartbeat_launch(store, launch_id=launch_id)
    store.close()
    result = check_budget_dangling_launches(
        DoctorContext(program_root=program_root, platform_root=platform_root)
    )
    assert result.status == "pass"


def test_heartbeat_is_an_event_every_time(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    heartbeat_launch(store, launch_id=launch_id)
    heartbeat_launch(store, launch_id=launch_id)
    rows = store.ops.execute(
        "SELECT payload, launch_id, session_id FROM event WHERE type = 'launch_heartbeat'"
    ).fetchall()
    assert len(rows) == 2
    assert {r["launch_id"] for r in rows} == {launch_id}
    assert {r["session_id"] for r in rows} == {session_id}
    payload = json.loads(rows[0]["payload"])
    assert payload["booked_ts_before"] != payload["booked_ts_after"]


def test_heartbeat_refused_without_an_open_session(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    store.ops.execute("UPDATE session SET status = 'closed' WHERE session_id = ?", (session_id,))
    store.ops.commit()
    with pytest.raises(NoOpenSessionError):
        heartbeat_launch(store, launch_id=launch_id)


def test_heartbeat_refused_for_a_launch_the_open_session_does_not_own(store, program_root, platform_root):
    account_id, owner_session = open_account_session(store)
    launch_id = _book_past_ttl(store, owner_session)
    # the owner closes; a different session opens
    store.ops.execute("UPDATE session SET status = 'closed' WHERE session_id = ?", (owner_session,))
    store.ops.commit()
    other_session = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": other_session, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    with pytest.raises(LaunchNotOwnedError) as exc:
        heartbeat_launch(store, launch_id=launch_id)
    assert owner_session in str(exc.value) and other_session in str(exc.value)
    row = store.platform.execute("SELECT booked_ts FROM launch WHERE launch_id = ?", (launch_id,)).fetchone()
    assert row["booked_ts"] == _PAST  # refused means nothing moved


def test_heartbeat_refused_for_an_unknown_launch(store, program_root, platform_root):
    open_account_session(store)
    with pytest.raises(BudgetError):
        heartbeat_launch(store, launch_id="LNCH-nope")


def test_heartbeat_refused_for_a_settled_booking(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    reconcile_launch(store, launch_id=launch_id, actual_tokens=5)
    with pytest.raises(BudgetError) as exc:
        heartbeat_launch(store, launch_id=launch_id)
    assert "RECONCILED" in str(exc.value)


def test_heartbeat_cli_envelope(store, program_root, platform_root):
    from trialerror.cli import budget as cli_budget

    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    store.close()

    class _Args:
        def __init__(self, **kw):
            self.program_root = str(program_root)
            self.platform_root = str(platform_root)
            for k, v in kw.items():
                setattr(self, k, v)

    env = cli_budget._run_heartbeat(_Args(launch_id=launch_id))
    assert env["ok"] is True
    assert env["result"]["launch_id"] == launch_id
    assert env["result"]["booked_ts_before"] == _PAST

    env_bad = cli_budget._run_heartbeat(_Args(launch_id="LNCH-nope"))
    assert env_bad["ok"] is False
    assert env_bad["error"]["code"] == "heartbeat_refused"


def test_heartbeat_refuses_two_open_sessions_in_an_envelope(store, program_root, platform_root):
    """fix-accept V-7. ``resolve_open_session`` raises a bare RuntimeError for
    more than one OPEN session, and main() does not catch handler exceptions,
    so this verb exited with a traceback where ``budget status`` -- taught by
    this same lane -- returns a refusal. Two open sessions is exactly the state
    that produces orphan-looking bookings, i.e. the state an operator reaches
    for heartbeat in."""
    from trialerror.cli import budget as cli_budget

    account_id, session_id = open_account_session(store)
    launch_id = _book_past_ttl(store, session_id)
    second = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": second, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    booked_before = store.platform.execute(
        "SELECT booked_ts FROM launch WHERE launch_id = ?", (launch_id,)
    ).fetchone()["booked_ts"]
    store.close()

    class _Args:
        def __init__(self, **kw):
            self.program_root = str(program_root)
            self.platform_root = str(platform_root)
            for k, v in kw.items():
                setattr(self, k, v)

    env = cli_budget._run_heartbeat(_Args(launch_id=launch_id))
    assert env["ok"] is False
    assert env["error"]["code"] == "multiple_open_sessions"
    assert env["nextActions"][0]["argv"] == [
        "trialerror", "doctor", "--only", "session_multiple_open",
    ]

    # A refusal moves nothing.
    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        assert reopened.platform.execute(
            "SELECT booked_ts FROM launch WHERE launch_id = ?", (launch_id,)
        ).fetchone()["booked_ts"] == booked_before
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# the shared module itself
# ---------------------------------------------------------------------------


def test_dangling_message_is_the_one_sentence_both_surfaces_print():
    assert dangling.dangling_message([], []) == "no launches past their booking TTL"
    assert dangling.dangling_message([], None) == "no launches past their booking TTL"
    one = [{"launch_id": "LNCH-1"}]
    assert "cannot tell which" in dangling.dangling_message(one, None)
    assert "all under a session that is still open" in dangling.dangling_message([], one)


def test_the_default_columns_carry_the_key_the_split_reads(store, platform_root):
    """fix-accept V-6. A caller that took DEFAULT_COLUMNS and then split got
    every row in ``offenders`` and an empty ``alive`` list -- no error, no
    warning, i.e. the silent disagreement this shared module exists to
    prevent."""
    assert "session_id" in dangling.DEFAULT_COLUMNS
    account_id, session_id = open_account_session(store)
    record_hook_alive_once(store, session_id=session_id, hook_name="session_start")
    launch_id = _book_past_ttl(store, session_id)
    rows = dangling.past_ttl_rows(store.platform)
    offenders, alive = dangling.split_by_liveness(rows, {session_id})
    assert offenders == []
    assert [r["launch_id"] for r in alive] == [launch_id]


def test_a_row_without_a_session_id_is_refused_rather_than_counted():
    """The other half of V-6: a row that cannot be judged must not be judged.
    Counting it as "no evidence of life" on the strength of a column the
    caller did not select is the silent version of the same bug."""
    with pytest.raises(ValueError) as exc:
        dangling.split_by_liveness([{"launch_id": "L1", "state": "PROVISIONAL"}], {"SESS-1"})
    assert "session_id" in str(exc.value)


def test_split_by_liveness_never_drops_or_duplicates_a_row():
    rows = [{"launch_id": f"L{i}", "session_id": "S1" if i % 2 else "S2"} for i in range(6)]
    offenders, alive = dangling.split_by_liveness(rows, {"S1"})
    assert len(offenders) + len(alive) == len(rows)
    assert {r["launch_id"] for r in offenders} | {r["launch_id"] for r in alive} == {
        r["launch_id"] for r in rows
    }
