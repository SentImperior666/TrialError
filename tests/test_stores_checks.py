"""M1's doctor checks: ``store_schema_version``, ``xid_dangling``,
``anchors_dangling``. Auto-discovery (no import needed) plus planted-
fixture adversarial cases for each.
"""

from __future__ import annotations

from pathlib import Path

from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything


def _run(names, program_root):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    results = run_checks(ctx, only=names)
    return {r.name: r for r in results}


def test_platform_root_precedence_explicit_over_env_over_default(tmp_path, monkeypatch):
    """Regression for fix-accept (C-0064, task_c92b015f): DoctorContext had
    no ``platform_root`` field at all and ``_db_path``'s "platform" branch
    ignored ``ctx`` entirely, always re-deriving from ``TRIALERROR_PLATFORM_ROOT``/
    ``~/.trialerror`` -- which made `trialerror accept` false-positive ``xid_dangling``
    against a real machine's own ``~/.trialerror/platform.db`` even though the
    acceptance journey had resolved its own scratch ``platform_root``
    elsewhere. This test deliberately does NOT use the ``platform_root``
    fixture from ``tests/conftest.py`` (which sets ``TRIALERROR_PLATFORM_ROOT`` to
    the SAME directory it hands back, so an env-only resolution would
    coincidentally still match and mask this exact bug -- that masking is
    exactly how the bug shipped past the full test suite in the first
    place). Instead it points ``TRIALERROR_PLATFORM_ROOT`` at one directory and
    ``DoctorContext.platform_root`` at a DIFFERENT one, so the two can only
    agree if the explicit field actually wins.
    """
    from trialerror.stores.checks import _db_path
    from trialerror.util.doctor import DoctorContext

    env_root = tmp_path / "env_platform"
    explicit_root = tmp_path / "explicit_platform"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(env_root))

    # explicit ctx.platform_root wins over the env var.
    ctx_explicit = DoctorContext(platform_root=explicit_root)
    resolved = _db_path(ctx_explicit, "platform")
    assert resolved == explicit_root / "platform.db"
    assert resolved != env_root / "platform.db"

    # a caller that supplies no platform_root still falls back to the env
    # var (must not break every other test/check in this suite that relies
    # on TRIALERROR_PLATFORM_ROOT alone).
    ctx_env_only = DoctorContext()
    resolved_env_only = _db_path(ctx_env_only, "platform")
    assert resolved_env_only == env_root / "platform.db"

    # and with neither an explicit platform_root nor the env var set, the
    # ~/.trialerror default still applies (unchanged pre-fix behavior).
    monkeypatch.delenv("TRIALERROR_PLATFORM_ROOT", raising=False)
    ctx_default = DoctorContext()
    resolved_default = _db_path(ctx_default, "platform")
    assert resolved_default == Path.home() / ".trialerror" / "platform.db"


def test_checks_are_auto_discovered_without_import():
    clear_registry()
    discover_and_register_checks()
    from trialerror.util.doctor import registered_checks

    names = set(registered_checks())
    assert {"store_schema_version", "xid_dangling", "anchors_dangling"} <= names


def test_store_schema_version_passes_on_freshly_migrated_store(store, program_root, platform_root):
    populate_one_of_everything(store)
    results = _run(["store_schema_version"], program_root)
    r = results["store_schema_version"]
    assert r.status == "pass"
    # schema-v2 (build-v1-schemav2): the check's "expected" version comes
    # straight from trialerror.stores.migrate.latest_version(SCHEMA_MODULES[db].
    # MIGRATIONS) -- adding the v2 Migration to ops/jobs/knowledge's own
    # MIGRATIONS tuples is the entire "bump expected versions" step; no
    # doctor-check code change was needed. platform.db has no v2 migration.
    # ops (build-v2-polish's ops-v3, rooms; build-v2dash-data's ops-v4,
    # criterion + feed_post_translation) and knowledge (build-v2-summary's
    # knowledge-v3, the summary table) each independently gained more
    # versions later -- jobs.db has no v3 (still at 2).
    assert r.details["ops"] == {"current_version": 4, "expected_version": 4, "match": True}
    assert r.details["jobs"] == {"current_version": 2, "expected_version": 2, "match": True}
    assert r.details["knowledge"] == {"current_version": 3, "expected_version": 3, "match": True}
    assert r.details["platform"]["expected_version"] == 1


def test_store_schema_version_skips_when_db_absent(tmp_path, platform_root):
    """`platform_root` isolation is required here too (not just
    program_root) -- otherwise an uninitialized program would still resolve
    platform.db to the real developer's ~/.trialerror."""
    empty_program = tmp_path / "never_initialized"
    results = _run(["store_schema_version"], empty_program)
    r = results["store_schema_version"]
    for db_kind in ("platform", "ops", "knowledge", "jobs"):
        assert r.details[db_kind]["status"] == "skip"
    assert r.status == "pass"  # nothing to check yet is not a failure


def test_xid_dangling_passes_on_clean_store(store, program_root):
    populate_one_of_everything(store)
    results = _run(["xid_dangling"], program_root)
    assert results["xid_dangling"].status == "pass"
    assert results["xid_dangling"].details["offenders"] == {}


def test_xid_dangling_catches_planted_dangling_reference(store, program_root):
    populate_one_of_everything(store)
    # bypass the validated write API on purpose -- simulates a legacy
    # import / a target row deleted after the XID was written.
    with store.ops:
        store.ops.execute(
            "INSERT INTO thread(thread_id, title, created_ts, created_by_launch) VALUES (?,?,?,?)",
            (new_id("THR"), "planted dangling", now(), "LNCH-does-not-exist"),
        )
    results = _run(["xid_dangling"], program_root)
    r = results["xid_dangling"]
    assert r.status == "fail"
    assert any(k.startswith("thread.created_by_launch") for k in r.details["offenders"])


def test_anchors_dangling_passes_on_clean_store(store, program_root):
    populate_one_of_everything(store)
    results = _run(["anchors_dangling"], program_root)
    r = results["anchors_dangling"]
    assert r.status == "pass"
    assert r.details["doc_sha256_mismatches"] == 0


def test_anchors_dangling_catches_planted_stale_anchor(store, program_root):
    ids = populate_one_of_everything(store)
    # simulate a document re-normalization: bump document.sha256 without
    # touching the anchor stamped against the old hash.
    with store.knowledge:
        store.knowledge.execute(
            "UPDATE document SET sha256 = ? WHERE doc_id = ?", ("f" * 64, ids["document"])
        )
    results = _run(["anchors_dangling"], program_root)
    r = results["anchors_dangling"]
    assert r.status == "warn"  # staleness is informational, not a hard failure
    assert r.details["doc_sha256_mismatches"] == 1
