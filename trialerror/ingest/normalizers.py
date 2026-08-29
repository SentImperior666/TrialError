"""Format handlers: design Section 6 stage 3 ("normalize"), converging every
supported input format on the Unstructured-shaped Element taxonomy (design
Section 4.1). Covers pdf-text, html/web, epub, md directly; pdf-scan and
image route to the OCR stage instead (see :mod:`trialerror.ingest.backends` /
:mod:`trialerror.ingest.handlers` -- the OCR job produces the same element shape
from a different source).

Every ``normalize_*`` function returns a list of ELEMENT DRAFTS: plain
dicts with the same keys as the ``element`` table minus ``element_id``/
``doc_id`` (the caller mints those) -- ``seq`` starting at 0, ``type`` from
the Unstructured taxonomy subset this module actually produces (``Title``,
``NarrativeText``, ``ListItem``, ``Table``). Text is NOT sanitized here --
:mod:`trialerror.ingest.pipeline` runs :func:`trialerror.ingest.sanitizer.sanitize`
uniformly over every draft's ``text`` right before insert, so no normalizer
has to remember to call it itself.
"""

from __future__ import annotations

import html.parser
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from pypdf import PdfReader

__all__ = [
    "NORMALIZER_ID",
    "NORMALIZER_VERSION",
    "MEDIA_TYPES_NEEDING_OCR",
    "MEDIA_TYPES_DIRECT",
    "detect_media_type",
    "normalize_pdf_text",
    "normalize_html",
    "normalize_epub",
    "normalize_markdown",
    "normalize_direct",
]

#: Stamped on ``document.normalizer_id``/``normalizer_version`` (design
#: Section 4.1). Bump the version when a normalizer's OUTPUT shape changes
#: in a way that should be reflected as a re-normalization (doctor's
#: anchors_dangling doc_sha256 check catches the downstream effect).
NORMALIZER_ID = "trialerror-normalize"
NORMALIZER_VERSION = "1"

#: media_type strings that route to the OCR stage instead of direct
#: normalization (design Section 6: "pdf-scan->OCR"; image likewise has no
#: extractable text layer to normalize directly).
MEDIA_TYPES_NEEDING_OCR = frozenset({"pdf-scan", "image"})

#: media_type strings this module normalizes directly.
MEDIA_TYPES_DIRECT = frozenset({"pdf-text", "html", "epub", "md"})

_EXTENSION_MEDIA_TYPE = {
    ".html": "html",
    ".htm": "html",
    ".epub": "epub",
    ".md": "md",
    ".markdown": "md",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
}

#: Below this average extracted characters/page, a ".pdf" is treated as
#: image-only (scanned) and routed to OCR instead of trusted as pdf-text --
#: a cheap, deliberately conservative heuristic (design Section 6 leaves
#: pdf-text/pdf-scan disambiguation to the implementer; callers can always
#: override via ``add_document(..., media_type=...)`` when they already
#: know which route a file needs, which is what the acceptance fixtures do).
_SCANNED_PDF_CHARS_PER_PAGE_THRESHOLD = 20


def detect_media_type(path: Path) -> str:
    """Best-effort media_type from a file's extension (``.pdf`` gets the
    text-layer heuristic below; everything else is a straight extension
    lookup). Callers that already know the right route (fixtures, a
    request-queue delivery note) should pass ``media_type=`` explicitly to
    ``trialerror.ingest.pipeline.add_document`` rather than rely on this."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _detect_pdf_media_type(path)
    media_type = _EXTENSION_MEDIA_TYPE.get(suffix)
    if media_type is None:
        from trialerror.ingest.errors import UnsupportedMediaTypeError

        raise UnsupportedMediaTypeError(f"no normalizer registered for extension {suffix!r} ({path})")
    return media_type


def _detect_pdf_media_type(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        pages = reader.pages
        total_chars = sum(len((p.extract_text() or "")) for p in pages)
        avg = total_chars / max(len(pages), 1)
    except Exception:
        return "pdf-scan"  # unreadable as text -> assume scanned, route to OCR
    return "pdf-text" if avg >= _SCANNED_PDF_CHARS_PER_PAGE_THRESHOLD else "pdf-scan"


def _draft(
    seq: int,
    type_: str,
    text: str | None,
    *,
    text_as_html: str | None = None,
    page_number: int | None = None,
    category_depth: int | None = None,
    detection_origin: str,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "type": type_,
        "text": text,
        "text_as_html": text_as_html,
        "page_number": page_number,
        "bbox": None,
        "parent_element": None,
        "category_depth": category_depth,
        "detection_origin": detection_origin,
    }


# --------------------------------------------------------------------------
# pdf-text
# --------------------------------------------------------------------------


def normalize_pdf_text(path: Path) -> list[dict[str, Any]]:
    """One ``NarrativeText`` element per page (a coarse but faithful v0
    partitioning -- the two-pass chunker's own boundary-aware splitting is
    what actually shapes retrieval units downstream, so page-granularity
    input here is sufficient rather than under-powered)."""
    reader = PdfReader(str(path))
    drafts: list[dict[str, Any]] = []
    seq = 0
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        drafts.append(_draft(seq, "NarrativeText", text, page_number=page_number, detection_origin="pypdf"))
        seq += 1
    return drafts


# --------------------------------------------------------------------------
# html / web
# --------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "section", "article", "li", "td", "th", "blockquote", "pre"}
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_SKIP_TAGS = {"script", "style", "head", "nav", "footer", "noscript"}


class _BlockTextExtractor(html.parser.HTMLParser):
    """Minimal block-aware HTML->text extractor (stdlib-only, per the
    project's zero-new-dependency ethos for straightforward parsing --
    mirrors the FTS5-over-tantivy D2 rationale). Emits one Title element
    per heading tag (``category_depth`` = heading level), one ListItem per
    ``<li>``, one Table per ``<table>`` (``text_as_html`` = the table's raw
    markup, ``text`` = its flattened cell text -- design Section 4.1:
    ``stream_v1`` reads ``text``, never ``text_as_html``), and one
    NarrativeText per other block-level element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.drafts: list[dict[str, Any]] = []
        self._seq = 0
        self._skip_depth = 0
        self._table_depth = 0
        self._table_html: list[str] = []
        self._table_rows: list[list[str]] = []
        self._row_depth = 0
        self._current_row: list[str] = []
        self._cell_depth = 0
        self._current_cell: list[str] = []
        self._buf: list[str] = []
        self._buf_tag: str | None = None

    def _flush(self) -> None:
        if self._buf_tag is None:
            self._buf = []
            return
        text = "".join(self._buf).strip()
        text = re.sub(r"[ \t]+", " ", text)
        if text:
            if self._buf_tag in _HEADING_TAGS:
                self.drafts.append(
                    _draft(self._seq, "Title", text, category_depth=_HEADING_TAGS[self._buf_tag], detection_origin="html.parser")
                )
            elif self._buf_tag == "li":
                self.drafts.append(_draft(self._seq, "ListItem", text, detection_origin="html.parser"))
            else:
                self.drafts.append(_draft(self._seq, "NarrativeText", text, detection_origin="html.parser"))
            self._seq += 1
        self._buf = []
        self._buf_tag = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "table":
            self._table_depth += 1
            self._table_html = [f"<{tag}>"]
            self._table_rows = []
            return
        if self._table_depth:
            self._table_html.append(f"<{tag}>")
            if tag == "tr":
                self._row_depth += 1
                self._current_row = []
            elif tag in ("td", "th"):
                self._cell_depth += 1
                self._current_cell = []
            return
        if tag in _HEADING_TAGS or tag in _BLOCK_TAGS:
            self._flush()
            self._buf_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            self._table_html.append("</table>")
            if self._table_depth == 0:
                # design Section 6: "table isolation + header-row repeat" --
                # rows are newline-joined (never flattened to one blob) so
                # the chunker can split a too-large table by row and repeat
                # row 0 (the header) into each split piece.
                row_lines = [" | ".join(c.strip() for c in row if c.strip()) for row in self._table_rows]
                flat = "\n".join(r for r in row_lines if r)
                if flat.strip():
                    self.drafts.append(
                        _draft(
                            self._seq,
                            "Table",
                            flat.strip(),
                            text_as_html="".join(self._table_html),
                            detection_origin="html.parser",
                        )
                    )
                    self._seq += 1
            return
        if self._table_depth:
            self._table_html.append(f"</{tag}>")
            if tag in ("td", "th") and self._cell_depth:
                self._cell_depth -= 1
                if self._cell_depth == 0:
                    self._current_row.append("".join(self._current_cell))
                    self._current_cell = []
            elif tag == "tr" and self._row_depth:
                self._row_depth -= 1
                if self._row_depth == 0:
                    self._table_rows.append(self._current_row)
                    self._current_row = []
            return
        if tag == self._buf_tag:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._table_depth:
            self._table_html.append(data)
            if self._cell_depth:
                self._current_cell.append(data)
            return
        if self._buf_tag is not None:
            self._buf.append(data)


def normalize_html(path: Path, *, encoding: str = "utf-8") -> list[dict[str, Any]]:
    text = path.read_text(encoding=encoding, errors="replace")
    return _extract_html_elements(text)


def _extract_html_elements(html_text: str) -> list[dict[str, Any]]:
    parser = _BlockTextExtractor()
    parser.feed(html_text)
    parser.close()
    parser._flush()
    return parser.drafts


# --------------------------------------------------------------------------
# epub
# --------------------------------------------------------------------------

_OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}
_CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}


def normalize_epub(path: Path) -> list[dict[str, Any]]:
    """EPUB = a zip archive; ``META-INF/container.xml`` points at the OPF
    package document, whose ``<spine>`` gives reading order over
    ``<manifest>``-declared XHTML items. stdlib ``zipfile`` +
    ``xml.etree`` only (no new dependency), reusing the HTML block
    extractor per spine document so heading/paragraph/list/table handling
    stays in one place."""
    drafts: list[dict[str, Any]] = []
    seq_offset = 0
    with zipfile.ZipFile(path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile_el = container.find(".//c:rootfile", _CONTAINER_NS)
        if rootfile_el is None:
            raise ValueError(f"epub {path}: no <rootfile> in META-INF/container.xml")
        opf_path = rootfile_el.attrib["full-path"]
        opf_dir = "/".join(opf_path.split("/")[:-1])
        opf = ET.fromstring(zf.read(opf_path))

        manifest = {
            item.attrib["id"]: item.attrib["href"]
            for item in opf.findall(".//opf:manifest/opf:item", _OPF_NS)
        }
        spine_ids = [it.attrib["idref"] for it in opf.findall(".//opf:spine/opf:itemref", _OPF_NS)]

        for idref in spine_ids:
            href = manifest.get(idref)
            if href is None:
                continue
            full_path = f"{opf_dir}/{href}" if opf_dir else href
            try:
                raw = zf.read(full_path).decode("utf-8", errors="replace")
            except KeyError:
                continue
            doc_elements = _extract_html_elements(raw)
            for d in doc_elements:
                d["seq"] += seq_offset
                drafts.append(d)
            seq_offset += len(doc_elements)
    return drafts


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$")


def normalize_markdown(path: Path, *, encoding: str = "utf-8") -> list[dict[str, Any]]:
    text = path.read_text(encoding=encoding, errors="replace")
    lines = text.split("\n")
    drafts: list[dict[str, Any]] = []
    seq = 0
    para_buf: list[str] = []

    def flush_para() -> None:
        nonlocal seq
        joined = " ".join(l.strip() for l in para_buf if l.strip()).strip()
        para_buf.clear()
        if joined:
            drafts.append(_draft(seq, "NarrativeText", joined, detection_origin="md-split"))
            seq += 1

    for line in lines:
        heading = _MD_HEADING_RE.match(line)
        list_item = _MD_LIST_RE.match(line)
        if heading:
            flush_para()
            level = len(heading.group(1))
            drafts.append(_draft(seq, "Title", heading.group(2).strip(), category_depth=level, detection_origin="md-split"))
            seq += 1
        elif list_item:
            flush_para()
            drafts.append(_draft(seq, "ListItem", list_item.group(1).strip(), detection_origin="md-split"))
            seq += 1
        elif line.strip() == "":
            flush_para()
        else:
            para_buf.append(line)
    flush_para()
    return drafts


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

_DIRECT_DISPATCH = {
    "pdf-text": normalize_pdf_text,
    "html": normalize_html,
    "epub": normalize_epub,
    "md": normalize_markdown,
}


def normalize_direct(media_type: str, path: Path) -> list[dict[str, Any]]:
    """Dispatch to the right ``normalize_*`` for a directly-normalizable
    ``media_type`` (design Section 6: everything except pdf-scan/image,
    which route to the OCR stage instead -- see
    :data:`MEDIA_TYPES_NEEDING_OCR`)."""
    fn = _DIRECT_DISPATCH.get(media_type)
    if fn is None:
        from trialerror.ingest.errors import UnsupportedMediaTypeError

        raise UnsupportedMediaTypeError(
            f"media_type {media_type!r} has no direct normalizer (needs OCR routing, or is unsupported)"
        )
    return fn(path)
