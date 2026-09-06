"""Reading a list of links a human wrote.

The acceptance case of design §7 (C-0073(1)) is a delivered markdown file:
eight links, seven hosts, two GitHub repos and one markdown-escaped
``deterministic\\_lockstep``. The fixture below has that shape without being
a copy of the operator's actual file — the parser's job is to survive a
document written for people, and the awkward parts of that document are the
tests.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trialerror.webfetch.links import (
    LIST_TAG_KEYS,
    ListParseError,
    distinct_hosts,
    infer_kind,
    list_ref_for,
    parse_link_list,
    read_link_list,
)

LIST = """# Requested links for research

A paragraph of prose that names no URL and must contribute nothing.

## Papers

- [Deterministic lockstep](https://en.example.org/wiki/deterministic\\_lockstep)
- [A paper](https://papers.example.org/one.pdf) tier=open
- <https://docs.example.org/tutorials/>

## Code

- https://github.com/an-owner/a-repo
- [Another repo](https://github.com/other/repo/tree/main/docs) kind=git

## Notes

Read https://blog.example.org/post-one, then stop.

> A quoted line with https://quoted.example.org/ in it.
"""


def urls(text: str) -> list[str]:
    return [entry.url for entry in parse_link_list(text)]


# ---------------------------------------------------------------------------
# what a line may contain
# ---------------------------------------------------------------------------


def test_the_delivered_list_shape_parses_to_its_links():
    found = urls(LIST)
    assert found == [
        "https://en.example.org/wiki/deterministic_lockstep",
        "https://papers.example.org/one.pdf",
        "https://docs.example.org/tutorials/",
        "https://github.com/an-owner/a-repo",
        "https://github.com/other/repo/tree/main/docs",
        "https://blog.example.org/post-one",
    ]


def test_a_markdown_escape_is_markdowns_and_not_the_urls():
    """``deterministic\\_lockstep`` is a real line from the real list: the
    backslash is there for markdown's benefit and is not part of the URL."""
    assert "https://en.example.org/wiki/deterministic_lockstep" in urls(LIST)
    assert not any("\\" in url for url in urls(LIST))


def test_headings_prose_and_quoted_lines_contribute_nothing():
    """A delivered list is a document, and "## Papers" is not a URL. A
    quoted line is someone else's words, quoted for context."""
    found = urls(LIST)
    assert not any("quoted.example.org" in url for url in found)
    assert len(found) == 6


def test_a_bare_url_loses_the_sentence_punctuation_it_picked_up():
    assert urls("See https://example.org/a, then stop.") == ["https://example.org/a"]
    assert urls("At https://example.org/b.") == ["https://example.org/b"]


def test_a_closing_parenthesis_is_kept_because_wikipedia_urls_end_in_one():
    """The opposite failure from the one the trim guards: losing it turns a
    good link into a 404."""
    assert urls("https://en.example.org/wiki/Lock_(computing)") == [
        "https://en.example.org/wiki/Lock_(computing)"
    ]


def test_a_markdown_link_inside_a_sentence_is_not_double_counted():
    line = "See [the page](https://example.org/a) for more."
    assert urls(line) == ["https://example.org/a"]


def test_an_autolink_is_not_double_counted_either():
    assert urls("<https://example.org/a>") == ["https://example.org/a"]


def test_one_line_may_name_two_links():
    line = "- [one](https://a.example/1) and [two](https://b.example/2)"
    assert urls(line) == ["https://a.example/1", "https://b.example/2"]


def test_the_same_url_twice_yields_two_entries():
    """Deduplication belongs to the enqueue path. Doing it here would hide
    from the operator that their list repeats itself."""
    text = "- https://example.org/a\n- https://example.org/a\n"
    assert len(parse_link_list(text)) == 2


def test_a_non_http_scheme_is_not_a_link():
    assert urls("- [mail](mailto:a@example.org)\n- [f](ftp://example.org/x)") == []


def test_the_markdown_link_title_is_kept():
    entry = parse_link_list("- [A good title](https://example.org/a)")[0]
    assert entry.title == "A good title"


def test_every_entry_carries_its_line_number_and_raw_line():
    """When a list is misread, the operator is the one who has to work out
    why — so every entry can say which line it came from."""
    entries = parse_link_list(LIST)
    assert entries[0].line_no == 7
    assert "deterministic" in entries[0].raw_line
    for entry in entries:
        assert LIST.splitlines()[entry.line_no - 1] == entry.raw_line


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


def test_a_trailing_tag_is_read():
    entry = next(e for e in parse_link_list(LIST) if e.url.endswith("one.pdf"))
    assert entry.license_tier == "open"


def test_a_kind_tag_overrides_the_inference():
    entry = parse_link_list("- https://example.org/a kind=git")[0]
    assert entry.kind == "git"
    assert entry.resolved_kind == "git"


def test_a_query_string_is_not_read_as_a_tag():
    """A URL's own query is full of ``key=value`` pairs. Reading
    ``?tier=premium`` out of one as an instruction to this harness would let
    the page decide its own license tier."""
    entry = parse_link_list("- https://example.org/a?tier=commercial_restricted&kind=git")[0]
    assert entry.license_tier is None
    assert entry.kind is None
    assert entry.resolved_kind == "page"


def test_an_unknown_tag_key_is_simply_not_a_tag():
    entry = parse_link_list("- https://example.org/a tiers=open")[0]
    assert entry.license_tier is None


def test_the_tag_vocabulary_is_the_documented_pair():
    assert LIST_TAG_KEYS == {"tier", "kind"}


# ---------------------------------------------------------------------------
# kind inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo", "git"),
        ("https://www.github.com/owner/repo", "git"),
        ("https://github.com/owner/repo/tree/main/docs", "git"),
        ("https://github.com/owner", "page"),
        ("https://example.org/paper.pdf", "pdf"),
        ("https://example.org/paper.pdf?download=1", "pdf"),
        ("https://example.org/paper.PDF", "pdf"),
        ("https://example.org/article", "page"),
        ("https://gitlab.example/owner/repo", "page"),
    ],
)
def test_kind_is_inferred_from_the_url_shape(url, expected):
    assert infer_kind(url) == expected


def test_the_inference_is_a_hint_and_the_sidecar_re_checks_it():
    """A ``.pdf`` URL that serves HTML is caught by the magic sniff on the
    other side of the boundary (design §4 T5), not here."""
    assert infer_kind("https://example.org/actually-html.pdf") == "pdf"


# ---------------------------------------------------------------------------
# the list reference
# ---------------------------------------------------------------------------


def test_the_list_ref_is_the_files_content_hash(tmp_path: Path):
    path = tmp_path / "links.md"
    path.write_bytes(LIST.encode("utf-8"))
    digest = hashlib.sha256(LIST.encode("utf-8")).hexdigest()
    assert list_ref_for(path) == f"sha256:{digest}/links.md"


def test_an_edited_list_is_provably_a_different_list(tmp_path: Path):
    path = tmp_path / "links.md"
    path.write_text(LIST, encoding="utf-8")
    before = list_ref_for(path)
    path.write_text(LIST + "- https://example.org/new\n", encoding="utf-8")
    assert list_ref_for(path) != before


def test_the_list_ref_never_records_an_absolute_host_path(tmp_path: Path):
    """This string is a corpus column. One that records ``/home/<someone>/``
    has made the corpus machine-specific for no gain."""
    program_root = tmp_path / "program"
    (program_root / "deliveries").mkdir(parents=True)
    path = program_root / "deliveries" / "links.md"
    path.write_text(LIST, encoding="utf-8")

    ref = list_ref_for(path, program_root)
    assert ref.endswith("/deliveries/links.md")
    assert str(tmp_path) not in ref

    outside = tmp_path / "elsewhere.md"
    outside.write_text(LIST, encoding="utf-8")
    assert list_ref_for(outside, program_root).endswith("/elsewhere.md")
    assert str(tmp_path) not in list_ref_for(outside, program_root)


def test_read_link_list_returns_the_entries_and_the_ref(tmp_path: Path):
    path = tmp_path / "links.md"
    path.write_text(LIST, encoding="utf-8")
    entries, ref = read_link_list(path)
    assert len(entries) == 6
    assert ref.startswith("sha256:")


def test_an_absent_list_stops_the_verb(tmp_path: Path):
    with pytest.raises(ListParseError, match="does not exist"):
        read_link_list(tmp_path / "nope.md")


def test_a_list_that_is_actually_a_corpus_is_refused(tmp_path: Path):
    path = tmp_path / "huge.md"
    path.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(ListParseError, match="not a corpus"):
        read_link_list(path)


def test_an_undecodable_list_is_read_with_replacement_rather_than_lost(tmp_path: Path):
    path = tmp_path / "links.md"
    path.write_bytes(b"- https://example.org/a \xff\xfe\n")
    entries, _ref = read_link_list(path)
    assert [e.url for e in entries] == ["https://example.org/a"]


# ---------------------------------------------------------------------------
# hosts, for the operator's approval step
# ---------------------------------------------------------------------------


def test_distinct_hosts_are_first_seen_order():
    assert distinct_hosts(parse_link_list(LIST)) == [
        "en.example.org",
        "papers.example.org",
        "docs.example.org",
        "github.com",
        "blog.example.org",
    ]


def test_a_host_this_build_would_refuse_is_still_shown_when_it_is_nameable():
    """The operator needs to SEE a host to understand why its line will not
    fetch. A URL with no usable host at all contributes nothing."""
    entries = parse_link_list("- https://127.0.0.1/x\n- https://ok.example/y")
    assert distinct_hosts(entries) == ["127.0.0.1", "ok.example"]
