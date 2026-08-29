"""build-v2-summary: the ``summarize`` job handler
(:mod:`trialerror.summarize.handlers`) -- envelope-producing, riding M2's ledger
like M7's ``extract`` does (this handler's own module docstring). Driven
end to end via ``trialerror.jobs.worker.run_one`` (the same claim-run-settle
loop a real detached worker uses), exactly like ``tests/
test_ingest_handlers.py`` drives M7's handlers."""

from __future__ import annotations

import json

from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.summarize.api import build_summary_envelope, get_summary, store_summary

from tests._summarize_fixtures import build_small_corpus


def _run(store, job_id: str, payload: dict) -> dict:
    result = run_one(store, job_id=job_id, kind="custom", payload=payload)
    job = ledger.get_job(store, job_id)
    return {"result": result, "job": job}


def test_explicit_target_with_judgment_stores_a_summary(store):
    corpus = build_small_corpus(store)
    out = _run(
        store,
        "JOB-sum-1",
        {
            "handler": "summarize",
            "subject_kind": "document",
            "created_by_launch": corpus["launch_id"],
            "targets": [{"subject_id": corpus["open_doc_id"]}],
            "judgments": {corpus["open_doc_id"]: "A batch-authored overview of the open source."},
        },
    )
    assert out["result"]["status"] == "complete"
    assert out["job"]["state"] == "complete"

    current = get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert current is not None
    assert current["body"] == "A batch-authored overview of the open source."

    checkpoint = json.loads(out["job"]["checkpoint"])
    assert checkpoint["written"] == {corpus["open_doc_id"]: current["summary_id"]}
    assert checkpoint["pending_envelopes"] == []
    assert checkpoint["skipped"] == []


def test_explicit_target_without_judgment_records_a_pending_envelope(store):
    corpus = build_small_corpus(store)
    out = _run(
        store,
        "JOB-sum-2",
        {
            "handler": "summarize",
            "subject_kind": "document",
            "created_by_launch": corpus["launch_id"],
            "targets": [{"subject_id": corpus["open_doc_id"]}],
        },
    )
    assert out["result"]["status"] == "complete"
    assert get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"]) is None

    checkpoint = json.loads(out["job"]["checkpoint"])
    assert checkpoint["written"] == {}
    assert len(checkpoint["pending_envelopes"]) == 1
    pending = checkpoint["pending_envelopes"][0]
    assert pending["subject_id"] == corpus["open_doc_id"]
    assert pending["kind"] == "summary"


def test_auto_discovery_processes_every_missing_document(store):
    corpus = build_small_corpus(store)
    out = _run(
        store,
        "JOB-sum-3",
        {
            "handler": "summarize",
            "created_by_launch": corpus["launch_id"],
            "judgments": {
                corpus["open_doc_id"]: "open overview",
                corpus["restricted_doc_id"]: "restricted overview, no quotes",
            },
        },
    )
    assert out["result"]["status"] == "complete"
    checkpoint = json.loads(out["job"]["checkpoint"])
    assert set(checkpoint["written"]) == {corpus["open_doc_id"], corpus["restricted_doc_id"]}
    assert get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"])["body"] == "open overview"
    assert get_summary(store, subject_kind="document", subject_id=corpus["restricted_doc_id"])["body"] == "restricted overview, no quotes"


def test_auto_discovery_skips_a_subject_already_current_and_unstale(store):
    corpus = build_small_corpus(store)
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    existing = store_summary(store, envelope=envelope, body="already summarized", issued_by_launch=corpus["launch_id"])

    out = _run(
        store,
        "JOB-sum-4",
        {
            "handler": "summarize",
            "created_by_launch": corpus["launch_id"],
            "judgments": {corpus["restricted_doc_id"]: "restricted overview, no quotes"},
        },
    )
    assert out["result"]["status"] == "complete"
    checkpoint = json.loads(out["job"]["checkpoint"])
    # open_doc_id already had a current, non-stale summary and no judgment
    # was supplied for it this run -- skipped, not rewritten.
    assert corpus["open_doc_id"] not in checkpoint["written"]
    assert corpus["restricted_doc_id"] in checkpoint["written"]

    unchanged = get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert unchanged["summary_id"] == existing["summary_id"]
    assert unchanged["body"] == "already summarized"


def test_collection_subject_kind_requires_explicit_targets(store):
    corpus = build_small_corpus(store)
    out = _run(
        store,
        "JOB-sum-5",
        {"handler": "summarize", "subject_kind": "collection", "created_by_launch": corpus["launch_id"]},
    )
    assert out["result"]["status"] in ("failed", "abandoned")
    assert "collection" in out["job"]["last_error"]


def test_unnormalized_target_is_skipped_not_fatal(store):
    from trialerror.stores.writer import insert
    from trialerror.util.ids import new_id

    corpus = build_small_corpus(store)
    empty_doc_id = new_id("DOC")
    insert(
        store, "document",
        {
            "doc_id": empty_doc_id, "source_id": corpus["open_source_id"], "rel_path": "archive/empty.md",
            "media_type": "md", "normalizer_id": "fixture", "normalizer_version": "1",
            "sha256": "0" * 64, "status": "registered",
        },
    )
    out = _run(
        store,
        "JOB-sum-6",
        {
            "handler": "summarize",
            "created_by_launch": corpus["launch_id"],
            "targets": [{"subject_id": empty_doc_id}, {"subject_id": corpus["open_doc_id"]}],
            "judgments": {corpus["open_doc_id"]: "fine overview"},
        },
    )
    assert out["result"]["status"] == "complete"  # one bad target never fails the whole job
    checkpoint = json.loads(out["job"]["checkpoint"])
    assert len(checkpoint["skipped"]) == 1
    assert checkpoint["skipped"][0]["subject_id"] == empty_doc_id
    assert checkpoint["written"] == {corpus["open_doc_id"]: get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"])["summary_id"]}


def test_collection_targets_given_explicitly_are_honored(store):
    corpus = build_small_corpus(store)
    member_ids = [corpus["open_doc_id"], corpus["restricted_doc_id"]]
    out = _run(
        store,
        "JOB-sum-7",
        {
            "handler": "summarize",
            "subject_kind": "collection",
            "created_by_launch": corpus["launch_id"],
            "targets": [{"subject_id": "COLL-both", "doc_ids": member_ids}],
            "judgments": {"COLL-both": "an overview of both sources together"},
        },
    )
    assert out["result"]["status"] == "complete"
    current = get_summary(store, subject_kind="collection", subject_id="COLL-both")
    assert current is not None
    assert current["body"] == "an overview of both sources together"
    assert json.loads(current["source_doc_ids"]) == member_ids
