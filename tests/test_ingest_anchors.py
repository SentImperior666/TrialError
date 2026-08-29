"""Tests for ``trialerror.ingest.anchors``: quote-anchor construction over
``stream_v1`` and quote_sha256 spot-resolution (design Section 4.1
``quote_anchor`` + F6)."""

from __future__ import annotations

from trialerror.ingest.anchors import build_chunk_anchor, sha256_hex, spot_resolve
from trialerror.ingest.stream import stream_v1


def _el(eid, seq, type_, text):
    return {"element_id": eid, "seq": seq, "type": type_, "text": text}


def test_build_chunk_anchor_span_matches_stream_v1_substring():
    elements = [_el("e1", 0, "Title", "Hello"), _el("e2", 1, "NarrativeText", "World")]
    anchor = build_chunk_anchor(
        doc_id="DOC-1", doc_sha256="abc", elements=elements, chunk_id="CHK-1",
        element_first="e1", element_last="e2", page_number=1,
    )
    text = stream_v1(elements)
    assert text[anchor["char_start"]:anchor["char_end"]] == anchor["quote_text"]
    assert anchor["quote_text"] == "Hello\n\nWorld"
    assert anchor["stream_fn"] == "stream_v1"
    assert anchor["doc_sha256"] == "abc"
    assert anchor["quote_sha256"] == sha256_hex("Hello\n\nWorld")


def test_build_chunk_anchor_single_element_run():
    elements = [_el("e1", 0, "NarrativeText", "only")]
    anchor = build_chunk_anchor(
        doc_id="DOC-1", doc_sha256="x", elements=elements, chunk_id="CHK-1",
        element_first="e1", element_last="e1", page_number=None,
    )
    assert anchor["quote_text"] == "only"
    assert anchor["char_start"] == 0
    assert anchor["char_end"] == 4


def test_build_chunk_anchor_excludes_pagebreak_from_span():
    elements = [
        _el("e1", 0, "NarrativeText", "start"),
        _el("e2", 1, "PageBreak", "ignored"),
        _el("e3", 2, "NarrativeText", "end"),
    ]
    anchor = build_chunk_anchor(
        doc_id="DOC-1", doc_sha256="x", elements=elements, chunk_id="CHK-1",
        element_first="e1", element_last="e3", page_number=1,
    )
    assert anchor["quote_text"] == "start\n\nend"


def test_spot_resolve_true_for_unchanged_elements():
    elements = [_el("e1", 0, "NarrativeText", "hello world")]
    anchor = build_chunk_anchor(
        doc_id="DOC-1", doc_sha256="x", elements=elements, chunk_id="CHK-1",
        element_first="e1", element_last="e1", page_number=1,
    )
    assert spot_resolve(elements, anchor) is True


def test_spot_resolve_false_when_element_text_changes():
    elements = [_el("e1", 0, "NarrativeText", "hello world")]
    anchor = build_chunk_anchor(
        doc_id="DOC-1", doc_sha256="x", elements=elements, chunk_id="CHK-1",
        element_first="e1", element_last="e1", page_number=1,
    )
    changed_elements = [_el("e1", 0, "NarrativeText", "hello CHANGED world")]
    assert spot_resolve(changed_elements, anchor) is False


def test_spot_resolve_false_when_span_now_out_of_bounds():
    elements = [_el("e1", 0, "NarrativeText", "a longer piece of text here")]
    anchor = build_chunk_anchor(
        doc_id="DOC-1", doc_sha256="x", elements=elements, chunk_id="CHK-1",
        element_first="e1", element_last="e1", page_number=1,
    )
    shrunk_elements = [_el("e1", 0, "NarrativeText", "short")]
    assert spot_resolve(shrunk_elements, anchor) is False
