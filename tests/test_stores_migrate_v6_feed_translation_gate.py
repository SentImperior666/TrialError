"""lane-b-translator: per-migration proving tests for ops-v6
(``trialerror/stores/schema/ops.py``'s ``_V6`` / ``Migration(version=6,
name="ops_v6_feed_post_translation_gate_verdict", ...)``) -- the two
additive columns the AISPEAK translator's fail-closed gate writes:
``feed_post_translation.gate_status`` and ``.gate_reasons``.

Numbered v6, not v5: this migration was authored as "ops_v5" against the
branch point, but master independently landed its own, unrelated ops v5
first (FU-14's "ops_v5_meta_kv"). Renumbered here (B1, fix pass) so the
two lands merge as a visible conflict rather than a silently-shadowed
module-level constant -- see ``ops.py``'s TRIALERROR-DEV-NOTE at ``_V6``.

Follows ``tests/test_stores_migrate_v4_dashboard.py``'s pattern verbatim
(fresh-create-vs-migrate-from-vN schema diff, per-DDL-change proving
tests, failure-path rollback discipline), which follows v3's before it.
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


def _seed_post(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO thread (thread_id, title, created_ts, created_by_launch) "
        "VALUES ('THR-1','t','2026-01-01T00:00:00.000Z','LNCH-1')"
    )
    conn.execute(
        "INSERT INTO feed_post (post_id, thread_id, author, ts, body) "
        "VALUES ('POST-1','THR-1','lens:LNCH-1','2026-01-01T00:00:00.000Z','LNCH-1 booking may be deferred')"
    )


def _insert_v4_translation(conn: sqlite3.Connection, translation_id: str = "XLAT-1") -> None:
    conn.execute(
        "INSERT INTO feed_post_translation "
        "(translation_id, post_id, translator_version, style_mode, body, original_sha256, status, created_ts) "
        "VALUES (?, 'POST-1', '1', 'flavored', 'plain text', 'a', 'current', '2026-01-01T00:00:00.000Z')",
        (translation_id,),
    )


# ---------------------------------------------------------------------------
# fresh-create vs. migrate-from-v4 land identical schemas
# ---------------------------------------------------------------------------


def test_fresh_create_and_migrate_from_v4_land_identical_schemas():
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, ops.MIGRATIONS)  # v1..vN in one call, from empty

    migrated = sqlite3.connect(":memory:")
    applied_first = apply_migrations(migrated, _v_only(4))
    assert applied_first == [1, 2, 3, 4]
    applied_rest = apply_migrations(migrated, ops.MIGRATIONS)  # every version > 4 that exists in THIS branch
    assert applied_rest == sorted(m.version for m in ops.MIGRATIONS if m.version > 4)

    assert current_version(fresh) == current_version(migrated) == latest_version(ops.MIGRATIONS) == 6
    assert _schema_snapshot(fresh) == _schema_snapshot(migrated)


def test_v6_migration_is_idempotent_reapply_is_noop():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    assert apply_migrations(conn, ops.MIGRATIONS) == []
    assert current_version(conn) == latest_version(ops.MIGRATIONS)


# ---------------------------------------------------------------------------
# the two columns
# ---------------------------------------------------------------------------


def test_gate_columns_exist_after_v6():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feed_post_translation)").fetchall()}
    assert {"gate_status", "gate_reasons"} <= cols


def test_pre_v6_rows_backfill_to_ungated_not_to_fail():
    """The whole point of a third value: a translation stored before the
    gate existed was never CHECKED, and must not be reported as having
    FAILED (which would make the dashboard withhold it and doctor count it
    as a translator defect)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _v_only(4))
    _seed_post(conn)
    _insert_v4_translation(conn)
    conn.commit()  # close the implicit DML transaction before the migration's BEGIN IMMEDIATE

    # every version above 4 that exists on the merged branch: master's v5
    # (FU-14's meta kv table) and this lane's v6 -- asserted as "the rest of
    # the list" like test_v6_fresh_create_matches_migrate_from_v4 does.
    assert apply_migrations(conn, ops.MIGRATIONS) == sorted(m.version for m in ops.MIGRATIONS if m.version > 4)
    row = conn.execute(
        "SELECT gate_status, gate_reasons, body FROM feed_post_translation WHERE translation_id = 'XLAT-1'"
    ).fetchone()
    assert row[0] == "ungated"
    assert row[1] is None
    assert row[2] == "plain text"  # the pre-existing row is untouched otherwise


def test_gate_status_check_constraint_rejects_an_unknown_value():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    _seed_post(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            "INSERT INTO feed_post_translation "
            "(translation_id, post_id, translator_version, style_mode, body, original_sha256, status, "
            " created_ts, gate_status) "
            "VALUES ('XLAT-bad','POST-1','1','flavored','b','a','current','2026-01-01T00:00:00.000Z','maybe')"
        )


def test_gate_status_accepts_pass_fail_ungated():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    _seed_post(conn)
    for i, value in enumerate(("pass", "fail", "ungated")):
        conn.execute(
            "INSERT INTO feed_post_translation "
            "(translation_id, post_id, translator_version, style_mode, body, original_sha256, status, "
            " created_ts, gate_status, gate_reasons) "
            "VALUES (?, 'POST-1','1','flavored','b','a','superseded','2026-01-01T00:00:00.000Z', ?, '{}')",
            (f"XLAT-{i}", value),
        )
    got = {r[0] for r in conn.execute("SELECT gate_status FROM feed_post_translation")}
    assert got == {"pass", "fail", "ungated"}


def test_v6_creates_the_gate_status_index():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'feed_post_translation'"
        )
    }
    assert "idx_feed_post_translation_gate" in names


# ---- failure-path rollback discipline -----------------------------------------


def test_v6_migration_failure_does_not_advance_version_or_partially_apply():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _v_only(4))
    before_snapshot = _schema_snapshot(conn)

    real_v6 = next(m for m in ops.MIGRATIONS if m.version == 6)
    broken = Migration(
        version=6, name=real_v6.name + "_broken_for_test", statements=real_v6.statements + ("THIS IS NOT SQL",)
    )
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])

    assert current_version(conn) == 4  # never advanced to 6
    assert _schema_snapshot(conn) == before_snapshot  # not one ALTER survived
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")  # would raise if a transaction leaked
