"""``ops_v9_lens_control_seat_recipe_cards_and_arm_mode`` — the rebuild that
widens ``lens_roster.seat`` and adds the card/mode columns.

``lens_roster`` is the first rebuilt table in ops.db with a same-file FK
child that has ROWS in it (``lens_assignment.roster_id``), so the copy path
is tested with a populated child present, not just an empty schema.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.migrate import apply_migrations, current_version, latest_version
from trialerror.stores.schema import ops

TS = "2026-09-06T12:00:00.000Z"


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _upto(migrations, max_version: int):
    return tuple(m for m in migrations if m.version <= max_version)


def _ops_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(ops.MIGRATIONS, version))
    return conn


def _seed_roster_and_assignment(conn: sqlite3.Connection, *, seat: str = "standard") -> None:
    conn.execute(
        "INSERT INTO lens_roster (roster_id, round_id, lens_name, vantage, seat, model_class, created_ts) "
        "VALUES (?,?,?,?,?,?,?)",
        ("ROST-1", "round-0", "lens-1", "a vantage", seat, "top", TS),
    )
    conn.execute(
        "INSERT INTO lens_assignment (assign_id, roster_id, slice_spec, arm, seed, created_ts) "
        "VALUES (?,?,?,?,?,?)",
        ("ASGN-1", "ROST-1", '{"candidate_id":"DOC-1"}', "far", "s", TS),
    )
    conn.commit()


def test_v9_is_contiguous_and_uniquely_named():
    versions = [m.version for m in ops.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert 9 in versions
    assert len({m.name for m in ops.MIGRATIONS}) == len(ops.MIGRATIONS)
    v9 = next(m for m in ops.MIGRATIONS if m.version == 9)
    assert v9.name == "ops_v9_lens_control_seat_recipe_cards_and_arm_mode"


def test_v8_refuses_the_control_seat_and_v9_accepts_it():
    at_v8 = _ops_at(8)
    with pytest.raises(sqlite3.IntegrityError):
        at_v8.execute(
            "INSERT INTO lens_roster (roster_id, round_id, lens_name, vantage, seat, model_class, created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("ROST-c", "r", "control-1", "CONTROL:no-recipe", "control", "top", TS),
        )
    at_v8.rollback()

    apply_migrations(at_v8, ops.MIGRATIONS)
    at_v8.execute(
        "INSERT INTO lens_roster (roster_id, round_id, lens_name, vantage, seat, model_class, created_ts) "
        "VALUES (?,?,?,?,?,?,?)",
        ("ROST-c", "r", "control-1", "CONTROL:no-recipe", "control", "top", TS),
    )
    assert at_v8.execute("SELECT COUNT(*) FROM lens_roster WHERE seat='control'").fetchone()[0] == 1


def test_v9_still_refuses_a_seat_outside_the_widened_check():
    conn = _ops_at(9)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lens_roster (roster_id, round_id, lens_name, vantage, seat, model_class, created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("ROST-x", "r", "l", "v", "observer", "top", TS),
        )


def test_the_rebuild_survives_a_populated_fk_child_and_keeps_both_rows():
    conn = _ops_at(8)
    _seed_roster_and_assignment(conn, seat="assumption_buster")

    apply_migrations(conn, ops.MIGRATIONS)

    conn.row_factory = sqlite3.Row
    roster = conn.execute("SELECT * FROM lens_roster WHERE roster_id='ROST-1'").fetchone()
    assert roster["seat"] == "assumption_buster"
    assert roster["lens_name"] == "lens-1"
    assert roster["recipe_cards"] is None
    assignment = conn.execute("SELECT * FROM lens_assignment WHERE assign_id='ASGN-1'").fetchone()
    assert assignment["roster_id"] == "ROST-1"
    assert assignment["arm_mode"] is None
    assert assignment["far_lens_floor"] is None
    assert assignment["recipe_cards"] is None


def test_the_fk_from_assignment_to_roster_is_still_enforced_after_the_rebuild():
    conn = _ops_at(9)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lens_assignment (assign_id, roster_id, slice_spec, arm, seed, created_ts) "
            "VALUES (?,?,?,?,?,?)",
            ("ASGN-orphan", "ROST-nope", "{}", "near", "s", TS),
        )


def test_arm_mode_check_refuses_an_unknown_mode():
    conn = _ops_at(9)
    _seed_roster_and_assignment(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lens_assignment (assign_id, roster_id, slice_spec, arm, arm_mode, seed, created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("ASGN-2", "ROST-1", "{}", "near", "per_round", "s", TS),
        )


@pytest.mark.parametrize("prefix", [1, 4, 8])
def test_stepping_from_an_earlier_version_lands_the_same_schema(prefix: int):
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, ops.MIGRATIONS)

    stepped = sqlite3.connect(":memory:")
    apply_migrations(stepped, _upto(ops.MIGRATIONS, prefix))
    apply_migrations(stepped, ops.MIGRATIONS)

    assert current_version(stepped) == latest_version(ops.MIGRATIONS)
    assert _schema_snapshot(stepped) == _schema_snapshot(fresh)


def test_reapplying_v9_is_a_noop():
    conn = _ops_at(9)
    assert apply_migrations(conn, _upto(ops.MIGRATIONS, 9)) == []
    assert current_version(conn) == 9


def test_the_scratch_table_is_gone_and_the_child_index_survives():
    conn = _ops_at(9)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "lens_roster" in names
    assert "lens_roster__v9new" not in names
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_lens_assignment_roster" in indexes
