"""``vec_chunks``: sqlite-vec loadable-extension factory with a pure-stdlib
fallback. Design Section 4.1: "``vec_chunks``: sqlite-vec virtual table
(chunk_id, vector) per active model_key" — one virtual table per embedding
model (each model has its own dimensionality, so a shared table doesn't
fit sqlite-vec's fixed-width ``vec0`` column type).

TRIALERROR-DEV-NOTE (build-brief judgment call): the brief directs "implement
the loadable-extension hookup behind a factory with a pure-stdlib fallback
so tests pass on a machine without the extension." ``sqlite-vec`` is
therefore an OPTIONAL dependency (``pyproject.toml``'s ``dev``/``vec``
extras), never a hard import at module load time — :func:`try_load_sqlite_vec`
probes for it at call time and both branches below are exercised by
``tests/test_stores_vecindex.py`` (the real extension when installed, and a
forced fallback via monkeypatch either way, so the fallback path is never
untested even in an environment that happens to have the extension).

M1 ships exactly this factory (DDL + backend bookkeeping) — no query/search
logic. Nearest-neighbor search over the resulting table(s) is M8's
(``trialerror/retrieve/``) job; a fallback-backend table is queryable with plain
SQL (cosine similarity computed in Python over the ``vector`` BLOBs) but M1
does not implement that query path, only the storage shape it will read.
"""

from __future__ import annotations

import os
import re
import sqlite3
import struct
from enum import Enum

from trialerror.util.timeutil import now

__all__ = [
    "VecBackend",
    "vec_table_name",
    "try_load_sqlite_vec",
    "ensure_vec_table",
    "serialize_vector_fallback",
    "deserialize_vector_fallback",
]

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


class VecBackend(str, Enum):
    SQLITE_VEC = "sqlite_vec"
    FALLBACK = "fallback"


def vec_table_name(model_key: str) -> str:
    """Deterministic, SQL-identifier-safe table name for a given
    ``model_key`` (design: "per active model_key")."""
    safe = _SAFE_NAME_RE.sub("_", model_key)
    return f"vec_chunks__{safe}"


def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the ``sqlite-vec`` loadable extension into
    ``conn``. Returns ``False`` (never raises) on any failure: the package
    not installed, the Python build lacking
    ``sqlite3.Connection.enable_load_extension`` (some distro/Homebrew
    builds omit it), or the extension failing to load for any other
    reason — every failure mode degrades to the fallback backend rather
    than propagating."""
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
        return False
    return True


def ensure_vec_table(
    conn: sqlite3.Connection, model_key: str, dims: int, *, _force_fallback: bool = False
) -> VecBackend:
    """Create (idempotently) the vector table for ``model_key`` and record
    it in the ``vec_index_registry`` bookkeeping table. Returns which
    backend was actually used.

    ``_force_fallback`` is a test-only seam (skips the sqlite-vec load
    attempt entirely) proving the fallback path works even on a machine
    that DOES have the extension installed.
    """
    table = vec_table_name(model_key)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vec_index_registry (
            model_key    TEXT PRIMARY KEY,
            table_name   TEXT NOT NULL,
            dims         INTEGER NOT NULL,
            backend      TEXT NOT NULL,
            created_ts   TEXT NOT NULL
        )
        """
    )

    # TRIALERROR-DEV-NOTE (B.4a, spikes/index_bakeoffs/BAKEOFF_REPORT.md): the
    # fallback table is the DEFAULT backend now. Production's read path does
    # ordinary row access, which the bake-off measured 7-17x SLOWER on vec0
    # than on the plain table at every scale (15k/50k/150k), with vec0's real
    # strength (native MATCH) unreachable until the B.4b query-mechanism work
    # lands. Formats are wire-compatible both ways, so this is reversible via
    # TRIALERROR_VEC_BACKEND=sqlite_vec (the opt-in for that future work).
    prefer_vec0 = os.environ.get("TRIALERROR_VEC_BACKEND", "fallback").strip().lower() in ("sqlite_vec", "vec0")
    loaded = try_load_sqlite_vec(conn) if (prefer_vec0 and not _force_fallback) else False
    if loaded:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
            f"USING vec0(chunk_id TEXT PRIMARY KEY, vector float[{int(dims)}])"
        )
        backend = VecBackend.SQLITE_VEC
    else:
        # Pure-stdlib fallback: a plain table shaped so a future reader can
        # tell exactly what it's standing in for (same chunk_id PK, same
        # per-row dims, vector stored as a packed-float32 BLOB via
        # `serialize_vector_fallback` -- the same wire format sqlite-vec
        # itself uses, so a later `sqlite-vec install` + backfill needs no
        # reserialization).
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                chunk_id    TEXT PRIMARY KEY,
                model_key   TEXT NOT NULL,
                dims        INTEGER NOT NULL,
                vector      BLOB NOT NULL
            )
            """
        )
        backend = VecBackend.FALLBACK

    with conn:
        conn.execute(
            """
            INSERT INTO vec_index_registry(model_key, table_name, dims, backend, created_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(model_key) DO UPDATE SET
                table_name = excluded.table_name,
                dims = excluded.dims,
                backend = excluded.backend
            """,
            (model_key, table, int(dims), backend.value, now()),
        )
    return backend


def serialize_vector_fallback(values: list[float]) -> bytes:
    """Pack a list of floats as little-endian float32 — the same on-disk
    shape ``sqlite_vec.serialize_float32`` produces, so fallback-written
    rows are forward-compatible with a later real sqlite-vec backfill."""
    return struct.pack(f"<{len(values)}f", *values)


def deserialize_vector_fallback(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))
