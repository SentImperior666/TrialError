"""DB file path resolution. Design Section 3.2 (per-program scaffold):
"``stores/`` # knowledge.db, ops.db, jobs.db (SQLite WAL; gitignored)".
Design Section 4.3: "``~/.trialerror/platform.db``" (platform stores are
per-account and cross-program, not per-program).

``TRIALERROR_PLATFORM_ROOT`` overrides the platform root for tests/CI (avoids
every test touching the real developer's ``~/.trialerror``) — the same style of
override ``trialerror doctor``'s ``--vendored-root``/``--repo-root`` flags use
in M0, just as an env var since path-resolution helpers (unlike a CLI
command) don't get their own argv.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from trialerror.util.config import resolve_configured_path

__all__ = [
    "platform_root",
    "platform_db_path",
    "program_store_dir",
    "program_index_dir",
    "knowledge_db_path",
    "ops_db_path",
    "jobs_db_path",
    "fulltext_index_path",
]

_PLATFORM_ROOT_ENV = "TRIALERROR_PLATFORM_ROOT"

#: the import-design notes (internal, not in this export) Sec 5 knob #1 (``[paths].stores_dir``, punch-list
#: item 1: "mirror the existing TRIALERROR_PLATFORM_ROOT env-var pattern").
_DEFAULT_STORES_DIR = "stores"

#: ``[paths].index_dir`` -- the home of DERIVED, rebuildable index state
#: that does NOT live inside a ``.db`` file. Deliberately a sibling of
#: ``stores/`` rather than a child: everything under ``stores/`` is a
#: SQLite database that is (or contains) source of truth and belongs in a
#: backup, while everything under ``index/`` is reconstructible from those
#: databases by one command (``trialerror ingest reindex-fulltext``) and is
#: therefore safe to delete, exclude from a backup, and gitignore. Today's
#: one tenant is the tantivy full-text index
#: (:mod:`trialerror.retrieve.tantivysearch`).
_DEFAULT_INDEX_DIR = "index"


def platform_root() -> Path:
    override = os.environ.get(_PLATFORM_ROOT_ENV)
    return Path(override) if override else Path.home() / ".trialerror"


def platform_db_path(*, root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else platform_root()
    return base / "platform.db"


def program_store_dir(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    """``[paths].stores_dir`` (default ``"stores"``, program-root-relative
    unless the configured value is absolute) -- ``config`` defaults to
    ``None``, which reproduces the old hardcoded-literal behavior exactly
    (zero behavior change for every pre-existing caller that doesn't pass
    one, including every ``trialerror.*.checks`` doctor module -- see
    ``trialerror.stores.store.open_store``'s own module docstring for the one
    place ``config`` is auto-discovered instead of passed explicitly)."""
    return resolve_configured_path(program_root, config, "stores_dir", _DEFAULT_STORES_DIR)


def knowledge_db_path(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    return program_store_dir(program_root, config) / "knowledge.db"


def ops_db_path(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    return program_store_dir(program_root, config) / "ops.db"


def jobs_db_path(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    return program_store_dir(program_root, config) / "jobs.db"


def program_index_dir(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    """``[paths].index_dir`` (default ``"index"``) -- see
    :data:`_DEFAULT_INDEX_DIR` for why derived index state gets its own
    root rather than living under ``stores/``."""
    return resolve_configured_path(program_root, config, "index_dir", _DEFAULT_INDEX_DIR)


def fulltext_index_path(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    """``<index_dir>/tantivy/chunks`` -- the tantivy lexical index over the
    ``chunk`` corpus (:mod:`trialerror.retrieve.tantivysearch`). The
    ``tantivy/`` level names the engine and the ``chunks`` level names the
    indexed table, so a second tantivy index over some other table later
    needs no path migration for this one."""
    return program_index_dir(program_root, config) / "tantivy" / "chunks"
