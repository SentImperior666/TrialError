"""B-1 fix round: end-to-end tests of the two REAL production paths the
review's probe E ran against a fresh interpreter -- SessionStart's canary,
and the ``probe_vector_canary`` job handler -- with stores opened the way
production actually opens them, not the way a unit test's own fixture
opens them. Before this fix round, neither path ever wrote a row to
``probe_run``, so `tests/test_probes_canary.py`'s unit tests (which import
`trialerror.retrieve.probes` at module level and open `check_same_thread=
False` stores) passed while production silently did nothing (B-1).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.probes import registry as preg
from trialerror.probes.stamps import answer_stamp
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._retrieve_fixtures import build_small_corpus

SESSION_START = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "session_start.py"


def _run_session_start(payload: dict, *, platform_root: Path, probes_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    env["TRIALERROR_PROBES_DIR"] = str(probes_dir)
    # See tests/test_hooks_usage_capture.py's own note: a bare `import
    # trialerror` in the subprocess otherwise resolves to whatever copy pip
    # installed, which in a git worktree can be a DIFFERENT checkout.
    tree_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([tree_root, env["PYTHONPATH"]]) if env.get("PYTHONPATH") else tree_root
    return subprocess.run(
        [sys.executable, str(SESSION_START)], input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=60,
    )


def test_session_start_end_to_end_with_a_broken_fts_index_records_a_fail_row_and_degrades_the_stamp(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    probes_dir = tmp_path / "probes"

    store = open_store(program_root, platform_root=platform_root)
    build_small_corpus(store)
    # Break the full-text index: every chunk is still there, but no query
    # can ever find one -- exactly design Section 3.4's canary failure case.
    # `with store.knowledge:` commits before close() -- a bare .execute()
    # leaves the DELETE in an uncommitted implicit transaction that .close()
    # then silently rolls back.
    with store.knowledge:
        store.knowledge.execute("DELETE FROM chunk_fts")
    store.close()

    payload = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(program_root)}
    proc = _run_session_start(payload, platform_root=platform_root, probes_dir=probes_dir)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("SEARCH DEGRADED: full-text search did not find a known passage")

    verify_store = open_store(program_root, platform_root=platform_root)
    try:
        rows = verify_store.platform.execute(
            "SELECT * FROM probe_run WHERE name = 'fulltext_canary' ORDER BY started_ts DESC"
        ).fetchall()
        assert len(rows) == 1, "the canary's result was never recorded in probe_run (B-1)"
        assert rows[0]["status"] == "fail"

        stamp = answer_stamp(verify_store)
        assert stamp["canary"]["fulltext"] == "fail"
        assert stamp["degraded"] is True
    finally:
        verify_store.close()


def test_session_start_end_to_end_with_a_healthy_index_records_a_pass_row(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    probes_dir = tmp_path / "probes"

    store = open_store(program_root, platform_root=platform_root)
    build_small_corpus(store)
    store.close()

    payload = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(program_root)}
    proc = _run_session_start(payload, platform_root=platform_root, probes_dir=probes_dir)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "SEARCH DEGRADED" not in out["hookSpecificOutput"]["additionalContext"]

    verify_store = open_store(program_root, platform_root=platform_root)
    try:
        row = verify_store.platform.execute(
            "SELECT * FROM probe_run WHERE name = 'fulltext_canary' ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "pass"
        assert answer_stamp(verify_store)["degraded"] is False
    finally:
        verify_store.close()


# ---------------------------------------------------------------------------
# the probe_vector_canary job handler, as a real worker actually calls it
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """A real worker process starts with an EMPTY probe registry -- nothing
    has imported trialerror.retrieve.probes yet. Simulated here by clearing
    the registry the OTHER test files' module-level imports already
    populated, rather than relying on process isolation."""
    snapshot = dict(preg._REGISTRY)
    preg.clear_registry()
    yield
    preg.clear_registry()
    preg._REGISTRY.update(snapshot)


class _FakeJobContext:
    """The handler-facing surface of trialerror.jobs.worker.JobContext this
    handler actually uses: .store and .payload."""

    def __init__(self, store, payload):
        self.store = store
        self._payload = payload

    @property
    def payload(self):
        return self._payload


def test_the_job_handler_with_an_empty_registry_and_a_default_open_store_records_a_pass(tmp_path):
    """The exact production shape (review's probe E, step 4): a worker's
    Store opened with the ORDINARY open_store() default
    (check_same_thread=True), and a probe registry that starts empty
    because nothing on that path has imported trialerror.retrieve.probes."""
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()

    store = open_store(program_root, platform_root=platform_root)  # default check_same_thread=True
    try:
        build_small_corpus(store)
        assert preg.registered_probes() == {}, "the registry must start empty, matching a real worker process"

        from trialerror.retrieve.handlers import run_probe_vector_canary

        run_probe_vector_canary(_FakeJobContext(store, {"host": "dev"}))

        row = store.platform.execute(
            "SELECT * FROM probe_run WHERE name = 'vector_canary' ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "the handler wrote no row -- discovery or the cross-thread connection failed"
        assert row["status"] == "pass"
        assert row["program_id"] is not None  # B-2: scoped to this job's own program
    finally:
        store.close()
