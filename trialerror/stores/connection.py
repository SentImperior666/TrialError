"""Per-DB connection management: WAL mode, busy timeouts, same-file foreign
keys on. Design Section 12 (M1 row) + Section 3.2 ("SQLite WAL") + Section
3.2's cross-account-safety note: "Moving ledgers ... into SQLite WAL with
``busy_timeout=10s`` makes concurrent appends serialize instead of
colliding."

Every connection this module opens shares the same pragmas, so "per-DB
connection management" means exactly one function
(:func:`connect`) that every ``open_*_db`` helper in
``trialerror.stores.store`` funnels through — no subsystem opens sqlite3
directly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "connect"]

#: Design Section 3.2: "``busy_timeout=10s``" — a concurrent writer blocks
#: (and retries internally, per SQLite's busy handler) for up to this long
#: before raising ``sqlite3.OperationalError('database is locked')``, rather
#: than failing immediately.
DEFAULT_BUSY_TIMEOUT_MS = 10_000


def connect(
    path: Path | str,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    foreign_keys: bool = True,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open one WAL-mode connection to the SQLite file at ``path``.

    ``path``'s parent directory is created if missing (mirrors
    :func:`trialerror.util.atomic.atomic_write_bytes`'s own "create the parent"
    convenience) — except for ``read_only`` connections, which must not
    have side effects against a store they're only inspecting (doctor
    checks use this).

    Pragmas applied on every connection: ``journal_mode=WAL`` (concurrent
    readers don't block the writer, and vice versa — the mode this whole
    design is named "SQLite-WAL" after), ``busy_timeout`` (serializes
    concurrent writers instead of racing them), and ``foreign_keys=ON``
    (same-file ``FK`` columns are enforced by SQLite itself; cross-file
    ``XID`` columns are NOT covered by this pragma — see
    ``trialerror.stores.xid`` for those).
    """
    p = Path(path)
    if read_only:
        if not p.is_file():
            raise FileNotFoundError(f"read-only connect: no such database file: {p}")
        uri = f"file:{p.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=busy_timeout_ms / 1000)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")
    return conn
