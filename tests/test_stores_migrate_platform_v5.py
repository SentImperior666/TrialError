"""``platform_v5_launch_spawn_identity`` -- the additive migration that gives
``launch`` the four columns the spawn gate and the spawn-failure hook use to
tell a spawn that never started from one that did.

ADD COLUMN and CREATE INDEX only: additivity, a v4 store with real rows
migrates and keeps them, and a fresh store lands on the identical schema.
Pinned to v5's own prefix; the earlier migrations have their own tests.
"""

from __future__ import annotations

import sqlite3

from trialerror.stores.migrate import apply_migrations, current_version, is_additive
from trialerror.stores.schema import platform

TS = "2026-09-28T12:00:00.000Z"
NEW_COLUMNS = {"spawn_tool_use_id", "spawn_ts", "spawn_transcript_dir", "agent_id"}


def _upto(max_version: int):
    return tuple(m for m in platform.MIGRATIONS if m.version <= max_version)


def _platform_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(version))
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_v5_is_additive_and_numbered_five():
    v5 = next(m for m in platform.MIGRATIONS if m.version == 5)
    assert is_additive(v5)
    assert [m.version for m in _upto(5)] == [1, 2, 3, 4, 5]


def test_v4_store_migrates_to_v5_and_keeps_its_launch_rows():
    conn = _platform_at(4)
    assert not (NEW_COLUMNS & _columns(conn, "launch"))
    conn.execute("INSERT INTO account (account_id, label, created_ts) VALUES ('ACC-1','a',?)", (TS,))
    conn.execute(
        "INSERT INTO launch (launch_id, session_id, program_id, account_id, agent_kind, model_class, "
        "model, purpose, est_tokens, state, booked_ts, booking_ttl_s) "
        "VALUES ('L-1','S-1','P','ACC-1','lens','mid','sonnet','mechanical',100,'PROVISIONAL',?,3600)",
        (TS,),
    )
    conn.commit()

    assert apply_migrations(conn, platform.MIGRATIONS) == [5]
    assert current_version(conn) == 5
    assert NEW_COLUMNS <= _columns(conn, "launch")
    row = conn.execute("SELECT * FROM launch WHERE launch_id = 'L-1'").fetchone()
    assert row["state"] == "PROVISIONAL"
    assert all(row[c] is None for c in NEW_COLUMNS)
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(launch)").fetchall()}
    assert "ix_launch_spawn_tool_use" in indexes


def test_the_migration_is_idempotent_and_schema_identical_to_a_fresh_v5_store():
    upgraded = _platform_at(4)
    apply_migrations(upgraded, platform.MIGRATIONS)
    assert apply_migrations(upgraded, platform.MIGRATIONS) == []
    fresh = _platform_at(5)

    def snapshot(conn):
        return [
            (r["type"], r["name"], r["sql"])
            for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ]

    assert snapshot(upgraded) == snapshot(fresh)
