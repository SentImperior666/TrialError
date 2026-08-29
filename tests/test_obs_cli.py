"""Tests for the ``trialerror obs`` CLI group (``trialerror/cli/obs.py``): status,
start-phoenix, smoke. Follows the ``trialerror.cli.main(...)`` + ``capsys``/
``json.loads`` pattern established in ``tests/test_cli_doctor.py`` /
``tests/test_jobs_cli.py``. ``start-phoenix`` never launches a real
``phoenix serve`` here -- ``subprocess.Popen`` is monkeypatched so the test
suite never leaves an orphan detached process behind; the real launch is
covered by this build's manual live-Phoenix smoke (see the M12 report)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys

import pytest

from trialerror.cli import main
from trialerror.obs import state, tracer

pytest.importorskip("opentelemetry.sdk.trace")


@pytest.fixture()
def open_local_endpoint(monkeypatch):
    """Points TRIALERROR_OBS_OTLP_ENDPOINT at a real, listening loopback socket
    -- ``obs status``'s ``obs_exporter_reachable`` check must report
    ``pass`` against it regardless of whether a real Phoenix happens to be
    running on this dev machine's default :6006 (it may or may not be, and
    tests must not depend on that)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    monkeypatch.setenv(tracer.ENV_ENDPOINT, f"http://127.0.0.1:{port}/v1/traces")
    yield
    srv.close()


@pytest.fixture(autouse=True)
def _reset():
    tracer.reset_for_tests()
    state.reset_for_tests()
    yield
    tracer.reset_for_tests()
    state.reset_for_tests()


def _run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_obs_status_reports_otel_available(open_local_endpoint, capsys):
    rc, env = _run(["obs", "status"], capsys)
    assert rc == 0
    assert env["result"]["otel_available"] is True
    assert "endpoint" in env["result"]


def test_obs_status_warns_when_endpoint_unreachable(monkeypatch, capsys):
    monkeypatch.setenv(tracer.ENV_ENDPOINT, "http://127.0.0.1:1/v1/traces")  # port 1: refused
    rc, env = _run(["obs", "status"], capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "obs_degraded"


def test_obs_status_scopes_span_drop_counter_to_given_program_root(tmp_path, capsys):
    # Deliberately does NOT use open_local_endpoint: the drop-counter warn
    # alone must be what makes this an error envelope, proving the two obs
    # checks are independently wired into `obs status`'s aggregation, not
    # just the reachability one.
    state.record_span_drop(tmp_path, count=1, reason="planted")
    rc, env = _run(["obs", "status", "--program-root", str(tmp_path)], capsys)
    assert rc == 1
    assert env["error"]["details"]["checks"]["obs_span_drop_counter"]["status"] == "warn"


def test_obs_smoke_emits_four_spans_and_reports_flushed(tmp_path, capsys, monkeypatch):
    # Point at a closed port -- this proves the CLI round trip end to end
    # (configure -> emit 4 spans -> flush -> shutdown) without needing a
    # real collector; the live-Phoenix round trip is covered separately.
    monkeypatch.setenv(tracer.ENV_ENDPOINT, "http://127.0.0.1:1/v1/traces")
    rc, env = _run(["obs", "smoke", "--program-root", str(tmp_path)], capsys)
    assert rc == 0
    assert env["result"]["spans_emitted"] == 4
    assert env["result"]["launch_id"].startswith("LNCH")
    assert env["result"]["job_id"].startswith("JOB")


def test_obs_start_phoenix_spawns_detached_with_expected_argv(tmp_path, capsys, monkeypatch):
    # O-4's idempotent-start probe runs first now -- point at a guaranteed-
    # refused port (same "port 1: refused" convention
    # test_obs_status_warns_when_endpoint_unreachable uses) so the probe
    # reports "not reachable" regardless of whatever happens to be running
    # on this dev machine's real default :6006.
    monkeypatch.setenv(tracer.ENV_ENDPOINT, "http://127.0.0.1:1/v1/traces")
    captured = {}

    class _FakeProc:
        pid = 424242

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    rc, env = _run(["obs", "start-phoenix", "--platform-root", str(tmp_path)], capsys)

    assert rc == 0
    assert env["result"]["already_running"] is False
    assert env["result"]["pid"] == 424242
    assert env["result"]["url"] == "http://localhost:6006"
    assert env["result"]["message"]
    assert captured["argv"] == [sys.executable, "-m", "phoenix.server.main", "serve"]
    if sys.platform == "win32":
        assert captured["kwargs"]["creationflags"] & subprocess.DETACHED_PROCESS
        assert captured["kwargs"]["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    log_path = tmp_path / "obs" / "phoenix_serve.log"
    assert log_path.is_file()


def test_obs_start_phoenix_reports_a_structured_error_if_launch_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(tracer.ENV_ENDPOINT, "http://127.0.0.1:1/v1/traces")

    def _raise(*_a, **_kw):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "Popen", _raise)
    rc, env = _run(["obs", "start-phoenix", "--platform-root", str(tmp_path)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "phoenix_not_installed"


def test_obs_start_phoenix_skips_spawn_when_already_reachable(open_local_endpoint, tmp_path, capsys, monkeypatch):
    """O-4: a fake listener bound to the configured endpoint is enough to
    make `trialerror obs start-phoenix` refuse to spawn a second process --
    proven here by making a real spawn attempt an assertion failure."""

    def _poison_pill(*_a, **_kw):
        raise AssertionError("subprocess.Popen must not be called when the endpoint is already reachable")

    monkeypatch.setattr(subprocess, "Popen", _poison_pill)
    rc, env = _run(["obs", "start-phoenix", "--platform-root", str(tmp_path)], capsys)

    assert rc == 0, env
    assert env["result"]["already_running"] is True
    assert env["result"]["pid"] is None
    assert env["result"]["message"]
    assert not (tmp_path / "obs" / "phoenix_serve.log").exists()


def test_obs_group_default_handler_is_a_structured_no_subcommand_error():
    # The group's bare `run()` handler is only reachable if some future
    # caller invokes the group without going through argparse's own
    # `required=True` subparser enforcement (e.g. calling it directly, the
    # same convention tests/test_jobs_cli.py-style modules don't bother
    # covering via main() for the identical reason budget.py's own `run()`
    # exists) -- covered directly here for completeness.
    import argparse

    from trialerror.cli.obs import run

    env = run(argparse.Namespace())
    assert env["ok"] is False
    assert env["error"]["code"] == "no_subcommand"
