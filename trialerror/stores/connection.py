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
import time
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
    # busy_timeout goes on BEFORE the journal-mode switch below -- see the
    # TRIALERROR-DEV-NOTE there for why setting it after does not help.
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")

    if read_only:
        # Unchanged from before: a read-only connection can't obtain the
        # write access this pragma needs to actually flip a file that
        # isn't already WAL, so on such a file SQLite treats this as a
        # silent no-op (returns the file's existing mode rather than
        # raising) instead of switching it. No retry, no verification --
        # preserve that behaviour exactly rather than forcing a mode
        # switch a read-only connection has no business making.
        conn.execute("PRAGMA journal_mode = WAL")
    else:
        # TRIALERROR-DEV-NOTE: on the sandbox host (Ubuntu 24.04, SQLite
        # 3.45.1) two writers racing connect() on a brand-new database file
        # hit `sqlite3.OperationalError: database is locked` right here, 3/3
        # runs, in well under a second -- see
        # tests/test_stores_concurrency.py. Flipping journal_mode to WAL
        # needs a brief EXCLUSIVE lock, and on 3.45.1 the collision with the
        # other connection doing the same first-open switch surfaces as an
        # immediate SQLITE_BUSY that bypasses SQLite's own busy-handler
        # retry loop -- so the `timeout=`/busy_timeout we already set above
        # never gets a chance to absorb it (SQLite 3.49.1 on Windows DEV
        # does not reproduce this). Design lane-0 D14 (disaster containment)
        # and the engram-F7 migration lock both lean on "first writer flips
        # WAL, everyone else waits" holding on the sandbox host, not just on
        # DEV, so we retry the pragma ourselves rather than trust the
        # driver's internal handler for this one statement.
        deadline = time.monotonic() + busy_timeout_ms / 1000
        delay_s = 0.01  # ~10ms, doubled each retry, bounded by busy_timeout_ms
        while True:
            try:
                mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                break
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                remaining = deadline - time.monotonic()
                if ("locked" not in message and "busy" not in message) or remaining <= 0:
                    raise
                time.sleep(min(delay_s, remaining))
                delay_s *= 2
        # A fresh file another connection already switched returns "wal"
        # immediately (no lock needed to read the current mode); anything
        # else here means the switch silently didn't take.
        if str(mode).lower() != "wal":
            raise sqlite3.OperationalError(
                f"PRAGMA journal_mode=WAL did not take effect (got {mode!r}) for {p}"
            )

    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")
    return conn
