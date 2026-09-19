"""lane C step C7: per-migration proving tests for ops-v8
(``trialerror/stores/schema/ops.py``'s ``_V8`` / ``Migration(version=8,
name="ops_v8_thread_created_by_nullable_and_author", ...)``) -- the one
migration lane C takes.

Two column changes on ``thread``, both in service of one verb the dashboard
could not offer: ``created_by_launch`` becomes NULLABLE, and a derived
``created_by`` joins it. Before this, the operator -- who has a session and no
launch -- could post INTO a thread and never START one.

The interesting part of the migration is not the columns; it is that ``thread``
has a same-file FK CHILD with rows in it (``feed_post.thread_id``), so the
table-rebuild recipe only works because ``stores/migrate.py`` brackets each
migration with ``PRAGMA foreign_keys`` OFF/ON. Half of this file exists to
prove that the child's rows survive intact and its FK still points at the
rebuilt parent afterwards.

Numbered v8, not v7: the 2026-09 mining-adoptions lane's ``memory_relation``
migration merged first and took v7 (ruling L-C1 as amended). Follows
``tests/test_stores_migrate_v6_feed_translation_gate.py``'s pattern verbatim.
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


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _seed_thread_and_posts(conn: sqlite3.Connection) -> None:
    """A pre-v8 thread with two posts under it, one of them a reply -- the
    exact shape the rebuild has to carry through untouched."""
    conn.execute(
        "INSERT INTO thread (thread_id, title, created_ts, created_by_launch, status, refs) "
        "VALUES ('THR-1','the old thread','2026-01-01T00:00:00.000Z','LNCH-1','archived','{\"k\":1}')"
    )
    conn.execute(
        "INSERT INTO feed_post (post_id, thread_id, author, launch_id, ts, body) "
        "VALUES ('POST-1','THR-1','lens:LNCH-1','LNCH-1','2026-01-01T00:00:01.000Z','first')"
    )
    conn.execute(
        "INSERT INTO feed_post (post_id, thread_id, author, launch_id, ts, body, in_reply_to) "
        "VALUES ('POST-2','THR-1','lens:LNCH-1','LNCH-1','2026-01-01T00:00:02.000Z','second','POST-1')"
    )


# ---------------------------------------------------------------------------
# fresh-create vs. migrate-from-v7 land identical schemas
# ---------------------------------------------------------------------------


def test_fresh_create_and_migrate_from_v7_land_identical_schemas():
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, ops.MIGRATIONS)

    migrated = sqlite3.connect(":memory:")
    applied_first = apply_migrations(migrated, _v_only(7))
    assert applied_first == [1, 2, 3, 4, 5, 6, 7]
    applied_rest = apply_migrations(migrated, ops.MIGRATIONS)
    assert applied_rest == sorted(m.version for m in ops.MIGRATIONS if m.version > 7)

    # No frozen tip number -- the same reasoning v6's own file records. What
    # is pinned is that v8 actually ran and that a step-wise migration and a
    # from-empty create land the SAME schema, which survives later additions.
    assert 8 in applied_rest
    assert current_version(fresh) == current_version(migrated) == latest_version(ops.MIGRATIONS)
    assert _schema_snapshot(fresh) == _schema_snapshot(migrated)


def test_v8_migration_is_idempotent_reapply_is_noop():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    before = _schema_snapshot(conn)
    assert apply_migrations(conn, ops.MIGRATIONS) == []
    assert _schema_snapshot(conn) == before


# ---------------------------------------------------------------------------
# the two column changes
# ---------------------------------------------------------------------------


def test_created_by_launch_is_nullable_after_v8_and_was_not_before():
    """Non-vacuity, in one test: the same INSERT is refused at v7 and accepted
    at v8. Asserting only the post-state would pass against a schema that had
    always allowed it."""
    at_v7 = sqlite3.connect(":memory:")
    apply_migrations(at_v7, _v_only(7))
    assert _columns(at_v7, "thread")["created_by_launch"][3] == 1, "notnull flag set at v7"
    with pytest.raises(sqlite3.IntegrityError):
        at_v7.execute(
            "INSERT INTO thread (thread_id, title, created_ts, created_by_launch) "
            "VALUES ('THR-X','t','2026-01-01T00:00:00.000Z', NULL)"
        )

    at_v8 = sqlite3.connect(":memory:")
    apply_migrations(at_v8, ops.MIGRATIONS)
    assert _columns(at_v8, "thread")["created_by_launch"][3] == 0
    at_v8.execute(
        "INSERT INTO thread (thread_id, title, created_ts, created_by_launch, created_by) "
        "VALUES ('THR-X','t','2026-01-01T00:00:00.000Z', NULL, 'orchestrator:SESS-1')"
    )
    row = at_v8.execute("SELECT created_by_launch, created_by FROM thread WHERE thread_id='THR-X'").fetchone()
    assert row == (None, "orchestrator:SESS-1")


def test_created_by_is_new_and_nullable():
    at_v7 = sqlite3.connect(":memory:")
    apply_migrations(at_v7, _v_only(7))
    assert "created_by" not in _columns(at_v7, "thread")

    at_v8 = sqlite3.connect(":memory:")
    apply_migrations(at_v8, ops.MIGRATIONS)
    col = _columns(at_v8, "thread")["created_by"]
    assert col[2] == "TEXT"
    assert col[3] == 0, "nullable: a pre-v8 row has no author this migration could derive"


def test_v2s_status_and_refs_columns_survive_the_rebuild():
    """A rebuild that forgets a column added by an earlier migration is the
    classic way this recipe goes wrong, and it goes wrong SILENTLY -- the
    column is simply gone."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = _columns(conn, "thread")
    assert cols["status"][3] == 1 and cols["status"][4] == "'active'"
    assert "refs" in cols
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'thread'").fetchone()[0]
    assert "CHECK (status IN ('active','archived'))" in sql, "v2's CHECK is carried, not dropped"


# ---------------------------------------------------------------------------
# the rebuild itself -- rows, and the FK child
# ---------------------------------------------------------------------------


def test_existing_rows_are_carried_through_with_created_by_null():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _v_only(7))
    _seed_thread_and_posts(conn)
    conn.commit()

    apply_migrations(conn, ops.MIGRATIONS)

    row = conn.execute(
        "SELECT thread_id, title, created_ts, created_by_launch, created_by, status, refs "
        "FROM thread WHERE thread_id = 'THR-1'"
    ).fetchone()
    assert row == (
        "THR-1", "the old thread", "2026-01-01T00:00:00.000Z", "LNCH-1", None, "archived", '{"k":1}'
    )
    # created_by is NULL rather than backfilled: deriving one needs a
    # platform.launch lookup, and platform is a different FILE -- a migration
    # on ops.db cannot reach it. Readers fall back to created_by_launch.


def test_the_fk_child_survives_a_parent_rebuild_with_rows_in_it():
    """``feed_post.thread_id REFERENCES thread(thread_id)`` is a same-file FK,
    and SQLite refuses a bare DROP of a still-referenced parent under
    enforcement. This passes only because ``apply_migrations`` brackets the
    whole migration with ``PRAGMA foreign_keys`` OFF/ON."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _v_only(7))
    _seed_thread_and_posts(conn)
    conn.commit()

    apply_migrations(conn, ops.MIGRATIONS)

    posts = conn.execute("SELECT post_id, thread_id, in_reply_to FROM feed_post ORDER BY post_id").fetchall()
    assert posts == [("POST-1", "THR-1", None), ("POST-2", "THR-1", "POST-1")]

    # the FK still points at the rebuilt parent, and is still enforced
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feed_post (post_id, thread_id, author, ts, body) "
            "VALUES ('POST-9','THR-nope','x','2026-01-01T00:00:00.000Z','y')"
        )


def test_foreign_keys_are_re_enabled_after_the_migration_runs():
    """The bracketing is OFF/ON, not OFF: leaving a caller's connection
    permanently FK-unchecked would be a far worse bug than the one the toggle
    exists to work around."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, ops.MIGRATIONS)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_no_scratch_table_is_left_behind():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "thread__v8new" not in names
    assert "thread" in names


# ---------------------------------------------------------------------------
# failure-path discipline
# ---------------------------------------------------------------------------


def test_a_failing_v8_rolls_back_whole_and_leaves_the_version_pointer_alone():
    """Each migration is one transaction: a failure partway through must not
    leave a half-rebuilt thread table behind a bumped user_version -- which,
    on THIS migration, would mean a dropped table and no replacement."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _v_only(7))
    _seed_thread_and_posts(conn)
    conn.commit()
    before = _schema_snapshot(conn)

    broken = _v_only(7) + (
        Migration(
            version=8,
            name="ops_v8_thread_created_by_nullable_and_author",
            statements=ops._V8[:-1] + ("THIS IS NOT SQL",),
        ),
    )
    with pytest.raises(MigrationError) as exc:
        apply_migrations(conn, broken)
    assert "migration 8" in str(exc.value)

    assert current_version(conn) == 7
    assert _schema_snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM thread").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM feed_post").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# the XID entry is unchanged, and still enforced for a non-null value
# ---------------------------------------------------------------------------


def test_the_xid_entry_for_created_by_launch_is_unchanged():
    """"Null allowed, non-null still validated" is not a new rule this
    migration writes -- it falls out of ``_validate_xids``, which already
    skips a NULL column value. The registry entry must therefore stay exactly
    as it was; removing it would stop validating the non-null case."""
    from trialerror.stores.xid import XID_REGISTRY

    target = XID_REGISTRY[("thread", "created_by_launch")]
    assert (target.db, target.table, target.pk_column) == ("platform", "launch", "launch_id")
