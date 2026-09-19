"""Integration tests for the ``arxiv_index_build`` job handler
(:mod:`trialerror.arxiv_index.handlers`), riding the real jobs ledger
(``trialerror.jobs.worker.run_one``) end to end -- auto-discovered purely
because ``trialerror/arxiv_index/handlers.py`` exists (no manual registration
needed in these tests, unlike ``tests/_job_handlers.py``'s own test-only
handlers)."""

from __future__ import annotations

import json

from trialerror.arxiv_index.store import META_TABLE_NAME, get_build_state, row_count, open_arxiv_index_db
from trialerror.events.api import tail_events
from trialerror.jobs import ledger
from trialerror.jobs.worker import make_worker_id, run_one
from tests._arxiv_index_fixtures import write_small_fixture_zip
from tests._ingest_fixtures import bootstrap_launch


def _base_payload(zip_path, db_path, *, launch_id=None, **overrides):
    payload = {
        "handler": "arxiv_index_build",
        "zip_path": str(zip_path),
        "db_path": str(db_path),
        "dims": 8,
        "batch_size": 4,
        "min_free_gb": 0.001,  # tiny -- never fails preflight on a real dev machine
        "created_by_launch": launch_id,
    }
    payload.update(overrides)
    return payload


def test_handler_happy_path_via_run_one(store, program_root, tmp_path):
    launch_id = bootstrap_launch(store)
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=16, dims=8)
    db_path = program_root / "data" / "arxiv_index.sqlite3"
    payload = _base_payload(zip_path, db_path, launch_id=launch_id)

    job_id = "JOB-arxiv-index-build-test-happy"
    result = run_one(store, worker_id=make_worker_id(), job_id=job_id, kind="custom", payload=payload)

    assert result["status"] == "complete"
    job_row = ledger.get_job(store, job_id)
    assert job_row["state"] == "complete"
    checkpoint = json.loads(job_row["checkpoint"])
    assert checkpoint["rows_ingested"] == 16

    conn = open_arxiv_index_db(db_path)
    try:
        assert row_count(conn) == 16
        state = get_build_state(conn)
        assert state["status"] == "complete"
    finally:
        conn.close()

    started = tail_events(store, event_type="arxiv_index_build_started")
    completed = tail_events(store, event_type="arxiv_index_build_complete")
    assert len(started) == 1
    assert len(completed) == 1
    assert completed[0]["payload"]["rows_ingested"] == 16


def test_handler_missing_required_payload_fields_fails_cleanly(store, program_root):
    job_id = "JOB-arxiv-index-build-test-missing-fields"
    result = run_one(store, worker_id=make_worker_id(), job_id=job_id, kind="custom", payload={"handler": "arxiv_index_build"})
    assert result["status"] == "failed"
    job_row = ledger.get_job(store, job_id)
    assert "zip_path" in job_row["last_error"]


def test_handler_nonexistent_zip_fails_cleanly(store, program_root, tmp_path):
    job_id = "JOB-arxiv-index-build-test-no-zip"
    payload = _base_payload(tmp_path / "does-not-exist.zip", program_root / "data" / "arxiv_index.sqlite3")
    result = run_one(store, worker_id=make_worker_id(), job_id=job_id, kind="custom", payload=payload)
    assert result["status"] == "failed"
    job_row = ledger.get_job(store, job_id)
    assert "does not exist" in job_row["last_error"]


def test_handler_disk_preflight_failure_fails_the_job(store, program_root, tmp_path):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=3, dims=8)
    db_path = program_root / "data" / "arxiv_index.sqlite3"
    payload = _base_payload(zip_path, db_path, min_free_gb=10_000_000.0)  # impossible floor

    job_id = "JOB-arxiv-index-build-test-preflight"
    result = run_one(store, worker_id=make_worker_id(), job_id=job_id, kind="custom", payload=payload)
    assert result["status"] == "failed"
    job_row = ledger.get_job(store, job_id)
    assert "disk preflight failed" in job_row["last_error"]
    # never even created the db (preflight runs before open_arxiv_index_db)
    assert not db_path.exists()


def test_handler_kill_mid_build_then_resume_via_ledger_checkpoint(store, program_root, tmp_path):
    """The full end-to-end version of the ingest-level resume test:
    kill via the handler's own test-only ``_raise_after_rows`` seam,
    confirm the job settles 'failed' with a partial checkpoint retained,
    then resume it through the ledger (same pattern
    tests/test_jobs_worker.py uses to force immediate re-eligibility:
    directly clearing ``next_attempt_ts``) and confirm byte-identical
    final state -- no duplicates, nothing missing."""
    launch_id = bootstrap_launch(store)
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=20, dims=8)
    db_path = program_root / "data" / "arxiv_index.sqlite3"
    payload = _base_payload(zip_path, db_path, launch_id=launch_id, _raise_after_rows=8, progress_event_every=1)

    job_id = "JOB-arxiv-index-build-test-kill-resume"
    r1 = run_one(store, worker_id=make_worker_id(), job_id=job_id, kind="custom", payload=payload)
    assert r1["status"] == "failed"

    job_row = ledger.get_job(store, job_id)
    assert job_row["state"] == "failed"
    checkpoint = json.loads(job_row["checkpoint"])
    partial = checkpoint["rows_ingested"]
    assert 0 < partial < 20

    conn = open_arxiv_index_db(db_path)
    try:
        assert row_count(conn) == partial
    finally:
        conn.close()

    # resume: strip the kill-seam from the job's own persisted payload
    # (claim_or_create reuses the EXISTING row's payload for an
    # already-created job_id -- a fresh payload argument only applies at
    # first creation) and force immediate re-eligibility, same idiom
    # tests/test_jobs_worker.py::test_... uses.
    resumed_payload = dict(payload)
    resumed_payload.pop("_raise_after_rows")
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET payload = ?, next_attempt_ts = NULL WHERE job_id = ?",
            (json.dumps(resumed_payload), job_id),
        )

    r2 = run_one(store, worker_id=make_worker_id(), job_id=job_id, kind="custom", payload=resumed_payload)
    assert r2["status"] == "complete"

    job_row2 = ledger.get_job(store, job_id)
    final_checkpoint = json.loads(job_row2["checkpoint"])
    assert final_checkpoint["rows_ingested"] == 20

    conn = open_arxiv_index_db(db_path)
    try:
        assert row_count(conn) == 20
        ids = {r["arxiv_id"] for r in conn.execute(f"SELECT arxiv_id FROM {META_TABLE_NAME}").fetchall()}
        assert ids == {f"9999.{i:05d}" for i in range(20)}
    finally:
        conn.close()

    progress_events = tail_events(store, event_type="arxiv_index_build_progress", limit=1000)
    assert len(progress_events) >= 1  # at least one progress event was emitted across both attempts
