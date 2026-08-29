"""M12's doctor checks: ``obs_exporter_reachable``, ``obs_span_drop_
counter``. Mirrors ``tests/test_jobs_checks.py``'s auto-discovery +
planted-fixture convention."""

from __future__ import annotations

import socket

import pytest

from trialerror.obs import state, tracer
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

pytest.importorskip("opentelemetry.sdk.trace")


@pytest.fixture(autouse=True)
def _reset():
    tracer.reset_for_tests()
    state.reset_for_tests()
    yield
    tracer.reset_for_tests()
    state.reset_for_tests()


def _run(names, program_root=None):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    results = run_checks(ctx, only=names)
    return {r.name: r for r in results}


def test_obs_checks_are_auto_discovered_without_import():
    from trialerror.util.doctor import clear_registry, registered_checks

    clear_registry()
    discover_and_register_checks()
    names = set(registered_checks())
    assert {"obs_exporter_reachable", "obs_span_drop_counter"} <= names


def test_exporter_reachable_passes_against_a_real_open_local_socket(monkeypatch):
    # A doctor check runs in its OWN process (see trialerror.obs.state's module
    # docstring for why the whole obs/ doctor surface is designed around
    # that) -- the env var, not an in-process tracer.configure() call, is
    # the real cross-process way to steer which endpoint it probes (same
    # override convention as TRIALERROR_PLATFORM_ROOT).
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        monkeypatch.setenv(tracer.ENV_ENDPOINT, f"http://127.0.0.1:{port}/v1/traces")
        result = _run(["obs_exporter_reachable"])["obs_exporter_reachable"]
        assert result.status == "pass"
        assert str(port) in result.details["endpoint"]
    finally:
        srv.close()


def test_exporter_reachable_warns_when_endpoint_is_unreachable(monkeypatch):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listening now
    monkeypatch.setenv(tracer.ENV_ENDPOINT, f"http://127.0.0.1:{port}/v1/traces")
    result = _run(["obs_exporter_reachable"])["obs_exporter_reachable"]
    assert result.status == "warn"


def test_span_drop_counter_skips_when_program_root_absent():
    result = _run(["obs_span_drop_counter"], program_root=None)["obs_span_drop_counter"]
    assert result.status == "skip"


def test_span_drop_counter_skips_when_no_obs_state_dir_yet(tmp_path):
    result = _run(["obs_span_drop_counter"], program_root=tmp_path)["obs_span_drop_counter"]
    assert result.status == "skip"


def test_span_drop_counter_passes_when_zero_drops_recorded(tmp_path):
    (tmp_path / "obs").mkdir()
    result = _run(["obs_span_drop_counter"], program_root=tmp_path)["obs_span_drop_counter"]
    assert result.status == "pass"


def test_span_drop_counter_warns_on_a_planted_drop(tmp_path):
    state.record_span_drop(tmp_path, count=2, reason="planted for test")
    result = _run(["obs_span_drop_counter"], program_root=tmp_path)["obs_span_drop_counter"]
    assert result.status == "warn"
    assert result.details["count"] == 2
    assert result.details["last_reason"] == "planted for test"


# ---------------------------------------------------------------------------
# disk_growth (O-1)
# ---------------------------------------------------------------------------


def test_disk_growth_is_auto_discovered_without_import():
    from trialerror.util.doctor import clear_registry, registered_checks

    clear_registry()
    discover_and_register_checks()
    assert "disk_growth" in set(registered_checks())


def test_disk_growth_passes_when_nothing_present(tmp_path, platform_root):
    # platform_root fixture isolates TRIALERROR_PLATFORM_ROOT -- disk_growth
    # always checks phoenix_serve.log there too, and must never leak onto
    # the real developer machine's ~/.trialerror.
    result = _run(["disk_growth"], program_root=tmp_path / "program")["disk_growth"]
    assert result.status == "pass"
    assert result.details["surfaces"]["jobs_logs"]["size_mb"] == 0
    assert result.details["surfaces"]["jobs_work"]["size_mb"] == 0
    assert result.details["surfaces"]["phoenix_serve_log"]["exists"] is False


def test_disk_growth_never_skips_even_with_no_program_root(platform_root):
    """Unlike the two OTel-specific checks above, disk_growth always has
    SOMETHING to report (at minimum, the platform-scoped phoenix_serve.log
    path) -- no program_root is not a reason to skip."""
    result = _run(["disk_growth"], program_root=None)["disk_growth"]
    assert result.status == "pass"
    assert "jobs_logs" not in result.details["surfaces"]
    assert "phoenix_serve_log" in result.details["surfaces"]


def test_disk_growth_warns_when_a_surface_exceeds_the_configured_threshold(tmp_path, platform_root):
    program_root = tmp_path / "program"
    (program_root / "jobs_logs").mkdir(parents=True)
    (program_root / "jobs_logs" / "big.log").write_bytes(b"x" * 2048)
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "t"\n\n[doctor]\ndisk_warn_mb = 0.001\n', encoding="utf-8"
    )

    result = _run(["disk_growth"], program_root=program_root)["disk_growth"]
    assert result.status == "warn"
    assert "jobs_logs" in result.message
    assert result.details["warn_threshold_mb"] == 0.001
    assert result.details["surfaces"]["jobs_logs"]["size_mb"] > 0.001


def test_disk_growth_uses_sane_default_threshold_when_unconfigured(tmp_path, platform_root):
    program_root = tmp_path / "program"
    program_root.mkdir()
    result = _run(["disk_growth"], program_root=program_root)["disk_growth"]
    from trialerror.obs.checks import _DEFAULT_DISK_WARN_MB

    assert result.details["warn_threshold_mb"] == _DEFAULT_DISK_WARN_MB


def test_disk_growth_counts_phoenix_serve_log_against_platform_root(tmp_path):
    """phoenix_serve.log is platform-scoped (trialerror obs start-phoenix's own
    ``<platform_root>/obs/phoenix_serve.log``), not per-program."""
    platform_root = tmp_path / "platform"
    (platform_root / "obs").mkdir(parents=True)
    (platform_root / "obs" / "phoenix_serve.log").write_bytes(b"x" * 2048)

    from trialerror.util.doctor import DoctorContext, run_checks

    discover_and_register_checks()
    ctx = DoctorContext(program_root=None, platform_root=platform_root)
    result = {r.name: r for r in run_checks(ctx, only=["disk_growth"])}["disk_growth"]
    assert result.details["surfaces"]["phoenix_serve_log"]["exists"] is True
    assert result.details["surfaces"]["phoenix_serve_log"]["size_mb"] > 0
    assert platform_root.as_posix() in result.details["surfaces"]["phoenix_serve_log"]["path"].replace("\\", "/")
