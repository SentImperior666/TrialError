"""Tests for the ``trialerror jobs`` CLI group (``trialerror/cli/jobs.py``): list,
start-worker --foreground, tick, pause, resume, logs. Follows the
``trialerror.cli.main(...)`` + ``capsys``/``json.loads`` pattern established in
``tests/test_cli_doctor.py``."""

from __future__ import annotations

import json

from trialerror.cli import main
from trialerror.stores.store import open_store


def _run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def _common_args(program_root, platform_root):
    return ["--program-root", str(program_root), "--platform-root", str(platform_root)]


def test_jobs_list_empty(program_root, platform_root, capsys):
    rc, env = _run(["jobs", "list", *_common_args(program_root, platform_root)], capsys)
    assert rc == 0
    assert env["ok"] is True
    assert env["result"]["jobs"] == []


def test_jobs_start_worker_foreground_once_runs_noop_job(program_root, platform_root, capsys):
    rc, env = _run(
        [
            "jobs",
            "start-worker",
            *_common_args(program_root, platform_root),
            "--foreground",
            "--job-id",
            "JOB-cli1",
            "--kind",
            "custom",
            "--payload",
            json.dumps({"handler": "noop"}),
        ],
        capsys,
    )
    assert rc == 0
    assert env["ok"] is True
    assert env["result"]["status"] == "complete"

    rc, env = _run(["jobs", "list", *_common_args(program_root, platform_root), "--state", "complete"], capsys)
    assert rc == 0
    assert [j["job_id"] for j in env["result"]["jobs"]] == ["JOB-cli1"]


def test_jobs_start_worker_foreground_idle_when_queue_empty(program_root, platform_root, capsys):
    rc, env = _run(["jobs", "start-worker", *_common_args(program_root, platform_root), "--foreground"], capsys)
    assert rc == 0
    assert env["result"]["status"] == "idle"


def test_jobs_tick_reports_reclaimed_jobs(program_root, platform_root, capsys):
    store = open_store(program_root, platform_root=platform_root)
    try:
        from trialerror.jobs import ledger

        job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
        ledger.claim_next(store, worker_id="dead:ts1")
        with store.jobs:
            store.jobs.execute(
                "UPDATE job SET lease_expires_ts = '2000-01-01T00:00:00.000Z' WHERE job_id = ?", (job["job_id"],)
            )
    finally:
        store.close()

    rc, env = _run(["jobs", "tick", *_common_args(program_root, platform_root)], capsys)
    assert rc == 0
    assert env["result"]["count"] == 1
    assert env["result"]["reclaimed"][0]["job_id"] == job["job_id"]
    assert env["result"]["reclaimed"][0]["state"] == "pending"


def test_jobs_pause_and_resume_roundtrip_via_cli(program_root, platform_root, capsys):
    store = open_store(program_root, platform_root=platform_root)
    try:
        from trialerror.jobs import ledger

        job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    finally:
        store.close()

    rc, env = _run(["jobs", "pause", job["job_id"], *_common_args(program_root, platform_root)], capsys)
    assert rc == 0
    assert env["result"]["state"] == "paused"

    rc, env = _run(["jobs", "resume", job["job_id"], *_common_args(program_root, platform_root)], capsys)
    assert rc == 0
    assert env["result"]["state"] == "pending"
    assert env["nextActions"]  # relaunch suggestion is present


def test_jobs_pause_unknown_job_returns_error_envelope(program_root, platform_root, capsys):
    rc, env = _run(["jobs", "pause", "JOB-nonexistent", *_common_args(program_root, platform_root)], capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "JobNotFoundError"


def test_jobs_logs_returns_event_history(program_root, platform_root, capsys):
    _run(
        [
            "jobs",
            "start-worker",
            *_common_args(program_root, platform_root),
            "--foreground",
            "--job-id",
            "JOB-cli2",
            "--kind",
            "custom",
            "--payload",
            json.dumps({"handler": "noop"}),
        ],
        capsys,
    )
    rc, env = _run(["jobs", "logs", "JOB-cli2", *_common_args(program_root, platform_root)], capsys)
    assert rc == 0
    types = [e["type"] for e in env["result"]["events"]]
    assert types == ["enqueued", "claimed", "heartbeat", "completed"]


def test_jobs_no_program_root_errors_cleanly(tmp_path, capsys, monkeypatch):
    empty_cwd = tmp_path / "not_a_program"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    rc, env = _run(["jobs", "list"], capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "no_program_root"
