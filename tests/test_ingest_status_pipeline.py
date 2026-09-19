"""Tests for the two additive ``ingest status`` keys (``quality``,
``pipeline``), the ledger's per-document job lookup, and the
``pipeline_state`` derivation on every ledger shape -- including the ones
that look like something they are not (an environmentally-deferred job
looks ``pending``; a quality-refused document's jobs are all ``complete``).
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import pipeline, quality
from trialerror.ingest.pipeline_status import (
    PIPELINE_STATES,
    derive_pipeline_state,
    document_pipeline,
    job_stage,
)
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture
from tests._quality_fixtures import CLEAN_TEXT, GLUED_TEXT, seed_document


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        for k, v in kw.items():
            setattr(self, k, v)


def _drain(store, max_steps=10):
    for i in range(max_steps):
        if run_one(store, worker_id=f"w{i}")["status"] == "idle":
            break


def _ingest_one_doc(store, program_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = write_html_fixture(raw_dir / "doc.html")
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=path,
        created_by_launch=launch_id,
    )
    return result["document"]["doc_id"], launch_id


# ---------------------------------------------------------------------------
# ledger.list_jobs_for_doc -- the link without a doc_id column
# ---------------------------------------------------------------------------
def test_jobs_for_doc_matches_on_the_payload(store, program_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    rows = ledger.list_jobs_for_doc(store, doc_id)
    assert rows
    assert {r["match_kind"] for r in rows} == {"payload+job_id_prefix"}


def test_jobs_for_doc_falls_back_to_the_job_id_convention(store, program_root):
    """A row whose payload lost its doc_id is still this document's job --
    the id convention says so."""
    doc_id, launch_id = _ingest_one_doc(store, program_root)
    ledger.enqueue(
        store, kind="chunk", payload={"created_by_launch": launch_id},
        job_id=f"JOB-ingest-{doc_id}-chunk",
    )
    rows = {r["job_id"]: r["match_kind"] for r in ledger.list_jobs_for_doc(store, doc_id)}
    assert rows[f"JOB-ingest-{doc_id}-chunk"] == "job_id_prefix"


def test_jobs_for_doc_finds_a_payload_row_whose_id_is_unconventional(store, program_root):
    doc_id, launch_id = _ingest_one_doc(store, program_root)
    ledger.enqueue(store, kind="embed", payload={"doc_id": doc_id}, job_id="JOB-handmade-1")
    rows = {r["job_id"]: r["match_kind"] for r in ledger.list_jobs_for_doc(store, doc_id)}
    assert rows["JOB-handmade-1"] == "payload"


def test_jobs_for_doc_is_empty_for_a_document_with_no_jobs(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, CLEAN_TEXT)
    assert ledger.list_jobs_for_doc(store, doc_id) == []


def test_jobs_for_doc_does_not_match_another_documents_jobs(store, program_root):
    doc_a, launch_id = _ingest_one_doc(store, program_root)
    doc_b = seed_document(store, launch_id, CLEAN_TEXT)
    ledger.enqueue(store, kind="chunk", payload={"doc_id": doc_b}, job_id=f"JOB-ingest-{doc_b}-chunk")
    assert [r["job_id"] for r in ledger.list_jobs_for_doc(store, doc_b)] == [f"JOB-ingest-{doc_b}-chunk"]
    assert all(doc_b not in r["job_id"] for r in ledger.list_jobs_for_doc(store, doc_a))


# ---------------------------------------------------------------------------
# job_stage -- the inverse of the stage->kind map
# ---------------------------------------------------------------------------
def test_job_stage_reads_first_class_kinds_and_unwraps_custom():
    assert job_stage("normalize", {"doc_id": "D"}) == "normalize"
    assert job_stage("custom", {"handler": "djvu"}) == "djvu"
    assert job_stage("custom", json.dumps({"handler": "djvu"})) == "djvu"
    assert job_stage("custom", None) == "custom"
    assert job_stage("custom", "not json") == "custom"


def test_job_stage_agrees_with_the_forward_map():
    for stage in ("normalize", "chunk", "embed", "index", "ocr", "djvu"):
        kind, payload = pipeline.stage_job_kind_and_payload(stage, {"doc_id": "D"})
        assert job_stage(kind, payload) == stage


# ---------------------------------------------------------------------------
# derive_pipeline_state -- every ledger shape, as a pure function
# ---------------------------------------------------------------------------
def _job(**kw):
    row = {
        "job_id": "JOB-x", "kind": "chunk", "state": "pending", "attempts": 0,
        "max_attempts": 3, "failure_class": None, "last_error": None,
        "next_attempt_ts": None, "claimed_by": None, "payload": {"doc_id": "D"},
    }
    row.update(kw)
    return row


def test_no_jobs_at_all_never_raises_and_names_the_situation():
    out = derive_pipeline_state("registered", [])
    assert out["pipeline_state"] == "queued"
    assert "no job rows at all" in out["reason"]
    assert derive_pipeline_state("indexed", [])["pipeline_state"] == "complete"
    assert derive_pipeline_state(None, [])["pipeline_state"] == "queued"


def test_pending_is_queued():
    out = derive_pipeline_state("registered", [_job(state="pending")])
    assert out["pipeline_state"] == "queued"
    assert out["next_argv"] == ["trialerror", "jobs", "start-worker"]


@pytest.mark.parametrize("state", ["claimed", "running"])
def test_claimed_or_running_is_running(state):
    out = derive_pipeline_state("normalized", [_job(state=state, claimed_by="w1")])
    assert out["pipeline_state"] == "running"
    assert "w1" in out["reason"]


def test_paused_is_parked_and_points_at_resume():
    out = derive_pipeline_state("normalized", [_job(state="paused")])
    assert out["pipeline_state"] == "parked"
    assert out["next_argv"] == ["trialerror", "jobs", "resume", "--job-id", "JOB-x"]


def test_an_environmental_defer_is_parked_not_queued():
    """The shape that made this derivation necessary: the ledger says
    ``pending`` and the job is waiting on another machine.

    ``now`` is pinned rather than read from the clock -- the defer is parked
    because its time has not come, which is a statement about two
    timestamps, not about the column being set (fix pass V-8)."""
    out = derive_pipeline_state(
        "registered",
        [_job(state="pending", failure_class="environmental", next_attempt_ts="2026-09-12T10:00:00Z",
              last_error="no published result yet")],
        now="2026-09-12T09:00:00.000Z",
    )
    assert out["pipeline_state"] == "parked"
    assert "2026-09-12T10:00:00Z" in out["reason"]
    assert "no published result yet" in out["reason"]


def test_a_defer_whose_time_has_passed_is_queued_and_asks_for_a_worker():
    """V-8: next_attempt_ts was never compared to now, so a defer that had
    already elapsed still read "nothing here -- an environmental defer
    clears when the condition does" while the job actually needed a
    worker. The comparison is the ledger's own claim predicate."""
    out = derive_pipeline_state(
        "registered",
        [_job(state="pending", failure_class="environmental", next_attempt_ts="2026-09-12T10:00:00Z",
              last_error="no published result yet")],
        now="2026-09-12T11:00:00.000Z",
    )
    assert out["pipeline_state"] == "queued"
    assert out["next_argv"] == ["trialerror", "jobs", "start-worker"]
    assert "claimable now" in out["reason"]
    assert "2026-09-12T10:00:00Z" in out["reason"]


def test_an_abandoned_stage_outranks_a_retryable_sibling():
    """V-8: a document whose embed stage is dead read `failed-will-retry`
    off its healthier chunk stage -- a retry that cannot save it."""
    out = derive_pipeline_state(
        "chunked",
        [
            _job(job_id="JOB-chunk", kind="chunk", state="failed", attempts=1, max_attempts=3,
                 next_attempt_ts="2999-01-01T00:00:00Z"),
            _job(job_id="JOB-embed", kind="embed", state="abandoned", attempts=3, max_attempts=3,
                 last_error="the embed backend is gone"),
        ],
    )
    assert out["pipeline_state"] == "failed"
    assert out["stalled_stage"] == "embed"
    assert "the embed backend is gone" in out["reason"]


def test_a_retry_with_no_recorded_backoff_never_renders_a_none_time():
    """V-8: the reason read "scheduled to retry at None" for a row the
    ledger considers claimable right now."""
    out = derive_pipeline_state("registered", [_job(state="failed", attempts=1, max_attempts=3)])
    assert out["pipeline_state"] == "failed-will-retry"
    assert "None" not in out["reason"]
    assert "claimable" in out["reason"]


def test_a_retry_whose_time_has_passed_says_so():
    out = derive_pipeline_state(
        "registered",
        [_job(state="failed", attempts=1, max_attempts=3, next_attempt_ts="2026-09-12T10:00:00Z")],
        now="2026-09-12T11:00:00.000Z",
    )
    assert out["pipeline_state"] == "failed-will-retry"
    assert "has passed" in out["reason"]


def test_failed_with_attempts_left_is_failed_will_retry():
    out = derive_pipeline_state(
        "registered",
        [_job(state="failed", attempts=1, max_attempts=3, next_attempt_ts="2999-01-01T00:00:00Z",
              failure_class="logic", last_error="boom")],
    )
    assert out["pipeline_state"] == "failed-will-retry"
    assert "1 of 3" in out["reason"]


def test_failed_out_of_attempts_and_abandoned_are_both_terminal():
    for row in (_job(state="failed", attempts=3, max_attempts=3), _job(state="abandoned", attempts=3)):
        out = derive_pipeline_state("registered", [row])
        assert out["pipeline_state"] == "failed"
        assert "out of attempts" in out["reason"]


def test_all_complete_but_not_indexed_is_queued_and_says_nothing_is_enqueued():
    out = derive_pipeline_state("chunked", [_job(state="complete")])
    assert out["pipeline_state"] == "queued"
    assert "nothing is enqueued" in out["reason"]


def test_all_complete_and_indexed_is_complete():
    out = derive_pipeline_state("indexed", [_job(state="complete")])
    assert out["pipeline_state"] == "complete"
    assert out["next_action"] is None


def test_running_outranks_a_queued_sibling_and_parked_outranks_a_retry():
    assert derive_pipeline_state(
        "registered", [_job(state="pending"), _job(job_id="JOB-y", state="running")]
    )["pipeline_state"] == "running"
    assert derive_pipeline_state(
        "registered",
        [_job(state="failed", attempts=1), _job(job_id="JOB-y", state="paused")],
    )["pipeline_state"] == "parked"


def test_a_refused_document_outranks_every_job_row():
    out = derive_pipeline_state("failed", [_job(state="complete")], doc_id="DOC-1", refused=True)
    assert out["pipeline_state"] == "failed"
    assert "refused" in out["reason"]
    assert out["next_argv"] == ["trialerror", "ingest", "quality", "--doc-id", "DOC-1"]


def test_a_retracted_document_is_failed_with_nothing_to_do():
    out = derive_pipeline_state("failed", [_job(state="complete")], retracted=True)
    assert out["pipeline_state"] == "failed"
    assert "retracted" in out["reason"]
    assert out["next_argv"] is None


def test_a_bare_failed_status_is_reported_rather_than_explained_away():
    out = derive_pipeline_state("failed", [])
    assert out["pipeline_state"] == "failed"
    assert "no refusal or retraction record" in out["reason"]


def test_every_derived_state_is_in_the_declared_set():
    shapes = [
        ([], {}), ([_job()], {}), ([_job(state="running")], {}), ([_job(state="paused")], {}),
        ([_job(state="pending", failure_class="environmental", next_attempt_ts="t")], {}),
        ([_job(state="failed", attempts=1)], {}), ([_job(state="abandoned", attempts=3)], {}),
        ([_job(state="complete")], {}), ([], {"refused": True}), ([], {"retracted": True}),
    ]
    for stages, kw in shapes:
        assert derive_pipeline_state("registered", stages, **kw)["pipeline_state"] in PIPELINE_STATES


def test_a_ledger_row_with_missing_columns_does_not_raise():
    """Defensive: a hand-built or migrated row may not carry every column
    this derivation reads."""
    out = derive_pipeline_state("registered", [{"job_id": "JOB-z", "state": "failed"}])
    assert out["pipeline_state"] in PIPELINE_STATES


# ---------------------------------------------------------------------------
# document_pipeline over a real store
# ---------------------------------------------------------------------------
def test_document_pipeline_reports_the_whole_chain_after_a_full_run(store, program_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    _drain(store)
    out = document_pipeline(store, doc_id)
    stages = [s["stage"] for s in out["stages"]]
    assert stages[:2] == ["normalize", "chunk"]
    assert "index" in stages
    assert out["pipeline_state"] == "complete"
    assert out["match_kind"] == "payload+job_id_prefix"


def test_document_pipeline_on_a_fresh_document_is_queued(store, program_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    out = document_pipeline(store, doc_id)
    assert out["pipeline_state"] == "queued"
    assert out["stages"][0]["stage"] == "normalize"


def test_document_pipeline_degrades_without_an_offload_queue(store, program_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    out = document_pipeline(store, doc_id)
    assert out["offload"] is None
    assert all("offload" not in s for s in out["stages"])


def test_document_pipeline_annotates_a_published_offload_result(store, program_root):
    from trialerror.offload import protocol

    doc_id, _launch = _ingest_one_doc(store, program_root)
    job_id = ledger.list_jobs_for_doc(store, doc_id)[0]["job_id"]
    root = protocol.ensure_layout(protocol.offload_root(program_root))
    (root / "pending" / f"{job_id}.json").write_text(json.dumps({"job_id": job_id}), encoding="utf-8")

    out = document_pipeline(store, doc_id)
    assert out["offload"]["queue_dir"] == "offload"
    annotated = [s for s in out["stages"] if s["job_id"] == job_id][0]
    assert annotated["offload"]["queue_state"] == "pending"
    assert annotated["offload"]["manifest"].startswith("offload/pending/")


# ---------------------------------------------------------------------------
# the CLI's two additive keys
# ---------------------------------------------------------------------------
def _status(program_root, platform_root, doc_id) -> dict:
    args = _Args(program_root=str(program_root), platform_root=str(platform_root), doc_id=doc_id)
    return cli_ingest._cmd_status(args)


def test_status_keeps_every_existing_key_and_adds_two(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, CLEAN_TEXT)
    store.close()
    env = _status(program_root, platform_root, doc_id)
    assert env["ok"] is True
    result = env["result"]
    for key in ("document", "counts", "retracted", "retraction"):
        assert key in result
    assert set(result) == {"document", "counts", "retracted", "retraction", "quality", "pipeline"}


def test_status_quality_carries_the_four_numbers_and_the_thresholds(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, GLUED_TEXT)
    store.close()
    q = _status(program_root, platform_root, doc_id)["result"]["quality"]
    for key in quality.MEASURE_KEYS:
        assert key in q
    assert q["suspect"] is True
    assert q["reasons"]
    assert q["thresholds"]["glued_token_rate_max"] == quality.DEFAULT_THRESHOLDS["glued_token_rate_max"]


def test_status_quality_on_a_clean_document_is_not_suspect(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, [CLEAN_TEXT, CLEAN_TEXT], page_count=2)
    store.close()
    q = _status(program_root, platform_root, doc_id)["result"]["quality"]
    assert q["suspect"] is False
    assert q["reasons"] == []


def test_status_pipeline_reports_state_and_a_next_action(store, program_root, platform_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    store.close()
    env = _status(program_root, platform_root, doc_id)
    p = env["result"]["pipeline"]
    assert p["pipeline_state"] == "queued"
    assert p["next_action"]
    assert env["nextActions"][0]["argv"] == ["trialerror", "jobs", "start-worker"]


def test_status_on_a_document_with_no_jobs_still_answers(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, CLEAN_TEXT, status="indexed")
    store.close()
    p = _status(program_root, platform_root, doc_id)["result"]["pipeline"]
    assert p["stages"] == []
    assert p["match_kind"] == "none"
    assert p["pipeline_state"] == "complete"


def test_status_still_answers_for_an_unnormalized_document(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, None, status="registered")
    store.close()
    result = _status(program_root, platform_root, doc_id)["result"]
    assert result["quality"]["measurable"] is False
    assert result["quality"]["suspect"] is False
    assert result["pipeline"]["pipeline_state"] == "queued"


def test_status_of_an_unknown_document_is_still_an_error_envelope(store, program_root, platform_root):
    store.close()
    env = _status(program_root, platform_root, "DOC-nope")
    assert env["ok"] is False
    assert env["error"]["code"] == "document_not_found"


def test_status_is_json_serializable(store, program_root, platform_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    _drain(store)
    store.close()
    env = _status(program_root, platform_root, doc_id)
    assert json.loads(json.dumps(env))["result"]["pipeline"]["pipeline_state"] == "complete"
