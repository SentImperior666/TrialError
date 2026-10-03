"""``platform_v4_quota_policy`` -- L4's additive migration adding
``quota_capture``, ``quota_rate`` and ``quota_notice`` to platform.db
(design ``L4_quota-policy.md`` Section 2.2 / Section 3 item 8).

Pure ``CREATE TABLE``/``CREATE INDEX``, mirroring
``tests/test_stores_migrate_platform_v3.py``: additivity, a v3 store with
real rows migrates cleanly, and a fresh store lands on the identical
schema.
"""

from __future__ import annotations

import sqlite3

from trialerror.stores.migrate import apply_migrations, current_version, is_additive, latest_version
from trialerror.stores.schema import platform

TS = "2026-09-27T12:00:00.000Z"


def _upto(max_version: int):
    return tuple(m for m in platform.MIGRATIONS if m.version <= max_version)


def _platform_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(version))
    return conn


def _seed_v3(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO account (account_id, label, created_ts) VALUES ('ACC-1','a',?)", (TS,))
    conn.execute(
        "INSERT INTO unit (unit_key, host, kind, session_id, project_slug, usage_source, "
        "extractor_version, scanned_ts) VALUES ('dev/SESS-1/-','dev','main','SESS-1','p','none','units-1',?)",
        (TS,),
    )
    conn.commit()


def test_v4_is_the_latest_of_the_capped_helper():
    # Pinned to v4's own prefix: later migrations have their own tests.
    assert latest_version(_upto(4)) == 4


def test_v4_is_additive():
    v4 = next(m for m in platform.MIGRATIONS if m.version == 4)
    assert is_additive(v4)


def test_tables_declared():
    assert {"quota_capture", "quota_rate", "quota_notice"} <= set(platform.TABLES)


def test_a_v3_store_with_real_rows_migrates_and_keeps_them():
    conn = _platform_at(3)
    _seed_v3(conn)
    assert current_version(conn) == 3

    applied = apply_migrations(conn, _upto(4))
    assert applied == [4]
    assert current_version(conn) == 4

    row = conn.execute("SELECT * FROM unit WHERE unit_key = 'dev/SESS-1/-'").fetchone()
    assert row["session_id"] == "SESS-1"

    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"quota_capture", "quota_rate", "quota_notice"} <= names


def test_quota_capture_unique_constraint_and_round_trip():
    conn = _platform_at(4)
    conn.execute(
        "INSERT INTO quota_capture (host, account_label, epoch, captured_ts, session_id, session_cost_usd, "
        "five_pct, five_resets, seven_pct, seven_resets, cc_version, model) VALUES "
        "('dev','a',100.0,?,'S1',1.5,42,1790481600,52,1791018000,'2.1.141','Fable 5')",
        (TS,),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM quota_capture").fetchone()
    assert row["five_pct"] == 42 and row["session_cost_usd"] == 1.5
    try:
        conn.execute(
            "INSERT INTO quota_capture (host, account_label, epoch, captured_ts, session_id) "
            "VALUES ('dev','a',100.0,?,'S1')",
            (TS,),
        )
        assert False, "expected the UNIQUE(host, epoch, session_id) constraint to refuse the duplicate"
    except sqlite3.IntegrityError:
        pass


def test_quota_rate_window_check():
    conn = _platform_at(4)
    conn.execute(
        "INSERT INTO quota_rate (account_label, window, points_per_usd, n_windows, excluded_windows, "
        "fit_from_ts, fit_to_ts, fitted_ts, method) VALUES ('a','five_hour',0.4,10,2,?,?,?,'ratio')",
        (TS, TS, TS),
    )
    try:
        conn.execute(
            "INSERT INTO quota_rate (account_label, window, points_per_usd, n_windows, excluded_windows, "
            "fit_from_ts, fit_to_ts, fitted_ts, method) VALUES ('a','monthly',0.4,10,2,?,?,?,'ratio')",
            (TS, TS, TS),
        )
        assert False, "expected an IntegrityError on an unknown window"
    except sqlite3.IntegrityError:
        pass


def test_quota_notice_kind_check_and_one_row_per_level_is_a_caller_rule_not_a_constraint():
    conn = _platform_at(4)
    conn.execute(
        "INSERT INTO quota_notice (account_label, kind, period, level, created_ts) "
        "VALUES ('a','monthly_level','2026-09',80,?)",
        (TS,),
    )
    try:
        conn.execute(
            "INSERT INTO quota_notice (account_label, kind, created_ts) VALUES ('a','bogus',?)",
            (TS,),
        )
        assert False, "expected an IntegrityError on an unknown kind"
    except sqlite3.IntegrityError:
        pass


def test_the_migration_is_idempotent_and_schema_identical_to_a_fresh_v4_store():
    upgraded = _platform_at(3)
    _seed_v3(upgraded)
    apply_migrations(upgraded, _upto(4))
    assert apply_migrations(upgraded, _upto(4)) == []
    assert current_version(upgraded) == 4

    fresh = _platform_at(4)

    def snapshot(conn):
        return [
            (r["type"], r["name"], r["sql"])
            for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ]

    assert snapshot(upgraded) == snapshot(fresh)


def test_v1_through_v3_ddl_is_untouched():
    """Trap 3: fresh stores replay every migration -- _V1.._V3 must not have
    been edited to make room for v4."""
    v1_names = {stmt.split()[2] for stmt in platform._V1 if stmt.strip().upper().startswith("CREATE TABLE")}
    assert v1_names == {"account", "budget_pool", "launch", "quota_snapshot", "calibration"}
    v3_names = {stmt.split()[2] for stmt in platform._V3 if stmt.strip().upper().startswith("CREATE TABLE")}
    assert v3_names == {"unit", "unit_msg", "probe_run"}
