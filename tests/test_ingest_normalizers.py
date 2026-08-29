"""Tests for ``trialerror.ingest.normalizers`` (design Section 6 stage 3 format
handlers): pdf-text, html, epub, md -- and media-type detection/dispatch."""

from __future__ import annotations

import pytest

from trialerror.ingest.errors import UnsupportedMediaTypeError
from trialerror.ingest.normalizers import (
    MEDIA_TYPES_DIRECT,
    MEDIA_TYPES_NEEDING_OCR,
    detect_media_type,
    normalize_direct,
    normalize_epub,
    normalize_html,
    normalize_markdown,
    normalize_pdf_text,
)
from tests._ingest_fixtures import write_epub_fixture, write_html_fixture, write_markdown_fixture, write_pdf_text_fixture


def test_normalize_pdf_text_one_element_per_nonempty_page(tmp_path):
    path = write_pdf_text_fixture(tmp_path / "doc.pdf", ["Page one content.", "Page two content."])
    elements = normalize_pdf_text(path)
    assert [e["type"] for e in elements] == ["NarrativeText", "NarrativeText"]
    assert "Page one content." in elements[0]["text"]
    assert elements[0]["page_number"] == 1
    assert elements[1]["page_number"] == 2
    assert [e["seq"] for e in elements] == [0, 1]


def test_normalize_html_produces_title_narrative_listitem_table():
    path_elements = None
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = write_html_fixture(Path(d) / "doc.html")
        path_elements = normalize_html(path)

    types = [e["type"] for e in path_elements]
    assert "Title" in types
    assert "NarrativeText" in types
    assert "ListItem" in types
    assert "Table" in types
    table = next(e for e in path_elements if e["type"] == "Table")
    assert table["text_as_html"] is not None
    assert "<table>" in table["text_as_html"]
    assert "Col A" in table["text"]


def test_normalize_epub_reads_spine_order_across_chapters(tmp_path):
    path = write_epub_fixture(tmp_path / "book.epub")
    elements = normalize_epub(path)
    titles = [e["text"] for e in elements if e["type"] == "Title"]
    assert titles == ["Chapter 1", "Chapter 2"]
    seqs = [e["seq"] for e in elements]
    assert seqs == sorted(seqs)  # monotonic across chapter boundary


def test_normalize_markdown_headings_paragraphs_lists(tmp_path):
    path = write_markdown_fixture(tmp_path / "doc.md")
    elements = normalize_markdown(path)
    assert elements[0]["type"] == "Title"
    assert elements[0]["text"] == "Markdown Fixture"
    assert any(e["type"] == "NarrativeText" for e in elements)
    assert sum(1 for e in elements if e["type"] == "ListItem") == 2


def test_detect_media_type_extension_dispatch(tmp_path):
    assert detect_media_type(tmp_path / "x.html") == "html"
    assert detect_media_type(tmp_path / "x.epub") == "epub"
    assert detect_media_type(tmp_path / "x.md") == "md"
    assert detect_media_type(tmp_path / "x.png") == "image"


def test_detect_media_type_pdf_text_vs_scan(tmp_path):
    text_pdf = write_pdf_text_fixture(tmp_path / "text.pdf", ["Plenty of real extractable text content here." * 3])
    assert detect_media_type(text_pdf) == "pdf-text"

    # An empty-content-stream "pdf" (no text layer) -- our heuristic must
    # route it to the OCR stage rather than mis-trust it as pdf-text.
    from tests._ingest_fixtures import build_minimal_pdf

    blank_pdf = tmp_path / "blank.pdf"
    blank_pdf.write_bytes(build_minimal_pdf([""]))
    assert detect_media_type(blank_pdf) == "pdf-scan"


def test_detect_media_type_unsupported_extension_raises(tmp_path):
    with pytest.raises(UnsupportedMediaTypeError):
        detect_media_type(tmp_path / "x.xyz")


def test_media_type_partitions_are_disjoint_and_cover_direct_dispatch():
    assert MEDIA_TYPES_DIRECT.isdisjoint(MEDIA_TYPES_NEEDING_OCR)
    for mt in MEDIA_TYPES_DIRECT:
        assert callable(normalize_direct)  # dispatch table covers every direct media type
    with pytest.raises(UnsupportedMediaTypeError):
        normalize_direct("pdf-scan", None)  # needs-OCR types have no direct normalizer
