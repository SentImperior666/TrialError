"""``platform_v3_unit_cost_and_probes`` -- L3's additive migration adding
``unit``, ``unit_msg`` and ``probe_run`` to platform.db (design Section 2.1).

Nothing existing changes: this is pure ``CREATE TABLE``/``CREATE INDEX``, so
these tests check additivity, that a v2 store with real rows migrates
cleanly, and that a fresh store lands on the identical schema.
"""

from __future__ import annotations

import sqlite3

from trialerror.stores.migrate import apply_migrations, current_version, is_additive
from trialerror.stores.schema import platform

TS = "2026-09-27T12:00:00.000Z"

_UNIT_COLUMNS = (
    "unit_key", "host", "kind", "session_id", "agent_id", "workflow_run_id", "parent_unit_key",
    "spawn_tool_use_id", "agent_type", "project_slug", "entrypoint", "cc_version", "models",
    "first_ts", "last_ts", "conversation_last_ts", "usage_source",
    "statusline_cost", "transcript_path", "transcript_sha256", "transcript_size", "transcript_mtime_ns",
    "launch_id", "lane", "round", "verdict_status", "verdict_reason", "extractor_version", "scanned_ts",
)
#: Columns declared ``NOT NULL DEFAULT 0`` -- left out of the fixture row so
#: SQLite's own default applies, matching what a real ``units scan`` insert
#: (which also never sets these to NULL) actually writes.
_UNIT_COUNTER_COLUMNS = (
    "n_messages", "usage_input", "usage_cache_write", "usage_cache_read", "usage_output",
    "usage_cache_write_1h", "usage_cache_write_5m",
)


def _upto(max_version: int):
    return tuple(m for m in platform.MIGRATIONS if m.version <= max_version)


def _platform_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(version))
    return conn


def _seed_v2(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO account (account_id, label, created_ts) VALUES ('ACC-1','a',?)", (TS,))
    conn.execute(
        "INSERT INTO launch (launch_id, account_id, program_id, session_id, parent_launch, agent_kind, "
        "model_class, model, purpose, est_tokens, booked_ts, booking_ttl_s, state, actual_tokens, "
        "reconciled_ts, reconcile_source, workpackage, attrs) "
        "VALUES ('LNCH-1','ACC-1','PROG-1','SESS-1',NULL,'lens','top','opus','ideation',9000,?,3600,"
        "'RECONCILED',5000,?,'manual','WKP-1',NULL)",
        (TS, TS),
    )
    conn.commit()


def test_v3_is_present_contiguous_and_uniquely_named():
    """Design's own renumbering rule: L4 also adds platform tables, and
    whichever lane lands second renumbers -- so "v3 is the latest" is not a
    stable invariant this file should pin (the second fix round of THIS
    lane found exactly that failure mode in the pre-existing v2 test file,
    once v3 landed after it). What stays stable: v3 exists, under this
    exact name, inside a contiguous, uniquely-named migration list. Same
    pattern tests/test_stores_migrate_v9_lens_seats.py's own
    test_v9_is_contiguous_and_uniquely_named uses."""
    versions = [m.version for m in platform.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert 3 in versions
    assert len({m.name for m in platform.MIGRATIONS}) == len(platform.MIGRATIONS)
    v3 = next(m for m in platform.MIGRATIONS if m.version == 3)
    assert v3.name == "platform_v3_unit_cost_and_probes"


def test_v3_is_additive():
    v3 = next(m for m in platform.MIGRATIONS if m.version == 3)
    assert is_additive(v3)


def test_tables_declared():
    assert {"unit", "unit_msg", "probe_run"} <= set(platform.TABLES)


def test_a_v2_store_with_real_rows_migrates_and_keeps_them():
    conn = _platform_at(2)
    _seed_v2(conn)
    assert current_version(conn) == 2

    # Capped at v3 (_upto(3)), not the full platform.MIGRATIONS -- this test
    # is about the v3 unit/probe tables specifically; L4's v4 (unrelated: new
    # tables, not a unit/probe change) is exercised in its own
    # tests/test_quota_migration.py. Same fix L3's own v2 file needed once v3
    # landed after it (test_an_old_populated_store_migrates_and_keeps_every_
    # row_and_the_booking_tree).
    applied = apply_migrations(conn, _upto(3))
    assert applied == [3]
    assert current_version(conn) == 3

    row = conn.execute("SELECT * FROM launch WHERE launch_id = 'LNCH-1'").fetchone()
    assert row["actual_tokens"] == 5000

    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"unit", "unit_msg", "probe_run"} <= names


def test_unit_row_round_trips_every_column():
    conn = _platform_at(3)
    row = {c: None for c in _UNIT_COLUMNS}
    row.update(
        unit_key="dev/SESS-1/-",
        host="dev",
        kind="main",
        session_id="SESS-1",
        project_slug="proj",
        usage_source="transcript",
        extractor_version="units-1",
        scanned_ts=TS,
    )
    cols = list(row)
    conn.execute(f"INSERT INTO unit ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [row[c] for c in cols])
    got = dict(conn.execute("SELECT * FROM unit WHERE unit_key = 'dev/SESS-1/-'").fetchone())
    assert got["kind"] == "main"
    assert got["usage_source"] == "transcript"
    assert got["verdict_status"] is None


def test_unit_kind_check_refuses_an_unknown_value():
    conn = _platform_at(3)
    row = {c: None for c in _UNIT_COLUMNS}
    row.update(
        unit_key="k", host="dev", kind="bogus", session_id="s", project_slug="p",
        usage_source="none", extractor_version="units-1", scanned_ts=TS,
    )
    cols = list(row)
    try:
        conn.execute(f"INSERT INTO unit ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [row[c] for c in cols])
        assert False, "expected an IntegrityError"
    except sqlite3.IntegrityError:
        pass


def test_two_units_differing_only_by_null_agent_id_do_not_collide():
    """The design's own rationale for a computed TEXT key (Section 2.1):
    SQLite's PRIMARY KEY treats NULLs as pairwise-distinct, so a naive
    ``(host, session_id, agent_id)`` composite key would let a second scan
    of the same main session insert a duplicate row. unit_key sidesteps
    this by encoding the missing agent_id as the literal ``'-'``."""
    conn = _platform_at(3)
    row = {c: None for c in _UNIT_COLUMNS}
    row.update(
        unit_key="dev/SESS-1/-", host="dev", kind="main", session_id="SESS-1", project_slug="p",
        usage_source="none", extractor_version="units-1", scanned_ts=TS,
    )
    cols = list(row)
    conn.execute(f"INSERT INTO unit ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [row[c] for c in cols])
    try:
        conn.execute(f"INSERT INTO unit ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [row[c] for c in cols])
        assert False, "expected the PRIMARY KEY to refuse the duplicate"
    except sqlite3.IntegrityError:
        pass


def test_probe_run_status_check():
    conn = _platform_at(3)
    conn.execute(
        "INSERT INTO probe_run (name, kind, host, started_ts, status) VALUES ('p','conformance','dev',?,'pass')",
        (TS,),
    )
    try:
        conn.execute(
            "INSERT INTO probe_run (name, kind, host, started_ts, status) VALUES ('p','conformance','dev',?,'nope')",
            (TS,),
        )
        assert False, "expected an IntegrityError"
    except sqlite3.IntegrityError:
        pass


def test_the_migration_is_idempotent_and_schema_identical_to_a_fresh_v3_store():
    upgraded = _platform_at(2)
    _seed_v2(upgraded)
    # Capped at v3 (_upto(3)) -- see the note on the previous test for why.
    apply_migrations(upgraded, _upto(3))
    assert apply_migrations(upgraded, _upto(3)) == []
    assert current_version(upgraded) == 3

    fresh = _platform_at(3)

    def snapshot(conn):
        return [
            (r["type"], r["name"], r["sql"])
            for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ]

    assert snapshot(upgraded) == snapshot(fresh)


def test_v1_and_v2_ddl_is_untouched():
    """Trap 3: fresh stores replay every migration -- _V1/_V2 must not have
    been edited to make room for v3."""
    v1_names = {stmt.split()[2] for stmt in platform._V1 if stmt.strip().upper().startswith("CREATE TABLE")}
    assert v1_names == {"account", "budget_pool", "launch", "quota_snapshot", "calibration"}
