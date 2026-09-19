"""Unit tests for the ``arxiv_index_ready`` doctor check
(:mod:`trialerror.arxiv_index.checks`) -- absent/building/ready/dims-mismatch
states, per the build brief's item 5."""

from __future__ import annotations

import sqlite3

from trialerror.arxiv_index.checks import check_arxiv_index_ready
from trialerror.arxiv_index.store import ensure_schema, set_build_state
from trialerror.util.doctor import DoctorContext


def test_absent_when_no_program_root(tmp_path):
    ctx = DoctorContext(repo_root=tmp_path, program_root=None)
    result = check_arxiv_index_ready(ctx)
    assert result.status == "skip"


def test_absent_when_db_file_does_not_exist(tmp_path, monkeypatch):
    ctx = DoctorContext(repo_root=tmp_path, program_root=tmp_path)
    result = check_arxiv_index_ready(ctx)
    assert result.status == "skip"
    assert result.details["state"] == "absent"


def test_building_state_warns_with_row_count(tmp_path, monkeypatch):
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda c: False)

    db_path = tmp_path / "data" / "arxiv_index.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, dims=3072)
    conn.execute("INSERT INTO arxiv_meta(arxiv_id, ingested_ts) VALUES ('a', 'ts')")
    conn.commit()
    set_build_state(conn, {"status": "building", "rows_ingested": 1, "zip_path": "x.zip"})
    conn.close()

    ctx = DoctorContext(repo_root=tmp_path, program_root=tmp_path)
    result = check_arxiv_index_ready(ctx)
    assert result.status == "warn"
    assert result.details["state"] == "building"
    assert result.details["row_count"] == 1


def test_complete_and_dims_ok_passes(tmp_path, monkeypatch):
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda c: False)

    db_path = tmp_path / "data" / "arxiv_index.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, dims=3072)
    set_build_state(conn, {"status": "complete", "rows_ingested": 0})
    conn.close()

    ctx = DoctorContext(repo_root=tmp_path, program_root=tmp_path)
    result = check_arxiv_index_ready(ctx)
    assert result.status == "pass"
    assert result.details["dims_ok"] is True
    assert result.details["backend"] == "fallback"


def test_complete_but_wrong_dims_fails(tmp_path, monkeypatch):
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda c: False)

    db_path = tmp_path / "data" / "arxiv_index.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, dims=1536)  # wrong -- text-embedding-3-large is 3072
    set_build_state(conn, {"status": "complete"})
    conn.close()

    ctx = DoctorContext(repo_root=tmp_path, program_root=tmp_path)
    result = check_arxiv_index_ready(ctx)
    assert result.status == "fail"
    assert result.details["dims_ok"] is False


def test_respects_configured_db_path(tmp_path, monkeypatch):
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda c: False)

    (tmp_path / "trialerror.toml").write_text(
        '[program]\nid = "test"\n\n[litapi.arxiv_index]\ndb_path = "custom/where.sqlite3"\n', encoding="utf-8"
    )
    db_path = tmp_path / "custom" / "where.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, dims=3072)
    set_build_state(conn, {"status": "complete"})
    conn.close()

    ctx = DoctorContext(repo_root=tmp_path, program_root=tmp_path)
    result = check_arxiv_index_ready(ctx)
    assert result.status == "pass"
    assert result.details["db_path"].replace("\\", "/").endswith("custom/where.sqlite3")
