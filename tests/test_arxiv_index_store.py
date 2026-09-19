"""Unit tests for :mod:`trialerror.arxiv_index.store` -- schema creation, disk
preflight, build-state bookkeeping."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.arxiv_index.store import (
    BUILD_STATE_TABLE_NAME,
    DEFAULT_DB_RELPATH,
    META_TABLE_NAME,
    VEC_TABLE_NAME,
    DiskPreflightError,
    VecBackend,
    default_db_path,
    disk_preflight,
    ensure_schema,
    get_build_state,
    open_arxiv_index_db,
    row_count,
    set_build_state,
)


def test_default_db_path_is_under_program_root(tmp_path):
    p = default_db_path(tmp_path)
    assert p == tmp_path / DEFAULT_DB_RELPATH
    assert str(p).replace("\\", "/").endswith("data/arxiv_index.sqlite3")


def test_disk_preflight_passes_with_generous_floor(tmp_path):
    result = disk_preflight(tmp_path / "db.sqlite3", min_free_gb=0.001)
    assert result.ok is True
    assert result.free_gb > 0


def test_disk_preflight_raises_when_floor_impossibly_high(tmp_path):
    with pytest.raises(DiskPreflightError, match="disk preflight failed"):
        disk_preflight(tmp_path / "db.sqlite3", min_free_gb=10_000_000.0)


def test_open_arxiv_index_db_creates_parent_dirs(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "arxiv.sqlite3"
    conn = open_arxiv_index_db(db_path)
    try:
        assert db_path.parent.is_dir()
        assert db_path.is_file()
    finally:
        conn.close()


def test_ensure_schema_creates_all_three_tables_fallback_backend(tmp_path, monkeypatch):
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    backend = ensure_schema(conn, dims=8)
    assert backend == VecBackend.FALLBACK

    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert VEC_TABLE_NAME in tables
    assert META_TABLE_NAME in tables
    assert BUILD_STATE_TABLE_NAME in tables

    state = get_build_state(conn)
    assert state["backend"] == "fallback"
    assert state["dims"] == "8"


def test_ensure_schema_is_idempotent(tmp_path, monkeypatch):
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, dims=8)
    ensure_schema(conn, dims=8)  # second call must not raise
    conn.execute(f"INSERT INTO {META_TABLE_NAME}(arxiv_id, ingested_ts) VALUES ('x', 'ts')")
    assert row_count(conn) == 1


def test_ensure_schema_real_sqlite_vec_when_available():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    backend = ensure_schema(conn, dims=4)
    if backend == VecBackend.FALLBACK:
        pytest.skip("sqlite-vec extension not installed in this environment")
    assert backend == VecBackend.SQLITE_VEC
    # a vec0 virtual table exists and accepts the expected columns
    conn.execute(f"INSERT INTO {VEC_TABLE_NAME}(arxiv_id, embedding) VALUES (?, ?)", ("a", b"\x00" * 16))


def test_get_build_state_on_never_initialized_db_returns_empty_dict():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert get_build_state(conn) == {}


def test_row_count_on_never_initialized_db_returns_zero():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert row_count(conn) == 0


def test_set_build_state_updates_and_stamps_last_updated_ts(monkeypatch):
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, dims=8)
    set_build_state(conn, {"status": "building", "rows_ingested": 5})
    state = get_build_state(conn)
    assert state["status"] == "building"
    assert state["rows_ingested"] == "5"
    assert "last_updated_ts" in state

    set_build_state(conn, {"status": "complete"})
    state2 = get_build_state(conn)
    assert state2["status"] == "complete"
    assert state2["rows_ingested"] == "5"  # untouched key survives a partial update
