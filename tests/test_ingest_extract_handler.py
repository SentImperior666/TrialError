"""Tests for the ``extract`` job handler (``trialerror.ingest.handlers.run_extract``)
made real: disk-to-disk judgments (design Section 6 preamble: "page text
never transits the orchestrator's context; agents get ids + stats back",
C-0007), driven end to end via ``trialerror.jobs.worker.run_one`` -- the same
claim-run-settle loop a real detached worker uses (``tests/test_ingest_handlers.py``'s
own convention for the other stage handlers)."""

from __future__ import annotations

import json

from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.util.ids import new_id

from tests._retrieve_fixtures import build_small_corpus

from .test_ingest_extract import _fake_judge, _open_chunk_id


def _judgments_payload(corpus):
    judge = _fake_judge(corpus)
    return {_open_chunk_id(corpus): judge({"chunk_id": _open_chunk_id(corpus)})}


def test_extract_job_stub_when_no_judgments_path_given(store, program_root):
    corpus = build_small_corpus(store)
    job_id = f"JOB-extract-{new_id('R')}"
    ledger.enqueue(store, kind="extract", payload={"doc_id": corpus["open_doc_id"], "created_by_launch": corpus["launch_id"]}, job_id=job_id)
    result = run_one(store, worker_id="w0", job_id=job_id)
    assert result["status"] == "complete"

    job = ledger.get_job(store, job_id)
    checkpoint = json.loads(job["checkpoint"])
    assert checkpoint["claims_extracted"] == 0
    assert "note" in checkpoint

    assert store.knowledge.execute("SELECT COUNT(*) FROM record WHERE register_key='kg_extract_pending'").fetchone()[0] == 0


def test_extract_job_real_with_judgments_path_queues_candidates_and_checkpoints(store, program_root):
    corpus = build_small_corpus(store)
    judgments_path = program_root / "judgments.json"
    judgments_path.write_text(json.dumps(_judgments_payload(corpus)), encoding="utf-8")

    job_id = f"JOB-extract-{new_id('R')}"
    ledger.enqueue(
        store, kind="extract",
        payload={"doc_id": corpus["open_doc_id"], "created_by_launch": corpus["launch_id"], "judgments_path": str(judgments_path)},
        job_id=job_id,
    )
    result = run_one(store, worker_id="w0", job_id=job_id)
    assert result["status"] == "complete"

    job = ledger.get_job(store, job_id)
    checkpoint = json.loads(job["checkpoint"])
    assert checkpoint["done"] is True
    assert checkpoint["chunks_processed"] == 1
    assert checkpoint["entities_queued"] == 2
    assert checkpoint["relations_queued"] == 1
    assert checkpoint["claims_queued"] == 1

    pending = store.knowledge.execute(
        "SELECT COUNT(*) FROM record WHERE register_key='kg_extract_pending'"
    ).fetchone()[0]
    assert pending == 4


def test_extract_job_resumed_run_skips_already_processed_chunk(store, program_root):
    """A second run against the SAME doc_id (the resume-after-restart
    shape) must not re-queue duplicate candidates for a chunk the ledger
    already has a ``kg_extract_chunk_processed`` event for."""
    corpus = build_small_corpus(store)
    judgments_path = program_root / "judgments.json"
    judgments_path.write_text(json.dumps(_judgments_payload(corpus)), encoding="utf-8")

    payload = {"doc_id": corpus["open_doc_id"], "created_by_launch": corpus["launch_id"], "judgments_path": str(judgments_path)}
    job_id_1 = f"JOB-extract-{new_id('R')}"
    ledger.enqueue(store, kind="extract", payload=payload, job_id=job_id_1)
    run_one(store, worker_id="w0", job_id=job_id_1)

    job_id_2 = f"JOB-extract-{new_id('R')}"
    ledger.enqueue(store, kind="extract", payload=payload, job_id=job_id_2)
    result_2 = run_one(store, worker_id="w1", job_id=job_id_2)
    assert result_2["status"] == "complete"

    job_2 = ledger.get_job(store, job_id_2)
    checkpoint_2 = json.loads(job_2["checkpoint"])
    assert checkpoint_2["chunks_processed"] == 0
    assert checkpoint_2["chunks_skipped"] == 1

    pending = store.knowledge.execute("SELECT COUNT(*) FROM record WHERE register_key='kg_extract_pending'").fetchone()[0]
    assert pending == 4  # unchanged -- no duplicate queueing


def test_extract_job_missing_judgments_file_raises_logic_failure(store, program_root):
    corpus = build_small_corpus(store)
    job_id = f"JOB-extract-{new_id('R')}"
    ledger.enqueue(
        store, kind="extract",
        payload={"doc_id": corpus["open_doc_id"], "created_by_launch": corpus["launch_id"], "judgments_path": str(program_root / "nope.json")},
        job_id=job_id,
    )
    result = run_one(store, worker_id="w0", job_id=job_id)
    assert result["status"] == "failed"
