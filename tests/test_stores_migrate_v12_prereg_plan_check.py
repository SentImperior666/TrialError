"""``ops_v12_prereg_plan_check_and_registration_dispositions`` -- additive
columns on ``prereg``, ``gate`` and ``artifact``."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.migrate import apply_migrations, is_additive
from trialerror.stores.schema import ops

TS = "2026-09-27T00:00:00.000Z"
V12 = next(m for m in ops.MIGRATIONS if m.version == 12)

PREREG_NEW = ["round_id", "plan_suite", "parent_prereg_id", "plan_check", "plan_check_status", "plan_checked_ts"]
GATE_NEW = ["disposition", "deviation_ref"]
ARTIFACT_NEW = ["disposition"]


def _upto(version: int):
    return tuple(m for m in ops.MIGRATIONS if m.version <= version)


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(r)[1:3] for r in conn.execute(f"PRAGMA table_info({table})")]


def _names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [c[0] for c in _columns(conn, table)]


def test_v12_is_contiguous_additive_and_named():
    versions = [m.version for m in ops.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert V12.name == "ops_v12_prereg_plan_check_and_registration_dispositions"
    assert is_additive(V12)


def test_previous_store_migrates_and_old_rows_keep_nulls():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _upto(11))
    assert "plan_suite" not in _names(conn, "prereg")
    conn.execute(
        "INSERT INTO prereg (prereg_id, title, procedure_sha256, params_sha256, committed_ts, escrow_path, status) "
        "VALUES ('PREG-old','t','p','q',?, 'x', 'committed')",
        (TS,),
    )
    conn.execute(
        "INSERT INTO template (type_key, title, version, path, gated) VALUES ('report','r','1','t.md',1)"
    )
    conn.execute(
        "INSERT INTO artifact (artifact_id, type, title, path, sha256, status, registered_by_launch) "
        "VALUES ('ART-old','report','t','p','s','draft','LNCH-x')"
    )
    conn.execute("INSERT INTO gate (gate_id, artifact_id, state) VALUES ('GATE-old','ART-old','draft')")
    conn.commit()

    apply_migrations(conn, ops.MIGRATIONS)
    old_gate = conn.execute(f"SELECT {', '.join(GATE_NEW)} FROM gate WHERE gate_id='GATE-old'").fetchone()
    old_artifact = conn.execute(f"SELECT {', '.join(ARTIFACT_NEW)} FROM artifact WHERE artifact_id='ART-old'").fetchone()
    assert all(v is None for v in old_gate) and all(v is None for v in old_artifact)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == max(m.version for m in ops.MIGRATIONS)
    names = _names(conn, "prereg")
    for col in PREREG_NEW:
        assert col in names
    for col in GATE_NEW:
        assert col in _names(conn, "gate")
    for col in ARTIFACT_NEW:
        assert col in _names(conn, "artifact")
    row = conn.execute(f"SELECT title, {', '.join(PREREG_NEW)} FROM prereg WHERE prereg_id='PREG-old'").fetchone()
    assert row[0] == "t" and all(v is None for v in row[1:])
    index = conn.execute("SELECT name FROM sqlite_master WHERE name='ix_prereg_round_suite'").fetchone()
    assert index is not None


def test_fresh_and_migrated_stores_have_identical_columns():
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, ops.MIGRATIONS)
    migrated = sqlite3.connect(":memory:")
    apply_migrations(migrated, _upto(11))
    apply_migrations(migrated, ops.MIGRATIONS)
    for table in ("prereg", "gate", "artifact"):
        assert _columns(fresh, table) == _columns(migrated, table)


def test_check_constraints_on_the_new_columns():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    conn.execute(
        "INSERT INTO prereg (prereg_id, title, procedure_sha256, params_sha256, committed_ts, escrow_path, status, "
        "plan_check_status) VALUES ('P','t','p','q',?, 'x', 'committed', 'deviations_accepted')",
        (TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO prereg (prereg_id, title, procedure_sha256, params_sha256, committed_ts, escrow_path, "
            "status, plan_check_status) VALUES ('P2','t','p','q',?, 'x', 'committed', 'bogus')",
            (TS,),
        )


def test_check_constraints_on_the_disposition_columns():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops.MIGRATIONS)
    conn.execute(
        "INSERT INTO template (type_key, title, version, path, gated) VALUES ('report','r','1','t.md',1)"
    )
    conn.execute(
        "INSERT INTO artifact (artifact_id, type, title, path, sha256, status, registered_by_launch, disposition) "
        "VALUES ('A1','report','t','p','s','registered','L','registered_with_deviation')"
    )
    conn.execute(
        "INSERT INTO gate (gate_id, artifact_id, state, disposition) VALUES ('G1','A1','registered','failure_registered')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO artifact (artifact_id, type, title, path, sha256, status, registered_by_launch, disposition) "
            "VALUES ('A2','report','t','p','s','registered','L','bogus')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO gate (gate_id, artifact_id, state, disposition) VALUES ('G2','A1','registered','bogus')")
