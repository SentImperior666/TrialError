"""build-v2-summary: per-migration proving tests for the
``knowledge_v3_summary_table`` migration, mirroring
``tests/test_stores_migrate_v2.py``'s own "fresh-create path and
migrate-path (v1(+v2)->v3) must land IDENTICAL schemas" discipline test
-- scoped to ``trialerror.stores.schema.knowledge`` only (the other three
schema modules have no v3 of their own from this build)."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import Migration, apply_migrations, current_version
from trialerror.stores.schema import knowledge


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """``(type, name, sql)`` for every non-internal schema object, in a
    stable order -- matches ``tests/test_stores_migrate_v2.py``'s own
    helper exactly (independent copy, not an import, per this suite's own
    file-per-lane convention)."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def test_fresh_create_and_migrate_from_v2_land_identical_schemas():
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, knowledge.MIGRATIONS)  # v1+v2+v3 in one call, from empty

    migrated = sqlite3.connect(":memory:")
    v1_v2_only = tuple(m for m in knowledge.MIGRATIONS if m.version <= 2)
    applied_early = apply_migrations(migrated, v1_v2_only)
    assert applied_early == [1, 2]  # sanity: v1+v2 really did apply on their own first
    applied_v3 = apply_migrations(migrated, knowledge.MIGRATIONS)  # only v3 is newer now
    assert applied_v3 == [3]

    assert current_version(fresh) == current_version(migrated) == 3
    assert _schema_snapshot(fresh) == _schema_snapshot(migrated)


def test_v3_migration_is_idempotent_reapply_is_noop():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, knowledge.MIGRATIONS)
    snapshot_after_first = _schema_snapshot(conn)

    second = apply_migrations(conn, knowledge.MIGRATIONS)
    assert second == []
    assert _schema_snapshot(conn) == snapshot_after_first


def test_summary_table_shape_and_constraints():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, knowledge.MIGRATIONS)

    conn.execute(
        "INSERT INTO summary (summary_id, subject_kind, subject_id, tier, body, word_count, "
        "word_cap, source_doc_ids, subject_sha256, fenced, status, procedure_version, "
        "created_by_launch, created_ts) VALUES "
        "('SUM-1','document','DOC-1','L1','a body',2,150,'[\"DOC-1\"]',?,0,"
        "'current','1','LNCH-1','t')",
        ("0" * 64,),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM summary WHERE summary_id='SUM-1'").fetchone()
    assert row is not None

    # subject_kind CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO summary (summary_id, subject_kind, subject_id, body, word_count, "
            "word_cap, source_doc_ids, subject_sha256, status, procedure_version, "
            "created_by_launch, created_ts) VALUES "
            "('SUM-bad','bogus','X','b',1,1,'[]','sha','current','1','LNCH-1','t')"
        )

    # status CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO summary (summary_id, subject_kind, subject_id, body, word_count, "
            "word_cap, source_doc_ids, subject_sha256, status, procedure_version, "
            "created_by_launch, created_ts) VALUES "
            "('SUM-bad2','document','DOC-1','b',1,1,'[]','sha','bogus-status','1','LNCH-1','t')"
        )

    # tier CHECK (only 'L1' is a valid value in v0).
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO summary (summary_id, subject_kind, subject_id, tier, body, word_count, "
            "word_cap, source_doc_ids, subject_sha256, status, procedure_version, "
            "created_by_launch, created_ts) VALUES "
            "('SUM-bad3','document','DOC-1','L2','b',1,1,'[]','sha','current','1','LNCH-1','t')"
        )

    # fenced CHECK (0/1 only).
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO summary (summary_id, subject_kind, subject_id, body, word_count, "
            "word_cap, source_doc_ids, subject_sha256, fenced, status, procedure_version, "
            "created_by_launch, created_ts) VALUES "
            "('SUM-bad4','document','DOC-1','b',1,1,'[]','sha',2,'current','1','LNCH-1','t')"
        )

    # NO partial-unique index on (subject_kind, subject_id) WHERE
    # status='current' (the module docstring's own deliberate choice --
    # supersede-before-insert is a write-API convention, not a DDL
    # constraint) -- a second 'current' row for the SAME subject inserts
    # cleanly at the raw-DDL level.
    conn.execute(
        "INSERT INTO summary (summary_id, subject_kind, subject_id, body, word_count, "
        "word_cap, source_doc_ids, subject_sha256, status, procedure_version, "
        "created_by_launch, created_ts) VALUES "
        "('SUM-2','document','DOC-1','second body',2,150,'[\"DOC-1\"]','sha2','current','1','LNCH-1','t2')"
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM summary WHERE subject_kind='document' AND subject_id='DOC-1' AND status='current'").fetchone()[0]
    assert count == 2


def test_v3_migration_failure_does_not_advance_version_or_partially_apply():
    """Mirrors ``tests/test_stores_migrate.py``'s failure-path discipline
    test, re-proven for this build's own v3 migration."""
    conn = sqlite3.connect(":memory:")
    v1_v2_only = tuple(m for m in knowledge.MIGRATIONS if m.version <= 2)
    apply_migrations(conn, v1_v2_only)
    before_snapshot = _schema_snapshot(conn)

    real_v3 = next(m for m in knowledge.MIGRATIONS if m.version == 3)
    broken = Migration(version=3, name=real_v3.name + "_broken_for_test", statements=real_v3.statements + ("THIS IS NOT SQL",))
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])

    assert current_version(conn) == 2  # never advanced to 3
    assert _schema_snapshot(conn) == before_snapshot
    conn.execute("PRAGMA foreign_keys")
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")
