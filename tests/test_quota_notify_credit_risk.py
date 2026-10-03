"""L9 `exchange-rate-on-observed-spend.md` §3, Part E: the one-time alert
before usage credits are touched. Tests for
:func:`trialerror.quota.notify.check_credit_risk` (E1-E4) -- temporary
in-memory stores and directories only, no real host reads."""

from __future__ import annotations

import json
import sqlite3

import pytest

from trialerror.packet.store import PacketError, add_item, packet_settings
from trialerror.quota.notify import check_credit_risk, credit_risk_packet_payload
from trialerror.quota.reading import Reading, WindowReading
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn, platform.MIGRATIONS)
    return conn


def _reading(
    *, five_basis="reading", five_value=None, five_upper=None, five_resets=None,
    seven_basis="reading", seven_value=None, seven_upper=None, seven_resets=None,
) -> Reading:
    return Reading(
        five=WindowReading(basis=five_basis, value=five_value, upper=five_upper, resets=five_resets),
        seven=WindowReading(basis=seven_basis, value=seven_value, upper=seven_upper, resets=seven_resets),
    )


class _Pusher:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, body: str) -> None:
        self.calls.append((title, body))


def _notice_rows(conn, account_label, period):
    return conn.execute(
        "SELECT * FROM quota_notice WHERE account_label = ? AND kind = 'limit_hit' AND period = ? ORDER BY id",
        (account_label, period),
    ).fetchall()


# ---------------------------------------------------------------------------
# E1 / E4: a 100% row fires once per month per window
# ---------------------------------------------------------------------------


def test_a_100pct_reading_fires_the_five_hour_trigger():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == ["five_hour"]
    assert len(push.calls) == 1
    rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")
    assert len(rows) == 1 and rows[0]["sent_ts"] is not None


def test_above_100pct_also_fires():
    conn = _conn()
    push = _Pusher()
    reading = _reading(seven_value=104.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == ["seven_day"]


def test_below_100pct_never_fires():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=88.0, seven_value=95.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == [] and result["recorded"] == []
    assert push.calls == []
    assert _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09") == []


def test_a_second_event_in_the_same_month_writes_a_row_but_does_not_push_again():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0, five_resets=1_700_020_000)

    first = check_credit_risk(conn, "acct", reading, False, "2026-09-05T10:00:00Z", "2026-09", push)
    second = check_credit_risk(conn, "acct", reading, False, "2026-09-05T10:05:00Z", "2026-09", push)

    assert first["fired"] == ["five_hour"]
    assert second["fired"] == []  # no second push
    assert second["recorded"] == ["five_hour"]  # but the event was still recorded
    assert len(push.calls) == 1
    rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")
    assert len(rows) == 1  # the original, push-carrying row
    assert rows[0]["sent_ts"] is not None
    repeat_rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09:reset=1700020000")
    assert len(repeat_rows) == 1  # the one repeat row for this still-open window
    assert repeat_rows[0]["sent_ts"] is None


def test_n5_a_repeat_row_is_written_once_per_window_not_once_per_tick():
    """N5 (fix check): the SAME window (same resets) ticking many times
    while the condition holds writes at most one repeat row; a NEW window
    (a different resets, the old one having rolled over) writes another."""
    conn = _conn()
    push = _Pusher()
    reading_a = _reading(five_value=100.0, five_resets=1_700_020_000)
    reading_b = _reading(five_value=100.0, five_resets=1_700_038_000)  # the window rolled over

    check_credit_risk(conn, "acct", reading_a, False, "2026-09-05T10:00:00Z", "2026-09", push)  # fires
    check_credit_risk(conn, "acct", reading_a, False, "2026-09-05T10:05:00Z", "2026-09", push)  # 1st repeat
    check_credit_risk(conn, "acct", reading_a, False, "2026-09-05T10:10:00Z", "2026-09", push)  # same window: no new row
    check_credit_risk(conn, "acct", reading_a, False, "2026-09-05T10:15:00Z", "2026-09", push)  # still no new row
    check_credit_risk(conn, "acct", reading_b, False, "2026-09-05T15:00:00Z", "2026-09", push)  # new window: 1 more

    all_rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")
    repeats_a = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09:reset=1700020000")
    repeats_b = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09:reset=1700038000")
    assert len(all_rows) == 1  # only the original fired row uses the plain period
    assert len(repeats_a) == 1
    assert len(repeats_b) == 1
    assert len(push.calls) == 1  # never a second push, regardless of how many windows recur


def test_a_repeat_row_with_no_known_resets_adds_nothing():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0, five_resets=None)

    check_credit_risk(conn, "acct", reading, False, "2026-09-05T10:00:00Z", "2026-09", push)
    check_credit_risk(conn, "acct", reading, False, "2026-09-05T10:05:00Z", "2026-09", push)

    rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")
    assert len(rows) == 1  # no repeat row is added when the window's resets is unknown


def test_a_new_calendar_month_fires_again():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)
    october = check_credit_risk(conn, "acct", reading, False, "2026-10-01T10:00:00Z", "2026-10", push)

    assert october["fired"] == ["five_hour"]
    assert len(push.calls) == 2


# ---------------------------------------------------------------------------
# E1 (fix check S1): a LIVE window (basis "reading" or "bound") with a real
# 100% sighting fires; a real ESTIMATE (upper) reaching 100 with the real
# sighting (value) below it does not; bound_after_reset and unknown never do.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis", ["reading", "bound"])
def test_a_live_window_with_a_real_100pct_sighting_fires(basis):
    """S1: basis alone (reading vs bound) only says how fresh the LATEST
    sighting is -- a real 100% seen a few minutes ago falls to "bound" but
    is exactly the figure the alert must still catch."""
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_basis=basis, five_value=100.0, five_upper=100.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == ["five_hour"]
    assert len(push.calls) == 1


def test_a_bound_never_fires_it():
    """The real "bound" case: upper (the safe UPPER ESTIMATE, grown by the
    fastest rise ever measured) reaches 100, but the real sighting (value)
    never did. Only a real figure may fire this."""
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_basis="bound", five_value=88.0, five_upper=104.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == [] and result["recorded"] == []
    assert push.calls == []


def test_bound_after_reset_never_fires():
    """value is None after a reset (the old window's figure is gone) --
    already excluded without a separate basis check."""
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_basis="bound_after_reset", five_value=None, five_upper=104.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == [] and result["recorded"] == []
    assert push.calls == []


def test_unknown_never_fires_even_with_a_real_value():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_basis="unknown", five_value=100.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == [] and result["recorded"] == []
    assert push.calls == []


def test_no_reading_at_all_never_fires_from_the_window_side():
    conn = _conn()
    push = _Pusher()

    result = check_credit_risk(conn, "acct", None, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == []
    assert push.calls == []


# ---------------------------------------------------------------------------
# E1: a limit_hit flag fires it
# ---------------------------------------------------------------------------


def test_a_limit_hit_flag_fires_it_even_with_no_reading():
    conn = _conn()
    push = _Pusher()

    result = check_credit_risk(conn, "acct", None, True, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == ["flag"]
    assert len(push.calls) == 1


def test_s6_a_limit_hit_flag_also_gets_a_packet_item():
    """S6: E2's three steps apply to any E1 trigger; a flag names no window,
    so it gets the plain "usage limit" wording instead of a window name."""
    conn = _conn()
    push = _Pusher()

    result = check_credit_risk(
        conn, "acct", None, True, "2026-09-29T10:00:00Z", "2026-09", push,
    )

    assert len(result["packet_payloads"]) == 1
    payload = result["packet_payloads"][0]
    assert payload["what"] == "The plan's usage limit was reached on 2026-09-29"
    assert payload["why"] == "Usage credits are an emergency reserve; you asked to discuss any planned use first"
    assert payload["if_undecided"] == "nothing changes; the next month's first event alerts again"
    assert [o["key"] for o in payload["options"]] == ["fine", "find_out"]


def test_s6_the_flag_packet_item_passes_packet_add_strict(tmp_path):
    payload = credit_risk_packet_payload(["flag"], "2026-09-29")
    settings = packet_settings(tmp_path)

    item, warnings = add_item(settings, payload, strict=True)

    assert warnings == []
    assert item["priority"] == "blocking"


def test_s_b_a_trigger_row_never_carries_the_dropped_custodian_note():
    """S-b (fix check): E3's item is always written to the programme's own
    packet store unless [quota] packet_dir overrides it -- there is no
    longer a "cannot be written" case for check_credit_risk itself to note
    in a trigger's own row (routing the item to a store is the caller's
    job, cli/quota.py's _run_notify, not this function's)."""
    conn = _conn()
    push = _Pusher()

    check_credit_risk(conn, "acct", None, True, "2026-09-29T10:00:00Z", "2026-09", push)

    row = _notice_rows(conn, "acct", "credit_risk:flag:2026-09")[0]
    detail = json.loads(row["detail"])
    assert "custodian_note" not in detail


# ---------------------------------------------------------------------------
# E2: the push body carries no amount
# ---------------------------------------------------------------------------


def test_the_push_body_carries_no_dollar_amount():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    title, body = push.calls[0]
    assert title == "Plan limit reached: work may be using the emergency reserve"
    assert "$" not in body
    assert "STOP" in body


def test_n7_the_push_body_gives_the_reset_time_for_a_real_window():
    conn = _conn()
    push = _Pusher()
    # 2026-09-22T12:34:00Z -- only the HH:MM matters to this test
    reading = _reading(five_value=100.0, five_resets=1790080440)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    _, body = push.calls[0]
    assert "resets at 12:34Z" in body


def test_the_flag_trigger_ends_with_until_the_limit_resets():
    """E3 item 2: "the same, but beginning 'A session stopped on a usage
    limit.' and ending 'until the limit resets.'" (amended 2026-09-29)."""
    conn = _conn()
    push = _Pusher()

    check_credit_risk(conn, "acct", None, True, "2026-09-29T10:00:00Z", "2026-09", push)

    _, body = push.calls[0]
    assert body.startswith("A session stopped on a usage limit.")
    assert body.endswith("until the limit resets.")
    assert "resets at" not in body


# ---------------------------------------------------------------------------
# E2 item 3: the packet item passes `packet add --strict`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window_key", ["five_hour", "seven_day"])
def test_the_packet_item_passes_packet_add_strict(tmp_path, window_key):
    payload = credit_risk_packet_payload([window_key], "2026-09-29")
    settings = packet_settings(tmp_path)

    item, warnings = add_item(settings, payload, strict=True)

    assert warnings == []
    assert item["priority"] == "blocking"
    assert item["what"].startswith("The plan's")


def test_check_credit_risk_reports_packet_payloads_for_a_real_window_trigger():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert len(result["packet_payloads"]) == 1
    assert result["packet_payloads"][0]["what"] == "The plan's five-hour limit was reached on 2026-09-29"


def test_a_window_trigger_row_also_never_carries_the_dropped_custodian_note():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    row = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")[0]
    detail = json.loads(row["detail"])
    assert detail["credit_risk"] is True
    assert "custodian_note" not in detail


# ---------------------------------------------------------------------------
# E1: the flag's short rate limit -- both windows live with even their upper
# estimates under 100% means the plan's limit cannot have been reached
# ---------------------------------------------------------------------------


def test_e1_a_flag_is_suppressed_when_both_windows_are_live_and_low():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_basis="bound", five_upper=40.0, five_value=10.0, seven_basis="reading", seven_value=20.0)

    result = check_credit_risk(conn, "acct", reading, True, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == []
    assert push.calls == []
    # still recorded by process_limit_hit_flags -- check_credit_risk itself
    # never sees the flag file, only the caller's fresh_flag_seen bool.


def test_e1_a_flag_still_fires_when_one_window_is_not_live():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_basis="unknown", seven_basis="reading", seven_value=20.0)

    result = check_credit_risk(conn, "acct", reading, True, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == ["flag"]


def test_e1_a_flag_still_fires_when_a_window_is_at_or_above_100_too():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0, seven_basis="reading", seven_value=20.0)

    result = check_credit_risk(conn, "acct", reading, True, "2026-09-29T10:00:00Z", "2026-09", push)

    assert set(result["fired"]) == {"five_hour", "flag"}


# ---------------------------------------------------------------------------
# E3/E9: several triggers newly firing in one run give ONE push and ONE item
# ---------------------------------------------------------------------------


def test_e3_several_triggers_in_one_run_give_one_push_and_one_item():
    conn = _conn()
    push = _Pusher()
    reading = _reading(five_value=100.0, seven_value=100.0)

    result = check_credit_risk(
        conn, "acct", reading, True, "2026-09-29T10:00:00Z", "2026-09", push,
    )

    assert set(result["fired"]) == {"five_hour", "seven_day", "flag"}
    assert len(push.calls) == 1
    title, body = push.calls[0]
    assert "five-hour" in body and "weekly" in body
    assert len(result["packet_payloads"]) == 1
    assert "five-hour and weekly" in result["packet_payloads"][0]["what"]
    assert "a session stopped on it" in result["packet_payloads"][0]["what"]


# ---------------------------------------------------------------------------
# E4: the outbox route -- sent means delivered, retried on failure
# ---------------------------------------------------------------------------


class _QueueingPusher:
    """Simulates the outbox path: every call queues (never fails outright)
    and returns a fresh id; delivery is decided later by reconcile_result."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def __call__(self, title: str, body: str) -> str:
        self._n += 1
        entry_id = f"queued-{self._n}"
        self.calls.append((title, body))
        return entry_id


def test_s_d_a_new_trigger_after_a_delivered_alert_pushes_at_once():
    """Review fix check S-d, shaped exactly like the review's own
    attempts_probe.py: with notify_cmd (delivered at once), a five-hour
    alert at 10:00, then a NEW trigger (the weekly window) 30 minutes
    later, must push immediately -- not wait behind the 1-hour retry delay
    a PRIOR alert's own attempts used. E4's cap of 3 belongs to the retries
    of ONE alert, never to the whole account/month."""
    conn = _conn()
    push = _Pusher()
    live = lambda v, r: WindowReading("bound", v, max(v, 100.0), resets=r)
    five_r, seven_r = 1_790_690_000, 1_790_900_000

    r1 = check_credit_risk(
        conn, "a", Reading(five=live(100.0, five_r), seven=live(80.0, seven_r)), False,
        "2026-09-29T10:00:00Z", "2026-09", push,
    )
    assert r1["fired"] == ["five_hour"]

    r2 = check_credit_risk(
        conn, "a", Reading(five=live(100.0, five_r), seven=live(100.0, seven_r)), False,
        "2026-09-29T10:30:00Z", "2026-09", push,
    )
    assert r2["fired"] == ["seven_day"]  # pushed AT ONCE, not held behind a retry delay
    assert len(push.calls) == 2


def test_n_b_a_pending_alert_at_a_month_boundary_is_carried_and_reconciled():
    """Review fix check N-b: the attempt state used to be keyed purely on
    period_month, so a queued alert with no receipt yet at the boundary was
    never looked at again once the new month started -- the September row
    kept sent_ts NULL forever. Each carried trigger remembers its own
    origin month, so a receipt that comes back in October still updates
    SEPTEMBER's own quota_notice row."""
    conn = _conn()
    push = _QueueingPusher()
    reading = _reading(five_value=100.0)

    sept = check_credit_risk(conn, "acct", reading, False, "2026-09-30T23:55:00Z", "2026-09", push)
    assert sept["pending"] == ["five_hour"]
    assert len(push.calls) == 1

    oct_result = check_credit_risk(
        conn, "acct", None, False, "2026-10-01T00:05:00Z", "2026-10", push,
        reconcile_result={"delivered": ["queued-1"], "failed": []},
    )
    assert oct_result["pending"] == []
    row = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")[0]
    assert row["sent_ts"] == "2026-10-01T00:05:00Z"


def test_e4_a_queued_alert_is_not_sent_until_a_delivered_receipt_arrives():
    conn = _conn()
    push = _QueueingPusher()
    reading = _reading(five_value=100.0)

    result = check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    assert result["fired"] == []  # queued, not yet delivered
    assert result["pending"] == ["five_hour"]
    rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")
    assert rows[0]["sent_ts"] is None

    # no receipt yet: a second run within the hour must not queue again
    result2 = check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T10:05:00Z", "2026-09", push, reconcile_result={"delivered": [], "failed": []},
    )
    assert len(push.calls) == 1
    assert result2["pending"] == ["five_hour"]

    # now the receipt comes back delivered
    result3 = check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T10:10:00Z", "2026-09", push,
        reconcile_result={"delivered": ["queued-1"], "failed": []},
    )
    assert result3["pending"] == []
    rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")
    assert rows[0]["sent_ts"] == "2026-09-29T10:10:00Z"


def test_n_c_sent_ts_comes_from_the_receipts_own_ts_not_the_runs_clock():
    """Review fix check N-c: E4 says sent_ts is set to the receipt's own
    time -- a run's clock can drift from when the host actually sent it."""
    conn = _conn()
    push = _QueueingPusher()
    reading = _reading(five_value=100.0)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)
    check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T10:10:00Z", "2026-09", push,
        reconcile_result={
            "delivered": ["queued-1"], "failed": [],
            "receipts": {"queued-1": {"ts": "2026-09-29T10:04:17Z", "exit_code": 0}},
        },
    )
    rows = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")
    assert rows[0]["sent_ts"] == "2026-09-29T10:04:17Z"  # the receipt's own ts, not 10:10 (the run's clock)


def test_n_c_the_trigger_row_records_the_outbox_id_once_queued():
    conn = _conn()
    push = _QueueingPusher()
    reading = _reading(five_value=100.0)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)

    row = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")[0]
    assert json.loads(row["detail"])["outbox_id"] == "queued-1"


def test_n_c_a_failed_receipts_code_lands_in_the_triggers_own_detail():
    conn = _conn()
    push = _QueueingPusher()
    reading = _reading(five_value=100.0)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)
    check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T10:30:00Z", "2026-09", push,
        reconcile_result={
            "delivered": [], "failed": ["queued-1"],
            "receipts": {"queued-1": {"ts": "2026-09-29T10:05:00Z", "exit_code": 4}},
        },
    )
    row = _notice_rows(conn, "acct", "credit_risk:five_hour:2026-09")[0]
    assert json.loads(row["detail"])["failure_code"] == 4
    assert row["sent_ts"] is None  # still not delivered


def test_e4_a_failed_receipt_is_retried_after_an_hour_then_a_day_never_a_fourth_time():
    conn = _conn()
    push = _QueueingPusher()
    reading = _reading(five_value=100.0)

    check_credit_risk(conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push)  # attempt 1
    assert len(push.calls) == 1

    # failure comes back, but under an hour later -- no retry yet
    check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T10:30:00Z", "2026-09", push,
        reconcile_result={"delivered": [], "failed": ["queued-1"]},
    )
    assert len(push.calls) == 1

    # an hour after the first attempt -- attempt 2
    check_credit_risk(conn, "acct", reading, False, "2026-09-29T11:01:00Z", "2026-09", push)
    assert len(push.calls) == 2

    # its failure, under a day later -- no attempt 3 yet
    check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T12:00:00Z", "2026-09", push,
        reconcile_result={"delivered": [], "failed": ["queued-2"]},
    )
    assert len(push.calls) == 2

    # a day after attempt 2 -- attempt 3
    check_credit_risk(conn, "acct", reading, False, "2026-09-30T11:02:00Z", "2026-09", push)
    assert len(push.calls) == 3

    # its failure -- no fourth attempt, ever, this month
    check_credit_risk(
        conn, "acct", reading, False, "2026-09-30T12:00:00Z", "2026-09", push,
        reconcile_result={"delivered": [], "failed": ["queued-3"]},
    )
    check_credit_risk(conn, "acct", reading, False, "2026-09-30T13:00:00Z", "2026-09", push)
    assert len(push.calls) == 3


def test_e4_the_packet_item_is_written_once_at_first_firing_even_when_the_push_fails():
    conn = _conn()
    push = _QueueingPusher()
    reading = _reading(five_value=100.0)

    result1 = check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T10:00:00Z", "2026-09", push,
    )
    assert len(result1["packet_payloads"]) == 1

    result2 = check_credit_risk(
        conn, "acct", reading, False, "2026-09-29T10:05:00Z", "2026-09", push,
        reconcile_result={"delivered": [], "failed": ["queued-1"]},
    )
    assert result2["packet_payloads"] == []  # never again
