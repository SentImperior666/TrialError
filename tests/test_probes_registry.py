"""``trialerror.probes.registry`` -- the decorator registry and time-bounded
runner (design Section 3.1). Each test clears/restores the registry so it
never leaks a probe into another test module's discovery pass."""

from __future__ import annotations

import json
import time

import pytest

from trialerror.probes import registry as preg
from trialerror.stores.connection import connect
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform as platform_schema
from trialerror.stores.store import Store


@pytest.fixture(autouse=True)
def _clean_registry():
    snapshot = dict(preg._REGISTRY)
    preg.clear_registry()
    yield
    preg.clear_registry()
    preg._REGISTRY.update(snapshot)


@pytest.fixture(autouse=True)
def _isolated_probes_dir(tmp_path, monkeypatch):
    """Defensive isolation: nothing in this file should read a real probe
    log, but see tests/test_probes_cli.py's own note on what happens
    without it."""
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(tmp_path / "probes"))


def _memory_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def platform_store(tmp_path):
    conn = connect(tmp_path / "platform.db", check_same_thread=False)
    apply_migrations(conn, platform_schema.MIGRATIONS)
    s = Store(platform=conn, ops=_memory_conn(), knowledge=_memory_conn(), jobs=_memory_conn(),
              program_root=tmp_path, platform_root=tmp_path)
    yield s
    s.close()


def _rows(store):
    return [dict(r) for r in store.platform.execute("SELECT * FROM probe_run ORDER BY id").fetchall()]


def test_register_and_run_a_passing_probe(platform_store):
    @preg.register_probe("ok_probe", kind="conformance", timeout_s=1.0)
    def _ok(ctx):
        return preg.ProbeResult(status="pass", detail={"n": 1})

    ctx = preg.ProbeContext(host="h", platform_store=platform_store)
    results = preg.run_probes(ctx)
    assert len(results) == 1
    assert results[0].status == "pass"
    rows = _rows(platform_store)
    assert rows[0]["status"] == "pass"
    assert json.loads(rows[0]["detail"]) == {"n": 1}
    assert rows[0]["build"]


def test_a_probe_that_overruns_its_timeout_is_recorded_as_error(platform_store):
    @preg.register_probe("slow_probe", kind="conformance", timeout_s=0.05)
    def _slow(ctx):
        time.sleep(2)
        return preg.ProbeResult(status="pass")

    ctx = preg.ProbeContext(host="h", platform_store=platform_store)
    results = preg.run_probes(ctx)
    assert results[0].status == "error"
    assert results[0].detail["reason"] == "timeout"
    rows = _rows(platform_store)
    assert rows[0]["status"] == "error"


def test_an_exception_is_recorded_as_error_and_other_probes_still_run(platform_store):
    @preg.register_probe("boom", kind="conformance", timeout_s=1.0)
    def _boom(ctx):
        raise RuntimeError("kaboom")

    @preg.register_probe("fine", kind="conformance", timeout_s=1.0)
    def _fine(ctx):
        return preg.ProbeResult(status="pass")

    ctx = preg.ProbeContext(host="h", platform_store=platform_store)
    results = {r.name: r for r in preg.run_probes(ctx)}
    assert results["boom"].status == "error"
    assert "kaboom" in results["boom"].detail["reason"]
    assert results["fine"].status == "pass"


def test_kind_filter(platform_store):
    @preg.register_probe("a_conf", kind="conformance", timeout_s=1.0)
    def _a(ctx):
        return preg.ProbeResult(status="pass")

    @preg.register_probe("a_canary", kind="canary", timeout_s=1.0)
    def _b(ctx):
        return preg.ProbeResult(status="pass")

    ctx = preg.ProbeContext(host="h", platform_store=platform_store)
    results = preg.run_probes(ctx, kind="canary")
    assert [r.name for r in results] == ["a_canary"]


def test_names_filter(platform_store):
    @preg.register_probe("one", kind="conformance", timeout_s=1.0)
    def _one(ctx):
        return preg.ProbeResult(status="pass")

    @preg.register_probe("two", kind="conformance", timeout_s=1.0)
    def _two(ctx):
        return preg.ProbeResult(status="pass")

    ctx = preg.ProbeContext(host="h", platform_store=platform_store)
    results = preg.run_probes(ctx, names=["two"])
    assert [r.name for r in results] == ["two"]


def test_no_platform_store_means_no_rows_written():
    @preg.register_probe("p", kind="conformance", timeout_s=1.0)
    def _p(ctx):
        return preg.ProbeResult(status="pass")

    ctx = preg.ProbeContext(host="h", platform_store=None)
    results = preg.run_probes(ctx)
    assert results[0].status == "pass"


def test_discovery_imports_a_real_probes_module():
    imported = preg.discover_and_register_probes()
    assert any(name.endswith(".probes") for name in imported)
    assert preg.registered_probes()  # at least one probe registered somewhere


def test_discovery_never_imports_the_cli_or_probes_subpackages():
    """S-4: neither is a probe-definition home. trialerror.cli in
    particular has its own trialerror/cli/probes.py (the `probes` CLI
    group), which discovery must never import or reload."""
    imported = preg.discover_and_register_probes()
    assert "trialerror.cli.probes" not in imported
    assert "trialerror.probes.probes" not in imported


# ---------------------------------------------------------------------------
# run_probe_inline (B-1/S-1: a caller with its own timeout, no probe thread)
# ---------------------------------------------------------------------------


def test_run_probe_inline_runs_on_the_calling_thread_and_records_a_row(platform_store):
    seen_thread_ident = []

    @preg.register_probe("inline_ok", kind="canary", timeout_s=1.0)
    def _ok(ctx):
        import threading

        seen_thread_ident.append(threading.current_thread().ident)
        return preg.ProbeResult(status="pass", detail={"n": 1})

    import threading

    ctx = preg.ProbeContext(host="h", platform_store=platform_store, program_id="PROG-A")
    row = preg.run_probe_inline(ctx, "inline_ok")
    assert row.status == "pass"
    assert row.program_id == "PROG-A"
    assert seen_thread_ident == [threading.current_thread().ident]
    rows = _rows(platform_store)
    assert rows[0]["name"] == "inline_ok"
    assert rows[0]["program_id"] == "PROG-A"


def test_run_probe_inline_isolates_an_exception_as_error(platform_store):
    @preg.register_probe("inline_boom", kind="canary", timeout_s=1.0)
    def _boom(ctx):
        raise RuntimeError("kaboom")

    ctx = preg.ProbeContext(host="h", platform_store=platform_store)
    row = preg.run_probe_inline(ctx, "inline_boom")
    assert row.status == "error"
    assert "kaboom" in row.detail["reason"]


def test_run_probe_inline_raises_for_an_unregistered_name(platform_store):
    ctx = preg.ProbeContext(host="h", platform_store=platform_store)
    with pytest.raises(KeyError):
        preg.run_probe_inline(ctx, "no_such_probe")


def test_probe_result_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        preg.ProbeResult(status="bogus")


def test_register_probe_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        preg.register_probe("x", kind="bogus", timeout_s=1.0)
