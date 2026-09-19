"""``trialerror.lexicon.api``'s three text-facing reads (design §5):
``match_terms_in_text``, ``lookup_terms`` and ``covered_region_labels``.

Kept in their own module rather than appended to ``test_lexicon_api``
because they test a different kind of thing. Everything in that module is
about the write lifecycle and what it refuses; everything here is about
one question -- *does a written form reach the right term, and can the
caller find that form again in the text it started with* -- and the answers
turn on normalization, offsets and word boundaries rather than on
statuses and launches.

Three properties are worth naming before the tests, because most of what
follows is one of them looked at from a different angle:

1. **Spans are into the ORIGINAL text.** Normalization folds characters
   away (a soft hyphen, a BOM) and conjures others (``ß`` -> ``ss``), so an
   offset that is correct against the normalized copy is silently wrong
   against the text a reader sees. Every span assertion here is written as
   ``text[span_start:span_end] == <the written form>`` rather than as a
   number, which is the assertion that actually catches drift.
2. **Longest wins, then leftmost, and no two matches overlap.**
3. **A name resolves the way ``find_term(follow_merges=True)`` resolves
   it** -- through aliases, and through a merge to the term that is live
   now.

The lemma vocabulary is the synthetic instrument-handbook one from
``tests/_lexicon_fixtures``, extended with a handful of names in the same
invented register where a test needs a specific overlap (a term that
contains another, a term carrying an apostrophe). Nothing here is a real
name from any real corpus.
"""

from __future__ import annotations

import pytest

from trialerror.lexicon import api, policy
from trialerror.lexicon.errors import InvalidTermInputError
from trialerror.lexicon.normalize import norm_lemma, norm_with_offsets

from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


def _term(store, ids, lemma: str, **overrides) -> dict:
    """One ``current`` instance term for ``lemma``, through the real write
    path (so it gets a ``preferred_sense_id`` the way any accepted sense
    does, rather than a hand-set column)."""
    kwargs = {
        "lemma": lemma,
        "gloss": f"what an instrument handbook records under the name {lemma}",
        "origin_kind": "manual",
        "origin_ref": None,
        "evidence": [f"anchor:{ids['quote_anchor']}"],
        "by_launch": ids["launch"],
        "procedure_version": policy.MANUAL_PROCEDURE_VERSION,
        "granularity": "instance",
        "status": "current",
    }
    kwargs.update(overrides)
    return api.propose(store, **kwargs)


def _lemmas(matches) -> list[str]:
    return [m["lemma"] for m in matches]


# ---------------------------------------------------------------------------
# the offset map itself -- norm_with_offsets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace only"),
        pytest.param("baseline drift", id="plain"),
        pytest.param("  Baseline   Drift  ", id="case and runs and edges"),
        pytest.param("set­tling time", id="soft hyphen"),
        pytest.param("﻿noise floor", id="byte order mark"),
        pytest.param("operator’s margin", id="curly apostrophe"),
        pytest.param("self–heating", id="en dash"),
        pytest.param("baseline drift", id="non-breaking space"),
        pytest.param("straße", id="casefold lengthens"),
        pytest.param("ﬁlter", id="ligature expands"),
        pytest.param("éclair", id="decomposed accent composes"),
        pytest.param("ＡＢ", id="full width"),
        pytest.param("gate\twidth\nhold", id="tabs and newlines"),
    ],
)
def test_norm_with_offsets_returns_exactly_norm_lemmas_string(text):
    """The contract the whole feature rests on. ``match_terms_in_text``
    looks up keys built by ``norm_lemma`` in text folded by
    ``norm_with_offsets``; the day those two disagree by one character the
    matches stop happening and nothing else in the system notices."""
    normalized, spans = norm_with_offsets(text)
    assert normalized == norm_lemma(text)
    assert len(spans) == len(normalized)


def test_norm_with_offsets_spans_point_at_the_original_characters():
    text = "  Set­tling   Time  "
    normalized, spans = norm_with_offsets(text)

    assert normalized == "settling time"
    # every span is a real, non-empty, non-decreasing slice of the original
    assert all(0 <= s < e <= len(text) for s, e in spans)
    assert [s for s, _ in spans] == sorted(s for s, _ in spans)
    # and the characters the folded ones came from are the ones we expect
    start = spans[0][0]
    end = spans[-1][1]
    assert text[start:end] == "Set­tling   Time"


def test_norm_with_offsets_maps_one_expanded_character_back_to_that_character():
    """``ß`` folds to two characters; both must point at the one character
    in the original that produced them. There is no half of a ``ß``."""
    text = "aßb"
    normalized, spans = norm_with_offsets(text)
    assert normalized == "assb"
    assert spans[1] == spans[2] == (1, 2)


# ---------------------------------------------------------------------------
# match_terms_in_text
# ---------------------------------------------------------------------------


def test_match_reports_the_term_and_the_span_of_the_original_text(store, ids):
    created = _term(store, ids, "baseline drift")
    text = "the log shows baseline drift before the run settles"

    matches = api.match_terms_in_text(store, text)

    assert len(matches) == 1
    match = matches[0]
    assert match["term_id"] == created["term_id"]
    assert match["lemma"] == "baseline drift"
    assert match["sense_id"] == created["sense_id"]
    assert text[match["span_start"] : match["span_end"]] == "baseline drift"
    assert set(match) == {"term_id", "lemma", "span_start", "span_end", "sense_id"}


def test_a_longer_term_beats_a_shorter_one_it_overlaps(store, ids):
    """Longest wins, then leftmost -- read literally, which is not the same
    as a leftmost-greedy scan. ``peak deviation`` starts first and would
    win a greedy pass; ``deviation threshold`` is longer, overlaps it, and
    is the answer the design asks for."""
    _term(store, ids, "peak deviation")
    longer = _term(store, ids, "deviation threshold")
    text = "peak deviation threshold reached"

    matches = api.match_terms_in_text(store, text)

    assert _lemmas(matches) == ["deviation threshold"]
    assert matches[0]["term_id"] == longer["term_id"]
    assert text[matches[0]["span_start"] : matches[0]["span_end"]] == "deviation threshold"


def test_a_longer_term_beats_a_shorter_one_contained_in_it(store, ids):
    _term(store, ids, "sample")
    longer = _term(store, ids, "sample interval")

    matches = api.match_terms_in_text(store, "the sample interval was doubled")

    assert _lemmas(matches) == ["sample interval"]
    assert matches[0]["term_id"] == longer["term_id"]


def test_an_alias_matches_and_reports_the_term_it_belongs_to(store, ids):
    created = _term(store, ids, "settling time", aliases=["settling times", ("stl", "abbreviation")])
    text = "two settling times, then stl again"

    matches = api.match_terms_in_text(store, text)

    assert _lemmas(matches) == ["settling time", "settling time"]
    assert {m["term_id"] for m in matches} == {created["term_id"]}
    assert text[matches[0]["span_start"] : matches[0]["span_end"]] == "settling times"
    assert text[matches[1]["span_start"] : matches[1]["span_end"]] == "stl"


def test_a_term_does_not_match_inside_a_longer_word(store, ids):
    _term(store, ids, "gate width")

    assert api.match_terms_in_text(store, "the gate widths were equal") == []
    assert api.match_terms_in_text(store, "a floodgate width reading") == []
    assert _lemmas(api.match_terms_in_text(store, "(gate width)")) == ["gate width"]


@pytest.mark.parametrize(
    "lemma,written",
    [
        pytest.param("settling time", "Settling Time", id="case"),
        pytest.param("settling time", "SETTLING TIME", id="upper case"),
        pytest.param("settling time", "set­tling time", id="soft hyphen inside a word"),
        pytest.param("settling time", "settling time", id="non-breaking space between words"),
        pytest.param("settling time", "settling​ time", id="zero width space before the space"),
        pytest.param("self-heating", "self–heating", id="en dash for a hyphen"),
        pytest.param("operator's margin", "operator’s margin", id="curly apostrophe"),
        pytest.param("baseline drift", "Baseline\n   drift", id="newline and indent between words"),
    ],
)
def test_a_written_variant_matches_and_its_span_covers_the_variant(store, ids, lemma, written):
    """The spans are the point of these cases, not the matches. Each
    written form is a different length from the lemma it matches -- the
    soft hyphen and the zero-width space add a character that folds away,
    the dash and the apostrophe swap one character for another of a
    different byte length, the newline case collapses four characters into
    one -- so a span computed against the normalized copy would land in the
    wrong place in every row below."""
    _term(store, ids, lemma)
    text = f"before {written} after"

    matches = api.match_terms_in_text(store, text)

    assert _lemmas(matches) == [lemma]
    assert text[matches[0]["span_start"] : matches[0]["span_end"]] == written


def test_a_merged_terms_old_lemma_resolves_to_the_canonical_term(store, ids):
    canonical = _term(store, ids, "baseline drift")
    folded = _term(store, ids, "zero offset")
    api.merge_terms(store, canonical["term_id"], folded["term_id"], by_launch=ids["launch"])
    text = "the handbook still calls it zero offset here"

    matches = api.match_terms_in_text(store, text)

    assert len(matches) == 1
    assert matches[0]["term_id"] == canonical["term_id"]
    assert matches[0]["lemma"] == "baseline drift", "the canonical lemma, not the folded one"
    assert text[matches[0]["span_start"] : matches[0]["span_end"]] == "zero offset"
    assert matches[0]["sense_id"] == canonical["sense_id"]


def test_a_retired_terms_lemma_still_matches(store, ids):
    """A retirement says the program stopped using the name, not that an
    older document stopped containing it -- and a link to the term is how a
    reader of that document finds out."""
    created = _term(store, ids, "guard ring")
    api.retire_sense(store, created["sense_id"], by_launch=ids["launch"], reason="no longer used")

    matches = api.match_terms_in_text(store, "an old note about the guard ring")

    assert [m["term_id"] for m in matches] == [created["term_id"]]
    assert matches[0]["sense_id"] is None, "retiring the last sense leaves no preferred one"


def test_sense_id_is_none_for_a_term_with_no_preferred_sense(store, ids):
    """The fixture's own ``term`` row is written around this API and has no
    ``preferred_sense_id``, which is exactly the state a caller must not
    get a guessed answer for."""
    matches = api.match_terms_in_text(store, "one Test Term in a sentence")

    assert [m["term_id"] for m in matches] == [ids["term"]]
    assert matches[0]["sense_id"] is None


def test_matches_come_back_in_text_order_and_never_overlap(store, ids):
    _term(store, ids, "noise floor")
    _term(store, ids, "hold time")
    text = "noise floor first, hold time second, noise floor again"

    matches = api.match_terms_in_text(store, text)

    assert _lemmas(matches) == ["noise floor", "hold time", "noise floor"]
    spans = [(m["span_start"], m["span_end"]) for m in matches]
    assert spans == sorted(spans)
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n ", id="whitespace"),
        pytest.param("nothing here", id="no term"),
    ],
)
def test_match_returns_no_matches_for_text_with_nothing_in_it(store, ids, text):
    _term(store, ids, "baseline drift")
    assert api.match_terms_in_text(store, text) == []


def test_match_on_a_store_with_no_terms_returns_nothing(store):
    assert api.match_terms_in_text(store, "baseline drift") == []


# ---------------------------------------------------------------------------
# lookup_terms
# ---------------------------------------------------------------------------


def test_lookup_terms_matches_a_whole_text_by_lemma_and_by_alias(store, ids):
    created = _term(store, ids, "settling time", aliases=["settling times"])

    rows = api.lookup_terms(store, ["Settling  Time", "settling times"])

    assert [r["index"] for r in rows] == [0, 1]
    assert [r["matched_by"] for r in rows] == ["lemma", "alias"]
    assert {r["term_id"] for r in rows} == {created["term_id"]}
    assert [r["lemma"] for r in rows] == ["settling time", "settling time"]
    assert [r["text"] for r in rows] == ["Settling  Time", "settling times"]
    assert {r["sense_id"] for r in rows} == {created["sense_id"]}


def test_lookup_terms_carries_the_input_index_past_the_texts_that_miss(store, ids):
    _term(store, ids, "noise floor")

    rows = api.lookup_terms(store, ["nothing", "", None, "noise floor", "also nothing"])

    assert [r["index"] for r in rows] == [3]


def test_lookup_terms_is_a_whole_text_match_not_a_substring_one(store, ids):
    """The AIIF pre-pass asks "is this candidate name already a term", and
    a description that happens to contain a term is not that."""
    _term(store, ids, "noise floor")

    assert api.lookup_terms(store, ["a mechanic with a noise floor in it"]) == []
    assert api.lookup_terms(store, ["noise"]) == []


def test_lookup_terms_does_not_return_a_family_granularity_term(store, ids):
    _term(store, ids, "stability behaviours", granularity="family")
    instance = _term(store, ids, "baseline drift")

    rows = api.lookup_terms(store, ["stability behaviours", "baseline drift"])

    assert [r["term_id"] for r in rows] == [instance["term_id"]]
    assert [r["index"] for r in rows] == [1]


def test_lookup_terms_filters_on_the_granularity_of_the_live_term(store, ids):
    """The filter is applied after merges are followed: a name folded into
    a family term is a family name now, whatever the row it was found on
    still says."""
    family = _term(store, ids, "timing behaviours", granularity="family")
    folded = _term(store, ids, "dwell period")
    api.merge_terms(store, family["term_id"], folded["term_id"], by_launch=ids["launch"])

    assert api.lookup_terms(store, ["dwell period"]) == []


def test_lookup_terms_of_a_term_with_no_granularity_returns_nothing(store, ids):
    _term(store, ids, "ramp rate", granularity=None)
    assert api.lookup_terms(store, ["ramp rate"]) == []


def test_lookup_terms_refuses_a_bare_string(store, ids):
    with pytest.raises(InvalidTermInputError):
        api.lookup_terms(store, "baseline drift")


def test_lookup_terms_of_no_texts_is_empty(store, ids):
    _term(store, ids, "baseline drift")
    assert api.lookup_terms(store, []) == []


# ---------------------------------------------------------------------------
# covered_region_labels
# ---------------------------------------------------------------------------


def test_covered_region_labels_returns_labels_and_nothing_else(store, ids):
    created = _term(store, ids, "baseline drift", tags=["f-stability", "f-timing"])

    result = api.covered_region_labels(store, [created["term_id"]])

    assert result["missing"] == []
    assert result["labels"] == [
        {
            "term_id": created["term_id"],
            "granularity": "instance",
            "family_tags": ["f-stability", "f-timing"],
        }
    ]
    row = result["labels"][0]
    assert "gloss" not in row and "senses" not in row and "evidence" not in row


def test_covered_region_labels_carries_the_family_level_and_an_empty_tag_list(store, ids):
    family = _term(store, ids, "stability behaviours", granularity="family", tags=["f-stability"])
    untagged = _term(store, ids, "cold junction")

    result = api.covered_region_labels(store, [family["term_id"], untagged["term_id"]])

    assert [row["granularity"] for row in result["labels"]] == ["family", "instance"]
    assert [row["family_tags"] for row in result["labels"]] == [["f-stability"], []]


def test_covered_region_labels_reports_an_unknown_id_instead_of_raising(store, ids):
    created = _term(store, ids, "baseline drift")

    result = api.covered_region_labels(store, ["TERM-does-not-exist", created["term_id"], "TERM-nor-this"])

    assert [row["term_id"] for row in result["labels"]] == [created["term_id"]]
    assert result["missing"] == ["TERM-does-not-exist", "TERM-nor-this"]


def test_covered_region_labels_answers_a_repeated_id_once(store, ids):
    created = _term(store, ids, "baseline drift")

    result = api.covered_region_labels(store, [created["term_id"], created["term_id"], "", None])

    assert len(result["labels"]) == 1
    assert result["missing"] == []


def test_covered_region_labels_does_not_follow_a_merge(store, ids):
    """The two lookups answer "what does this name mean now" and follow
    merges; this one answers "what are the labels on this row" about an id
    the caller already resolved, so it answers about the row it was asked
    about."""
    canonical = _term(store, ids, "stability behaviours", granularity="family", tags=["f-stability"])
    folded = _term(store, ids, "baseline drift", tags=["f-stability"])
    api.merge_terms(store, canonical["term_id"], folded["term_id"], by_launch=ids["launch"])

    result = api.covered_region_labels(store, [folded["term_id"]])

    assert result["labels"] == [
        {"term_id": folded["term_id"], "granularity": "instance", "family_tags": ["f-stability"]}
    ]


def test_covered_region_labels_tolerates_a_tags_column_that_is_not_a_json_list(store, ids):
    created = _term(store, ids, "baseline drift")
    with store.knowledge:
        store.knowledge.execute(
            "UPDATE term SET tags = ? WHERE term_id = ?", ("not json at all", created["term_id"])
        )

    result = api.covered_region_labels(store, [created["term_id"]])

    assert result["labels"][0]["family_tags"] == []


def test_covered_region_labels_refuses_a_bare_string(store, ids):
    with pytest.raises(InvalidTermInputError):
        api.covered_region_labels(store, "TERM-0001")


def test_covered_region_labels_of_no_ids_is_empty(store, ids):
    assert api.covered_region_labels(store, []) == {"labels": [], "missing": []}
