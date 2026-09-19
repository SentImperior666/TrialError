"""Acceptance criteria: "migration up from empty" and idempotency
(re-running the same migration list against an already-migrated DB is a
no-op).
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import Migration, apply_migrations, current_version, latest_version
from trialerror.stores.schema import jobs, knowledge, ops, platform


@pytest.fixture(params=[platform, ops, knowledge, jobs], ids=["platform", "ops", "knowledge", "jobs"])
def schema_module(request):
    return request.param


def test_migration_up_from_empty(schema_module):
    conn = sqlite3.connect(":memory:")
    assert current_version(conn) == 0
    applied = apply_migrations(conn, schema_module.MIGRATIONS)
    assert applied == [m.version for m in schema_module.MIGRATIONS]
    assert current_version(conn) == latest_version(schema_module.MIGRATIONS)

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    for table in schema_module.TABLES:
        assert table in tables


def test_migration_idempotent_reapply_is_noop(schema_module):
    conn = sqlite3.connect(":memory:")
    first = apply_migrations(conn, schema_module.MIGRATIONS)
    assert first  # something was applied the first time
    version_after_first = current_version(conn)

    second = apply_migrations(conn, schema_module.MIGRATIONS)
    assert second == []
    assert current_version(conn) == version_after_first


def test_migration_applies_only_newer_versions():
    conn = sqlite3.connect(":memory:")
    m1 = Migration(version=1, name="v1", statements=("CREATE TABLE t (id TEXT PRIMARY KEY)",))
    m2 = Migration(version=2, name="v2", statements=("ALTER TABLE t ADD COLUMN label TEXT",))

    applied1 = apply_migrations(conn, [m1])
    assert applied1 == [1]

    # re-run with BOTH migrations: only the newer one (2) should apply
    applied2 = apply_migrations(conn, [m1, m2])
    assert applied2 == [2]
    assert current_version(conn) == 2
    cols = {r[1] for r in conn.execute("PRAGMA table_info(t)").fetchall()}
    assert "label" in cols


def test_migration_rejects_duplicate_version_numbers():
    conn = sqlite3.connect(":memory:")
    m1 = Migration(version=1, name="a", statements=("CREATE TABLE a (id TEXT)",))
    m1_dup = Migration(version=1, name="b", statements=("CREATE TABLE b (id TEXT)",))
    with pytest.raises(MigrationError, match="duplicate migration version"):
        apply_migrations(conn, [m1, m1_dup])


def test_migration_failure_does_not_advance_version(tmp_path):
    conn = sqlite3.connect(":memory:")
    bad = Migration(version=1, name="broken", statements=("CREATE TABLE ok (id TEXT)", "THIS IS NOT SQL"))
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [bad])
    # the whole migration (including the first, valid statement) rolled back
    assert current_version(conn) == 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert "ok" not in tables


def test_migration_version_must_be_positive():
    with pytest.raises(MigrationError, match="version must be >= 1"):
        Migration(version=0, name="bad", statements=())
