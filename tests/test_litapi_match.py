"""Lane SI item A4: citation-vs-record matching. Pure -- no transport, no store."""

from __future__ import annotations

from trialerror.litapi.citeparse import parse_citation
from trialerror.litapi.match import best_title_match, match_citation, title_similarity
from trialerror.litapi.models import WorkRecord

_CITED_WRONG_DOI = "C. Writer, D. Other, E. Third, J. Studies 29(1), 1986, doi:10.9999/x1"


def test_identifier_resolving_to_other_work_is_mismatch():
    """FAILS BEFORE lane SI: the module did not exist. The DOI resolved to a
    different work by a different author from a different year: surname and year
    both fail -> ``mismatch``. The brief's own pair, whose record year 1985 sits
    inside the year tolerance, is the next test's (amendment B0 moved it from
    ``probable`` to ``mismatch``)."""
    cited = parse_citation(_CITED_WRONG_DOI)
    record = WorkRecord(title="An Unrelated Evaluation of Boards", doi="10.9999/x1", authors=["F. Someone"], year=1979)

    result = match_citation(cited, record)

    assert result.resolution == "mismatch"
    assert result.reasons == {"title_similarity": None, "surname_hit": False, "year_delta": -7, "via": "doi"}
    assert result.thresholds == {"title_floor": 0.90, "year_tolerance": 1}
    assert result.notes == ["surname_mismatch"]


def test_brief_fixture_surname_mismatch_is_decisive_within_year_tolerance():
    """EXPECTATION CHANGED by lane SI amendment B0 (this test was
    ``test_brief_fixture_year_within_tolerance_reads_probable_under_the_fixed_rule``
    and pinned ``probable``). The brief's own fixture pair: cited 1986 with
    surname Writer, record 1985 by F. Someone, no quoted title. Under Part A's
    two-failures rule the year was inside the tolerance, only the surname failed,
    and one failure read ``probable`` -- which is the very wrong-identifier case
    the investigator exists to catch (a DOI resolving to another paper by other
    authors in an adjacent year). B0 makes a first-author surname mismatch
    decisive on an identifier route, so the pair is ``mismatch`` -- at any year
    delta, including none at all."""
    cited = parse_citation(_CITED_WRONG_DOI)
    record = WorkRecord(title="An Unrelated Evaluation of Boards", doi="10.9999/x1", authors=["F. Someone"], year=1985)

    result = match_citation(cited, record)

    assert result.resolution == "mismatch"
    assert result.reasons["surname_hit"] is False
    assert result.reasons["year_delta"] == -1
    assert result.notes == ["surname_mismatch"]
    # with a zero year tolerance the same pair is a mismatch too
    assert match_citation(cited, record, year_tolerance=0).resolution == "mismatch"
    # and the same year does not rescue it
    same_year = WorkRecord(title="An Unrelated Evaluation of Boards", doi="10.9999/x1", authors=["F. Someone"], year=1986)
    assert match_citation(cited, same_year).resolution == "mismatch"


def test_surname_mismatch_with_matching_quoted_title_is_at_most_probable():
    """Amendment B0's one way out: the citation quotes a title that reaches the
    floor against the record's. Then the surname is not decisive -- the label is
    at most ``probable``, with the ``surname_mismatch`` note -- and the
    two-failures rule still applies on top (a failed year as well is two failed
    checks, ``mismatch``)."""
    cited = parse_citation('C. Writer, "A Study of Widgets", 1986, doi:10.9999/x1')
    same_title = WorkRecord(title="A Study of Widgets", doi="10.9999/x1", authors=["F. Someone"], year=1985)
    title_but_far_year = WorkRecord(title="A Study of Widgets", doi="10.9999/x1", authors=["F. Someone"], year=1979)

    result = match_citation(cited, same_title)
    assert result.resolution == "probable"
    assert result.notes == ["surname_mismatch"]
    assert result.reasons["title_similarity"] == 1.0

    assert match_citation(cited, title_but_far_year).resolution == "mismatch"
    # a title search never says mismatch: a surname miss there is probable or none
    assert match_citation(cited, same_title, via="title").resolution == "probable"
    assert match_citation(cited, title_but_far_year, via="title").resolution == "none"


def test_missing_surname_is_never_decisive():
    """B0 fires on a surname that FAILED, not on one nobody could read: a
    citation with no parseable first author and a record in the cited year stays
    ``probable`` on an identifier route, with no note."""
    no_surname = parse_citation("1986, doi:10.9999/x1")
    assert no_surname.surnames == []
    record = WorkRecord(title="Anything", doi="10.9999/x1", authors=["F. Someone"], year=1986)

    result = match_citation(no_surname, record)
    assert result.resolution == "probable"
    assert result.notes == []


def test_identifier_with_quoted_title_mismatch_counts_title_failure():
    cited = parse_citation('C. Writer, "A Study of Widgets", 1986, doi:10.9999/x1')
    record = WorkRecord(title="An Unrelated Evaluation of Boards", doi="10.9999/x1", authors=["F. Someone"], year=1986)

    result = match_citation(cited, record)

    assert result.resolution == "mismatch"  # surname and title fail
    assert result.reasons["title_similarity"] < 0.9


def test_identifier_exact_when_surname_and_year_hold():
    cited = parse_citation("C. Writer, J. Studies 29(1), 1986, doi:10.9999/x1")
    record = WorkRecord(title="A Study of Widgets", doi="10.9999/X1", authors=["Cat Writer", "Dan Other"], year=1986)

    assert match_citation(cited, record).resolution == "exact"


def test_subtitle_drift_still_exact():
    cited = parse_citation('C. Writer, "A Study of Widgets", 1986, doi:10.9999/x1')
    record = WorkRecord(
        title="A Study of Widgets: Theory and Practice", doi="10.9999/x1", authors=["Cat Writer"], year=1986
    )

    result = match_citation(cited, record)

    assert result.resolution == "exact"
    assert result.reasons["title_similarity"] == 1.0
    assert title_similarity("A Study of Widgets", "A Study of Widgets: Theory and Practice") == 1.0


def test_title_search_needs_surname_and_year_for_exact():
    cited = parse_citation('C. Writer, "A Study of Widgets", 1986')
    both = WorkRecord(title="A Study of Widgets", authors=["Cat Writer"], year=1986)
    surname_only = WorkRecord(title="A Study of Widgets", authors=["Cat Writer"], year=2001)
    neither = WorkRecord(title="A Study of Widgets", authors=["F. Someone"], year=2001)
    other_title = WorkRecord(title="Boards and Their Uses", authors=["Cat Writer"], year=1986)

    assert match_citation(cited, both).resolution == "exact"
    assert match_citation(cited, both).reasons["via"] == "title"
    assert match_citation(cited, surname_only).resolution == "probable"
    assert match_citation(cited, neither).resolution == "none"
    assert match_citation(cited, other_title).resolution == "none"


def test_year_off_by_one_is_within_tolerance():
    cited = parse_citation('C. Writer, "A Study of Widgets", 1986')
    record = WorkRecord(title="A Study of Widgets", authors=["Cat Writer"], year=1987)

    assert match_citation(cited, record).resolution == "exact"
    assert match_citation(cited, record).reasons["year_delta"] == 1
    assert match_citation(cited, record, year_tolerance=0).resolution == "probable"


def test_reprint_first_pub_year_counts_as_the_cited_year():
    cited = parse_citation('C. Writer, "A Study of Widgets", 1969/2002')
    original = WorkRecord(title="A Study of Widgets", authors=["Cat Writer"], year=1969)

    assert match_citation(cited, original).reasons["year_delta"] == 0


def test_best_title_match_picks_the_best_and_refuses_below_probable():
    cited = parse_citation('C. Writer, "A Study of Widgets", 1986')
    records = [
        WorkRecord(title="Boards and Their Uses", authors=["Cat Writer"], year=1986),
        WorkRecord(title="A Study of Widgets", authors=["F. Someone"], year=1986),
        WorkRecord(title="A Study of Widgets", authors=["Cat Writer"], year=1986),
    ]

    record, result = best_title_match(cited, records)
    assert record is records[2]
    assert result.resolution == "exact"

    none_record, none_result = best_title_match(cited, records[:1])
    assert none_record is None
    assert none_result.resolution == "none"
    assert none_result.reasons["via"] == "title"

    empty_record, empty_result = best_title_match(cited, [], title_floor=0.8)
    assert empty_record is None
    assert empty_result.thresholds == {"title_floor": 0.8, "year_tolerance": 1}
