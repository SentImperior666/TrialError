"""Tests for ``trialerror.ingest.quality``: the four measures, their
denominators, and the read-only composition over a document/corpus.

The denominator tests are the point of this module. Two of the four
measures are defined by reuse -- the chunker's own ``estimate_tokens`` and
the DjVu normalizer's own unusable-character regex -- and a copy of either
would be a second answer that drifts silently.
"""

from __future__ import annotations

import pytest

from trialerror.ingest import quality
from trialerror.ingest.chunker import estimate_tokens
from trialerror.ingest.errors import DocumentNotFoundError, QualityRefusalConfigError
from trialerror.ingest.normalize_djvu import _UNUSABLE_CHARS_RE
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from tests._ingest_fixtures import bootstrap_launch
from tests._quality_fixtures import (
    CLEAN_TEXT,
    GLUED_TEXT,
    NO_TERMINATOR_TEXT,
    UNUSABLE_TEXT,
    seed_document,
)


# ---------------------------------------------------------------------------
# glued_token_rate -- denominator: the chunker's estimate_tokens
# ---------------------------------------------------------------------------
def test_glued_token_rate_denominator_is_the_chunkers_token_count():
    """Not "a whitespace split" -- THE one the chunker cuts this same text
    with. Every token glued means a rate of exactly 1.0, which can only
    hold if numerator and denominator count the same units."""
    glued = " ".join(["x" * 40] * 7)
    assert estimate_tokens(glued) == 7
    assert quality.glued_token_rate(glued) == 1.0

    for text in (CLEAN_TEXT, GLUED_TEXT, NO_TERMINATOR_TEXT, "", "   ", "one"):
        total = estimate_tokens(text)
        rate = quality.glued_token_rate(text)
        expected_numerator = sum(1 for t in text.split() if len(t) > quality.GLUED_TOKEN_MIN_CHARS)
        assert rate == pytest.approx(expected_numerator / total if total else 0.0)


def test_glued_token_rate_boundary_is_strictly_longer_than_the_limit():
    at_limit = "a" * quality.GLUED_TOKEN_MIN_CHARS
    over = "a" * (quality.GLUED_TOKEN_MIN_CHARS + 1)
    assert quality.glued_token_rate(at_limit) == 0.0
    assert quality.glued_token_rate(over) == 1.0


def test_glued_token_rate_is_zero_on_empty_text():
    assert quality.glued_token_rate("") == 0.0
    assert quality.glued_token_rate("\n\n  \t") == 0.0


def test_glued_token_rate_flags_the_glued_fixture_and_spares_the_clean_one():
    assert quality.glued_token_rate(GLUED_TEXT) > 0.5
    assert quality.glued_token_rate(CLEAN_TEXT) == 0.0


# ---------------------------------------------------------------------------
# unusable_char_count -- the imported regex, never a second one
# ---------------------------------------------------------------------------
def test_unusable_char_count_uses_the_normalizers_own_regex():
    text = UNUSABLE_TEXT
    assert quality.unusable_char_count(text) == len(_UNUSABLE_CHARS_RE.findall(text))
    assert quality.unusable_char_count(text) > 0


def test_unusable_char_count_spares_ordinary_whitespace_and_text():
    assert quality.unusable_char_count("Ordinary prose.\n\tWith a tab.\r\n") == 0
    assert quality.unusable_char_count("") == 0


def test_unusable_char_count_counts_replacement_and_control_bytes():
    assert quality.unusable_char_count("a�b") == 1
    assert quality.unusable_char_count("a\x00\x01\x7fb") == 3


# ---------------------------------------------------------------------------
# terminator_density -- per 1,000 characters
# ---------------------------------------------------------------------------
def test_terminator_density_is_per_thousand_characters():
    text = "a" * 999 + "."
    assert quality.terminator_density(text) == pytest.approx(1.0)
    assert quality.terminator_density("a" * 499 + ".") == pytest.approx(2.0)


def test_terminator_density_counts_the_closing_forms_too():
    assert quality.terminator_density("x" * 998 + "。！") == pytest.approx(2.0)


def test_terminator_density_is_zero_on_empty_text_and_on_fragments():
    assert quality.terminator_density("") == 0.0
    assert quality.terminator_density(NO_TERMINATOR_TEXT) == 0.0
    assert quality.terminator_density(CLEAN_TEXT) > 5.0


# ---------------------------------------------------------------------------
# chars_per_page_cv
# ---------------------------------------------------------------------------
def test_chars_per_page_cv_none_without_at_least_two_pages():
    assert quality.chars_per_page_cv([]) is None
    assert quality.chars_per_page_cv(["only one page of text"]) is None


def test_chars_per_page_cv_is_zero_on_even_pages_and_rises_with_unevenness():
    even = quality.chars_per_page_cv(["a" * 100, "b" * 100, "c" * 100])
    uneven = quality.chars_per_page_cv(["a" * 5, "b" * 100, "c" * 900])
    assert even == pytest.approx(0.0)
    assert uneven > 1.0


def test_chars_per_page_cv_accepts_counts_as_well_as_texts():
    assert quality.chars_per_page_cv([100, 100, 100]) == pytest.approx(0.0)
    assert quality.chars_per_page_cv([10, 20]) == pytest.approx(
        quality.chars_per_page_cv(["x" * 10, "y" * 20])
    )


def test_chars_per_page_cv_none_when_every_page_is_empty():
    assert quality.chars_per_page_cv(["", "", ""]) is None


# ---------------------------------------------------------------------------
# measure_elements
# ---------------------------------------------------------------------------
def test_measure_elements_reports_all_four_plus_measurability():
    elements = [
        {"seq": 0, "text": CLEAN_TEXT, "page_number": 1},
        {"seq": 1, "text": CLEAN_TEXT, "page_number": 2},
    ]
    row = quality.measure_elements(elements, page_count=2)
    for key in quality.MEASURE_KEYS:
        assert key in row
    assert row["measurable"] is True
    assert row["elements"] == 2
    assert row["pages_with_text"] == 2
    assert row["chars_per_page_cv"] == pytest.approx(0.0)


def test_measure_elements_cv_is_none_without_a_page_count():
    elements = [
        {"seq": 0, "text": "a" * 10, "page_number": 1},
        {"seq": 1, "text": "b" * 900, "page_number": 2},
    ]
    assert quality.measure_elements(elements, page_count=None)["chars_per_page_cv"] is None
    assert quality.measure_elements(elements, page_count=2)["chars_per_page_cv"] is not None


def test_the_cv_denominator_is_the_declared_page_count_not_the_pages_with_text():
    """Fix pass V-4: a 900-page scan with text on two pages used to measure
    a perfectly even 0.0, because the CV was taken over the pages that had
    element rows. It now measures what it is -- and, crucially, measures the
    SAME as the identical document whose extractor emitted empty element
    rows for the blank pages."""
    sparse = [
        {"seq": 0, "text": CLEAN_TEXT, "page_number": 1},
        {"seq": 1, "text": CLEAN_TEXT, "page_number": 2},
    ]
    padded = sparse + [{"seq": 2 + i, "text": "", "page_number": 3 + i} for i in range(898)]

    row = quality.measure_elements(sparse, page_count=900)
    assert row["pages_with_text"] == 2
    assert row["chars_per_page_cv"] > 1.5
    assert quality.suspect_reasons(row)
    assert row["chars_per_page_cv"] == pytest.approx(
        quality.measure_elements(padded, page_count=900)["chars_per_page_cv"]
    )


def test_a_fully_extracted_document_is_unaffected_by_the_padding():
    elements = [{"seq": i, "text": CLEAN_TEXT, "page_number": i + 1} for i in range(4)]
    assert quality.measure_elements(elements, page_count=4)["chars_per_page_cv"] == pytest.approx(0.0)


def test_a_single_page_document_still_has_no_page_variation_to_report():
    elements = [{"seq": 0, "text": CLEAN_TEXT, "page_number": 1}]
    assert quality.measure_elements(elements, page_count=1)["chars_per_page_cv"] is None


def test_an_unreadable_page_count_reads_as_no_page_structure():
    elements = [
        {"seq": 0, "text": CLEAN_TEXT, "page_number": 1},
        {"seq": 1, "text": CLEAN_TEXT, "page_number": 2},
    ]
    assert quality.measure_elements(elements, page_count="many")["chars_per_page_cv"] is None


def test_measure_elements_on_no_elements_is_unmeasurable_and_never_suspect():
    row = quality.measure_elements([])
    assert row["measurable"] is False
    assert row["terminator_density"] == 0.0
    # the whole point: zero density on NO text must not read as suspect
    assert quality.suspect_reasons(row) == []
    assert quality.severity(row) == 0.0


def test_measure_elements_orders_by_seq_before_joining():
    a = quality.measure_elements([{"seq": 1, "text": "second."}, {"seq": 0, "text": "first."}])
    b = quality.measure_elements([{"seq": 0, "text": "first."}, {"seq": 1, "text": "second."}])
    assert a == b


# ---------------------------------------------------------------------------
# measure_document / measure_corpus (read-only, over a store)
# ---------------------------------------------------------------------------
def test_measure_document_over_a_store(store):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, GLUED_TEXT, page_count=2)
    row = quality.measure_document(store, doc_id)
    assert row["doc_id"] == doc_id
    assert row["measurable"] is True
    assert row["glued_token_rate"] > 0.5


def test_measure_document_accepts_a_bare_connection(store):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, CLEAN_TEXT)
    from_store = quality.measure_document(store, doc_id)
    from_conn = quality.measure_document(store.knowledge, doc_id)
    assert from_store == from_conn


def test_measure_document_refuses_an_unknown_doc_id(store):
    with pytest.raises(DocumentNotFoundError):
        quality.measure_document(store, "DOC-nope")


def test_measure_corpus_one_row_per_document(store):
    launch_id = bootstrap_launch(store)
    ids = {
        seed_document(store, launch_id, CLEAN_TEXT),
        seed_document(store, launch_id, GLUED_TEXT),
        seed_document(store, launch_id, UNUSABLE_TEXT),
    }
    rows = quality.measure_corpus(store)
    assert {r["doc_id"] for r in rows} == ids


def test_measure_corpus_sample_is_seeded_and_bounded(store):
    launch_id = bootstrap_launch(store)
    for _ in range(12):
        seed_document(store, launch_id, CLEAN_TEXT)
    first = [r["doc_id"] for r in quality.measure_corpus(store, sample=4, seed=7)]
    again = [r["doc_id"] for r in quality.measure_corpus(store, sample=4, seed=7)]
    other = [r["doc_id"] for r in quality.measure_corpus(store, sample=4, seed=8)]
    assert len(first) == 4
    assert first == again          # seeded: an unchanged corpus reports the same rows
    assert first == sorted(first)  # corpus order, not draw order
    assert other != first or len(set(first) & set(other)) < 4


def test_measure_corpus_sample_larger_than_the_corpus_measures_everything(store):
    launch_id = bootstrap_launch(store)
    for _ in range(3):
        seed_document(store, launch_id, CLEAN_TEXT)
    assert len(quality.measure_corpus(store, sample=50)) == 3


def test_measure_corpus_excludes_retracted_documents(store):
    launch_id = bootstrap_launch(store)
    kept = seed_document(store, launch_id, CLEAN_TEXT)
    gone = seed_document(store, launch_id, CLEAN_TEXT)
    insert(
        store,
        "record",
        {
            "record_id": new_id("REC"),
            "register_key": "ingest.retraction",
            "artifact_id": None,
            "seq": 1,
            "payload": '{"doc_id": "%s"}' % gone,
            "anchors": None,
            "created_ts": "2026-09-12T00:00:00Z",
        },
    )
    ids = [r["doc_id"] for r in quality.measure_corpus(store)]
    assert ids == [kept]
    assert gone in quality.corpus_doc_ids(store, include_retracted=True)


# ---------------------------------------------------------------------------
# thresholds, suspicion, refusal vocabulary
# ---------------------------------------------------------------------------
def test_thresholds_from_config_defaults_and_overrides():
    assert quality.thresholds_from_config(None)["glued_token_rate_max"] == (
        quality.DEFAULT_THRESHOLDS["glued_token_rate_max"]
    )
    cfg = {"ingest": {"quality": {"glued_token_rate_max": 0.5, "worst_n": 3, "sample": 9, "seed": 4}}}
    t = quality.thresholds_from_config(cfg)
    assert (t["glued_token_rate_max"], t["worst_n"], t["sample"], t["seed"]) == (0.5, 3, 9, 4)


def test_thresholds_from_config_survives_a_mistyped_value():
    cfg = {"ingest": {"quality": {"glued_token_rate_max": "not a number", "worst_n": True}}}
    t = quality.thresholds_from_config(cfg)
    assert t["glued_token_rate_max"] == quality.DEFAULT_THRESHOLDS["glued_token_rate_max"]
    assert t["worst_n"] == quality.DEFAULT_THRESHOLDS["worst_n"]


def test_thresholds_from_config_survives_a_non_table_quality_key():
    assert quality.thresholds_from_config({"ingest": {"quality": "yes please"}}) == (
        quality.thresholds_from_config(None)
    )


def test_suspect_reasons_names_the_measure_and_the_threshold():
    row = quality.measure_elements([{"seq": 0, "text": GLUED_TEXT}])
    reasons = quality.suspect_reasons(row)
    assert quality.is_suspect(row)
    assert any("glued_token_rate" in r and "glued_token_rate_max" in r for r in reasons)


def test_a_clean_document_is_not_suspect():
    row = quality.measure_elements([{"seq": 0, "text": CLEAN_TEXT * 5}])
    assert quality.suspect_reasons(row) == []


def test_refusal_is_absent_by_default_and_empty_tables_stay_absent():
    assert quality.refusal_thresholds_from_config(None) is None
    assert quality.refusal_thresholds_from_config({"ingest": {"quality": {}}}) is None
    assert quality.refusal_thresholds_from_config({"ingest": {"quality": {"refuse_below": {}}}}) is None
    assert quality.refusal_thresholds_from_config(
        {"ingest": {"quality": {"refuse_below": {"nonsense_key": 1}}}}
    ) is None


@pytest.mark.parametrize(
    "value",
    ["eight", "0.35x", "", True, False, None, [0.35], {"n": 1}],
)
def test_an_unreadable_refusal_bound_raises_instead_of_borrowing_the_warn_default(value):
    """Fix pass V-2: the fallback used to be DEFAULT_THRESHOLDS, so a
    mistyped refusal bound installed the conservative WARN number as a
    REFUSAL bound and the normalize stage began failing documents against a
    threshold nobody wrote."""
    cfg = {"ingest": {"quality": {"refuse_below": {"terminator_density_min": value}}}}
    with pytest.raises(QualityRefusalConfigError) as excinfo:
        quality.refusal_thresholds_from_config(cfg)
    assert "terminator_density_min" in str(excinfo.value)


def test_a_refuse_below_that_is_not_a_table_raises_rather_than_reading_as_unconfigured():
    with pytest.raises(QualityRefusalConfigError):
        quality.refusal_thresholds_from_config({"ingest": {"quality": {"refuse_below": 0.35}}})


def test_a_readable_refusal_bound_keeps_its_own_type():
    limits = quality.refusal_thresholds_from_config(
        {"ingest": {"quality": {"refuse_below": {"glued_token_rate_max": "0.35", "unusable_chars_max": "5000"}}}}
    )
    assert limits == {"glued_token_rate_max": 0.35, "unusable_chars_max": 5000}
    assert isinstance(limits["unusable_chars_max"], int)


def test_the_warn_side_still_forgives_a_mistyped_threshold():
    """A mistyped WARN bound cannot stop anything, so it keeps falling back
    to the default -- the asymmetry is the point."""
    out = quality.thresholds_from_config({"ingest": {"quality": {"terminator_density_min": "eight"}}})
    assert out["terminator_density_min"] == quality.DEFAULT_THRESHOLDS["terminator_density_min"]


def test_refusal_thresholds_keep_only_the_configured_measures():
    cfg = {"ingest": {"quality": {"refuse_below": {"glued_token_rate_max": 0.3}}}}
    limits = quality.refusal_thresholds_from_config(cfg)
    assert limits == {"glued_token_rate_max": 0.3}

    glued = quality.measure_elements([{"seq": 0, "text": GLUED_TEXT}])
    fragments = quality.measure_elements([{"seq": 0, "text": NO_TERMINATOR_TEXT}])
    assert quality.refusal_reasons(glued, limits)
    # the terminator measure is not part of THIS program's refusal posture
    assert quality.refusal_reasons(fragments, limits) == []


def test_nothing_is_refused_without_configuration():
    glued = quality.measure_elements([{"seq": 0, "text": GLUED_TEXT}])
    assert quality.refusal_reasons(glued, None) == []
    assert quality.refusal_reasons(glued, {}) == []


def test_an_unmeasurable_document_is_never_refused():
    row = quality.measure_elements([])
    assert quality.refusal_reasons(row, {"terminator_density_min": 5.0}) == []


def test_worst_first_orders_by_severity_and_truncates():
    rows = [
        {**quality.measure_elements([{"seq": 0, "text": CLEAN_TEXT * 3}]), "doc_id": "DOC-clean"},
        {**quality.measure_elements([{"seq": 0, "text": GLUED_TEXT}]), "doc_id": "DOC-glued"},
        {**quality.measure_elements([{"seq": 0, "text": UNUSABLE_TEXT}]), "doc_id": "DOC-unusable"},
    ]
    ordered = quality.worst_first(rows)
    assert ordered[0]["doc_id"] != "DOC-clean"
    assert ordered[-1]["doc_id"] == "DOC-clean"
    assert ordered[-1]["suspect"] is False
    assert len(quality.worst_first(rows, limit=2)) == 2
    assert quality.worst_first(rows, limit=0) == []


def test_severity_is_zero_for_a_clean_row_and_positive_for_a_bad_one():
    clean = quality.measure_elements([{"seq": 0, "text": CLEAN_TEXT * 3}])
    bad = quality.measure_elements([{"seq": 0, "text": GLUED_TEXT}])
    assert quality.severity(clean) == 0.0
    assert quality.severity(bad) > 0.0


# ---------------------------------------------------------------------------
# FB-1b item 4: the size floor. Three of the four measures are rates over the
# document's own text, so a note-sized document trips them by arithmetic. The
# first day this ran on a live corpus, the WARN was made of 8-to-31-token
# notes.
# ---------------------------------------------------------------------------
TINY_GLUED = " ".join(["Theapparatuswasassembledfrompartsalreadyonthebench"] * 3)


def test_the_default_floor_is_two_hundred_tokens():
    assert quality.DEFAULT_MIN_TOKENS == 200
    assert quality.DEFAULT_THRESHOLDS["min_tokens"] == 200


def test_the_pathological_fixtures_are_above_the_floor():
    """Otherwise every suspicion test in this suite would be testing the
    floor instead of the measure it is named for."""
    for text in (CLEAN_TEXT, GLUED_TEXT, UNUSABLE_TEXT, NO_TERMINATOR_TEXT):
        row = quality.measure_elements([{"seq": 0, "text": text}])
        assert row["tokens"] >= quality.DEFAULT_MIN_TOKENS
        assert quality.below_min_tokens(row) is False


def test_a_tiny_note_is_measured_reported_and_never_suspect():
    row = quality.measure_elements([{"seq": 0, "text": TINY_GLUED}])
    # measured: the numbers are all there, and they look terrible
    assert row["measurable"] is True
    assert row["glued_token_rate"] == 1.0
    assert row["terminator_density"] == 0.0
    # reported: under its own name
    assert quality.below_min_tokens(row) is True
    # never suspect
    assert quality.suspect_reasons(row) == []
    assert quality.is_suspect(row) is False
    assert quality.severity(row) == 0.0


def test_the_same_text_at_document_length_is_suspect():
    """The floor must be about SIZE, not about the pathology: the identical
    shape above the floor still fails."""
    long_row = quality.measure_elements([{"seq": 0, "text": " ".join([TINY_GLUED] * 70)}])
    assert long_row["glued_token_rate"] == 1.0
    assert quality.below_min_tokens(long_row) is False
    assert quality.is_suspect(long_row) is True


def test_the_floor_never_hides_a_large_document():
    row = quality.measure_elements([{"seq": 0, "text": GLUED_TEXT}])
    assert row["tokens"] > quality.DEFAULT_MIN_TOKENS
    assert quality.below_min_tokens(row) is False
    assert quality.is_suspect(row) is True


def test_an_unmeasurable_document_is_not_reported_below_the_floor():
    """"Not normalized yet" and "a genuinely tiny note" are different
    statements and must not share a flag."""
    row = quality.measure_elements([])
    assert row["measurable"] is False
    assert quality.below_min_tokens(row) is False


def test_min_tokens_comes_from_the_programs_config():
    row = quality.measure_elements([{"seq": 0, "text": TINY_GLUED}])
    lowered = quality.thresholds_from_config({"ingest": {"quality": {"min_tokens": 3}}})
    assert lowered["min_tokens"] == 3
    assert quality.below_min_tokens(row, lowered) is False
    assert quality.is_suspect(row, lowered) is True


def test_a_floor_of_zero_turns_the_floor_off():
    row = quality.measure_elements([{"seq": 0, "text": TINY_GLUED}])
    off = quality.thresholds_from_config({"ingest": {"quality": {"min_tokens": 0}}})
    assert off["min_tokens"] == 0
    assert quality.below_min_tokens(row, off) is False
    assert quality.is_suspect(row, off) is True


def test_an_unreadable_or_negative_floor_falls_back_the_way_the_others_do():
    assert quality.thresholds_from_config({"ingest": {"quality": {"min_tokens": "many"}}})["min_tokens"] == (
        quality.DEFAULT_MIN_TOKENS
    )
    assert quality.thresholds_from_config({"ingest": {"quality": {"min_tokens": -5}}})["min_tokens"] == 0
    assert quality.thresholds_from_config({"ingest": {"quality": {"min_tokens": 40.9}}})["min_tokens"] == 40


def test_the_floor_does_not_touch_refusal():
    """`refuse_below` is a posture the operator states; this module never
    quietly widens or narrows it."""
    row = quality.measure_elements([{"seq": 0, "text": TINY_GLUED}])
    assert quality.below_min_tokens(row) is True
    assert quality.refusal_reasons(row, {"glued_token_rate_max": 0.2}) != []
    assert quality.refusal_thresholds_from_config(
        {"ingest": {"quality": {"min_tokens": 5000, "refuse_below": {"glued_token_rate_max": 0.2}}}}
    ) == {"glued_token_rate_max": 0.2}


def test_worst_first_carries_the_flag_and_never_ranks_a_tiny_note_first():
    rows = [
        {**quality.measure_elements([{"seq": 0, "text": TINY_GLUED}]), "doc_id": "DOC-note"},
        {**quality.measure_elements([{"seq": 0, "text": GLUED_TEXT}]), "doc_id": "DOC-glued"},
        {**quality.measure_elements([{"seq": 0, "text": CLEAN_TEXT}]), "doc_id": "DOC-clean"},
    ]
    ordered = quality.worst_first(rows)
    assert ordered[0]["doc_id"] == "DOC-glued"
    by_id = {r["doc_id"]: r for r in ordered}
    assert by_id["DOC-note"]["below_min_tokens"] is True
    assert by_id["DOC-note"]["suspect"] is False
    assert by_id["DOC-glued"]["below_min_tokens"] is False
    assert by_id["DOC-clean"]["below_min_tokens"] is False


def test_a_partial_thresholds_mapping_without_min_tokens_applies_no_floor():
    """V-2: a caller naming only the bounds it wants gets only those bounds,
    as ``_violations`` already reads them; the floor needs its own key."""
    from trialerror.ingest import quality as q
    row = {"measurable": True, "tokens": 50, "glued_token_rate": 0.9}
    assert q.below_min_tokens(row, {"glued_token_rate_max": 0.1}) is False
    assert q.below_min_tokens(row, None) is True
    assert q.below_min_tokens(row, {"min_tokens": 200}) is True
