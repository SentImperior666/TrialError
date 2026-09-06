"""Per-migration proving tests for ops-v7 (``trialerror/stores/schema/ops.py``'s
``_V7`` / ``Migration(version=7,
name="ops_v7_memory_relation_and_reviewed_ts", ...)``) -- the 2026-09 mining
lane's ``memory_relation`` table (engram-F4) plus the additive
``memory_item.reviewed_ts`` column (engram-F5).

Added by the fix pass (verify of 2026-09-05), finding F-05: v7 shipped
without the per-migration file every prior additive ops migration has (v3 ->
``test_stores_migrate_v3_rooms.py``, v4 -> ``..._v4_dashboard.py``, v6 ->
``..._v6_feed_translation_gate.py``). Follows
``test_stores_migrate_v6_feed_translation_gate.py``'s pattern, which follows
v4's before it: fresh-create-vs-migrate-from-vN schema diff, per-DDL-change
proving tests, failure-path rollback discipline.

Numbered v7, not the v8 it briefly wore: this migration was authored as
"ops_v6" against the branch point, renumbered to v8 while lane c looked
likely to land first, and settled at v7 by orchestrator ruling L-C1 once it
merged ahead of lane c (which takes v8). ``ops.MIGRATIONS`` must stay
contiguous 1..7 -- asserted below, since a renumber is exactly the kind of
edit that leaves a hole.

The CONSTRAINTS matter more here than in a typical additive migration,
because the lane record's advisory-only contract is enforced at this layer
and nowhere stronger: no UNIQUE on the pair (two actors are allowed to
disagree about the same two items), a ``marked_by_kind`` vocabulary that
admits ``system`` at the schema level while the API refuses to write it, and
``relation`` NULLable so a pending candidate can exist with no verb attached
to anyone's name.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import Migration, apply_migrations, current_version, latest_version
from trialerror.stores.schema import ops


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _v_only(max_version: int) -> tuple[Migration, ...]:
    return tuple(m for m in ops.MIGRATIONS if m.version <= max_version)


def _at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _v_only(version))
    return conn


def _seed_items(conn: sqlite3.Connection, *keys: str) -> None:
    for key in keys:
        conn.execute(
            "INSERT INTO memory_item (memory_item_id, key, tier, kind, body, updated_ts) "
            "VALUES (?, ?, 'L0', 'rule', 'b', '2026-01-01T00:00:00.000Z')",
            (f"MITM-{key}", key),
        )


def _insert_relation(conn: sqlite3.Connection, relation_id: str, **overrides: object) -> None:
    row: dict[str, object] = {
        "relation_id": relation_id,
        "source_id": "MITM-a",
        "target_id": "MITM-b",
        "relation": None,
        "judgment_status": "pending",
        "created_ts": "2026-01-01T00:00:00.000Z",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO memory_relation ({cols}) VALUES ({marks})", tuple(row.values()))


# ---------------------------------------------------------------------------
# the migration list itself
# ---------------------------------------------------------------------------


def test_ops_migrations_are_contiguous_one_through_seven():
    """A renumber (v6 -> v8 -> v7) is exactly the edit that leaves a hole or
    a duplicate behind."""
    versions = [m.version for m in ops.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == 7
    assert len({m.name for m in ops.MIGRATIONS}) == len(ops.MIGRATIONS)


def test_v7_is_the_memory_relation_migration_under_its_ruled_name():
    v7 = next(m for m in ops.MIGRATIONS if m.version == 7)
    assert v7.name == "ops_v7_memory_relation_and_reviewed_ts"
    assert v7.statements is ops._V7
    assert not hasattr(ops, "_V8")


# ---------------------------------------------------------------------------
# fresh-create vs. migrate-from-v6 land identical schemas
# ---------------------------------------------------------------------------


def test_fresh_create_and_migrate_from_v6_land_identical_schemas():
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, ops.MIGRATIONS)

    migrated = sqlite3.connect(":memory:")
    assert apply_migrations(migrated, _v_only(6)) == [1, 2, 3, 4, 5, 6]
    assert apply_migrations(migrated, ops.MIGRATIONS) == sorted(m.version for m in ops.MIGRATIONS if m.version > 6)

    assert current_version(fresh) == current_version(migrated) == latest_version(ops.MIGRATIONS)
    assert _schema_snapshot(fresh) == _schema_snapshot(migrated)


@pytest.mark.parametrize("prefix", [1, 2, 3, 4, 5, 6])
def test_stepping_from_any_earlier_version_lands_the_same_schema(prefix: int):
    """Not just from v6: an operator's store can be sitting at ANY prior
    version when this lane arrives."""
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, ops.MIGRATIONS)

    stepped = sqlite3.connect(":memory:")
    apply_migrations(stepped, _v_only(prefix))
    apply_migrations(stepped, ops.MIGRATIONS)

    assert current_version(stepped) == latest_version(ops.MIGRATIONS)
    assert _schema_snapshot(stepped) == _schema_snapshot(fresh)


def test_v7_migration_is_idempotent_reapply_is_noop():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    assert apply_migrations(conn, ops.MIGRATIONS) == []
    assert current_version(conn) == latest_version(ops.MIGRATIONS)


# ---------------------------------------------------------------------------
# memory_item.reviewed_ts is ADDITIVE
# ---------------------------------------------------------------------------


def test_reviewed_ts_is_added_nullable_and_costs_pre_existing_rows_nothing():
    conn = _at(6)
    _seed_items(conn, "a")
    conn.commit()  # close the implicit DML transaction before the migration's BEGIN IMMEDIATE

    before = {r[1] for r in conn.execute("PRAGMA table_info(memory_item)")}
    assert "reviewed_ts" not in before

    apply_migrations(conn, ops.MIGRATIONS)

    info = {r[1]: r for r in conn.execute("PRAGMA table_info(memory_item)")}
    assert "reviewed_ts" in info
    assert info["reviewed_ts"][2] == "TEXT"
    assert info["reviewed_ts"][3] == 0  # notnull = 0: an un-reviewed row is the normal case
    assert info["reviewed_ts"][4] is None  # no default -- absence means "never reviewed", not a timestamp
    assert before <= set(info)  # purely additive: not one pre-existing column lost

    row = conn.execute("SELECT reviewed_ts, body, status FROM memory_item WHERE memory_item_id = 'MITM-a'").fetchone()
    assert row == (None, "b", "active")


# ---------------------------------------------------------------------------
# memory_relation's DDL
# ---------------------------------------------------------------------------


def test_memory_relation_carries_every_declared_column():
    conn = _at(7)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(memory_relation)")}
    assert set(cols) == {
        "relation_id",
        "source_id",
        "target_id",
        "relation",
        "judgment_status",
        "score",
        "reason",
        "evidence",
        "confidence",
        "marked_by_actor",
        "marked_by_kind",
        "marked_by_model",
        "created_ts",
        "judged_ts",
        "superseded_by_relation_id",
    }
    assert cols["relation_id"][5] == 1  # primary key
    for required in ("source_id", "target_id", "judgment_status", "created_ts"):
        assert cols[required][3] == 1, required
    # A pending candidate has no verb and nobody's name on it yet.
    for optional in ("relation", "marked_by_actor", "marked_by_kind", "judged_ts", "confidence"):
        assert cols[optional][3] == 0, optional


def test_the_relation_check_is_exactly_the_sources_six_verbs():
    conn = _at(7)
    _seed_items(conn, "a", "b")
    verbs = ("related", "compatible", "scoped", "conflicts_with", "supersedes", "not_conflict")

    from trialerror.memory.conflicts import RELATION_VERBS

    assert set(RELATION_VERBS) == set(verbs), "the API vocabulary and the DDL CHECK must not drift apart"

    for i, verb in enumerate(verbs):
        _insert_relation(conn, f"MREL-{i}", relation=verb, judgment_status="judged")
    assert {r[0] for r in conn.execute("SELECT relation FROM memory_relation")} == set(verbs)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_relation(conn, "MREL-bad", relation="disagrees_with", judgment_status="judged")


def test_a_pending_row_may_carry_no_verb_at_all():
    """The advisory-only contract at the schema layer: the save-time scan
    writes a candidate, never a claim."""
    conn = _at(7)
    _seed_items(conn, "a", "b")
    _insert_relation(conn, "MREL-pending")
    row = conn.execute(
        "SELECT relation, marked_by_actor, marked_by_kind, judged_ts, confidence FROM memory_relation"
    ).fetchone()
    assert row == (None, None, None, None, None)


def test_judgment_status_is_a_closed_three_value_vocabulary():
    conn = _at(7)
    _seed_items(conn, "a", "b")
    for i, status in enumerate(("pending", "judged", "superseded")):
        _insert_relation(conn, f"MREL-{i}", judgment_status=status)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_relation(conn, "MREL-bad", judgment_status="maybe")


def test_marked_by_kind_is_checked_at_the_schema_layer():
    """``system`` is legal DDL and refused by the API. The schema keeps the
    value expressible (a future non-advisory writer would need it) while
    ``trialerror.memory.conflicts.judge`` is the thing that forbids a machine
    from settling a conflict -- see ``test_memory_conflicts.py``'s
    ``test_judge_refuses_a_system_actor``."""
    conn = _at(7)
    _seed_items(conn, "a", "b")
    for i, kind in enumerate(("human", "agent", "system")):
        _insert_relation(conn, f"MREL-{i}", marked_by_kind=kind)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_relation(conn, "MREL-bad", marked_by_kind="daemon")


def test_there_is_no_unique_constraint_anywhere_on_memory_relation():
    """Two actors are allowed to disagree about the same pair, and one pair
    may be re-judged over time. A UNIQUE here would silently make the last
    writer the only writer."""
    conn = _at(7)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'memory_relation'").fetchone()[0]
    assert "UNIQUE" not in sql.upper()

    index_rows = list(conn.execute("PRAGMA index_list(memory_relation)"))
    non_pk = [r for r in index_rows if r[3] != "pk"]
    assert all(r[2] == 0 for r in non_pk), f"a unique index snuck onto memory_relation: {non_pk}"

    _seed_items(conn, "a", "b")
    _insert_relation(conn, "MREL-1", relation="conflicts_with", judgment_status="judged", marked_by_actor="ana")
    _insert_relation(conn, "MREL-2", relation="not_conflict", judgment_status="judged", marked_by_actor="ben")
    assert conn.execute("SELECT count(*) FROM memory_relation").fetchone()[0] == 2


def test_v7_creates_both_lookup_indexes():
    conn = _at(7)
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'memory_relation'")
    }
    assert {"idx_memory_relation_source", "idx_memory_relation_status"} <= names


def test_both_endpoints_are_enforced_foreign_keys_into_memory_item():
    conn = _at(7)
    _seed_items(conn, "a")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_relation(conn, "MREL-dangling", source_id="MITM-a", target_id="MITM-ghost")


# ---- failure-path rollback discipline -----------------------------------------


def test_v7_migration_failure_does_not_advance_version_or_partially_apply():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _v_only(6))
    before_snapshot = _schema_snapshot(conn)

    real_v7 = next(m for m in ops.MIGRATIONS if m.version == 7)
    broken = Migration(
        version=7, name=real_v7.name + "_broken_for_test", statements=real_v7.statements + ("THIS IS NOT SQL",)
    )
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])

    assert current_version(conn) == 6  # never advanced to 7
    assert _schema_snapshot(conn) == before_snapshot  # not one CREATE or ALTER survived
    assert "reviewed_ts" not in {r[1] for r in conn.execute("PRAGMA table_info(memory_item)")}
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")  # would raise if a transaction leaked
