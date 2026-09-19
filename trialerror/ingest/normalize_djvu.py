"""DjVu ingest normalizer (design Section 6 stage 3 extension).

``.djvu``/``.djv`` is registered as its own ``media_type`` ("djvu",
:mod:`trialerror.ingest.normalizers`'s ``_EXTENSION_MEDIA_TYPE``) but is
deliberately absent from both ``MEDIA_TYPES_DIRECT`` and
``MEDIA_TYPES_NEEDING_OCR`` -- unlike every format those two sets cover,
DjVu is never normalized (or OCRed) directly. Instead it rides a THIRD,
transitional job stage, ``'djvu'`` (``trialerror.ingest.pipeline``'s
``_CUSTOM_STAGE_KINDS`` extension point -- a stage that will never itself
become a first-class ``job.kind`` value, exactly the use that frozenset's
own docstring names), whose handler (:func:`trialerror.ingest.handlers.run_djvu`)
does three things and nothing else:

1. Converts the source with DjVuLibre's own ``ddjvu -format=pdf`` into a
   derived PDF under this document's own ``<archive_dir>/derived/<doc_id>/``
   directory (Fix pass, VERIFY_ingest-djvu.md F3: NOT ``jobs_work/`` --
   that directory is a per-document SCRATCH area :mod:`trialerror.obs.checks`
   documents as an unbounded-growth surface an operator may prune at will;
   the derived PDF becomes this document's permanent ``raw_path``, so it
   lives beside ``archive/<doc_id>.txt`` in the same durable tree instead,
   never next to the raw ``.djvu`` file itself).
2. Decides where this document's TEXT comes from, by running DjVuLibre's
   ``djvutxt`` over the ORIGINAL ``.djvu`` source and counting
   non-whitespace characters: ``>= DJVU_TEXT_LAYER_MIN_CHARS`` means "there
   is a real text layer here".

   **FX-D1 (2026-09-06), the fix that matters most in this module.** When
   there IS a text layer, the elements are now built FROM IT --
   :func:`extract_djvu_page_texts` calls ``djvutxt --page=N`` once per page
   and those page texts become the document's elements. The derived PDF is
   still produced, and is still what ``raw_path`` points at for viewing,
   but it is no longer the text source.

   The previous revision routed ``pdf-text`` through
   ``normalize_pdf_text``'s pypdf extraction over the derived PDF, and
   cross-checked the ``djvutxt`` verdict against that PDF's own extractable
   text (Fix pass F1), downgrading to ``pdf-scan`` on disagreement. Seen
   live: ``djvutxt`` reported 484,825 characters, ``ddjvu -format=pdf``
   produced a PDF whose extractable text failed the per-page threshold, and
   the cross-check therefore threw away a real digitized text layer to go
   and OCR images of the same pages. VERIFY_ingest-djvu.md F9 had named
   that assumption explicitly ("rests on ``ddjvu -format=pdf`` embedding
   the source's hidden text layer into the PDF it produces"); this is what
   happens when it does not hold.

   F1's underlying concern is kept, on the right evidence: a whole-document
   character count is scale-blind, and U+FFFD/control bytes survive a
   replace-decode and are not whitespace, so the extracted PER-PAGE text
   must also average :data:`DJVU_MIN_USABLE_CHARS_PER_PAGE` of
   :func:`usable_text` -- the same bar a native PDF's own route uses.
   Failing that, or extracting no non-blank page, routes to OCR as before.
   ``derived_pdf_media_type`` is still computed and recorded on the job
   checkpoint, as a diagnostic rather than a veto.

   **Still an assumption (F9's other half):** the page INDEX comes from the
   derived PDF (``ddjvu`` rendering one PDF page per DjVu page). A mismatch
   now fails loudly -- a ``djvutxt --page=N`` call errors and the job fails
   by name -- rather than half-ingesting a book. DjVuLibre is not installed
   on the build machine (hard rule), so ``tests/test_ingest_djvu.py``'s one
   real-binary test always skips here; treat the first real ``.djvu`` on a
   DjVuLibre-equipped machine as an explicit operator acceptance step and
   confirm the element count and page count before trusting the route.
3. Finishes the job by whichever route step 2 chose. On the text-layer
   route the pages go straight through ``handlers._finish_normalize_stage``
   (elements, ``stream_v1``, the document sha256, the archived stream text,
   the ``chunk`` hand-off) -- the same shared tail every other format's
   normalizer ends in, so anchors and chunking are produced by identical
   code. On the OCR route the document's ``media_type``/``raw_path`` are
   rewritten to the derived PDF and the ``ocr`` stage is enqueued for it,
   exactly as a native scanned PDF. Either way ``document.normalizer_id`` =
   :data:`NORMALIZER_ID_DJVU` ("djvu-ddjvu") and ``normalizer_version`` =
   the ``ddjvu --version``-probed string (or ``'unknown'``) rather than the
   generic ``trialerror.ingest.normalizers`` constants -- carried into the
   OCR route via ``normalizer_id_override``/``normalizer_version_override``
   in that job's payload.

**Provenance note (no migration):** the brief asks for the derived PDF's
own sha256 to land "in the document/normalizer metadata the schema already
offers ... if there is no free field, put it in the existing JSON/notes
column the other normalizers use". ``document`` has no free-form JSON/
notes column at all (``trialerror/stores/schema/knowledge.py``'s ``CREATE
TABLE document`` -- checked, not assumed). The nearest already-existing
JSON slot every handler in this package already treats as free-form,
durable, informational metadata is the ``job.checkpoint`` column (see
:mod:`trialerror.ingest.handlers`'s own module docstring: "``ctx.set_checkpoint``
is called for liveness/heartbeat and an informational progress payload").
``run_djvu`` writes ``{"djvu_pdf_sha256": ..., "djvu_text_layer": ...,
"djvu_text_chars": ..., "djvu_route": ...}`` there via ``ctx.set_checkpoint``
on the ``djvu`` stage's own job (``JOB-ingest-<doc_id>``) -- no schema
change required. Fix pass (F7): the checkpoint payload lives in the
``job.checkpoint`` column itself, which surfaces through ``trialerror jobs
list`` (a plain ``SELECT *``), NOT through ``trialerror jobs logs``
(``_cmd_logs`` reads the separate ``job_event`` table, where
``ledger.heartbeat`` logs only ``{"checkpoint_updated": true}``).

Every subprocess-calling function here is a thin, independently testable
seam (argument building / timeout / non-zero-exit are each their own
function) precisely so ``tests/test_ingest_djvu.py`` can exercise the
error-mapping and text-layer-threshold logic by monkeypatching
``subprocess.run``/``shutil.which`` -- DjVuLibre is not installed on the
build machine (hard rule), so nothing in this module's own test coverage
may require the real ``ddjvu``/``djvutxt`` binaries to exist.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from trialerror.ingest.errors import (
    DjVuConversionError,
    DjVuOutputTooLargeError,
    DjVuToolMissingError,
)

# Fix pass (F1): trialerror.ingest.normalizers has no import of THIS module
# (checked -- it only imports pypdf/stdlib at module level, plus a LOCAL
# import of trialerror.ingest.errors inside detect_media_type), so importing
# it here at module level closes no cycle. Used to cross-check the derived
# PDF's own extractable text against djvutxt's source-side count.
from trialerror.ingest import normalizers

# Same non-circular seam trialerror.ingest.backends already uses for its own
# real (marker/Qwen) backends: trialerror.jobs.worker imports only
# trialerror.jobs.{ledger,errors,registry}/trialerror.stores.store/trialerror.util.*,
# never trialerror.ingest, so importing EnvironmentalFailure here at module
# level creates no cycle.
from trialerror.jobs.worker import EnvironmentalFailure

# Fix pass (F8): non-numeric [ingest.djvu] config values now raise a named
# ConfigError (naming the file and the key) instead of a bare ValueError.
from trialerror.util.config import ConfigError

__all__ = [
    "MEDIA_TYPE_DJVU",
    "DJVU_STAGE",
    "DJVU_DEBIAN_PACKAGE",
    "NORMALIZER_ID_DJVU",
    "DJVU_TEXT_LAYER_MIN_CHARS",
    "DJVU_MIN_USABLE_CHARS_PER_PAGE",
    "DEFAULT_DJVU_TIMEOUT_S",
    "DEFAULT_DJVU_MAX_PDF_BYTES",
    "TEXT_SOURCE_DJVUTXT",
    "TEXT_SOURCE_OCR",
    "DETECTION_ORIGIN_DJVUTXT",
    "resolve_djvu_binaries",
    "build_ddjvu_convert_cmd",
    "build_djvutxt_cmd",
    "build_djvutxt_page_cmd",
    "build_ddjvu_version_cmd",
    "convert_djvu_to_pdf",
    "extract_djvu_text_char_count",
    "extract_djvu_page_texts",
    "derived_pdf_page_count",
    "usable_text",
    "has_text_layer",
    "assert_pdf_within_size_cap",
    "probe_ddjvu_version",
    "convert_and_route",
]

#: ``trialerror.ingest.normalizers._EXTENSION_MEDIA_TYPE`` maps both
#: ``.djvu``/``.djv`` to this ``media_type`` string.
MEDIA_TYPE_DJVU = "djvu"

#: The logical pipeline stage name this media_type's FIRST job rides
#: (``trialerror.ingest.pipeline.add_document`` / ``_CUSTOM_STAGE_KINDS``).
DJVU_STAGE = "djvu"

#: Named in every "binary missing" error message (design: "says which
#: binary and the Debian package") -- ``djvulibre-bin`` ships both
#: ``ddjvu`` and ``djvutxt``.
DJVU_DEBIAN_PACKAGE = "djvulibre-bin"

#: Stamped on ``document.normalizer_id`` for a document that went through
#: the djvu->pdf conversion route (instead of the generic
#: ``trialerror.ingest.normalizers.NORMALIZER_ID``).
NORMALIZER_ID_DJVU = "djvu-ddjvu"

#: design: "djvutxt <in> yields non-trivial text (>= 200 non-whitespace
#: characters over the document)". Below this, the derived PDF is treated
#: as image-only and routed to OCR exactly like a scanned PDF.
DJVU_TEXT_LAYER_MIN_CHARS = 200

#: Second gate, applied to the PER-PAGE text ``djvutxt`` actually returns
#: (Fix pass F9/FX-D1). :data:`DJVU_TEXT_LAYER_MIN_CHARS` is a whole-document
#: count and is scale-blind: a 400-page scan with a 200-character title page
#: clears it. This is the same average-chars-per-page bar a NATIVE PDF's own
#: pdf-text/pdf-scan route is decided by
#: (``normalizers._SCANNED_PDF_CHARS_PER_PAGE_THRESHOLD``), imported rather
#: than restated so the two routes cannot drift apart, and it is applied to
#: :func:`usable_text` -- so U+FFFD and control bytes, which the naive
#: non-whitespace count reads as "real characters", cannot carry a document
#: over the bar.
DJVU_MIN_USABLE_CHARS_PER_PAGE = normalizers._SCANNED_PDF_CHARS_PER_PAGE_THRESHOLD

#: ``convert_and_route``'s ``text_source``: where this document's element
#: text will come from. The checkpoint records it, because "which source of
#: truth did this document's text come from" is not recoverable afterwards
#: from the row alone.
TEXT_SOURCE_DJVUTXT = "djvutxt"
TEXT_SOURCE_OCR = "ocr"

#: ``element.detection_origin`` for a page whose text came from the DjVu
#: text layer -- distinct from ``pypdf`` (the derived PDF's own extraction)
#: and from ``ocr:<backend>``, so an element's provenance names the tool
#: that actually produced it.
DETECTION_ORIGIN_DJVUTXT = "djvutxt"

#: Default subprocess timeout (seconds) for both ``ddjvu`` and ``djvutxt``
#: -- 30 minutes, "for large books" (design), same order of magnitude as
#: ``trialerror.ingest.backends.DEFAULT_OCR_TIMEOUT_S``/``DEFAULT_EMBED_TIMEOUT_S``
#: and overridable the same way (``[ingest.djvu] timeout_s``).
#:
#: Fix pass (VERIFY_ingest-djvu.md F5): this is LARGER than
#: ``trialerror.jobs.ledger.LEASE_DURATION_S`` (900s default), same trap
#: ``DEFAULT_OCR_TIMEOUT_S``'s own docstring already documents for OCR --
#: a legitimate long-running conversion can silently outlive its lease and
#: be reclaimed by another worker mid-convert. Deployments running real
#: DjVuLibre conversions that take longer than the default lease should
#: pair ``[ingest.djvu] timeout_s`` with a matching ``--lease-s``
#: (``trialerror/cli/jobs.py``) so THIS worker's own timeout fires before
#: the ledger's lease-expiry reclaim would -- ``run_djvu`` also calls
#: ``ctx.heartbeat()`` immediately before shelling out to ``ddjvu`` to keep
#: the lease fresh going in, but that alone does not cover the conversion
#: call itself (one blocking ``subprocess.run``, no heartbeat granularity
#: inside it).
DEFAULT_DJVU_TIMEOUT_S = 1800

#: Size cap (bytes) on the PDF ``ddjvu`` produces, overridable via
#: ``[ingest.djvu] max_pdf_bytes`` -- mirrors
#: ``trialerror.webfetch.handlers._REPO_MAX_PDF_BYTES`` (64 MiB), the
#: existing precedent in this codebase for "how big a fetched/derived PDF
#: is allowed to be before something refuses it".
DEFAULT_DJVU_MAX_PDF_BYTES = 64 * 1024 * 1024

#: How much of a failed tool's stderr the named error carries (design:
#: "named error with ddjvu's stderr head" -- the HEAD, not the tail
#: ``trialerror.ingest.backends``'s marker/Qwen backends keep of theirs).
_STDERR_HEAD_CHARS = 2000


def _missing_tool_message(tool: str) -> str:
    return (
        f"DjVu ingest needs the {tool!r} executable, which was not found on PATH "
        f"and no [ingest.djvu] {tool}_exe override is set in trialerror.toml. "
        f"Install the Debian package {DJVU_DEBIAN_PACKAGE!r} ({DJVU_DEBIAN_PACKAGE} "
        f"ships both ddjvu and djvutxt), or point [ingest.djvu] {tool}_exe at its path."
    )


def _missing_override_message(tool: str, config_key: str, override: str) -> str:
    return (
        f"[ingest.djvu] {config_key} = {override!r} in trialerror.toml does not exist "
        f"or is not runnable (checked both as a bare command on PATH and as a file "
        f"path). Fix the {config_key!r} override, or remove it so {tool!r} falls back "
        f"to a PATH lookup (Debian package {DJVU_DEBIAN_PACKAGE!r})."
    )


def _resolve_one_djvu_binary(config: dict[str, Any], *, tool: str, config_key: str) -> str:
    """Fix pass (VERIFY_ingest-djvu.md F2): the old ``config.get(key) or
    shutil.which(tool)`` short-circuit never checked a CONFIGURED override
    for existence, so the most likely operator failure -- a typo in
    ``[ingest.djvu] ddjvu_exe``/``djvutxt_exe``, or a path only valid on the
    machine the config was written on -- surfaced as a bare
    ``FileNotFoundError`` from ``subprocess.run`` instead of the named
    :class:`DjVuToolMissingError` this function exists to raise. Now
    validated the same way :func:`shutil.which` would validate a bare
    command: resolvable on PATH, or an existing file on disk."""
    override = config.get(config_key)
    if override:
        resolved = shutil.which(override)
        if resolved is None and Path(override).is_file():
            resolved = override
        if resolved is None:
            raise DjVuToolMissingError(_missing_override_message(tool, config_key, override))
        return resolved
    found = shutil.which(tool)
    if not found:
        raise DjVuToolMissingError(_missing_tool_message(tool))
    return found


def resolve_djvu_binaries(config: dict[str, Any]) -> tuple[str, str]:
    """``config`` = the program's ``[ingest.djvu]`` table (a plain dict,
    read generically like every other ``[ingest.*]`` table in this
    package). Returns ``(ddjvu_exe, djvutxt_exe)``, config override first
    (validated -- F2 above), ``shutil.which`` fallback -- raises
    :class:`DjVuToolMissingError` (named, package-naming) for whichever is
    missing, ``ddjvu`` checked first since nothing else in this module can
    run without it."""
    ddjvu_exe = _resolve_one_djvu_binary(config, tool="ddjvu", config_key="ddjvu_exe")
    djvutxt_exe = _resolve_one_djvu_binary(config, tool="djvutxt", config_key="djvutxt_exe")
    return ddjvu_exe, djvutxt_exe


def build_ddjvu_convert_cmd(ddjvu_exe: str, src: Path, dest_pdf: Path) -> list[str]:
    return [ddjvu_exe, "-format=pdf", str(src), str(dest_pdf)]


def build_djvutxt_cmd(djvutxt_exe: str, src: Path) -> list[str]:
    return [djvutxt_exe, str(src)]


def build_djvutxt_page_cmd(djvutxt_exe: str, src: Path, page_number: int) -> list[str]:
    """``djvutxt --page=N <src>`` -- ONE page's text layer.

    Why per-page calls rather than one ``--detail=page`` call and a parse
    (Fix pass FX-D1): ``--detail=page`` returns a nested S-expression whose
    exact quoting/escaping this build cannot verify (DjVuLibre is not
    installed here, hard rule), and guessing an output format for the code
    path that decides a book's element text is how the F9 assumption became
    a live bug in the first place. ``--page=N`` returns plain text and the
    page number is an INPUT rather than something parsed out of the output,
    so a mismatch surfaces as a non-zero exit rather than as silently
    mis-numbered pages. The cost is one process per page; against the
    alternative for a book with a text layer -- OCRing it on a GPU -- that
    is not a trade worth agonizing over."""
    return [djvutxt_exe, f"--page={int(page_number)}", str(src)]


def build_ddjvu_version_cmd(ddjvu_exe: str) -> list[str]:
    return [ddjvu_exe, "--version"]


def _run(cmd: list[str], *, timeout_s: float, tool: str, action: str) -> subprocess.CompletedProcess:
    """Shared ``subprocess.run`` shape for ``ddjvu``/``djvutxt`` (FX-1/FX-2
    conventions this package's OCR/embed backends already established:
    ``capture_output`` + explicit UTF-8 decode with ``errors='replace'`` so
    a stray non-UTF-8 byte in a tool's own output degrades to U+FFFD
    instead of raising ``UnicodeDecodeError`` and burning a retry on a
    decode bug rather than the real diagnostics; a timeout raises
    :class:`~trialerror.jobs.worker.EnvironmentalFailure`, not a plain
    exception, so the ledger re-queues without consuming an attempt --
    same as a wedged marker/embed subprocess)."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise EnvironmentalFailure(
            f"{tool} timed out after {timeout_s}s {action} (raise [ingest.djvu] timeout_s "
            "in trialerror.toml if this is expected for large books)"
        ) from exc


def convert_djvu_to_pdf(ddjvu_exe: str, src: Path, dest_pdf: Path, *, timeout_s: float = DEFAULT_DJVU_TIMEOUT_S) -> None:
    """``ddjvu -format=pdf <src> <dest_pdf>``. ``dest_pdf``'s parent
    directory is created if needed (the caller's derived-PDF directory --
    ``<archive_dir>/derived/<doc_id>/`` post Fix-pass-F3 -- may not exist
    yet). Raises :class:`DjVuConversionError`
    (named, carries ``ddjvu``'s own stderr head) on a non-zero exit OR a
    zero exit that nonetheless produced no file at ``dest_pdf``."""
    dest_pdf.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ddjvu_convert_cmd(ddjvu_exe, src, dest_pdf)
    result = _run(cmd, timeout_s=timeout_s, tool="ddjvu", action=f"converting {src} to PDF")
    if result.returncode != 0 or not dest_pdf.exists():
        raise DjVuConversionError(
            f"ddjvu exited {result.returncode} converting {src} to PDF "
            f"(expected output at {dest_pdf}): {(result.stderr or '')[:_STDERR_HEAD_CHARS]}"
        )


def extract_djvu_text_char_count(djvutxt_exe: str, src: Path, *, timeout_s: float = DEFAULT_DJVU_TIMEOUT_S) -> int:
    """``djvutxt <src>``'s stdout, counted as non-whitespace characters
    (design: "yields non-trivial text (>= 200 non-whitespace characters
    over the document)") -- a routing signal only, never inserted as
    element text itself (see this module's own docstring). Raises
    :class:`DjVuConversionError` on a non-zero exit, same shape as the
    conversion call."""
    cmd = build_djvutxt_cmd(djvutxt_exe, src)
    result = _run(cmd, timeout_s=timeout_s, tool="djvutxt", action=f"extracting text from {src}")
    if result.returncode != 0:
        raise DjVuConversionError(
            f"djvutxt exited {result.returncode} extracting text from {src}: "
            f"{(result.stderr or '')[:_STDERR_HEAD_CHARS]}"
        )
    return len(re.sub(r"\s+", "", result.stdout or ""))


def has_text_layer(char_count: int) -> bool:
    return char_count >= DJVU_TEXT_LAYER_MIN_CHARS


#: U+FFFD plus the C0/C1 control characters that are not whitespace. These
#: survive an ``errors='replace'`` decode of a mis-encoded text layer and
#: are not ``\s``, so :func:`extract_djvu_text_char_count` -- which only
#: strips whitespace -- counts them as real characters. They are not text.
_UNUSABLE_CHARS_RE = re.compile(r"[�\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")


def usable_text(text: str) -> str:
    """``text`` with replacement characters and non-whitespace control
    bytes removed -- what is left is what could plausibly be read.

    Used for the routing DECISION only; the element text inserted into the
    record is the raw page text (minus the sanitizer's own pass), because
    silently rewriting a book's characters to make a threshold is exactly
    the kind of quiet edit this pipeline should not make."""
    return _UNUSABLE_CHARS_RE.sub("", text or "")


def extract_djvu_page_texts(
    djvutxt_exe: str,
    src: Path,
    *,
    page_count: int,
    timeout_s: float = DEFAULT_DJVU_TIMEOUT_S,
    on_progress=None,
) -> list[tuple[int, str]]:
    """``[(page_number, text), ...]`` for pages 1..``page_count``, straight
    out of the DjVu text layer -- the document's element text on the
    text-layer route.

    Blank pages are dropped (they contribute no element), but the page
    numbers of the pages that remain are the DjVu page numbers, so anchors
    and citations point where a reader would look.

    ``on_progress(page_number)`` is called after each page: the caller
    (``handlers.run_djvu``) hooks its ``ctx.heartbeat()`` to it, because a
    500-page book is 500 subprocess calls and the job's lease is 900s by
    default -- the very trap :data:`DEFAULT_DJVU_TIMEOUT_S`'s docstring
    documents for the conversion call, which that call at least cannot fix
    from the inside and this one can.

    A non-zero exit on any page raises :class:`DjVuConversionError`, the
    same shape as the document-wide call. Loud: a page index that does not
    line up with the derived PDF's is a wrong assumption about the file,
    and truncating the book silently would hide it."""
    pages: list[tuple[int, str]] = []
    for page_number in range(1, max(0, int(page_count)) + 1):
        cmd = build_djvutxt_page_cmd(djvutxt_exe, src, page_number)
        result = _run(
            cmd, timeout_s=timeout_s, tool="djvutxt", action=f"extracting page {page_number} of {src}"
        )
        if result.returncode != 0:
            raise DjVuConversionError(
                f"djvutxt exited {result.returncode} extracting page {page_number} of {src}: "
                f"{(result.stderr or '')[:_STDERR_HEAD_CHARS]}"
            )
        text = (result.stdout or "").strip()
        if text:
            pages.append((page_number, text))
        if on_progress is not None:
            on_progress(page_number)
    return pages


def derived_pdf_page_count(pdf_path: Path) -> int:
    """How many pages ``ddjvu`` rendered, via pypdf (already a dependency,
    and already opened one stage later by ``normalize_pdf_text``).

    This is the page INDEX the per-page ``djvutxt`` calls walk, so it rests
    on ``ddjvu -format=pdf`` rendering one PDF page per DjVu page -- the
    same F9-class assumption this module already documents, now with a
    louder failure: a mismatch means a ``--page=N`` call errors out and the
    job fails by name, instead of a book being quietly half-ingested.
    Returns 0 on an unreadable PDF, which routes the document to OCR."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf_path)).pages)
    except Exception:  # noqa: BLE001 - unreadable PDF -> no page index -> OCR route
        return 0


def assert_pdf_within_size_cap(pdf_path: Path, *, max_bytes: int = DEFAULT_DJVU_MAX_PDF_BYTES) -> int:
    """Returns the file's size on success; raises
    :class:`DjVuOutputTooLargeError` when ``ddjvu``'s own output exceeds
    ``max_bytes`` (``[ingest.djvu] max_pdf_bytes``) -- refused BEFORE it is
    ever hitched onto the document row / handed to normalize-or-ocr.

    Fix pass (VERIFY_ingest-djvu.md F4): the over-cap PDF is now unlinked
    before raising. Previously the refused file was left on disk at its
    ``jobs_work``/(post-F3) archive path, and because the failure settles
    ``failure_class='logic'`` (retryable), a retry re-ran the -- potentially
    30-minute -- conversion and rewrote the exact same oversized file on
    every attempt for no benefit (the outcome cannot change on retry)."""
    size = pdf_path.stat().st_size
    if size > max_bytes:
        pdf_path.unlink(missing_ok=True)
        raise DjVuOutputTooLargeError(
            f"ddjvu produced a {size}-byte PDF at {pdf_path}, over the {max_bytes}-byte "
            "cap ([ingest.djvu] max_pdf_bytes) -- refusing to route an oversized derived "
            "PDF into normalize/ocr (the oversized file has been deleted)"
        )
    return size


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def probe_ddjvu_version(ddjvu_exe: str, *, timeout_s: float = 10.0) -> str:
    """Best-effort ``ddjvu --version``-style probe (design: "if available,
    else 'unknown'") -- deliberately swallows EVERY failure (a bad probe
    must never fail the whole conversion job over a cosmetic provenance
    field) rather than propagating :class:`EnvironmentalFailure` or any
    other exception the shared ``_run`` helper might raise."""
    try:
        result = subprocess.run(
            build_ddjvu_version_cmd(ddjvu_exe),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except Exception:
        return "unknown"
    combined = (result.stdout or "") + (result.stderr or "")
    match = _VERSION_RE.search(combined)
    return match.group(1) if match else "unknown"


def _read_djvu_numeric_config(config: dict[str, Any], key: str, default, caster):
    """Fix pass (F8): ``float(config.get(...))``/``int(config.get(...))``
    used to let a typo'd ``[ingest.djvu]`` value (``timeout_s = "thirty"``)
    reach the job as a bare ``ValueError: could not convert string to
    float: 'thirty'`` naming neither the file nor the key --
    :func:`trialerror.ingest.handlers._load_config`'s OTHER config failures
    all raise :class:`~trialerror.util.config.ConfigError` naming
    ``trialerror.toml``; this one now does too."""
    raw = config.get(key, default)
    try:
        return caster(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"[ingest.djvu] {key} = {raw!r} in trialerror.toml is not a valid number ({exc})"
        ) from exc


def convert_and_route(
    *,
    program_root: Path,
    doc_id: str,
    src_path: Path,
    config: dict[str, Any],
    archive_dir: str | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """The pure(ish) core of the ``djvu`` job stage -- deliberately free of
    any ``Store``/``ctx``/ledger dependency (only ``Path``s and a plain
    config dict) so the decision logic is unit-testable without the job
    machinery (:func:`trialerror.ingest.handlers.run_djvu` is the thin ctx-aware
    wrapper around this). ``config`` = the program's ``[ingest.djvu]``
    table; ``archive_dir`` is the program's resolved ``[paths].archive_dir``
    (default :data:`trialerror.ingest.pipeline.DEFAULT_ARCHIVE_DIR` when
    ``None`` -- the caller resolves this the same way ``pipeline.add_document``
    does, since ``config`` here is scoped to ``[ingest.djvu]`` only).

    Converts ``src_path`` to a PDF under
    ``<program_root>/<archive_dir>/derived/<doc_id>/<doc_id>.pdf`` (Fix
    pass F3 -- durable, not ``jobs_work/`` scratch), enforces the size cap,
    probes the text layer via ``djvutxt``, and returns everything the
    handler needs::

        {
            "derived_pdf_path": Path,       # absolute
            "derived_pdf_rel_path": str,    # posix, relative to program_root
            "derived_pdf_sha256": str,
            "media_type": "pdf-text" | "pdf-scan",
            "text_source": "djvutxt" | "ocr",
            "pages": [(page_number, text), ...],   # empty on the ocr route
            "page_count": int,
            "text_layer": bool,
            "text_chars": int,
            "derived_pdf_media_type": str,  # informational -- see below
            "normalizer_version": str,      # ddjvu --version probe, or "unknown"
        }

    **FX-D1: the text layer is the source of truth now.** Seen live
    2026-09-06: ``djvutxt`` reported 484,825 characters over a book, and
    the PDF ``ddjvu`` produced from it had extractable text below the
    per-page threshold -- so the previous revision's F1 cross-check
    downgraded the route to ``pdf-scan`` and the real, already-digitized
    text layer was discarded in favour of OCRing images of the same pages.
    VERIFY_ingest-djvu.md F9 had flagged exactly that assumption ("all of
    the above rests on ``ddjvu -format=pdf`` embedding the source's hidden
    text layer into the PDF it produces"); the live run is what happens
    when it does not hold.

    So when the document has a text layer, the pages come from ``djvutxt``
    itself (:func:`extract_djvu_page_texts`, one call per page, page
    numbers from the derived PDF's page index) and the derived PDF is kept
    for VIEWING only -- it is no longer the text source, and its own
    extractability no longer decides anything. ``derived_pdf_media_type``
    is still computed and returned, and lands on the job checkpoint, but as
    a diagnostic rather than a veto.

    F1's real concern -- "a document-wide character count can be cleared by
    garbage or by one dense page in a 400-page scan" -- is preserved, moved
    onto the correct evidence: the extracted PER-PAGE text must average at
    least :data:`DJVU_MIN_USABLE_CHARS_PER_PAGE` of :func:`usable_text`
    (the same bar a native PDF is judged by, applied after U+FFFD and
    control bytes are discounted). Fail that, or extract no non-blank page
    at all, and the document takes the OCR route exactly as before.
    """
    ddjvu_exe, djvutxt_exe = resolve_djvu_binaries(config)
    timeout_s = _read_djvu_numeric_config(config, "timeout_s", DEFAULT_DJVU_TIMEOUT_S, float)
    max_pdf_bytes = _read_djvu_numeric_config(config, "max_pdf_bytes", DEFAULT_DJVU_MAX_PDF_BYTES, int)

    # Local import: trialerror.ingest.pipeline is the module that OWNS
    # add_document's media-type dispatch (which imports THIS module's
    # MEDIA_TYPE_DJVU at module level, see pipeline.py) -- importing
    # sha256_file/DEFAULT_ARCHIVE_DIR from it at THIS module's top level
    # would close a cycle. Same local-import convention
    # trialerror.ingest.handlers._enqueue_next_stage already uses for the
    # same reason.
    from trialerror.ingest.pipeline import DEFAULT_ARCHIVE_DIR, sha256_file

    archive_dir_value = archive_dir if archive_dir is not None else DEFAULT_ARCHIVE_DIR
    derived_dir = program_root / archive_dir_value / "derived" / doc_id
    derived_pdf_path = derived_dir / f"{doc_id}.pdf"
    convert_djvu_to_pdf(ddjvu_exe, src_path, derived_pdf_path, timeout_s=timeout_s)
    assert_pdf_within_size_cap(derived_pdf_path, max_bytes=max_pdf_bytes)

    text_chars = extract_djvu_text_char_count(djvutxt_exe, src_path, timeout_s=timeout_s)
    djvutxt_says_text_layer = has_text_layer(text_chars)

    # Informational only as of FX-D1 (it used to veto the route -- see the
    # docstring). Still computed and still recorded on the checkpoint,
    # because "djvutxt found a text layer and the derived PDF did not" is
    # exactly the discrepancy that produced the live bug, and an operator
    # should be able to see it after the fact.
    derived_pdf_media_type = normalizers._detect_pdf_media_type(derived_pdf_path)

    page_count = derived_pdf_page_count(derived_pdf_path)
    pages: list[tuple[int, str]] = []
    text_layer = False
    if djvutxt_says_text_layer and page_count > 0:
        pages = extract_djvu_page_texts(
            djvutxt_exe, src_path, page_count=page_count, timeout_s=timeout_s, on_progress=on_progress
        )
        # F1's concern, on the right evidence: the per-page text that will
        # actually become elements has to carry real characters, at a
        # density a reader would recognize as text.
        usable_chars = sum(len(usable_text(text)) for _n, text in pages)
        text_layer = bool(pages) and (usable_chars / page_count) >= DJVU_MIN_USABLE_CHARS_PER_PAGE
        if not text_layer:
            pages = []

    resolved_media_type = "pdf-text" if text_layer else "pdf-scan"
    text_source = TEXT_SOURCE_DJVUTXT if text_layer else TEXT_SOURCE_OCR

    derived_pdf_sha256 = sha256_file(derived_pdf_path)
    try:
        derived_pdf_rel_path = derived_pdf_path.resolve().relative_to(program_root.resolve()).as_posix()
    except ValueError:
        derived_pdf_rel_path = str(derived_pdf_path)

    # Fix pass (F8): the version probe used to always get a hardcoded 10s,
    # the one subprocess call an operator's [ingest.djvu] timeout_s could
    # never bound. Now shares the same configured timeout (harmless if
    # timeout_s is large -- probe_ddjvu_version swallows every failure
    # regardless, including its own TimeoutExpired).
    normalizer_version = probe_ddjvu_version(ddjvu_exe, timeout_s=timeout_s)

    return {
        "derived_pdf_path": derived_pdf_path,
        "derived_pdf_rel_path": derived_pdf_rel_path,
        "derived_pdf_sha256": derived_pdf_sha256,
        "media_type": resolved_media_type,
        "text_source": text_source,
        "pages": pages,
        "page_count": page_count,
        "text_layer": text_layer,
        "text_chars": text_chars,
        "derived_pdf_media_type": derived_pdf_media_type,
        "normalizer_version": normalizer_version,
    }
