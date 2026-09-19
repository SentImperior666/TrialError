"""Tests for ``trialerror.ingest.chunker``: the two-pass boundary-aware chunker
(design Section 6 stage 5)."""

from __future__ import annotations

from trialerror.ingest.chunker import (
    CHUNKER_ID,
    CHUNKER_VERSION,
    MAX_CHUNK_TOKENS,
    ROW_CHUNKER_ID,
    ROW_CHUNKER_VERSION,
    build_chunks,
    build_row_chunks,
    estimate_tokens,
)


def _el(eid, seq, type_, text, page=1):
    return {"element_id": eid, "seq": seq, "type": type_, "text": text, "page_number": page}


def test_build_chunks_stamps_chunker_id_and_version():
    elements = [_el("e1", 0, "NarrativeText", "hello world")]
    chunks = build_chunks(elements)
    assert len(chunks) == 1
    assert chunks[0]["chunker_id"] == CHUNKER_ID
    assert chunks[0]["chunker_version"] == CHUNKER_VERSION


def test_title_opens_a_new_section_boundary():
    elements = [
        _el("e1", 0, "Title", "Section A"),
        _el("e2", 1, "NarrativeText", "body a"),
        _el("e3", 2, "Title", "Section B"),
        _el("e4", 3, "NarrativeText", "body b"),
    ]
    chunks = build_chunks(elements, min_standalone_tokens=0)
    # two small sections, neither over cap and no recombination requested here
    texts = [c["text"] for c in chunks]
    assert any("Section A" in t and "body a" in t for t in texts)
    assert any("Section B" in t and "body b" in t for t in texts)
    assert not any("Section A" in t and "Section B" in t for t in texts)  # never merged across a Title boundary


def test_table_is_isolated_never_merged_with_surrounding_prose():
    elements = [
        _el("e1", 0, "NarrativeText", "before text"),
        _el("e2", 1, "Table", "H1 | H2\nr1 | r2"),
        _el("e3", 2, "NarrativeText", "after text"),
    ]
    chunks = build_chunks(elements, min_standalone_tokens=0)
    table_chunk = next(c for c in chunks if "H1 | H2" in c["text"])
    assert "before text" not in table_chunk["text"]
    assert "after text" not in table_chunk["text"]


def test_hard_split_respects_max_token_cap_for_an_oversized_element():
    huge_text = " ".join(["word"] * (MAX_CHUNK_TOKENS * 2 + 50))
    elements = [_el("e1", 0, "NarrativeText", huge_text)]
    chunks = build_chunks(elements)
    assert len(chunks) >= 2
    for c in chunks:
        assert c["token_count"] <= MAX_CHUNK_TOKENS
        assert estimate_tokens(c["text"]) <= MAX_CHUNK_TOKENS


def test_recombine_undersized_merges_small_trailing_groups_within_section():
    elements = [
        _el("e1", 0, "Title", "S"),
        _el("e2", 1, "NarrativeText", " ".join(["w"] * 5)),  # tiny
        _el("e3", 2, "NarrativeText", " ".join(["w"] * 5)),  # tiny
    ]
    chunks_no_recombine = build_chunks(elements, min_standalone_tokens=0)
    chunks_recombine = build_chunks(elements, min_standalone_tokens=1000)
    assert len(chunks_recombine) <= len(chunks_no_recombine)
    assert len(chunks_recombine) == 1


def test_table_header_row_repeated_across_split_pieces():
    header = "H1 | H2"
    rows = "\n".join(f"r{i} | v{i}" for i in range(400))
    elements = [_el("e1", 0, "Table", f"{header}\n{rows}")]
    chunks = build_chunks(elements)
    assert len(chunks) > 1
    for c in chunks:
        assert c["text"].startswith(header)


def test_all_chunks_respect_max_cap_never_exceeded():
    elements = [_el(f"e{i}", i, "NarrativeText", " ".join(["tok"] * 300)) for i in range(10)]
    chunks = build_chunks(elements)
    assert all(c["token_count"] <= MAX_CHUNK_TOKENS for c in chunks)


def test_chunk_seq_is_contiguous_and_ordered():
    elements = [_el(f"e{i}", i, "NarrativeText", f"text {i}") for i in range(5)]
    chunks = build_chunks(elements, min_standalone_tokens=0)
    seqs = [c["seq"] for c in chunks]
    assert seqs == list(range(len(chunks)))


def test_empty_elements_list_produces_no_chunks():
    assert build_chunks([]) == []


def test_element_first_last_reference_real_element_ids():
    elements = [_el("e1", 0, "NarrativeText", "a"), _el("e2", 1, "NarrativeText", "b")]
    chunks = build_chunks(elements, min_standalone_tokens=1000)
    assert len(chunks) == 1
    assert chunks[0]["element_first"] == "e1"
    assert chunks[0]["element_last"] == "e2"


# ---------------------------------------------------------------------------
# build_row_chunks -- one chunk per element (the inventory source kind)
# ---------------------------------------------------------------------------


def test_row_chunks_never_group_and_never_recombine():
    """The whole point: the two-pass chunker would merge these four short
    elements into one prose chunk. A reference set whose rows are the unit
    of comparison must not have them blended."""
    elements = [_el(f"e{i}", i, "NarrativeText", f"row {i} short text") for i in range(4)]
    prose = build_chunks(elements)
    rows = build_row_chunks(elements)

    assert len(prose) < len(rows)
    assert len(rows) == 4
    assert [c["text"] for c in rows] == [f"row {i} short text" for i in range(4)]
    assert [c["seq"] for c in rows] == [0, 1, 2, 3]
    assert [c["element_first"] for c in rows] == [c["element_last"] for c in rows]


def test_row_chunks_carry_their_own_chunker_id_not_the_two_pass_one():
    rows = build_row_chunks([_el("e1", 0, "NarrativeText", "one row")])
    assert rows[0]["chunker_id"] == ROW_CHUNKER_ID
    assert rows[0]["chunker_version"] == ROW_CHUNKER_VERSION
    assert rows[0]["chunker_id"] != CHUNKER_ID


def test_row_chunks_read_elements_in_seq_order_not_list_order():
    elements = [
        _el("e2", 1, "NarrativeText", "second"),
        _el("e0", 0, "NarrativeText", "first"),
        _el("e3", 2, "NarrativeText", "third"),
    ]
    assert [c["text"] for c in build_row_chunks(elements)] == ["first", "second", "third"]


def test_a_row_over_the_token_cap_is_split_and_still_attributed_to_its_element():
    long_row = " ".join(f"w{i}" for i in range(MAX_CHUNK_TOKENS + 30))
    rows = build_row_chunks([_el("e1", 0, "NarrativeText", long_row)])
    assert len(rows) == 2
    assert all(c["token_count"] <= MAX_CHUNK_TOKENS for c in rows)
    assert all(c["element_first"] == "e1" and c["element_last"] == "e1" for c in rows)
    assert [c["seq"] for c in rows] == [0, 1]


def test_an_empty_row_is_skipped_rather_than_written_as_an_empty_chunk():
    elements = [
        _el("e0", 0, "NarrativeText", "a real row"),
        _el("e1", 1, "NarrativeText", "   "),
        _el("e2", 2, "NarrativeText", ""),
        _el("e3", 3, "NarrativeText", "another real row"),
    ]
    rows = build_row_chunks(elements)
    assert [c["text"] for c in rows] == ["a real row", "another real row"]
    assert [c["seq"] for c in rows] == [0, 1]


def test_row_chunks_carry_the_page_number_of_their_own_element():
    elements = [_el("e0", 0, "NarrativeText", "row a", page=3), _el("e1", 1, "NarrativeText", "row b", page=4)]
    rows = build_row_chunks(elements)
    assert [(c["page_start"], c["page_end"]) for c in rows] == [(3, 3), (4, 4)]
