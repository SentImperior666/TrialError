"""What a page range COST, recorded beside what it produced (lane FB-8b
item 2).

Sizing ``max_range_pixels`` from the raster arithmetic under-states a
``marker_single`` process by two orders of magnitude, so the guide's
procedure is "run one small range and read what it actually cost". This is
the machinery that makes that readable: a wall-clock figure for every range,
and the child's peak resident memory wherever the platform hands it over
without a new dependency.

Two things are being claimed and they are tested apart:

* **the fields are there and are the right shape** -- driven through the real
  backend and the fake ``marker_single``, because the entry under test is the
  one a real run writes into the job's result manifest;
* **the probe cannot take the run down with it.** It is a diagnostic. Every
  platform path, including the ones this machine cannot be, has to end in a
  number or a ``None`` -- never in an exception out of an OCR run that was
  otherwise going to succeed. Those paths are driven through the probe's own
  seams (``platform``, ``psutil_module``) rather than by monkeypatching the
  interpreter, so the Windows-with-psutil and Windows-without-psutil cases are
  both exercised on a POSIX machine.
"""

from __future__ import annotations

import sys

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import (
    DEFAULT_BOUNDED_DPI,
    PEAK_RSS_SOURCE_PSUTIL,
    PEAK_RSS_SOURCE_RUSAGE,
    RANGE_PIXEL_SAFETY,
    RealMarkerOcrBackend,
    _PeakRssProbe,
)

from tests._ocr_range_fixtures import A4_PT, make_pdf, write_stub_marker

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the fake marker_single is a shebang script; the real backend is GPU-gated anyway",
)

PAGES = 10


def _budget_for(pages: int) -> float:
    per_page = (A4_PT[0] / 72.0 * DEFAULT_BOUNDED_DPI) * (A4_PT[1] / 72.0 * DEFAULT_BOUNDED_DPI)
    return per_page * pages * RANGE_PIXEL_SAFETY


def _chunked(tmp_path, **kwargs):
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * PAGES)
    script = write_stub_marker(tmp_path / "bin", total_pages=PAGES)
    backend = RealMarkerOcrBackend(
        marker_single_exe=str(script), timeout_s=60, max_range_pixels=_budget_for(3), **kwargs
    )
    return pdf, script, backend


# ---------------------------------------------------------------------------
# the fields, on a real chunked run
# ---------------------------------------------------------------------------


def test_every_range_carries_a_wall_time_and_a_peak_field(tmp_path):
    pdf, _script, backend = _chunked(tmp_path)
    result = backend.run(input_path=pdf, work_dir=tmp_path / "work")

    assert len(result.ranges) == 4
    for entry in result.ranges:
        assert isinstance(entry["range_wall_s"], float)
        assert entry["range_wall_s"] >= 0.0
        assert entry["cached"] is False
        # Present as a KEY on every entry, whatever the platform answered:
        # an absent key and a null one read very differently to somebody
        # diffing two manifests.
        assert "peak_rss_bytes" in entry
        assert "peak_rss_source" in entry
        assert entry["peak_rss_bytes"] is None or isinstance(entry["peak_rss_bytes"], int)
        assert entry["peak_rss_source"] is None or isinstance(entry["peak_rss_source"], str)
        # and the fields the lane inherited are untouched
        assert len(entry["sha256"]) == 64


def test_on_posix_the_peak_is_a_real_number_and_says_what_it_measured(tmp_path):
    """``getrusage`` is free on this platform, so a range that ran a child
    has no excuse for reporting nothing. The SOURCE string is asserted too:
    the number is a cumulative high-water mark, and one printed without that
    said beside it is one somebody will subtract from another."""
    pdf, _script, backend = _chunked(tmp_path)
    result = backend.run(input_path=pdf, work_dir=tmp_path / "work")

    for entry in result.ranges:
        assert isinstance(entry["peak_rss_bytes"], int)
        assert entry["peak_rss_bytes"] > 0
        assert entry["peak_rss_source"] == PEAK_RSS_SOURCE_RUSAGE
        assert "max over this process's children so far" in entry["peak_rss_source"]


def test_a_cached_range_says_so_and_measures_no_invocation(tmp_path):
    """A resumed range comes back in milliseconds. Without ``cached`` beside
    it, that reads as an impossibly cheap marker invocation -- and the peak
    is deliberately absent, because no child ran to have one."""
    pdf, _script, backend = _chunked(tmp_path)
    cache = tmp_path / "cache"
    backend.run(input_path=pdf, work_dir=tmp_path / "work", cache_dir=cache)

    again = backend.run(input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache)
    assert [e["cached"] for e in again.ranges] == [True] * 4
    assert [e["peak_rss_bytes"] for e in again.ranges] == [None] * 4
    assert [e["peak_rss_source"] for e in again.ranges] == [None] * 4
    assert all(isinstance(e["range_wall_s"], float) for e in again.ranges)


def test_the_record_callback_sees_each_range_as_it_is_produced(tmp_path):
    pdf, _script, backend = _chunked(tmp_path)
    seen: list[dict] = []
    result = backend.run(
        input_path=pdf, work_dir=tmp_path / "work", on_range_record=seen.append
    )

    assert [(e["first_page"], e["last_page"]) for e in seen] == [(0, 2), (3, 5), (6, 8), (9, 9)]
    assert seen == [dict(e) for e in result.ranges]


def test_the_callback_is_handed_a_copy_it_cannot_edit_the_manifest_with(tmp_path):
    """A report, not a handle on the result."""
    pdf, _script, backend = _chunked(tmp_path)

    def vandalize(record):
        record["sha256"] = "x" * 64

    result = backend.run(
        input_path=pdf, work_dir=tmp_path / "work", on_range_record=vandalize
    )
    assert all(entry["sha256"] != "x" * 64 for entry in result.ranges)


def test_an_unchunked_run_records_no_ranges_at_all(tmp_path):
    """The single-invocation path is byte for byte what it always was: no
    range list, and therefore nothing to measure per range."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 2)
    script = write_stub_marker(tmp_path / "bin", total_pages=2)
    backend = RealMarkerOcrBackend(
        marker_single_exe=str(script), timeout_s=60, max_range_pixels=_budget_for(50)
    )
    result = backend.run(input_path=pdf, work_dir=tmp_path / "work")
    assert result.ranges == ()


# ---------------------------------------------------------------------------
# the probe itself -- every platform path, none of them fatal
# ---------------------------------------------------------------------------


class _FakePsutilProcess:
    def __init__(self, children):
        self._children = children

    def children(self, recursive=False):
        return list(self._children)


class _FakeChild:
    def __init__(self, peak=None, rss=None, raises=False):
        self._peak, self._rss, self._raises = peak, rss, raises

    def memory_info(self):
        if self._raises:
            raise RuntimeError("the child exited between the listing and the read")
        info = type("info", (), {})()
        if self._peak is not None:
            info.peak_wset = self._peak
        if self._rss is not None:
            info.rss = self._rss
        return info


def _fake_psutil(children):
    module = type("psutil", (), {})()
    module.Process = lambda *_a, **_kw: _FakePsutilProcess(children)
    return module


def test_windows_without_psutil_measures_nothing_and_raises_nothing():
    """The stated rule: on Windows the number is worth less than a
    dependency, so its absence is a ``None`` and not an error."""
    with _PeakRssProbe(platform="win32", psutil_module=None) as probe:
        pass
    assert probe.peak_rss_bytes is None
    assert probe.source is None


def test_windows_without_psutil_does_not_even_look_for_one(monkeypatch):
    """The import seam is not consulted twice: an environment where
    importing psutil is itself expensive or noisy pays nothing per range
    beyond the one attempt."""
    calls = []

    def _looked():
        calls.append(1)
        return None

    monkeypatch.setattr(backends, "_import_psutil", _looked)
    with _PeakRssProbe(platform="win32") as probe:
        pass
    assert probe.peak_rss_bytes is None
    assert len(calls) == 1


def test_windows_with_psutil_samples_the_childs_peak_working_set():
    child = _FakeChild(peak=3_000_000_000)
    with _PeakRssProbe(
        platform="win32", psutil_module=_fake_psutil([child]), sample_interval_s=0.01
    ) as probe:
        import time as _time

        _time.sleep(0.05)
    assert probe.peak_rss_bytes == 3_000_000_000
    assert probe.source == PEAK_RSS_SOURCE_PSUTIL


def test_the_sampler_falls_back_to_rss_and_survives_a_child_that_vanishes():
    children = [_FakeChild(rss=2_000_000), _FakeChild(raises=True)]
    with _PeakRssProbe(
        platform="win32", psutil_module=_fake_psutil(children), sample_interval_s=0.01
    ) as probe:
        import time as _time

        _time.sleep(0.05)
    assert probe.peak_rss_bytes == 2_000_000


def test_a_psutil_that_explodes_on_construction_is_not_an_ocr_failure():
    module = type("psutil", (), {})()

    def _boom(*_a, **_kw):
        raise RuntimeError("no access to this process")

    module.Process = _boom
    with _PeakRssProbe(platform="win32", psutil_module=module) as probe:
        pass
    assert probe.peak_rss_bytes is None
    assert probe.source is None


def test_a_sampler_that_saw_nothing_reports_no_source_either():
    """A child that lived less than one sampling interval. "Measured 0"
    would be a claim; "measured nothing" is the truth."""
    with _PeakRssProbe(
        platform="win32", psutil_module=_fake_psutil([]), sample_interval_s=5.0
    ) as probe:
        pass
    assert probe.peak_rss_bytes is None
    assert probe.source is None


def test_a_getrusage_that_raises_leaves_the_range_unmeasured(monkeypatch):
    """The POSIX path, denied. Nothing about an OCR run may depend on a
    diagnostic being available."""
    import resource

    def _boom(_who):
        raise OSError("RUSAGE_CHILDREN is not available here")

    monkeypatch.setattr(resource, "getrusage", _boom)
    with _PeakRssProbe(platform="linux") as probe:
        pass
    assert probe.peak_rss_bytes is None
    assert probe.source is None


def test_a_zero_rusage_reading_is_reported_as_no_measurement(monkeypatch):
    """No child has been reaped yet, so ``ru_maxrss`` is 0. Zero bytes is
    not a measurement of a process."""
    import resource

    monkeypatch.setattr(
        resource, "getrusage", lambda _who: type("ru", (), {"ru_maxrss": 0})()
    )
    with _PeakRssProbe(platform="linux") as probe:
        pass
    assert probe.peak_rss_bytes is None
    assert probe.source is None


@pytest.mark.parametrize(
    "platform,factor", [("linux", 1024), ("freebsd13", 1024), ("darwin", 1)]
)
def test_the_rusage_unit_is_platform_dependent_and_converted(monkeypatch, platform, factor):
    """``ru_maxrss`` is kilobytes on Linux and bytes on macOS. Getting this
    wrong is a silent factor of 1024 in a number an operator sizes a memory
    budget from -- the one thing this field must not be."""
    import resource

    monkeypatch.setattr(
        resource, "getrusage", lambda _who: type("ru", (), {"ru_maxrss": 4096})()
    )
    with _PeakRssProbe(platform=platform) as probe:
        pass
    assert probe.peak_rss_bytes == 4096 * factor


def test_an_ocr_run_survives_a_probe_that_cannot_measure_anything(tmp_path, monkeypatch):
    """The whole point, end to end: a platform that gives nothing up still
    produces every page, and the manifest simply says so."""
    import resource

    monkeypatch.setattr(
        resource, "getrusage", lambda _who: (_ for _ in ()).throw(OSError("denied"))
    )
    pdf, _script, backend = _chunked(tmp_path)
    result = backend.run(input_path=pdf, work_dir=tmp_path / "work")

    assert [p.page_number for p in result.pages] == list(range(PAGES))
    assert [e["peak_rss_bytes"] for e in result.ranges] == [None] * 4
    assert all(isinstance(e["range_wall_s"], float) for e in result.ranges)


# ---------------------------------------------------------------------------
# probe (d), kept: two ways the probe could still make noise or raise
# ---------------------------------------------------------------------------


def test_a_seam_that_raises_is_not_an_ocr_failure(monkeypatch):
    """Probe (d), finding 1. `_import_psutil` swallows a broken psutil, but
    the CALL to it sat outside the guard -- so a seam that raised came out of
    `__enter__`, where there is no `with` body yet to protect the run."""

    def _explode():
        raise RuntimeError("a half-installed psutil")

    monkeypatch.setattr(backends, "_import_psutil", _explode)
    with _PeakRssProbe(platform="win32") as probe:
        pass
    assert probe.peak_rss_bytes is None
    assert probe.source is None


def test_a_junk_memory_reading_does_not_kill_the_sampler_thread(capfd):
    """Probe (d), finding 2. `int(value)` sat outside the per-child try, so a
    `memory_info` whose field was not a number killed the sampler thread and
    printed a traceback into the worker's log -- a diagnostic making noise
    that looks like a crash."""

    class _Junk:
        def memory_info(self):
            return type("info", (), {"peak_wset": object()})()

    good = _FakeChild(rss=1_500_000)
    with _PeakRssProbe(
        platform="win32",
        psutil_module=_fake_psutil([_Junk(), good]),
        sample_interval_s=0.01,
    ) as probe:
        import time as _time

        _time.sleep(0.05)
    # the usable child is still measured, and nothing was printed
    assert probe.peak_rss_bytes == 1_500_000
    err = capfd.readouterr().err
    assert "Traceback" not in err, err


@pytest.mark.parametrize("platform", ["", "sunos5", "cygwin", "WIN32", "emscripten", "darwin"])
def test_no_platform_string_makes_the_probe_raise(platform):
    """Probe (d): every route ends in a number or a None. `WIN32` is in the
    list because the check is on a lowercased string now -- it used to be
    right only because `sys.platform` happens to be lowercase."""
    with _PeakRssProbe(platform=platform, psutil_module=None) as probe:
        pass
    assert probe.peak_rss_bytes is None or isinstance(probe.peak_rss_bytes, int)


def test_a_negative_sample_interval_does_not_spin_or_raise():
    with _PeakRssProbe(
        platform="win32", psutil_module=_fake_psutil([]), sample_interval_s=-1.0
    ) as probe:
        pass
    assert probe.peak_rss_bytes is None
