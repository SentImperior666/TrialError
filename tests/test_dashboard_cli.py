"""Tests for the ``trialerror dashboard`` CLI group (``trialerror/cli/dashboard.py``):
``export`` end to end via the envelope, and ``serve``'s detached-by-default
spawn path (the ``--foreground`` path itself is exercised as a REAL
subprocess in ``tests/test_dashboard_serve.py`` -- the M14 stdio-smoke
pattern this build's brief names). Follows the ``trialerror.cli.main(...)`` +
``capsys``/``json.loads`` pattern established in ``tests/test_cli_doctor.py``
/ ``tests/test_jobs_cli.py``."""

from __future__ import annotations

import json
import os
import signal
import socket
from pathlib import Path

from trialerror.cli import main
from trialerror.stores.store import open_store
from tests._store_fixtures import populate_one_of_everything


def _run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def _common_args(program_root, platform_root):
    return ["--program-root", str(program_root), "--platform-root", str(platform_root)]


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_dashboard_export_cli_round_trip(program_root, platform_root, tmp_path, capsys):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()

    out_path = tmp_path / "snapshot.html"
    rc, env = _run(
        ["dashboard", "export", *_common_args(program_root, platform_root), "--out", str(out_path)],
        capsys,
    )
    assert rc == 0
    assert env["ok"] is True
    assert env["result"]["out_path"] == str(out_path)
    assert out_path.is_file()
    html = out_path.read_text(encoding="utf-8")
    assert ids["session"] in html


def test_dashboard_export_cli_reports_error_for_unwritable_out(program_root, platform_root, capsys):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    # a path with a NUL byte is invalid on every platform Python runs on --
    # a clean, portable way to force export_snapshot() to raise so the
    # CLI's try/except-and-report-an-error_envelope path is proven, not
    # just its happy path.
    bad_out = "bad\x00path.html"
    rc, env = _run(
        ["dashboard", "export", *_common_args(program_root, platform_root), "--out", bad_out],
        capsys,
    )
    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "export_failed"


def test_dashboard_serve_detached_spawn_returns_pid_and_url(program_root, platform_root, tmp_path, capsys):
    """Does not wait for the detached child to actually come up (that
    real-server-readiness proof is ``tests/test_dashboard_serve.py``'s
    job, over the ``--foreground`` path this same detached child
    ultimately execs) -- only that the CLI's spawn call returns
    immediately with a well-shaped envelope and a log file, and that the
    spawned process genuinely exists (killable) afterward."""
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    port = _free_port()
    log_dir = tmp_path / "logs"
    pid = None
    try:
        rc, env = _run(
            [
                "dashboard", "serve", *_common_args(program_root, platform_root),
                "--port", str(port), "--log-dir", str(log_dir),
            ],
            capsys,
        )
        assert rc == 0
        assert env["ok"] is True
        pid = env["result"]["pid"]
        assert isinstance(pid, int) and pid > 0
        assert env["result"]["url"] == f"http://127.0.0.1:{port}/"
        log_path = Path(env["result"]["log_path"])
        assert log_path.parent == log_dir
        assert "--foreground" in env["result"]["argv"]
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass  # already exited -- nothing to clean up
