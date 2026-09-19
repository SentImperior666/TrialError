"""Acceptance criterion: "concurrent-writer test (2 procs, 1k appends,
zero loss)." Two real OS processes (``subprocess``, launched concurrently
via ``Popen`` — not ``multiprocessing``, which on Windows re-imports the
pytest entrypoint under the ``spawn`` start method) each append 1,000 rows
to the SAME WAL-mode database file. Design Section 3.2's cross-account-
safety note is exactly this claim: "Moving ledgers ... into SQLite WAL with
``busy_timeout=10s`` makes concurrent appends serialize instead of
colliding."

N-1 (C-0064 fix-tier3): every assertion below is already structural (total
count / per-worker count / distinct (worker, seq) pairs) -- there is no
wall-clock or timing-margin assert here to re-anchor. A transient failure
of this test WAS observed once during the M9 build under heavy host load;
it was environmental (``sqlite3.OperationalError('database is locked')``
from ``tests/_concurrent_writer_worker.py`` exhausting its busy_timeout
retry window under extreme contention, not a serialization bug -- see that
worker script's own TRIALERROR-DEV-NOTE), and is addressed there by widening
the WORKER's busy_timeout for this test only. Production's
``DEFAULT_BUSY_TIMEOUT_MS`` (design's real 10s pin) is untouched.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from trialerror.stores.connection import connect

_WORKER = Path(__file__).parent / "_concurrent_writer_worker.py"
_N_PER_WORKER = 1_000


def test_two_process_concurrent_writers_lose_nothing(tmp_path):
    db_path = tmp_path / "concurrency_probe.db"

    procs = [
        subprocess.Popen(
            [sys.executable, str(_WORKER), str(db_path), tag, str(_N_PER_WORKER)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("worker-a", "worker-b")
    ]

    outcomes = [(p.wait(timeout=120), p) for p in procs]
    for returncode, p in outcomes:
        if returncode != 0:
            _, stderr = p.communicate()
            raise AssertionError(f"writer subprocess failed (exit {returncode}):\n{stderr}")

    conn = connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM concurrency_probe").fetchone()[0]
    per_worker = {
        row[0]: row[1]
        for row in conn.execute("SELECT worker, COUNT(*) FROM concurrency_probe GROUP BY worker").fetchall()
    }
    # every (worker, seq) pair is unique -- proves no torn/duplicated writes,
    # not just a correct total count.
    distinct_pairs = conn.execute(
        "SELECT COUNT(DISTINCT worker || ':' || seq) FROM concurrency_probe"
    ).fetchone()[0]
    conn.close()

    assert total == 2 * _N_PER_WORKER
    assert per_worker == {"worker-a": _N_PER_WORKER, "worker-b": _N_PER_WORKER}
    assert distinct_pairs == 2 * _N_PER_WORKER


# Inline worker source for test_four_process_simultaneous_first_open_all_succeed
# below -- deliberately NOT a new file under tests/ (this fix's commit is
# pathspec-limited to this file + trialerror/stores/connection.py), and
# deliberately NOT reusing tests/_concurrent_writer_worker.py, whose CLI
# hardcodes a widened test-only busy_timeout (see its own TRIALERROR-DEV-NOTE)
# where this regression needs the small one passed explicitly instead.
_FOUR_WAY_WORKER_SRC = """
import sys
sys.path.insert(0, {repo_root!r})
from trialerror.stores.connection import connect

db_path, worker_tag, busy_timeout_ms = sys.argv[1], sys.argv[2], int(sys.argv[3])
conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
conn.execute(
    "CREATE TABLE IF NOT EXISTS concurrency_probe "
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, worker TEXT, seq INTEGER)"
)
conn.commit()
conn.execute("INSERT INTO concurrency_probe(worker, seq) VALUES (?, ?)", (worker_tag, 0))
conn.commit()
conn.close()
""".format(repo_root=str(Path(__file__).resolve().parents[1]))


def test_four_process_simultaneous_first_open_all_succeed(tmp_path):
    """Regression for the Linux/SQLite-3.45.1 "database is locked" defect:
    N=4 processes all call ``connect()`` on the SAME brand-new (not-yet-
    created) database file at once, each with a deliberately small
    ``busy_timeout`` (2s, well under production's 10s default) so that a
    non-retrying journal-mode switch fails fast instead of masking the bug
    by accident. See ``trialerror/stores/connection.py``'s TRIALERROR-DEV-NOTE
    for the mechanism: flipping journal_mode to WAL needs a brief EXCLUSIVE
    lock, and on SQLite 3.45.1 losing that race surfaces as an immediate
    SQLITE_BUSY that bypasses the busy-timeout retry entirely unless
    ``connect()`` retries the pragma itself.
    """
    db_path = tmp_path / "four_way_probe.db"
    n_workers = 4
    small_busy_timeout_ms = 2_000

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _FOUR_WAY_WORKER_SRC, str(db_path), f"worker-{i}", str(small_busy_timeout_ms)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(n_workers)
    ]

    outcomes = [(p.wait(timeout=60), p) for p in procs]
    for returncode, p in outcomes:
        if returncode != 0:
            _, stderr = p.communicate()
            raise AssertionError(f"writer subprocess failed (exit {returncode}):\n{stderr}")

    conn = connect(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM concurrency_probe").fetchone()[0]
    distinct_pairs = conn.execute(
        "SELECT COUNT(DISTINCT worker || ':' || seq) FROM concurrency_probe"
    ).fetchone()[0]
    conn.close()

    assert str(mode).lower() == "wal"
    assert total == n_workers
    assert distinct_pairs == n_workers
