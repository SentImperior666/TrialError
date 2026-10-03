"""``ops_v13_gate_post_edit_sha256`` -- the additive columns the gate uses to
record the corrected bytes' hash itself. Pinned to v13's own prefix; the
earlier migrations have their own tests."""

from __future__ import annotations

import sqlite3

from trialerror.stores.migrate import apply_migrations, is_additive, latest_version
from trialerror.stores.schema import ops

V13 = next(m for m in ops.MIGRATIONS if m.version == 13)
NEW = ["post_edit_sha256", "post_edit_ts"]


def _upto(version: int):
    return tuple(m for m in ops.MIGRATIONS if m.version <= version)


def _names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_v13_is_contiguous_additive_and_named():
    versions = [m.version for m in _upto(13)]
    assert versions == list(range(1, 14))
    assert latest_version(_upto(13)) == 13
    assert V13.name == "ops_v13_gate_post_edit_sha256"
    assert is_additive(V13)


def test_a_v12_store_migrates_and_keeps_its_gates():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _upto(12))
    assert not set(NEW) & set(_names(conn, "gate"))
    conn.execute(
        "INSERT INTO template (type_key, title, version, path, gated) VALUES ('report','r','1','t.md',1)"
    )
    conn.execute(
        "INSERT INTO artifact (artifact_id, type, title, path, sha256, status, registered_by_launch) "
        "VALUES ('ART-old','report','t','p','s','draft','LNCH-x')"
    )
    conn.execute("INSERT INTO gate (gate_id, artifact_id, state) VALUES ('GATE-old','ART-old','draft')")
    conn.commit()

    assert apply_migrations(conn, _upto(13)) == [13]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    for column in NEW:
        assert column in _names(conn, "gate")
    row = conn.execute(f"SELECT state, {', '.join(NEW)} FROM gate WHERE gate_id='GATE-old'").fetchone()
    assert row[0] == "draft" and row[1] is None and row[2] is None


def test_the_migration_is_idempotent_and_schema_identical_to_a_fresh_v13_store():
    migrated = sqlite3.connect(":memory:")
    apply_migrations(migrated, _upto(12))
    apply_migrations(migrated, _upto(13))
    assert apply_migrations(migrated, _upto(13)) == []
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, _upto(13))

    def snapshot(conn):
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()

    assert snapshot(migrated) == snapshot(fresh)
