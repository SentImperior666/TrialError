"""schema-v2 (build-v1-schemav2): per-migration proving tests for the four
DDL changes in docs/the migration-plan notes (internal, not in this export) Section 4 / docs/INTEGRATION_NOTES.md
items 8+14, plus the migration-discipline test the mission requires:
"fresh-create path and migrate-path (v1->v2) must land IDENTICAL schemas —
add a test that creates fresh vs migrates-from-v1 and diffs sqlite_master."

Each schema module's ``MIGRATIONS`` tuple is the SAME code path for both
scenarios (``trialerror.stores.migrate.apply_migrations`` always walks forward
from the DB's current ``PRAGMA user_version``, whatever it is) -- a truly
fresh DB already applies v1 then v2 in one ``apply_migrations`` call; a
"migrated" DB applies v1 first (simulating a pre-existing v1 install), then
v2 separately. This test's real job is to catch a regression where v1's own
DDL (``_V1``) was edited in place instead of adding a proper v2 migration --
that would still pass "migration up from empty" (tests/test_stores_migrate.py,
which always starts from empty) but would silently break every real
pre-existing v1 database, since apply_migrations would then never re-apply
the (already-"applied", per ``user_version``) v1 statements to bring it
in line.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import Migration, apply_migrations, current_version, latest_version
from trialerror.stores.schema import jobs, knowledge, ops

#: the three schema modules this build's v2 migrations touch (platform.db
#: has no v2 migration -- the migration-plan notes (internal, not in this export) Section 4 names no platform
#: change).
_V2_MODULES = [ops, jobs, knowledge]
_V2_MODULE_IDS = ["ops", "jobs", "knowledge"]


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """``(type, name, sql)`` for every non-internal schema object, in a
    stable order -- what "diff sqlite_master" means here. ``sqlite_%``
    (the internal sequence table, etc.) is excluded: its presence/absence
    depends on incidental AUTOINCREMENT bookkeeping, not on the schema
    itself."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


@pytest.fixture(params=_V2_MODULES, ids=_V2_MODULE_IDS)
def v2_schema_module(request):
    return request.param


def test_fresh_create_and_migrate_from_v1_land_identical_schemas(v2_schema_module):
    """The mission's own migration-discipline requirement, verbatim.

    build-v2-polish/build-v2-summary note: ops.py and knowledge.py each
    independently grew a v3 migration AFTER this test was written (still
    only jobs.py stays at exactly one post-v1 migration) -- the "only v2 is
    newer now" / hardcoded ``== [2]``/``== 2`` this test used to assert
    would have silently stopped covering v3 for those two modules (a v3
    that was itself never applied would still make ``applied_v2 == [2]``
    true). Generalized to "every version > 1", so this test keeps proving
    the real thing (fresh-create vs. migrate-from-v1 land identical
    schemas) regardless of how many post-v1 migrations a module has grown
    since."""
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, v2_schema_module.MIGRATIONS)  # v1..vN in one call, from empty

    migrated = sqlite3.connect(":memory:")
    v1_only = tuple(m for m in v2_schema_module.MIGRATIONS if m.version == 1)
    applied_v1 = apply_migrations(migrated, v1_only)
    assert applied_v1 == [1]  # sanity: v1 really did apply on its own first
    applied_rest = apply_migrations(migrated, v2_schema_module.MIGRATIONS)  # every version > 1
    expected_rest = sorted(m.version for m in v2_schema_module.MIGRATIONS if m.version > 1)
    assert applied_rest == expected_rest

    latest = latest_version(v2_schema_module.MIGRATIONS)
    assert current_version(fresh) == current_version(migrated) == latest
    assert _schema_snapshot(fresh) == _schema_snapshot(migrated)


def test_v2_migration_is_idempotent_reapply_is_noop(v2_schema_module):
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, v2_schema_module.MIGRATIONS)
    snapshot_after_first = _schema_snapshot(conn)

    second = apply_migrations(conn, v2_schema_module.MIGRATIONS)
    assert second == []
    assert _schema_snapshot(conn) == snapshot_after_first


# ---- item 1: ops.memory_item.account_id NOT NULL -> nullable --------------


def test_ops_v2_memory_item_account_id_is_nullable_after_migration():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(memory_item)").fetchall()}
    assert cols["account_id"][3] == 0  # PRAGMA table_info's notnull column: 0 = nullable

    conn.execute(
        "INSERT INTO memory_item (memory_item_id, key, tier, kind, body, updated_ts, account_id) "
        "VALUES ('MEM-1', 'k', 'L0', 'rule', 'body text', 't', NULL)"
    )
    conn.commit()
    row = conn.execute("SELECT account_id, status FROM memory_item WHERE memory_item_id = 'MEM-1'").fetchone()
    assert row[0] is None
    assert row[1] == "active"  # status default survived the table-rebuild


def test_ops_v2_memory_item_preserves_existing_rows_and_other_constraints():
    """The table-rebuild recipe must not silently drop pre-existing v1 data,
    and every OTHER constraint (the CHECKs on tier/kind, the NOT NULLs on
    key/body/updated_ts) must survive unchanged."""
    conn = sqlite3.connect(":memory:")
    v1_only = tuple(m for m in ops.MIGRATIONS if m.version == 1)
    apply_migrations(conn, v1_only)
    conn.execute(
        "INSERT INTO memory_item (memory_item_id, key, tier, kind, body, updated_ts, account_id) "
        "VALUES ('MEM-preexisting', 'k', 'L1', 'fact', 'pre-v2 body', 't', 'ACC-1')"
    )
    conn.commit()

    apply_migrations(conn, ops.MIGRATIONS)  # v2 now applies on top of real v1 data

    row = conn.execute(
        "SELECT key, tier, kind, body, account_id, status FROM memory_item WHERE memory_item_id = 'MEM-preexisting'"
    ).fetchone()
    assert row == ("k", "L1", "fact", "pre-v2 body", "ACC-1", "active")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_item (memory_item_id, key, tier, kind, body, updated_ts, account_id) "
            "VALUES ('MEM-bad', 'k', 'not-a-tier', 'fact', 'x', 't', NULL)"
        )


# ---- item 4: ops.thread.status/refs -----------------------------------


def test_ops_v2_thread_gains_status_default_and_refs():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    conn.execute("INSERT INTO thread (thread_id, title, created_ts, created_by_launch) VALUES ('THR-1','t','ts','LNCH-1')")
    conn.commit()
    row = conn.execute("SELECT status, refs FROM thread WHERE thread_id = 'THR-1'").fetchone()
    assert row == ("active", None)

    conn.execute("UPDATE thread SET refs = ? WHERE thread_id = 'THR-1'", (json.dumps(["ART-1", "C-0001"]),))
    conn.execute("UPDATE thread SET status = 'archived' WHERE thread_id = 'THR-1'")
    conn.commit()
    row = conn.execute("SELECT status, refs FROM thread WHERE thread_id = 'THR-1'").fetchone()
    assert row[0] == "archived"
    assert json.loads(row[1]) == ["ART-1", "C-0001"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE thread SET status = 'bogus' WHERE thread_id = 'THR-1'")


# ---- item 2: jobs.job.kind CHECK gains normalize/chunk --------------------


def test_jobs_v2_kind_check_accepts_normalize_and_chunk():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, jobs.MIGRATIONS)
    for kind in ("normalize", "chunk"):
        conn.execute(
            f"INSERT INTO job (job_id, kind, payload, state, created_ts) VALUES ('JOB-{kind}', ?, '{{}}', 'pending', 't')",
            (kind,),
        )
    conn.commit()
    got = {r[0] for r in conn.execute("SELECT kind FROM job").fetchall()}
    assert got == {"normalize", "chunk"}

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO job (job_id, kind, payload, state, created_ts) VALUES ('JOB-bad', 'bogus', '{}', 'pending', 't')")


def test_jobs_v2_preserves_existing_job_and_job_event_rows_and_fk():
    """The table-rebuild recipe on ``job`` (the FK PARENT) must not orphan
    or lose ``job_event`` (the FK CHILD) rows, and the FK must still be
    live and enforced against the rebuilt parent afterward."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    v1_only = tuple(m for m in jobs.MIGRATIONS if m.version == 1)
    apply_migrations(conn, v1_only)
    conn.execute("INSERT INTO job (job_id, kind, payload, state, created_ts) VALUES ('JOB-1', 'embed', '{}', 'pending', 't1')")
    conn.execute("INSERT INTO job_event (job_id, ts, type) VALUES ('JOB-1', 't1', 'created')")
    conn.commit()

    apply_migrations(conn, jobs.MIGRATIONS)  # v2 rebuilds job; job_event must survive untouched

    job_row = conn.execute("SELECT kind, state FROM job WHERE job_id = 'JOB-1'").fetchone()
    assert job_row == ("embed", "pending")
    event_rows = conn.execute("SELECT job_id, type FROM job_event WHERE job_id = 'JOB-1'").fetchall()
    assert event_rows == [("JOB-1", "created")]

    # FK still live against the REBUILT job table (not silently dropped).
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO job_event (job_id, ts, type) VALUES ('JOB-does-not-exist', 't2', 'x')")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # idx_job_state (dropped along with the old job table) was re-created.
    idx = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_job_state'").fetchone()
    assert idx is not None


# ---- item 3: knowledge.idea promoted columns -------------------------


def test_knowledge_v2_idea_gains_promoted_columns_with_tier_check():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, knowledge.MIGRATIONS)
    conn.execute(
        "INSERT INTO idea (idea_id, author_launch, body, status, created_ts, home, assumed_circle, provenance, tier, set_distance) "
        "VALUES ('IDEA-1','LNCH-1','body','raw','t','home-doc','skeptics','{\"source\":\"r1\"}','far',0.5)"
    )
    conn.commit()
    row = conn.execute(
        "SELECT home, assumed_circle, provenance, tier, set_distance FROM idea WHERE idea_id='IDEA-1'"
    ).fetchone()
    assert row == ("home-doc", "skeptics", '{"source":"r1"}', "far", 0.5)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO idea (idea_id, author_launch, body, status, created_ts, tier) "
            "VALUES ('IDEA-bad','LNCH-1','body','raw','t','bogus-tier')"
        )


def test_knowledge_v2_preserves_existing_idea_rows_promoted_columns_null():
    conn = sqlite3.connect(":memory:")
    v1_only = tuple(m for m in knowledge.MIGRATIONS if m.version == 1)
    apply_migrations(conn, v1_only)
    conn.execute(
        "INSERT INTO idea (idea_id, author_launch, body, status, created_ts, slice_ref) "
        "VALUES ('IDEA-old','LNCH-1','pre-v2 body','raw','t','{\"assign_id\":\"ASGN-1\"}')"
    )
    conn.commit()

    apply_migrations(conn, knowledge.MIGRATIONS)

    row = conn.execute(
        "SELECT body, slice_ref, home, tier, set_distance FROM idea WHERE idea_id='IDEA-old'"
    ).fetchone()
    assert row == ("pre-v2 body", '{"assign_id":"ASGN-1"}', None, None, None)


# ---- failure-path rollback discipline (M1's pattern, per-DB) --------------


@pytest.mark.parametrize("schema_module", _V2_MODULES, ids=_V2_MODULE_IDS)
def test_v2_migration_failure_does_not_advance_version_or_partially_apply(schema_module):
    """M1's own failure-path discipline (tests/test_stores_migrate.py::
    test_migration_failure_does_not_advance_version), re-proven for a v2
    migration whose statement list is a real multi-statement table-rebuild
    (not the toy single-CREATE-TABLE case M1's own test covers) -- a
    mid-rebuild failure must roll back EVERY statement already run in that
    migration, not just the last one, leaving the DB exactly as v1 left it."""
    conn = sqlite3.connect(":memory:")
    v1_only = tuple(m for m in schema_module.MIGRATIONS if m.version == 1)
    apply_migrations(conn, v1_only)
    before_snapshot = _schema_snapshot(conn)

    real_v2 = next(m for m in schema_module.MIGRATIONS if m.version == 2)
    broken = Migration(
        version=2,
        name=real_v2.name + "_broken_for_test",
        statements=real_v2.statements + ("THIS IS NOT SQL",),
    )
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])

    assert current_version(conn) == 1  # never advanced to 2
    assert _schema_snapshot(conn) == before_snapshot  # not one statement survived
    # the connection is left usable (foreign_keys restored, no lingering
    # open transaction) -- a plain DML statement must still work afterward.
    conn.execute("PRAGMA foreign_keys")
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")  # would raise "cannot start a transaction within a transaction" if one leaked
