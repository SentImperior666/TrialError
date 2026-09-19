"""Read-only access to a program's four stores -- the dashboard's entire
write-safety story lives in this one module.

Deliberately does NOT call :func:`trialerror.stores.store.open_store`: that
function applies migrations (a write) and creates missing DB files/parent
directories -- exactly the two things a passive viewer over someone else's
running program must never do. Instead this mirrors the pattern every
``trialerror.<subsystem>.checks`` doctor module already uses (see
``trialerror.stores.checks.check_store_schema_version`` /
``trialerror.budget.checks.check_budget_dangling_launches``): resolve each DB
path via ``trialerror.stores.paths``, connect only the files that already exist,
via ``trialerror.stores.connection.connect(path, read_only=True)``. SQLite's
``mode=ro`` URI connection refuses at the OS/driver level, not just by
polite convention -- a stray ``INSERT``/``UPDATE`` reached from a panel
builder fails loudly (``sqlite3.OperationalError: attempt to write a
readonly database``) rather than silently landing.

:class:`RoStore` is deliberately duck-type compatible with
:class:`trialerror.stores.store.Store` (same four connection attribute names,
same ``conn_for_table`` method) so panel builders can reuse the SAME
business-logic read functions the rest of the codebase already has and
tests (``trialerror.sessions.lifecycle.session_status``,
``trialerror.budget.pools.budget_status``/``list_pools``,
``trialerror.jobs.ledger.list_jobs``/``list_events``,
``trialerror.artifacts.registry.list_artifacts``, ...) instead of hand-rolling a
second, dashboard-only copy of e.g. close-readiness or headroom math. Every
one of those functions was read (module source, this build session) to
confirm it never writes when called the way the dashboard calls it (no
``mark_read=True``, no ``boot_session``/``close_session``, no ``insert``/
``update``) -- and the read-only connection is the backstop if that ever
drifts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.stores.errors import UnknownTableError
from trialerror.util.config import CONFIG_FILENAME, load_config

__all__ = ["RoStore", "open_store_ro", "DB_KINDS"]

#: Same four db_kind names ``trialerror.stores.store.SCHEMA_MODULES`` uses.
DB_KINDS = ("platform", "ops", "knowledge", "jobs")


def _load_paths_config(program_root: Path) -> dict | None:
    """Best-effort ``[paths]`` read, same private-per-module convention
    every doctor ``checks.py`` and ``trialerror.stores.store.open_store`` itself
    already use (``_auto_load_paths_config`` / ``_load_paths_config``) --
    so a program whose ``trialerror.toml`` relocates ``[paths].stores_dir``
    resolves identically here as it does for every write-path caller.
    Missing/invalid ``trialerror.toml`` -> ``None`` (reproduces the hardcoded-
    default-literal behavior, same as every other reader of this
    convention)."""
    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return None
    try:
        return load_config(cfg_path).raw
    except Exception:
        return None


@dataclass
class RoStore:
    """Up to four read-only ``sqlite3.Connection``\\ s. A DB file that does
    not exist yet (a fresh or partially-initialized program) leaves its
    field ``None`` -- not an error; every panel builder in
    :mod:`trialerror.dashboard.data` reports that as a ``"not_initialized"``
    panel status, the same "visible, not refused" spirit doctor checks
    report ``skip`` with."""

    platform: sqlite3.Connection | None
    ops: sqlite3.Connection | None
    knowledge: sqlite3.Connection | None
    jobs: sqlite3.Connection | None
    program_root: Path | None
    platform_root: Path

    def conn_for_table(self, table: str) -> sqlite3.Connection:
        """Duck-type match to ``trialerror.stores.store.Store.conn_for_table`` --
        lets read-only business-logic functions written against a real
        ``Store`` (e.g. ``trialerror.sessions.lifecycle.session_status``) accept
        an ``RoStore`` unmodified. Raises :class:`UnknownTableError` for an
        unknown table name (same as ``Store``); raises a plain
        :class:`RuntimeError` -- not silently ``None`` -- if the table's DB
        was never connected (missing file), since a caller reaching this
        path asked for a table it should have checked ``is_available()``
        for first."""
        from trialerror.stores.store import TABLE_DB

        db_kind = TABLE_DB.get(table)
        if db_kind is None:
            raise UnknownTableError(f"unknown table {table!r} (not declared in any schema module)")
        conn = getattr(self, db_kind)
        if conn is None:
            raise RuntimeError(
                f"table {table!r} lives in {db_kind}.db, which has no read-only connection "
                f"open (file not found) -- check RoStore.is_available({db_kind!r}) first"
            )
        return conn

    def is_available(self, db_kind: str) -> bool:
        return getattr(self, db_kind, None) is not None

    def close(self) -> None:
        for kind in DB_KINDS:
            conn = getattr(self, kind)
            if conn is not None:
                conn.close()

    def __enter__(self) -> "RoStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_store_ro(
    program_root: Path | str | None,
    *,
    platform_root: Path | str | None = None,
) -> RoStore:
    """Open read-only connections to every store DB file that currently
    exists. ``program_root=None`` connects platform.db only (a
    program-agnostic view -- e.g. an account-wide budget check before any
    program is chosen); ``platform_root`` defaults to
    ``trialerror.stores.paths.platform_root()`` exactly like ``open_store``."""
    p_root = Path(platform_root) if platform_root is not None else paths.platform_root()

    platform_conn: sqlite3.Connection | None = None
    platform_path = paths.platform_db_path(root=p_root)
    if platform_path.is_file():
        platform_conn = connect(platform_path, read_only=True)

    ops_conn: sqlite3.Connection | None = None
    knowledge_conn: sqlite3.Connection | None = None
    jobs_conn: sqlite3.Connection | None = None
    resolved_program_root: Path | None = None

    if program_root is not None:
        resolved_program_root = Path(program_root)
        config = _load_paths_config(resolved_program_root)

        ops_path = paths.ops_db_path(resolved_program_root, config)
        if ops_path.is_file():
            ops_conn = connect(ops_path, read_only=True)

        knowledge_path = paths.knowledge_db_path(resolved_program_root, config)
        if knowledge_path.is_file():
            knowledge_conn = connect(knowledge_path, read_only=True)

        jobs_path = paths.jobs_db_path(resolved_program_root, config)
        if jobs_path.is_file():
            jobs_conn = connect(jobs_path, read_only=True)

    return RoStore(
        platform=platform_conn,
        ops=ops_conn,
        knowledge=knowledge_conn,
        jobs=jobs_conn,
        program_root=resolved_program_root,
        platform_root=p_root,
    )
