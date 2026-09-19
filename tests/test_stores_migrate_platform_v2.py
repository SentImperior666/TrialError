"""``platform_v2_launch_usage_split_pool_id_and_event_source`` — the first
rebuild of ``platform.launch``.

Two things make this migration worth its own module. It is the first table
rebuild in platform.db, and ``launch`` is the only table in this build with a
SELF-referencing foreign key (``parent_launch`` REFERENCES
``launch(launch_id)``) that carries real rows: a booking tree is exactly what
``tree_rollup`` walks, so a migration that silently dropped the parent links
would pass a column check and lose the rollups. And it widens a CHECK
constraint (``reconcile_source`` gains ``'event'``), which is the whole
reason it is a rebuild rather than five ``ADD COLUMN``s.

Lane FB-3 items 2/3/5.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.migrate import apply_migrations, current_version, latest_version
from trialerror.stores.schema import platform

TS = "2026-09-15T12:00:00.000Z"

_LEGACY_COLUMNS = (
    "launch_id", "account_id", "program_id", "session_id", "parent_launch", "agent_kind",
    "model_class", "model", "purpose", "est_tokens", "booked_ts", "booking_ttl_s", "state",
    "actual_tokens", "reconciled_ts", "reconcile_source", "workpackage", "attrs",
)

_NEW_COLUMNS = (
    "usage_input_tokens", "usage_cache_creation_tokens", "usage_cache_read_tokens",
    "usage_output_tokens", "pool_id",
)


def _upto(max_version: int):
    return tuple(m for m in platform.MIGRATIONS if m.version <= max_version)


def _platform_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(version))
    return conn


def _seed_v1(conn: sqlite3.Connection) -> None:
    """One account, one pool, and a two-deep booking TREE (child ->
    parent), so the self-FK is exercised with rows on both sides."""
    conn.execute("INSERT INTO account (account_id, label, created_ts) VALUES ('ACC-1','a',?)", (TS,))
    conn.execute(
        "INSERT INTO budget_pool (pool_id, account_id, model_class, period, period_start, cap_tokens, "
        "spent_visible_tokens, billed_multiplier, soft_pct, hard_pct, updated_ts) "
        "VALUES ('POOL-1','ACC-1','top','weekly',?,1000000,0,2.75,95,100,?)",
        (TS, TS),
    )
    for launch_id, parent, state, actual, source in (
        ("LNCH-parent", None, "RECONCILED", 5000, "manual"),
        ("LNCH-child", "LNCH-parent", "RECONCILED", 1200, "transcript"),
        ("LNCH-live", None, "PROVISIONAL", None, None),
    ):
        conn.execute(
            "INSERT INTO launch (launch_id, account_id, program_id, session_id, parent_launch, "
            "agent_kind, model_class, model, purpose, est_tokens, booked_ts, booking_ttl_s, state, "
            "actual_tokens, reconciled_ts, reconcile_source, workpackage, attrs) "
            "VALUES (?,?,'PROG-1','SESS-1',?,'lens','top','opus','ideation',9000,?,3600,?,?,?,?,'WKP-1',NULL)",
            (launch_id, "ACC-1", parent, TS, state, actual, TS if actual else None, source),
        )
    conn.commit()


def test_latest_version_is_two():
    assert latest_version(platform.MIGRATIONS) == 2


def test_an_old_populated_store_migrates_and_keeps_every_row_and_the_booking_tree():
    conn = _platform_at(1)
    _seed_v1(conn)
    assert current_version(conn) == 1

    applied = apply_migrations(conn, platform.MIGRATIONS)
    assert applied == [2]
    assert current_version(conn) == 2

    rows = {r["launch_id"]: dict(r) for r in conn.execute("SELECT * FROM launch").fetchall()}
    assert set(rows) == {"LNCH-parent", "LNCH-child", "LNCH-live"}
    # the self-FK survived the rebuild -- tree_rollup walks exactly this
    assert rows["LNCH-child"]["parent_launch"] == "LNCH-parent"
    assert rows["LNCH-parent"]["actual_tokens"] == 5000
    assert rows["LNCH-child"]["reconcile_source"] == "transcript"
    assert rows["LNCH-live"]["state"] == "PROVISIONAL"
    # every new column is present and NULL on a pre-v2 row: "nobody measured
    # this", which a zero could not say
    for row in rows.values():
        for column in _NEW_COLUMNS:
            assert row[column] is None, column


def test_the_rebuild_recreates_every_index():
    conn = _platform_at(1)
    _seed_v1(conn)
    apply_migrations(conn, platform.MIGRATIONS)
    names = {r["name"] for r in conn.execute("PRAGMA index_list(launch)").fetchall()}
    assert {"idx_launch_state", "idx_launch_account", "idx_launch_pool"} <= names


def test_the_migration_is_idempotent_and_schema_identical_to_a_fresh_v2_store():
    upgraded = _platform_at(1)
    _seed_v1(upgraded)
    apply_migrations(upgraded, platform.MIGRATIONS)
    assert apply_migrations(upgraded, platform.MIGRATIONS) == []
    assert current_version(upgraded) == 2

    fresh = _platform_at(2)

    def snapshot(conn):
        return [
            (r["type"], r["name"], r["sql"])
            for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ]

    assert snapshot(upgraded) == snapshot(fresh)
    # and no rebuild scaffolding left behind
    assert not [n for _t, n, _s in snapshot(upgraded) if "__v2new" in n]


def test_the_event_reconcile_source_is_accepted_only_after_v2():
    old = _platform_at(1)
    _seed_v1(old)
    with pytest.raises(sqlite3.IntegrityError):
        old.execute("UPDATE launch SET reconcile_source = 'event' WHERE launch_id = 'LNCH-parent'")

    new = _platform_at(1)
    _seed_v1(new)
    apply_migrations(new, platform.MIGRATIONS)
    new.execute("UPDATE launch SET reconcile_source = 'event' WHERE launch_id = 'LNCH-parent'")
    assert new.execute(
        "SELECT reconcile_source FROM launch WHERE launch_id = 'LNCH-parent'"
    ).fetchone()[0] == "event"


def test_the_widened_check_still_refuses_an_unknown_source():
    conn = _platform_at(2)
    _seed_v1(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE launch SET reconcile_source = 'vibes' WHERE launch_id = 'LNCH-parent'")


def test_pool_id_is_a_real_foreign_key_after_the_rebuild():
    conn = _platform_at(2)
    _seed_v1(conn)
    conn.execute("UPDATE launch SET pool_id = 'POOL-1' WHERE launch_id = 'LNCH-live'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE launch SET pool_id = 'POOL-nope' WHERE launch_id = 'LNCH-live'")


def test_the_legacy_column_set_is_still_all_there():
    conn = _platform_at(2)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(launch)").fetchall()}
    assert set(_LEGACY_COLUMNS) <= columns
    assert set(_NEW_COLUMNS) <= columns
