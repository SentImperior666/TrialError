"""Tests for ``trialerror quota import`` (design ``L4_quota-policy.md``
Section 2.3 step 1 / Section 2.5 / Section 3 item 2):
:func:`trialerror.quota.capture_import.import_history` and
:func:`trialerror.quota.rate.mixed_account_windows`."""

from __future__ import annotations

import json
import sqlite3

from trialerror.quota.capture_import import import_history
from trialerror.quota.rate import mixed_account_windows
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn, platform.MIGRATIONS)
    return conn


def _write_history(path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _row(epoch, five_resets=2_000_018_000, session="S1", account="a", cost=1.0, seven_resets=2_000_000_000):
    return {
        "epoch": epoch,
        "captured_ts": "2026-09-27T12:00:00Z",
        "session_id": session,
        "session_cost_usd": cost,
        "account_label": account,
        "rate_limits": {
            "five_hour": {"used_percentage": 40, "resets_at": five_resets},
            "seven_day": {"used_percentage": 50, "resets_at": seven_resets},
        },
        "cc_version": "2.1.141",
        "model": "Fable 5",
    }


def test_import_inserts_rows_and_reports_counts(tmp_path):
    conn = _conn()
    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_row(100), _row(700)])
    report = import_history(conn, str(history), "dev")
    assert report == {"host": "dev", "rows_read": 2, "rows_inserted": 2, "rows_duplicate": 0, "rows_malformed": 0}
    rows = conn.execute("SELECT * FROM quota_capture").fetchall()
    assert len(rows) == 2
    assert rows[0]["five_pct"] == 40 and rows[0]["session_cost_usd"] == 1.0


def test_import_is_idempotent_on_a_second_run_of_the_same_file(tmp_path):
    conn = _conn()
    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_row(100), _row(700)])
    import_history(conn, str(history), "dev")
    report = import_history(conn, str(history), "dev")
    assert report["rows_inserted"] == 0
    assert report["rows_duplicate"] == 2
    assert conn.execute("SELECT COUNT(*) FROM quota_capture").fetchone()[0] == 2


def test_import_handles_both_hosts_independently(tmp_path):
    conn = _conn()
    dev_history = tmp_path / "dev.jsonl"
    sandbox_history = tmp_path / "sandbox.jsonl"
    _write_history(dev_history, [_row(100)])
    _write_history(sandbox_history, [_row(100)])  # same epoch, different host: not a duplicate
    import_history(conn, str(dev_history), "dev")
    report = import_history(conn, str(sandbox_history), "sandbox")
    assert report["rows_inserted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM quota_capture").fetchone()[0] == 2


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    conn = _conn()
    history = tmp_path / "rate_limits.jsonl"
    history.write_text(json.dumps(_row(100)) + "\nnot json{{\n[1,2,3]\n", encoding="utf-8")
    report = import_history(conn, str(history), "dev")
    assert report == {"host": "dev", "rows_read": 3, "rows_inserted": 1, "rows_duplicate": 0, "rows_malformed": 2}


def test_a_missing_history_file_reports_zero_rows_rather_than_raising(tmp_path):
    conn = _conn()
    report = import_history(conn, str(tmp_path / "nope.jsonl"), "dev")
    assert report == {"host": "dev", "rows_read": 0, "rows_inserted": 0, "rows_duplicate": 0, "rows_malformed": 0}


def test_mixed_account_guard_flags_disagreeing_five_resets_at_the_same_time(tmp_path):
    conn = _conn()
    dev_history = tmp_path / "dev.jsonl"
    sandbox_history = tmp_path / "sandbox.jsonl"
    # both captured near the same instant, inside dev's own window span, but
    # the reset times disagree by more than the 900s tolerance: different
    # accounts.
    _write_history(dev_history, [_row(1_999_990_000, five_resets=2_000_000_000, account="a")])
    _write_history(sandbox_history, [_row(1_999_990_010, five_resets=2_000_010_000, account="a")])
    import_history(conn, str(dev_history), "dev")
    import_history(conn, str(sandbox_history), "sandbox")
    flagged = mixed_account_windows(conn, "a")
    assert flagged == {2_000_000_000, 2_000_010_000}


def test_agreeing_five_resets_within_tolerance_is_not_flagged(tmp_path):
    conn = _conn()
    dev_history = tmp_path / "dev.jsonl"
    sandbox_history = tmp_path / "sandbox.jsonl"
    _write_history(dev_history, [_row(1_999_990_000, five_resets=2_000_000_000, account="a")])
    _write_history(sandbox_history, [_row(1_999_990_010, five_resets=2_000_000_500, account="a")])
    import_history(conn, str(dev_history), "dev")
    import_history(conn, str(sandbox_history), "sandbox")
    assert mixed_account_windows(conn, "a") == set()


def test_mixed_account_guard_catches_a_sparse_capture_inside_the_window_span(tmp_path):
    """Review S2: the original guard only compared captures within 900s of
    each other, so a host whose statusLine ticks rarely (an idle machine)
    could sit inside another host's whole five-hour window without ever
    landing near one of its captures -- and nothing was flagged. Comparing
    against the WINDOW SPAN instead needs no coincidence of timing: host B
    captures once, well inside host A's window, with its own reset 2 hours
    later and never within 900s of any of host A's captures."""
    conn = _conn()
    dev_history = tmp_path / "dev.jsonl"
    sandbox_history = tmp_path / "sandbox.jsonl"
    reset_a = 2_000_000_000
    # dev's one capture, near the end of its own window.
    _write_history(dev_history, [_row(reset_a - 100, five_resets=reset_a, account="a")])
    # sandbox's one capture, well inside dev's window span (reset_a - 5h .. reset_a)
    # but 14,900s away from dev's own capture -- far outside the 900s
    # proximity the old guard required -- on a DIFFERENT account (reset 2h later).
    reset_b = reset_a + 7200
    _write_history(sandbox_history, [_row(reset_a - 15_000, five_resets=reset_b, account="a")])
    import_history(conn, str(dev_history), "dev")
    import_history(conn, str(sandbox_history), "sandbox")
    flagged = mixed_account_windows(conn, "a")
    assert flagged == {reset_a, reset_b}
