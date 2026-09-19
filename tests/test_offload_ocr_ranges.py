"""The DEV worker driving a chunked OCR job (lane e1e Part B).

``tests/test_ingest_ocr_page_ranges.py`` measures the backend's own
behaviour; this file measures what the WORKER does with it, which is a
different set of claims and the ones C-0097's control law is about:

* a long document is counted in ranges on the heartbeat, not in "1 job";
* a stop lands on a range boundary, hands the claim back and publishes
  nothing -- and the ranges already produced survive the sweep that removes
  the stopped job's directory;
* the next claim of that job re-runs only what was never produced, and
  publishes a result carrying one hashed entry per range;
* a page count that disagrees with the manifest fails the job instead of
  publishing pages that belong to another document.

The backend under the worker is the real
:class:`~trialerror.ingest.backends.RealMarkerOcrBackend` against the fake
``marker_single`` of ``tests/_ocr_range_fixtures.py`` -- the worker's job is
to drive a real one, and a stub that cannot be stopped between ranges would
be a stub of the thing being tested.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from trialerror.ingest.backends import DEFAULT_BOUNDED_DPI, RealMarkerOcrBackend
from trialerror.offload import protocol
from trialerror.offload.worker import (
    OCR_RANGE_CACHE_DIRNAME,
    STOPPED_MARKER,
    run_worker,
)

from tests._ocr_range_fixtures import (
    A4_PT,
    BOTH_CONVENTIONS,
    make_pdf,
    stub_argv,
    write_stub_marker,
)
from tests._offload_fixtures import ControlTransport, StubDevBackends

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the fake marker_single is a shebang script; the real backend is GPU-gated anyway",
)

FAST_BEAT = 0.02
JOB = "JOB-ocr-ranges-1"
PAGES = 10
#: Three pages of A4 at the PLANNING DPI (``DEFAULT_BOUNDED_DPI`` = 192,
#: marker's own high-res render pass), plus the planner's safety factor: the
#: budget that turns this ten-page document into four ranges. Worked out at
#: 96 until the planning DPI was corrected, which is the low-res pass and
#: four times too generous in pixels.
BUDGET = (
    (595.0 / 72.0 * DEFAULT_BOUNDED_DPI)
    * (842.0 / 72.0 * DEFAULT_BOUNDED_DPI)
    * 3
    * 1.1
)


class HeldMarker:
    """The real backend, held at the door.

    The same determinism seam ``tests/_offload_fixtures.StubOcrBackend``'s
    ``before_run`` is, wrapped around a backend this suite cannot replace: a
    worker's cooperative checkpoint can only act on a control word the
    heartbeat thread has already read, so a test that wants a stop to land at
    a KNOWN range boundary has to be able to hold the model until the word is
    in. ``gate`` is called once, before the first range; everything else is
    the real backend's."""

    def __init__(self, inner: RealMarkerOcrBackend, gate=None):
        self.inner = inner
        self.gate = gate
        self.name = inner.name
        self.version = inner.version
        self.supports_page_ranges = inner.supports_page_ranges
        self.max_range_pixels = inner.max_range_pixels
        self.bounded_dpi = inner.bounded_dpi
        self.planning_dpi = inner.planning_dpi
        self.planning_dpi_source = inner.planning_dpi_source

    def plan_ranges(self, input_path):
        return self.inner.plan_ranges(input_path)

    def run(self, **kwargs):
        if self.gate is not None:
            self.gate()
        return self.inner.run(**kwargs)


def two_more_beats(transport):
    """A gate that returns once TWO further beats for this job have been
    recorded.

    Two FURTHER, counted from the moment the gate is entered, because the
    beats before it are the claim's: :meth:`_Heartbeat.beat` fires once on
    entry and again every interval while the worker pulls and unpacks the
    input, all of them carrying ``state: "claiming"``. Waiting for "two beats
    in total" would therefore be satisfied before the job had started, which
    is the one thing the gate exists to rule out -- the worker's cooperative
    checkpoint can only act on a word the heartbeat thread has already read.
    """

    def gate():
        target = len(transport.beats_for(JOB)) + 2
        assert transport.wait_for_beats(JOB, target), "the worker stopped beating"

    return gate


def _queue_pdf(root: Path, pdf: Path, *, page_count: int | None = PAGES) -> dict:
    return protocol.queue_marker(
        root,
        job_id=JOB,
        stage="ocr",
        doc_id="DOC-ranges",
        expect={
            "stage": "ocr",
            "backend": "marker",
            "outputs": ["pages.json"],
            "input_name": "input.pdf",
            "page_count": page_count,
        },
        config_hash="cfg-hash",
        inputs=[("input.pdf", pdf.read_bytes())],
    )


@pytest.fixture(params=BOTH_CONVENTIONS, ids=lambda c: f"marker-numbers-{c}")
def job(tmp_path, request):
    """A queued ten-page OCR job, and a backend that will plan it into four
    ranges of three.

    Parametrised over both page-numbering conventions: what the worker does
    with a chunked job must not depend on which release produced the ranges,
    and the lane this hotfix repairs shipped a worker suite that only ever
    saw one of them."""
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * PAGES)
    script = write_stub_marker(tmp_path / "bin", total_pages=PAGES, numbering=request.param)
    _queue_pdf(root, pdf)
    backend = RealMarkerOcrBackend(
        marker_single_exe=str(script), timeout_s=60, max_range_pixels=BUDGET
    )
    return root, script, backend


def _run(transport, tmp_path, backend, **kwargs):
    return run_worker(
        transport=transport,
        backends=StubDevBackends(ocr=backend),
        work_root=tmp_path / "work",
        heartbeat_interval_s=FAST_BEAT,
        **kwargs,
    )


def _ranges_run(script) -> list[str]:
    return [row[row.index("--page_range") + 1] for row in stub_argv(script) if "--page_range" in row]


def _stop_after_one_range(tmp_path, root, backend):
    """Run the worker with a stop that is certainly in hand by the first
    range boundary, and return its summary."""
    transport = ControlTransport(
        root, word_for=lambda beat, j, payload: "stop" if j == JOB else "none"
    )
    held = HeldMarker(backend, gate=two_more_beats(transport))
    return _run(transport, tmp_path, held)


def _published(root) -> dict:
    return json.loads(
        (protocol.done_dir(root) / JOB / protocol.RESULT_FILENAME).read_text(encoding="utf-8")
    )


def _pages(root) -> list[dict]:
    return json.loads(
        (protocol.done_dir(root) / JOB / "pages.json").read_text(encoding="utf-8")
    )["pages"]


# ---------------------------------------------------------------------------
# a chunked job, start to finish
# ---------------------------------------------------------------------------


def test_a_chunked_job_publishes_absolute_pages_and_a_hashed_range_list(tmp_path, job):
    root, script, backend = job
    transport = ControlTransport(root)
    summary = _run(transport, tmp_path, backend)

    assert summary["published"] == [JOB]
    assert _ranges_run(script) == ["0-2", "3-5", "6-8", "9-9"]
    assert [p["page_number"] for p in _pages(root)] == list(range(PAGES))

    result = _published(root)
    assert result["page_count"] == PAGES
    assert [(r["first_page"], r["last_page"]) for r in result["ranges"]] == [
        (0, 2),
        (3, 5),
        (6, 8),
        (9, 9),
    ]
    assert all(len(r["sha256"]) == 64 for r in result["ranges"])


def test_the_heartbeat_counts_ranges_and_carries_the_plan(tmp_path, job):
    """"1 of 1 jobs" tells an operator watching a 540-page scan nothing at
    all. The card renders `unit + "s"`, so a unit of ``range`` is all the
    plumbing this needs -- and the settings block carries what the count
    cannot: how many pages a range is, and the knob that decided."""
    root, script, backend = job
    transport = ControlTransport(root)
    held = HeldMarker(backend, gate=two_more_beats(transport))
    _run(transport, tmp_path, held)

    beats = [p for p in transport.beats_for(JOB) if p]
    running = [p for p in beats if p.get("state") == "running"]
    assert running, "the job beat at least once while it ran"
    assert {p["unit"] for p in running} == {"range"}
    assert {p["units_total"] for p in running} == {4}
    assert max(p["units_done"] for p in running) > 0


def test_the_progress_payload_is_never_refused_by_the_queue(tmp_path, job):
    """Every beat of a chunked job carries its payload.

    The trap this closes (found by this suite): ``settings`` is an ALLOWLIST
    both halves of the protocol share, and the queue-host half is a POSIX-sh
    wrapper on another machine. A key this worker invents is refused on
    arrival, and the FIX V-6 fallback then beats BARE for the whole job --
    the card shows a live claim with no detail, and nothing fails. A new
    number on the card therefore costs a wrapper rollout; until one happens,
    the plan goes in the worker's own log."""
    root, script, backend = job
    transport = ControlTransport(root)
    held = HeldMarker(backend, gate=two_more_beats(transport))
    _run(transport, tmp_path, held)

    assert all(payload is not None for _job, payload in transport.beats)


def test_the_plan_is_in_the_workers_log(tmp_path, job):
    root, script, backend = job
    lines: list[str] = []
    _run(ControlTransport(root), tmp_path, backend, log=lines.append)
    planned = [line for line in lines if "ocr planned as" in line]
    assert len(planned) == 1
    assert "4 range(s) of up to 3 page(s) over 10 page(s)" in planned[0]
    # The DPI the count was derived FROM: page area goes as DPI squared, so
    # the same document planned against two beliefs about marker's render
    # size is two different plans, and an operator who thinks the plan is
    # too fine is looking for exactly this number.
    assert "at 192 dpi (bounded_dpi)" in planned[0]


def test_the_manifest_records_the_dpi_the_ranges_were_planned_at(tmp_path, job):
    root, script, backend = job
    _run(ControlTransport(root), tmp_path, backend)
    result = _published(root)
    assert result["planning_dpi"] == 192
    assert result["planning_dpi_source"] == "bounded_dpi"


def test_a_raised_render_dpi_is_visible_in_the_plan_and_the_log(tmp_path):
    """A program that passes marker a larger ``--highres_image_dpi`` gets a
    finer plan, and the log says which setting did it -- no second knob to
    fall out of step with the CLI argument beside it."""
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * PAGES)
    script = write_stub_marker(tmp_path / "bin", total_pages=PAGES)
    _queue_pdf(root, pdf)
    backend = RealMarkerOcrBackend(
        marker_single_exe=str(script),
        timeout_s=60,
        max_range_pixels=BUDGET,
        extra_args=["--highres_image_dpi", "384"],
    )
    lines: list[str] = []
    summary = _run(ControlTransport(root), tmp_path, backend, log=lines.append)

    assert summary["published"] == [JOB]
    # 384 doubles the DPI, so a page is 4x the pixels: 3 pages a range -> 1.
    assert _ranges_run(script) == [f"{i}-{i}" for i in range(PAGES)]
    planned = [line for line in lines if "ocr planned as" in line]
    assert "at 384 dpi (marker_extra_args --highres_image_dpi)" in planned[0]
    result = _published(root)
    assert result["planning_dpi"] == 384
    assert result["planning_dpi_source"] == "marker_extra_args --highres_image_dpi"


def test_the_manifest_and_the_log_say_which_numbering_was_used(tmp_path, job, request):
    """A page number means nothing without the rule that produced it, and
    that rule is a fact about the marker release on THIS machine -- so it
    goes in the result the other machine reads, and in the log an operator
    watching the run is already looking at."""
    root, script, backend = job
    expected = request.node.callspec.params["job"]
    lines: list[str] = []
    _run(ControlTransport(root), tmp_path, backend, log=lines.append)

    result = _published(root)
    assert result["page_range_numbering"] == expected
    assert result["page_range_numbering_decided_by"] == "range 3-5", (
        "the first range whose windows are disjoint is what decided it"
    )
    said = [line for line in lines if "ocr page numbering" in line]
    assert len(said) == 1
    assert expected in said[0] and "decided by range 3-5" in said[0]


# ---------------------------------------------------------------------------
# a stop on a boundary, and the resume that follows it
# ---------------------------------------------------------------------------


def test_a_stop_lands_on_a_range_boundary_and_returns_the_claim(tmp_path, job):
    """The C-0097 D2 shape, on the stage that had no pause point at all: the
    unit in flight finishes, the claim goes back through the existing return
    verb, and nothing half-finished is published."""
    root, script, backend = job
    transport = ControlTransport(root, word_for=lambda beat, j, payload: "stop" if j == JOB else "none")
    held = HeldMarker(backend, gate=two_more_beats(transport))
    summary = _run(transport, tmp_path, held)

    assert summary["stopped"] == [JOB]
    assert summary["published"] == []
    assert protocol.list_pending(root) == [JOB]
    assert protocol.list_claims(root) == []
    assert protocol.list_done(root) == []

    base = tmp_path / "work" / JOB
    assert (base / STOPPED_MARKER).is_file()
    assert not (base / "out" / "pages.json").exists(), (
        "a partial pages.json would be a publishable-looking file for a document "
        "whose later pages do not exist yet"
    )
    result = json.loads((base / "out" / protocol.RESULT_FILENAME).read_text(encoding="utf-8"))
    assert result["status"] == "stopped"
    assert result["unit"] == "range"
    assert result["units_total"] == 4
    assert 0 < result["units_done"] < 4
    assert len(_ranges_run(script)) == result["units_done"]


def test_the_ranges_a_stopped_job_produced_survive_the_sweep(tmp_path, job):
    """The stopped job's own directory is swept on the next run's first poll
    (FIX V-10), and the resume cache must not be swept with it: it lives
    beside the job directories precisely because the two have opposite
    lifetimes."""
    root, script, backend = job
    _stop_after_one_range(tmp_path, root, backend)

    cache = tmp_path / "work" / OCR_RANGE_CACHE_DIRNAME / JOB
    produced = sorted(p.name for p in cache.glob("range-*.md"))
    assert produced, "the finished ranges are on disk"
    assert len(produced) == len(_ranges_run(script))


def test_the_resume_re_runs_only_the_ranges_that_were_never_produced(tmp_path, job):
    """The point of the whole thing: a stop three ranges into a book does not
    cost those three ranges' GPU minutes when the job is claimed again."""
    root, script, backend = job
    _stop_after_one_range(tmp_path, root, backend)
    stopped_at = len(_ranges_run(script))
    assert 0 < stopped_at < 4

    summary = _run(ControlTransport(root), tmp_path, backend)
    assert summary["published"] == [JOB]
    # Every range ran exactly once ACROSS the two runs: the ones before the
    # boundary in the first, the ones after it in the second.
    assert _ranges_run(script) == ["0-2", "3-5", "6-8", "9-9"]
    assert [p["page_number"] for p in _pages(root)] == list(range(PAGES))
    assert [p["text"] for p in _pages(root)] == [f"body of page {i}" for i in range(PAGES)]


class StopAtTheLastBoundary:
    """A :class:`~trialerror.offload.worker.WorkerControl` stand-in whose
    stop arrives only at the FINAL range boundary.

    The real control word rides a heartbeat reply, so which boundary a stop
    lands on is a race this suite cannot aim; the checkpoint reads
    ``stopping`` and ``paused`` and nothing else, so a counter over the reads
    puts the word exactly where the claim needs it."""

    paused = False

    def __init__(self, stop_on_read: int):
        self.stop_on_read = stop_on_read
        self.reads = 0

    @property
    def stopping(self) -> bool:
        self.reads += 1
        return self.reads >= self.stop_on_read


def test_a_stop_on_the_last_range_publishes_the_finished_document(tmp_path, job):
    """FIX V-2. Every range has been produced, so the unit is COMPLETE: the
    document is written and the stop settles afterwards. Before the fix the
    backend raised at the last boundary, the job went back to `pending`
    having done all of its work, and the worker's own docstring said the
    opposite ("GPU minutes that have already been spent are never thrown
    away")."""
    root, script, backend = job
    from trialerror.offload import worker as W

    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "input.pdf").write_bytes((tmp_path / "doc.pdf").read_bytes())
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    control = StopAtTheLastBoundary(stop_on_read=4)
    outcome = W._run_ocr(
        backend,
        {"job_id": JOB, "expect": {"input_name": "input.pdf", "page_count": PAGES}},
        in_dir,
        out_dir,
        tmp_path / "scratch",
        control=control,
        range_cache=tmp_path / "cache" / JOB,
    )

    assert (outcome.complete, outcome.stopped) == (True, True)
    assert (outcome.units_done, outcome.units_total, outcome.unit) == (4, 4, "range")
    pages = json.loads((out_dir / "pages.json").read_text(encoding="utf-8"))["pages"]
    assert [p["page_number"] for p in pages] == list(range(PAGES))
    assert _ranges_run(script) == ["0-2", "3-5", "6-8", "9-9"]
    assert not (tmp_path / "cache" / JOB).exists(), "a published document's ranges are nobody's"


def test_a_stop_before_the_last_range_is_still_an_incomplete_unit(tmp_path, job):
    """The other side of the same boundary, so the fix cannot be read as
    "stops are ignored now": a stop with ranges still unproduced hands the
    claim back with nothing written."""
    root, script, backend = job
    from trialerror.offload import worker as W

    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "input.pdf").write_bytes((tmp_path / "doc.pdf").read_bytes())
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    outcome = W._run_ocr(
        backend,
        {"job_id": JOB, "expect": {"input_name": "input.pdf", "page_count": PAGES}},
        in_dir,
        out_dir,
        tmp_path / "scratch",
        control=StopAtTheLastBoundary(stop_on_read=2),
        range_cache=tmp_path / "cache" / JOB,
    )

    assert (outcome.complete, outcome.stopped) == (False, True)
    assert (outcome.units_done, outcome.units_total) == (2, 4)
    assert not (out_dir / "pages.json").exists()
    assert sorted(p.name for p in (tmp_path / "cache" / JOB).glob("range-*.md")) == [
        "range-000000-000002.md",
        "range-000003-000005.md",
    ]


def test_a_finished_document_leaves_no_cache_behind(tmp_path, job):
    """The cache is a resume, not an archive: once the document is published
    the ranges that built it are nobody's."""
    root, script, backend = job
    _run(ControlTransport(root), tmp_path, backend)
    assert not (tmp_path / "work" / OCR_RANGE_CACHE_DIRNAME / JOB).exists()


# ---------------------------------------------------------------------------
# the page-count check
# ---------------------------------------------------------------------------


def test_a_page_count_that_disagrees_with_the_manifest_fails_the_job(tmp_path):
    """The one check the two machines can make against each other without
    shipping the document back. Two different counts mean the bytes here are
    not the bytes the record is about, and every page number in the output
    would be an assertion about the wrong document."""
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * PAGES)
    script = write_stub_marker(tmp_path / "bin", total_pages=PAGES)
    _queue_pdf(root, pdf, page_count=PAGES + 5)
    backend = RealMarkerOcrBackend(
        marker_single_exe=str(script), timeout_s=60, max_range_pixels=BUDGET
    )

    summary = _run(ControlTransport(root), tmp_path, backend)
    assert summary["failed"] == [JOB]
    error = json.loads(
        (protocol.done_dir(root) / JOB / protocol.ERROR_FILENAME).read_text(encoding="utf-8")
    )
    assert "PageCountMismatchError" in error["error"]
    assert "15" in error["error"] and "10" in error["error"]


def test_an_absent_page_count_is_not_checked(tmp_path):
    """Absence is not disagreement: not every route records a page count, and
    a check against a missing number would refuse the documents it knows
    least about."""
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * PAGES)
    script = write_stub_marker(tmp_path / "bin", total_pages=PAGES)
    _queue_pdf(root, pdf, page_count=None)
    backend = RealMarkerOcrBackend(
        marker_single_exe=str(script), timeout_s=60, max_range_pixels=BUDGET
    )
    summary = _run(ControlTransport(root), tmp_path, backend)
    assert summary["published"] == [JOB]


def test_an_unchunked_document_still_runs_as_one_unit(tmp_path):
    """The other half of the control story: a document that fits one
    invocation is counted, checkpointed and published exactly as it was
    before this lane -- the unit is the job, because marker really does run
    once."""
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 3)
    script = write_stub_marker(tmp_path / "bin", total_pages=3)
    _queue_pdf(root, pdf, page_count=3)
    backend = RealMarkerOcrBackend(marker_single_exe=str(script), timeout_s=60)

    transport = ControlTransport(root)
    summary = _run(transport, tmp_path, backend)

    assert summary["published"] == [JOB]
    assert _ranges_run(script) == []
    running = [p for p in transport.beats_for(JOB) if p and p.get("state") == "running"]
    assert {p["unit"] for p in running} == {"job"}
    assert "ranges" not in _published(root)
    assert "page_range_numbering" not in _published(root), (
        "no range flag was passed, so there is no numbering convention to have chosen"
    )


# ---------------------------------------------------------------------------
# what each range cost (lane FB-8b item 2)
# ---------------------------------------------------------------------------


def test_each_range_is_logged_with_its_wall_time_and_peak(tmp_path, job):
    """The measurement an operator sizes `max_range_pixels` from.

    The raster arithmetic under-states a marker process by two orders of
    magnitude, so the guide's procedure is "run one small range and read the
    peak" -- and the machine that could read it by hand is a GPU box behind a
    queue. So the worker says it, per range, while the run is still going;
    the result manifest keeps the same numbers for afterwards."""
    root, _script, backend = job
    lines: list[str] = []
    _run(ControlTransport(root), tmp_path, backend, log=lines.append)

    costs = [line for line in lines if "ocr range" in line]
    assert len(costs) == 4, costs
    assert "ocr range 0-2 ran in " in costs[0]
    assert "ocr range 9-9 ran in " in costs[3]
    # POSIX: getrusage is free, so a peak is there -- with what it is a peak
    # OF, which is the part that stops somebody subtracting two of them.
    for line in costs:
        assert "peak " in line
        assert "max over this process's children so far" in line


def test_the_manifest_carries_each_ranges_cost_beside_its_hash(tmp_path, job):
    root, _script, backend = job
    _run(ControlTransport(root), tmp_path, backend)

    ranges = _published(root)["ranges"]
    assert len(ranges) == 4
    for entry in ranges:
        assert isinstance(entry["range_wall_s"], float)
        assert entry["cached"] is False
        assert isinstance(entry["peak_rss_bytes"], int) and entry["peak_rss_bytes"] > 0
        assert "getrusage" in entry["peak_rss_source"]


def test_a_resumed_range_is_logged_as_coming_from_the_cache(tmp_path, job):
    """A resume's ranges come back in milliseconds. The log says "from
    cache" rather than reporting a marker invocation that never happened."""
    root, _script, backend = job
    _stop_after_one_range(tmp_path, root, backend)

    lines: list[str] = []
    _run(ControlTransport(root), tmp_path, backend, log=lines.append)
    costs = [line for line in lines if "ocr range" in line]
    assert costs, "the second claim logged no ranges at all"
    assert any("from cache" in line for line in costs)
    assert any("peak not measured" in line for line in costs), (
        "no child ran for a cached range, so there is no peak to report"
    )
