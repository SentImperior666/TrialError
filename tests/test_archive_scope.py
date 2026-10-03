"""L5 `quickwins.md` §B6: a scope for the transcript archive -- ``archive
run --include GLOB``. Synthetic files in temporary folders only; every
folder name here is neutral (B6.5), never a real project's."""

from __future__ import annotations

import json
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trialerror.archive import audit
from trialerror.archive.store import open_index, run_archive, scope_matches, walk_source

T0 = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
OLD = 1_700_000_000


def put(path: Path, data: bytes = b'{"n": 1}\n', mtime: int = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def rows(dest: Path, sql: str, *params):
    db = sqlite3.connect(str(dest / "index.db"))
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


@pytest.fixture()
def area(tmp_path):
    src = tmp_path / "projects"
    put(src / "alpha-proj" / "s1.jsonl")
    put(src / "beta-proj" / "s1.jsonl")
    put(src / "loose.jsonl")  # directly under src, no project folder
    return src, tmp_path / "arc"


# ---------------------------------------------------------------------------
# B6.6.1: a matching folder is archived, a non-matching one is not entered
# ---------------------------------------------------------------------------


def test_include_archives_a_matching_folder_and_prunes_a_non_matching_one(area):
    src, dest = area
    opened: list[str] = []

    def counting_walk():
        return list(walk_source(src, ["alpha-*"]))

    walked = counting_walk()
    rels = {rel for rel, *_ in walked}
    assert rels == {"alpha-proj/s1.jsonl"}  # beta-proj and the loose file never entered

    result = run_archive(src, dest, "hx", now=T0, include=["alpha-*"])
    assert result["scanned"] == 1
    assert result["scope"] == ["alpha-*"]
    stored = {r["rel_path"] for r in rows(dest, "SELECT rel_path FROM path_state WHERE host='hx'")}
    assert stored == {"alpha-proj/s1.jsonl"}


def test_include_matching_is_case_insensitive(area):
    src, dest = area
    result = run_archive(src, dest, "hx", now=T0, include=["ALPHA-*"])
    assert result["scanned"] == 1
    assert scope_matches("Alpha-Proj/s1.jsonl", ["alpha-*"]) is True
    assert scope_matches("alpha-proj/s1.jsonl", ["ALPHA-*"]) is True


def test_a_loose_file_directly_under_src_is_out_of_scope_with_include(area):
    src, dest = area
    result = run_archive(src, dest, "hx", now=T0, include=["*"])
    stored = {r["rel_path"] for r in rows(dest, "SELECT rel_path FROM path_state WHERE host='hx'")}
    assert "loose.jsonl" not in stored
    assert result["skipped_out_of_scope_folders"] == 0  # "*" matches every folder


def test_skipped_out_of_scope_folders_is_counted(area):
    src, dest = area
    result = run_archive(src, dest, "hx", now=T0, include=["alpha-*"])
    assert result["skipped_out_of_scope_folders"] == 1  # beta-proj


# ---------------------------------------------------------------------------
# B6.6.2: rows from outside the scope stay intact after a scoped run
# ---------------------------------------------------------------------------


def test_out_of_scope_rows_stay_intact_after_a_scoped_run(area):
    src, dest = area
    # first, an unscoped run archives everything
    run_archive(src, dest, "hx", now=T0)
    before = {r["rel_path"]: dict(r) for r in rows(dest, "SELECT * FROM path_state WHERE host='hx'")}
    assert before["beta-proj/s1.jsonl"]["gone_ts"] is None

    # then a scoped run touches only alpha-proj
    run_archive(src, dest, "hx", now=T0 + timedelta(hours=2), include=["alpha-*"])
    after = {r["rel_path"]: dict(r) for r in rows(dest, "SELECT * FROM path_state WHERE host='hx'")}

    assert after["beta-proj/s1.jsonl"]["gone_ts"] is None  # never marked gone
    assert after["beta-proj/s1.jsonl"]["last_seen_ts"] == before["beta-proj/s1.jsonl"]["last_seen_ts"]
    assert after["beta-proj/s1.jsonl"]["latest_sha256"] == before["beta-proj/s1.jsonl"]["latest_sha256"]
    obj = rows(dest, "SELECT * FROM object WHERE sha256 = ?", after["beta-proj/s1.jsonl"]["latest_sha256"])
    assert obj and obj[0]["deleted_ts"] is None


# ---------------------------------------------------------------------------
# B6.6.3: the run records its scope; an index from before B6 gets the column
# ---------------------------------------------------------------------------


def test_the_run_records_its_scope(area):
    src, dest = area
    run_archive(src, dest, "hx", now=T0, include=["alpha-*"])
    run = rows(dest, "SELECT scope FROM run WHERE host='hx' ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(run["scope"]) == ["alpha-*"]


def test_without_include_the_run_records_no_scope(area):
    src, dest = area
    run_archive(src, dest, "hx", now=T0)
    run = rows(dest, "SELECT scope FROM run WHERE host='hx' ORDER BY id DESC LIMIT 1")[0]
    assert run["scope"] is None


def test_an_index_from_before_b6_gets_the_scope_column(tmp_path):
    dest = tmp_path / "arc"
    dest.mkdir()
    # a pre-B6 index: the run table with no `scope` column, schema_version '1'
    db = sqlite3.connect(str(dest / "index.db"))
    db.executescript(
        """
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE run (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL,
            started_ts TEXT NOT NULL, finished_ts TEXT, scanned INTEGER, new_objects INTEGER,
            bytes_stored INTEGER, superseded INTEGER, gone INTEGER, errors INTEGER);
        CREATE TABLE path_state (host TEXT NOT NULL, rel_path TEXT NOT NULL,
            latest_sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            kind TEXT NOT NULL, first_seen_ts TEXT NOT NULL, last_seen_ts TEXT NOT NULL, gone_ts TEXT,
            PRIMARY KEY (host, rel_path));
        CREATE TABLE object (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL,
            stored_size INTEGER NOT NULL, stored_ts TEXT NOT NULL, superseded_by TEXT, deleted_ts TEXT);
        CREATE TABLE snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL,
            rel_path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, seen_ts TEXT NOT NULL);
        """
    )
    db.execute("INSERT INTO meta(k, v) VALUES ('schema_version', '1')")
    db.execute("INSERT INTO run(host, started_ts) VALUES ('hx', '2026-01-01T00:00:00Z')")
    db.commit()
    db.close()

    reopened = open_index(dest, create=False)
    try:
        cols = {r["name"] for r in reopened.execute("PRAGMA table_info(run)").fetchall()}
        assert "scope" in cols
        version = reopened.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()["v"]
        assert version == "2"
        # the pre-existing row survives the migration, with scope NULL
        old_row = reopened.execute("SELECT scope FROM run WHERE host = 'hx'").fetchone()
        assert old_row["scope"] is None
    finally:
        reopened.close()


def test_open_index_leaves_a_newer_index_alone(area):
    """N1 (fix check): a version-3 index -- opened by a client ahead of this
    one -- must not be re-stamped down to this client's own version 2, and
    its own (unknown-to-this-client) shape must not be touched."""
    src, dest = area
    run_archive(src, dest, "hx", now=T0)  # a real, current-shape index

    db = sqlite3.connect(str(dest / "index.db"))
    db.execute("UPDATE meta SET v = '3' WHERE k = 'schema_version'")
    db.execute("ALTER TABLE run ADD COLUMN future_column TEXT")
    db.commit()
    db.close()

    reopened = open_index(dest, create=False)
    try:
        version = reopened.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()["v"]
        assert version == "3"  # not re-stamped to '2'
        cols = {r["name"] for r in reopened.execute("PRAGMA table_info(run)").fetchall()}
        assert "future_column" in cols  # untouched
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# B6.6.4/5: audit coverage uses the recorded scope; a scope change is named
# ---------------------------------------------------------------------------


def do_audit(dest, src, **kw):
    kw.setdefault("rng", random.Random(1))
    return audit.run_audit(dest, "hx", src=src, **kw)


def status_of(report, n):
    return next(c for c in report["checks"] if c["n"] == n)


def test_coverage_ignores_a_new_file_in_an_out_of_scope_folder(area, monkeypatch):
    src, dest = area
    run_archive(src, dest, "hx", now=T0, include=["alpha-*"])
    real = audit.shutil.disk_usage
    monkeypatch.setattr(audit.shutil, "disk_usage", lambda p: real(p)._replace(free=500 * 1000 ** 3))
    # a NEW file appears only in the out-of-scope folder
    put(src / "beta-proj" / "s2.jsonl", mtime=OLD)

    report = do_audit(dest, src, now=T0 + timedelta(hours=3))
    coverage = status_of(report, 2)
    assert coverage["status"] == "pass"
    assert "1 project folder" in coverage["sentence"]  # the scope's own folder count


def test_coverage_flags_a_new_file_in_an_in_scope_folder(area, monkeypatch):
    src, dest = area
    run_archive(src, dest, "hx", now=T0, include=["alpha-*"])
    real = audit.shutil.disk_usage
    monkeypatch.setattr(audit.shutil, "disk_usage", lambda p: real(p)._replace(free=500 * 1000 ** 3))
    # a new, already-at-rest file appears in the IN-scope folder, never archived
    put(src / "alpha-proj" / "s2.jsonl", mtime=OLD)

    report = do_audit(dest, src, now=T0 + timedelta(hours=3))
    coverage = status_of(report, 2)
    assert coverage["status"] == "fail"
    assert "alpha-proj/s2.jsonl" in coverage["sentence"]


def test_a_scope_change_between_runs_is_named_by_the_audit(area, monkeypatch):
    src, dest = area
    run_archive(src, dest, "hx", now=T0, include=["alpha-*"])
    run_archive(src, dest, "hx", now=T0 + timedelta(hours=2), include=["beta-*"])
    real = audit.shutil.disk_usage
    monkeypatch.setattr(audit.shutil, "disk_usage", lambda p: real(p)._replace(free=500 * 1000 ** 3))

    report = do_audit(dest, src, now=T0 + timedelta(hours=4))
    coverage = status_of(report, 2)
    assert "the scope changed on 2026-01-10" in coverage["sentence"]


# ---------------------------------------------------------------------------
# B6.6.6: `archive status` shows the scope
# ---------------------------------------------------------------------------


def test_archive_status_shows_the_scope(area):
    from trialerror.archive.store import archive_status

    src, dest = area
    run_archive(src, dest, "hx", now=T0)  # unscoped: archives both folders
    run_archive(src, dest, "hx", now=T0 + timedelta(hours=2), include=["alpha-*"])

    status = archive_status(dest)
    host_status = status["hosts"]["hx"]
    assert host_status["scope"] == ["alpha-*"]
    assert host_status["files_in_scope"] == 1
    assert host_status["files_kept_out_of_scope"] == 2  # beta-proj/s1.jsonl and loose.jsonl


# ---------------------------------------------------------------------------
# B6.6.7: without --include, everything works as before -- a quick smoke
# (the full pre-existing suites are also run unchanged through the lock)
# ---------------------------------------------------------------------------


def test_without_include_behaves_exactly_as_before(area):
    src, dest = area
    result = run_archive(src, dest, "hx", now=T0)
    assert result["scanned"] == 3  # both project folders AND the loose file
    assert result.get("scope") is None
    assert result["skipped_out_of_scope_folders"] == 0
