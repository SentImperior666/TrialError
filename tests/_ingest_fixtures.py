"""Not a test module (pytest only collects ``test_*.py``) -- shared
fixture builders for the M7 (``trialerror.ingest``) test suite: a minimal
launch chain (account/session/launch, XID-satisfying), and the four
document-format fixtures the M7 acceptance criterion names (pdf-text,
scanned pdf via the OCR route, html, epub)."""

from __future__ import annotations

from pathlib import Path

from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def bootstrap_launch(store: Store) -> str:
    """Insert a minimal account/session/launch chain and return the
    ``launch_id`` -- every ``source``/``quote_anchor`` write in this
    package is XID-validated against ``platform.launch``."""
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "test account", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id,
            "account_id": account_id,
            "program_id": "PROG-test",
            "session_id": session_id,
            "agent_kind": "tester",
            "model_class": "top",
            "model": "sonnet",
            "purpose": "fixture",
            "est_tokens": 100,
            "booked_ts": now(),
            "state": "PROVISIONAL",
        },
    )
    return launch_id


# --------------------------------------------------------------------------
# pdf-text: a minimal, hand-built, spec-valid multi-page PDF with a real
# extractable text layer (no external PDF-authoring dependency -- pypdf
# reads but does not author content streams).
# --------------------------------------------------------------------------


def build_minimal_pdf(pages_text: list[str]) -> bytes:
    n_pages = len(pages_text)
    catalog_id = 1
    pages_id = 2
    page_ids = [3 + i for i in range(n_pages)]
    font_id = 3 + n_pages
    content_ids = [font_id + 1 + i for i in range(n_pages)]

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objs: list[tuple[int, str]] = []
    objs.append((catalog_id, f"<< /Type /Catalog /Pages {pages_id} 0 R >>"))
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs.append((pages_id, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>"))
    for i, pid in enumerate(page_ids):
        objs.append(
            (
                pid,
                f"<< /Type /Page /Parent {pages_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/MediaBox [0 0 300 400] /Contents {content_ids[i]} 0 R >>",
            )
        )
    objs.append((font_id, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    for i, text in enumerate(pages_text):
        y = 350
        parts = []
        for line in text.split("\n"):
            parts.append(f"BT /F1 12 Tf 20 {y} Td ({esc(line)}) Tj ET")
            y -= 16
        stream = "\n".join(parts)
        objs.append((content_ids[i], f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))

    out = bytearray()
    out += b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for oid, body in objs:
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_offset = len(out)
    max_id = max(oid for oid, _ in objs)
    out += f"xref\n0 {max_id + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for oid in range(1, max_id + 1):
        off = offsets.get(oid, 0)
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {max_id + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def write_pdf_text_fixture(path: Path, pages_text: list[str] | None = None) -> Path:
    pages_text = pages_text or ["First page of a text-native PDF.", "Second page of the same PDF."]
    path.write_bytes(build_minimal_pdf(pages_text))
    return path


# --------------------------------------------------------------------------
# scanned pdf (OCR route): the FakeOcrBackend treats the file's own bytes
# as already-recognized text (form-feed page breaks) -- no image rendering
# dependency needed to exercise the OCR-route path end to end.
# --------------------------------------------------------------------------


def write_scanned_pdf_fixture(path: Path, pages_text: list[str] | None = None) -> Path:
    pages_text = pages_text or ["Scanned page one recognized text.", "Scanned page two recognized text."]
    path.write_text("\x0c".join(pages_text), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# html
# --------------------------------------------------------------------------


def write_html_fixture(path: Path) -> Path:
    path.write_text(
        "<html><body>"
        "<h1>HTML Fixture Title</h1>"
        "<p>First paragraph of the html fixture document.</p>"
        "<p>Second paragraph with more prose content for chunking.</p>"
        "<ul><li>item one</li><li>item two</li></ul>"
        "<table><tr><th>Col A</th><th>Col B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "</body></html>",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# epub
# --------------------------------------------------------------------------


def write_epub_fixture(path: Path) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>\n'
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">\n'
            '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles>\n'
            "</container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">\n'
            "  <manifest>\n"
            '    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
            '    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>\n'
            "  </manifest>\n"
            '  <spine><itemref idref="ch1"/><itemref idref="ch2"/></spine>\n'
            "</package>",
        )
        zf.writestr(
            "OEBPS/ch1.xhtml",
            "<html><body><h1>Chapter 1</h1><p>First chapter epub fixture text.</p></body></html>",
        )
        zf.writestr(
            "OEBPS/ch2.xhtml",
            "<html><body><h1>Chapter 2</h1><p>Second chapter epub fixture text.</p></body></html>",
        )
    return path


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------


def write_markdown_fixture(path: Path) -> Path:
    path.write_text(
        "# Markdown Fixture\n\nA paragraph of markdown fixture prose.\n\n- point one\n- point two\n",
        encoding="utf-8",
    )
    return path
