"""ops-v3 (build-v2-polish): per-migration proving tests for the rooms-DDL
v3 migration (``trialerror/stores/schema/ops.py``'s ``_V3``/
``Migration(version=3, name="ops_v3_rooms_created_ts_scored_link_
deliverable", ...)``) -- addressing items 2, 3, 4, 5 of ``trialerror/rooms/
api.py``'s own module TRIALERROR-DEV-NOTE (item 1 stays open, out of scope).

Follows ``tests/test_stores_migrate_v2.py``'s established pattern verbatim
(fresh-create-vs-migrate-from-v1 schema-diff, per-DDL-change proving tests,
failure-path rollback discipline) but scoped to ops.py ONLY -- ops.py is
the only schema module this build's own v3 touches (a concurrent lane's
own knowledge-v3 is proven by its own test file, not duplicated here; see
this build's own coordination note in ``trialerror/stores/schema/ops.py``'s
migration-version comment about per-DB version slots not colliding)."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import Migration, apply_migrations, current_version, latest_version
from trialerror.stores.schema import ops


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Same ``(type, name, sql)`` diff-of-sqlite_master convention
    ``tests/test_stores_migrate_v2.py`` uses."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _v_only(max_version: int) -> tuple[Migration, ...]:
    return tuple(m for m in ops.MIGRATIONS if m.version <= max_version)


# ---------------------------------------------------------------------------
# fresh-create vs. migrate-from-v1v2 land identical schemas
# ---------------------------------------------------------------------------


def test_fresh_create_and_migrate_from_v1v2_land_identical_schemas():
    # scoped to _v_only(3), not the full (now longer) ops.MIGRATIONS -- this
    # test's job is proving v3 specifically lands the same schema either
    # way; later versions (ops_v4, build-v2dash-data) have their own proving
    # tests in tests/test_stores_migrate_v4_dashboard.py.
    v1v2v3 = _v_only(3)
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, v1v2v3)  # v1+v2+v3 in one call, from empty

    migrated = sqlite3.connect(":memory:")
    v1v2_only = _v_only(2)
    applied_v1v2 = apply_migrations(migrated, v1v2_only)
    assert applied_v1v2 == [1, 2]  # sanity: v1 then v2 really did apply first
    applied_v3 = apply_migrations(migrated, v1v2v3)  # only v3 is newer now
    assert applied_v3 == [3]

    assert current_version(fresh) == current_version(migrated) == latest_version(v1v2v3) == 3
    assert _schema_snapshot(fresh) == _schema_snapshot(migrated)


def test_v3_migration_is_idempotent_reapply_is_noop():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    snapshot_after_first = _schema_snapshot(conn)

    second = apply_migrations(conn, ops.MIGRATIONS)
    assert second == []
    assert _schema_snapshot(conn) == snapshot_after_first


# ---- item 2: room.created_ts / room_turn.ts --------------------------------


def test_room_gains_nullable_created_ts():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(room)").fetchall()}
    assert "created_ts" in cols
    assert cols["created_ts"][3] == 0  # notnull column: 0 = nullable

    conn.execute("INSERT INTO room (room_id, topic, dps, state, created_ts) VALUES ('ROOM-1','t','[]','open','2026-01-01T00:00:00.000Z')")
    conn.execute("INSERT INTO room (room_id, topic, dps, state) VALUES ('ROOM-2','t','[]','open')")  # created_ts omitted -> NULL
    conn.commit()
    rows = {r[0]: r[1] for r in conn.execute("SELECT room_id, created_ts FROM room").fetchall()}
    assert rows == {"ROOM-1": "2026-01-01T00:00:00.000Z", "ROOM-2": None}


def test_room_turn_gains_nullable_ts():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    conn.execute("INSERT INTO room (room_id, topic, dps, state) VALUES ('ROOM-1','t','[]','open')")
    conn.execute(
        "INSERT INTO room_turn (room_id, seq, author_launch, dp_ref, body, ts) "
        "VALUES ('ROOM-1', 1, 'LNCH-1', 'ROOM-1::DP1', 'hi', '2026-01-01T00:00:00.000Z')"
    )
    conn.commit()
    row = conn.execute("SELECT ts FROM room_turn WHERE room_id='ROOM-1' AND seq=1").fetchone()
    assert row[0] == "2026-01-01T00:00:00.000Z"


def test_room_and_room_turn_preexisting_rows_survive_with_null_new_columns():
    """A pre-v3 (v1/v2) row has nothing to backfill created_ts/ts FROM at
    the DDL level -- the table-rebuild/ADD COLUMN recipe must not lose the
    row itself, just leave the new column NULL."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    v1v2_only = _v_only(2)
    apply_migrations(conn, v1v2_only)
    conn.execute("INSERT INTO room (room_id, topic, dps, state) VALUES ('ROOM-old','t','[]','open')")
    conn.execute(
        "INSERT INTO room_turn (room_id, seq, author_launch, dp_ref, body) "
        "VALUES ('ROOM-old', 1, 'LNCH-1', 'ROOM-old::DP1', 'pre-v3 turn')"
    )
    conn.commit()

    apply_migrations(conn, ops.MIGRATIONS)  # v3 now applies on top of real v1/v2 data

    room_row = conn.execute("SELECT topic, state, created_ts FROM room WHERE room_id='ROOM-old'").fetchone()
    assert room_row == ("t", "open", None)
    turn_row = conn.execute("SELECT body, ts FROM room_turn WHERE room_id='ROOM-old' AND seq=1").fetchone()
    assert turn_row == ("pre-v3 turn", None)


# ---- item 5: room.deliverable_artifact_id ---------------------------------


def test_room_gains_deliverable_artifact_id_fk_to_artifact():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    conn.execute("INSERT INTO room (room_id, topic, dps, state) VALUES ('ROOM-1','t','[]','open')")
    conn.execute(
        "INSERT INTO template (type_key, title, version, path, gated) VALUES ('room_theory_doc','Doc','1','p.md',0)"
    )
    conn.execute(
        "INSERT INTO artifact (artifact_id, type, title, path, sha256, status, registered_by_launch) "
        "VALUES ('ART-1','room_theory_doc','t','p.md',?,'draft','LNCH-1')",
        ("0" * 64,),
    )
    conn.commit()

    conn.execute("UPDATE room SET deliverable_artifact_id = 'ART-1' WHERE room_id = 'ROOM-1'")
    conn.commit()
    row = conn.execute("SELECT deliverable_artifact_id FROM room WHERE room_id='ROOM-1'").fetchone()
    assert row[0] == "ART-1"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE room SET deliverable_artifact_id = 'ART-does-not-exist' WHERE room_id = 'ROOM-1'")


# ---- item 3: room_score composite PK (room_id, dp_id), dp_ref backfill ----


def test_room_score_gains_composite_pk_room_id_dp_id():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(room_score)").fetchall()}
    assert {"room_id", "dp_id", "agreement_pct", "frozen"} <= cols
    assert "dp_ref" not in cols  # namespacing convention retired for this table

    conn.execute("INSERT INTO room (room_id, topic, dps, state) VALUES ('ROOM-1','t','[]','open')")
    conn.execute("INSERT INTO room_score (room_id, dp_id, agreement_pct) VALUES ('ROOM-1','DP1', 92.5)")
    conn.commit()

    # composite PK rejects a duplicate (room_id, dp_id) pair...
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO room_score (room_id, dp_id, agreement_pct) VALUES ('ROOM-1','DP1', 10.0)")
    # ...but the SAME dp_id under a DIFFERENT room is fine (the whole point
    # of item 3 -- no more global-dp_ref collision across rooms).
    conn.execute("INSERT INTO room (room_id, topic, dps, state) VALUES ('ROOM-2','t','[]','open')")
    conn.execute("INSERT INTO room_score (room_id, dp_id, agreement_pct) VALUES ('ROOM-2','DP1', 10.0)")
    conn.commit()
    rows = {(r[0], r[1]): r[2] for r in conn.execute("SELECT room_id, dp_id, agreement_pct FROM room_score").fetchall()}
    assert rows == {("ROOM-1", "DP1"): 92.5, ("ROOM-2", "DP1"): 10.0}


def test_room_score_backfills_room_id_dp_id_from_preexisting_dp_ref():
    """A pre-v3 room_score row only ever had ``dp_ref = "<room_id>::<dp_id>"``
    -- the table-rebuild migration must split it correctly, losing no data,
    including a dp_id that itself contains "::"."""
    conn = sqlite3.connect(":memory:")
    v1v2_only = _v_only(2)
    apply_migrations(conn, v1v2_only)
    conn.execute("INSERT INTO room_score (dp_ref, agreement_pct, frozen) VALUES ('ROOM-1::DP1', 92.5, 0)")
    conn.execute("INSERT INTO room_score (dp_ref, agreement_pct, frozen) VALUES ('ROOM-2::DP1::weird', 10.0, 1)")
    conn.commit()

    apply_migrations(conn, ops.MIGRATIONS)  # v3 now applies on top of real v1/v2 data

    rows = {
        (r[0], r[1]): (r[2], r[3])
        for r in conn.execute("SELECT room_id, dp_id, agreement_pct, frozen FROM room_score").fetchall()
    }
    assert rows == {
        ("ROOM-1", "DP1"): (92.5, 0),
        ("ROOM-2", "DP1::weird"): (10.0, 1),
    }


# ---- item 4: room_link ------------------------------------------------------


def test_room_link_table_created_with_composite_pk():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(room_link)").fetchall()}
    assert cols == {"room_id", "dp_id", "idea_id"}

    conn.execute("INSERT INTO room (room_id, topic, dps, state) VALUES ('ROOM-1','t','[]','open')")
    conn.execute("INSERT INTO room_link (room_id, dp_id, idea_id) VALUES ('ROOM-1','DP1','IDEA-1')")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO room_link (room_id, dp_id, idea_id) VALUES ('ROOM-1','DP1','IDEA-2')")

    # same-file FK to room(room_id) is enforced.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO room_link (room_id, dp_id, idea_id) VALUES ('ROOM-does-not-exist','DP1','IDEA-3')")


# ---- failure-path rollback discipline (M1's pattern, ops-v3 own version) --


def test_v3_migration_failure_does_not_advance_version_or_partially_apply():
    """Same discipline as ``tests/test_stores_migrate_v2.py``'s own
    failure-path test, re-proven for ops-v3's real multi-statement
    table-rebuild (room_score) -- a mid-migration failure must roll back
    EVERY statement already run in v3, leaving the DB exactly as v1+v2
    left it."""
    conn = sqlite3.connect(":memory:")
    v1v2_only = _v_only(2)
    apply_migrations(conn, v1v2_only)
    before_snapshot = _schema_snapshot(conn)

    real_v3 = next(m for m in ops.MIGRATIONS if m.version == 3)
    broken = Migration(version=3, name=real_v3.name + "_broken_for_test", statements=real_v3.statements + ("THIS IS NOT SQL",))
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])

    assert current_version(conn) == 2  # never advanced to 3
    assert _schema_snapshot(conn) == before_snapshot  # not one statement survived
    conn.execute("PRAGMA foreign_keys")
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")  # would raise if a transaction leaked
