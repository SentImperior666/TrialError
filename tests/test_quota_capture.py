"""Tests for the L4 capture additions to
:mod:`trialerror.obs.statusline_capture` (design ``L4_quota-policy.md``
Section 2.1 / Section 3 item 1): ``account_label``, ``session_cost_usd``,
the new "cost rose by >= 0.50" history-append rule, and the ``accounts.json``
upsert. Run as a bare file the same way ``tests/test_statusline_quota.py``
does -- this script is deliberately stdlib-only and importable with no
``trialerror`` on the path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "trialerror" / "obs" / "statusline_capture.py"

SAMPLE = {
    "session_id": "sess-test",
    "version": "2.1.141",
    "model": {"id": "claude-fable-5", "display_name": "Fable 5"},
    "context_window": {"used_percentage": 20},
    "rate_limits": {
        "five_hour": {"used_percentage": 42, "resets_at": 1790481600},
        "seven_day": {"used_percentage": 52, "resets_at": 1791018000},
    },
}


def _run(payload: dict, quota_dir: Path, *, account: str | None = None) -> subprocess.CompletedProcess:
    env = {"TRIALERROR_QUOTA_DIR": str(quota_dir), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    if account is not None:
        env["TRIALERROR_ACCOUNT"] = account
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _latest(quota_dir: Path) -> dict:
    return json.loads((quota_dir / "latest.json").read_text(encoding="utf-8"))


def _history(quota_dir: Path) -> list[dict]:
    text = (quota_dir / "rate_limits.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _accounts(quota_dir: Path) -> dict:
    return json.loads((quota_dir / "accounts.json").read_text(encoding="utf-8"))


# --- account_label -----------------------------------------------------------------------------------------------


def test_account_label_comes_from_the_environment(tmp_path):
    proc = _run(SAMPLE, tmp_path, account="acct-a")
    assert proc.returncode == 0
    assert _latest(tmp_path)["account_label"] == "acct-a"
    assert _history(tmp_path)[0]["account_label"] == "acct-a"


def test_account_label_is_the_empty_string_when_unset(tmp_path):
    proc = _run(SAMPLE, tmp_path)
    assert proc.returncode == 0
    assert _latest(tmp_path)["account_label"] == ""


# --- session_cost_usd ---------------------------------------------------------------------------------------------


def test_session_cost_usd_is_taken_from_cost_total_cost_usd(tmp_path):
    payload = dict(SAMPLE, cost={"total_cost_usd": 1.25})
    _run(payload, tmp_path)
    assert _latest(tmp_path)["session_cost_usd"] == 1.25
    assert _history(tmp_path)[0]["session_cost_usd"] == 1.25


def test_session_cost_usd_is_null_when_absent_or_non_numeric(tmp_path):
    _run(SAMPLE, tmp_path)
    assert _latest(tmp_path)["session_cost_usd"] is None
    _run(dict(SAMPLE, cost={"total_cost_usd": "lots"}), tmp_path)
    assert _latest(tmp_path)["session_cost_usd"] is None


# --- the new append rule: cost rose by >= 0.50 --------------------------------------------------------------------


def test_a_new_session_forces_the_first_history_row(tmp_path):
    # global throttle would otherwise force nothing new for the SECOND
    # session's low-percentage-move capture; the "no row yet" clause must
    # still append it.
    _run(dict(SAMPLE, session_id="sess-a", cost={"total_cost_usd": 0.1}), tmp_path)
    assert len(_history(tmp_path)) == 1
    _run(dict(SAMPLE, session_id="sess-b", cost={"total_cost_usd": 0.1}), tmp_path)
    history = _history(tmp_path)
    assert len(history) == 2
    assert history[-1]["session_id"] == "sess-b"


def test_a_cost_rise_of_exactly_0_50_forces_a_row_but_0_49_does_not(tmp_path):
    _run(dict(SAMPLE, session_id="sess-a", cost={"total_cost_usd": 1.00}), tmp_path)
    assert len(_history(tmp_path)) == 1
    # 0.49 rise, same pcts, well within the 300s throttle: no new row
    _run(dict(SAMPLE, session_id="sess-a", cost={"total_cost_usd": 1.49}), tmp_path)
    assert len(_history(tmp_path)) == 1
    # one more cent tips it to a 0.50 total rise since the last HISTORY row
    _run(dict(SAMPLE, session_id="sess-a", cost={"total_cost_usd": 1.50}), tmp_path)
    assert len(_history(tmp_path)) == 2


def test_the_existing_throttle_tests_still_pass_unaffected_by_cost(tmp_path):
    payload = dict(SAMPLE, cost={"total_cost_usd": 1.0})
    _run(payload, tmp_path)
    _run(payload, tmp_path)  # identical pcts and cost, within throttle: no new row
    assert len(_history(tmp_path)) == 1
    moved = json.loads(json.dumps(payload))
    moved["rate_limits"]["five_hour"]["used_percentage"] = 44
    _run(moved, tmp_path)  # a real percentage move still appends
    assert len(_history(tmp_path)) == 2


# --- accounts.json -------------------------------------------------------------------------------------------------


def test_accounts_json_upserts_first_seen_last_seen_and_n_rows(tmp_path):
    _run(SAMPLE, tmp_path, account="acct-a")
    doc = _accounts(tmp_path)
    assert set(doc) == {"acct-a"}
    first = doc["acct-a"]
    assert first["n_rows"] == 1
    assert first["first_seen_ts"] == first["last_seen_ts"]
    assert first["windows_seen"] == [1790481600]

    # review N2: n_rows counts APPENDED history rows, not status-line ticks --
    # an identical repeat (throttled, no new history row) must not bump it,
    # even though last_seen_ts still advances (the account WAS observed).
    _run(SAMPLE, tmp_path, account="acct-a")
    unchanged = _accounts(tmp_path)["acct-a"]
    assert unchanged["n_rows"] == 1
    assert unchanged["last_seen_ts"] >= first["last_seen_ts"]

    moved = json.loads(json.dumps(SAMPLE))
    moved["rate_limits"]["five_hour"]["used_percentage"] = 44
    _run(moved, tmp_path, account="acct-a")
    second = _accounts(tmp_path)["acct-a"]
    assert second["n_rows"] == 2
    assert second["first_seen_ts"] == first["first_seen_ts"]
    assert second["windows_seen"] == [1790481600]  # same reset: not duplicated


def test_accounts_json_tracks_distinct_labels_and_the_unlabelled_account(tmp_path):
    _run(SAMPLE, tmp_path, account="acct-a")
    _run(SAMPLE, tmp_path)  # unlabelled
    doc = _accounts(tmp_path)
    assert set(doc) == {"acct-a", ""}


def test_windows_seen_is_capped_at_20(tmp_path):
    for i in range(25):
        payload = json.loads(json.dumps(SAMPLE))
        payload["rate_limits"]["five_hour"]["resets_at"] = 1790481600 + i * 18000
        _run(payload, tmp_path, account="a")
    windows = _accounts(tmp_path)["a"]["windows_seen"]
    assert len(windows) == 20
    assert windows[-1] == 1790481600 + 24 * 18000  # the most recent 20, oldest dropped


def test_accounts_json_is_not_written_when_rate_limits_are_absent(tmp_path):
    payload = {k: v for k, v in SAMPLE.items() if k != "rate_limits"}
    _run(payload, tmp_path, account="a")
    assert not (tmp_path / "accounts.json").exists()


def test_a_torn_accounts_file_never_breaks_the_status_line(tmp_path):
    (tmp_path / "accounts.json").write_text("{torn", encoding="utf-8")
    proc = _run(SAMPLE, tmp_path, account="a")
    assert proc.returncode == 0
    assert _accounts(tmp_path)["a"]["n_rows"] == 1


# --- N1: last_history_cost_usd must be persisted, not lost to the sessions.json rewrite-skip ------------------------


def test_an_unchanged_cost_after_upgrade_does_not_flood_history_with_extra_rows(tmp_path):
    """Review N1: the rewrite-skip compared model/cc_version/cost/window_pct
    but not last_history_cost_usd, so a tick that had just appended a
    history row could still be skipped as "nothing changed" -- which meant
    the new field was never actually written to disk, so the NEXT tick read
    it back as missing ("no row yet") and forced ANOTHER append. At upgrade
    (a pre-L4 sessions.json entry, lacking the field entirely) this appended
    a row on every tick for up to 60s regardless of the throttle."""
    import time

    quota_dir = tmp_path
    quota_dir.mkdir(exist_ok=True)
    now = time.time()
    # a pre-L4-shaped entry: no last_history_cost_usd at all, otherwise
    # identical to what the upcoming tick will compute (same cost, same
    # window_pct) so ONLY that missing field can trigger (or, pre-fix,
    # fail to trigger) the rewrite.
    (quota_dir / "sessions.json").write_text(json.dumps({
        "version": 1,
        "sessions": {
            "sess-test": {
                "first_seen_ts": "2026-09-27T00:00:00Z",
                "last_seen_ts": "2026-09-27T00:00:00Z",
                "last_seen_epoch": now - 5,
                "model": "Fable 5",
                "cc_version": "2.1.141",
                "cost": {"total_cost_usd": 1.0},
                "window_pct": {"five_hour": 42.0, "seven_day": 52.0},
            }
        },
    }), encoding="utf-8")

    payload = dict(SAMPLE, cost={"total_cost_usd": 1.0})
    _run(payload, quota_dir)  # tick 1: "no row yet" (missing field) forces one append, as designed
    _run(payload, quota_dir)  # tick 2: cost unchanged since tick 1 -- must NOT force another
    _run(payload, quota_dir)  # tick 3: likewise

    assert len(_history(quota_dir)) == 1
    sessions = json.loads((quota_dir / "sessions.json").read_text(encoding="utf-8"))["sessions"]
    assert sessions["sess-test"]["last_history_cost_usd"] == 1.0


# --- prev_five_resets / prev_seven_resets ------------------------------------------------------------------------


def _weekly_only(payload: dict) -> dict:
    """The same payload with no five-hour figure: the seven-day figure alone, as stamped just after a five-hour reset."""
    out = json.loads(json.dumps(payload))
    del out["rate_limits"]["five_hour"]
    return out


def test_latest_keeps_the_last_five_hour_reset_when_a_payload_carries_no_five_hour_figure(tmp_path):
    quota = tmp_path / "quota"
    assert _run(SAMPLE, quota).returncode == 0
    assert "prev_five_resets" not in _latest(quota)  # a payload that carries the window keeps nothing extra
    assert _run(_weekly_only(SAMPLE), quota).returncode == 0
    latest = _latest(quota)
    assert latest["prev_five_resets"] == SAMPLE["rate_limits"]["five_hour"]["resets_at"]
    assert "five_hour" not in latest["rate_limits"]
    assert "prev_seven_resets" not in latest  # the weekly figure was there


def test_the_carried_reset_survives_a_second_figure_less_payload_and_clears_when_the_figure_returns(tmp_path):
    quota = tmp_path / "quota"
    _run(SAMPLE, quota)
    _run(_weekly_only(SAMPLE), quota)
    _run(_weekly_only(SAMPLE), quota)
    assert _latest(quota)["prev_five_resets"] == SAMPLE["rate_limits"]["five_hour"]["resets_at"]
    _run(SAMPLE, quota)
    assert "prev_five_resets" not in _latest(quota)


def test_the_seven_day_reset_is_carried_the_same_way(tmp_path):
    quota = tmp_path / "quota"
    _run(SAMPLE, quota)
    only_five = json.loads(json.dumps(SAMPLE))
    del only_five["rate_limits"]["seven_day"]
    _run(only_five, quota)
    assert _latest(quota)["prev_seven_resets"] == SAMPLE["rate_limits"]["seven_day"]["resets_at"]


def test_history_rows_never_carry_the_prev_keys(tmp_path):
    quota = tmp_path / "quota"
    _run(SAMPLE, quota)
    weekly = _weekly_only(SAMPLE)
    weekly["rate_limits"]["seven_day"]["used_percentage"] = 60  # a 1+ point change appends a row
    _run(weekly, quota)
    rows = _history(quota)
    assert len(rows) == 2
    assert all("prev_five_resets" not in r and "prev_seven_resets" not in r for r in rows)
