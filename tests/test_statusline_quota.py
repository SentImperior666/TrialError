"""Tests for the statusLine plan-quota feed: capture script
(trialerror/obs/statusline_capture.py, run as a bare file the way Claude Code
invokes it) and read side (trialerror.budget.quota)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
