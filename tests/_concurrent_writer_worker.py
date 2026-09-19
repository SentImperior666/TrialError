"""Standalone worker script for the concurrent-writer acceptance test
(``tests/test_stores_concurrency.py``). Run as a real separate OS process
(``subprocess``, not ``multiprocessing`` -- avoids Windows `spawn`
re-importing pytest's own entrypoint) so the "2 procs" wording in the
acceptance criterion is literal, not simulated with threads.

Usage: ``python _concurrent_writer_worker.py <db_path> <worker_tag> <n>``
Appends ``n`` rows to a throwaway table, each insert its own transaction
(worst case for WAL contention -- proves ``busy_timeout`` actually
serializes concurrent writers instead of raising "database is locked").

TRIALERROR-DEV-NOTE (N-1, C-0064 fix-tier3 -- busy_timeout widened, TEST ONLY):
the M9 build log (``WKP-063 events/build-M9.jsonl``) recorded one transient
failure of this exact worker under load; the parent test
(``test_two_process_concurrent_writers_lose_nothing``) has no wall-clock or
timing-margin assert to re-anchor -- every assertion there is already
structural (total row count, per-worker count, distinct (worker, seq)
pairs). The transient is environmental: 1,000 single-row commits per
worker, worst-case WAL contention, against the production
``DEFAULT_BUSY_TIMEOUT_MS`` (10s, design Section 3.2's real pin -- left
untouched in ``trialerror/stores/connection.py``). Under EXTREME host load (a
busy build session running many concurrent subprocesses, as the M9 note
describes) the OTHER writer's own commits can be scheduler-delayed enough
that 10s of retrying isn't enough headroom, and SQLite raises
``sqlite3.OperationalError('database is locked')`` -- a real, if rare,
environmental exhaustion of the busy-timeout retry window, not a bug in
the serialization mechanism itself (a low-load rerun of the SAME build
event passed). Widened to 60s HERE ONLY (this worker script; production's
10s pin is unchanged) purely to buy this test more headroom on a busy CI
box -- it does not change what the test proves.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trialerror.stores.connection import connect  # noqa: E402

#: TEST-ONLY widening -- see module TRIALERROR-DEV-NOTE. Production connections
#: (``trialerror.stores.connection.DEFAULT_BUSY_TIMEOUT_MS``) are unaffected.
_TEST_BUSY_TIMEOUT_MS = 60_000


def main() -> None:
    db_path, worker_tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    conn = connect(db_path, busy_timeout_ms=_TEST_BUSY_TIMEOUT_MS)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS concurrency_probe (id INTEGER PRIMARY KEY AUTOINCREMENT, worker TEXT, seq INTEGER)"
    )
    conn.commit()
    for i in range(n):
        conn.execute("INSERT INTO concurrency_probe(worker, seq) VALUES (?, ?)", (worker_tag, i))
        conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
