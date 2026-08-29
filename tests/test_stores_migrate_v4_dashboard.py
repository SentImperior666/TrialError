"""build-v2dash-data: per-migration proving tests for ops-v4 (``trialerror/stores/
schema/ops.py``'s ``_V4``/``Migration(version=4, name=
"ops_v4_criterion_and_feed_post_translation", ...)``) -- the two additive
table-only seams the V2 dashboard redesign names (REDESIGN_V2_RATIONALE.md
Section 5.3 items 6 and 8): ``criterion`` (the Course surface's minimal
seam) and ``feed_post_translation`` (the AISPEAK translator's storage
design, table seam only).

Follows ``tests/test_stores_migrate_v3_rooms.py``'s established pattern
verbatim (fresh-create-vs-migrate-from-vN schema-diff, per-DDL-change
proving tests, failure-path rollback discipline).
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


# ---------------------------------------------------------------------------
# fresh-create vs. migrate-from-v1v2v3 land identical schemas
# ---------------------------------------------------------------------------


def test_fresh_create_and_migrate_from_v1v2v3_land_identical_schemas():
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, ops.MIGRATIONS)  # v1..v4 in one call, from empty

    migrated = sqlite3.connect(":memory:")
    v1v2v3_only = _v_only(3)
    applied_first = apply_migrations(migrated, v1v2v3_only)
    assert applied_first == [1, 2, 3]
    applied_v4 = apply_migrations(migrated, ops.MIGRATIONS)  # only v4 is newer now
    assert applied_v4 == [4]

    assert current_version(fresh) == current_version(migrated) == latest_version(ops.MIGRATIONS) == 4
    assert _schema_snapshot(fresh) == _schema_snapshot(migrated)


def test_v4_migration_is_idempotent_reapply_is_noop():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    snapshot_after_first = _schema_snapshot(conn)

    second = apply_migrations(conn, ops.MIGRATIONS)
    assert second == []
    assert _schema_snapshot(conn) == snapshot_after_first


# ---- criterion ---------------------------------------------------------------


def test_criterion_table_created_with_expected_columns_and_check():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(criterion)").fetchall()}
    assert cols == {"criterion_id", "label", "phase", "state", "discharged_by_artifact"}

    conn.execute(
        "INSERT INTO criterion (criterion_id, label, phase, state) VALUES ('G-01','breadth','ideation','open')"
    )
    conn.commit()
    row = conn.execute("SELECT label, phase, state, discharged_by_artifact FROM criterion WHERE criterion_id='G-01'").fetchone()
    assert row == ("breadth", "ideation", "open", None)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO criterion (criterion_id, label, phase, state) VALUES ('G-02','x','p','not-a-real-state')"
        )


def test_criterion_discharged_by_artifact_is_a_same_file_fk():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    conn.execute(
        "INSERT INTO template (type_key, title, version, path, gated) VALUES ('note','Note','1','p.md',0)"
    )
    conn.execute(
        "INSERT INTO artifact (artifact_id, type, title, path, sha256, status, registered_by_launch) "
        "VALUES ('ART-1','note','t','p.md',?,'registered','LNCH-1')",
        ("0" * 64,),
    )
    conn.execute(
        "INSERT INTO criterion (criterion_id, label, phase, state, discharged_by_artifact) "
        "VALUES ('G-01','breadth','ideation','discharged','ART-1')"
    )
    conn.commit()
    row = conn.execute("SELECT discharged_by_artifact FROM criterion WHERE criterion_id='G-01'").fetchone()
    assert row[0] == "ART-1"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO criterion (criterion_id, label, phase, state, discharged_by_artifact) "
            "VALUES ('G-02','x','p','discharged','ART-does-not-exist')"
        )


# ---- feed_post_translation ----------------------------------------------------


def test_feed_post_translation_table_created_with_expected_columns_and_checks():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feed_post_translation)").fetchall()}
    assert cols == {
        "translation_id", "post_id", "translator_version", "style_mode", "body",
        "original_sha256", "faithfulness_score", "faithfulness_verdict_id", "glossary_links",
        "status", "supersedes", "created_by_launch", "created_ts",
    }

    conn.execute(
        "INSERT INTO thread (thread_id, title, created_ts, created_by_launch) "
        "VALUES ('THR-1','t','2026-01-01T00:00:00.000Z','LNCH-1')"
    )
    conn.execute(
        "INSERT INTO feed_post (post_id, thread_id, author, ts, body) "
        "VALUES ('POST-1','THR-1','orchestrator:SESS-1','2026-01-01T00:00:00.000Z','hello')"
    )
    conn.execute(
        "INSERT INTO feed_post_translation "
        "(translation_id, post_id, translator_version, style_mode, body, original_sha256, "
        "status, created_ts) VALUES ('XLAT-1','POST-1','1','flavored','plain hello',?, 'current', "
        "'2026-01-01T00:00:01.000Z')",
        ("1" * 64,),
    )
    conn.commit()
    row = conn.execute("SELECT body, status FROM feed_post_translation WHERE translation_id='XLAT-1'").fetchone()
    assert row == ("plain hello", "current")

    # post_id is a same-file FK -> feed_post(post_id).
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feed_post_translation "
            "(translation_id, post_id, translator_version, style_mode, body, original_sha256, "
            "status, created_ts) VALUES ('XLAT-2','POST-does-not-exist','1','flavored','x',?, "
            "'current', '2026-01-01T00:00:01.000Z')",
            ("2" * 64,),
        )

    # style_mode CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feed_post_translation "
            "(translation_id, post_id, translator_version, style_mode, body, original_sha256, "
            "status, created_ts) VALUES ('XLAT-3','POST-1','1','loose','x',?, 'current', "
            "'2026-01-01T00:00:01.000Z')",
            ("3" * 64,),
        )


def test_feed_post_translation_created_by_launch_and_faithfulness_verdict_id_are_xids():
    """Both columns cross a store-file boundary (platform.launch,
    knowledge.verdict) -- registered in ``trialerror/stores/xid.py``, verified
    here at the schema level: ``created_by_launch``/``faithfulness_verdict_id``
    carry NO same-file ``REFERENCES`` clause (SQLite itself cannot enforce a
    cross-file FK), which is exactly why the XID registry entry exists."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='feed_post_translation'"
    ).fetchone()[0]
    assert "created_by_launch" in sql
    assert "REFERENCES" not in sql.split("created_by_launch")[1].split(",")[0]

    from trialerror.stores.xid import XID_REGISTRY, XidTarget

    assert XID_REGISTRY[("feed_post_translation", "created_by_launch")] == XidTarget("platform", "launch", "launch_id")
    assert XID_REGISTRY[("feed_post_translation", "faithfulness_verdict_id")] == XidTarget("knowledge", "verdict", "verdict_id")


# ---- failure-path rollback discipline -----------------------------------------


def test_v4_migration_failure_does_not_advance_version_or_partially_apply():
    conn = sqlite3.connect(":memory:")
    v1v2v3_only = _v_only(3)
    apply_migrations(conn, v1v2v3_only)
    before_snapshot = _schema_snapshot(conn)

    real_v4 = next(m for m in ops.MIGRATIONS if m.version == 4)
    broken = Migration(version=4, name=real_v4.name + "_broken_for_test", statements=real_v4.statements + ("THIS IS NOT SQL",))
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])

    assert current_version(conn) == 3  # never advanced to 4
    assert _schema_snapshot(conn) == before_snapshot  # not one statement survived
    conn.execute("PRAGMA foreign_keys")
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")  # would raise if a transaction leaked
