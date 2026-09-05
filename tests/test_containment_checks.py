"""Lane L0-F: the ``mass_deletion`` doctor check and its dashboard item
source. Mirrors ``tests/test_budget_checks.py``'s
``monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", ...)`` convention for
exercising ``trialerror.stores.paths.platform_root()``'s fallback, and
``tests/test_jobs_checks.py``'s auto-discovery + planted-fixture
convention.
"""

from __future__ import annotations

import json

from trialerror.containment.checks import FLAG_FILENAME, MANIFEST_FILENAME, PREV_MANIFEST_FILENAME, check_mass_deletion
from trialerror.containment.dashboard_items import mass_deletion_items
from trialerror.dashboard.store_ro import RoStore
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks


def _make_workspace(tmp_path, counts: dict[str, int] | None = None):
    """A workspace root with ``platform/`` plus however many throwaway
    files under each of the three watched paths ``counts`` (default: 5
    each) asks for. Returns ``(workspace_root, platform_root)``."""
    counts = counts if counts is not None else {"origin-project/research": 5, "origin-project/curriculum": 5, "origin-project-program/stores": 5}
    workspace_root = tmp_path / "workspace"
    platform_root = workspace_root / "platform"
    platform_root.mkdir(parents=True)
    for rel, n in counts.items():
        d = workspace_root / rel
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (d / f"f{i}.txt").write_text(f"content {i}", encoding="utf-8")
    return workspace_root, platform_root


def _write_manifest(platform_root, counts: dict[str, int], store_files: list[str] | None = None, snapshot_ts="20260905T000000Z"):
    manifest = {
        "schema": 1,
        "snapshot_ts": snapshot_ts,
        "watched_paths": {rel: {"file_count": n, "total_bytes": n * 10} for rel, n in counts.items()},
        "store_files": store_files or [],
    }
    (platform_root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _run(ctx):
    clear_registry()
    discover_and_register_checks()
    return run_checks(ctx, only=["mass_deletion"])[0]


def test_mass_deletion_is_auto_discovered_without_import():
    from trialerror.util.doctor import registered_checks

    clear_registry()
    discover_and_register_checks()
    assert "mass_deletion" in registered_checks()


def test_skips_when_no_snapshot_manifest_yet_and_workspace_is_genuinely_fresh(tmp_path, monkeypatch):
    """L0F-02(a): `skip` is now reserved for a workspace with NEITHER a
    prior manifest NOR any content under the watched paths -- a genuinely
    fresh scratch program, not a sandbox whose manifest just vanished."""
    workspace_root, platform_root = _make_workspace(
        tmp_path, {"origin-project/research": 0, "origin-project/curriculum": 0, "origin-project-program/stores": 0}
    )
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    result = _run(DoctorContext())
    assert result.status == "skip"
    assert not (platform_root / FLAG_FILENAME).exists()


def test_fails_when_manifest_missing_but_watched_paths_are_nonempty(tmp_path, monkeypatch):
    """L0F-02(a): the actual bug this closes -- `rm -rf /workspace/*`
    deletes the manifest ALONG WITH the watched-path content; before this
    fix that combination returned `skip` (silence), not `fail` (alarm)."""
    workspace_root, platform_root = _make_workspace(tmp_path, {"origin-project/research": 5, "origin-project/curriculum": 0, "origin-project-program/stores": 0})
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())

    assert result.status == "fail"
    assert "manifest" in result.message.lower()
    assert (platform_root / FLAG_FILENAME).is_file()
    assert result.details["watched_path_live_counts"]["origin-project/research"] == 5


def test_fails_when_manifest_missing_but_a_prev_manifest_exists(tmp_path, monkeypatch):
    """L0F-02(a): a `.prev.json` sibling with no current manifest is
    exactly what deleting the live manifest by hand looks like -- fail,
    even though every watched path happens to be empty in this fixture."""
    workspace_root, platform_root = _make_workspace(
        tmp_path, {"origin-project/research": 0, "origin-project/curriculum": 0, "origin-project-program/stores": 0}
    )
    (platform_root / PREV_MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())

    assert result.status == "fail"
    assert result.details["prev_manifest_exists"] is True
    assert (platform_root / FLAG_FILENAME).is_file()


def test_fails_closed_on_corrupt_manifest_json(tmp_path, monkeypatch):
    """L0F-11: the corrupt/unreadable-manifest branch is the one place
    this check already fails CLOSED (not skip) -- pin it with a test."""
    workspace_root, platform_root = _make_workspace(tmp_path)
    (platform_root / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())

    assert result.status == "fail"
    assert "unreadable" in result.message.lower() or "corrupt" in result.message.lower()
    # the corrupt-manifest branch returns before any flag write is attempted
    assert not (platform_root / FLAG_FILENAME).exists()


def test_flag_write_error_is_folded_into_message_not_raised(tmp_path, monkeypatch):
    """L0F-11: `_write_flag`'s OSError path must degrade the result, never
    raise -- one broken check must not crash the whole doctor run."""
    from pathlib import Path

    counts = {"origin-project/research": 90, "origin-project/curriculum": 5, "origin-project-program/stores": 5}
    workspace_root, platform_root = _make_workspace(tmp_path, counts)
    _write_manifest(platform_root, {"origin-project/research": 100, "origin-project/curriculum": 5, "origin-project-program/stores": 5})
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    real_write_text = Path.write_text

    def _boom(self, *args, **kwargs):
        if self.name == FLAG_FILENAME:
            raise OSError("simulated: read-only filesystem")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _boom)

    result = _run(DoctorContext())

    assert result.status == "fail"
    assert "flag_write_error" in result.details
    assert "WARNING" in result.message


def test_passes_when_live_counts_match_manifest_and_clears_a_stale_flag(tmp_path, monkeypatch):
    counts = {"origin-project/research": 5, "origin-project/curriculum": 5, "origin-project-program/stores": 5}
    workspace_root, platform_root = _make_workspace(tmp_path, counts)
    _write_manifest(platform_root, counts)
    # plant a stale flag from a previous failing run -- a clean run must
    # remove it, not just fail to add to it.
    (platform_root / FLAG_FILENAME).write_text("stale", encoding="utf-8")
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())

    assert result.status == "pass"
    assert not (platform_root / FLAG_FILENAME).exists()


def test_passes_when_files_were_only_added(tmp_path, monkeypatch):
    """More files than the manifest recorded is not a drop -- design
    section 14 only cares about disappearance."""
    workspace_root, platform_root = _make_workspace(tmp_path, {"origin-project/research": 8, "origin-project/curriculum": 5, "origin-project-program/stores": 5})
    _write_manifest(platform_root, {"origin-project/research": 5, "origin-project/curriculum": 5, "origin-project-program/stores": 5})
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())
    assert result.status == "pass"


def test_passes_at_exactly_the_one_percent_boundary(tmp_path, monkeypatch):
    """100 -> 99 files is exactly 1% -- design's wording ('greater than
    1%') is strict, so this must still pass."""
    workspace_root, platform_root = _make_workspace(tmp_path, {"origin-project/research": 99, "origin-project/curriculum": 0, "origin-project-program/stores": 0})
    _write_manifest(platform_root, {"origin-project/research": 100, "origin-project/curriculum": 0, "origin-project-program/stores": 0})
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())
    assert result.status == "pass"
    assert result.details["watched_paths"]["origin-project/research"]["drop_fraction"] == 0.01


def test_fails_and_writes_flag_on_a_drop_over_one_percent(tmp_path, monkeypatch):
    workspace_root, platform_root = _make_workspace(tmp_path, {"origin-project/research": 90, "origin-project/curriculum": 5, "origin-project-program/stores": 5})
    _write_manifest(platform_root, {"origin-project/research": 100, "origin-project/curriculum": 5, "origin-project-program/stores": 5})
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())

    assert result.status == "fail"
    assert "origin-project/research" in result.message
    flag_path = platform_root / FLAG_FILENAME
    assert flag_path.is_file()
    assert result.details["watched_paths"]["origin-project/research"]["over_threshold"] is True
    assert result.details["watched_paths"]["origin-project/curriculum"]["over_threshold"] is False


def test_fails_on_missing_store_file_even_with_no_percentage_drop(tmp_path, monkeypatch):
    counts = {"origin-project/research": 5, "origin-project/curriculum": 5, "origin-project-program/stores": 1}
    workspace_root, platform_root = _make_workspace(tmp_path, counts)
    # manifest names a store file that was never created in the fixture --
    # a missing named store file fails regardless of aggregate percentages.
    _write_manifest(platform_root, counts, store_files=["origin-project-program/stores/knowledge.db"])
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())

    assert result.status == "fail"
    assert result.details["missing_store_files"] == ["origin-project-program/stores/knowledge.db"]
    assert (platform_root / FLAG_FILENAME).is_file()


def test_passes_when_named_store_file_is_present(tmp_path, monkeypatch):
    counts = {"origin-project/research": 5, "origin-project/curriculum": 5, "origin-project-program/stores": 1}
    workspace_root, platform_root = _make_workspace(tmp_path, counts)
    (workspace_root / "origin-project-program/stores/knowledge.db").write_text("db", encoding="utf-8")
    _write_manifest(platform_root, counts, store_files=["origin-project-program/stores/knowledge.db"])
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())
    assert result.status == "pass"


def test_deleted_directory_counts_as_a_full_drop_not_a_crash(tmp_path, monkeypatch):
    """A watched path deleted entirely (not just thinned out) must not
    crash the check -- `_count_dir` treats a missing directory as (0, 0),
    the same shape as 'everything in it was deleted'."""
    counts = {"origin-project/research": 5, "origin-project/curriculum": 5, "origin-project-program/stores": 5}
    workspace_root, platform_root = _make_workspace(tmp_path, counts)
    _write_manifest(platform_root, counts)
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    import shutil

    shutil.rmtree(workspace_root / "origin-project/curriculum")

    result = _run(DoctorContext())
    assert result.status == "fail"
    assert result.details["watched_paths"]["origin-project/curriculum"]["live_file_count"] == 0
    assert result.details["watched_paths"]["origin-project/curriculum"]["drop_fraction"] == 1.0


def test_explicit_platform_root_on_ctx_wins_over_env(tmp_path, monkeypatch):
    """fix-accept (C-0064) convention: an explicit ctx.platform_root must
    be honored ahead of TRIALERROR_PLATFORM_ROOT, exactly like
    trialerror.budget.checks."""
    counts = {"origin-project/research": 5, "origin-project/curriculum": 5, "origin-project-program/stores": 5}
    real_workspace, real_platform = _make_workspace(tmp_path / "real", counts)
    _write_manifest(real_platform, counts)

    decoy_workspace, decoy_platform = _make_workspace(tmp_path / "decoy", {"origin-project/research": 0, "origin-project/curriculum": 0, "origin-project-program/stores": 0})
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(decoy_platform))

    result = _run(DoctorContext(platform_root=real_platform))
    assert result.status == "pass"
    assert result.details["manifest_path"] == str(real_platform / MANIFEST_FILENAME)


# ---------------------------------------------------------------------------
# dashboard_items.mass_deletion_items
# ---------------------------------------------------------------------------


def _ro_store(platform_root):
    return RoStore(platform=None, ops=None, knowledge=None, jobs=None, program_root=None, platform_root=platform_root)


def test_dashboard_item_empty_when_no_flag(tmp_path):
    platform_root = tmp_path / "platform"
    platform_root.mkdir()
    assert mass_deletion_items(_ro_store(platform_root)) == []


def test_dashboard_item_present_and_blocking_when_flag_set(tmp_path):
    platform_root = tmp_path / "platform"
    platform_root.mkdir()
    (platform_root / FLAG_FILENAME).write_text("mass_deletion doctor check FAILED\n", encoding="utf-8")

    items = mass_deletion_items(_ro_store(platform_root))

    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "mass_deletion"
    assert item["blocking"] is True
    assert "FAILED" in item["detail"]


def test_dashboard_item_reflects_check_writing_the_flag(tmp_path, monkeypatch):
    """End-to-end within the test suite: a failing check's flag write is
    exactly what the dashboard item source reads back."""
    counts = {"origin-project/research": 90, "origin-project/curriculum": 5, "origin-project-program/stores": 5}
    workspace_root, platform_root = _make_workspace(tmp_path, counts)
    _write_manifest(platform_root, {"origin-project/research": 100, "origin-project/curriculum": 5, "origin-project-program/stores": 5})
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))

    result = _run(DoctorContext())
    assert result.status == "fail"

    items = mass_deletion_items(_ro_store(platform_root))
    assert len(items) == 1
