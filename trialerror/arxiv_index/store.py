"""The standalone arXiv semantic-search index store. NOT a program's
``knowledge.db`` (module docstring) -- one dedicated sqlite file, path
configurable (``[litapi.arxiv_index].db_path``, default
``<program_root>/data/arxiv_index.sqlite3``, ``data/`` gitignored per the
build brief).

Schema, three tables:

- the vector table (name :data:`VEC_TABLE_NAME`) -- a ``vec0`` virtual
  table when the sqlite-vec loadable extension is available, else the same
  pure-stdlib fallback shape ``trialerror.stores.vecindex`` uses elsewhere
  (packed-float32 BLOB, same wire format). UNLIKE
  ``trialerror.stores.vecindex.ensure_vec_table`` (which defaults to the
  fallback backend and requires ``TRIALERROR_VEC_BACKEND=sqlite_vec`` to opt
  into vec0 -- B.4a's finding, current production read pattern is SLOWER
  on vec0), this store ALWAYS attempts the real extension first,
  unconditionally: this dataset (2.7-2.9M rows) is BAKEOFF_REPORT.md
  Sec B.4b's own named trigger case for native ``MATCH`` -- there is no
  "current slow read pattern" to preserve here because this is a brand
  new index with no existing callers, and the whole point of building it
  is the native-MATCH query path (``trialerror.arxiv_index.query``). Falls back
  loudly (a doctor-visible ``backend`` field, never silent) only when the
  extension genuinely isn't installed on this machine.
- :data:`META_TABLE_NAME` -- per-paper metadata (arxiv_id PK, title,
  categories, authors, published, doi, journal_ref -- see this package's
  own ``__init__.py`` docstring for the ASSUMED-schema disclosure).
- :data:`BUILD_STATE_TABLE_NAME` -- one row, the build's own bookkeeping
  (source zip path/size, model_key/dims, row_count, last_build_ts) that
  :mod:`trialerror.arxiv_index.checks` reads for the ``arxiv_index_ready``
  doctor check without having to COUNT(*) a multi-million-row table on
  every doctor run.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trialerror.stores.vecindex import (
    VecBackend,
    deserialize_vector_fallback,
    serialize_vector_fallback,
    try_load_sqlite_vec,
)
from trialerror.util.timeutil import now

__all__ = [
    "VEC_TABLE_NAME",
    "META_TABLE_NAME",
    "BUILD_STATE_TABLE_NAME",
    "DEFAULT_DB_RELPATH",
    "DEFAULT_MIN_FREE_GB",
    "DiskPreflightError",
    "DiskPreflightResult",
    "default_db_path",
    "open_arxiv_index_db",
    "ensure_schema",
    "disk_preflight",
    "get_build_state",
    "set_build_state",
    "row_count",
    "serialize_vector_fallback",
    "deserialize_vector_fallback",
]

#: Single dedicated vector table -- unlike ``vec_chunks__<model_key>``
#: (one table per embedding model in the shared knowledge store), this
#: whole DB file is already scoped to exactly one model
#: (``text-embedding-3-large``, 3072-dim), so one fixed name is simpler and
#: matches this store's "one purpose" framing.
VEC_TABLE_NAME = "arxiv_vec"
META_TABLE_NAME = "arxiv_meta"
BUILD_STATE_TABLE_NAME = "arxiv_index_build_state"

#: Relative to ``program_root`` -- ``[litapi.arxiv_index].db_path``'s
#: built-in default when a program's ``trialerror.toml`` doesn't set one.
#: ``data/`` (gitignored, this build's own pathspec adds the entry) is a
#: NEW top-level convention for this feature specifically: every other
#: per-program store lives under ``stores/`` (``[paths].stores_dir``,
#: ``trialerror.stores.paths``) but that directory is scoped to the four
#: XID-governed program DBs (Section 3.2's store-placement rule) -- this
#: index is deliberately NOT one of those four (module docstring), so it
#: gets its own top-level, clearly-disposable directory instead of
#: crowding into a directory whose whole contract is "the four program
#: DBs".
DEFAULT_DB_RELPATH = "data/arxiv_index.sqlite3"

#: Build brief HARD FACT: "106GB free on C: ... require an explicit disk
#: preflight of >=80GB free before any download/build step." This is the
#: floor :func:`disk_preflight` enforces by default -- a program's
#: ``trialerror.toml`` may raise it, never lower it silently (a caller can still
#: pass an explicit smaller value for a synthetic-fixture test -- see this
#: function's own docstring).
DEFAULT_MIN_FREE_GB = 80.0


class DiskPreflightError(RuntimeError):
    """Raised by :func:`disk_preflight` when free disk space is below the
    configured floor. A build MUST refuse to start (not just warn) on this
    -- the build brief's own "streaming ingest mandatory ... require an
    explicit disk preflight" is a hard gate, not an advisory."""


@dataclass(frozen=True)
class DiskPreflightResult:
    path: str
    free_gb: float
    required_gb: float
    ok: bool


def default_db_path(program_root: Path | str) -> Path:
    return Path(program_root) / DEFAULT_DB_RELPATH


def disk_preflight(path: Path | str, *, min_free_gb: float = DEFAULT_MIN_FREE_GB) -> DiskPreflightResult:
    """Check free space on the drive/volume that will hold ``path`` (the
    db file, or its parent dir if the file doesn't exist yet -- either way
    ``shutil.disk_usage`` resolves the same underlying volume). Raises
    :class:`DiskPreflightError` when free space is below ``min_free_gb`` --
    callers that want the numbers WITHOUT the hard refusal (e.g. a doctor
    check, which reports rather than raises) call
    :func:`shutil.disk_usage` directly instead, or catch this exception.
    """
    p = Path(path)
    probe_dir = p if p.is_dir() else (p.parent if p.parent.exists() else Path(p.anchor or "."))
    usage = shutil.disk_usage(probe_dir)
    free_gb = usage.free / (1024**3)
    result = DiskPreflightResult(path=str(p), free_gb=round(free_gb, 2), required_gb=min_free_gb, ok=free_gb >= min_free_gb)
    if not result.ok:
        raise DiskPreflightError(
            f"disk preflight failed: {free_gb:.2f}GB free at {probe_dir} (need >= {min_free_gb}GB) -- "
            "the arXiv Kaggle embeddings zip is ~34.9GB and this build streams it (never fully "
            "extracts), but the destination db + the zip download itself still need real headroom; "
            "free up space or point [litapi.arxiv_index].db_path at a volume with more room"
        )
    return result


def open_arxiv_index_db(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating parent dirs as needed) the standalone index db.
    ``row_factory = sqlite3.Row`` (matches every other store connection in
    this repo -- ``trialerror.stores.connection.connect``'s own convention,
    reimplemented here rather than imported because that function also
    wires WAL/foreign_keys pragmas scoped to the four Section-3.2 program
    DBs this store deliberately is not one of)."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection, *, dims: int) -> VecBackend:
    """Idempotent schema creation. Returns which vector backend is live for
    THIS connection (per-connection, per ``try_load_sqlite_vec``'s own
    "sqlite-vec is per-CONNECTION, not per-database-file" rule --
    ``trialerror.retrieve.vecsearch``'s module docstring states the same rule
    for the knowledge-store tables; it applies identically here)."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BUILD_STATE_TABLE_NAME} (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {META_TABLE_NAME} (
            arxiv_id      TEXT PRIMARY KEY,
            title         TEXT,
            abstract      TEXT,
            categories    TEXT,
            authors       TEXT,
            published     TEXT,
            doi           TEXT,
            journal_ref   TEXT,
            ingested_ts   TEXT NOT NULL
        )
        """
    )

    # module docstring: always ATTEMPT the real extension first for this
    # store (never env-gated behind TRIALERROR_VEC_BACKEND the way the shared
    # knowledge-store factory is -- B.4a's "current read pattern is
    # slower" reasoning doesn't apply to a brand-new index with no
    # existing unconverted callers).
    loaded = try_load_sqlite_vec(conn)
    if loaded:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE_NAME} "
            f"USING vec0(arxiv_id TEXT PRIMARY KEY, embedding float[{int(dims)}])"
        )
        backend = VecBackend.SQLITE_VEC
    else:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {VEC_TABLE_NAME} (
                arxiv_id  TEXT PRIMARY KEY,
                dims      INTEGER NOT NULL,
                vector    BLOB NOT NULL
            )
            """
        )
        backend = VecBackend.FALLBACK

    with conn:
        conn.execute(
            f"INSERT INTO {BUILD_STATE_TABLE_NAME}(key, value) VALUES ('backend', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (backend.value,),
        )
        conn.execute(
            f"INSERT INTO {BUILD_STATE_TABLE_NAME}(key, value) VALUES ('dims', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(int(dims)),),
        )
    return backend


def get_build_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """The whole ``arxiv_index_build_state`` key/value table as a plain
    dict -- absent table (schema never created) returns ``{}`` rather than
    raising, so a doctor check can call this against a not-yet-built index
    and report "absent" cleanly."""
    try:
        rows = conn.execute(f"SELECT key, value FROM {BUILD_STATE_TABLE_NAME}").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["key"]: r["value"] for r in rows}


def set_build_state(conn: sqlite3.Connection, updates: dict[str, Any]) -> None:
    with conn:
        for key, value in updates.items():
            conn.execute(
                f"INSERT INTO {BUILD_STATE_TABLE_NAME}(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, "" if value is None else str(value)),
            )
        conn.execute(
            f"INSERT INTO {BUILD_STATE_TABLE_NAME}(key, value) VALUES ('last_updated_ts', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now(),),
        )


def row_count(conn: sqlite3.Connection) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {META_TABLE_NAME}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
