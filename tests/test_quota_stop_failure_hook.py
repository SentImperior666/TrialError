"""Tests for the ``StopFailure`` hook (design ``L4_quota-policy.md`` Section
2.6 / Section 3 item 6, amended by ``L9_exchange-rate-on-observed-spend.md``
§3 Part E5, 2026-09-29): :mod:`trialerror.hooks.stop_failure`.

Covers: it records keys and short error fields only (a long ``message``
field is never stored whole); it always exits 0; a ``limit_hit`` flag leads
to exactly one notice, recorded and cleared with NO push of its own (E5 --
that push is folded into L9 Part E's combined credit-risk alert, tested in
``test_quota_notify_credit_risk.py``); and nothing pushes from inside the
hook itself."""

from __future__ import annotations

import json
import sqlite3
from io import StringIO

import pytest

from trialerror.hooks import stop_failure
from trialerror.hooks.probe_log import hook_events_path
from trialerror.quota.notify import process_limit_hit_flags
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform


@pytest.fixture()
def probes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(tmp_path / "probes"))
    return tmp_path / "probes"


def _run_hook(payload: dict, monkeypatch) -> int:
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
    return stop_failure.main()


def _last_record(path) -> dict:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def test_records_keys_and_never_stores_a_long_message_field_whole(probes_home, monkeypatch):
    payload = {
        "session_id": "SESS-1",
        "cc_version": "2.1.141",
        "error_type": "overloaded_error",
        "message": "x" * 5000,  # NEVER a documented field name here -- must not leak through
    }
    rc = _run_hook(payload, monkeypatch)
    assert rc == 0
    record = _last_record(hook_events_path())
    assert record["hook"] == "stop_failure"
    assert set(record["keys"]) == set(payload)
    assert "message" not in record  # not a name containing error/reason: never copied
    assert record["error_class"] == "overloaded_error"


def test_a_field_whose_name_contains_error_is_captured_but_cut_to_100_chars(probes_home, monkeypatch):
    payload = {"session_id": "S", "long_error_detail": "y" * 500}
    rc = _run_hook(payload, monkeypatch)
    assert rc == 0
    record = _last_record(hook_events_path())
    assert record["long_error_detail"] == "y" * 100


def test_a_field_whose_name_contains_reason_is_captured_too(probes_home, monkeypatch):
    payload = {"session_id": "S", "reasonCode": "billing_hard_cap"}
    _run_hook(payload, monkeypatch)
    record = _last_record(hook_events_path())
    assert record["reasonCode"] == "billing_hard_cap"
    assert record["error_class"] == "billing_hard_cap"


def test_always_exits_0_on_garbage_stdin(probes_home, monkeypatch):
    monkeypatch.setattr("sys.stdin", StringIO("not json at all{{{"))
    assert stop_failure.main() == 0


def test_always_exits_0_when_the_probes_directory_is_unwritable(tmp_path, monkeypatch):
    unwritable = tmp_path / "probes_file_not_dir"
    unwritable.write_text("x", encoding="utf-8")  # a FILE where a directory is expected
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(unwritable))
    rc = _run_hook({"session_id": "S", "error_type": "rate_limit_error"}, monkeypatch)
    assert rc == 0


def test_a_rate_limit_class_writes_a_limit_hit_flag_file(probes_home, monkeypatch):
    _run_hook({"session_id": "S", "error_type": "rate_limit_error"}, monkeypatch)
    flags = list((probes_home / "quota_limit_hit").glob("*.flag"))
    assert len(flags) == 1
    data = json.loads(flags[0].read_text(encoding="utf-8"))
    assert data["error_class"] == "rate_limit_error"


def test_n3_a_bare_error_field_is_read_as_the_error_class(probes_home, monkeypatch):
    """Review N3: "error" is likely the documented field (a StopFailure
    payload's own top-level error code) and was missing from the candidate
    list entirely -- a payload shaped {"error": "rate_limit"} got
    error_class: null."""
    _run_hook({"session_id": "S", "error": "rate_limit"}, monkeypatch)
    record = _last_record(hook_events_path())
    assert record["error_class"] == "rate_limit"


def test_n3_an_overloaded_error_is_not_treated_as_a_quota_limit_hit(probes_home, monkeypatch):
    """Review N3: a 529 server-capacity error is not a quota/billing limit;
    matching "overloaded" made every such error a false "Quota limit hit"
    push. It is still detected and recorded (just never flagged)."""
    _run_hook({"session_id": "S", "error_type": "overloaded_error"}, monkeypatch)
    record = _last_record(hook_events_path())
    assert record["error_class"] == "overloaded_error"
    flag_dir = probes_home / "quota_limit_hit"
    assert not flag_dir.is_dir() or not list(flag_dir.glob("*.flag"))


def test_a_non_limit_error_writes_no_flag_file(probes_home, monkeypatch):
    _run_hook({"session_id": "S", "error_type": "network_timeout"}, monkeypatch)
    flag_dir = probes_home / "quota_limit_hit"
    assert not flag_dir.is_dir() or not list(flag_dir.glob("*.flag"))


def test_the_hook_never_pushes_anything_itself(probes_home, monkeypatch):
    """No network I/O in a hook (design's own rule): asserts no subprocess is
    ever launched while handling the payload."""

    def _forbidden(*_a, **_k):
        raise AssertionError("must not run a subprocess")

    monkeypatch.setattr("subprocess.run", _forbidden, raising=False)
    rc = _run_hook({"session_id": "S", "error_type": "rate_limit_error"}, monkeypatch)
    assert rc == 0


# --- E5: recorded and cleared, no push of its own -------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn, platform.MIGRATIONS)
    return conn


def test_e5_process_limit_hit_flags_takes_no_push_callable_any_more():
    """E5: "process_limit_hit_flags records each flag once in quota_notice
    and clears it, with no push of its own." """
    import inspect

    assert "push" not in inspect.signature(process_limit_hit_flags).parameters


def test_a_fresh_limit_hit_flag_is_recorded_and_cleared(probes_home, monkeypatch):
    _run_hook({"session_id": "S", "error_type": "rate_limit_error"}, monkeypatch)
    flags_dir = probes_home / "quota_limit_hit"
    flag_path = next(flags_dir.glob("*.flag"))
    flag_ts = json.loads(flag_path.read_text(encoding="utf-8"))["ts"]  # the hook's own real `now()`

    conn = _conn()
    report = process_limit_hit_flags(conn, flags_dir, "a", flag_ts)

    assert report["processed"] == 1
    assert report["fresh"] == 1
    assert report["backlog_cleared"] == 0
    assert len(report["fresh_flags"]) == 1
    rows = conn.execute("SELECT * FROM quota_notice WHERE kind = 'limit_hit'").fetchall()
    assert len(rows) == 1 and rows[0]["sent_ts"] == flag_ts
    assert not list(flags_dir.glob("*.flag"))  # consumed

    # a second run finds nothing left
    report2 = process_limit_hit_flags(conn, flags_dir, "a", flag_ts)
    assert report2 == {"processed": 0, "fresh": 0, "backlog_cleared": 0, "fresh_flags": []}


def test_e1_an_old_flag_is_recorded_and_cleared_without_alerting(probes_home, monkeypatch):
    """E1: "Older flags ... are recorded and cleared without an alert. That
    includes the backlog left since the hook was wired. The run reports how
    many it cleared." """
    _run_hook({"session_id": "S", "error_type": "rate_limit_error"}, monkeypatch)
    flags_dir = probes_home / "quota_limit_hit"
    conn = _conn()

    # well over 6h past the flag's own real `ts`, whatever today's wall clock is
    report = process_limit_hit_flags(conn, flags_dir, "a", "2026-10-15T00:00:00.000Z")

    assert report["processed"] == 1
    assert report["fresh"] == 0
    assert report["backlog_cleared"] == 1
    assert report["fresh_flags"] == []
    assert not list(flags_dir.glob("*.flag"))
    rows = conn.execute("SELECT * FROM quota_notice WHERE kind = 'limit_hit'").fetchall()
    assert len(rows) == 1  # still recorded, just not counted fresh


def test_e1_a_flag_with_no_readable_ts_is_backlog_too(probes_home):
    flags_dir = probes_home / "quota_limit_hit"
    flags_dir.mkdir(parents=True)
    (flags_dir / "a-b.flag").write_text(
        json.dumps({"session_id": "S", "error_class": "rate_limit_error"}), encoding="utf-8",
    )
    conn = _conn()

    report = process_limit_hit_flags(conn, flags_dir, "a", "2026-09-29T12:00:00.000Z")

    assert report["processed"] == 1
    assert report["fresh"] == 0
    assert report["backlog_cleared"] == 1
    assert not list(flags_dir.glob("*.flag"))
