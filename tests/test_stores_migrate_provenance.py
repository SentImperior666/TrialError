"""Lane FB-acq item 5 (F17), the write half: the migration runner records
what it did.

``PRAGMA user_version`` is one integer. It says which version a store is on
and nothing else: not when it got there, not which client took it there, not
whether the step was additive. So an older client opening a newer store had no
way to tell a store that is AHEAD of it from a store that is broken, and no
migration anywhere left a trace beyond the pointer it moved.

This module pins the runner-owned ``schema_migration`` table: one row per
applied migration with a timestamp and the client that wrote it, a backfill
for stores whose runner predates the table, the mechanical additive/
non-additive classification, and the failure path (a migration that rolls back
takes its provenance row with it).

Nothing here touches a live store: every DB is an in-memory connection or a
file under ``tmp_path``.
"""

from __future__ import annotations

import sqlite3

import pytest

import trialerror
from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import (
    PROVENANCE_TABLE,
    Migration,
    apply_migrations,
    current_version,
    is_additive,
    latest_version,
    read_provenance,
)
from trialerror.stores.schema import jobs, knowledge, ops, platform

_MODULES = [platform, ops, knowledge, jobs]
_MODULE_IDS = ["platform", "ops", "knowledge", "jobs"]


@pytest.fixture(params=_MODULES, ids=_MODULE_IDS)
def schema_module(request):
    return request.param


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------


def test_every_db_gets_the_table_and_it_is_not_declared_in_any_schema_module(schema_module):
    """One provenance table per DB, created by the RUNNER -- so it must stay
    out of every module's ``TABLES`` tuple, which is what builds ``TABLE_DB``
    and what refuses a table name declared in two DBs."""
    from trialerror.stores.store import TABLE_DB

    conn = _conn()
    apply_migrations(conn, schema_module.MIGRATIONS)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (PROVENANCE_TABLE,)
    ).fetchone() is not None
    assert PROVENANCE_TABLE not in schema_module.TABLES
    assert PROVENANCE_TABLE not in TABLE_DB


def test_the_table_is_not_in_the_xid_registry():
    """It holds no typed id, so it has nothing to register and nothing for the
    dangling-XID scan to walk."""
    from trialerror.stores.xid import XID_REGISTRY

    assert not [key for key in XID_REGISTRY if key[0] == PROVENANCE_TABLE]


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def test_migrating_from_empty_records_one_row_per_migration(schema_module):
    conn = _conn()
    applied = apply_migrations(conn, schema_module.MIGRATIONS)
    rows = read_provenance(conn)
    assert [row["version"] for row in rows] == applied
    latest = latest_version(schema_module.MIGRATIONS)
    by_version = {m.version: m for m in schema_module.MIGRATIONS}
    for row in rows:
        assert row["provenance"] == "recorded"
        assert row["name"] == by_version[row["version"]].name
        assert row["applied_ts"], row
        assert row["client_version"] == trialerror.__version__
        assert row["client_schema_latest"] == latest
        assert row["additive"] in (0, 1)


def test_the_additive_flag_on_a_recorded_row_is_the_mechanical_classification(schema_module):
    conn = _conn()
    apply_migrations(conn, schema_module.MIGRATIONS)
    by_version = {m.version: m for m in schema_module.MIGRATIONS}
    for row in read_provenance(conn):
        assert bool(row["additive"]) is is_additive(by_version[row["version"]])


def test_a_client_version_may_be_stated_explicitly():
    conn = _conn()
    apply_migrations(conn, jobs.MIGRATIONS, client_version="9.9.9-test")
    assert {row["client_version"] for row in read_provenance(conn)} == {"9.9.9-test"}


def test_reapplying_adds_no_rows_and_applies_nothing(schema_module):
    conn = _conn()
    apply_migrations(conn, schema_module.MIGRATIONS)
    before = read_provenance(conn)
    assert apply_migrations(conn, schema_module.MIGRATIONS) == []
    assert read_provenance(conn) == before


def test_stepping_forward_records_only_the_step_it_took():
    """A real store is migrated in steps: the client that applied v1..v12 is
    not the one that applied v13, and the history has to say so."""
    conn = _conn()
    partial = tuple(m for m in knowledge.MIGRATIONS if m.version <= 12)
    apply_migrations(conn, partial, client_version="first-client")
    apply_migrations(conn, knowledge.MIGRATIONS, client_version="second-client")
    rows = {row["version"]: row for row in read_provenance(conn)}
    assert rows[12]["client_version"] == "first-client"
    assert rows[12]["client_schema_latest"] == 12
    assert rows[13]["client_version"] == "second-client"
    assert rows[13]["client_schema_latest"] == latest_version(knowledge.MIGRATIONS)


# ---------------------------------------------------------------------------
# backfill: a store whose runner predates the table
# ---------------------------------------------------------------------------


def test_a_store_migrated_by_an_older_runner_is_backfilled_on_the_next_call(schema_module):
    conn = _conn()
    apply_migrations(conn, schema_module.MIGRATIONS)
    # exactly the shape a pre-lane runner left behind: the schema and the
    # version pointer, and no history at all.
    conn.execute(f"DROP TABLE {PROVENANCE_TABLE}")
    assert read_provenance(conn) == []

    assert apply_migrations(conn, schema_module.MIGRATIONS) == []
    rows = read_provenance(conn)
    assert [row["version"] for row in rows] == [m.version for m in schema_module.MIGRATIONS]
    by_version = {m.version: m for m in schema_module.MIGRATIONS}
    for row in rows:
        assert row["provenance"] == "backfilled"
        assert row["applied_ts"] is None
        assert row["client_version"] is None
        assert row["client_schema_latest"] is None
        assert row["name"] == by_version[row["version"]].name
        assert bool(row["additive"]) is is_additive(by_version[row["version"]])


def test_a_backfill_invents_no_row_for_a_version_the_client_does_not_know():
    """A store migrated by a NEWER client sits on a version this one has no
    name for. The absence of that row is the signal the schema-direction check
    reads; a made-up name would destroy it."""
    conn = _conn()
    apply_migrations(conn, ops.MIGRATIONS)
    latest = latest_version(ops.MIGRATIONS)
    conn.execute(f"DROP TABLE {PROVENANCE_TABLE}")
    conn.execute(f"PRAGMA user_version = {latest + 3:d}")

    assert apply_migrations(conn, ops.MIGRATIONS) == []
    assert max(row["version"] for row in read_provenance(conn)) == latest


def test_a_backfilled_store_records_the_next_real_migration_normally():
    """Never a hard-coded schema number: this asserts about "the last
    migration" and "the one before it", so the next schema bump (lane FB-acq
    item 4 was knowledge v14) does not have to come back and edit it."""
    conn = _conn()
    latest = latest_version(knowledge.MIGRATIONS)
    previous = max(m.version for m in knowledge.MIGRATIONS if m.version < latest)
    partial = tuple(m for m in knowledge.MIGRATIONS if m.version <= previous)
    apply_migrations(conn, partial)
    conn.execute(f"DROP TABLE {PROVENANCE_TABLE}")

    assert apply_migrations(conn, knowledge.MIGRATIONS) == [latest]
    rows = {row["version"]: row for row in read_provenance(conn)}
    assert rows[previous]["provenance"] == "backfilled"
    assert rows[latest]["provenance"] == "recorded"
    assert rows[latest]["applied_ts"]


# ---------------------------------------------------------------------------
# the failure path
# ---------------------------------------------------------------------------


def test_a_failed_migration_leaves_no_provenance_row_and_no_version_bump():
    conn = _conn()
    good = Migration(version=1, name="fine", statements=("CREATE TABLE ok (id TEXT)",))
    bad = Migration(
        version=2, name="broken", statements=("CREATE TABLE also_ok (id TEXT)", "THIS IS NOT SQL")
    )
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [good, bad])
    assert current_version(conn) == 1
    assert [row["version"] for row in read_provenance(conn)] == [1]


# ---------------------------------------------------------------------------
# is_additive
# ---------------------------------------------------------------------------


def test_is_additive_is_true_for_the_add_only_migrations_and_false_for_a_rebuild():
    """Parametrised over the REAL migration history rather than toy DDL: the
    table-rebuild recipe (new table, copy, drop, rename) must classify as
    non-additive, and a plain ADD COLUMN / CREATE must not."""
    rebuilds = [m for m in jobs.MIGRATIONS if m.version in (2, 3)]
    assert rebuilds, "jobs v2/v3 are the rebuild migrations this asserts against"
    for m in rebuilds:
        assert is_additive(m) is False, m.name

    add_only = [m for m in knowledge.MIGRATIONS if m.version in (2, 8, 11, 12)]
    assert add_only
    for m in add_only:
        assert is_additive(m) is True, m.name


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("CREATE TABLE IF NOT EXISTS t (a TEXT)", True),
        ("create index idx_t_a on t(a)", True),
        ("CREATE UNIQUE INDEX idx_t_a ON t(a)", True),
        ("CREATE VIRTUAL TABLE t_fts USING fts5(body)", True),
        ("ALTER TABLE t ADD COLUMN b TEXT", True),
        ("  alter  table\n  t\n  add  column  b  TEXT ", True),
        ("ALTER TABLE t RENAME TO u", False),
        ("DROP TABLE t", False),
        ("INSERT INTO t (a) SELECT a FROM u", False),
        ("UPDATE t SET a = 'x'", False),
        ("CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1; END", False),
    ],
)
def test_is_additive_classifies_one_statement(statement, expected):
    assert is_additive(Migration(version=1, name="x", statements=(statement,))) is expected


def test_a_migration_is_additive_only_if_every_statement_is():
    mixed = Migration(
        version=1, name="mixed", statements=("CREATE TABLE t (a TEXT)", "DROP TABLE old")
    )
    assert is_additive(mixed) is False


def test_an_empty_migration_is_not_called_additive():
    """Nothing to inspect is not a safety claim."""
    assert is_additive(Migration(version=1, name="empty", statements=())) is False


# ---------------------------------------------------------------------------
# read_provenance
# ---------------------------------------------------------------------------


def test_read_provenance_on_a_store_without_the_table_is_empty_not_an_error():
    conn = _conn()
    conn.execute("CREATE TABLE unrelated (a TEXT)")
    assert read_provenance(conn) == []


def test_read_provenance_works_on_a_read_only_connection(tmp_path, platform_root, program_root):
    """The doctor's schema-direction check reads it through a read-only
    connection, so it must not need to create or write anything."""
    from trialerror.stores.connection import connect
    from trialerror.stores.paths import knowledge_db_path
    from trialerror.stores.store import open_store

    store = open_store(program_root, platform_root=platform_root)
    store.close()
    conn = connect(knowledge_db_path(program_root), read_only=True)
    try:
        rows = read_provenance(conn)
    finally:
        conn.close()
    assert [row["version"] for row in rows] == [m.version for m in knowledge.MIGRATIONS]
