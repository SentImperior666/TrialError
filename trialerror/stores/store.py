"""``Store``: one handle bundling connections to all four DBs a program
touches (platform + this program's ops/knowledge/jobs), migrated and ready.
This is the object every write-API call (``trialerror.stores.writer.insert``,
``trialerror.stores.bitemporal``) takes — XID validation needs to reach across
DB files, so the handle groups the connections XID targets might live in.

Design Section 3.2 store-placement rule: "research *content* ->
knowledge.db; program *operations* -> ops.db; worker coordination ->
jobs.db ...; *money* -> platform ``~/.trialerror/platform.db``." One
:class:`Store` per program root is the unit that rule operationalizes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import jobs as jobs_schema
from trialerror.stores.schema import knowledge as knowledge_schema
from trialerror.stores.schema import ops as ops_schema
from trialerror.stores.schema import platform as platform_schema

__all__ = ["Store", "open_store", "SCHEMA_MODULES", "TABLE_DB"]

#: db_kind -> its schema module (TABLES + MIGRATIONS). The one place that
#: maps "platform"/"ops"/"knowledge"/"jobs" to the module that owns them.
SCHEMA_MODULES = {
    "platform": platform_schema,
    "ops": ops_schema,
    "knowledge": knowledge_schema,
    "jobs": jobs_schema,
}

#: table name -> which DB it lives in. Built once from the four schema
#: modules' TABLES tuples; used by the write API to route a table name to
#: the right connection without every caller having to know or state which
#: DB their table lives in. Table names are unique across all four DBs in
#: this design (no collisions) -- asserted at import time below so a future
#: schema addition that violates it fails loudly at import, not at some
#: unlucky runtime insert.
TABLE_DB: dict[str, str] = {}
for _db_kind, _mod in SCHEMA_MODULES.items():
    for _table in _mod.TABLES:
        if _table in TABLE_DB:
            raise RuntimeError(
                f"table name collision: {_table!r} declared in both "
                f"{TABLE_DB[_table]!r} and {_db_kind!r} schema modules"
            )
        TABLE_DB[_table] = _db_kind
del _db_kind, _mod, _table


@dataclass
class Store:
    platform: sqlite3.Connection
    ops: sqlite3.Connection
    knowledge: sqlite3.Connection
    jobs: sqlite3.Connection
    program_root: Path
    platform_root: Path

    def conn_for_table(self, table: str) -> sqlite3.Connection:
        db_kind = TABLE_DB.get(table)
        if db_kind is None:
            from trialerror.stores.errors import UnknownTableError

            raise UnknownTableError(f"unknown table {table!r} (not declared in any schema module)")
        return getattr(self, db_kind)

    def close(self) -> None:
        for conn in (self.platform, self.ops, self.knowledge, self.jobs):
            conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _auto_load_paths_config(program_root: Path) -> dict | None:
    """Best-effort ``[paths]`` read from ``<program_root>/trialerror.toml`` when
    a caller doesn't already have a loaded config to pass in -- so
    ``[paths].stores_dir`` (the import-design notes (internal, not in this export) Sec 5 knob #1) resolves
    consistently everywhere ``open_store`` is called (every CLI group, the
    MCP servers, doctor's own store-adjacent tooling) without requiring
    every one of those ~20 call sites to be individually taught to load
    and thread a config dict through -- the same "ambient, no caller
    opt-in needed" spirit ``TRIALERROR_PLATFORM_ROOT`` already has for the
    platform root. Missing/invalid ``trialerror.toml`` -> ``None`` (same
    "read generically, tolerate absence" convention
    ``trialerror.cli.budget._load_policy`` already documents) -- most tests open
    a bare ``tmp_path`` with no ``trialerror.toml`` at all, and must see
    byte-identical behavior to before this knob existed."""
    cfg_path = program_root / "trialerror.toml"
    if not cfg_path.is_file():
        return None
    try:
        from trialerror.util.config import load_config

        return load_config(cfg_path).raw
    except Exception:
        return None


def open_store(
    program_root: Path | str,
    *,
    platform_root: Path | str | None = None,
    config: dict | None = None,
) -> Store:
    """Open (creating + migrating as needed) all four DBs for one program.

    ``program_root`` is the per-program scaffold root (design Section 3.2:
    its ``stores/`` subdirectory holds knowledge.db/ops.db/jobs.db, unless
    relocated via ``[paths].stores_dir`` -- the import-design notes (internal, not in this export) Sec 5 knob
    #1). ``platform_root`` defaults to ``trialerror.stores.paths.platform_root()``
    (``~/.trialerror``, overridable via ``TRIALERROR_PLATFORM_ROOT`` — tests always
    pass an explicit tmp path so they never touch a real developer's
    platform store).

    ``config`` is the plain ``ProgramConfig.raw`` dict a caller who already
    loaded ``trialerror.toml`` for its own purposes may pass through (e.g. a CLI
    group that also needs ``[license]``/``[models]``); when omitted (the
    default, and every pre-existing call site's behavior), it is
    best-effort auto-loaded from ``<program_root>/trialerror.toml`` if one
    exists -- see :func:`_auto_load_paths_config`. Only ``[paths].
    stores_dir`` is consulted here; the other five path knobs are each
    resolved by their own owning subsystem (``trialerror.law.service``,
    ``trialerror.sessions.handoff``/``.lifecycle``, ``trialerror.ingest.requests``/
    ``.pipeline``, ``trialerror.cli.memory``).
    """
    program_root = Path(program_root)
    p_root = Path(platform_root) if platform_root is not None else paths.platform_root()
    if config is None:
        config = _auto_load_paths_config(program_root)

    platform_conn = connect(paths.platform_db_path(root=p_root))
    apply_migrations(platform_conn, platform_schema.MIGRATIONS)

    ops_conn = connect(paths.ops_db_path(program_root, config))
    apply_migrations(ops_conn, ops_schema.MIGRATIONS)

    knowledge_conn = connect(paths.knowledge_db_path(program_root, config))
    apply_migrations(knowledge_conn, knowledge_schema.MIGRATIONS)

    jobs_conn = connect(paths.jobs_db_path(program_root, config))
    apply_migrations(jobs_conn, jobs_schema.MIGRATIONS)

    return Store(
        platform=platform_conn,
        ops=ops_conn,
        knowledge=knowledge_conn,
        jobs=jobs_conn,
        program_root=program_root,
        platform_root=p_root,
    )
