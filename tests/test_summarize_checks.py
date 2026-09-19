"""build-v2-summary: :mod:`trialerror.summarize.checks` -- the
``summaries_missing``/``summaries_stale`` doctor checks (build brief:
"doctor: summaries_stale (docs newer than their summary)")."""

from __future__ import annotations

from trialerror.stores.writer import update
from trialerror.summarize.api import build_summary_envelope, store_summary
from trialerror.summarize.checks import check_summaries_missing, check_summaries_stale
from trialerror.util.doctor import DoctorContext

from tests._summarize_fixtures import build_small_corpus


def test_skip_when_program_root_not_configured():
    ctx = DoctorContext()
    assert check_summaries_missing(ctx).status == "skip"
    assert check_summaries_stale(ctx).status == "skip"


def test_skip_when_knowledge_db_not_yet_created(tmp_path):
    ctx = DoctorContext(program_root=tmp_path / "never-initialized")
    assert check_summaries_missing(ctx).status == "skip"
    assert check_summaries_stale(ctx).status == "skip"


def test_missing_warns_for_normalized_documents_with_no_summary(store, program_root):
    build_small_corpus(store)
    ctx = DoctorContext(program_root=program_root)
    result = check_summaries_missing(ctx)
    assert result.status == "warn"
    assert result.details["doc_ids"]  # both fixture docs have elements, neither has a summary yet


def test_missing_passes_once_every_document_has_a_current_summary(store, program_root):
    corpus = build_small_corpus(store)
    for doc_key in ("open_doc_id", "restricted_doc_id"):
        envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus[doc_key])
        store_summary(store, envelope=envelope, body="an overview", issued_by_launch=corpus["launch_id"])

    ctx = DoctorContext(program_root=program_root)
    result = check_summaries_missing(ctx)
    assert result.status == "pass"


def test_stale_passes_when_summary_matches_current_document_sha(store, program_root):
    corpus = build_small_corpus(store)
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    store_summary(store, envelope=envelope, body="an overview", issued_by_launch=corpus["launch_id"])

    ctx = DoctorContext(program_root=program_root)
    result = check_summaries_stale(ctx)
    assert result.status == "pass"
    assert result.details["doc_ids"] == []


def test_stale_warns_when_document_content_changed_since_generation(store, program_root):
    corpus = build_small_corpus(store)
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    row = store_summary(store, envelope=envelope, body="an overview", issued_by_launch=corpus["launch_id"])

    update(store, "document", pk_column="doc_id", pk_value=corpus["open_doc_id"], changes={"sha256": "e" * 64})

    ctx = DoctorContext(program_root=program_root)
    result = check_summaries_stale(ctx)
    assert result.status == "warn"
    assert corpus["open_doc_id"] in result.details["doc_ids"]
    assert row["summary_id"] in result.details["summary_ids"]


def test_stale_check_ignores_a_superseded_row_for_the_same_document(store, program_root):
    """A superseded (non-current) summary must never be reported --
    ``summaries_stale`` is about the CURRENT answer only."""
    corpus = build_small_corpus(store)
    envelope1 = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    store_summary(store, envelope=envelope1, body="first", issued_by_launch=corpus["launch_id"])
    envelope2 = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    store_summary(store, envelope=envelope2, body="second (current, matches doc sha)", issued_by_launch=corpus["launch_id"])

    ctx = DoctorContext(program_root=program_root)
    result = check_summaries_stale(ctx)
    assert result.status == "pass"
