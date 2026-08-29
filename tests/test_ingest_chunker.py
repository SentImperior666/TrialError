"""Tests for ``trialerror.ingest.chunker``: the two-pass boundary-aware chunker
(design Section 6 stage 5)."""

from __future__ import annotations

from trialerror.ingest.chunker import (
    CHUNKER_ID,
    CHUNKER_VERSION,
    MAX_CHUNK_TOKENS,
    build_chunks,
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
