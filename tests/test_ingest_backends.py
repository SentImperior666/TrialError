"""Tests for ``trialerror.ingest.backends``: the pluggable OCR/embed backend
factories, and the deterministic fake backends every other M7 test relies
on to stay GPU-free (design Section 13 flag F18/M15: hardware-gated tests
skip-unless-available). The ``Real*`` backends are exercised only by tests
explicitly marked ``skipif`` on their configured executable's presence."""

from __future__ import annotations

import shutil
import sys

import pytest

from trialerror.ingest.backends import (
    DEFAULT_EMBED_TIMEOUT_S,
    DEFAULT_FAKE_EMBED_DIMS,
    DEFAULT_OCR_TIMEOUT_S,
    FakeEmbedBackend,
    FakeOcrBackend,
    RealMarkerOcrBackend,
    RealQwenEmbedBackend,
    load_embed_backend,
    load_ocr_backend,
)
from trialerror.jobs.worker import EnvironmentalFailure


def test_fake_ocr_backend_splits_on_form_feed_pages(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_text("page one\x0cpage two\x0cpage three", encoding="utf-8")
    backend = FakeOcrBackend()
    result = backend.run(input_path=path, work_dir=tmp_path / "work")
    assert [p.text for p in result.pages] == ["page one", "page two", "page three"]
    assert [p.page_number for p in result.pages] == [1, 2, 3]
    assert result.ocr_backend == "fake"


def test_fake_ocr_backend_single_page_when_no_form_feed(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_text("just one page of text", encoding="utf-8")
    result = FakeOcrBackend().run(input_path=path, work_dir=tmp_path / "work")
    assert len(result.pages) == 1
    assert result.pages[0].page_number == 1


def test_fake_ocr_backend_skips_blank_pages(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_text("real\x0c   \x0canother real", encoding="utf-8")
    result = FakeOcrBackend().run(input_path=path, work_dir=tmp_path / "work")
    assert [p.text for p in result.pages] == ["real", "another real"]


def test_fake_embed_backend_deterministic_same_text_same_vector():
    backend = FakeEmbedBackend(dims=8)
    v1 = backend.embed_batch(["hello world"])[0]
    v2 = backend.embed_batch(["hello world"])[0]
    assert v1 == v2


def test_fake_embed_backend_different_text_different_vector():
    backend = FakeEmbedBackend(dims=8)
    v1 = backend.embed_batch(["hello world"])[0]
    v2 = backend.embed_batch(["goodbye world"])[0]
    assert v1 != v2


def test_fake_embed_backend_respects_configured_dims():
    backend = FakeEmbedBackend(dims=32)
    vecs = backend.embed_batch(["a", "b", "c"])
    assert all(len(v) == 32 for v in vecs)
    assert backend.dims == 32


def test_fake_embed_backend_model_key_namespaced_by_dims():
    b8 = FakeEmbedBackend(dims=8)
    b16 = FakeEmbedBackend(dims=16)
    assert b8.model_key != b16.model_key


def test_fake_embed_backend_vectors_are_l2_normalized():
    backend = FakeEmbedBackend(dims=DEFAULT_FAKE_EMBED_DIMS)
    v = backend.embed_batch(["some text"])[0]
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_load_ocr_backend_defaults_to_fake():
    backend = load_ocr_backend({})
    assert isinstance(backend, FakeOcrBackend)


def test_load_ocr_backend_marker_requires_exe_path():
    with pytest.raises(ValueError):
        load_ocr_backend({"backend": "marker"})


def test_load_ocr_backend_marker_config_builds_real_backend():
    backend = load_ocr_backend({"backend": "marker", "marker_single_exe": "C:/fake/marker_single.exe"})
    assert isinstance(backend, RealMarkerOcrBackend)
    assert backend.marker_single_exe == "C:/fake/marker_single.exe"


def test_load_ocr_backend_marker_defaults_timeout_s_when_unconfigured():
    """FX-1: the config seam (``[ingest.ocr].timeout_s``) already exists --
    unconfigured falls back to the module default, not an unbounded
    subprocess.run."""
    backend = load_ocr_backend({"backend": "marker", "marker_single_exe": "C:/fake/marker_single.exe"})
    assert backend.timeout_s == DEFAULT_OCR_TIMEOUT_S


def test_load_ocr_backend_marker_respects_configured_timeout_s():
    backend = load_ocr_backend(
        {"backend": "marker", "marker_single_exe": "C:/fake/marker_single.exe", "timeout_s": 42}
    )
    assert backend.timeout_s == 42


def test_load_ocr_backend_unknown_backend_raises():
    with pytest.raises(ValueError):
        load_ocr_backend({"backend": "nonsense"})


def test_load_embed_backend_defaults_to_fake():
    backend = load_embed_backend({})
    assert isinstance(backend, FakeEmbedBackend)


def test_load_embed_backend_real_requires_python_exe_and_module_dir():
    with pytest.raises(ValueError):
        load_embed_backend({"backend": "qwen3-4b"})


def test_load_embed_backend_real_config_builds_real_backend():
    backend = load_embed_backend(
        {"backend": "qwen3-4b", "python_exe": "C:/fake/python.exe", "module_dir": "C:/fake/embeddings_local"}
    )
    assert isinstance(backend, RealQwenEmbedBackend)
    assert backend.model_key == "qwen3-4b"


def test_load_embed_backend_real_defaults_timeout_s_when_unconfigured():
    """FX-1: same config-seam-first, module-constant-fallback shape as the
    OCR backend's timeout_s."""
    backend = load_embed_backend(
        {"backend": "qwen3-4b", "python_exe": "C:/fake/python.exe", "module_dir": "C:/fake/embeddings_local"}
    )
    assert backend.timeout_s == DEFAULT_EMBED_TIMEOUT_S


def test_load_embed_backend_real_respects_configured_timeout_s():
    backend = load_embed_backend(
        {
            "backend": "qwen3-4b",
            "python_exe": "C:/fake/python.exe",
            "module_dir": "C:/fake/embeddings_local",
            "timeout_s": 7,
        }
    )
    assert backend.timeout_s == 7


def test_marker_backend_page_split_parses_paginate_output_markers():
    text = "{1}------------------------------------------------\nPage one body.\n\n{2}------------------------------------------------\nPage two body."
    pages = RealMarkerOcrBackend._split_pages(text)
    assert [p.page_number for p in pages] == [1, 2]
    assert pages[0].text == "Page one body."
    assert pages[1].text == "Page two body."


def test_marker_backend_page_split_no_markers_falls_back_to_single_page():
    pages = RealMarkerOcrBackend._split_pages("just some text with no markers")
    assert len(pages) == 1
    assert pages[0].page_number == 1


# --------------------------------------------------------------------------
# FX-1/FX-2 -- Real* backend subprocess plumbing (timeout -> Environmental
# Failure; UTF-8 decode of subprocess output). Deliberately NOT GPU-gated:
# these stand ``sys.executable`` in for ``marker_single_exe``/``python_exe``
# and drive a REAL, controlled child script through the actual
# ``subprocess.run`` call sites -- no mocking of ``subprocess.run`` itself,
# no marker/Qwen install needed (build brief: "tests must NOT need the
# GPU"), and no live-GPU skip either.
# --------------------------------------------------------------------------


def test_real_marker_ocr_backend_timeout_raises_environmental_failure(tmp_path):
    """FX-1 (IMPL_REVIEW_C_ops.md N-3): a wedged marker child must not hang
    the handler forever -- subprocess.run's own timeout kills the real
    child (no zombie left by this call), and the backend converts
    TimeoutExpired into EnvironmentalFailure so trialerror.jobs.worker.run_one's
    dedicated arm re-queues the job WITHOUT consuming a retry attempt,
    instead of the bare RuntimeError logic-failure path. ``input_path`` is
    itself a real script that sleeps well past ``timeout_s`` -- python.exe
    stood in as ``marker_single_exe`` executes it as argv[1] the same way
    it executes any script path."""
    script = tmp_path / "hangs.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    backend = RealMarkerOcrBackend(marker_single_exe=sys.executable, timeout_s=0.3)
    with pytest.raises(EnvironmentalFailure) as excinfo:
        backend.run(input_path=script, work_dir=tmp_path / "work")
    assert "timed out" in excinfo.value.reason


def test_real_marker_ocr_backend_decodes_non_ascii_stderr_as_utf8_not_cp1252(tmp_path):
    """FX-2 (IMPL_REVIEW_C_ops.md N-4): marker's own stderr is UTF-8;
    without ``encoding='utf-8'`` on subprocess.run, ``text=True`` alone
    decodes as the Windows ANSI codepage on this platform, mangling
    non-ASCII bytes instead of raising the real diagnostics. The child
    writes real UTF-8 BYTES straight to ``stderr.buffer`` (bypassing the
    child's own text-layer codepage guessing) so the assertion is really
    about the PARENT's decode, not the child's encode."""
    script = tmp_path / "fails_non_ascii.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.buffer.write('marker failed on caf\\u00e9 \\u2014 mojibake canary'.encode('utf-8'))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    backend = RealMarkerOcrBackend(marker_single_exe=sys.executable, timeout_s=30)
    with pytest.raises(RuntimeError) as excinfo:
        backend.run(input_path=script, work_dir=tmp_path / "work")
    assert "café" in str(excinfo.value)


def test_real_marker_ocr_backend_invalid_utf8_stderr_does_not_raise_unicode_decode_error(tmp_path):
    """FX-2's ``errors='replace'`` half: a stray non-UTF-8 byte on stderr
    must degrade gracefully, not crash subprocess.run's own decode with an
    uncaught UnicodeDecodeError that would masquerade as a logic failure
    unrelated to the real marker error."""
    script = tmp_path / "fails_invalid_bytes.py"
    script.write_text(
        "import sys\nsys.stderr.buffer.write(b'bad byte follows: \\xff\\xfe garbage')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    backend = RealMarkerOcrBackend(marker_single_exe=sys.executable, timeout_s=30)
    with pytest.raises(RuntimeError):  # NOT UnicodeDecodeError
        backend.run(input_path=script, work_dir=tmp_path / "work")


def test_real_qwen_embed_backend_timeout_raises_environmental_failure(tmp_path):
    """FX-1, embed side: same TimeoutExpired -> EnvironmentalFailure
    conversion as the OCR backend, for the driver-subprocess call site.
    ``_DRIVER_SOURCE`` is overridden on the INSTANCE (an internal test
    seam, mirrors this module's own ``FakeEmbedBackend.delay_s``
    precedent) to a script that sleeps, so no real embed_backend/torch
    install is needed to prove the timeout path."""
    backend = RealQwenEmbedBackend(python_exe=sys.executable, module_dir=str(tmp_path), timeout_s=0.3)
    backend._DRIVER_SOURCE = "import time\ntime.sleep(30)\n"
    with pytest.raises(EnvironmentalFailure) as excinfo:
        backend.embed_batch(["hello world"])
    assert "timed out" in excinfo.value.reason


def test_real_qwen_embed_backend_decodes_non_ascii_stderr_as_utf8_not_cp1252(tmp_path):
    """FX-2, embed side: the real embed driver's stderr (a torch/
    transformers traceback) is UTF-8 and can carry non-ASCII; must decode
    correctly, not as cp1252."""
    backend = RealQwenEmbedBackend(python_exe=sys.executable, module_dir=str(tmp_path), timeout_s=30)
    backend._DRIVER_SOURCE = (
        "import sys\n"
        "sys.stderr.buffer.write('embed failed on caf\\u00e9 \\u2014 mojibake canary'.encode('utf-8'))\n"
        "sys.exit(1)\n"
    )
    with pytest.raises(RuntimeError) as excinfo:
        backend.embed_batch(["hello world"])
    assert "café" in str(excinfo.value)


def test_real_qwen_embed_backend_invalid_utf8_stderr_does_not_raise_unicode_decode_error(tmp_path):
    """FX-2's ``errors='replace'`` half, embed side."""
    backend = RealQwenEmbedBackend(python_exe=sys.executable, module_dir=str(tmp_path), timeout_s=30)
    backend._DRIVER_SOURCE = "import sys\nsys.stderr.buffer.write(b'bad byte follows: \\xff\\xfe garbage')\nsys.exit(1)\n"
    with pytest.raises(RuntimeError):  # NOT UnicodeDecodeError
        backend.embed_batch(["hello world"])


# --------------------------------------------------------------------------
# GPU/real-backend integration -- skip-unless-available (design Section 13
# flag F18/M15: "state the hardware assumption"). These are the ONLY tests
# in the M7 suite that touch a real external executable; every other test
# in this package runs on the fake backends and needs no GPU.
# --------------------------------------------------------------------------

_MARKER_SINGLE_EXE = shutil.which("marker_single")


@pytest.mark.skipif(_MARKER_SINGLE_EXE is None, reason="marker_single not on PATH -- real-OCR-backend integration test")
def test_real_marker_ocr_backend_smoke(tmp_path):  # pragma: no cover - exercised only on a GPU/marker-equipped host
    from tests._ingest_fixtures import write_pdf_text_fixture

    backend = RealMarkerOcrBackend(marker_single_exe=_MARKER_SINGLE_EXE)
    pdf_path = write_pdf_text_fixture(tmp_path / "doc.pdf", ["Real marker smoke test page."])
    result = backend.run(input_path=pdf_path, work_dir=tmp_path / "work")
    assert result.pages
