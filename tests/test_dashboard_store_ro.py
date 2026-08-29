"""``trialerror.dashboard.store_ro`` -- proves the dashboard's whole write-safety
story: connections it opens genuinely refuse writes at the SQLite driver
level, missing DB files leave the corresponding attribute ``None`` rather
than raising, and the ``Store``-duck-type surface (``conn_for_table``)
works for both present and absent DBs."""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.dashboard.store_ro import open_store_ro
from trialerror.stores.errors import UnknownTableError
from trialerror.stores.store import open_store
from tests._store_fixtures import populate_one_of_everything


def test_open_store_ro_connects_every_existing_db(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        for kind in ("platform", "ops", "knowledge", "jobs"):
            assert rostore.is_available(kind), f"{kind} should be connected"
            assert getattr(rostore, kind) is not None
    finally:
        rostore.close()


def test_open_store_ro_leaves_missing_dbs_none(tmp_path):
    rostore = open_store_ro(tmp_path / "never-initialized", platform_root=tmp_path / "no-platform")
    try:
        for kind in ("platform", "ops", "knowledge", "jobs"):
            assert not rostore.is_available(kind)
            assert getattr(rostore, kind) is None
    finally:
        rostore.close()


def test_open_store_ro_with_no_program_root_connects_platform_only(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    rostore = open_store_ro(None, platform_root=platform_root)
    try:
        assert rostore.is_available("platform")
        assert not rostore.is_available("ops")
        assert not rostore.is_available("knowledge")
        assert not rostore.is_available("jobs")
    finally:
        rostore.close()


def test_ro_connection_refuses_writes(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        with pytest.raises(sqlite3.OperationalError):
            rostore.ops.execute("INSERT INTO thread (thread_id, title, created_ts, created_by_launch) "
                                 "VALUES ('THR-illegal', 'x', 'x', 'x')")
    finally:
        rostore.close()


def test_conn_for_table_raises_for_unknown_table(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        with pytest.raises(UnknownTableError):
            rostore.conn_for_table("not_a_real_table")
    finally:
        rostore.close()


def test_conn_for_table_raises_runtime_error_when_db_missing(tmp_path):
    rostore = open_store_ro(tmp_path / "never-initialized", platform_root=tmp_path / "no-platform")
    try:
        with pytest.raises(RuntimeError):
            rostore.conn_for_table("session")  # lives in ops.db, which was never connected
    finally:
        rostore.close()


def test_rostore_context_manager_closes():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        with open_store_ro(Path(td) / "no-program", platform_root=Path(td) / "no-platform") as rostore:
            assert rostore is not None
        # closing twice (context manager __exit__ already ran) must not raise
        rostore.close()
