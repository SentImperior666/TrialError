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
    "knowledge_db_path",
    "ops_db_path",
    "jobs_db_path",
]

_PLATFORM_ROOT_ENV = "TRIALERROR_PLATFORM_ROOT"

#: the import-design notes (internal, not in this export) Sec 5 knob #1 (``[paths].stores_dir``, punch-list
#: item 1: "mirror the existing TRIALERROR_PLATFORM_ROOT env-var pattern").
_DEFAULT_STORES_DIR = "stores"


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
