"""Tests for the two extraction doctor checks (design Section 11 v1
deliverable 3): ``extract_pending_backlog``, ``entity_dupes_suspected``."""

from __future__ import annotations

from trialerror.ingest import extract as extract_api
from trialerror.ingest.checks import check_entity_dupes_suspected, check_extract_pending_backlog
from trialerror.util.doctor import DoctorContext

from tests._retrieve_fixtures import build_small_corpus

from .test_ingest_extract import _fake_judge, _open_chunk_id, _restricted_chunk_id


def test_extract_pending_backlog_skips_without_a_program_root():
    result = check_extract_pending_backlog(DoctorContext())
    assert result.status == "skip"


def test_extract_pending_backlog_passes_on_an_empty_queue(store, program_root):
    build_small_corpus(store)
    result = check_extract_pending_backlog(DoctorContext(program_root=program_root))
    assert result.status == "pass"
    assert result.details["count"] == 0


def test_extract_pending_backlog_warns_on_pending_candidates(store, program_root):
    corpus = build_small_corpus(store)
    extracted = extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=_fake_judge(corpus), created_by_launch=corpus["launch_id"])
    result = check_extract_pending_backlog(DoctorContext(program_root=program_root))
    assert result.status == "warn"
    assert result.details["count"] == 4
    all_ids = extracted["record_ids"]["entities"] + extracted["record_ids"]["relations"] + extracted["record_ids"]["claims"]
    assert set(result.details["record_ids"]) == set(all_ids)


def test_extract_pending_backlog_shrinks_after_accept_and_reject(store, program_root):
    corpus = build_small_corpus(store)
    extracted = extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=_fake_judge(corpus), created_by_launch=corpus["launch_id"])
    for rid in extracted["record_ids"]["entities"]:
        extract_api.accept_candidate(store, rid, by_launch=corpus["launch_id"])
    extract_api.reject_candidate(store, extracted["record_ids"]["claims"][0], by_launch=corpus["launch_id"])

    result = check_extract_pending_backlog(DoctorContext(program_root=program_root))
    assert result.status == "warn"  # the relation candidate is still pending (its entities are now resolvable)
    assert result.details["count"] == 1


def test_entity_dupes_suspected_skips_without_a_program_root():
    result = check_entity_dupes_suspected(DoctorContext())
    assert result.status == "skip"


def test_entity_dupes_suspected_passes_with_no_draft_proposals(store, program_root):
    build_small_corpus(store)
    result = check_entity_dupes_suspected(DoctorContext(program_root=program_root))
    assert result.status == "pass"


def test_entity_dupes_suspected_warns_on_a_draft_merge_proposal(store, program_root):
    corpus = build_small_corpus(store)
    extracted = extract_api.run_extract_chunk(store, _open_chunk_id(corpus), judge=_fake_judge(corpus), created_by_launch=corpus["launch_id"])
    gm_record_id = extracted["record_ids"]["entities"][0]
    extract_api.accept_candidate(store, gm_record_id, by_launch=corpus["launch_id"])

    def judge_dup(envelope):
        return {"entities": [{"name": "Coordinator", "entity_type": "role"}], "relations": [], "claims": []}

    dup = extract_api.run_extract_chunk(store, _restricted_chunk_id(corpus), judge=judge_dup, created_by_launch=corpus["launch_id"])
    extract_api.accept_candidate(store, dup["record_ids"]["entities"][0], by_launch=corpus["launch_id"])

    result = check_entity_dupes_suspected(DoctorContext(program_root=program_root))
    assert result.status == "warn"
    assert len(result.details["proposals"]) == 1


def test_extract_checks_register_with_the_generic_doctor_sweep():
    from trialerror.util.doctor import clear_registry, discover_and_register_checks, registered_checks

    clear_registry()
    discover_and_register_checks()
    regs = registered_checks()
    assert regs["extract_pending_backlog"][0] == "ingest"
    assert regs["entity_dupes_suspected"][0] == "ingest"
