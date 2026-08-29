"""build-v2-summary: :mod:`trialerror.summarize.api` -- envelope round trip w/ a
deterministic fake judge, versioned supersession, D-COC-1 embedded-quote
fence enforcement, and staleness-key computation. Built against real stores
via ``tests/_summarize_fixtures.build_small_corpus`` (both an ``open`` and a
``commercial_restricted`` source, real chunker/anchor primitives)."""

from __future__ import annotations

import json

import pytest

from trialerror.stores.writer import get, update
from trialerror.summarize.api import (
    DEFAULT_WORD_CAP,
    MAX_EMBEDDED_QUOTE_WORDS,
    build_summary_envelope,
    compute_subject_sha256,
    find_stale_or_missing_document_summaries,
    get_summary,
    get_summary_by_id,
    list_summaries,
    store_summary,
)
from trialerror.summarize.errors import InvalidSubjectKindError, SubjectNotFoundError, SummarizeError, SummaryFenceViolationError

from tests._summarize_fixtures import OVER_LENGTH_QUOTE, build_small_corpus


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


# ---------------------------------------------------------------------------
# build_summary_envelope -- document subject
# ---------------------------------------------------------------------------


def test_document_envelope_shape_and_content(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert envelope["kind"] == "summary"
    assert envelope["subject_kind"] == "document"
    assert envelope["subject_id"] == corpus["open_doc_id"]
    assert envelope["source_doc_ids"] == [corpus["open_doc_id"]]
    assert envelope["word_cap"] == DEFAULT_WORD_CAP
    assert envelope["fenced"] is False
    assert "dice pools" in envelope["context"].lower()
    assert str(envelope["word_cap"]) in envelope["instruction"]

    doc = get(store, "document", pk_column="doc_id", pk_value=corpus["open_doc_id"])
    assert envelope["subject_sha256"] == doc["sha256"]


def test_document_envelope_over_restricted_source_is_fenced_with_instruction(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["restricted_doc_id"])
    assert envelope["fenced"] is True
    assert "commercial_restricted" in envelope["instruction"]
    assert f"{MAX_EMBEDDED_QUOTE_WORDS} words or fewer" in envelope["instruction"]


def test_document_envelope_custom_word_cap(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"], word_cap=42)
    assert envelope["word_cap"] == 42
    assert "42" in envelope["instruction"]


def test_document_envelope_no_such_document_refused(store, corpus):
    with pytest.raises(SubjectNotFoundError):
        build_summary_envelope(store, subject_kind="document", subject_id="DOC-does-not-exist")


def test_document_envelope_unnormalized_document_refused(store, corpus):
    from trialerror.stores.writer import insert
    from trialerror.util.ids import new_id

    doc_id = new_id("DOC")
    insert(
        store, "document",
        {
            "doc_id": doc_id, "source_id": corpus["open_source_id"], "rel_path": "archive/empty.md",
            "media_type": "md", "normalizer_id": "fixture", "normalizer_version": "1",
            "sha256": "0" * 64, "status": "registered",
        },
    )
    with pytest.raises(SubjectNotFoundError, match="not normalized"):
        build_summary_envelope(store, subject_kind="document", subject_id=doc_id)


def test_invalid_subject_kind_refused(store, corpus):
    with pytest.raises(InvalidSubjectKindError):
        build_summary_envelope(store, subject_kind="bogus", subject_id=corpus["open_doc_id"])


def test_document_subject_with_mismatched_doc_ids_refused(store, corpus):
    with pytest.raises(SummarizeError):
        build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"], doc_ids=["DOC-someone-else"])


# ---------------------------------------------------------------------------
# build_summary_envelope -- collection subject
# ---------------------------------------------------------------------------


def test_collection_envelope_explicit_doc_ids(store, corpus):
    member_ids = [corpus["open_doc_id"], corpus["restricted_doc_id"]]
    envelope = build_summary_envelope(store, subject_kind="collection", subject_id="COLL-test", doc_ids=member_ids)
    assert envelope["subject_kind"] == "collection"
    assert envelope["source_doc_ids"] == member_ids
    assert envelope["fenced"] is True  # one member is commercial_restricted
    assert "raw excerpt, no summary yet" in envelope["context"]


def test_collection_envelope_defaults_to_every_doc_under_a_source_id(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="collection", subject_id=corpus["open_source_id"])
    assert envelope["source_doc_ids"] == [corpus["open_doc_id"]]


def test_collection_envelope_unresolvable_subject_refused(store, corpus):
    with pytest.raises(SubjectNotFoundError):
        build_summary_envelope(store, subject_kind="collection", subject_id="not-a-real-source-or-key")


def test_collection_envelope_aggregates_bottom_up_from_existing_document_summary(store, corpus):
    doc_envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    stored = store_summary(store, envelope=doc_envelope, body="A concise open-source overview.", issued_by_launch=corpus["launch_id"])

    coll_envelope = build_summary_envelope(
        store, subject_kind="collection", subject_id="COLL-bottom-up", doc_ids=[corpus["open_doc_id"]]
    )
    assert "existing L1 summary" in coll_envelope["context"]
    assert "A concise open-source overview." in coll_envelope["context"]
    assert stored["body"] in coll_envelope["context"]


# ---------------------------------------------------------------------------
# store_summary -- round trip, versioning
# ---------------------------------------------------------------------------


def test_store_and_get_round_trip(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    body = "Tabletop games use dice pools; a GM adjudicates disputes."
    row = store_summary(store, envelope=envelope, body=body, issued_by_launch=corpus["launch_id"])

    assert row["summary_id"].startswith("SUM-")
    assert row["body"] == body
    assert row["word_count"] == len(body.split())
    assert row["status"] == "current"
    assert row["supersedes"] is None
    assert json.loads(row["source_doc_ids"]) == [corpus["open_doc_id"]]
    assert row["fenced"] == 0

    fetched = get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert fetched == row

    by_id = get_summary_by_id(store, row["summary_id"])
    assert by_id == row


def test_store_summary_empty_body_refused(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    with pytest.raises(SummarizeError):
        store_summary(store, envelope=envelope, body="   ", issued_by_launch=corpus["launch_id"])


def test_resummarize_supersedes_never_overwrites(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    first = store_summary(store, envelope=envelope, body="first draft overview", issued_by_launch=corpus["launch_id"])

    second_envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    second = store_summary(store, envelope=second_envelope, body="second, better overview", issued_by_launch=corpus["launch_id"])

    assert second["summary_id"] != first["summary_id"]
    assert second["supersedes"] == first["summary_id"]
    assert second["status"] == "current"

    old_row = get_summary_by_id(store, first["summary_id"])
    assert old_row["status"] == "superseded"
    assert old_row["body"] == "first draft overview"  # never overwritten in place

    current = get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert current["summary_id"] == second["summary_id"]
    assert current["body"] == "second, better overview"

    all_rows = list_summaries(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert {r["summary_id"] for r in all_rows} == {first["summary_id"], second["summary_id"]}
    assert {r["status"] for r in all_rows} == {"current", "superseded"}


def test_third_generation_chains_supersedes_correctly(store, corpus):
    ids = []
    for body in ("v1", "v2", "v3"):
        envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
        row = store_summary(store, envelope=envelope, body=body, issued_by_launch=corpus["launch_id"])
        ids.append(row["summary_id"])

    assert get_summary_by_id(store, ids[0])["status"] == "superseded"
    assert get_summary_by_id(store, ids[1])["status"] == "superseded"
    assert get_summary_by_id(store, ids[1])["supersedes"] == ids[0]
    assert get_summary_by_id(store, ids[2])["supersedes"] == ids[1]
    current = get_summary(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert current["summary_id"] == ids[2]


# ---------------------------------------------------------------------------
# D-COC-1 embedded-quote fence
# ---------------------------------------------------------------------------


def test_fenced_subject_with_short_quote_is_accepted(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["restricted_doc_id"])
    body = 'The rulebook covers combat. It says: "roll initiative, then act in order" among other things.'
    row = store_summary(store, envelope=envelope, body=body, issued_by_launch=corpus["launch_id"])
    assert row["fenced"] == 1


def test_fenced_subject_with_over_length_quote_is_refused(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["restricted_doc_id"])
    body = f"The rulebook's combat section states {OVER_LENGTH_QUOTE} and continues from there."
    before = list_summaries(store, subject_kind="document", subject_id=corpus["restricted_doc_id"])
    with pytest.raises(SummaryFenceViolationError, match=str(MAX_EMBEDDED_QUOTE_WORDS)):
        store_summary(store, envelope=envelope, body=body, issued_by_launch=corpus["launch_id"])
    after = list_summaries(store, subject_kind="document", subject_id=corpus["restricted_doc_id"])
    assert before == after  # refused BEFORE any write -- nothing landed


def test_over_length_quote_is_fine_when_no_source_is_restricted(store, corpus):
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    body = f"An open paper says {OVER_LENGTH_QUOTE} and that's fine since nothing here is restricted."
    row = store_summary(store, envelope=envelope, body=body, issued_by_launch=corpus["launch_id"])
    assert row["fenced"] == 0
    assert row["body"] == body  # served/stored in full, unmodified


def test_fenced_body_itself_is_never_truncated_only_the_embedded_quote_is_capped(store, corpus):
    """The build brief, verbatim: "an L1 overview of a restricted source is
    EXTRACTION not verbatim, so it serves" -- a long, quote-free overview
    of a restricted source is accepted at full length."""
    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["restricted_doc_id"])
    long_body = "This is a long paraphrased overview. " * 40  # ~280 words, well over the 20-word fence, zero quotes
    row = store_summary(store, envelope=envelope, body=long_body, issued_by_launch=corpus["launch_id"])
    assert row["body"] == long_body
    assert row["word_count"] > MAX_EMBEDDED_QUOTE_WORDS


# ---------------------------------------------------------------------------
# staleness key
# ---------------------------------------------------------------------------


def test_compute_subject_sha256_document_matches_document_sha256(store, corpus):
    doc = get(store, "document", pk_column="doc_id", pk_value=corpus["open_doc_id"])
    assert compute_subject_sha256(store, "document", [corpus["open_doc_id"]]) == doc["sha256"]


def test_compute_subject_sha256_collection_is_order_independent(store, corpus):
    ids_ab = [corpus["open_doc_id"], corpus["restricted_doc_id"]]
    ids_ba = [corpus["restricted_doc_id"], corpus["open_doc_id"]]
    assert compute_subject_sha256(store, "collection", ids_ab) == compute_subject_sha256(store, "collection", ids_ba)


def test_find_stale_or_missing_document_summaries(store, corpus):
    discovery = find_stale_or_missing_document_summaries(store)
    assert corpus["open_doc_id"] in discovery["missing"]
    assert corpus["restricted_doc_id"] in discovery["missing"]

    envelope = build_summary_envelope(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    store_summary(store, envelope=envelope, body="fresh summary", issued_by_launch=corpus["launch_id"])

    discovery = find_stale_or_missing_document_summaries(store)
    assert corpus["open_doc_id"] not in discovery["missing"]
    assert corpus["open_doc_id"] not in discovery["stale"]

    # simulate a re-normalization: the document's sha256 changes underneath
    # the already-generated summary.
    update(store, "document", pk_column="doc_id", pk_value=corpus["open_doc_id"], changes={"sha256": "f" * 64})
    discovery = find_stale_or_missing_document_summaries(store)
    assert corpus["open_doc_id"] in discovery["stale"]
    assert corpus["open_doc_id"] not in discovery["missing"]


# ---------------------------------------------------------------------------
# list_summaries
# ---------------------------------------------------------------------------


def test_list_summaries_filters(store, corpus):
    for doc_id in (corpus["open_doc_id"], corpus["restricted_doc_id"]):
        envelope = build_summary_envelope(store, subject_kind="document", subject_id=doc_id)
        store_summary(store, envelope=envelope, body=f"summary for {doc_id}", issued_by_launch=corpus["launch_id"])

    all_current = list_summaries(store, status="current")
    assert len(all_current) == 2

    scoped = list_summaries(store, subject_kind="document", subject_id=corpus["open_doc_id"])
    assert len(scoped) == 1
    assert scoped[0]["subject_id"] == corpus["open_doc_id"]
