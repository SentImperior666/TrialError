"""``trialerror probes run|status`` (design Section 3.1/3.3). ``--live`` is
tested ONLY against a mocked ``subprocess.run`` -- it must never actually be
invoked here (it spends model tokens and is a human operator's to run).
"""

from __future__ import annotations

import argparse

import pytest

from trialerror.cli import probes as probes_cli
from trialerror.probes import registry as preg


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture(autouse=True)
def _clean_registry():
    snapshot = dict(preg._REGISTRY)
    yield
    preg.clear_registry()
    preg._REGISTRY.update(snapshot)


@pytest.fixture(autouse=True)
def _isolated_probes_dir(tmp_path, monkeypatch):
    """Never let a probe read the real machine-wide ~/.trialerror/probes/
    hook_events.jsonl -- discovered live during development: without this,
    probe_hook_payload_keys silently read the ambient real hook history
    instead of the test's fixture data."""
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(tmp_path / "probes"))
    # L8 part F (second fix step): these tests call _run_run/_run_status with
    # no --program-root, which falls back to find_program_root() -- a real
    # walk-up from cwd that must never land on the harness's own checkout.
    monkeypatch.setenv("TRIALERROR_PROGRAM_ROOT", str(tmp_path / "program"))


@pytest.fixture()
def platform_root(tmp_path):
    return tmp_path / "platform"


def test_discovery_never_imports_or_reloads_the_cli_probes_module():
    """S-4: trialerror.cli has its own trialerror/cli/probes.py (the CLI
    GROUP module, not a probe-definition module). Discovery must never
    import or reload it -- doing so re-executes the module and rebinds
    every name in it, undoing any monkeypatch a caller applied first."""
    imported = preg.discover_and_register_probes()
    assert "trialerror.cli.probes" not in imported


def test_a_patch_on_detect_cc_version_survives_run_run(platform_root, monkeypatch):
    """S-4 regression: this is the exact shape of the previous round's
    "the patch did not take effect" anomaly. Before the fix, _run_run's own
    call to discover_and_register_probes() reloaded trialerror.cli.probes
    in place and rebound detect_cc_version to a fresh real function,
    silently discarding this monkeypatch."""
    sentinel_calls = []

    def _fake_detect_cc_version():
        sentinel_calls.append(1)
        return "SENTINEL-VERSION"

    monkeypatch.setattr(probes_cli, "detect_cc_version", _fake_detect_cc_version)
    args = _ns(platform_root=str(platform_root), kind="conformance", names=["cc_version_seen"], host="dev", live=False, live_model=None)
    env = probes_cli._run_run(args)
    assert sentinel_calls, "the patched detect_cc_version was never called -- discovery replaced it"
    assert env["result"]["probes"][0]["cc_version"] == "SENTINEL-VERSION"


def test_run_discovers_and_runs_registered_probes(platform_root, monkeypatch):
    monkeypatch.setattr(probes_cli, "detect_cc_version", lambda: "2.1.280")
    args = _ns(platform_root=str(platform_root), kind="conformance", names=None, host="dev", live=False, live_model=None)
    env = probes_cli._run_run(args)
    assert env["ok"] is True
    names = {p["name"] for p in env["result"]["probes"]}
    # discovered from trialerror/units/probes.py without this test naming it
    assert "cc_version_seen" in names


def test_run_with_a_failing_probe_is_an_error_envelope(platform_root, monkeypatch):
    monkeypatch.setattr(probes_cli, "detect_cc_version", lambda: "2.1.280")

    @preg.register_probe("always_fails", kind="conformance", timeout_s=1.0)
    def _f(ctx):
        return preg.ProbeResult(status="fail", detail={})

    args = _ns(platform_root=str(platform_root), kind=None, names=["always_fails"], host="dev", live=False, live_model=None)
    env = probes_cli._run_run(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "probe_failed"


def test_status_reports_the_latest_run_with_an_age(platform_root, monkeypatch):
    monkeypatch.setattr(probes_cli, "detect_cc_version", lambda: "2.1.280")
    run_args = _ns(platform_root=str(platform_root), kind=None, names=None, host="dev", live=False, live_model=None)
    probes_cli._run_run(run_args)

    status_args = _ns(platform_root=str(platform_root), host="dev")
    env = probes_cli._run_status(status_args)
    assert env["ok"] is True
    assert env["result"]["probes"]
    for row in env["result"]["probes"]:
        assert "ago" in row["age"] or row["age"] == "unknown"


def test_status_with_no_prior_runs_is_empty(platform_root):
    env = probes_cli._run_status(_ns(platform_root=str(platform_root), host="dev"))
    assert env["ok"] is True
    assert env["result"]["probes"] == []


# ---------------------------------------------------------------------------
# --live -- mocked subprocess ONLY, never actually invoked
# ---------------------------------------------------------------------------


def test_live_capture_command_shape():
    cmd = probes_cli.live_capture_command("claude-haiku-4-5-20251001")
    assert cmd[0] == "claude"
    assert "--model" in cmd
    assert "claude-haiku-4-5-20251001" in cmd
    assert "Agent" in cmd[-1]


def test_run_live_capture_refuses_by_default_and_never_calls_subprocess(monkeypatch, tmp_path):
    """The safety interlock (module docstring): without the allow-env set,
    run_live_capture must return an error and must NEVER reach
    subprocess.run at all -- proven here by making subprocess.run raise if
    it's ever called."""
    import subprocess as subprocess_module

    monkeypatch.delenv(probes_cli.LIVE_CAPTURE_ALLOW_ENV, raising=False)

    def _must_not_be_called(cmd, **kwargs):
        raise AssertionError("subprocess.run must not be called without the allow-env set")

    monkeypatch.setattr(subprocess_module, "run", _must_not_be_called)
    result = probes_cli.run_live_capture(model="claude-haiku-4-5-20251001", cwd=tmp_path)
    assert "error" in result
    assert probes_cli.LIVE_CAPTURE_ALLOW_ENV in result["error"]


def test_run_live_capture_prints_the_required_warning_and_parses_session_id(monkeypatch, capsys, tmp_path):
    import subprocess as subprocess_module

    monkeypatch.setenv(probes_cli.LIVE_CAPTURE_ALLOW_ENV, "1")

    class _FakeCompleted:
        returncode = 0
        stdout = '{"session_id": "SESS-LIVE-1"}'
        stderr = ""

    def _fake_run(cmd, **kwargs):
        return _FakeCompleted()

    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    result = probes_cli.run_live_capture(model="claude-haiku-4-5-20251001", cwd=tmp_path)
    assert result["ok"] is True
    assert result["session_id"] == "SESS-LIVE-1"
    assert probes_cli.LIVE_WARNING in capsys.readouterr().out


def test_run_live_capture_reports_an_error_without_raising(monkeypatch, tmp_path):
    import subprocess as subprocess_module

    monkeypatch.setenv(probes_cli.LIVE_CAPTURE_ALLOW_ENV, "1")

    def _fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    result = probes_cli.run_live_capture(model="x", cwd=tmp_path)
    assert "error" in result


def test_run_with_live_never_shells_out_for_real(platform_root, monkeypatch):
    """The one place this test suite exercises `--live` through the CLI:
    the allow-env is deliberately left UNSET, so even if every mock in this
    file were wrong, the real subprocess call still could not happen."""
    monkeypatch.setattr(probes_cli, "detect_cc_version", lambda: "2.1.280")
    monkeypatch.delenv(probes_cli.LIVE_CAPTURE_ALLOW_ENV, raising=False)
    args = _ns(
        platform_root=str(platform_root), kind="conformance", names=None, host="dev",
        live=True, live_model="claude-haiku-4-5-20251001",
    )
    env = probes_cli._run_run(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "live_capture_failed"
