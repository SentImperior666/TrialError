"""Page-range chunked OCR, at the backend seam (lane e1e Part B).

Every test here drives the REAL :class:`trialerror.ingest.backends.
RealMarkerOcrBackend` through the real ``subprocess.run``, against a fake
``marker_single`` that records its argv (``tests/_ocr_range_fixtures.py``).
The subject is a command line and a concatenation, and neither can be
measured through a stub that replaces the thing issuing them.

The claims, in the order the brief states them:

1. **range planning** on synthetic page boxes -- portrait, landscape, mixed
   -- never exceeds the pixel bound, and plans against the LARGEST page;
2. **page numbering** across ranges: the document's pages come out under
   absolute numbers whichever convention the release used to say so;
3. **resume** after a stop at a boundary re-runs only the ranges that were
   never produced;
4. the **flag-absent** refusal;
5. the **numbering** refusals -- a marker no convention can place, a
   document that disagrees with itself, a forced convention the output does
   not obey, and a concatenation that is not strictly increasing;
6. **byte-identical output** against the unchunked path for a small
   document, which is what makes the chunked path auditable against the
   behaviour it replaces.

**Both conventions, everywhere.** Which page a ``{N}`` marker names under
``--page_range`` is a fact about the marker RELEASE -- marker-pdf 1.10.x
numbers absolutely, other releases number from the start of the invocation
-- and a suite that exercises only one of them is a suite that passes
against an assumption. So the stub's numbering is a fixture parameter and
every test whose claim depends on a page number runs twice. The stub's
default is ``absolute``, because that is what the supported release does and
therefore the case a regression appears in first.

No GPU is involved and none is skipped for: the stub is the only marker
here, and the one thing it cannot tell us -- the real flag's spelling in the
installed release -- is exactly what the ``--help`` probe exists to find out
on the machine that has it.
"""

from __future__ import annotations

import hashlib
import sys

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import (
    DEFAULT_BOUNDED_DPI,
    DEFAULT_MAX_RANGE_PIXELS,
    OcrRangesIncomplete,
    PAGE_RANGE_NUMBERING_ABSOLUTE,
    PAGE_RANGE_NUMBERING_RELATIVE,
    PageRange,
    RANGE_PIXEL_SAFETY,
    RealMarkerOcrBackend,
    page_boxes,
    plan_page_ranges,
)
from trialerror.ingest.errors import PageRangeFlagMissingError, PageRangeNumberingError
from trialerror.util.atomic import atomic_write_bytes

from tests._ocr_range_fixtures import (
    A4_LANDSCAPE_PT,
    A4_PT,
    BOTH_CONVENTIONS,
    POSTER_PT,
    make_pdf,
    stub_argv,
    write_stub_marker,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the fake marker_single is a shebang script; the real backend is GPU-gated anyway",
)


def _pixels(size: tuple[float, float], dpi: float) -> float:
    return (size[0] / 72.0 * dpi) * (size[1] / 72.0 * dpi)


def _budget_for(pages: int, size: tuple[float, float] = A4_PT) -> float:
    """A ``max_range_pixels`` that plans exactly ``pages`` pages per range.

    Computed at :data:`DEFAULT_BOUNDED_DPI`, because that is the DPI the
    backend plans at -- these fixtures used to work the budget out at 96,
    which was the planning default and is now marker's LOW-res pass, and a
    budget worked out at the wrong resolution sizes a range by accident.
    """
    return _pixels(size, DEFAULT_BOUNDED_DPI) * pages * RANGE_PIXEL_SAFETY


def _backend(script, **kwargs):
    return RealMarkerOcrBackend(marker_single_exe=str(script), timeout_s=60, **kwargs)


def _ranges_of(argv_rows, flag="--page_range"):
    out = []
    for row in argv_rows:
        if flag in row:
            out.append(row[row.index(flag) + 1])
        else:
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# 1. planning
# ---------------------------------------------------------------------------


def test_the_plan_never_exceeds_the_pixel_bound_on_a4():
    """The bound is the whole point, so it is asserted as the bound: every
    range's pages times the largest page's area, times the safety factor,
    fits the budget."""
    boxes = [A4_PT] * 200
    budget = 20_000_000
    plan = plan_page_ranges(boxes, dpi=96, max_range_pixels=budget)
    per_page = _pixels(A4_PT, 96)
    for page_range in plan:
        assert page_range.count * per_page * RANGE_PIXEL_SAFETY <= budget
    assert sum(r.count for r in plan) == 200
    assert plan[0].first == 0 and plan[-1].last == 199


def test_a_landscape_page_plans_the_same_as_its_portrait(tmp_path):
    """Area, not width. A planner that read one dimension would size the two
    differently for the same number of pixels."""
    portrait = plan_page_ranges([A4_PT] * 50, dpi=96, max_range_pixels=20_000_000)
    landscape = plan_page_ranges([A4_LANDSCAPE_PT] * 50, dpi=96, max_range_pixels=20_000_000)
    assert portrait == landscape


def test_a_mixed_document_is_planned_against_its_largest_page():
    """The failure being bounded is a PEAK -- a run holds every page image it
    has built -- so three fold-out plates in a book of A4 decide the range
    length for the whole book."""
    boxes = [A4_PT] * 100 + [POSTER_PT] * 3
    budget = 20_000_000
    plan = plan_page_ranges(boxes, dpi=96, max_range_pixels=budget)
    poster_pages = int(budget // (_pixels(POSTER_PT, 96) * RANGE_PIXEL_SAFETY))
    assert plan[0].count == poster_pages
    for page_range in plan:
        assert page_range.count * _pixels(POSTER_PT, 96) * RANGE_PIXEL_SAFETY <= budget


def test_the_documented_default_puts_a_540_page_a4_scan_in_ranges_of_16():
    """The arithmetic the constant's docstring states, asserted so the
    sentence and the number cannot drift apart.

    At the DEFAULT planning DPI -- 192, marker's own high-res render pass --
    A4 is 1586.7 x 2245.3 = 3,562,596 px, and 64,000,000 / (3,562,596 x 1.1)
    is 16 pages per range, so 540 pages are 33 ranges of 16 (528) and a 34th
    of 12.

    This test read 65 pages per range until the planning DPI was corrected:
    that was the same budget against a page area computed at 96 DPI, which
    is marker's LOW-res pass. Area goes as DPI squared, so the old plan was
    four times too generous in pixels and the bound was not a bound -- the
    run that proved it died with a MemoryError inside marker's own
    rasteriser."""
    plan = plan_page_ranges(
        [A4_PT] * 540, dpi=DEFAULT_BOUNDED_DPI, max_range_pixels=DEFAULT_MAX_RANGE_PIXELS
    )
    assert DEFAULT_BOUNDED_DPI == 192, "the default this arithmetic is stated at"
    assert plan[0].count == 16
    assert len(plan) == 34
    assert plan[-1] == PageRange(528, 539)
    # FIX V-4: the DECOMPOSITION, not only the two endpoints -- the shipped
    # prose said "nine ranges of 65 and one of 45" (630 pages) beside this
    # green test for exactly as long as nothing here pinned the shape.
    assert [r.count for r in plan] == [16] * 33 + [12]
    # The default really is what the planner uses when nobody says.
    assert plan_page_ranges([A4_PT] * 540, max_range_pixels=DEFAULT_MAX_RANGE_PIXELS) == plan


def test_the_old_96_dpi_plan_was_four_times_too_generous():
    """The defect, stated as arithmetic rather than as a story: the same
    document and the same budget, planned against marker's low-res pass and
    against its high-res one. Four times the pixels per page is four times
    fewer pages per range, and the difference is the memory the bound was
    supposed to be holding."""
    at_low = plan_page_ranges([A4_PT] * 540, dpi=96, max_range_pixels=DEFAULT_MAX_RANGE_PIXELS)
    at_high = plan_page_ranges(
        [A4_PT] * 540, dpi=192, max_range_pixels=DEFAULT_MAX_RANGE_PIXELS
    )
    assert (at_low[0].count, at_high[0].count) == (65, 16)
    assert _pixels(A4_PT, 192) == pytest.approx(_pixels(A4_PT, 96) * 4)


def test_the_planning_dpi_is_markers_high_res_pass_by_default(tmp_path):
    """marker renders EVERY page of a range at ``highres_image_dpi`` before
    it recognises any of them (marker 1.x default 192; the low-res pass is
    96). The planner's belief about page size has to be the larger one or
    the bound is not bounding what fails."""
    script = write_stub_marker(tmp_path / "bin", total_pages=3)
    backend = _backend(script)
    assert backend.planning_dpi == 192
    assert backend.planning_dpi_source == "bounded_dpi"


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--highres_image_dpi", "384"],
        ["--highres_image_dpi=384"],
        ["--force_ocr", "--highres_image_dpi", "384", "--disable_image_extraction"],
    ],
    ids=["separate", "equals", "among-others"],
)
def test_a_highres_dpi_in_extra_args_raises_the_planning_dpi(tmp_path, extra_args):
    """No second knob: a program that passes marker its render DPI has
    already stated the number once, and a harness key that could disagree
    with the CLI argument beside it would be a new way to get the plan
    wrong. Both spellings, because both are things people write."""
    script = write_stub_marker(tmp_path / "bin", total_pages=3)
    backend = _backend(script, extra_args=extra_args)
    assert backend.planning_dpi == 384
    assert backend.planning_dpi_source == "marker_extra_args --highres_image_dpi"


def test_a_highres_dpi_in_extra_args_never_lowers_the_planning_dpi(tmp_path):
    """Raised only. Rendering LOWER than the planner assumes costs
    invocations; rendering HIGHER than it assumes is the memory failure the
    bound exists to prevent, so the asymmetry is deliberate."""
    script = write_stub_marker(tmp_path / "bin", total_pages=3)
    backend = _backend(script, extra_args=["--highres_image_dpi", "96"])
    assert backend.planning_dpi == 192
    assert backend.planning_dpi_source == "bounded_dpi"
    # …and a junk value is not a refusal: marker's own parser owns marker's
    # own arguments, and the planner keeps the DPI it can defend.
    assert _backend(script, extra_args=["--highres_image_dpi", "wide"]).planning_dpi == 192


def test_the_raised_dpi_really_reaches_the_plan(tmp_path):
    """Not just an attribute: four times the pixels per page is four times
    fewer pages per invocation, on the plan the worker drives."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 12)
    script = write_stub_marker(tmp_path / "bin", total_pages=12)
    budget = _budget_for(4)
    assert [r.count for r in _backend(script, max_range_pixels=budget).plan_ranges(pdf)] == [
        4,
        4,
        4,
    ]
    doubled = _backend(
        script, max_range_pixels=budget, extra_args=["--highres_image_dpi", "384"]
    )
    # 384 is 2x the planning default, so a page is 4x the pixels and a
    # quarter as many fit: 4 -> 1.
    assert [r.count for r in doubled.plan_ranges(pdf)] == [1] * 12


def test_the_result_records_the_dpi_its_ranges_were_planned_at(tmp_path):
    """A range count means nothing without the page area it came from, and
    that area is a DPI squared. The number goes in the result so the machine
    that reads it does not have to guess which belief produced the plan."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10)
    result = _backend(script, max_range_pixels=_budget_for(3)).run(
        input_path=pdf, work_dir=tmp_path / "work"
    )
    assert (result.planning_dpi, result.planning_dpi_source) == (192, "bounded_dpi")

    raised = _backend(
        script, max_range_pixels=_budget_for(3), extra_args=["--highres_image_dpi=384"]
    ).run(input_path=pdf, work_dir=tmp_path / "work-2")
    assert raised.planning_dpi == 384
    assert raised.planning_dpi_source == "marker_extra_args --highres_image_dpi"


def test_a_budget_of_zero_is_no_bound():
    """`max_range_pixels = 0` is how an operator turns the whole thing off,
    and it has to mean ONE range rather than an empty plan."""
    assert plan_page_ranges([A4_PT] * 300, dpi=96, max_range_pixels=0) == [PageRange(0, 299)]


def test_a_budget_under_one_page_still_plans_one_page_per_range():
    """There is no plan smaller than a page. A bound that cannot be honoured
    is honoured as far as it can be, rather than turning into an empty plan
    or a division by zero."""
    plan = plan_page_ranges([A4_PT] * 4, dpi=96, max_range_pixels=10)
    assert [r.count for r in plan] == [1, 1, 1, 1]


def test_an_empty_document_plans_nothing():
    assert plan_page_ranges([], dpi=96, max_range_pixels=DEFAULT_MAX_RANGE_PIXELS) == []


def test_the_page_boxes_come_from_the_real_page_tree(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT, POSTER_PT, A4_LANDSCAPE_PT])
    assert page_boxes(pdf) == [A4_PT, POSTER_PT, A4_LANDSCAPE_PT]


def test_a_file_with_no_page_tree_cannot_be_chunked(tmp_path):
    """Not a failure and not a silent fallback to something weaker: an input
    with no pages has nothing to range over, so the one invocation this
    backend has always made is the whole of the work."""
    not_a_pdf = tmp_path / "input.txt"
    not_a_pdf.write_text("this is not a pdf", encoding="utf-8")
    script = write_stub_marker(tmp_path / "bin", total_pages=1)
    assert _backend(script).plan_ranges(not_a_pdf) is None


# ---------------------------------------------------------------------------
# 2. the invocations, and absolute numbering across them
# ---------------------------------------------------------------------------


@pytest.fixture(params=BOTH_CONVENTIONS, ids=lambda c: f"marker-numbers-{c}")
def ten_page_job(tmp_path, request):
    """A ten-page A4 document and a stub marker, with a budget that plans it
    into ranges of three.

    **Parametrised over both numbering conventions**, so every test that
    takes this fixture runs against a release that numbers a range's pages
    absolutely AND one that numbers them from the start of the invocation.
    The lane this hotfix repairs had a fixture that only ever did the
    latter, which is how eight real claims were refused by a green suite.
    """
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10, numbering=request.param)
    budget = _budget_for(3)
    return pdf, script, budget


def test_one_invocation_per_range_each_carrying_the_flag(tmp_path, ten_page_job):
    pdf, script, budget = ten_page_job
    backend = _backend(script, max_range_pixels=budget)
    backend.run(input_path=pdf, work_dir=tmp_path / "work")

    rows = stub_argv(script)
    assert len(rows) == 4
    assert _ranges_of(rows) == ["0-2", "3-5", "6-8", "9-9"]
    for row in rows:
        assert "--paginate_output" in row
        assert "--disable_tqdm" in row
        assert "--disable_multiprocessing" in row


def test_the_pages_carry_absolute_numbers_across_ranges(tmp_path, ten_page_job):
    """Whichever convention the release used, the DOCUMENT's pages come out
    numbered absolutely: a release counting from the start of the invocation
    returns 0,1,2 for range 3-5 and they are read as 3,4,5; one counting
    absolutely returns 3,4,5 and they are left alone. The body text is the
    document's own page number either way, so a wrong offset is visible as a
    page whose number and content disagree."""
    pdf, script, budget = ten_page_job
    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work"
    )
    assert [p.page_number for p in result.pages] == list(range(10))
    assert [p.text for p in result.pages] == [f"body of page {i}" for i in range(10)]
    assert result.page_count == 10


def test_the_result_carries_one_hashed_entry_per_range(tmp_path, ten_page_job):
    """"Hash them into the result manifest": what makes a resumed document's
    output reconcilable range by range rather than only in total."""
    pdf, script, budget = ten_page_job
    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work"
    )
    assert [(r["first_page"], r["last_page"]) for r in result.ranges] == [
        (0, 2),
        (3, 5),
        (6, 8),
        (9, 9),
    ]
    for entry in result.ranges:
        assert len(entry["sha256"]) == 64
    assert len({e["sha256"] for e in result.ranges}) == 4


def test_extra_args_still_ride_after_the_range_flag(tmp_path, ten_page_job):
    pdf, script, budget = ten_page_job
    backend = _backend(script, max_range_pixels=budget, extra_args=["--custom", "1"])
    backend.run(input_path=pdf, work_dir=tmp_path / "work")
    row = stub_argv(script)[0]
    assert row[-2:] == ["--custom", "1"]


# ---------------------------------------------------------------------------
# 6. byte-identical against the unchunked path
# ---------------------------------------------------------------------------


def test_a_small_document_runs_the_unchunked_command_byte_for_byte(tmp_path):
    """A document that plans to one range is the command this backend always
    sent -- no range flag, no probe, nothing to go wrong for a document that
    was never going to need any of it."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 3)
    script = write_stub_marker(tmp_path / "bin", total_pages=3)
    result = _backend(script).run(input_path=pdf, work_dir=tmp_path / "work")

    rows = stub_argv(script)
    assert len(rows) == 1
    assert "--page_range" not in rows[0]
    assert [p.page_number for p in result.pages] == [0, 1, 2]
    assert result.ranges == ()


@pytest.mark.parametrize("numbering", BOTH_CONVENTIONS)
def test_the_chunked_and_unchunked_outputs_agree_page_for_page(tmp_path, numbering):
    """The same document through both paths: one invocation, and four. The
    pages that come out are the same pages with the same numbers and the same
    text -- which is the claim that makes `max_range_pixels = 0` a real
    escape hatch rather than a different feature.

    Under both conventions, because "the chunked path is the unchunked path"
    is exactly the claim a numbering rule can break."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    unchunked_bin = write_stub_marker(tmp_path / "bin-a", total_pages=10, numbering=numbering)
    chunked_bin = write_stub_marker(tmp_path / "bin-b", total_pages=10, numbering=numbering)

    whole = _backend(unchunked_bin, max_range_pixels=0).run(
        input_path=pdf, work_dir=tmp_path / "work-a"
    )
    budget = _budget_for(3)
    chunked = _backend(chunked_bin, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work-b"
    )

    assert [(p.page_number, p.text) for p in whole.pages] == [
        (p.page_number, p.text) for p in chunked.pages
    ]
    assert len(stub_argv(unchunked_bin)) == 1
    assert len(stub_argv(chunked_bin)) == 4


def test_a_range_with_no_page_marker_is_numbered_as_its_own_first_page(tmp_path):
    """FIX V-6. A range whose markdown is non-empty but carries no ``{N}``
    marker at all falls back to a single page. Within a range that page is
    the range's FIRST page: the unchunked path's "page 1" plus the offset
    put it one too high, which is a silently wrong page number -- or, when it
    lands on the next range's first page, a numbering refusal that names a
    cause ("two ranges produced the same page") that did not happen."""
    split = RealMarkerOcrBackend._split_pages
    assert [(p.page_number, p.text) for p in split("body\n", page_range=PageRange(6, 8))] == [
        (6, "body")
    ]
    assert [(p.page_number, p.text) for p in split("body\n")] == [(1, "body")], (
        "the unchunked path is unchanged: an unpaginated document is page 1"
    )
    # The collision the old numbering produced, now absent: an unpaginated
    # first range followed by a properly paginated second one.
    pages = split("lonely body\n", page_range=PageRange(0, 0))
    pages += split("{0}" + "-" * 48 + "\nnext\n", page_range=PageRange(1, 1))
    assert [p.page_number for p in pages] == [0, 1]
    backends._assert_monotonic(pages)


# ---------------------------------------------------------------------------
# 4/5. the refusals
# ---------------------------------------------------------------------------


def test_a_marker_without_the_flag_is_refused_by_name(tmp_path):
    """A range flag that is silently ignored turns each of N ranges into a
    full-document run -- the memory failure this exists to avoid, N times
    over. So the absence is probed for and named, never tried."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10, supports_flag=False)
    budget = _budget_for(3)
    with pytest.raises(PageRangeFlagMissingError) as excinfo:
        _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w")
    assert "--page_range" in str(excinfo.value)
    assert "max_range_pixels = 0" in str(excinfo.value)
    assert stub_argv(script) == [], "nothing was run: the probe refused first"


@pytest.mark.parametrize("numbering", BOTH_CONVENTIONS)
def test_a_flag_that_is_only_a_prefix_of_the_advertised_one_is_refused(tmp_path, numbering):
    """Minor note 3 from the verification. The probe exists to catch a flag
    the installed release does not have; a release spelling it
    `--page_ranges` satisfied a SUBSTRING test for `--page_range` and would
    then have ignored the argument -- the exact failure the probe is for,
    arriving through the check meant to prevent it. Matched as a word."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(
        tmp_path / "bin", total_pages=10, flag="--page_ranges", numbering=numbering
    )
    budget = _budget_for(3)
    with pytest.raises(PageRangeFlagMissingError):
        _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w")
    assert stub_argv(script) == []
    # …and configured with the spelling this release really advertises, it runs.
    result = _backend(
        script, max_range_pixels=budget, page_range_flag="--page_ranges"
    ).run(input_path=pdf, work_dir=tmp_path / "w2")
    assert [p.page_number for p in result.pages] == list(range(10))


def test_a_small_document_is_never_refused_over_the_flag(tmp_path):
    """The other half of the same rule: a document that plans to one range
    does not probe at all, so a marker release without the flag still OCRs
    everything that fits one invocation."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 3)
    script = write_stub_marker(tmp_path / "bin", total_pages=3, supports_flag=False)
    result = _backend(script).run(input_path=pdf, work_dir=tmp_path / "work")
    assert len(result.pages) == 3


@pytest.mark.parametrize("numbering", BOTH_CONVENTIONS)
def test_the_flag_spelling_is_configurable(tmp_path, numbering):
    """It is a fact about somebody else's CLI. A release that renames it is a
    one-line config change on the machine that has that release."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 6)
    script = write_stub_marker(
        tmp_path / "bin", total_pages=6, flag="--pages", numbering=numbering
    )
    budget = _budget_for(3)
    backend = _backend(script, max_range_pixels=budget, page_range_flag="--pages")
    result = backend.run(input_path=pdf, work_dir=tmp_path / "work")
    assert _ranges_of(stub_argv(script), flag="--pages") == ["0-2", "3-5"]
    assert [p.page_number for p in result.pages] == list(range(6))


def test_the_help_probe_runs_once_for_the_whole_document(tmp_path, ten_page_job):
    """Four ranges, one probe. The backend instance is the worker's, held for
    the whole run."""
    pdf, script, budget = ten_page_job
    backend = _backend(script, max_range_pixels=budget)
    backend.run(input_path=pdf, work_dir=tmp_path / "work-1")
    assert backend._page_range_flag_seen is True
    backend.run(input_path=pdf, work_dir=tmp_path / "work-2")
    # The stub records nothing for --help, so the count of recorded
    # invocations is the count of REAL runs: eight, not eight plus probes.
    assert len(stub_argv(script)) == 8


@pytest.mark.parametrize("numbering", BOTH_CONVENTIONS)
def test_a_range_that_comes_back_out_of_order_is_refused(tmp_path, numbering):
    """The concatenation must be strictly increasing. A gap is legal (a blank
    page produces no body and is dropped, as the unchunked path has always
    done); an overlap or a reversal is not.

    Under both conventions, because the monotonic check runs AFTER numbering
    now and a mode resolved from reversed output must not swallow it."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(
        tmp_path / "bin", total_pages=10, reversed_pages=True, numbering=numbering
    )
    budget = _budget_for(3)
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w")
    assert "strictly increasing" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5b. which convention the release uses -- resolved once, from evidence
#
# The defect this hotfix repairs: marker-pdf 1.10.2 numbers a range's `{N}`
# markers ABSOLUTELY under `--page_range`, and the backend assumed the other
# convention and refused every range but the first. `auto` now decides per
# document from the output itself, and refuses rather than guesses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "numbering, expected",
    [
        (PAGE_RANGE_NUMBERING_ABSOLUTE, PAGE_RANGE_NUMBERING_ABSOLUTE),
        (PAGE_RANGE_NUMBERING_RELATIVE, PAGE_RANGE_NUMBERING_RELATIVE),
    ],
)
def test_auto_decides_the_convention_on_the_second_range(tmp_path, numbering, expected):
    """Range 0-2's windows are the SAME window (0..2 either way), so it never
    votes. Range 3-5's are disjoint -- 0,1,2 can only be invocation-relative
    and 3,4,5 can only be absolute -- so the second range is where the
    document says which release produced it, and the whole run is numbered
    on that evidence."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10, numbering=numbering)
    budget = _budget_for(3)
    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work"
    )
    assert result.page_range_numbering == expected
    assert result.page_range_numbering_decided_by == "range 3-5"
    assert [p.page_number for p in result.pages] == list(range(10))
    assert [p.text for p in result.pages] == [f"body of page {i}" for i in range(10)]


def test_an_undecided_range_takes_the_mode_a_later_range_establishes(tmp_path):
    """The overlap case, and the reason the mode is resolved over the whole
    run rather than range by range.

    Range 2-9 is eight pages starting at page 2, so its windows OVERLAP
    (0..7 relative, 2..9 absolute): with its last two pages blank -- this
    release emits nothing at all for those -- every marker it does emit sits
    in 2..7 and fits both readings. Nothing in that range can decide it. The
    range AFTER it can, and does, and carries it."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 13)
    script = write_stub_marker(
        tmp_path / "bin",
        total_pages=13,
        numbering=PAGE_RANGE_NUMBERING_ABSOLUTE,
        omit_pages=(8, 9),
    )
    plan = [PageRange(0, 1), PageRange(2, 9), PageRange(10, 12)]
    result = _backend(script).run(input_path=pdf, work_dir=tmp_path / "work", ranges=plan)

    assert result.page_range_numbering == PAGE_RANGE_NUMBERING_ABSOLUTE
    assert result.page_range_numbering_decided_by == "range 10-12", (
        "the range that decided is named, and it is a LATER one than the range it settles"
    )
    # 8 and 9 are the blank pages: a gap is legal and always has been.
    assert [p.page_number for p in result.pages] == [0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12]


def test_a_document_no_range_of_which_can_decide_is_refused_by_name(tmp_path):
    """The other side of the overlap: if NOTHING decides, nothing is picked.

    Same hand-supplied plan, without the range that settled it. Every range
    after the first fits both readings, so the document cannot say which
    release made it -- and a harness that chose here would be numbering a
    book by coin flip. Refused, naming the key that settles it."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(
        tmp_path / "bin",
        total_pages=10,
        numbering=PAGE_RANGE_NUMBERING_ABSOLUTE,
        omit_pages=(8, 9),
    )
    plan = [PageRange(0, 1), PageRange(2, 9)]
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(script).run(input_path=pdf, work_dir=tmp_path / "work", ranges=plan)
    message = str(excinfo.value)
    assert "2-9" in message
    assert "page_range_numbering" in message
    assert "equal-sized ranges" in message, "and why the planner's own plans cannot land here"

    # …and the config key really does settle it.
    result = _backend(script, page_range_numbering=PAGE_RANGE_NUMBERING_ABSOLUTE).run(
        input_path=pdf, work_dir=tmp_path / "work-2", ranges=plan
    )
    assert [p.page_number for p in result.pages] == [0, 1, 2, 3, 4, 5, 6, 7]


def test_two_ranges_under_different_conventions_are_refused(tmp_path):
    """A document cannot be concatenated out of two conventions: the same
    page would be claimed twice or claimed by nobody. One range numbering
    relatively and the next absolutely is not a case to arbitrate."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(
        tmp_path / "bin",
        total_pages=10,
        numbering=PAGE_RANGE_NUMBERING_RELATIVE,
        numbering_by_range={"6-8": PAGE_RANGE_NUMBERING_ABSOLUTE},
    )
    budget = _budget_for(3)
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w")
    message = str(excinfo.value)
    assert "range 3-5" in message and "range 6-8" in message
    assert "relative" in message and "absolute" in message


def test_a_marker_no_convention_can_place_is_refused(tmp_path):
    """Outside BOTH windows is not a convention this backend has not heard
    of -- it is output that cannot be placed in the document at all, and no
    setting rescues it. The message says so rather than recommending one."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10, marker_offset=100)
    budget = _budget_for(3)
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w")
    message = str(excinfo.value)
    assert "neither convention can explain" in message
    assert "{100}" in message


def test_a_one_based_range_proves_both_conventions_at_once_and_is_refused(tmp_path):
    """The case the lane's own verification flagged and could not reach: a
    release numbering a range's pages from 1.

    Range 3-5's markers would then be 1,2,3 -- 1 and 2 can only be
    invocation-relative, 3 can only be absolute -- so one invocation proves
    BOTH conventions, which no release can be doing. The stated rule reads a
    range's markers as voting together and does not name this case, so it is
    refused by name (the nearest thing the rule does say: a disagreement is
    never arbitrated) rather than resolved by a tiebreak nobody wrote down.
    A whole document under that release refuses even sooner -- its first
    range's last marker is already outside both windows."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(
        tmp_path / "bin",
        total_pages=10,
        numbering=PAGE_RANGE_NUMBERING_ABSOLUTE,
        numbering_by_range={"3-5": "one_based"},
    )
    budget = _budget_for(3)
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w")
    message = str(excinfo.value)
    assert "BOTH conventions at once" in message
    assert "range 3-5" in message
    assert "from 1 rather than 0" in message

    # And the whole-document case: refused too, one range earlier.
    whole = write_stub_marker(tmp_path / "bin2", total_pages=10, numbering="one_based")
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(whole, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w2")
    assert "neither convention can explain" in str(excinfo.value)


@pytest.mark.parametrize(
    "forced, produced",
    [
        (PAGE_RANGE_NUMBERING_ABSOLUTE, PAGE_RANGE_NUMBERING_RELATIVE),
        (PAGE_RANGE_NUMBERING_RELATIVE, PAGE_RANGE_NUMBERING_ABSOLUTE),
    ],
)
def test_a_forced_convention_refuses_the_other_ones_output(tmp_path, forced, produced):
    """A program that states the convention gets it enforced, not merely
    preferred: output under the other one is refused with the mode in force,
    the window it requires, the marker that broke it and the key that moves
    it."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10, numbering=produced)
    budget = _budget_for(3)
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(script, max_range_pixels=budget, page_range_numbering=forced).run(
            input_path=pdf, work_dir=tmp_path / "w"
        )
    message = str(excinfo.value)
    assert f"page_range_numbering = '{forced}'" in message
    assert "range 3-5" in message
    window = "0..2" if forced == PAGE_RANGE_NUMBERING_RELATIVE else "3..5"
    assert window in message, "the window the forced mode requires"
    assert repr(produced) in message, "and the setting that would accept this output"


@pytest.mark.parametrize("numbering", BOTH_CONVENTIONS)
def test_a_forced_convention_that_matches_the_release_just_works(tmp_path, numbering):
    """The other half: naming the convention your release really uses skips
    the resolution entirely, and the manifest says the config decided."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10, numbering=numbering)
    budget = _budget_for(3)
    result = _backend(script, max_range_pixels=budget, page_range_numbering=numbering).run(
        input_path=pdf, work_dir=tmp_path / "work"
    )
    assert (result.page_range_numbering, result.page_range_numbering_decided_by) == (
        numbering,
        "config",
    )
    assert [p.page_number for p in result.pages] == list(range(10))


def test_an_unknown_numbering_mode_is_refused_at_construction(tmp_path):
    """A typo in the key is not a silent fallback to a default: it names the
    key and the three values."""
    script = write_stub_marker(tmp_path / "bin", total_pages=3)
    with pytest.raises(ValueError) as excinfo:
        _backend(script, page_range_numbering="ABSOLUTE")
    assert "page_range_numbering" in str(excinfo.value)
    assert "'auto'" in str(excinfo.value)


def test_a_range_that_fails_fails_the_job(tmp_path):
    """No partial concatenation is returned for a document one of whose
    ranges never ran: a missing hundred pages is invisible to everything
    downstream."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    script = write_stub_marker(tmp_path / "bin", total_pages=10, fail_on=(3, 5))
    budget = _budget_for(3)
    with pytest.raises(RuntimeError, match="exited 3"):
        _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "w")


# ---------------------------------------------------------------------------
# 3. stopping on a boundary, and resuming from the cache
# ---------------------------------------------------------------------------


def test_a_stop_on_a_boundary_raises_with_the_counts(tmp_path, ten_page_job):
    pdf, script, budget = ten_page_job
    backend = _backend(script, max_range_pixels=budget)
    with pytest.raises(OcrRangesIncomplete) as excinfo:
        backend.run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=tmp_path / "cache",
        )
    assert (excinfo.value.ranges_done, excinfo.value.ranges_total) == (2, 4)
    assert len(stub_argv(script)) == 2, "the ranges after the boundary never ran"


def test_a_stop_on_the_last_range_still_returns_the_document(tmp_path, ten_page_job):
    """FIX V-2. The checkpoint runs after every range, the last one
    included, and a stop that arrives there is not an incomplete unit: every
    range has been produced. Raising would hand the claim back on a finished
    document and make the next worker re-read the cache to learn that. The
    stop is not lost -- the caller checkpoints again once the pages are
    written, which is where a finished unit's stop belongs."""
    pdf, script, budget = ten_page_job
    seen: list[tuple[int, int]] = []

    def stop_at_the_end(done, total):
        seen.append((done, total))
        return "stop" if done == total else "go"

    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf,
        work_dir=tmp_path / "work",
        on_range=stop_at_the_end,
        cache_dir=tmp_path / "cache",
    )
    assert seen[-1] == (4, 4), "the last range was reported like every other one"
    assert [p.page_number for p in result.pages] == list(range(10))
    assert len(result.ranges) == 4


def test_a_resume_re_runs_only_the_ranges_that_were_never_produced(tmp_path, ten_page_job):
    """The whole point of the cache: a stop two ranges into a book does not
    cost those two ranges' GPU minutes when the job is claimed again."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"
    backend = _backend(script, max_range_pixels=budget)
    with pytest.raises(OcrRangesIncomplete):
        backend.run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5"]

    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5", "6-8", "9-9"]
    assert [p.page_number for p in result.pages] == list(range(10))
    assert [p.text for p in result.pages] == [f"body of page {i}" for i in range(10)]


@pytest.mark.parametrize("numbering", BOTH_CONVENTIONS)
def test_a_resumed_document_is_exactly_the_single_pass_document(tmp_path, numbering):
    """The claim a resume has to be able to make, under the convention the
    supported release really uses.

    A run killed at a range boundary, resumed from the cache, must produce
    the SAME pages -- numbers and bodies -- as the same document run in one
    invocation. The cache stores each range's RAW marker text, so numbering
    is re-derived over every range at concatenation time; a resume can
    therefore never mix a convention it inherited with one it just read."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    chunked_bin = write_stub_marker(tmp_path / "bin", total_pages=10, numbering=numbering)
    single_bin = write_stub_marker(tmp_path / "bin-single", total_pages=10, numbering=numbering)
    budget = _budget_for(3)
    cache = tmp_path / "cache"

    with pytest.raises(OcrRangesIncomplete):
        _backend(chunked_bin, max_range_pixels=budget).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    resumed = _backend(chunked_bin, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    single = _backend(single_bin, max_range_pixels=0).run(
        input_path=pdf, work_dir=tmp_path / "work-single"
    )

    assert [(p.page_number, p.text) for p in resumed.pages] == [
        (p.page_number, p.text) for p in single.pages
    ]
    assert len(stub_argv(single_bin)) == 1, "the comparison really is a single pass"
    assert _ranges_of(stub_argv(chunked_bin)) == ["0-2", "3-5", "6-8", "9-9"], (
        "and the resume bought only the ranges the stop never produced"
    )


def test_a_cache_and_a_release_that_disagree_are_refused_not_mixed(tmp_path):
    """Rule 3's soundness claim, driven rather than argued.

    The cache key does not mention numbering -- it does not have to: what is
    cached is the range's RAW markdown, and the convention is re-derived over
    cached and fresh ranges together. So a job whose first ranges were
    produced by a release numbering one way and whose later ranges are
    produced by a release numbering the other is the rule-2 refusal, never a
    silent concatenation of two conventions."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 10)
    before = write_stub_marker(
        tmp_path / "bin", total_pages=10, numbering=PAGE_RANGE_NUMBERING_RELATIVE
    )
    after = write_stub_marker(
        tmp_path / "bin2", total_pages=10, numbering=PAGE_RANGE_NUMBERING_ABSOLUTE
    )
    budget = _budget_for(3)
    cache = tmp_path / "cache"

    with pytest.raises(OcrRangesIncomplete):
        _backend(before, max_range_pixels=budget).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    with pytest.raises(PageRangeNumberingError) as excinfo:
        _backend(after, max_range_pixels=budget).run(
            input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
        )
    assert "number their pages differently" in str(excinfo.value)


def test_a_changed_budget_throws_the_cache_away(tmp_path, ten_page_job):
    """Ranges from two different plans are not a document. The cache is keyed
    to the plan and to the input's sha256, and a key that disagrees is
    removed rather than partly reused."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"
    with pytest.raises(OcrRangesIncomplete):
        _backend(script, max_range_pixels=budget).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    wider = _budget_for(5)
    result = _backend(script, max_range_pixels=wider).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5", "0-4", "5-9"]
    assert [p.page_number for p in result.pages] == list(range(10))


def test_changed_extra_args_throw_the_cache_away(tmp_path, ten_page_job):
    """FIX V-5. `[ingest.ocr] marker_extra_args` change the INVOCATION --
    `--force_ocr`, an OCR language, a model override -- so ranges produced
    before the change and ranges produced after it are two different
    commands' output. Concatenating those is the same failure a changed
    budget makes, and the key now refuses it the same way."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"
    with pytest.raises(OcrRangesIncomplete):
        _backend(script, max_range_pixels=budget).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5"]

    result = _backend(script, max_range_pixels=budget, extra_args=["--force_ocr"]).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5", "0-2", "3-5", "6-8", "9-9"], (
        "every range re-run under the new argument: none of the old command's output was reused"
    )
    assert [p.page_number for p in result.pages] == list(range(10))
    assert all("--force_ocr" in row for row in stub_argv(script)[2:])


def test_the_same_extra_args_keep_the_cache(tmp_path, ten_page_job):
    """The other side of V-5: the key discriminates on the arguments, it
    does not simply invalidate whenever any are set."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"
    with pytest.raises(OcrRangesIncomplete):
        _backend(script, max_range_pixels=budget, extra_args=["--force_ocr"]).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    _backend(script, max_range_pixels=budget, extra_args=["--force_ocr"]).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5", "6-8", "9-9"]


def test_a_changed_document_throws_the_cache_away(tmp_path):
    """The same job id, a different file: the pages in the cache are another
    document's, and concatenating them is the one failure a resume could
    introduce that nothing downstream would catch."""
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 6)
    script = write_stub_marker(tmp_path / "bin", total_pages=6)
    budget = _budget_for(3)
    cache = tmp_path / "cache"
    with pytest.raises(OcrRangesIncomplete):
        _backend(script, max_range_pixels=budget).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 1 else "go",
            cache_dir=cache,
        )
    make_pdf(pdf, [A4_PT] * 6 + [A4_PT])  # same name, different bytes
    script2 = write_stub_marker(tmp_path / "bin2", total_pages=7)
    _backend(script2, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert _ranges_of(stub_argv(script2)) == ["0-2", "3-5", "6-6"], (
        "every range re-run: none of the first document's pages were reused"
    )


def test_a_truncated_cached_range_is_re_run_rather_than_published(tmp_path, ten_page_job):
    """FIX V-1. A cached range that is only partly on disk reads back as a
    SHORTER range, and short is indistinguishable from "this range had blank
    pages" -- a gap, which the concatenation deliberately permits. So nothing
    downstream would refuse it and the document would be published with pages
    missing and ``page_count`` still claiming them all.

    The digest written beside each range closes that: a file that does not
    hash to it is not in the cache, and the range is bought again."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"
    with pytest.raises(OcrRangesIncomplete):
        _backend(script, max_range_pixels=budget).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    victim = cache / PageRange(3, 5).name
    full = victim.read_text(encoding="utf-8")
    victim.write_text(full[: len(full) // 3], encoding="utf-8")

    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert [p.page_number for p in result.pages] == list(range(10)), (
        "the truncated range's pages are all there"
    )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5", "3-5", "6-8", "9-9"], (
        "only the damaged range was re-run; 0-2 was still trusted"
    )


def test_a_cached_range_whose_digest_is_missing_is_re_run(tmp_path, ten_page_job):
    """The digest is written AFTER the range it describes, so a kill between
    the two writes leaves a range nothing vouches for. That is the same
    answer as a damaged one: not in the cache."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"
    with pytest.raises(OcrRangesIncomplete):
        _backend(script, max_range_pixels=budget).run(
            input_path=pdf,
            work_dir=tmp_path / "work",
            on_range=lambda done, total: "stop" if done == 2 else "go",
            cache_dir=cache,
        )
    (cache / f"{PageRange(0, 2).name}.sha256").unlink()

    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert _ranges_of(stub_argv(script)) == ["0-2", "3-5", "0-2", "6-8", "9-9"]
    assert [p.page_number for p in result.pages] == list(range(10))


def test_a_write_killed_part_way_leaves_no_range_at_all(tmp_path, ten_page_job, monkeypatch):
    """The write itself, not only the guard over it.

    A range goes to disk through ``atomic_write_bytes``, whose whole contract
    is that partial data only ever exists under a temp name: a process killed
    while it writes leaves the previous state at the target, never a short
    file. Driven through that helper's own kill-mid-write seam
    (``_chunk_size``/``_on_chunk``), which is how the primitive's own suite
    stands in for a ``SIGKILL``."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"

    class Killed(Exception):
        pass

    def killed_write(path, data, **kwargs):
        def after_first_chunk(_offset):
            raise Killed()

        atomic_write_bytes(path, data, _chunk_size=8, _on_chunk=after_first_chunk)

    monkeypatch.setattr(backends, "atomic_write_bytes", killed_write)
    with pytest.raises(Killed):
        _backend(script, max_range_pixels=budget).run(
            input_path=pdf, work_dir=tmp_path / "work", cache_dir=cache
        )

    assert list(cache.glob("range-*.md")) == [], "no half-written range under its real name"
    monkeypatch.undo()
    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work-2", cache_dir=cache
    )
    assert [p.page_number for p in result.pages] == list(range(10))


def test_a_cached_range_round_trips_byte_for_byte(tmp_path, ten_page_job):
    """The digest is over the bytes, so the cache has to hand back the bytes.
    A helper that normalized line endings on the way in would make a resumed
    document differ from the same document run in one go."""
    pdf, script, budget = ten_page_job
    cache = tmp_path / "cache"
    result = _backend(script, max_range_pixels=budget).run(
        input_path=pdf, work_dir=tmp_path / "work", cache_dir=cache
    )
    for entry in result.ranges:
        name = PageRange(entry["first_page"], entry["last_page"]).name
        raw = (cache / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert (cache / f"{name}.sha256").read_text(encoding="utf-8").strip() == entry["sha256"]


def test_without_a_cache_dir_nothing_is_kept(tmp_path, ten_page_job):
    """The single-machine handler passes no cache, and a backend with none
    computes every range and writes nothing -- the class stays out of the way
    rather than making the caller branch."""
    pdf, script, budget = ten_page_job
    _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "work")
    _backend(script, max_range_pixels=budget).run(input_path=pdf, work_dir=tmp_path / "work-2")
    assert len(stub_argv(script)) == 8
