"""Tests for ``trialerror.ingest.stream``: the ``stream_v1`` canonical
serialization (design Section 4.1, the F6 blocking finding)."""

from __future__ import annotations

from trialerror.ingest.stream import STREAM_FN, stream_v1, stream_v1_with_spans


def _el(element_id, seq, type_, text):
    return {"element_id": element_id, "seq": seq, "type": type_, "text": text}


def test_stream_v1_joins_with_double_newline():
    elements = [_el("e1", 0, "Title", "A"), _el("e2", 1, "NarrativeText", "B")]
    assert stream_v1(elements) == "A\n\nB"


def test_stream_v1_excludes_pagebreak_header_footer_pagenumber_image():
    elements = [
        _el("e1", 0, "NarrativeText", "start"),
        _el("e2", 1, "PageBreak", "ignored"),
        _el("e3", 2, "Header", "ignored"),
        _el("e4", 3, "Footer", "ignored"),
        _el("e5", 4, "PageNumber", "3"),
        _el("e6", 5, "Image", "ignored"),
        _el("e7", 6, "NarrativeText", "end"),
    ]
    assert stream_v1(elements) == "start\n\nend"


def test_stream_v1_skips_empty_text_without_bare_joiner():
    """design Section 4.1: "elements whose text is empty are likewise
    skipped -- they contribute no segment and no bare joiner, rather than
    an empty string between two \\n\\n."""
    elements = [
        _el("e1", 0, "NarrativeText", "start"),
        _el("e2", 1, "NarrativeText", ""),
        _el("e3", 2, "NarrativeText", None),
        _el("e4", 3, "NarrativeText", "end"),
    ]
    assert stream_v1(elements) == "start\n\nend"
    assert "\n\n\n\n" not in stream_v1(elements)


def test_stream_v1_table_uses_text_never_text_as_html():
    elements = [{"element_id": "e1", "seq": 0, "type": "Table", "text": "A | B", "text_as_html": "<table><tr><td>A</td></tr></table>"}]
    result = stream_v1(elements)
    assert result == "A | B"
    assert "<table>" not in result


def test_stream_v1_sorts_by_seq_regardless_of_input_order():
    elements = [_el("e2", 1, "NarrativeText", "second"), _el("e1", 0, "NarrativeText", "first")]
    assert stream_v1(elements) == "first\n\nsecond"


def test_stream_v1_empty_input_is_empty_string():
    assert stream_v1([]) == ""


def test_stream_v1_with_spans_matches_stream_v1_text():
    elements = [_el("e1", 0, "Title", "Hello"), _el("e2", 1, "PageBreak", None), _el("e3", 2, "NarrativeText", "World")]
    text, spans = stream_v1_with_spans(elements)
    assert text == stream_v1(elements) == "Hello\n\nWorld"
    assert spans["e1"] == (0, 5)
    assert spans["e3"] == (7, 12)
    assert "e2" not in spans  # excluded type contributes no span
    assert text[spans["e1"][0]: spans["e1"][1]] == "Hello"
    assert text[spans["e3"][0]: spans["e3"][1]] == "World"


def test_stream_fn_constant_is_stream_v1():
    assert STREAM_FN == "stream_v1"
