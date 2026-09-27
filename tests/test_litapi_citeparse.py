"""Lane SI item A3: free-text citation parsing. Pure -- no transport, no store."""

from __future__ import annotations

from trialerror.litapi.citeparse import CitedWork, isbn_to_13, parse_citation, parse_seed_list


def test_doi_with_balanced_parentheses_is_kept_whole():
    """FAILS BEFORE lane SI: the module did not exist."""
    cited = parse_citation("A. Author, J. Widg. 17, 1991, doi:10.1016/0167-6423(91)90036-W")

    assert cited.doi == "10.1016/0167-6423(91)90036-w"  # normalised (lower-cased) as WorkRecord.doi is
    assert cited.year == 1991
    assert cited.surnames == ["Author"]


def test_trailing_parenthesis_and_period_trimmed():
    assert parse_citation("A. Author, A Study of Widgets (doi:10.1234/widgets.1986).").doi == "10.1234/widgets.1986"
    assert parse_citation("see https://doi.org/10.1234/abc;").doi == "10.1234/abc"
    assert parse_citation("B. Other, 1990, doi:10.1234/x(1)y),").doi == "10.1234/x(1)y"


def test_a_doi_is_not_started_inside_a_longer_number():
    assert parse_citation("A. Author, p. 210.1234/5, 1986").doi is None


def test_doi_year_digits_are_not_read_as_the_year():
    cited = parse_citation("A. Author, 1986, doi:10.1234/widgets-1999-02")
    assert cited.year == 1986


def test_isbn10_and_isbn13_checksum():
    ten = parse_citation("A. Author, A Study of Widgets, Widget Press, 1986, ISBN 0-306-40615-2")
    thirteen = parse_citation("A. Author, A Study of Widgets, 1986, ISBN 978-0-306-40615-7")
    bare = parse_citation("A. Author, 1986, 9780306406157")
    bad = parse_citation("A. Author, 1986, ISBN 0-306-40615-3")

    assert ten.isbn == "9780306406157"  # ISBN-10 normalised to ISBN-13
    assert thirteen.isbn == "9780306406157"
    assert bare.isbn == "9780306406157"
    assert bad.isbn is None
    assert bad.notes and "checksum invalid" in bad.notes[0]
    assert isbn_to_13("080442957X") == "9780804429573"
    assert isbn_to_13("123") is None


def test_reprint_years_give_first_pub_year():
    cited = parse_citation("A. Author, *A Study of Widgets*, Widget Press, 1969/2002")

    assert cited.year == 2002
    assert cited.first_pub_year == 1969
    assert cited.title_hint == "A Study of Widgets"


def test_no_reprint_form_leaves_first_pub_year_none():
    assert parse_citation("A. Author, 1986").first_pub_year is None


def test_arxiv_forms():
    assert parse_citation("A. Author, 2021, arXiv:2101.00001v2").arxiv_id == "2101.00001"
    assert parse_citation("A. Author, https://arxiv.org/abs/2305.00002").arxiv_id == "2305.00002"
    assert parse_citation("A. Author, preprint 2101.00001, 2021").arxiv_id == "2101.00001"
    assert parse_citation("A. Author, math.GT/0309136, 2003").arxiv_id == "math.gt/0309136"
    # a DOI's own digits are never read as an arXiv id
    assert parse_citation("A. Author, doi:10.48550/arXiv.2101.00001").arxiv_id is None


def test_title_hint_forms():
    assert parse_citation('A. Author, "A Study of Widgets", 1986').title_hint == "A Study of Widgets"
    assert parse_citation("A. Author, “A Study of Widgets”, 1986").title_hint == "A Study of Widgets"
    assert parse_citation("A. Author, A Study of Widgets, 1986").title_hint is None


def test_surnames_are_conservative():
    assert parse_citation("Author, A., and Other, B. (1986)").surnames == ["Author"]
    assert parse_citation("A. Author & B. Other 1986").surnames == ["Author"]
    assert parse_citation("A. Author et al. 1986").surnames == ["Author"]
    # the LAST capitalised token of the first author's name: a given name is
    # never returned as a surname
    assert parse_citation("Ann van Author, 1986").surnames == ["Author"]
    assert parse_citation("Author AB, Other CD. A study. 1986").surnames == ["Author"]
    assert parse_citation("A. Author 1986").surnames == ["Author"]
    # only the first author is read: "J. Studies" after the commas is never taken for a name
    assert parse_citation("C. Writer, D. Other, E. Third, J. Studies 29(1), 1986").surnames == ["Writer"]
    # a title-first citation yields no surname rather than title words
    assert parse_citation("*A Study of Widgets*, 1986").surnames == []
    assert parse_citation("A Very Long Title About Widgets And Gadgets, 1986").surnames == []
    assert parse_citation("doi:10.1234/x, A. Author, 1986").surnames == []  # an identifier first


def test_to_dict_round_trips_every_field():
    cited = parse_citation("A. Author, 1986")
    assert set(cited.to_dict()) == {
        "raw", "doi", "arxiv_id", "isbn", "surnames", "year", "first_pub_year", "title_hint", "notes",
    }
    assert isinstance(cited, CitedWork)


def test_seed_list_split_ignores_semicolon_inside_quotes():
    works = parse_seed_list(
        'A. Author, "Widgets; A Study", 1986; B. Other, *Gadgets; Notes*, 1990 ;C. Third (1991; reprint)'
    )

    assert [w.raw for w in works] == [
        'A. Author, "Widgets; A Study", 1986',
        "B. Other, *Gadgets; Notes*, 1990",
        "C. Third (1991; reprint)",
    ]
    assert [w.year for w in works] == [1986, 1990, 1991]


def test_seed_list_keeps_a_semicolon_inside_a_doi():
    works = parse_seed_list(
        "B. Other, 1998, doi:10.1002/(SICI)1097-4571(199806)49:8<693::AID-ASI4>3.0.CO;2-O; A. Author, 1986"
    )

    assert len(works) == 2
    assert works[0].doi == "10.1002/(sici)1097-4571(199806)49:8<693::aid-asi4>3.0.co;2-o"
    assert works[1].surnames == ["Author"]


def test_seed_list_empty_pieces_dropped():
    assert parse_seed_list("") == []
    assert [w.raw for w in parse_seed_list(" ; A. Author, 1986 ;; ")] == ["A. Author, 1986"]


def test_text_without_identifiers_keeps_the_prose():
    """Lane SI part B: what a title search can use when the citation quotes no
    title -- every identifier the parser would find, and its label, cut out."""
    from trialerror.litapi.citeparse import text_without_identifiers

    assert text_without_identifiers("C. Writer, J. Studies 29(1), 1986, doi:10.9999/x1") == "C. Writer, J. Studies 29(1), 1986"
    assert text_without_identifiers("A. Author, J. Widg. 17, 1991, doi:10.1016/0167-6423(91)90036-W") == "A. Author, J. Widg. 17, 1991"
    assert text_without_identifiers("A. Author, A Study of Widgets, 1986, ISBN 0-306-40615-2.") == "A. Author, A Study of Widgets, 1986"
    assert text_without_identifiers("B. Writer, Some Paper, arXiv:2101.00001v2, 2021") == "B. Writer, Some Paper, 2021"
    assert text_without_identifiers('C. Writer, "A Study", 1986, https://doi.org/10.9999/x1') == 'C. Writer, "A Study", 1986'
    assert text_without_identifiers("no identifiers here, 1999") == "no identifiers here, 1999"
    assert text_without_identifiers("ISBN 0-306-40615-2") == ""
