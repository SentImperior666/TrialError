"""Tests for ``trialerror.stores.paths``'s ``config``-aware helpers and
``trialerror.stores.store.open_store``'s best-effort auto-load of
``[paths].stores_dir`` — the import-design notes (internal, not in this export) Sec 5 knob #1
(C-0067(c)(i)): the ONE scaffold-output knob that has to be resolved
BEFORE a store can even be opened, so it's handled differently from the
other five (each resolved by its own owning subsystem, explicit ``config``
argument) -- see ``trialerror.stores.store.open_store``'s own module docstring.
"""

from __future__ import annotations

import sqlite3

from trialerror.stores import paths
from trialerror.stores.store import open_store


# ---------------------------------------------------------------------------
# trialerror.stores.paths -- pure path-resolution helpers
# ---------------------------------------------------------------------------


def test_program_store_dir_default_unconfigured(tmp_path):
    assert paths.program_store_dir(tmp_path) == tmp_path / "stores"
    assert paths.program_store_dir(tmp_path, None) == tmp_path / "stores"
    assert paths.program_store_dir(tmp_path, {}) == tmp_path / "stores"


def test_program_store_dir_relative_override(tmp_path):
    config = {"paths": {"stores_dir": "db"}}
    assert paths.program_store_dir(tmp_path, config) == tmp_path / "db"


def test_program_store_dir_absolute_override_ignores_program_root(tmp_path):
    external = tmp_path / "elsewhere" / "stores"
    config = {"paths": {"stores_dir": str(external)}}
    assert paths.program_store_dir(tmp_path / "program", config) == external


def test_db_path_helpers_thread_config_through(tmp_path):
    config = {"paths": {"stores_dir": "db"}}
    assert paths.knowledge_db_path(tmp_path, config) == tmp_path / "db" / "knowledge.db"
    assert paths.ops_db_path(tmp_path, config) == tmp_path / "db" / "ops.db"
    assert paths.jobs_db_path(tmp_path, config) == tmp_path / "db" / "jobs.db"


# ---------------------------------------------------------------------------
# open_store -- explicit config vs. best-effort auto-load
# ---------------------------------------------------------------------------


def test_open_store_default_location_unchanged_with_no_trialerror_toml(tmp_path, platform_root):
    """The overwhelming common case (no trialerror.toml at all -- most of this
    suite's own ``program_root`` fixture) must be byte-identical to before
    this knob existed."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    store = open_store(program_root, platform_root=platform_root)
    try:
        assert (program_root / "stores" / "ops.db").is_file()
    finally:
        store.close()


def test_open_store_default_location_unchanged_with_trialerror_toml_but_no_paths_table(tmp_path, platform_root):
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "demo"\n', encoding="utf-8")
    store = open_store(program_root, platform_root=platform_root)
    try:
        assert (program_root / "stores" / "ops.db").is_file()
    finally:
        store.close()


def test_open_store_auto_loads_stores_dir_from_trialerror_toml(tmp_path, platform_root):
    """No explicit ``config=`` passed -- ``open_store`` discovers
    ``[paths].stores_dir`` from ``<program_root>/trialerror.toml`` on its own,
    the same "ambient, no caller opt-in" spirit ``TRIALERROR_PLATFORM_ROOT``
    already has (module docstring: this is what lets every CLI group open
    the RIGHT stores location without each of them individually loading
    and threading a config dict through)."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n\n[paths]\nstores_dir = "external-db"\n', encoding="utf-8"
    )
    store = open_store(program_root, platform_root=platform_root)
    try:
        assert (program_root / "external-db" / "ops.db").is_file()
        assert (program_root / "external-db" / "knowledge.db").is_file()
        assert (program_root / "external-db" / "jobs.db").is_file()
        assert not (program_root / "stores").exists()
    finally:
        store.close()


def test_open_store_auto_loads_absolute_stores_dir_outside_program_root(tmp_path, platform_root):
    program_root = tmp_path / "program"
    program_root.mkdir()
    external = tmp_path / "external-stores"
    (program_root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n\n[paths]\nstores_dir = {external.as_posix()!r}\n', encoding="utf-8"
    )
    store = open_store(program_root, platform_root=platform_root)
    try:
        assert (external / "ops.db").is_file()
        assert not (program_root / "stores").exists()
    finally:
        store.close()


def test_open_store_explicit_config_wins_over_auto_load(tmp_path, platform_root):
    """A caller that already loaded ``trialerror.toml`` for its own purposes and
    passes ``config=`` explicitly is never second-guessed by the
    auto-loader (``config is None`` is the only trigger)."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n\n[paths]\nstores_dir = "from-toml"\n', encoding="utf-8"
    )
    store = open_store(program_root, platform_root=platform_root, config={"paths": {"stores_dir": "from-caller"}})
    try:
        assert (program_root / "from-caller" / "ops.db").is_file()
        assert not (program_root / "from-toml").exists()
    finally:
        store.close()


def test_open_store_malformed_trialerror_toml_falls_back_to_default(tmp_path, platform_root):
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text("[program\nbroken toml", encoding="utf-8")
    store = open_store(program_root, platform_root=platform_root)
    try:
        assert (program_root / "stores" / "ops.db").is_file()
    finally:
        store.close()


def test_open_store_relocated_stores_dir_still_migrates_all_three_dbs(tmp_path, platform_root):
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n\n[paths]\nstores_dir = "db"\n', encoding="utf-8"
    )
    store = open_store(program_root, platform_root=platform_root)
    try:
        for name in ("ops.db", "knowledge.db", "jobs.db"):
            conn = sqlite3.connect(program_root / "db" / name)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                assert version > 0, f"{name} migration did not run at the relocated stores_dir"
            finally:
                conn.close()
    finally:
        store.close()
