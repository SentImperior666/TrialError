"""``knowledge_v6_idea_aiif_columns_and_statuses`` — the table-rebuild
migration that widens ``idea.status`` and promotes the ten ideation-record
fields to columns.

A rebuild is the one migration shape that can silently LOSE data (new
table, copy, DROP, RENAME — get the column list wrong in the copy and the
rows survive with holes in them), so the tests below are written against a
row inserted at v5 and read back after the step up, not just against the
resulting DDL.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.migrate import apply_migrations, current_version, latest_version
from trialerror.stores.schema import knowledge

TS = "2026-09-06T12:00:00.000Z"

_PRE_V6_COLUMNS = (
    "idea_id", "round_id", "author_launch", "body", "slice_ref", "feed_post_ref",
    "status", "created_ts", "home", "assumed_circle", "provenance", "tier", "set_distance",
)


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _upto(migrations, max_version: int):
    return tuple(m for m in migrations if m.version <= max_version)


def _knowledge_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, version))
    return conn


def test_v6_is_contiguous_and_uniquely_named():
    """What this protects is contiguity, unique names, and v6 being THIS
    migration -- not v6 being the tail of the list. A later lane adding v7
    is a normal event and must not fail a test about v6 (the same
    correction ``tests/test_lexicon_schema.py`` took when v6 landed after
    its own v5)."""
    versions = [m.version for m in knowledge.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == latest_version(knowledge.MIGRATIONS)
    assert 6 in versions
    assert len({m.name for m in knowledge.MIGRATIONS}) == len(knowledge.MIGRATIONS)
    v6 = next(m for m in knowledge.MIGRATIONS if m.version == 6)
    assert v6.name == "knowledge_v6_idea_aiif_columns_and_statuses"


def test_v5_refuses_the_new_statuses_and_v6_accepts_them():
    """The before/after that makes this migration worth having."""
    at_v5 = _knowledge_at(5)
    with pytest.raises(sqlite3.IntegrityError):
        at_v5.execute(
            "INSERT INTO idea (idea_id, author_launch, body, status, created_ts) VALUES (?,?,?,?,?)",
            ("IDEA-x", "LNCH-x", "b", "eliminated", TS),
        )
    # The refused INSERT left sqlite3's implicit transaction open; the
    # migration runner opens its own BEGIN IMMEDIATE and cannot nest.
    at_v5.rollback()

    apply_migrations(at_v5, knowledge.MIGRATIONS)
    for status in ("eliminated", "merged"):
        at_v5.execute(
            "INSERT INTO idea (idea_id, author_launch, body, status, created_ts) VALUES (?,?,?,?,?)",
            (f"IDEA-{status}", "LNCH-x", "b", status, TS),
        )
    assert at_v5.execute("SELECT COUNT(*) FROM idea").fetchone()[0] == 2


def test_v6_still_refuses_a_status_outside_the_widened_check():
    conn = _knowledge_at(6)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO idea (idea_id, author_launch, body, status, created_ts) VALUES (?,?,?,?,?)",
            ("IDEA-x", "LNCH-x", "b", "archived", TS),
        )


def test_the_rebuild_carries_every_pre_v6_column_value_across():
    conn = _knowledge_at(5)
    values = (
        "IDEA-old", "round-0", "LNCH-old", "the full body text", '{"arm":"far"}', None,
        "consolidated", TS, "cell-4", "skeptics", '{"recipe_card":"TRANSFER"}', "far", 0.81,
    )
    conn.execute(
        f"INSERT INTO idea ({', '.join(_PRE_V6_COLUMNS)}) VALUES ({', '.join('?' * len(values))})",
        values,
    )
    conn.commit()

    apply_migrations(conn, knowledge.MIGRATIONS)

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM idea WHERE idea_id = 'IDEA-old'").fetchone()
    assert tuple(row[c] for c in _PRE_V6_COLUMNS) == values
    # The ten new columns land NULL on a copied row -- nothing is invented.
    from trialerror.lens.ideas import AIIF_FIELDS

    assert all(row[field] is None for field in AIIF_FIELDS)


@pytest.mark.parametrize("prefix", [1, 2, 3, 4, 5])
def test_stepping_from_any_earlier_version_lands_the_same_schema(prefix: int):
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, knowledge.MIGRATIONS)

    stepped = sqlite3.connect(":memory:")
    apply_migrations(stepped, _upto(knowledge.MIGRATIONS, prefix))
    apply_migrations(stepped, knowledge.MIGRATIONS)

    assert current_version(stepped) == latest_version(knowledge.MIGRATIONS)
    assert _schema_snapshot(stepped) == _schema_snapshot(fresh)


def test_reapplying_v6_is_a_noop():
    conn = _knowledge_at(latest_version(knowledge.MIGRATIONS))
    assert apply_migrations(conn, knowledge.MIGRATIONS) == []
    assert current_version(conn) == latest_version(knowledge.MIGRATIONS)


def test_the_rebuilt_table_is_named_idea_and_the_scratch_table_is_gone():
    conn = _knowledge_at(6)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "idea" in names
    assert "idea__v6new" not in names
