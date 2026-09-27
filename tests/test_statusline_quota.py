"""Tests for the statusLine plan-quota feed: capture script
(trialerror/obs/statusline_capture.py, run as a bare file the way Claude Code
invokes it) and read side (trialerror.budget.quota)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.budget.quota import quota_status, read_latest_quota

SCRIPT = Path(__file__).resolve().parents[1] / "trialerror" / "obs" / "statusline_capture.py"

SAMPLE = {
    "session_id": "sess-test",
    "version": "2.1.141",
    "model": {"id": "claude-fable-5", "display_name": "Fable 5"},
    "context_window": {"used_percentage": 20},
    "rate_limits": {
        "five_hour": {"used_percentage": 42.3, "resets_at": "2026-08-29T18:00:00Z"},
        "seven_day": {"used_percentage": 67.0, "resets_at": "2026-09-01T08:00:00Z"},
    },
}


def _run(stdin_text: str, quota_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env={"TRIALERROR_QUOTA_DIR": str(quota_dir), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
        timeout=30,
    )


def test_capture_writes_latest_and_history_and_prints(tmp_path):
    proc = _run(json.dumps(SAMPLE), tmp_path)
    assert proc.returncode == 0
    assert "5h 42%" in proc.stdout and "7d 67%" in proc.stdout and "ctx 20%" in proc.stdout
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert latest["rate_limits"]["five_hour"]["used_percentage"] == 42.3
    assert latest["model"] == "Fable 5"
    history = (tmp_path / "rate_limits.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(history) == 1


def test_capture_throttles_unchanged_history_but_updates_latest(tmp_path):
    _run(json.dumps(SAMPLE), tmp_path)
    proc = _run(json.dumps(SAMPLE), tmp_path)
    assert proc.returncode == 0
    history = (tmp_path / "rate_limits.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(history) == 1  # same pcts within throttle window -> no second row
    moved = json.loads(json.dumps(SAMPLE))
    moved["rate_limits"]["five_hour"]["used_percentage"] = 44.0
    _run(json.dumps(moved), tmp_path)
    history = (tmp_path / "rate_limits.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(history) == 2  # >=1pt move -> appended
    assert read_latest_quota(str(tmp_path))["rate_limits"]["five_hour"]["used_percentage"] == 44.0


def test_capture_without_rate_limits_prints_but_writes_nothing(tmp_path):
    payload = {k: v for k, v in SAMPLE.items() if k != "rate_limits"}
    proc = _run(json.dumps(payload), tmp_path)
    assert proc.returncode == 0
    assert "ctx 20%" in proc.stdout
    assert not (tmp_path / "latest.json").exists()


def test_capture_never_crashes_on_garbage(tmp_path):
    proc = _run("this is not json{{{", tmp_path)
    assert proc.returncode == 0
    assert "TRIALERROR" in proc.stdout


def test_quota_status_fresh_stale_missing(tmp_path):
    missing = quota_status(str(tmp_path))
    assert missing == {**missing, "available": False, "fresh": False}
    _run(json.dumps(SAMPLE), tmp_path)
    fresh = quota_status(str(tmp_path))
    assert fresh["available"] and fresh["fresh"]
    assert fresh["windows"]["seven_day"]["used_percentage"] == 67.0
    epoch = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["epoch"]
    stale = quota_status(str(tmp_path), now_epoch=epoch + 3600)
    assert stale["available"] and not stale["fresh"]
    assert stale["age_s"] >= 3600


def test_quota_status_survives_torn_latest(tmp_path):
    (tmp_path / "latest.json").write_text("{torn", encoding="utf-8")
    status = quota_status(str(tmp_path))
    assert status["available"] is False


# --- A3: numbers_changed_ts, payload_cost_api_ms and the per-session cost file --------------------------------


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("statusline_capture_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _latest(quota_dir: Path) -> dict:
    return json.loads((quota_dir / "latest.json").read_text(encoding="utf-8"))


def test_numbers_changed_ts_is_kept_while_the_numbers_are_unchanged_and_reset_when_they_move(tmp_path):
    _run(json.dumps(SAMPLE), tmp_path)
    first = _latest(tmp_path)
    assert first["numbers_changed_ts"] == first["captured_ts"]
    # pretend the numbers have been the same since long ago
    aged = dict(first, numbers_changed_ts="2026-01-01T00:00:00Z")
    (tmp_path / "latest.json").write_text(json.dumps(aged), encoding="utf-8")
    _run(json.dumps(SAMPLE), tmp_path)
    second = _latest(tmp_path)
    assert second["numbers_changed_ts"] == "2026-01-01T00:00:00Z"  # kept: nothing moved
    assert second["captured_ts"] >= first["captured_ts"]
    moved = json.loads(json.dumps(SAMPLE))
    moved["rate_limits"]["five_hour"]["used_percentage"] = 44.0
    _run(json.dumps(moved), tmp_path)
    third = _latest(tmp_path)
    assert third["numbers_changed_ts"] == third["captured_ts"] != "2026-01-01T00:00:00Z"
    # a moved reset time counts as a change too
    (tmp_path / "latest.json").write_text(json.dumps(dict(third, numbers_changed_ts="2026-01-01T00:00:00Z")), encoding="utf-8")
    reset = json.loads(json.dumps(moved))
    reset["rate_limits"]["five_hour"]["resets_at"] = "2026-08-29T23:00:00Z"
    _run(json.dumps(reset), tmp_path)
    assert _latest(tmp_path)["numbers_changed_ts"] != "2026-01-01T00:00:00Z"


def test_payload_cost_api_ms_is_recorded_when_present_and_null_otherwise(tmp_path):
    with_cost = dict(SAMPLE, cost={"total_api_duration_ms": 12345, "total_cost_usd": 0.5})
    _run(json.dumps(with_cost), tmp_path)
    assert _latest(tmp_path)["payload_cost_api_ms"] == 12345
    _run(json.dumps(SAMPLE), tmp_path)
    assert _latest(tmp_path)["payload_cost_api_ms"] is None
    _run(json.dumps(dict(SAMPLE, cost={"total_api_duration_ms": "lots"})), tmp_path)
    assert _latest(tmp_path)["payload_cost_api_ms"] is None


def test_the_history_throttle_is_unaffected_by_the_new_fields(tmp_path):
    payload = dict(SAMPLE, cost={"total_api_duration_ms": 1})
    _run(json.dumps(payload), tmp_path)
    _run(json.dumps(dict(payload, cost={"total_api_duration_ms": 2})), tmp_path)
    assert len((tmp_path / "rate_limits.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1


def test_sessions_json_holds_each_sessions_cost_and_updates_in_place(tmp_path):
    a = dict(SAMPLE, session_id="sess-a", cost={"total_cost_usd": 0.25, "total_api_duration_ms": 900,
                                                "total_lines_added": 4, "note": "ignored", "total_duration_ms": True})
    b = dict(SAMPLE, session_id="sess-b", cost={"total_cost_usd": 1.5})
    _run(json.dumps(a), tmp_path)
    _run(json.dumps(b), tmp_path)
    doc = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert doc["version"] == 1 and set(doc["sessions"]) == {"sess-a", "sess-b"}
    entry = doc["sessions"]["sess-a"]
    assert entry["cost"] == {"total_cost_usd": 0.25, "total_api_duration_ms": 900, "total_lines_added": 4}
    assert entry["model"] == "Fable 5" and entry["cc_version"] == "2.1.141"
    assert entry["window_pct"]["five_hour"] == 42.3
    first_seen = entry["first_seen_ts"]
    _run(json.dumps(dict(a, cost={"total_cost_usd": 0.75})), tmp_path)
    again = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"]["sess-a"]
    assert again["cost"] == {"total_cost_usd": 0.75} and again["first_seen_ts"] == first_seen


def test_a_session_without_rate_limits_still_gets_its_cost_recorded_but_no_latest(tmp_path):
    payload = {k: v for k, v in SAMPLE.items() if k != "rate_limits"}
    payload["cost"] = {"total_cost_usd": 0.1}
    proc = _run(json.dumps(payload), tmp_path)
    assert proc.returncode == 0 and not (tmp_path / "latest.json").exists()
    doc = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert doc["sessions"]["sess-test"]["cost"] == {"total_cost_usd": 0.1}
    assert "window_pct" not in doc["sessions"]["sess-test"]


def test_no_cost_or_no_session_id_writes_no_sessions_file(tmp_path):
    _run(json.dumps(SAMPLE), tmp_path)
    _run(json.dumps(dict(SAMPLE, session_id=None, cost={"total_cost_usd": 1})), tmp_path)
    _run(json.dumps(dict(SAMPLE, cost={"nothing": "numeric"})), tmp_path)
    assert not (tmp_path / "sessions.json").exists()


def test_sessions_json_drops_old_sessions_and_caps_its_size(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(tmp_path))
    now = 2_000_000_000.0
    mod._update_sessions({"session_id": "old", "cost": {"total_cost_usd": 1}}, now - 31 * 86400)
    mod._update_sessions({"session_id": "recent", "cost": {"total_cost_usd": 2}}, now - 1 * 86400)
    mod._update_sessions({"session_id": "now", "cost": {"total_cost_usd": 3}}, now)
    assert set(json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"]) == {"recent", "now"}
    monkeypatch.setattr(mod, "SESSIONS_MAX", 3)
    for i in range(5):
        mod._update_sessions({"session_id": f"s{i}", "cost": {"total_cost_usd": i}}, now + i)
    kept = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"]
    assert set(kept) == {"s2", "s3", "s4"}  # the newest three


def test_a_torn_sessions_file_or_odd_payloads_never_break_the_status_line(tmp_path):
    (tmp_path / "sessions.json").write_text("{torn", encoding="utf-8")
    proc = _run(json.dumps(dict(SAMPLE, cost={"total_cost_usd": 1})), tmp_path)
    assert proc.returncode == 0 and "5h 42%" in proc.stdout
    assert json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"]["sess-test"]
    for weird in ({"cost": [1, 2]}, {"cost": "x"}, {"session_id": 5, "cost": {"total_cost_usd": 1}}, {"model": "notadict"}):
        assert _run(json.dumps(dict(SAMPLE, **weird)), tmp_path).returncode == 0


# --- S-4: a concurrent reader must not lose the update or leave a temp file behind ---------------------------


def _tmp_leftovers(quota_dir: Path) -> list[str]:
    return sorted(p.name for p in quota_dir.iterdir() if ".tmp" in p.name)


def _hold_open(path: Path, seconds: float):
    """Hold ``path`` open for reading for a while, the way a second session or the meter would."""
    import threading
    import time

    ready, handle = threading.Event(), {}

    def hold():
        with open(path, "rb") as f:
            handle["f"] = f
            ready.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(5)
    return thread


@pytest.mark.skipif(sys.platform != "win32", reason="a reader blocks os.replace on Windows only")
def test_a_reader_that_lets_go_within_the_retry_window_does_not_lose_the_update(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "REPLACE_SLEEP_S", 0.05)
    payload = dict(SAMPLE, cost={"total_cost_usd": 1})
    mod._update_sessions(payload, 1_900_000_000.0)
    target = tmp_path / "sessions.json"
    thread = _hold_open(target, 0.08)
    try:
        mod._update_sessions(dict(payload, cost={"total_cost_usd": 2}), 1_900_000_010.0)
    finally:
        thread.join()
    assert json.loads(target.read_text(encoding="utf-8"))["sessions"]["sess-test"]["cost"] == {"total_cost_usd": 2}
    assert _tmp_leftovers(tmp_path) == []


@pytest.mark.skipif(sys.platform != "win32", reason="a reader blocks os.replace on Windows only")
def test_a_reader_that_never_lets_go_costs_the_update_but_not_a_stray_temp_file(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "REPLACE_SLEEP_S", 0.001)
    mod._update_sessions(dict(SAMPLE, cost={"total_cost_usd": 1}), 1_900_000_000.0)
    target = tmp_path / "sessions.json"
    thread = _hold_open(target, 0.5)
    try:
        with pytest.raises(PermissionError):
            mod._update_sessions(dict(SAMPLE, cost={"total_cost_usd": 2}), 1_900_000_010.0)
        assert _tmp_leftovers(tmp_path) == []
        # the same for latest.json, written by _write
        mod._write({"epoch": 1_900_000_000.0, "captured_ts": "x", "rate_limits": {}})
    except PermissionError:
        pass
    finally:
        thread.join()
    assert _tmp_leftovers(tmp_path) == []


@pytest.mark.skipif(sys.platform != "win32", reason="a reader blocks os.replace on Windows only")
def test_the_status_line_still_prints_when_latest_json_is_held_open(tmp_path):
    _run(json.dumps(SAMPLE), tmp_path)
    thread = _hold_open(tmp_path / "latest.json", 1.5)
    try:
        proc = _run(json.dumps(dict(SAMPLE, cost={"total_cost_usd": 3})), tmp_path)
    finally:
        thread.join()
    assert proc.returncode == 0 and proc.stdout.strip().startswith("TRIALERROR |")
    assert _tmp_leftovers(tmp_path) == []


def test_a_write_that_fails_part_way_leaves_no_temp_file(tmp_path, monkeypatch):
    mod = _load_module()
    target = tmp_path / "sessions.json"

    def broken(f):
        f.write("{half")
        raise ValueError("boom")

    with pytest.raises(ValueError):
        mod._atomic_write(str(target), broken)
    assert not target.exists() and _tmp_leftovers(tmp_path) == []


def test_old_temp_files_are_swept_and_recent_ones_and_real_files_are_kept(tmp_path):
    import os
    import time

    mod = _load_module()
    now = time.time()
    old = tmp_path / "latest.json.tmp111"
    old2 = tmp_path / "sessions.json.tmp222"
    fresh = tmp_path / "latest.json.tmp333"
    real = tmp_path / "latest.json"
    for f in (old, old2, fresh, real):
        f.write_text("x", encoding="utf-8")
    for f in (old, old2):
        os.utime(f, (now - 2 * 3600, now - 2 * 3600))
    os.utime(real, (now - 9 * 3600, now - 9 * 3600))
    mod._sweep_stale_tmp(str(tmp_path), now)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["latest.json", "latest.json.tmp333"]


def test_every_render_sweeps_stale_temp_files(tmp_path):
    import os
    import time

    stale = tmp_path / "sessions.json.tmp4242"
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    proc = _run(json.dumps(SAMPLE), tmp_path)
    assert proc.returncode == 0 and not stale.exists()


# --- N-5: sessions.json is not rewritten while nothing but the clock moved -----------------------------------


def test_sessions_json_is_not_rewritten_when_only_last_seen_moved_less_than_a_minute(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(tmp_path))
    payload = dict(SAMPLE, cost={"total_cost_usd": 1, "total_api_duration_ms": 10})
    base = 1_900_000_000.0
    target = tmp_path / "sessions.json"

    def entry():
        return json.loads(target.read_text(encoding="utf-8"))["sessions"]["sess-test"]

    writes = []
    real = mod._atomic_write
    monkeypatch.setattr(mod, "_atomic_write", lambda path, write: (writes.append(path), real(path, write)))
    mod._update_sessions(payload, base)
    mod._update_sessions(payload, base + 10)  # nothing changed, 10 s later: no write
    mod._update_sessions(payload, base + 59)  # still under a minute since the last WRITE
    assert len(writes) == 1 and entry()["last_seen_ts"] == time_stamp(base)
    mod._update_sessions(payload, base + 61)  # a minute on: last_seen is refreshed
    assert len(writes) == 2 and entry()["last_seen_ts"] == time_stamp(base + 61)
    mod._update_sessions(dict(payload, cost={"total_cost_usd": 2, "total_api_duration_ms": 10}), base + 62)
    assert len(writes) == 3 and entry()["cost"]["total_cost_usd"] == 2  # a changed figure is always written
    mod._update_sessions(dict(payload, cost={"total_cost_usd": 2, "total_api_duration_ms": 10}, model={"display_name": "Other"}), base + 63)
    assert len(writes) == 4  # so is a changed model
    windows = dict(payload, cost={"total_cost_usd": 2, "total_api_duration_ms": 10}, model={"display_name": "Other"})
    windows["rate_limits"] = {"five_hour": {"used_percentage": 50.0}}
    mod._update_sessions(windows, base + 64)
    assert len(writes) == 5 and entry()["window_pct"] == {"five_hour": 50.0}


def time_stamp(epoch):
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
