"""Tests for ``trialerror.ingest.normalize_djvu`` (design Section 6 stage 3
extension) and its ``djvu`` job stage in ``trialerror.ingest.handlers``.

DjVuLibre is NOT installed on this build machine (hard rule) -- every test
below that needs ``ddjvu``/``djvutxt`` to actually run monkeypatches
``shutil.which``/``subprocess.run`` instead. Only
``test_djvu_end_to_end_with_real_djvulibre`` at the bottom touches the real
binaries, and it is ``skipif``-guarded to skip with a named reason (the
Debian package) when they are absent -- which is always, here.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from trialerror.ingest import pipeline
from trialerror.ingest.errors import (
    DjVuConversionError,
    DjVuOutputTooLargeError,
    DjVuResumeMediaTypeError,
    DjVuToolMissingError,
    InvalidNormalizerOverrideError,
)
from trialerror.ingest.normalize_djvu import (
    DEFAULT_DJVU_MAX_PDF_BYTES,
    DEFAULT_DJVU_TIMEOUT_S,
    DJVU_DEBIAN_PACKAGE,
    DJVU_STAGE,
    DJVU_TEXT_LAYER_MIN_CHARS,
    MEDIA_TYPE_DJVU,
    NORMALIZER_ID_DJVU,
    assert_pdf_within_size_cap,
    build_ddjvu_convert_cmd,
    build_ddjvu_version_cmd,
    build_djvutxt_cmd,
    convert_and_route,
    convert_djvu_to_pdf,
    extract_djvu_text_char_count,
    has_text_layer,
    probe_ddjvu_version,
    resolve_djvu_binaries,
)
from trialerror.ingest.normalizers import (
    MEDIA_TYPES_DIRECT,
    MEDIA_TYPES_NEEDING_OCR,
    detect_media_type,
)
from trialerror.jobs import ledger
from trialerror.jobs.worker import EnvironmentalFailure, run_one
from trialerror.util.config import ConfigError
from tests._ingest_fixtures import bootstrap_launch, build_minimal_pdf


# ---------------------------------------------------------------------------
# registration / dispatch
# ---------------------------------------------------------------------------


def test_djvu_and_djv_extensions_detect_as_djvu_media_type(tmp_path):
    assert detect_media_type(tmp_path / "book.djvu") == MEDIA_TYPE_DJVU
    assert detect_media_type(tmp_path / "book.djv") == MEDIA_TYPE_DJVU


def test_djvu_media_type_is_its_own_route_not_direct_or_ocr():
    """Unlike every MEDIA_TYPES_DIRECT/MEDIA_TYPES_NEEDING_OCR format,
    'djvu' never normalizes or OCRs directly -- it always converts first
    (this module's own docstring)."""
    assert MEDIA_TYPE_DJVU not in MEDIA_TYPES_DIRECT
    assert MEDIA_TYPE_DJVU not in MEDIA_TYPES_NEEDING_OCR


def test_djvu_stage_rides_custom_kind_via_the_documented_extension_point():
    kind, payload = pipeline.stage_job_kind_and_payload(DJVU_STAGE, {"doc_id": "DOC-1"})
    assert kind == "custom"
    assert payload == {"doc_id": "DOC-1", "handler": "djvu"}


def test_add_document_routes_djvu_media_type_to_the_djvu_stage(store, program_root):
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    djvu_path = raw_dir / "book.djvu"
    djvu_path.write_bytes(b"not a real djvu container -- never read by add_document itself")

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=djvu_path,
        created_by_launch=launch_id,
    )
    assert result["document"]["media_type"] == MEDIA_TYPE_DJVU
    job = result["job"]
    assert job["kind"] == "custom"
    import json

    assert json.loads(job["payload"])["handler"] == "djvu"


# ---------------------------------------------------------------------------
# binary discovery / error mapping when a tool is missing
# ---------------------------------------------------------------------------


def test_resolve_djvu_binaries_raises_named_error_when_ddjvu_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(DjVuToolMissingError) as excinfo:
        resolve_djvu_binaries({})
    assert "ddjvu" in str(excinfo.value)
    assert DJVU_DEBIAN_PACKAGE in str(excinfo.value)


def test_resolve_djvu_binaries_raises_named_error_when_djvutxt_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ddjvu" if name == "ddjvu" else None)
    with pytest.raises(DjVuToolMissingError) as excinfo:
        resolve_djvu_binaries({})
    assert "djvutxt" in str(excinfo.value)
    assert DJVU_DEBIAN_PACKAGE in str(excinfo.value)


def test_resolve_djvu_binaries_config_override_wins_over_which(monkeypatch, tmp_path):
    """F2 fix note: a configured override is now validated to exist, so
    this test uses REAL (empty, never executed -- ddjvu/djvutxt are never
    actually invoked by resolve_djvu_binaries) files rather than the
    made-up paths the pre-fix version used, which the fix would now
    (correctly) reject."""
    monkeypatch.setattr(shutil, "which", lambda name: None)  # nothing on PATH at all
    ddjvu_path = tmp_path / "ddjvu"
    djvutxt_path = tmp_path / "djvutxt"
    ddjvu_path.write_bytes(b"")
    djvutxt_path.write_bytes(b"")
    ddjvu_exe, djvutxt_exe = resolve_djvu_binaries(
        {"ddjvu_exe": str(ddjvu_path), "djvutxt_exe": str(djvutxt_path)}
    )
    assert ddjvu_exe == str(ddjvu_path)
    assert djvutxt_exe == str(djvutxt_path)


def test_resolve_djvu_binaries_falls_back_to_which_when_unconfigured(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    ddjvu_exe, djvutxt_exe = resolve_djvu_binaries({})
    assert ddjvu_exe == "/usr/bin/ddjvu"
    assert djvutxt_exe == "/usr/bin/djvutxt"


def test_resolve_djvu_binaries_raises_named_error_for_configured_but_absent_ddjvu(monkeypatch):
    """F2: a typo'd/stale [ingest.djvu] ddjvu_exe used to bypass
    DjVuToolMissingError entirely and surface as a bare FileNotFoundError
    from inside subprocess.run, naming neither the tool, the config key,
    nor djvulibre-bin."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(DjVuToolMissingError) as excinfo:
        resolve_djvu_binaries({"ddjvu_exe": "/nope/does-not-exist/ddjvu"})
    message = str(excinfo.value)
    assert "/nope/does-not-exist/ddjvu" in message
    assert "ddjvu_exe" in message


def test_resolve_djvu_binaries_raises_named_error_for_configured_but_absent_djvutxt(monkeypatch, tmp_path):
    ddjvu_path = tmp_path / "ddjvu"
    ddjvu_path.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(DjVuToolMissingError) as excinfo:
        resolve_djvu_binaries({"ddjvu_exe": str(ddjvu_path), "djvutxt_exe": "/nope/djvutxt"})
    message = str(excinfo.value)
    assert "/nope/djvutxt" in message
    assert "djvutxt_exe" in message


# ---------------------------------------------------------------------------
# argument building
# ---------------------------------------------------------------------------


def test_build_ddjvu_convert_cmd_shape(tmp_path):
    src = tmp_path / "book.djvu"
    dest = tmp_path / "out" / "book.pdf"
    cmd = build_ddjvu_convert_cmd("/usr/bin/ddjvu", src, dest)
    assert cmd == ["/usr/bin/ddjvu", "-format=pdf", str(src), str(dest)]


def test_build_djvutxt_cmd_shape(tmp_path):
    src = tmp_path / "book.djvu"
    assert build_djvutxt_cmd("/usr/bin/djvutxt", src) == ["/usr/bin/djvutxt", str(src)]


def test_build_ddjvu_version_cmd_shape():
    assert build_ddjvu_version_cmd("/usr/bin/ddjvu") == ["/usr/bin/ddjvu", "--version"]


# ---------------------------------------------------------------------------
# conversion: timeout / non-zero exit / missing output
# ---------------------------------------------------------------------------


def test_convert_djvu_to_pdf_timeout_raises_environmental_failure(tmp_path, monkeypatch):
    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    with pytest.raises(EnvironmentalFailure):
        convert_djvu_to_pdf("/usr/bin/ddjvu", tmp_path / "in.djvu", tmp_path / "out.pdf", timeout_s=5)


def test_convert_djvu_to_pdf_nonzero_exit_raises_conversion_error_with_stderr_head(tmp_path, monkeypatch):
    long_stderr = "boom: " + ("x" * 5000)

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=long_stderr)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(DjVuConversionError) as excinfo:
        convert_djvu_to_pdf("/usr/bin/ddjvu", tmp_path / "in.djvu", tmp_path / "out.pdf", timeout_s=5)
    message = str(excinfo.value)
    assert "boom:" in message
    assert len(message) < len(long_stderr) + 200  # the stderr HEAD, not the whole thing


def test_convert_djvu_to_pdf_zero_exit_but_no_output_file_raises(tmp_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")  # never wrote dest

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(DjVuConversionError):
        convert_djvu_to_pdf("/usr/bin/ddjvu", tmp_path / "in.djvu", tmp_path / "out.pdf", timeout_s=5)


def test_convert_djvu_to_pdf_creates_parent_dir_and_succeeds(tmp_path, monkeypatch):
    dest = tmp_path / "work" / "nested" / "out.pdf"

    def _fake_run(cmd, **kwargs):
        dest.write_bytes(b"%PDF-fake%")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    convert_djvu_to_pdf("/usr/bin/ddjvu", tmp_path / "in.djvu", dest, timeout_s=5)
    assert dest.is_file()


# ---------------------------------------------------------------------------
# text-layer probe / threshold
# ---------------------------------------------------------------------------


def test_extract_djvu_text_char_count_counts_only_nonwhitespace(tmp_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ab  cd\n\tef", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert extract_djvu_text_char_count("/usr/bin/djvutxt", tmp_path / "in.djvu") == 6  # "abcdef"


def test_extract_djvu_text_char_count_nonzero_exit_raises(tmp_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=2, stdout="", stderr="djvutxt: corrupt file")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(DjVuConversionError):
        extract_djvu_text_char_count("/usr/bin/djvutxt", tmp_path / "in.djvu")


def test_extract_djvu_text_char_count_timeout_raises_environmental_failure(tmp_path, monkeypatch):
    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    with pytest.raises(EnvironmentalFailure):
        extract_djvu_text_char_count("/usr/bin/djvutxt", tmp_path / "in.djvu", timeout_s=5)


@pytest.mark.parametrize(
    "char_count,expected",
    [
        (0, False),
        (DJVU_TEXT_LAYER_MIN_CHARS - 1, False),
        (DJVU_TEXT_LAYER_MIN_CHARS, True),
        (DJVU_TEXT_LAYER_MIN_CHARS + 500, True),
    ],
)
def test_has_text_layer_threshold_boundary(char_count, expected):
    assert has_text_layer(char_count) is expected


# ---------------------------------------------------------------------------
# version probe (best-effort, never raises)
# ---------------------------------------------------------------------------


def test_probe_ddjvu_version_extracts_version_string(monkeypatch):
    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="DjVuLibre-3.5.28\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert probe_ddjvu_version("/usr/bin/ddjvu") == "3.5.28"


def test_probe_ddjvu_version_reads_stderr_too(monkeypatch):
    """Some DjVuLibre builds print --version output to stderr, not stdout."""

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="ddjvu version 3.5.27")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert probe_ddjvu_version("/usr/bin/ddjvu") == "3.5.27"


def test_probe_ddjvu_version_falls_back_to_unknown_on_any_failure(monkeypatch):
    def _raise(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert probe_ddjvu_version("/usr/bin/ddjvu") == "unknown"


def test_probe_ddjvu_version_falls_back_to_unknown_when_no_version_looking_text(monkeypatch):
    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="usage: ddjvu [options]", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert probe_ddjvu_version("/usr/bin/ddjvu") == "unknown"


# ---------------------------------------------------------------------------
# size cap
# ---------------------------------------------------------------------------


def test_assert_pdf_within_size_cap_passes_under_cap(tmp_path):
    p = tmp_path / "small.pdf"
    p.write_bytes(b"x" * 100)
    assert assert_pdf_within_size_cap(p, max_bytes=1000) == 100


def test_assert_pdf_within_size_cap_raises_over_cap(tmp_path):
    p = tmp_path / "big.pdf"
    p.write_bytes(b"x" * 200)
    with pytest.raises(DjVuOutputTooLargeError):
        assert_pdf_within_size_cap(p, max_bytes=100)


def test_assert_pdf_within_size_cap_unlinks_the_refused_file(tmp_path):
    """F4: the over-cap PDF used to survive the refusal -- because the
    failure is retryable ('logic'), a retry re-ran the whole conversion and
    rewrote the same oversized file every attempt for no benefit."""
    p = tmp_path / "big.pdf"
    p.write_bytes(b"x" * 200)
    with pytest.raises(DjVuOutputTooLargeError):
        assert_pdf_within_size_cap(p, max_bytes=100)
    assert not p.exists()


def test_default_djvu_constants_are_sane():
    # Cheap regression guard on the constants themselves (design: "30 min
    # for large books"; a size cap "mirroring existing caps").
    assert DEFAULT_DJVU_TIMEOUT_S == 1800
    assert DEFAULT_DJVU_MAX_PDF_BYTES == 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# convert_and_route: the pure(ish) core, fully monkeypatched
# ---------------------------------------------------------------------------


def _patch_djvu_tools(monkeypatch, *, pdf_bytes: bytes, djvutxt_stdout: str, version_stdout: str = "DjVuLibre-3.5.28"):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=version_stdout, stderr="")
        if "-format=pdf" in cmd:
            dest = cmd[-1]
            from pathlib import Path

            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(pdf_bytes)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        # djvutxt <src>
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=djvutxt_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)


def test_convert_and_route_picks_pdf_text_when_text_layer_present(tmp_path, monkeypatch):
    # Fix pass (F1): each page needs to clear the NATIVE per-page threshold
    # too (normalizers._SCANNED_PDF_CHARS_PER_PAGE_THRESHOLD = 20), not just
    # DJVU_TEXT_LAYER_MIN_CHARS, now that the derived PDF is cross-checked
    # against it -- "Converted page one."/"...two." (19 chars each) used to
    # pass here but average just under 20, which is exactly the shape F1
    # exists to catch (see the two new tests below).
    pdf_bytes = build_minimal_pdf(["Converted page one with real content.", "Converted page two with real content."])
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="real text " * 30)  # well over 200 chars

    result = convert_and_route(program_root=tmp_path, doc_id="DOC-1", src_path=tmp_path / "book.djvu", config={})

    assert result["media_type"] == "pdf-text"
    assert result["text_layer"] is True
    assert result["text_chars"] >= DJVU_TEXT_LAYER_MIN_CHARS
    assert result["normalizer_version"] == "3.5.28"
    assert result["derived_pdf_path"].is_file()
    # F3 fix: the derived PDF is durable (archive_dir/derived/<doc_id>/),
    # never jobs_work/ scratch space an operator may prune at will.
    assert result["derived_pdf_rel_path"] == "archive/derived/DOC-1/DOC-1.pdf"

    import hashlib

    assert result["derived_pdf_sha256"] == hashlib.sha256(pdf_bytes).hexdigest()


def test_convert_and_route_picks_pdf_scan_when_no_text_layer(tmp_path, monkeypatch):
    pdf_bytes = build_minimal_pdf([""])
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="  \n\t ")  # whitespace only -> 0 chars

    result = convert_and_route(program_root=tmp_path, doc_id="DOC-2", src_path=tmp_path / "scan.djvu", config={})

    assert result["media_type"] == "pdf-scan"
    assert result["text_layer"] is False
    assert result["text_chars"] == 0


def test_convert_and_route_respects_configured_archive_dir(tmp_path, monkeypatch):
    """F3: the derived PDF's location follows [paths].archive_dir the same
    way pipeline.add_document's own archive/<doc_id>.txt does, so an
    operator who has already overridden archive_dir doesn't get a second,
    inconsistent durable-storage location."""
    pdf_bytes = build_minimal_pdf(["Converted page one.", "Converted page two."])
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="real text " * 30)

    result = convert_and_route(
        program_root=tmp_path, doc_id="DOC-1b", src_path=tmp_path / "book.djvu", config={},
        archive_dir="my-archive",
    )

    assert result["derived_pdf_rel_path"] == "my-archive/derived/DOC-1b/DOC-1b.pdf"
    assert (tmp_path / "my-archive" / "derived" / "DOC-1b" / "DOC-1b.pdf").is_file()


def test_convert_and_route_respects_config_size_cap(tmp_path, monkeypatch):
    pdf_bytes = build_minimal_pdf(["x"]) + b"0" * 500  # padded past a tiny cap
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="irrelevant")
    with pytest.raises(DjVuOutputTooLargeError):
        convert_and_route(
            program_root=tmp_path, doc_id="DOC-3", src_path=tmp_path / "book.djvu",
            config={"max_pdf_bytes": 50},
        )


def test_convert_and_route_forwards_configured_timeout_to_every_subprocess_call(tmp_path, monkeypatch):
    """F10: the test above (renamed from
    ...respects_config_timeout_and_size_cap) never actually asserted
    anything about the timeout. This one captures what reaches
    subprocess.run for all three calls convert_and_route makes -- ddjvu,
    djvutxt, and (F8) the version probe, which used to always get a
    hardcoded 10.0 regardless of [ingest.djvu] timeout_s."""
    seen_timeouts: list[float] = []
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="DjVuLibre-3.5.28", stderr="")
        if "-format=pdf" in cmd:
            dest = cmd[-1]
            from pathlib import Path

            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(build_minimal_pdf(["hello"]))
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="irrelevant", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    convert_and_route(
        program_root=tmp_path, doc_id="DOC-3b", src_path=tmp_path / "book.djvu", config={"timeout_s": 7}
    )

    assert seen_timeouts == [7.0, 7.0, 7.0]  # ddjvu, djvutxt, ddjvu --version -- all the same configured value


def test_convert_and_route_raises_config_error_for_non_numeric_timeout(tmp_path, monkeypatch):
    """F8: a bad [ingest.djvu] timeout_s used to surface as a bare
    ValueError naming neither the file nor the key -- every other
    _load_config failure in this codebase raises ConfigError naming
    trialerror.toml."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(ConfigError) as excinfo:
        convert_and_route(
            program_root=tmp_path, doc_id="DOC-3c", src_path=tmp_path / "book.djvu",
            config={"timeout_s": "thirty"},
        )
    message = str(excinfo.value)
    assert "timeout_s" in message
    assert "thirty" in message


def test_convert_and_route_raises_config_error_for_non_numeric_max_pdf_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(ConfigError) as excinfo:
        convert_and_route(
            program_root=tmp_path, doc_id="DOC-3d", src_path=tmp_path / "book.djvu",
            config={"max_pdf_bytes": "huge"},
        )
    message = str(excinfo.value)
    assert "max_pdf_bytes" in message
    assert "huge" in message


# ---------------------------------------------------------------------------
# F1: the route decision cross-checked against the PDF that was actually
# produced, not trusted from djvutxt's source-side count alone.
# ---------------------------------------------------------------------------


def test_convert_and_route_prefers_pdf_scan_when_derived_pdf_has_no_real_text(tmp_path, monkeypatch):
    """djvutxt's stdout clears DJVU_TEXT_LAYER_MIN_CHARS (as garbage --
    U+FFFD/NUL survive errors='replace' decoding and are not \\s, so the
    naive non-whitespace count sees them as 'real' characters), but the
    derived PDF itself has no extractable text at all. The cross-check
    against the PDF ddjvu actually produced must win: pdf-scan, not a
    near-empty document silently marked 'indexed'."""
    pdf_bytes = build_minimal_pdf([""])  # nothing pypdf can extract
    garbage_with_high_count = "�" * 150 + "\x00" * 150  # 300 chars, 0 of them real text
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout=garbage_with_high_count)

    result = convert_and_route(program_root=tmp_path, doc_id="DOC-4", src_path=tmp_path / "book.djvu", config={})

    assert result["text_chars"] >= DJVU_TEXT_LAYER_MIN_CHARS  # djvutxt alone would have said pdf-text
    assert result["media_type"] == "pdf-scan"  # the cross-check overrides it
    assert result["text_layer"] is False


def test_convert_and_route_prefers_pdf_scan_for_low_per_page_average_despite_document_wide_threshold(
    tmp_path, monkeypatch
):
    """The native PDF route's own threshold is a scale-invariant AVERAGE
    per page (normalizers._SCANNED_PDF_CHARS_PER_PAGE_THRESHOLD); the
    djvutxt-side threshold is an absolute whole-document count. A 30-page
    book whose only text is a 200-character front page clears the
    document-wide count but would never clear the native per-page average
    (avg ~6.7 chars/page) -- the cross-check must still route pdf-scan."""
    pages = ["x" * DJVU_TEXT_LAYER_MIN_CHARS] + [""] * 29
    pdf_bytes = build_minimal_pdf(pages)
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="x" * DJVU_TEXT_LAYER_MIN_CHARS)

    result = convert_and_route(program_root=tmp_path, doc_id="DOC-5", src_path=tmp_path / "book.djvu", config={})

    assert result["text_chars"] == DJVU_TEXT_LAYER_MIN_CHARS  # djvutxt alone crosses the threshold
    assert result["media_type"] == "pdf-scan"  # the per-page cross-check overrides it
    assert result["text_layer"] is False


def test_convert_and_route_still_picks_pdf_text_when_both_signals_agree(tmp_path, monkeypatch):
    """Sanity check that the cross-check doesn't just always say pdf-scan:
    when the derived PDF genuinely has a usable text layer at a reasonable
    per-page density, pdf-text still wins -- this is
    test_convert_and_route_picks_pdf_text_when_text_layer_present's own
    scenario, re-asserted here for the F1 cross-check specifically."""
    pdf_bytes = build_minimal_pdf(["Real extractable page text here.", "And a second real page of text."])
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="real text " * 30)

    result = convert_and_route(program_root=tmp_path, doc_id="DOC-5b", src_path=tmp_path / "book.djvu", config={})

    assert result["media_type"] == "pdf-text"
    assert result["text_layer"] is True


# ---------------------------------------------------------------------------
# full handler dispatch via run_one: djvu -> {normalize|ocr} -> chunk ->
# embed -> index, normalizer_id/version stamped 'djvu-ddjvu', checkpoint
# carries the derived PDF's sha256 (see this package's "no free-form JSON
# column on document" note).
# ---------------------------------------------------------------------------


def _drain(store, max_steps=12):
    results = []
    for i in range(max_steps):
        r = run_one(store, worker_id=f"w{i}")
        results.append(r)
        if r["status"] == "idle":
            break
    return results


def _add_djvu_document(store, program_root, monkeypatch, *, pdf_bytes, djvutxt_stdout):
    _patch_djvu_tools(monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout=djvutxt_stdout)
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    djvu_path = raw_dir / "book.djvu"
    djvu_path.write_bytes(b"placeholder djvu bytes -- ddjvu/djvutxt are monkeypatched, never actually read")

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=djvu_path,
        created_by_launch=launch_id,
    )
    return result["document"]["doc_id"]


def test_djvu_stage_end_to_end_pdf_text_route(store, program_root, monkeypatch):
    pdf_bytes = build_minimal_pdf(["Djvu converted page one.", "Djvu converted page two."])
    doc_id = _add_djvu_document(
        store, program_root, monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="genuine embedded text " * 20
    )
    djvu_job_id = f"JOB-ingest-{doc_id}"

    results = _drain(store)
    assert all(r["status"] in ("complete", "idle") for r in results), results

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "indexed"
    assert doc["media_type"] == "pdf-text"
    assert doc["normalizer_id"] == NORMALIZER_ID_DJVU
    assert doc["normalizer_version"] == "3.5.28"
    # F3 fix: durable archive/derived/<doc_id>/, not jobs_work/ scratch.
    assert doc["raw_path"] == f"archive/derived/{doc_id}/{doc_id}.pdf"
    assert doc["ocr_backend"] is None  # pdf-text route never touches OCR

    elements = store.knowledge.execute("SELECT COUNT(*) FROM element WHERE doc_id=?", (doc_id,)).fetchone()[0]
    assert elements == 2  # normalize_pdf_text: one NarrativeText per non-empty page

    djvu_job = ledger.get_job(store, djvu_job_id)
    import hashlib
    import json

    checkpoint = json.loads(djvu_job["checkpoint"])
    assert checkpoint["djvu_pdf_sha256"] == hashlib.sha256(pdf_bytes).hexdigest()
    assert checkpoint["djvu_route"] == "pdf-text"
    assert checkpoint["djvu_text_layer"] is True

    derived_pdf = program_root / doc["raw_path"]
    assert derived_pdf.is_file()
    assert derived_pdf.read_bytes() == pdf_bytes


def test_djvu_stage_end_to_end_pdf_scan_route_uses_fake_ocr_backend(store, program_root, monkeypatch):
    pdf_bytes = build_minimal_pdf([""])  # image-only: no real text layer either way
    doc_id = _add_djvu_document(
        store, program_root, monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout=""  # 0 chars -> pdf-scan
    )

    results = _drain(store)
    assert all(r["status"] in ("complete", "idle") for r in results), results

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "indexed"
    assert doc["media_type"] == "pdf-scan"
    assert doc["normalizer_id"] == NORMALIZER_ID_DJVU
    assert doc["ocr_backend"] == "fake"  # the OCR route DID run, through the fake backend


def test_djvu_stage_resume_after_row_rewrite_does_not_reconvert(store, program_root, monkeypatch):
    """Restart-safety: if a prior (crashed) attempt at the djvu job already
    rewrote document.media_type/raw_path but never got to settle, a resumed
    run must NOT try to feed the derived PDF back into ddjvu as if it were
    the original .djvu source -- it should just re-derive the next stage
    and enqueue it (idempotently)."""
    import json as json_mod

    pdf_bytes = build_minimal_pdf(["Djvu converted page one.", "Djvu converted page two."])
    doc_id = _add_djvu_document(
        store, program_root, monkeypatch, pdf_bytes=pdf_bytes, djvutxt_stdout="genuine embedded text " * 20
    )
    djvu_job_id = f"JOB-ingest-{doc_id}"

    r = run_one(store, worker_id="w0")  # runs the djvu stage to completion
    assert r["status"] == "complete"

    doc_after_first_run = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc_after_first_run["media_type"] == "pdf-text"

    # Now make subprocess.run explode if anything tries to shell out again
    # -- proves the resumed handler takes the "already converted" branch
    # rather than re-invoking ddjvu/djvutxt against the derived PDF.
    def _explode(cmd, **kwargs):
        raise AssertionError(f"resumed djvu stage must not re-invoke a subprocess, got: {cmd}")

    monkeypatch.setattr(subprocess, "run", _explode)

    job = ledger.get_job(store, djvu_job_id)
    store.jobs.execute("UPDATE job SET state='pending', claimed_by=NULL WHERE job_id=?", (djvu_job_id,))
    store.jobs.commit()
    r2 = run_one(store, worker_id="w1", job_id=djvu_job_id, kind="custom", payload=json_mod.loads(job["payload"]))
    assert r2["status"] == "complete"

    # document row untouched by the resumed no-op re-run
    doc_after_resume = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc_after_resume["media_type"] == "pdf-text"
    assert doc_after_resume["raw_path"] == doc_after_first_run["raw_path"]

    # the normalize job it hands off to still carries the right override
    normalize_job = ledger.get_job(store, f"JOB-ingest-{doc_id}-normalize")
    assert normalize_job is not None
    normalize_payload = json_mod.loads(normalize_job["payload"])
    assert normalize_payload["normalizer_id_override"] == NORMALIZER_ID_DJVU
    assert normalize_payload["normalizer_version_override"] == "3.5.28"


def test_djvu_stage_resume_rejects_unexpected_media_type(store, program_root):
    """F6: the resume ('already converted') branch used to trust whatever
    media_type it found on the row with no guard that it was one the
    conversion could have produced. Probe (misuse-only, not reachable from
    the shipped CLI): requeue the djvu stage's 'custom'/'djvu' handler
    against a document that never went through it at all -- must raise a
    named error, never silently dispatch it into OCR."""
    from tests._ingest_fixtures import write_html_fixture

    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    html_path = write_html_fixture(raw_dir / "doc.html")

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=html_path,
        created_by_launch=launch_id,
    )
    doc_id = result["document"]["doc_id"]  # media_type == 'html', never went near djvu

    r = run_one(
        store, worker_id="w0", job_id="JOB-manual-djvu-resume", kind="custom",
        payload={"doc_id": doc_id, "created_by_launch": launch_id, "handler": "djvu"},
    )
    assert r["status"] == "failed"
    job = ledger.get_job(store, "JOB-manual-djvu-resume")
    assert job["failure_class"] == "logic"
    assert DjVuResumeMediaTypeError.__name__ in job["last_error"]
    assert "html" in job["last_error"]

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "registered"  # never dispatched into ocr
    assert doc["ocr_backend"] is None


def test_run_djvu_heartbeats_before_shelling_out_to_ddjvu(store, program_root, monkeypatch):
    """F5: DEFAULT_DJVU_TIMEOUT_S (1800s) exceeds
    trialerror.jobs.ledger.LEASE_DURATION_S (900s default) -- a real
    conversion can outlive its lease before convert_and_route ever returns.
    run_djvu now calls ctx.heartbeat() immediately before that call so the
    lease is fresh going in."""
    import trialerror.ingest.normalize_djvu as normalize_djvu_mod

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")  # resolve_djvu_binaries must succeed

    calls: list[str] = []
    orig_heartbeat = ledger.heartbeat

    def _spy_heartbeat(store_, job_id, worker_id, **kwargs):
        calls.append("heartbeat")
        return orig_heartbeat(store_, job_id, worker_id, **kwargs)

    def _fake_convert_and_route(**kwargs):
        calls.append("convert_and_route")
        raise RuntimeError("boom -- never mind the real conversion")

    monkeypatch.setattr(ledger, "heartbeat", _spy_heartbeat)
    monkeypatch.setattr(normalize_djvu_mod, "convert_and_route", _fake_convert_and_route)

    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    djvu_path = raw_dir / "book.djvu"
    djvu_path.write_bytes(b"placeholder")
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=djvu_path,
        created_by_launch=launch_id,
    )

    r = run_one(store, worker_id="w0")
    assert r["status"] == "failed"
    assert calls == ["heartbeat", "convert_and_route"]  # heartbeat renews the lease BEFORE the long call


def test_run_normalize_rejects_unrecognized_normalizer_id_override(store, program_root):
    """F11: normalizer_id_override plumbing exists so the djvu route can
    stamp NORMALIZER_ID_DJVU -- it must not become a way for an arbitrary
    hand-written job payload (trialerror jobs start-worker --payload
    '{...}') to stamp free text onto document.normalizer_id for an
    ordinary document that never went near DjVu."""
    from tests._ingest_fixtures import write_pdf_text_fixture

    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = write_pdf_text_fixture(raw_dir / "doc.pdf", ["Ordinary page one.", "Ordinary page two."])

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=pdf_path,
        created_by_launch=launch_id, media_type="pdf-text",  # short fixture text; force the route explicitly
    )
    doc_id = result["document"]["doc_id"]

    r = run_one(
        store, worker_id="w0", job_id="JOB-manual-normalize-override", kind="normalize",
        payload={
            "doc_id": doc_id,
            "created_by_launch": launch_id,
            "normalizer_id_override": "hand-typed-anything",
            "normalizer_version_override": "99.99",
        },
    )
    assert r["status"] == "failed"
    job = ledger.get_job(store, "JOB-manual-normalize-override")
    assert job["failure_class"] == "logic"
    assert InvalidNormalizerOverrideError.__name__ in job["last_error"]
    assert "hand-typed-anything" in job["last_error"]

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "registered"  # never stamped -- the manual job failed before writing anything
    assert doc["normalizer_id"] == "pending"  # add_document's own NOT NULL placeholder, untouched


def test_run_normalize_accepts_the_djvu_override(store, program_root):
    """Sanity check that F11's allowlist doesn't just reject everything --
    the one override this codebase actually produces still works, on an
    ordinary pdf-text document (isolating this from the full djvu
    end-to-end route, which already covers it too)."""
    from tests._ingest_fixtures import write_pdf_text_fixture

    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = write_pdf_text_fixture(raw_dir / "doc.pdf", ["Ordinary page one.", "Ordinary page two."])

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=pdf_path,
        created_by_launch=launch_id, media_type="pdf-text",  # short fixture text; force the route explicitly
    )
    doc_id = result["document"]["doc_id"]

    r = run_one(
        store, worker_id="w0", job_id="JOB-manual-normalize-djvu-override", kind="normalize",
        payload={
            "doc_id": doc_id,
            "created_by_launch": launch_id,
            "normalizer_id_override": NORMALIZER_ID_DJVU,
            "normalizer_version_override": "3.5.28",
        },
    )
    assert r["status"] == "complete"
    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["normalizer_id"] == NORMALIZER_ID_DJVU
    assert doc["normalizer_version"] == "3.5.28"


def test_djvu_missing_binary_fails_the_job_as_a_logic_failure_not_a_traceback(store, program_root, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)  # neither tool found, no toml override
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    djvu_path = raw_dir / "book.djvu"
    djvu_path.write_bytes(b"placeholder")

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=djvu_path,
        created_by_launch=launch_id,
    )
    doc_id = result["document"]["doc_id"]

    r = run_one(store, worker_id="w0")
    assert r["status"] == "failed"  # settled, not a crashed worker process

    job = ledger.get_job(store, f"JOB-ingest-{doc_id}")
    assert job["failure_class"] == "logic"
    assert "ddjvu" in job["last_error"]
    assert DJVU_DEBIAN_PACKAGE in job["last_error"]

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "registered"  # never got past the djvu stage


# ---------------------------------------------------------------------------
# end-to-end with the REAL ddjvu/djvutxt binaries -- skipped on this
# machine (DjVuLibre is not installed here, by hard rule). If DjVuLibre's
# own cjb2 bitonal encoder happens to be present too, this authors a real
# tiny single-page DjVu from a hand-built PBM (a trivial, well-understood
# format -- unlike DjVu's own arithmetic-coded Sjbz/BG44 bitstream, which
# is not something to hand-roll the way tests/_ingest_fixtures.py hand-rolls
# a minimal PDF) and runs the full stage for real.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("ddjvu") is None or shutil.which("djvutxt") is None,
    reason=f"requires DjVuLibre's ddjvu+djvutxt (Debian package {DJVU_DEBIAN_PACKAGE}) -- not installed here",
)
def test_djvu_end_to_end_with_real_djvulibre(store, program_root):
    cjb2 = shutil.which("cjb2")
    if not cjb2:
        pytest.skip(
            "ddjvu/djvutxt are present but no djvulibre encoder (cjb2) is available in this "
            "sandbox to author a fixture .djvu file from scratch"
        )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    pbm_path = raw_dir / "page.pbm"
    # Minimal 8x8 1-bit PBM (P4, packed MSB-first) -- a trivial, spec-valid
    # format, same spirit as tests/_ingest_fixtures.py's hand-built PDF.
    pbm_path.write_bytes(b"P4\n8 8\n" + bytes([0xFF]) * 8)
    djvu_path = raw_dir / "book.djvu"
    subprocess.run([cjb2, str(pbm_path), str(djvu_path)], check=True, timeout=60)

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=djvu_path,
        created_by_launch=launch_id,
    )
    doc_id = result["document"]["doc_id"]

    results = _drain(store)
    assert all(r["status"] in ("complete", "idle") for r in results), results

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "indexed"
    assert doc["normalizer_id"] == NORMALIZER_ID_DJVU
