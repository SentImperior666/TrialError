"""Pluggable OCR/embed backends. Build brief: "REAL LOCAL MODELS: marker
OCR + Qwen3-Embedding-4B live in the origin-project repo ... implement the OCR and
embed handlers as PLUGGABLE BACKENDS: a real backend that shells out to
those proven origin-project tools (config-pathed in trialerror.toml, NOT hardcoded), plus a
deterministic fake backend for tests (tests must NOT need the GPU)."

Two small interfaces (:class:`OcrBackend`, :class:`EmbedBackend`), each with
exactly two implementations:

- ``Fake*`` -- deterministic, zero-dependency, used by default in every
  test and by ``trialerror.toml``'s own default config (``backend = "fake"``) so
  a fresh program scaffold works out of the box without a GPU.
- ``Real*`` -- shells out to the proven origin-project tools via ``subprocess``, paths
  taken from ``trialerror.toml``'s ``[ingest.ocr]``/``[ingest.embed]`` tables
  (never hardcoded). Exercised only by GPU-gated tests
  (``@pytest.mark.skipif`` on the configured executable's existence --
  design Section 13 flag F18/M15's "state the hardware assumption").

``load_ocr_backend(config)``/``load_embed_backend(config)`` are the one
factory pair callers (the ``ocr``/``embed`` job handlers) use -- neither
constructs a concrete backend class directly, so a third backend (a
different OCR/embedding model down the line) is a config value away.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

# TRIALERROR-DEV-NOTE (FX-1, IMPL_REVIEW_VERDICT.md Tier 1 / IMPL_REVIEW_C_ops.md
# N-3): trialerror.jobs.worker is the sole source of EnvironmentalFailure (the
# handler-facing "this failure is transient/environmental, don't consume a
# retry attempt" escape hatch) -- imported here, not re-derived, so a timed-
# out real backend re-queues through the exact same ledger path a GPU-busy
# handler would use. Non-circular: trialerror.jobs.worker imports only
# trialerror.jobs.{ledger,errors,registry}/trialerror.stores.store/trialerror.util.*, never
# trialerror.ingest.
from trialerror.jobs.worker import EnvironmentalFailure
from trialerror.ingest.errors import PageRangeFlagMissingError, PageRangeNumberingError

# Lane L0-C (design D5/D13): the third backend value, ``"offload"``, is not
# a model at all -- it routes the stage to the DEV GPU worker through the
# handler-level seam. ``trialerror.offload.marker`` is deliberately
# dependency-free so this import can sit at module level without a cycle.
from trialerror.offload.marker import OFFLOAD_BACKEND_NAME, OffloadMarker

# FIX V-1: the resume cache's one write primitive. A range file that is only
# partly on disk reads back as a shorter document, and a shorter document is
# indistinguishable from a document with blank pages -- so the write has to be
# the atomic one every other durable file in this tree goes through.
from trialerror.util.atomic import atomic_write_bytes, atomic_write_text

__all__ = [
    "OcrPage",
    "OcrResult",
    "OcrBackend",
    "FakeOcrBackend",
    "RealMarkerOcrBackend",
    "load_ocr_backend",
    "DEFAULT_OCR_TIMEOUT_S",
    "EmbedBackend",
    "FakeEmbedBackend",
    "RealQwenEmbedBackend",
    "LlamaCppEmbedBackend",
    "LLAMA_CPP_BACKEND_NAME",
    "native_logs_to_stderr",
    "LlamaServerEmbedBackend",
    "LLAMA_SERVER_BACKEND_NAME",
    "DEFAULT_LLAMA_SERVER_URL",
    "DEFAULT_LLAMA_SERVER_TIMEOUT_S",
    "DEFAULT_LLAMA_SERVER_N_CTX",
    "LLAMA_SERVER_EMBED_PATH",
    "LLAMA_SERVER_HEALTH_PATH",
    "LLAMA_SERVER_PROPS_PATH",
    "DEFAULT_QUERY_PROMPT",
    "embeddable_text",
    "cgroup_cpu_quota",
    "embed_backend_runtime_details",
    "load_embed_backend",
    "load_query_embed_backend",
    "QUERY_EMBED_BACKEND_INVALID",
    "query_embed_backend_name",
    "embed_backend_runnable",
    "QUERY_EMBED_TABLE",
    "QUERY_EMBED_SUBTABLE_KEY",
    "SAME_AS_DOCUMENT",
    "QueryEmbedBackendMismatchError",
    "EmbedBackendNotRunnable",
    "DEFAULT_FAKE_EMBED_DIMS",
    "DEFAULT_EMBED_TIMEOUT_S",
    "DEFAULT_EMBED_SESSION_MODE",
    "EmbedDriverCrashed",
    "RealBackendRequiredError",
    "assert_real_backends_if_required",
    "stage_requires_real",
    "resolve_stage_backend",
    "fake_stage_backend_warning",
    "FAKE_STAGE_BACKEND_WARNING_CODE",
    "require_real_key_for",
    "REQUIRE_REAL_STAGES",
    "REQUIRE_REAL_STAGE_KEY",
    "REQUIRE_REAL_GLOBAL_KEY",
    "close_embed_driver_sessions",
]


class RealBackendRequiredError(ValueError):
    """Raised at config-load time by :func:`assert_real_backends_if_required`
    when a program that declared ``[ingest] require_real_backends = true``
    would nonetheless route a stage through the fake backend.

    A ``ValueError`` subclass so every existing caller that already catches
    the loaders' ``ValueError`` for a malformed backend table keeps
    behaving the same way."""


#: The two stages a program can route through a fake backend, and therefore
#: the two that can be required to be real -- independently of each other
#: (lane FB-1 item F5).
REQUIRE_REAL_STAGES: tuple[str, ...] = ("ocr", "embed")

#: The per-stage key, inside the stage's own ``[ingest.<stage>]`` table.
REQUIRE_REAL_STAGE_KEY = "require_real"

#: The program-wide key it defaults to.
REQUIRE_REAL_GLOBAL_KEY = "require_real_backends"


def stage_requires_real(raw_config: dict[str, Any] | None, stage: str) -> bool:
    """Does THIS stage have to run on a real backend?

    Lane FB-1 item F5. The one global flag was too coarse for the situation
    it is most often used in: a program with a real embedder and no OCR
    stack at all (or the reverse) either declared the global flag and had
    every ingest refused for the stage it cannot run, or left it off and
    lost the guarantee on the stage it can. Two knobs, one default:

        [ingest]                     # the program-wide declaration
        require_real_backends = true

        [ingest.ocr]
        require_real = false         # ...except this stage

    ``[ingest.<stage>] require_real`` wins for its own stage; absent, the
    global value applies. So every configuration that exists today resolves
    byte-for-byte to what it resolved to before this key existed: no
    per-stage key anywhere means both stages read the same global flag.

    The per-stage key necessarily lives INSIDE the stage's table, so a
    program with no ``[ingest.ocr]`` table at all cannot exempt the OCR
    stage that way -- and should not be able to: "the table is absent" is
    one of the two conditions being refused (see
    :func:`assert_real_backends_if_required`), and an absent table means the
    fake backend. Set the key with the table, or turn the global flag off.
    """
    ingest = (raw_config or {}).get("ingest") or {}
    global_flag = bool(ingest.get(REQUIRE_REAL_GLOBAL_KEY, False))
    table = ingest.get(stage)
    if isinstance(table, dict) and REQUIRE_REAL_STAGE_KEY in table:
        return bool(table[REQUIRE_REAL_STAGE_KEY])
    return global_flag


def require_real_key_for(raw_config: dict[str, Any] | None, stage: str) -> str:
    """Which key actually decided :func:`stage_requires_real` for ``stage``
    -- so a refusal names the line the operator has to edit rather than the
    one they might have edited."""
    ingest = (raw_config or {}).get("ingest") or {}
    table = ingest.get(stage)
    if isinstance(table, dict) and REQUIRE_REAL_STAGE_KEY in table:
        return f"[ingest.{stage}] {REQUIRE_REAL_STAGE_KEY}"
    return f"[ingest] {REQUIRE_REAL_GLOBAL_KEY}"


def assert_real_backends_if_required(raw_config: dict[str, Any] | None) -> None:
    """Design D13: "the origin-project program's toml sets ``[ingest]
    require_real_backends = true`` (fake or absent backend tables refused);
    ... default remains permissive so the test suite and scratch programs
    keep working".

    Resolved PER STAGE since lane FB-1 item F5 (:func:`stage_requires_real`):
    ``[ingest.ocr] require_real`` / ``[ingest.embed] require_real`` each
    default to the global flag, so a program can require a real embedder
    while exempting a stage it has no stack for -- and every configuration
    written before those keys existed resolves exactly as it did before.

    ``raw_config`` is the WHOLE ``trialerror.toml`` dict (not one
    ``[ingest.*]`` table) because the flag and the tables it governs live
    at different depths, and because "the table is absent entirely" is one
    of the two conditions being refused -- a check that only ever sees the
    table cannot detect its absence.

    Why this is worth a hard refusal rather than a doctor warning: the
    failure it prevents is silent. A one-character typo in a table name
    (``[ingest.embeded]``) leaves a program that looks configured, runs
    without error, and writes hash-derived 16-dimensional vectors into a
    knowledge store whose whole purpose is retrieval. Nobody notices until
    search quality is quietly wrong, months of ingest later."""
    ingest = (raw_config or {}).get("ingest") or {}
    for stage in REQUIRE_REAL_STAGES:
        if not stage_requires_real(raw_config, stage):
            continue
        deciding_key = require_real_key_for(raw_config, stage)
        table = ingest.get(stage)
        if not isinstance(table, dict) or not table:
            raise RealBackendRequiredError(
                f"{deciding_key} = true, but [ingest.{stage}] is absent from "
                "trialerror.toml -- an absent table means the fake backend, which this program "
                f"has declared it will not accept. Set [ingest.{stage}] backend = "
                f"'{OFFLOAD_BACKEND_NAME}' (GPU on another machine) or a real local backend "
                f"(or exempt this stage with [ingest.{stage}] {REQUIRE_REAL_STAGE_KEY} = false)."
            )
        backend_name = table.get("backend", "fake")
        if backend_name == "fake":
            raise RealBackendRequiredError(
                f"{deciding_key} = true, but [ingest.{stage}] backend = 'fake' "
                "-- refusing to route the record through a deterministic stand-in."
            )
    # Lane F-1: the query side is the same declaration. Its vectors are
    # never stored, so nothing on disk would ever carry the evidence -- a
    # program embedding its queries through a stand-in simply ranks its
    # whole corpus by a hash and reads as a working search, which is
    # exactly the silent failure this function exists to refuse.
    #
    # FB-1 item F5: it follows the EMBED stage's own resolved requirement,
    # not the global flag -- a program that has exempted its embed stage has
    # said, in as many words, that a stand-in embedding is acceptable here,
    # and refusing its query side over the same declaration would be
    # refusing the exemption it just granted.
    if stage_requires_real(raw_config, "embed"):
        query_table = (ingest.get("embed") or {}).get(QUERY_EMBED_SUBTABLE_KEY)
        if isinstance(query_table, dict) and query_table.get("backend") == "fake":
            raise RealBackendRequiredError(
                f"{require_real_key_for(raw_config, 'embed')} = true, but [{QUERY_EMBED_TABLE}] "
                "backend = 'fake' -- a query embedded by a deterministic stand-in ranks this corpus "
                f"by a hash. Name a real query-side backend (e.g. '{LLAMA_CPP_BACKEND_NAME}' with a "
                f'model_path), or drop the table and inherit the document side ("{SAME_AS_DOCUMENT}").'
            )


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class OcrResult:
    pages: list[OcrPage]
    ocr_backend: str
    ocr_version: str
    #: How many pages the DOCUMENT has, when the backend read a page tree to
    #: find out (the chunked marker path does; the stand-in and the offload
    #: marker do not). Distinct from ``len(pages)``, which counts the pages
    #: that produced text -- a blank page is a page. This is the number a
    #: job manifest's ``expect.page_count`` is checked against.
    page_count: int | None = None
    #: One entry per page range a chunked run produced, each with its first
    #: and last page and the sha256 of the markdown that range wrote. Empty
    #: for an unchunked run. Carried into the job's result manifest so a
    #: resumed job's output can be reconciled range by range rather than
    #: only in total.
    #:
    #: Each entry also carries what the range COST (lane FB-8b item 2):
    #: ``range_wall_s`` (always), ``cached`` (whether the range came out of
    #: the resume cache instead of an invocation), and ``peak_rss_bytes`` /
    #: ``peak_rss_source`` -- the child's peak resident memory where the
    #: platform gives it cheaply, ``None`` where it does not. Diagnostics
    #: only: nothing in the pipeline reads them, and they exist because
    #: ``max_range_pixels`` cannot be sized honestly from the raster
    #: arithmetic alone. Read ``peak_rss_source`` before comparing two
    #: numbers -- on POSIX it is a cumulative high-water mark, not this
    #: range's own peak.
    ranges: tuple[dict[str, Any], ...] = ()
    #: Which convention this chunked run read the ``{N}`` page markers under
    #: (:data:`PAGE_RANGE_NUMBERING_ABSOLUTE` or
    #: :data:`PAGE_RANGE_NUMBERING_RELATIVE`), and what settled it --
    #: ``"config"`` when the program named one, ``"range <flag_value>"`` when
    #: ``auto`` read it off that range's own output, and ``"identical"`` for
    #: the run where no marker anywhere distinguishes them (so the choice has
    #: no consequence and nothing had to decide). ``None`` for an unchunked
    #: run, which has no range flag and no convention to choose.
    page_range_numbering: str | None = None
    page_range_numbering_decided_by: str | None = None
    #: The DPI the ranges were planned at, and which setting fixed it
    #: (``"bounded_dpi"`` or ``"marker_extra_args --highres_image_dpi"``).
    #: A range count only means something next to the page area it was
    #: derived from, and that area is a DPI squared. ``None`` for an
    #: unchunked run, which planned nothing.
    planning_dpi: float | None = None
    planning_dpi_source: str | None = None


class OcrBackend(Protocol):
    name: str
    version: str

    def run(self, *, input_path: Path, work_dir: Path) -> OcrResult: ...


class FakeOcrBackend:
    """Deterministic OCR stand-in for tests and GPU-less machines. Treats
    ``input_path``'s own bytes as already-recognized text (no actual image
    decoding -- test fixtures for the "scanned pdf"/"image" route are plain
    UTF-8 text files carrying that route's extension), splitting into pages
    on a literal ``\\x0c`` (form-feed, the conventional page-break byte)
    or, absent one, treating the whole file as a single page. This keeps
    the OCR-route acceptance path (normalize -> OCR job -> elements ->
    chunk -> embed) exercisable with zero GPU/model dependency, per the
    build brief's "tests must NOT need the GPU.\""""

    name = "fake"
    version = "1"

    def run(self, *, input_path: Path, work_dir: Path) -> OcrResult:
        raw = input_path.read_text(encoding="utf-8", errors="replace")
        page_texts = raw.split("\x0c") if "\x0c" in raw else [raw]
        pages = [OcrPage(page_number=i + 1, text=t.strip()) for i, t in enumerate(page_texts) if t.strip()]
        return OcrResult(pages=pages, ocr_backend=self.name, ocr_version=self.version)


#: Default subprocess timeout (seconds) for :class:`RealMarkerOcrBackend`
#: when ``trialerror.toml``'s ``[ingest.ocr]`` table doesn't set its own
#: ``timeout_s`` -- FX-1 (IMPL_REVIEW_VERDICT.md Tier 1 / IMPL_REVIEW_C_ops.md
#: N-3): before this fix ``subprocess.run`` had NO timeout at all, so a
#: wedged marker/torch child blocked the handler forever -- the job's lease
#: (``trialerror.jobs.ledger.LEASE_DURATION_S``, 900s default) would eventually
#: expire and get reclaimed by ANOTHER worker while the zombie subprocess
#: still held the GPU (double execution, the exact failure mode
#: ``EnvironmentalFailure`` exists to manage).
#:
#: TRIALERROR-DEV-NOTE (deviation from the review's literal "~1800s" suggestion,
#: disclosed per the fix brief): 1800s is kept as the OUT-OF-THE-BOX
#: default because a full-book marker GPU run can legitimately take longer
#: than the 900s lease -- but ``subprocess.run`` is one blocking call with
#: no heartbeat granularity inside it, so this default alone does NOT
#: guarantee the double-execution window closes; it only guarantees a
#: truly-wedged process no longer hangs FOREVER (it now dies, frees the
#: GPU, and settles the job as retryable). Deployments running the real
#: backend should pair ``ingest.ocr.timeout_s``/``ingest.embed.timeout_s``
#: with a ``--lease-s`` at least that large (``trialerror/cli/jobs.py``'s
#: ``--lease-s`` flag) so THIS worker's own timeout fires before the
#: ledger's lease-expiry reclaim would. Config-read first (the seam
#: already exists -- ``config`` here is the program's own ``trialerror.toml``
#: table, read generically), this constant is only the built-in fallback.
DEFAULT_OCR_TIMEOUT_S = 1800


# ---------------------------------------------------------------------------
# page-range chunking (lane e1e Part B)
# ---------------------------------------------------------------------------


#: How marker's own documentation spells the flag that limits one invocation
#: to part of a document. Behind a config key
#: (``[ingest.ocr] page_range_flag``) because it is a fact about somebody
#: else's CLI, not about this repo: a marker release that renames it must be
#: a one-line config change on the machine that has that release, not a
#: harness version bump. The backend probes ``--help`` once and refuses by
#: name if the flag it is about to pass is not there.
DEFAULT_PAGE_RANGE_FLAG = "--page_range"

#: ``auto`` -- work out from the output itself whether this marker release
#: numbers a range's ``{N}`` page markers from the start of the INVOCATION
#: (``relative``) or by the page's own index in the DOCUMENT (``absolute``).
PAGE_RANGE_NUMBERING_AUTO = "auto"

#: A marker ``N`` is the page's absolute index: ``first <= N <= last``. What
#: marker-pdf 1.10.x actually does under ``--page_range`` (measured on the
#: first real chunked run: every range after the first was refused by a guard
#: that assumed the other convention).
PAGE_RANGE_NUMBERING_ABSOLUTE = "absolute"

#: A marker ``N`` counts from the start of the invocation: ``0 <= N < count``,
#: and the page is ``N + first``.
PAGE_RANGE_NUMBERING_RELATIVE = "relative"

PAGE_RANGE_NUMBERING_MODES = (
    PAGE_RANGE_NUMBERING_AUTO,
    PAGE_RANGE_NUMBERING_ABSOLUTE,
    PAGE_RANGE_NUMBERING_RELATIVE,
)

#: Default ``auto``: a harness that ships a hardcoded convention is a harness
#: that is wrong on half the releases, and the evidence for which one is in
#: the output of the very first range that cannot be read both ways.
DEFAULT_PAGE_RANGE_NUMBERING = PAGE_RANGE_NUMBERING_AUTO

#: Spelled once, because every numbering refusal has to end with the one line
#: that settles it on the operator's machine.
PAGE_RANGE_NUMBERING_KEY = "[ingest.ocr] page_range_numbering"

#: The DPI a page's pixel area is estimated at when planning ranges
#: (``[ingest.ocr] bounded_dpi``). Not passed to marker -- this is the
#: harness's own arithmetic about how much raster a run will hold -- but it
#: is worthless unless it is **at least** what the OCR stack really
#: rasterises at.
#:
#: **192, because that is marker's own high-res render DPI.** marker builds
#: a range's page images in two passes and holds the pages of the range at
#: once; the larger pass is ``highres_image_dpi``, whose marker 1.x default
#: is 192 (the low-res pass defaults to 96). Observed, not inferred: a real
#: chunked run died with a ``MemoryError`` raised inside marker's own
#: ``document.build_document -> provider.get_images(page_range,
#: highres_image_dpi) -> pypdfium2 ... PIL.Image.frombytes``, which renders
#: EVERY page of the range at that DPI before anything is recognised.
#:
#: This constant used to be 96 -- the conventional screen resolution, and
#: the low-res pass -- with a comment admitting that under-estimating the
#: DPI over-estimates how many pages fit. It did: at 96 a plan is FOUR TIMES
#: too optimistic in pixels (area goes as DPI squared) for a stack rendering
#: at 192, which is the bound not being a bound. The safe direction is to
#: over-estimate, so the default is the larger pass, and a program passing
#: ``--highres_image_dpi`` through ``marker_extra_args`` has the planner
#: raised to match (:func:`highres_dpi_from_extra_args`) -- never lowered,
#: because a value below the render DPI is the failure this replaces.
DEFAULT_BOUNDED_DPI = 192

#: The marker flag that decides how big a range's page images really are.
#: Read out of ``[ingest.ocr] marker_extra_args`` rather than added as a
#: second knob: a deployment that passes it has already stated the number
#: once, and a harness key that could disagree with the CLI argument beside
#: it would be a way to get the plan wrong that did not exist before.
MARKER_HIGHRES_DPI_FLAG = "--highres_image_dpi"

#: Bytes per pixel of page raster. Not a knob: it is what an 8-bit RGB page
#: image costs, and it appears in the guide's translation of the pixel budget
#: into memory rather than in the planning arithmetic itself.
BYTES_PER_PAGE_PIXEL = 3

#: What the planner multiplies the raw page raster by before comparing it
#: with the budget. A page costs more than its own pixels while it is being
#: recognised -- intermediate buffers, the model's working copy of the image
#: -- and a bound that assumed otherwise would be the bound nobody set.
RANGE_PIXEL_SAFETY = 1.1

#: **The default pixel budget for ONE marker invocation**
#: (``[ingest.ocr] max_range_pixels``), and the arithmetic behind the number:
#:
#: * A4 is 8.27 x 11.69 inches. At :data:`DEFAULT_BOUNDED_DPI` that is
#:   1586.7 x 2245.3 = 3,562,596 pixels of raster per page.
#: * ``64,000,000 / (3,562,596 x 1.1)`` = 16 pages per range, so a 540-page
#:   A4 scan runs as 34 ranges -- 33 of 16 (528 pages) and a 34th of 12 --
#:   rather than as one invocation holding 540 page images at once.
#:   (FIX V-4: this sentence once said "nine of 65 and one of 45", which is
#:   630 pages and contradicted the test beside it. The 65 itself was the
#:   arithmetic at the old 96-DPI planning default, which under-counted a
#:   192-DPI render fourfold.)
#: * At :data:`BYTES_PER_PAGE_PIXEL` that budget is 192 MB of nominal page
#:   raster (about 175 MB once the safety factor has taken its tenth). The
#:   measured failure this replaces was a single
#:   540-page run committing ~58 GB on the GPU machine -- the raster is not
#:   the whole of that, which is exactly why the knob bounds the thing that
#:   scales with the document (pages x page area) rather than trying to
#:   predict the total.
#:
#: ``0`` (or any non-positive value) means **no bound**: one range, the
#: unchunked invocation, which is what every program that has never heard of
#: this knob gets until it sets one.
DEFAULT_MAX_RANGE_PIXELS = 64_000_000

#: PDF user-space units per inch. A page box is in points; the planner needs
#: pixels.
_POINTS_PER_INCH = 72.0


@dataclass(frozen=True)
class PageRange:
    """One contiguous span of pages, **0-indexed and inclusive on both ends**
    -- the convention marker's own ``--page_range`` argument uses, so the
    value this carries can be handed to the flag without a translation
    nobody would remember to keep.
    """

    first: int
    last: int

    @property
    def count(self) -> int:
        return self.last - self.first + 1

    @property
    def flag_value(self) -> str:
        """``"0-64"`` -- what goes after the page-range flag."""
        return f"{self.first}-{self.last}"

    @property
    def name(self) -> str:
        """The per-range output's filename in the resume cache. Zero-padded
        so a directory listing sorts in page order."""
        return f"range-{self.first:06d}-{self.last:06d}.md"


def page_boxes(input_path: Path) -> list[tuple[float, float]]:
    """``[(width_pt, height_pt), ...]`` for every page of a PDF.

    ``pypdf`` (already a core dependency for the pdf-text normalizer), and
    the media box rather than the crop box: what the OCR stack rasterises is
    the page it is given, and the media box is the larger of the two, so
    planning against it errs toward smaller ranges.

    Rotation is not applied because it cannot change the AREA, which is the
    only thing the planner reads -- a landscape page and its portrait
    rotation hold the same number of pixels.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(input_path))
    boxes: list[tuple[float, float]] = []
    for page in reader.pages:
        box = page.mediabox
        boxes.append((float(box.width), float(box.height)))
    return boxes


def highres_dpi_from_extra_args(extra_args: Sequence[str]) -> float | None:
    """The ``--highres_image_dpi`` a program passes marker, or ``None``.

    Both spellings, because both are things people write and a planner that
    understood one of them would silently mis-size the other's runs::

        ["--highres_image_dpi", "300"]      -> 300.0
        ["--highres_image_dpi=300"]         -> 300.0

    A value that is not a positive number is ``None`` rather than a refusal:
    this function's job is to raise the planning DPI when it can prove it
    should be raised, and marker's own argument parser is the thing entitled
    to complain about marker's own arguments.
    """
    args = list(extra_args or ())
    found: float | None = None
    for index, token in enumerate(args):
        raw: str | None = None
        if token == MARKER_HIGHRES_DPI_FLAG and index + 1 < len(args):
            raw = args[index + 1]
        elif token.startswith(f"{MARKER_HIGHRES_DPI_FLAG}="):
            raw = token.split("=", 1)[1]
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            # The LAST one wins, the way an argument parser reads a repeated
            # flag -- so the plan is sized by the value marker will use.
            found = value
    return found


def plan_page_ranges(
    boxes: Sequence[tuple[float, float]],
    *,
    dpi: float = DEFAULT_BOUNDED_DPI,
    max_range_pixels: float = DEFAULT_MAX_RANGE_PIXELS,
    safety: float = RANGE_PIXEL_SAFETY,
) -> list[PageRange]:
    """Tile ``0..len(boxes)-1`` into ranges no bigger than the pixel budget.

    ``range_pages x max_page_area_at_dpi x safety <= max_range_pixels``,
    solved for ``range_pages`` and applied as ONE length for the whole
    document.

    **What that arithmetic is about.** marker renders the whole range before
    it recognises any of it: ``build_document`` asks the provider for the
    images of ``page_range`` at ``highres_image_dpi`` and holds ALL of them
    at once. So a range's cost is its page count times a page's area *at the
    DPI the stack really renders at* -- which is why ``dpi`` here defaults to
    :data:`DEFAULT_BOUNDED_DPI` = 192, marker's own high-res pass, and not to
    the 96 of its low-res one. Area goes as DPI squared, so planning at 96
    against a 192 render is not a slightly loose bound, it is a bound four
    times too generous, and the run that found it died inside PIL rather
    than anywhere this harness can see.

    One length rather than a greedy per-range packing, and the largest page
    rather than the average, because the failure being bounded is a PEAK: a
    run holds every page image it has built, so the worst range is the one
    that decides whether the machine pages. A document of 500 portrait pages
    and 3 posters is planned against the poster -- which costs a few extra
    invocations on the portrait pages and is the direction to be wrong in.

    A budget of zero or less is "no bound" and plans one range over the whole
    document: the unchunked path, reachable by config, which is what makes
    the chunked path auditable against the behaviour it replaced. An empty
    document plans no ranges at all.
    """
    total = len(boxes)
    if total <= 0:
        return []
    max_area = 0.0
    for width_pt, height_pt in boxes:
        area = (float(width_pt) / _POINTS_PER_INCH * dpi) * (
            float(height_pt) / _POINTS_PER_INCH * dpi
        )
        max_area = max(max_area, area)
    if max_range_pixels <= 0 or max_area <= 0 or safety <= 0:
        return [PageRange(0, total - 1)]
    per_range = int(float(max_range_pixels) // (max_area * float(safety)))
    # At least one page per range, always. A budget smaller than a single
    # page cannot be honoured by any plan -- the alternative to one page per
    # invocation is no invocation at all -- so the bound is EXCEEDED rather
    # than silently turning into an empty plan.
    #
    # Minor note 2 from the stage-2 verification: nothing reports that, and
    # this comment used to say it was "reported". The only trace is the
    # caller's own plan line ("540 range(s) of up to 1 page(s)"), which is
    # where a budget read as megabytes shows up -- as an absurd range count,
    # not as a named refusal.
    length = max(1, per_range)
    return [
        PageRange(first, min(first + length - 1, total - 1))
        for first in range(0, total, length)
    ]


# ---------------------------------------------------------------------------
# What a range COST: wall clock always, peak child memory where the platform
# gives it away. Lane FB-8b item 2.
#
# Sizing `max_range_pixels` from the raster arithmetic alone under-states a
# marker_single process by two orders of magnitude (docs/OPERATOR_GUIDE.md's
# "OCR page-range chunking"), so the only honest way to set it is to run one
# small range and read what it actually cost. That reading has to come off
# the run itself -- an operator watching a GPU box through a queue on another
# machine cannot stand over Task Manager -- so the worker logs it per range
# and the result manifest keeps it.
#
# NOTHING depends on these numbers. They are never compared, never gated on,
# and a platform that will not give them up records `None` and carries on.
# ---------------------------------------------------------------------------

#: What ``peak_rss_bytes`` means when it came from ``getrusage``. Said in the
#: value rather than left to a reader, because ``RUSAGE_CHILDREN``'s
#: ``ru_maxrss`` is NOT this range's child: it is the high-water mark over
#: every child this process has ever reaped, for the process's whole
#: lifetime. It therefore only ever rises, and a later range that used less
#: memory than an earlier one reports the earlier one's peak. "Max so far" is
#: the only claim it supports, and the string says so wherever the number is
#: printed.
PEAK_RSS_SOURCE_RUSAGE = "getrusage(RUSAGE_CHILDREN).ru_maxrss: max over this process's children so far"

#: What it means when psutil sampled it. A true per-invocation peak (the
#: sampler only ever looks at the children alive during THIS range), but a
#: sampled one: the last poll before the child exits is the last evidence
#: there is.
PEAK_RSS_SOURCE_PSUTIL = "psutil: sampled peak working set of this invocation's child process(es)"

#: How often the psutil sampler looks, in seconds. Slow on purpose -- the
#: thing being measured takes minutes, and a tight loop would cost the run
#: more than the number is worth.
PEAK_RSS_SAMPLE_INTERVAL_S = 0.5


def _import_psutil() -> Any | None:
    """``psutil`` if this machine happens to have it, else ``None``.

    A SEAM, not a dependency: psutil is not in ``pyproject.toml`` and must
    not become so for a diagnostic number. Tests substitute this function to
    drive both the present and the absent path on a machine that has only
    one of them."""
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - the absent path is driven by the seam
        return None
    return psutil


class _PeakRssProbe:
    """Peak resident memory of the ``marker_single`` child of ONE range,
    when the platform gives it cheaply.

    Two strategies, and neither is bought at the price of a dependency or of
    restructuring the invocation:

    * **POSIX** -- ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` after
      the child has been reaped. Free, exact, and *cumulative*: see
      :data:`PEAK_RSS_SOURCE_RUSAGE`. Reported as "max so far", which is the
      only thing it is.
    * **Windows** -- skipped unless ``psutil`` is importable, in which case a
      daemon thread samples this process's live children while the invocation
      runs. Absent psutil, the range records ``None``: a number is worth less
      than a dependency.

    **It never raises.** Every read is wrapped, the sampler thread swallows
    its own errors, and every failure path records ``None`` -- an OCR run
    must not fail because a diagnostic could not be taken. A probe that
    measured nothing reports ``peak_rss_bytes is None`` and ``source is
    None``, which is the same shape as a platform that offers nothing."""

    def __init__(
        self,
        *,
        platform: str | None = None,
        psutil_module: Any | None = None,
        sample_interval_s: float = PEAK_RSS_SAMPLE_INTERVAL_S,
    ):
        # Lowercased: ``sys.platform`` is documented lowercase, and a check
        # that silently means something else for ``"WIN32"`` is a check that
        # is right by luck (this lane's probe (d)).
        self._platform = str(platform if platform is not None else sys.platform).lower()
        self._psutil = psutil_module
        self._sample_interval_s = float(sample_interval_s)
        self.peak_rss_bytes: int | None = None
        self.source: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the POSIX reading -------------------------------------------------
    @property
    def _posix(self) -> bool:
        return not self._platform.startswith("win")

    def _rusage_children_bytes(self) -> int | None:
        try:
            import resource  # noqa: PLC0415 -- POSIX-only, imported where it is used

            raw = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
        except Exception:
            return None
        if raw <= 0:
            return None
        # ru_maxrss is KILOBYTES on Linux and most BSDs, and BYTES on macOS.
        # Stated rather than assumed: the field's unit is the single thing
        # about it that is platform-dependent, and getting it wrong is a
        # silent factor of 1024 in a number an operator sizes a budget from.
        return raw if self._platform == "darwin" else raw * 1024

    # -- the sampled reading ----------------------------------------------
    def _sample_once(self, parent: Any) -> int | None:
        best: int | None = None
        try:
            children = parent.children(recursive=True)
        except Exception:
            return None
        for child in children:
            # ONE try around the whole per-child read, not just the call
            # (this lane's probe (d)): `int(value)` sat outside it, so a
            # memory_info whose field was not a number killed the sampler
            # thread and printed a traceback into the worker's log -- a
            # diagnostic making noise that looks like a crash.
            try:
                info = child.memory_info()
                # peak_wset where Windows offers it (already a peak, so one
                # successful read near the end is worth every earlier one),
                # rss otherwise.
                value = getattr(info, "peak_wset", None) or getattr(info, "rss", None)
                if value:
                    best = max(best or 0, int(value))
            except Exception:
                continue
        return best

    def _sample_loop(self, parent: Any) -> None:
        # Total, for the same reason: a thread that dies noisily is worse
        # than a range that reports no peak.
        try:
            while not self._stop.is_set():
                seen = self._sample_once(parent)
                if seen is not None:
                    self.peak_rss_bytes = max(self.peak_rss_bytes or 0, seen)
                self._stop.wait(max(0.0, self._sample_interval_s))
        except Exception:
            return

    # -- the context -------------------------------------------------------
    def __enter__(self) -> "_PeakRssProbe":
        if self._posix:
            return self
        # The resolution is inside the guard too (this lane's probe (d)): a
        # seam that raises -- a half-installed psutil, a monkeypatched import
        # hook -- used to come out of `__enter__`, where there is no `with`
        # body yet to protect the OCR run.
        try:
            psutil_module = self._psutil if self._psutil is not None else _import_psutil()
        except Exception:
            return self
        if psutil_module is None:
            return self
        try:
            parent = psutil_module.Process()
            self._thread = threading.Thread(
                target=self._sample_loop, args=(parent,), daemon=True,
                name="trialerror-ocr-peak-rss",
            )
            self._thread.start()
            self.source = PEAK_RSS_SOURCE_PSUTIL
        except Exception:
            self._thread = None
            self.source = None
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._thread is not None:
            self._stop.set()
            with contextlib.suppress(Exception):
                self._thread.join(timeout=self._sample_interval_s * 4)
            self._thread = None
            if self.peak_rss_bytes is None:
                # Sampled nothing -- a child that lived less than one
                # interval, say. An unmeasured range says so.
                self.source = None
            return
        if self._posix:
            measured = self._rusage_children_bytes()
            if measured is not None:
                self.peak_rss_bytes = measured
                self.source = PEAK_RSS_SOURCE_RUSAGE


class OcrRangesIncomplete(Exception):
    """A cooperative stop landed on a range boundary.

    Not a failure: the ranges already produced are in the resume cache and
    the ones after the boundary were never started. Raised out of
    :meth:`RealMarkerOcrBackend.run` so the caller can settle the job the way
    C-0097 D2 settles an incomplete unit -- hand the claim back, keep what
    the GPU already bought -- rather than by inspecting a partial result.

    ``ranges_done``/``ranges_total`` are what the heartbeat reports and what
    the stopped job's own result records."""

    def __init__(self, ranges_done: int, ranges_total: int):
        super().__init__(
            f"stopped at a page-range boundary after {ranges_done}/{ranges_total} range(s)"
        )
        self.ranges_done = ranges_done
        self.ranges_total = ranges_total


class RealMarkerOcrBackend:
    """Shells out to marker-pdf's own ``marker_single`` CLI (the exact
    invocation shape proven in the driver this is ported from:
    ``marker_single <path> --paginate_output --output_dir <dir>
    --disable_tqdm --disable_multiprocessing``) -- GPU-only per the standing
    law this build brief carries forward (no silent CPU fallback is attempted
    here; a non-zero exit is surfaced as a job failure, not swallowed).

    ``marker_single_exe`` is config-pathed (``trialerror.toml``
    ``[ingest.ocr].marker_single_exe``), never hardcoded to a repository
    path -- this module has no idea where that venv lives until told.

    ``timeout_s`` (``trialerror.toml`` ``[ingest.ocr].timeout_s``, default
    :data:`DEFAULT_OCR_TIMEOUT_S`) bounds the ``subprocess.run`` call --
    FX-1: a hung/wedged child now raises :class:`EnvironmentalFailure`
    (job re-queued, retry attempt NOT consumed) instead of blocking the
    handler forever. On the chunked path it bounds **each range**, which is
    the correct scope and also a tighter one: a range is minutes, so a
    wedged child is noticed in minutes rather than in the half hour a whole
    book was allowed.

    **Page-range chunked mode (lane e1e Part B).** marker builds every
    high-resolution page image up front, so its memory grows with
    ``pages x page area``: four large-format scans of 299-540 pages had to be
    paused at job level because one run committed ~58 GB. This backend now
    plans the document into ranges whose raster fits
    ``[ingest.ocr] max_range_pixels`` (see :data:`DEFAULT_MAX_RANGE_PIXELS`
    for the arithmetic), runs ``marker_single ... --page_range A-B`` once per
    range, and concatenates the pages with **absolute** numbers. The bound
    replaces a kill: nothing here watches a process's RSS and shoots it --
    the run is sized so the peak does not arrive.

    What that costs and what it buys, stated rather than discovered:

    * a document that plans to ONE range is run with the unchunked command,
      byte for byte what this backend always sent, and no ``--help`` probe
      happens at all. A small document cannot be refused over a flag it was
      never going to use;
    * the page-range flag's spelling is ``[ingest.ocr] page_range_flag`` and
      is **probed once** against ``--help`` before the first chunked run. A
      range flag that is silently ignored would turn each of N ranges into a
      full-document run, so its absence is
      :class:`~trialerror.ingest.errors.PageRangeFlagMissingError` rather
      than an attempt;
    * **how ``--paginate_output``'s ``{N}`` markers are numbered under a page
      range is a fact about the marker RELEASE, not about this backend.**
      marker-pdf 1.10.x -- the version this repo defaults to -- numbers them
      by the page's ABSOLUTE index in the document; other releases number
      them from the start of the invocation. ``[ingest.ocr]
      page_range_numbering`` is ``"absolute"``, ``"relative"`` or (the
      default) ``"auto"``, which resolves the convention ONCE per document
      from the ranges' own output and never guesses: a marker no convention
      can place, two ranges proving different conventions, or a document
      none of whose ranges can prove either is
      :class:`~trialerror.ingest.errors.PageRangeNumberingError` -- as is a
      concatenation that is not strictly increasing. A GAP is legal: marker
      emits no body for a blank page and this backend drops it, as the
      unchunked path always has;
    * ``cache_dir`` holds one ``.md`` per finished range, so a stop at a
      boundary and a later resume re-run only the ranges that were never
      produced. The cache is keyed to the input's sha256, to the plan and
      to the arguments the invocation carries, so a changed document, a
      changed budget or a changed ``extra_args`` starts again rather than
      concatenating two different runs;
    * ``on_range`` is called after each range with ``(done, total)`` and may
      return ``"stop"``, which raises :class:`OcrRangesIncomplete` -- except
      after the LAST range, where every range has been produced and the
      document is complete, so the result is returned and the caller's own
      checkpoint settles the stop. That is the cooperative checkpoint the
      offload worker already runs between embed batches, arriving at last on
      the stage whose unit used to be the whole job.
    * ``on_range_record`` is called with each range's manifest entry as it is
      produced -- the same dict the result carries, including what the range
      cost (``range_wall_s``, ``cached``, ``peak_rss_bytes``,
      ``peak_rss_source``). It exists so the worker can log a range's cost
      while the run is still going, rather than only in a result nobody reads
      until the document finishes. Purely a report: its return value is
      ignored and a raising callback is the caller's own bug, not a stop.

    TRIALERROR-DEV-NOTE: not exercised against a live GPU in this build
    session (no GPU test in the default suite) -- every path above is driven
    through a stub ``marker_single`` that records its argv. Live verification
    remains the stated integration-session follow-up (design Section 13
    F18/M15: "state the hardware assumption"), and the flag spelling in
    particular is a fact about the installed release that only the probe can
    confirm.
    """

    name = "marker"

    #: Read by the caller (the offload worker) to decide whether to drive
    #: this backend range by range. A backend without it is run the way OCR
    #: backends have always been run, one call for the document.
    supports_page_ranges = True

    _PAGE_MARKER_RE = re.compile(r"^\{(\d+)\}-{3,}\s*$", re.MULTILINE)

    #: What the resume cache records about the run its files belong to. A
    #: cache whose key disagrees with the run about to start is removed, not
    #: reused: concatenating ranges from two different documents, or from two
    #: different plans of the same one, is the one failure a resume can
    #: introduce that nothing downstream could detect.
    _PLAN_FILENAME = "plan.json"

    def __init__(
        self,
        *,
        marker_single_exe: str,
        version: str = "1.10.2",
        extra_args: Sequence[str] = (),
        timeout_s: float = DEFAULT_OCR_TIMEOUT_S,
        page_range_flag: str = DEFAULT_PAGE_RANGE_FLAG,
        max_range_pixels: float = DEFAULT_MAX_RANGE_PIXELS,
        bounded_dpi: float = DEFAULT_BOUNDED_DPI,
        page_range_numbering: str = DEFAULT_PAGE_RANGE_NUMBERING,
    ):
        self.marker_single_exe = marker_single_exe
        self.version = version
        self.extra_args = list(extra_args)
        self.timeout_s = timeout_s
        self.page_range_flag = str(page_range_flag or DEFAULT_PAGE_RANGE_FLAG)
        self.max_range_pixels = float(max_range_pixels)
        self.bounded_dpi = float(bounded_dpi)
        numbering = str(page_range_numbering or DEFAULT_PAGE_RANGE_NUMBERING)
        if numbering not in PAGE_RANGE_NUMBERING_MODES:
            raise ValueError(
                f"{PAGE_RANGE_NUMBERING_KEY} = {numbering!r} is not one of "
                f"{list(PAGE_RANGE_NUMBERING_MODES)}"
            )
        self.page_range_numbering = numbering
        #: ``None`` until the first chunked run probes ``--help``. One probe
        #: per backend instance, and the worker holds one instance per run.
        self._page_range_flag_seen: bool | None = None

    # -- planning ----------------------------------------------------------

    @property
    def planning_dpi(self) -> float:
        """The DPI ranges are actually planned at.

        ``bounded_dpi``, raised to ``--highres_image_dpi`` when
        ``marker_extra_args`` passes a larger one. Raised only, never
        lowered: a program rendering at a LOWER DPI than the planner assumes
        gets ranges smaller than they had to be, which costs invocations; one
        rendering HIGHER than the planner assumes gets the memory failure the
        bound exists to prevent.
        """
        override = highres_dpi_from_extra_args(self.extra_args)
        if override is None:
            return self.bounded_dpi
        return max(self.bounded_dpi, override)

    @property
    def planning_dpi_source(self) -> str:
        """Which of the two settled :attr:`planning_dpi`, so a plan that
        surprises an operator says where its number came from instead of
        leaving them to work it out."""
        override = highres_dpi_from_extra_args(self.extra_args)
        if override is not None and override > self.bounded_dpi:
            return f"marker_extra_args {MARKER_HIGHRES_DPI_FLAG}"
        return "bounded_dpi"

    def plan_ranges(self, input_path: Path) -> list[PageRange] | None:
        """How this document will be split, or ``None`` when it cannot be.

        ``None`` is not a failure and not a silent fallback to something
        weaker: it means this input has no page tree to range over (it is
        not a PDF, or pypdf cannot open it), so there is nothing to chunk and
        the one invocation this backend has always made is the whole of the
        work. The caller reports ``units_total`` accordingly.
        """
        try:
            boxes = page_boxes(Path(input_path))
        except Exception:  # noqa: BLE001 - any unreadable page tree is "cannot chunk"
            return None
        if not boxes:
            return None
        return plan_page_ranges(
            boxes,
            dpi=self.planning_dpi,
            max_range_pixels=self.max_range_pixels,
        )

    # -- running -----------------------------------------------------------

    def run(
        self,
        *,
        input_path: Path,
        work_dir: Path,
        ranges: Sequence[PageRange] | None = None,
        on_range: Callable[[int, int], str] | None = None,
        on_range_record: Callable[[dict[str, Any]], None] | None = None,
        cache_dir: Path | None = None,
    ) -> OcrResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        plan = list(ranges) if ranges is not None else (self.plan_ranges(input_path) or [])
        page_count = None
        if plan:
            page_count = plan[-1].last + 1
        if len(plan) <= 1:
            # The unchunked invocation, unchanged: one range covering the
            # document is the same work as no range at all, and sending the
            # flag for it would make a small document depend on a flag probe
            # it has no use for.
            text = self._run_marker(input_path, work_dir, page_range=None)
            return OcrResult(
                pages=self._split_pages(text),
                ocr_backend=self.name,
                ocr_version=self.version,
                page_count=page_count,
            )

        self._require_page_range_flag()
        cache = _RangeCache(cache_dir, input_path=input_path, plan=plan, backend=self)
        parsed_ranges: list[_ParsedRange] = []
        produced: list[dict[str, Any]] = []
        for index, page_range in enumerate(plan):
            started = time.monotonic()
            text = cache.read(page_range)
            cached = text is not None
            peak_rss_bytes: int | None = None
            peak_rss_source: str | None = None
            if text is None:
                # The probe brackets the INVOCATION and nothing else: the
                # cache read above and the parse below are this process's own
                # work, and folding them in would attribute them to marker.
                with _PeakRssProbe() as probe:
                    text = self._run_marker(
                        input_path,
                        work_dir / f"r{page_range.first:06d}-{page_range.last:06d}",
                        page_range=page_range,
                    )
                peak_rss_bytes = probe.peak_rss_bytes
                peak_rss_source = probe.source
            # Written (or re-written) either way, so the digest the manifest
            # records is the digest the cache verified this range against.
            digest = cache.write(page_range, text)
            # PARSED, not numbered. Which page a `{N}` names is a fact about
            # the marker release, and the evidence for it is spread over the
            # document's ranges -- a range numbered before the run's mode is
            # settled is a range numbered on an assumption.
            parsed_ranges.append(self._parse_range(text, page_range))
            record = {
                "first_page": page_range.first,
                "last_page": page_range.last,
                "sha256": digest,
                "chars": len(text),
                # What this range COST (lane FB-8b item 2). `range_wall_s` is
                # always recorded and covers the range's whole turn in this
                # loop; `cached` is what tells a reader whether that number
                # is a GPU minute or a file read, because a resumed job's
                # ranges come back in milliseconds and would otherwise read
                # as an impossibly fast invocation. `peak_rss_bytes` is
                # present when the platform gave it cheaply and `None` when
                # it did not -- see :class:`_PeakRssProbe`, and read
                # `peak_rss_source` before comparing two of them.
                "range_wall_s": round(max(0.0, time.monotonic() - started), 3),
                "cached": cached,
                "peak_rss_bytes": peak_rss_bytes,
                "peak_rss_source": peak_rss_source,
            }
            produced.append(record)
            # Reported BEFORE the checkpoint: a stop that lands on this
            # boundary must not swallow the measurement of the range that
            # was just paid for. A copy, so a caller that keeps it cannot
            # edit the manifest's own entry.
            if on_range_record is not None:
                on_range_record(dict(record))
            # FIX V-2: ``on_range`` is called after EVERY range -- it is the
            # progress report as well as the checkpoint -- but a "stop" on
            # the LAST one is not an incomplete unit. Every range has been
            # produced; raising here would hand back a claim on a document
            # that is finished and make the next worker re-read the whole
            # cache to discover that. The stop is not lost: the caller takes
            # its own checkpoint after the pages are written, which is where
            # a finished unit's stop belongs (the same shape ``_run_embed``
            # has after its last batch).
            stop = on_range is not None and on_range(index + 1, len(plan)) == "stop"
            if stop and index + 1 < len(plan):
                raise OcrRangesIncomplete(index + 1, len(plan))
        # Every range of the run is in hand -- the ones this process bought
        # and the ones a stopped predecessor left in the cache -- so the
        # convention is resolved over all of them at once. That is what makes
        # a resume sound: the cache holds RAW marker text, so a range produced
        # under one release cannot be silently numbered under another's rule;
        # a disagreement is a refusal, not a mix.
        numbering, decided_by = _resolve_page_range_numbering(
            parsed_ranges, self.page_range_numbering
        )
        pages: list[OcrPage] = []
        for parsed in parsed_ranges:
            pages.extend(self._number_range(parsed, numbering))
        _assert_monotonic(pages)
        return OcrResult(
            pages=pages,
            ocr_backend=self.name,
            ocr_version=self.version,
            page_count=page_count,
            ranges=tuple(produced),
            page_range_numbering=numbering,
            page_range_numbering_decided_by=decided_by,
            planning_dpi=self.planning_dpi,
            planning_dpi_source=self.planning_dpi_source,
        )

    def _require_page_range_flag(self) -> None:
        """Probe ``marker_single --help`` once and refuse if the flag this
        backend is about to pass is not in it."""
        if self._page_range_flag_seen:
            return
        # Minor note 1 from the stage-2 verification: the bound and the
        # sentence are one expression, so a program with `timeout_s = 30`
        # no longer reads "did not return within 120s".
        probe_timeout = min(float(self.timeout_s), 120.0)
        try:
            probe = subprocess.run(
                [self.marker_single_exe, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=probe_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise EnvironmentalFailure(
                f"marker_single --help did not return within {probe_timeout:g}s "
                f"({self.marker_single_exe})"
            ) from exc
        except OSError as exc:
            raise PageRangeFlagMissingError(
                f"could not run {self.marker_single_exe!r} to probe for "
                f"{self.page_range_flag!r}: {exc}"
            ) from exc
        help_text = f"{probe.stdout or ''}\n{probe.stderr or ''}"
        # Minor note 3: a WORD, not a substring. The probe's whole purpose is
        # to catch a flag this release does not have, and a release spelling
        # it `--page_ranges` satisfies a substring test for `--page_range`
        # while ignoring the argument -- which is the failure the probe
        # exists for, arriving through the check meant to prevent it.
        advertised = re.search(
            rf"(?<![\w-]){re.escape(self.page_range_flag)}(?![\w-])", help_text
        )
        if not advertised:
            raise PageRangeFlagMissingError(
                f"this document needs page-range chunking, and the installed marker_single "
                f"({self.marker_single_exe}) does not advertise {self.page_range_flag!r} in its "
                "--help. Set [ingest.ocr] page_range_flag to the spelling your marker release "
                "uses, or set [ingest.ocr] max_range_pixels = 0 to run the document in one "
                "invocation (which is the memory condition C-0098 measured)."
            )
        self._page_range_flag_seen = True

    def _run_marker(
        self, input_path: Path, out_dir: Path, *, page_range: PageRange | None
    ) -> str:
        """One ``marker_single`` invocation; returns the markdown it wrote."""
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.marker_single_exe,
            str(input_path),
            "--paginate_output",
            "--output_dir",
            str(out_dir),
            "--disable_tqdm",
            "--disable_multiprocessing",
        ]
        if page_range is not None:
            cmd.extend([self.page_range_flag, page_range.flag_value])
        cmd.extend(self.extra_args)
        # FX-2 (IMPL_REVIEW_C_ops.md N-4): encoding="utf-8" so marker's
        # stdout/stderr (UTF-8; progress lines can carry non-ASCII) is never
        # decoded as the Windows ANSI codepage (text=True alone means
        # cp1252 here); errors="replace" so a stray non-UTF-8 byte degrades
        # to U+FFFD instead of raising UnicodeDecodeError out of
        # communicate() and burning a retry attempt on a decode bug rather
        # than the real diagnostics.
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            # FX-1: subprocess.run itself kills the child on timeout (no
            # zombie left behind by THIS call) -- raising
            # EnvironmentalFailure (not RuntimeError) tells
            # trialerror.jobs.worker.run_one this is a transient/environmental
            # failure so the ledger re-queues without consuming an attempt
            # (trialerror/jobs/worker.py run_one's EnvironmentalFailure arm).
            where = f" pages {page_range.flag_value}" if page_range is not None else ""
            raise EnvironmentalFailure(
                f"marker_single timed out after {self.timeout_s}s for {input_path}{where} "
                "(GPU-bound OCR; raise ingest.ocr.timeout_s in trialerror.toml if this is "
                "expected for large documents)"
            ) from exc
        if result.returncode != 0:
            where = f" (pages {page_range.flag_value})" if page_range is not None else ""
            raise RuntimeError(
                f"marker_single exited {result.returncode} for {input_path}{where}: "
                f"{result.stderr[-2000:]}"
            )
        stem = input_path.stem
        produced = out_dir / stem / f"{stem}.md"
        if not produced.exists():
            candidates = list(out_dir.rglob("*.md"))
            if not candidates:
                raise RuntimeError(f"marker_single produced no .md output under {out_dir}")
            produced = candidates[0]
        return produced.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def _parse_range(cls, text: str, page_range: PageRange | None) -> "_ParsedRange":
        """One range's markdown, split but **not numbered**.

        Numbering cannot happen here: which page a ``{N}`` marker names is a
        fact about the marker RELEASE, and the evidence for it is spread over
        the document's ranges (see :func:`_resolve_page_range_numbering`).
        So this pass records the markers and their bodies verbatim and the
        caller numbers once the convention is known.
        """
        markers = list(cls._PAGE_MARKER_RE.finditer(text))
        if not markers:
            stripped = text.strip()
            return _ParsedRange(
                page_range=page_range, markers=(), bodies=(), fallback=stripped or None
            )
        numbers: list[int] = []
        bodies: list[tuple[int, str]] = []
        for i, m in enumerate(markers):
            page_num = int(m.group(1))
            numbers.append(page_num)
            start = m.end()
            end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            body = text[start:end].strip()
            if body:
                bodies.append((page_num, body))
        return _ParsedRange(
            page_range=page_range,
            markers=tuple(numbers),
            bodies=tuple(bodies),
            fallback=None,
        )

    @classmethod
    def _number_range(cls, parsed: "_ParsedRange", numbering: str) -> list[OcrPage]:
        """A parsed range's pages, numbered under a mode that is already
        settled. Never called before :func:`_resolve_page_range_numbering`
        has spoken."""
        page_range = parsed.page_range
        if parsed.fallback is not None:
            # FIX V-6: within a range, the fallback's page IS the range's
            # first page. The unchunked path has always called an unpaginated
            # document "page 1" and still does; adding the offset to that 1
            # numbered a range's only page one too high, which is either a
            # silently wrong page number or -- when it collides with the next
            # range's first page -- a PageRangeNumberingError blaming a cause
            # ("two ranges produced the same page") that is not what happened.
            #
            # It is also the one page numbering CANNOT disagree about: there
            # is no marker to read either way.
            first = page_range.first if page_range is not None else 1
            return [OcrPage(page_number=first, text=parsed.fallback)]
        pages: list[OcrPage] = []
        for page_num, body in parsed.bodies:
            if page_range is None:
                # The unchunked invocation: one call for the whole document,
                # so the marker IS the page under either convention and there
                # is no window to be outside of.
                pages.append(OcrPage(page_number=page_num, text=body))
                continue
            low, high = _numbering_window(page_range, numbering)
            if not low <= page_num <= high:
                raise PageRangeNumberingError(
                    f"range {page_range.flag_value} produced a page marker {{{page_num}}}, and "
                    f"{PAGE_RANGE_NUMBERING_KEY} = {numbering!r} requires a marker in "
                    f"{low}..{high} ({_numbering_window_prose(numbering)}). Set "
                    f"{PAGE_RANGE_NUMBERING_KEY} to "
                    f"{_other_numbering(numbering)!r} if that is what your marker release does, "
                    f"or to {PAGE_RANGE_NUMBERING_AUTO!r} to read the convention off the output."
                )
            pages.append(
                OcrPage(
                    page_number=(
                        page_num
                        if numbering == PAGE_RANGE_NUMBERING_ABSOLUTE
                        else page_num + page_range.first
                    ),
                    text=body,
                )
            )
        return pages

    @classmethod
    def _split_pages(
        cls,
        text: str,
        *,
        page_range: PageRange | None = None,
        numbering: str = DEFAULT_PAGE_RANGE_NUMBERING,
    ) -> list[OcrPage]:
        """One range (or one unchunked document) parsed, resolved and
        numbered in a single call.

        The whole-run path does not use this -- it has to resolve the mode
        over EVERY range before numbering any of them -- but a single range
        is a document of one as far as the rule is concerned, and the
        unchunked path has no range at all.
        """
        parsed = cls._parse_range(text, page_range)
        mode, _decided_by = _resolve_page_range_numbering([parsed], numbering)
        return cls._number_range(parsed, mode)


@dataclass(frozen=True)
class _ParsedRange:
    """One range's ``{N}`` markers and their bodies, before numbering.

    ``markers`` is EVERY marker the range emitted, including the ones whose
    body was blank: a blank page still says which convention the release is
    using, and that evidence is the whole of what
    :func:`_resolve_page_range_numbering` has to work with. ``bodies`` is the
    subset that produced text, which is what becomes pages -- a gap is legal
    and always has been.

    ``fallback`` is non-empty markdown carrying no marker at all.
    """

    page_range: PageRange | None
    markers: tuple[int, ...]
    bodies: tuple[tuple[int, str], ...]
    fallback: str | None


def _numbering_window(page_range: PageRange, numbering: str) -> tuple[int, int]:
    """The inclusive ``{N}`` window a range's markers must fall in under
    ``numbering``."""
    if numbering == PAGE_RANGE_NUMBERING_ABSOLUTE:
        return (page_range.first, page_range.last)
    return (0, page_range.count - 1)


def _numbering_window_prose(numbering: str) -> str:
    if numbering == PAGE_RANGE_NUMBERING_ABSOLUTE:
        return "this document's own page numbers"
    return "counted from the start of the invocation"


def _other_numbering(numbering: str) -> str:
    return (
        PAGE_RANGE_NUMBERING_RELATIVE
        if numbering == PAGE_RANGE_NUMBERING_ABSOLUTE
        else PAGE_RANGE_NUMBERING_ABSOLUTE
    )


def _range_numbering_vote(parsed: _ParsedRange) -> str | None:
    """Which convention THIS range's markers prove, or ``None`` for a range
    that cannot tell.

    A marker ``N`` of a range ``first-last`` is *relative-consistent* iff
    ``0 <= N < count`` and *absolute-consistent* iff ``first <= N <= last``.
    A marker consistent with neither is a refusal; one consistent with both
    is no evidence (the windows overlap, which is always true of the first
    range -- where they are the SAME window -- and can be true of a later
    range longer than its own first page index).
    """
    page_range = parsed.page_range
    if page_range is None:
        return None
    rel_low, rel_high = _numbering_window(page_range, PAGE_RANGE_NUMBERING_RELATIVE)
    abs_low, abs_high = _numbering_window(page_range, PAGE_RANGE_NUMBERING_ABSOLUTE)
    votes: set[str] = set()
    for page_num in parsed.markers:
        relative_ok = rel_low <= page_num <= rel_high
        absolute_ok = abs_low <= page_num <= abs_high
        if not relative_ok and not absolute_ok:
            raise PageRangeNumberingError(
                f"range {page_range.flag_value} produced a page marker {{{page_num}}} that "
                f"neither convention can explain: it is outside {rel_low}..{rel_high} (counted "
                f"from the start of the invocation) and outside {abs_low}..{abs_high} (this "
                f"document's own page numbers). The range flag was honoured but its output "
                f"cannot be placed in the document, so nothing is concatenated. "
                f"({PAGE_RANGE_NUMBERING_KEY} selects a convention; it cannot rescue a marker "
                f"that fits neither.)"
            )
        if relative_ok and not absolute_ok:
            votes.add(PAGE_RANGE_NUMBERING_RELATIVE)
        elif absolute_ok and not relative_ok:
            votes.add(PAGE_RANGE_NUMBERING_ABSOLUTE)
    if len(votes) > 1:
        # NOT covered by the stated rule, which reads a range's markers as
        # voting together: this is one invocation whose own markers prove
        # BOTH conventions, which no release can be doing. The nearest thing
        # the rule does say -- two ranges establishing different modes is a
        # refusal -- applies for the same reason, so it is refused rather
        # than broken by a tiebreak nobody wrote down. A 1-based release
        # lands here (markers 1..count against a 0-based range).
        raise PageRangeNumberingError(
            f"range {page_range.flag_value} produced markers under BOTH conventions at once: "
            f"{sorted(parsed.markers)} fit neither {rel_low}..{rel_high} alone (counted from "
            f"the start of the invocation) nor {abs_low}..{abs_high} alone (this document's own "
            f"page numbers). One invocation's markers are all one convention, so this output is "
            f"not a page range this backend can read -- a marker release that numbers pages from "
            f"1 rather than 0 looks exactly like this. Nothing is concatenated."
        )
    return next(iter(votes), None)


def _resolve_page_range_numbering(
    parsed_ranges: Sequence[_ParsedRange], configured: str
) -> tuple[str, str]:
    """``(mode, decided_by)`` for a whole run -- resolved ONCE, from evidence,
    never guessed.

    ``configured`` other than ``auto`` is the answer and the evidence is only
    checked against it (in :meth:`RealMarkerOcrBackend._number_range`, which
    refuses a marker outside the forced mode's window by name).

    Under ``auto`` every range votes (:func:`_range_numbering_vote`). One
    distinct vote is the document's mode, and it carries the undecided ranges
    with it -- an earlier or a later one, which is why this runs over the
    whole run rather than range by range. Two different votes, or none at all
    while ranges after the first had markers to vote with, is a refusal: the
    one thing this must never do is pick.
    """
    if configured not in PAGE_RANGE_NUMBERING_MODES:
        raise ValueError(
            f"{PAGE_RANGE_NUMBERING_KEY} = {configured!r} is not one of "
            f"{list(PAGE_RANGE_NUMBERING_MODES)}"
        )
    if configured != PAGE_RANGE_NUMBERING_AUTO:
        return configured, "config"

    decided: str | None = None
    decided_by: str | None = None
    for parsed in parsed_ranges:
        vote = _range_numbering_vote(parsed)
        if vote is None:
            continue
        assert parsed.page_range is not None  # only a real range can vote
        if decided is None:
            decided = vote
            decided_by = f"range {parsed.page_range.flag_value}"
            continue
        if vote != decided:
            raise PageRangeNumberingError(
                f"two ranges of this document number their pages differently: {decided_by} is "
                f"{decided!r} and range {parsed.page_range.flag_value} is {vote!r}. One document "
                f"cannot be concatenated out of two conventions -- the same page would be "
                f"claimed twice, or claimed by nobody. Nothing is concatenated. If you know "
                f"which one your marker release uses, set {PAGE_RANGE_NUMBERING_KEY} to it and "
                f"the other range's output will be refused by name instead."
            )
    if decided is not None:
        assert decided_by is not None
        return decided, decided_by

    # Nothing voted. The rule says a document whose every range with
    # ``first > 0`` is undecided is refused -- but only where the refusal is
    # about something: a range that emitted NO marker at all has no page
    # whose number the two conventions disagree about (its fallback page is
    # the range's own first page under either, FIX V-6), and neither does the
    # first range, whose two windows are the same window. So the refusal is
    # scoped to ranges that carried markers and still could not decide, and a
    # run where the two readings provably coincide is numbered rather than
    # refused over a choice that has no consequence.
    undecided = [
        parsed.page_range.flag_value
        for parsed in parsed_ranges
        if parsed.page_range is not None and parsed.page_range.first > 0 and parsed.markers
    ]
    if undecided:
        raise PageRangeNumberingError(
            f"no range of this document says which page-numbering convention its marker release "
            f"uses: every range after the first that produced markers ({', '.join(undecided)}) "
            f"is undecided -- its markers fit BOTH the invocation-relative window and the "
            f"absolute one. With the equal-sized ranges this backend plans, a range after the "
            f"first always has disjoint windows and so always decides, which means these ranges "
            f"were supplied by hand. Set {PAGE_RANGE_NUMBERING_KEY} to "
            f"{PAGE_RANGE_NUMBERING_ABSOLUTE!r} (what marker-pdf 1.10.x does) or "
            f"{PAGE_RANGE_NUMBERING_RELATIVE!r} to settle it."
        )
    # Not a guess and not a default: with no marker anywhere that the two
    # conventions read differently, they number this document identically.
    # ``decided_by`` says exactly that rather than crediting a range or a
    # setting that did no work.
    return PAGE_RANGE_NUMBERING_ABSOLUTE, "identical"


def _assert_monotonic(pages: Sequence[OcrPage]) -> None:
    """The concatenation covers each page at most once, in order.

    Strictly increasing, and that is the whole check: a GAP is legal (marker
    emits no body for a blank page, and this backend has always dropped
    those), an overlap or a reversal is not. Cheap, and the only structural
    statement that can be made about a page sequence without a second copy of
    the document to compare it with."""
    previous: int | None = None
    for page in pages:
        if previous is not None and page.page_number <= previous:
            raise PageRangeNumberingError(
                f"the concatenated pages are not strictly increasing: page {page.page_number} "
                f"follows page {previous}. Two ranges produced the same page, or produced them "
                "out of order -- the concatenation is not this document."
            )
        previous = page.page_number


class _RangeCache:
    """The per-range outputs a stopped job leaves behind for its resume.

    One ``.md`` per finished range, one ``.md.sha256`` beside it, and a
    ``plan.json`` naming the run they belong to: the input's sha256, the
    budget and DPI and arguments they were produced under, and the ranges
    themselves. A cache whose plan disagrees with the run starting now is
    REMOVED rather than partly reused -- half a concatenation from another
    document is the one failure mode a resume can introduce that nothing
    downstream could catch.

    **A range file is never half-written and never half-trusted** (FIX V-1).
    The plan key rules out ranges from another run; the two guards here rule
    out a range from THIS run whose bytes are not all there:

    * every write goes through :func:`~trialerror.util.atomic.atomic_write_bytes`,
      so a process killed mid-write (the OOM kill this whole lane exists
      because of, an ``ENOSPC``, a power loss) leaves the previous state and
      an orphaned temp file rather than a truncated ``.md``;
    * every read re-hashes what it found and compares it with the digest
      written beside it. A file whose digest is missing or disagrees is
      DISCARDED and its range is re-run -- a cache is an optimisation, so
      the safe answer to a damaged one is the GPU minutes it was saving,
      never a shorter document. Without this, missing pages arrive as a
      GAP, and a gap is legal (blank pages emit no body), so nothing
      downstream would refuse and nothing would log.

    ``cache_dir=None`` is a backend running with no resume at all (the
    single-machine handler, every test that does not ask for one): every
    range is computed, nothing is written, and the class stays out of the
    way rather than making the caller branch.
    """

    def __init__(
        self,
        cache_dir: Path | None,
        *,
        input_path: Path,
        plan: Sequence[PageRange],
        backend: "RealMarkerOcrBackend",
    ):
        self.dir = Path(cache_dir) if cache_dir is not None else None
        if self.dir is None:
            return
        key = {
            "input_sha256": _sha256_file(Path(input_path)),
            "max_range_pixels": backend.max_range_pixels,
            "bounded_dpi": backend.bounded_dpi,
            "page_range_flag": backend.page_range_flag,
            "marker_version": backend.version,
            # FIX V-5: the INVOCATION, not only the plan. `--force_ocr`, an
            # OCR language, a model override -- every one of them changes
            # what a range's markdown is, and a job resumed after
            # `[ingest.ocr] marker_extra_args` changed would otherwise
            # concatenate ranges produced by two different commands, which
            # is the exact failure this key exists to prevent.
            "extra_args": list(backend.extra_args),
            "ranges": [[r.first, r.last] for r in plan],
        }
        path = self.dir / RealMarkerOcrBackend._PLAN_FILENAME
        existing = None
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None
        if existing != key:
            shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(key, sort_keys=True))

    def _digest_path(self, page_range: PageRange) -> Path:
        assert self.dir is not None
        return self.dir / f"{page_range.name}.sha256"

    def read(self, page_range: PageRange) -> str | None:
        if self.dir is None:
            return None
        path = self.dir / page_range.name
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            recorded = self._digest_path(page_range).read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - a cache that cannot be read is no cache
            return None
        except ValueError:  # pragma: no cover - decode is already replace-tolerant
            return None
        if recorded != hashlib.sha256(text.encode("utf-8")).hexdigest():
            # Not a refusal: the range is simply not in the cache any more.
            # Leaving the file would make the next resume ask the same
            # question again and get the same wrong answer.
            self._discard(page_range)
            return None
        return text

    def _discard(self, page_range: PageRange) -> None:
        assert self.dir is not None
        for path in ((self.dir / page_range.name), self._digest_path(page_range)):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - a cache that cannot be pruned is still safe
                pass

    def write(self, page_range: PageRange, text: str) -> str:
        """Cache ``text`` for ``page_range`` and return its sha256.

        The digest is the same one the result manifest records for this
        range, computed once: the caller gets it back rather than hashing
        the string a second line later.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.dir is None:
            return digest
        # The bytes, not ``atomic_write_text``: that helper normalizes line
        # endings, and a cached range has to read back byte for byte as the
        # range that was hashed -- otherwise a resumed document would differ
        # from the same document run in one go on a marker release that
        # emits CRLF.
        atomic_write_bytes(self.dir / page_range.name, text.encode("utf-8"))
        # AFTER the range itself, so a kill between the two writes leaves a
        # range with no digest, which :meth:`read` treats as absent.
        atomic_write_text(self._digest_path(page_range), digest + "\n")
        return digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


#: The code every surface uses for "this document is about to be OCR'd by a
#: stand-in". One string, so an agent parsing the warning does not have to
#: learn a second spelling of the same finding.
FAKE_STAGE_BACKEND_WARNING_CODE = "fake_stage_backend"


def resolve_stage_backend(raw_config: dict[str, Any] | None, stage: str) -> dict[str, Any]:
    """What backend ``stage`` is configured to run on, as data.

    Lane FB-1 item F10a. The fake OCR backend is a deterministic stand-in
    that produces plausible-looking text; a program that meant to configure
    a real one and mistyped the table name gets a document whose pages were
    invented, and nothing anywhere said so at the moment it was added.
    ``ingest add`` is exactly where the operator is looking, so that is where
    this has to be readable.

    Reports the configured name, whether the table is there at all (an
    absent table means the fake backend -- the same reading
    :func:`assert_real_backends_if_required` gives it), and whether this
    program requires the stage to be real (which is what makes the case
    merely notable rather than already refused)."""
    ingest = (raw_config or {}).get("ingest") or {}
    table = ingest.get(stage)
    table_absent = not isinstance(table, dict) or not table
    backend = "fake" if table_absent else str(table.get("backend", "fake"))
    return {
        "stage": stage,
        "backend": backend,
        "table_absent": table_absent,
        "fake": backend == "fake",
        "require_real": stage_requires_real(raw_config, stage),
        "require_real_key": require_real_key_for(raw_config, stage),
    }


def fake_stage_backend_warning(stage_backend: dict[str, Any] | None) -> dict[str, Any] | None:
    """The envelope ``warnings`` entry for a stage about to run on a
    stand-in, or ``None`` when there is nothing to warn about.

    In the envelope, never on stderr: the command SUCCEEDED, and a caller
    parsing JSON must not have to read a second stream to learn how. (The
    additive ``warnings`` block lane F-1 added to
    :mod:`trialerror.util.envelope` is exactly this case: succeeded, and
    something about HOW is material.)"""
    if not stage_backend or not stage_backend.get("fake"):
        return None
    stage = stage_backend["stage"]
    reason = (
        f"[ingest.{stage}] is absent from trialerror.toml, and an absent table means the fake backend"
        if stage_backend.get("table_absent")
        else f"[ingest.{stage}] backend = 'fake'"
    )
    return {
        "code": FAKE_STAGE_BACKEND_WARNING_CODE,
        "message": (
            f"the {stage} stage will run on the deterministic stand-in: {reason}. Its output is "
            "plausible-looking text that came from nowhere -- name a real backend before this "
            f"document's text is used as evidence. This program does not require the {stage} stage "
            f"to be real ({REQUIRE_REAL_GLOBAL_KEY} in [ingest], or {REQUIRE_REAL_STAGE_KEY} in "
            f"[ingest.{stage}], would refuse it outright)."
        ),
        "stage": stage,
        "backend": stage_backend.get("backend"),
        "config_table": f"[ingest.{stage}]",
        "require_real_key": stage_backend.get("require_real_key"),
    }


def load_ocr_backend(config: dict[str, Any]) -> OcrBackend:
    """``config`` = the program's ``trialerror.toml`` ``[ingest.ocr]`` table
    (a plain dict; ``trialerror.util.config.ProgramConfig`` hands these through
    generically per M0's own "fields read generically" note). Defaults to
    the fake backend when unconfigured, so a fresh scaffold works with no
    setup."""
    backend_name = config.get("backend", "fake")
    if backend_name == "fake":
        return FakeOcrBackend()
    if backend_name == OFFLOAD_BACKEND_NAME:
        # Lane L0-C / design v3 delta N2: the OCR model runs on DEV. This
        # marker carries the identity the handler's offload branch needs
        # and raises if anything tries to compute through it.
        return OffloadMarker("ocr", config)
    if backend_name == "marker":
        marker_single_exe = config.get("marker_single_exe")
        if not marker_single_exe:
            raise ValueError("ingest.ocr.backend = 'marker' requires ingest.ocr.marker_single_exe in trialerror.toml")
        return RealMarkerOcrBackend(
            marker_single_exe=marker_single_exe,
            version=config.get("marker_version", "1.10.2"),
            extra_args=config.get("marker_extra_args", []),
            timeout_s=config.get("timeout_s", DEFAULT_OCR_TIMEOUT_S),
            # Page-range chunking (lane e1e Part B). Read generically like
            # every other value in this table; a program that names none of
            # them gets the default budget, which is what sizes a large
            # scan's ranges without anybody configuring anything.
            page_range_flag=config.get("page_range_flag", DEFAULT_PAGE_RANGE_FLAG),
            max_range_pixels=config.get("max_range_pixels", DEFAULT_MAX_RANGE_PIXELS),
            bounded_dpi=config.get("bounded_dpi", DEFAULT_BOUNDED_DPI),
            page_range_numbering=config.get(
                "page_range_numbering", DEFAULT_PAGE_RANGE_NUMBERING
            ),
        )
    raise ValueError(
        f"unknown ingest.ocr.backend {backend_name!r} (choices: 'fake', 'marker', "
        f"'{OFFLOAD_BACKEND_NAME}')"
    )


# --------------------------------------------------------------------------
# Embed
# --------------------------------------------------------------------------


class EmbedBackend(Protocol):
    model_key: str
    dims: int

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]: ...

    def runnable(self) -> tuple[bool, str]:
        """``(True, "")`` when :meth:`embed_batch` can actually compute IN
        THIS PROCESS right now, ``(False, <reason>)`` when it cannot.

        The reason is operator-facing prose, not an exception: a backend
        whose model runs on another machine, whose runtime library is
        missing, or whose model file is absent is a KNOWN state of the
        world, and a retrieval surface asked to embed a query under one has
        to be able to say so in an envelope rather than raise through the
        caller (lane F-1; see
        :func:`trialerror.retrieve.engine.query_vector_or_reason`).

        A backend object that predates this method is treated as runnable
        by :func:`embed_backend_runnable` -- that helper, not a bare
        ``backend.runnable()``, is what callers use.
        """
        ...


class EmbedBackendNotRunnable(RuntimeError):
    """:meth:`EmbedBackend.embed_batch` was called on a backend whose
    :meth:`~EmbedBackend.runnable` is ``False``.

    The sibling of :class:`trialerror.offload.marker.OffloadNotRunnable` (which
    is the same fail-closed signal for the offload stand-in) for backends
    whose runtime is simply absent here: a missing library, a missing model
    file, a loader the local C runtime cannot satisfy. Carries the same
    reason string :meth:`~EmbedBackend.runnable` returns, so a caller that
    skipped the check still gets the sentence it would have been told."""


def embed_backend_runnable(backend: Any) -> tuple[bool, str]:
    """:meth:`EmbedBackend.runnable` on ``backend``, defaulting to
    ``(True, "")`` for an object that does not implement it.

    Every caller goes through this rather than calling the method directly,
    for two reasons. A backend object is allowed to predate the method (the
    protocol grew it in lane F-1, and test doubles/duck-typed stand-ins in
    this repo and in a program's own code do not all carry it), and a
    ``runnable()`` that itself raises must not be worse than one that
    returns ``False`` -- a probe is allowed to fail, and the failure IS the
    answer."""
    probe = getattr(backend, "runnable", None)
    if probe is None:
        return True, ""
    try:
        ok, reason = probe()
    except Exception as exc:  # noqa: BLE001 - a probe that raises is a 'no', with its own text as the reason
        return False, f"{type(exc).__name__}: {exc}"
    return bool(ok), str(reason or "")


#: Small on purpose -- tests embed real (tiny) fixture batches through this
#: backend and must stay fast; production config overrides via
#: ``ingest.embed.dims`` (the real Qwen3-4B backend's matryoshka-truncated
#: 2048, per the origin-project embed_backend.py C-0060 pin).
DEFAULT_FAKE_EMBED_DIMS = 16

#: Default subprocess timeout (seconds) for :class:`RealQwenEmbedBackend`
#: when ``trialerror.toml``'s ``[ingest.embed]`` table doesn't set its own
#: ``timeout_s`` -- FX-1, same rationale as :data:`DEFAULT_OCR_TIMEOUT_S`
#: above (see that constant's docstring for the timeout-vs-lease
#: TRIALERROR-DEV-NOTE, which applies identically here).
DEFAULT_EMBED_TIMEOUT_S = 1800


class FakeEmbedBackend:
    """Deterministic, hash-derived embedding: ``sha256(text)`` expanded into
    ``dims`` floats in ``[-1, 1)``, L2-normalized. Same text -> same vector,
    always -- no model load, no GPU, exercises the full embed/index/anchor
    pipeline shape without needing a real embedding model.

    ``delay_s`` is an internal seam (mirrors
    ``trialerror.util.atomic.atomic_write_bytes``'s own ``_chunk_size``/
    ``_on_chunk`` precedent: "internal seams used by the kill-mid-write
    test to slow the write down deterministically; production callers
    never pass them") letting the kill-mid-embed acceptance test observe
    partial per-batch progress deterministically without any GPU/model
    dependency -- default ``0.0`` is a no-op."""

    def __init__(self, *, dims: int = DEFAULT_FAKE_EMBED_DIMS, delay_s: float = 0.0, model_key: str | None = None):
        self.dims = dims
        self.delay_s = delay_s
        # namespaced by dims -- emb's PK is (chunk_sha256, model_key), so two
        # differently-dimensioned fake configs must never collide under one key.
        #
        # ``model_key`` overrides that default for ONE case, and it is the
        # reason the argument exists (lane F-1): the QUERY-side backend
        # (``[ingest.embed.query]``) must answer under the SAME model_key the
        # corpus was embedded under, or its vectors would be looked up in a
        # ``vec_chunks__<key>`` table nothing wrote. A program serving a
        # real-keyed corpus while embedding queries through this backend
        # therefore says ``backend = "fake"`` and inherits the document
        # side's key -- deliberately, and visibly, a fake answering for a
        # real key. Nothing on the DOCUMENT side passes it.
        self.model_key = str(model_key) if model_key else f"fake-{dims}"

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        if self.delay_s:
            time.sleep(self.delay_s)
        return [self._embed_one(t) for t in texts]

    def runnable(self) -> tuple[bool, str]:
        """Always: a hash is always computable, which is the whole point of
        this backend (design Section 13 flag F18's GPU-free default)."""
        return True, ""

    def _embed_one(self, text: str) -> list[float]:
        needed_bytes = self.dims * 4
        digest = b""
        counter = 0
        while len(digest) < needed_bytes:
            digest += hashlib.sha256(f"{text}::{counter}".encode("utf-8")).digest()
            counter += 1
        raw = struct.unpack(f"<{self.dims}I", digest[:needed_bytes])
        floats = [(v / 0xFFFFFFFF) * 2.0 - 1.0 for v in raw]
        norm = sum(f * f for f in floats) ** 0.5 or 1.0
        return [f / norm for f in floats]


#: Whether :class:`RealQwenEmbedBackend` keeps ONE long-lived driver
#: process per backend instance (``True``) or re-launches the one-shot
#: driver for every batch (``False``, the pre-fix behaviour, kept as the
#: fallback). Overridable per program via ``[ingest.embed] session``.
#:
#: TRIALERROR-DEV-NOTE (FX-S1, observed live): with the one-shot driver a
#: GPU worker running ``--batch-size 8`` re-launched ``python -c <driver>``
#: for every eight chunks, and each launch re-imported torch and re-loaded
#: a multi-gigabyte model from disk. A 217-chunk document meant 28 model
#: loads for 28 batches of real work. Session mode loads the model once
#: per backend instance and serves every later batch over an already-warm
#: process, which is why it is the DEFAULT rather than an opt-in.
DEFAULT_EMBED_SESSION_MODE = True

#: How much of a crashed driver's stderr the surfaced error carries (the
#: HEAD, so the first traceback line -- usually the real cause -- survives
#: even when the child then spewed pages of CUDA noise).
_DRIVER_STDERR_HEAD_CHARS = 2000

#: Bounded stderr retention for a session driver: the pump thread has to
#: drain the child's stderr pipe continuously (a full pipe would block the
#: child mid-embed), but a chatty driver must not grow this process's
#: memory without limit.
_DRIVER_STDERR_MAX_LINES = 400


class EmbedDriverCrashed(RuntimeError):
    """The resident embed driver exited (or failed its own request) mid
    session. Deliberately a ``RuntimeError`` subclass, NOT an
    :class:`EnvironmentalFailure`: a driver that dies on a given batch is a
    logic failure by the same reading the one-shot driver's non-zero exit
    always was (that path raised a plain ``RuntimeError``), so the ledger
    consumes a retry attempt exactly as it did before this change. Only a
    per-request TIMEOUT stays environmental."""


#: Every live :class:`_EmbedDriverSession`, so :func:`close_embed_driver_sessions`
#: (registered with :mod:`atexit`) can reap a driver whose owning backend
#: was never explicitly closed -- a leaked GPU process outliving its parent
#: is the one failure mode a resident-process design adds that the one-shot
#: design could not have.
_LIVE_SESSIONS: list["_EmbedDriverSession"] = []
_LIVE_SESSIONS_LOCK = threading.Lock()


def close_embed_driver_sessions() -> int:
    """Close every driver process this interpreter still holds open;
    returns how many were closed. Idempotent, never raises."""
    with _LIVE_SESSIONS_LOCK:
        sessions = list(_LIVE_SESSIONS)
    closed = 0
    for session in sessions:
        try:
            if session.close():
                closed += 1
        except Exception:  # noqa: BLE001 - interpreter shutdown must not raise
            pass
    return closed


atexit.register(close_embed_driver_sessions)


class _EmbedDriverSession:
    """One long-lived embed-driver subprocess, spoken to over a tiny
    line-delimited control channel on stdin/stdout while the PAYLOAD
    travels through files.

    **Why files for the payload and only a control line on the pipe.** The
    request is a whole batch of chunk text and the response is a batch of
    2048-dimensional vectors -- megabytes each way. Windows anonymous pipes
    are the awkward case for that (small default buffers, and a text-mode
    pipe re-encodes), whereas the one-shot driver's existing JSON-file
    hand-off is already proven at exactly this size. So the pipe carries
    only ``{"in": ..., "out": ...}`` in and ``{"ok": true}`` back -- a few
    dozen bytes, no framing subtleties, no risk of a partial read -- and
    the vectors keep the disk-to-disk path (design C-0007) they always had.

    **Reading with a timeout.** ``readline()`` cannot be interrupted, so
    stdout is drained by a pump thread into a ``queue.Queue`` and the
    request waits on ``Queue.get(timeout=...)``. stderr gets its own pump
    (into a bounded deque) rather than being left unread -- an unread
    stderr pipe fills and blocks the child mid-embed, which is precisely
    the hang the timeout exists to bound and would be a self-inflicted one.

    **Every line on this pipe is a control line.** That holds only because
    the driver hands fd 1 over to the protocol and sends its own output to
    stderr before it imports anything (see
    :data:`RealQwenEmbedBackend._SESSION_DRIVER_SOURCE`); a shared stdout
    would make one stray ``print`` in the model load fatal to every batch.
    So a line that does not parse here is a real protocol violation and is
    reported as one, not filtered out as noise that might be.

    Not a public class: :class:`RealQwenEmbedBackend` owns the lifecycle.
    """

    #: Protocol version, echoed in the handshake so a driver from a
    #: different vintage fails loudly at startup instead of subtly later.
    PROTOCOL = "trialerror-embed-session/1"

    def __init__(
        self,
        *,
        python_exe: str,
        module_dir: str,
        model_key: str,
        driver_source: str,
        timeout_s: float,
        log: Callable[[str], None] | None = None,
    ):
        self.python_exe = python_exe
        self.module_dir = module_dir
        self.model_key = model_key
        self.driver_source = driver_source
        self.timeout_s = timeout_s
        self.log = log
        self._proc: subprocess.Popen | None = None
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=_DRIVER_STDERR_MAX_LINES)
        self._threads: list[threading.Thread] = []
        self._closed = False
        #: One request at a time on the channel. The write and the read are
        #: two halves of ONE exchange on a shared pipe, so two threads
        #: interleaving them would cross responses -- each getting the
        #: other's vectors, silently, with no error anywhere. Not reachable
        #: from today's callers (the worker and the handler are both
        #: single-threaded per job), but session mode is what turns a
        #: stateless backend into a long-lived shared object, which is the
        #: shape that invites a caller to share one.
        self._exchange = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._closed

    def start(self) -> None:
        """Spawn the driver and wait for its readiness handshake -- which
        is the model load, so it is bounded by the same ``timeout_s`` a
        request is. A driver that cannot import ``embed_backend`` fails
        HERE, with its stderr head, instead of looking like a failure of
        whichever batch happened to be first."""
        cmd = [self.python_exe, "-c", self.driver_source, self.module_dir, self.model_key]
        self._proc = subprocess.Popen(  # noqa: S603 - config-pathed interpreter, same as the one-shot driver
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with _LIVE_SESSIONS_LOCK:
            _LIVE_SESSIONS.append(self)
        self._threads = [
            threading.Thread(target=self._pump_stdout, name="embed-driver-out", daemon=True),
            threading.Thread(target=self._pump_stderr, name="embed-driver-err", daemon=True),
        ]
        for t in self._threads:
            t.start()
        hello = self._read_line(what="the driver's readiness handshake")
        try:
            parsed = json.loads(hello)
        except ValueError as exc:
            self._die()
            raise EmbedDriverCrashed(
                f"embed driver's first line was not JSON ({hello!r}): {self._stderr_head()}"
            ) from exc
        if not parsed.get("ready"):
            self._die()
            raise EmbedDriverCrashed(
                f"embed driver refused to start: {parsed.get('error') or self._stderr_head()}"
            )
        if self.log is not None:
            self.log(f"driver started (model_key={self.model_key})")

    def close(self) -> bool:
        """Close stdin (the driver's own EOF-exits contract), reap the
        process, and drop this session from the live registry. Returns
        whether there was anything to close. Never raises."""
        proc, self._proc = self._proc, None
        self._closed = True
        with _LIVE_SESSIONS_LOCK:
            if self in _LIVE_SESSIONS:
                _LIVE_SESSIONS.remove(self)
        if proc is None:
            return False
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 - already-broken pipe on a dead child
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - includes TimeoutExpired
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        return True

    def _die(self) -> None:
        """Kill without pretending the driver might still be listening --
        used on every failure path so a wedged child never outlives the
        request it wedged on."""
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self.close()

    # -- plumbing ----------------------------------------------------------

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover - closed before the thread ran
            self._lines.put(None)
            return
        try:
            for line in proc.stdout:
                self._lines.put(line.strip())
        except Exception:  # noqa: BLE001 - a killed child closes the pipe under us
            pass
        finally:
            self._lines.put(None)  # EOF sentinel: the driver is gone

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:  # pragma: no cover - closed before the thread ran
            return
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass

    def _stderr_head(self, *, settle_s: float = 2.0) -> str:
        """The head of whatever the driver said on stderr.

        ``settle_s`` matters: the failure paths reach here the instant the
        STDOUT pipe hits EOF, and the stderr pump is a separate thread that
        may not have drained the child's last -- usually the only
        interesting -- lines yet. Joining it briefly is the difference
        between reporting the traceback and reporting an empty string,
        which on a GPU box is the difference between a diagnosis and
        another run."""
        for t in self._threads:
            if t.name == "embed-driver-err" and t.is_alive():
                t.join(timeout=settle_s)
        return "\n".join(self._stderr)[:_DRIVER_STDERR_HEAD_CHARS]

    def _read_line(self, *, what: str) -> str:
        try:
            line = self._lines.get(timeout=self.timeout_s)
        except queue.Empty:
            self._die()
            raise EnvironmentalFailure(
                f"real embed driver timed out after {self.timeout_s}s waiting for {what} "
                "(GPU-bound embedding; raise ingest.embed.timeout_s in trialerror.toml if this "
                "is expected for large batches)"
            ) from None
        if line is None:
            stderr_head = self._stderr_head()
            self._die()
            raise EmbedDriverCrashed(
                f"real embed driver exited while waiting for {what}: {stderr_head}"
            )
        return line

    # -- the one request shape ---------------------------------------------

    def request(self, texts: Sequence[str], *, kind: str) -> list[list[float]]:
        """One batch, one exchange. Serialized on :attr:`_exchange`: the
        write and the read are two halves of the same conversation on one
        pipe, and two threads interleaving them would swap each other's
        vectors with nothing raising."""
        import tempfile

        proc = self._proc
        if proc is None:  # pragma: no cover - callers check .alive first
            raise EmbedDriverCrashed("embed driver session is not started")
        with self._exchange, tempfile.TemporaryDirectory(prefix="trialerror-embed-") as tmp:
            in_path = Path(tmp) / "in.json"
            out_path = Path(tmp) / "out.json"
            in_path.write_text(
                json.dumps({"texts": list(texts), "kind": kind}, ensure_ascii=False), encoding="utf-8"
            )
            control = json.dumps({"in": str(in_path), "out": str(out_path)}, ensure_ascii=False)
            try:
                assert proc.stdin is not None
                proc.stdin.write(control + "\n")
                proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001 - broken pipe == the driver died
                stderr_head = self._stderr_head()
                self._die()
                raise EmbedDriverCrashed(
                    f"real embed driver closed its input mid-session ({exc}): {stderr_head}"
                ) from exc
            line = self._read_line(what=f"a batch of {len(texts)} text(s)")
            try:
                response = json.loads(line)
            except ValueError as exc:
                stderr_head = self._stderr_head()
                self._die()
                raise EmbedDriverCrashed(
                    f"real embed driver answered with non-JSON ({line!r}): {stderr_head}"
                ) from exc
            if not response.get("ok"):
                # A per-request failure the driver itself reported: the
                # process is still healthy, so it is NOT torn down -- the
                # next batch reuses it, exactly as the one-shot driver's
                # non-zero exit only ever failed its own batch.
                raise EmbedDriverCrashed(
                    f"real embed driver failed a batch of {len(texts)} text(s): "
                    f"{str(response.get('error'))[:_DRIVER_STDERR_HEAD_CHARS]}"
                )
            out = json.loads(out_path.read_text(encoding="utf-8"))
        return out["vectors"]


class RealQwenEmbedBackend:
    """Shells out to the real Qwen3-Embedding backend
    (``research/tools/embeddings_local/embed_backend.py``'s
    ``load_backend(name).embed_batch(texts, kind=...)``) via a subprocess
    running in THAT venv (``python_exe``, config-pathed -- never
    hardcoded), so this process never needs torch/sentence-transformers
    itself installed.

    Two protocols, one code path for the caller:

    - **session mode** (:data:`DEFAULT_EMBED_SESSION_MODE`, the default):
      ONE driver process per backend instance, started lazily on the first
      :meth:`embed_batch` and reused for every later call, which loads the
      model exactly once. Requests ride :class:`_EmbedDriverSession`'s
      control channel (see that class for why the payload stays on disk).
      The process is closed by :meth:`close`, by using the backend as a
      context manager, or -- as a backstop -- at interpreter exit.
    - **one-shot mode** (``[ingest.embed] session = false``): the original
      protocol, unchanged -- a fresh ``python -c`` per batch that reads one
      temp JSON file and writes another. Kept as the fallback for a driver
      or a machine where a resident process is unwelcome.

    Disk-to-disk in both (design C-0007: "page text never transits the
    orchestrator's context"): the batch is written to a temp JSON file, the
    driver reads it, imports ``embed_backend`` from ``module_dir`` (added
    to ``sys.path``), embeds, and writes vectors back to a second temp JSON
    file this process reads.

    ``timeout_s`` (``trialerror.toml`` ``[ingest.embed].timeout_s``, default
    :data:`DEFAULT_EMBED_TIMEOUT_S`) bounds one request -- in session mode
    that is one batch (or the startup model load), in one-shot mode the
    whole subprocess. FX-1: a hung/wedged embed driver raises
    :class:`EnvironmentalFailure` (job re-queued, retry attempt NOT
    consumed) instead of blocking the handler forever.

    **Crash-then-restart.** A driver that dies mid-session surfaces its
    stderr head on the call that hit it (:class:`EmbedDriverCrashed`, a
    ``RuntimeError`` -- the ledger consumes an attempt, same as a one-shot
    non-zero exit always did) and the dead session is dropped. The NEXT
    call starts a fresh driver; no retry happens inside the failing call,
    because re-running a batch that just killed the model is how a crash
    loop burns GPU minutes rather than surfacing.

    TRIALERROR-DEV-NOTE: not exercised against a live GPU in this build session
    (no GPU test in the default suite) -- live verification is the stated
    M8/integration-session follow-up (design Section 13 F18/M15).
    """

    #: The session driver: imports ``embed_backend`` and loads the model
    #: ONCE, announces readiness, then serves ``{"in","out"}`` control
    #: lines until stdin reaches EOF. Run via ``python -c`` so no extra
    #: file ships outside this module, exactly like the one-shot driver.
    #: ``argv[1]`` is ``module_dir``, ``argv[2]`` the ``model_key``.
    #:
    #: **The control channel is private (VERIFY V-1).** Session mode's one
    #: real weakness against the one-shot protocol -- which read only
    #: ``out.json`` plus an exit code and was therefore immune by
    #: construction -- is that the protocol shares fd 1 with anything the
    #: model load decides to print. A single ``Loading checkpoint shards``
    #: line on stdout would derail every batch AND make the driver reload
    #: per batch, i.e. strictly worse than the bug this fix removes, on the
    #: one code path this repo does not own (``embed_backend.py`` lives
    #: outside it). So the FIRST thing the driver does, before importing
    #: anything: take a private duplicate of fd 1 for the protocol and
    #: point fd 1 (and ``sys.stdout``) at stderr. Python ``print()``,
    #: progress bars and native libraries writing straight to fd 1 all land
    #: in the diagnostic stream that already feeds the stderr head, where
    #: they are useful, instead of in the channel, where they are fatal.
    _SESSION_DRIVER_SOURCE = (
        "import json, os, sys, traceback\n"
        "_control = os.fdopen(os.dup(1), 'w', encoding='utf-8', newline='\\n')\n"
        "os.dup2(2, 1)\n"
        "sys.stdout = sys.stderr\n"
        "try:\n"
        "    sys.stderr.reconfigure(encoding='utf-8', errors='replace')\n"
        "except Exception:\n"
        "    pass\n"
        "def _say(obj):\n"
        "    _control.write(json.dumps(obj) + '\\n')\n"
        "    _control.flush()\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "try:\n"
        "    from embed_backend import load_backend\n"
        "    backend = load_backend(sys.argv[2])\n"
        "except Exception:\n"
        "    _say({'ready': False, 'error': traceback.format_exc()[:2000]})\n"
        "    raise SystemExit(1)\n"
        "_say({'ready': True})\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    try:\n"
        "        req = json.loads(line)\n"
        "        with open(req['in'], 'r', encoding='utf-8') as f:\n"
        "            payload = json.load(f)\n"
        "        vecs = backend.embed_batch(payload['texts'], kind=payload.get('kind', 'document'))\n"
        "        out = {'vectors': vecs.tolist(), 'dims': int(vecs.shape[-1])}\n"
        "        with open(req['out'], 'w', encoding='utf-8') as f:\n"
        "            json.dump(out, f)\n"
        "        resp = {'ok': True}\n"
        "    except Exception:\n"
        "        resp = {'ok': False, 'error': traceback.format_exc()[:2000]}\n"
        "    _say(resp)\n"
    )

    _DRIVER_SOURCE = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from embed_backend import load_backend\n"
        "with open(sys.argv[1], 'r', encoding='utf-8') as f:\n"
        "    payload = json.load(f)\n"
        "backend = load_backend(payload['model_key'])\n"
        "vecs = backend.embed_batch(payload['texts'], kind=payload.get('kind', 'document'))\n"
        "out = {'vectors': vecs.tolist(), 'dims': int(vecs.shape[-1])}\n"
        "with open(sys.argv[3], 'w', encoding='utf-8') as f:\n"
        "    json.dump(out, f)\n"
    )

    def __init__(
        self,
        *,
        python_exe: str,
        module_dir: str,
        model_key: str = "qwen3-4b",
        dims: int = 2048,
        timeout_s: float = DEFAULT_EMBED_TIMEOUT_S,
        session: bool = DEFAULT_EMBED_SESSION_MODE,
        log: Callable[[str], None] | None = None,
    ):
        self.python_exe = python_exe
        self.module_dir = module_dir
        self.model_key = model_key
        self.dims = dims
        self.timeout_s = timeout_s
        self.session = session
        #: Set by a caller that wants the driver's lifecycle narrated (the
        #: DEV offload worker's ``--format text`` log does: "driver started"
        #: exactly once per worker run is the whole point of session mode).
        self.log = log
        self._session: _EmbedDriverSession | None = None

    # -- session lifecycle -------------------------------------------------

    def close(self) -> None:
        """Shut the resident driver down. Safe to call repeatedly, safe in
        one-shot mode (a no-op), and safe on a backend that never embedded
        anything. Every long-lived caller should call it -- the offload
        worker does, in a ``finally``; :func:`close_embed_driver_sessions`
        is the interpreter-exit backstop, not the intended path."""
        session, self._session = self._session, None
        if session is not None:
            session.close()

    def __enter__(self) -> "RealQwenEmbedBackend":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def runnable(self) -> tuple[bool, str]:
        """Always ``(True, "")``.

        Deliberately NOT a probe of ``python_exe``/``module_dir``: this
        backend's whole contract is that the model lives behind a
        subprocess it launches, the launch is where every real failure
        surfaces (with the driver's own stderr head attached -- see
        :class:`EmbedDriverCrashed`), and a pre-flight ``os.path.exists``
        would answer a different question than the one asked. A caller that
        wants to know whether the driver comes up runs a batch."""
        return True, ""

    def _live_session(self) -> _EmbedDriverSession:
        """The running driver, starting one if there is none. THIS is the
        "restart once" the crash path relies on: a dead session was already
        dropped by whichever call observed the crash, so the next call
        lands here and gets a fresh process."""
        if self._session is not None and self._session.alive:
            return self._session
        if self._session is not None:
            self._session.close()
        session = _EmbedDriverSession(
            python_exe=self.python_exe,
            module_dir=self.module_dir,
            model_key=self.model_key,
            driver_source=self._SESSION_DRIVER_SOURCE,
            timeout_s=self.timeout_s,
            log=self.log,
        )
        self._session = session
        try:
            session.start()
        except BaseException:
            self._session = None
            raise
        return session

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        if self.session:
            session = self._live_session()
            try:
                return session.request(texts, kind=kind)
            except EmbedDriverCrashed:
                # A dead process must not be handed to the next batch; a
                # driver that merely FAILED one request is still alive and
                # stays (see _EmbedDriverSession.request's own note).
                if not session.alive:
                    self._session = None
                raise
            except EnvironmentalFailure:
                self._session = None  # _read_line already killed it
                raise
        return self._embed_batch_one_shot(texts, kind=kind)

    def _embed_batch_one_shot(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        """The pre-session protocol, unchanged: one driver process per
        batch. Reachable via ``[ingest.embed] session = false``."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="trialerror-embed-") as tmp:
            in_path = Path(tmp) / "in.json"
            out_path = Path(tmp) / "out.json"
            in_path.write_text(
                json.dumps({"texts": list(texts), "kind": kind, "model_key": self.model_key}, ensure_ascii=False),
                encoding="utf-8",
            )
            # FX-2 (IMPL_REVIEW_C_ops.md N-4): encoding="utf-8" so the
            # driver's own stderr (UTF-8; a torch/transformers traceback can
            # carry non-ASCII) is never decoded as the Windows ANSI
            # codepage; errors="replace" for the same reason
            # RealMarkerOcrBackend.run carries it -- a decode bug must never
            # masquerade as (or pre-empt reporting) the real diagnostics.
            try:
                result = subprocess.run(
                    [self.python_exe, "-c", self._DRIVER_SOURCE, str(in_path), self.module_dir, str(out_path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                # FX-1: see RealMarkerOcrBackend.run's identical arm --
                # subprocess.run kills the child on timeout, and
                # EnvironmentalFailure (not RuntimeError) routes this
                # through trialerror.jobs.worker.run_one's environmental-failure
                # arm so the ledger re-queues without consuming an attempt.
                raise EnvironmentalFailure(
                    f"real embed driver timed out after {self.timeout_s}s for a batch of {len(texts)} "
                    "text(s) (GPU-bound embedding; raise ingest.embed.timeout_s in trialerror.toml if this "
                    "is expected for large batches)"
                ) from exc
            if result.returncode != 0:
                raise RuntimeError(f"real embed driver exited {result.returncode}: {result.stderr[-2000:]}")
            out = json.loads(out_path.read_text(encoding="utf-8"))
        return out["vectors"]


def load_embed_backend(config: dict[str, Any], *, table: str = "ingest.embed") -> EmbedBackend:
    """``config`` = the program's ``trialerror.toml`` ``[ingest.embed]`` table.
    Defaults to the fake backend when unconfigured.

    ``table`` is the config table ``config`` CAME from, used only in refusal
    messages. It defaults to the document side's table, and
    :func:`load_query_embed_backend` passes :data:`QUERY_EMBED_TABLE` -- so a
    query-side table naming an unknown backend points the operator at the
    table they actually edited instead of at ``[ingest.embed]``."""
    backend_name = config.get("backend", "fake")
    if backend_name == "fake":
        return FakeEmbedBackend(
            dims=config.get("dims", DEFAULT_FAKE_EMBED_DIMS),
            delay_s=config.get("delay_s", 0.0),  # test-only seam -- see FakeEmbedBackend's docstring
        )
    if backend_name == LLAMA_CPP_BACKEND_NAME:
        # Lane F-1 item C: the CPU query-side encoder. ``model_key`` is
        # demanded rather than defaulted for the same reason the offload
        # marker demands it -- ``emb``/``vec_chunks__<key>`` are keyed by it,
        # and a defaulted key silently opens a parallel key space. On the
        # QUERY side ``load_query_embed_backend`` has already filled it in
        # from the document side, which is the normal path here.
        model_path = config.get("model_path")
        if not model_path:
            raise ValueError(
                f"{table}.backend = {LLAMA_CPP_BACKEND_NAME!r} requires model_path "
                "(the GGUF file to load) in the same table"
            )
        model_key = config.get("model_key")
        if not model_key:
            raise ValueError(
                f"{table}.backend = {LLAMA_CPP_BACKEND_NAME!r} requires model_key -- emb rows "
                "and vec_chunks__<model_key> are keyed by it, so it cannot be defaulted"
            )
        return LlamaCppEmbedBackend(
            model_path=str(model_path),
            model_key=str(model_key),
            dims=int(config.get("dims", 2048)),
            native_dims=int(config.get("native_dims", DEFAULT_LLAMA_NATIVE_DIMS)),
            n_ctx=int(config.get("n_ctx", DEFAULT_LLAMA_N_CTX)),
            n_threads=config.get("n_threads"),
            # Absent, the backend defaults it to min(n_threads, cgroup quota)
            # -- see LlamaCppEmbedBackend's docstring for why the library's
            # own default is the wrong number inside a container.
            n_threads_batch=config.get("n_threads_batch"),
            pooling=str(config.get("pooling", "last")),
            query_prompt=str(config.get("query_prompt", DEFAULT_QUERY_PROMPT)),
        )
    if backend_name == LLAMA_SERVER_BACKEND_NAME:
        # Lane F-1b item 2: the sidecar client. No ``model_path`` here -- this
        # process loads no weights at all; ``tokenizer_model_path`` (the
        # vocabulary used for pre-truncation) is optional at construction on
        # purpose, so a program whose GGUF is not mounted yet still resolves
        # and gets a doctor line naming the key instead of a config error on
        # every command. ``model_key`` is demanded for the same reason every
        # other real backend demands it: emb/vec_chunks__<key> are keyed by it.
        model_key = config.get("model_key")
        if not model_key:
            raise ValueError(
                f"{table}.backend = {LLAMA_SERVER_BACKEND_NAME!r} requires model_key -- emb rows "
                "and vec_chunks__<model_key> are keyed by it, so it cannot be defaulted"
            )
        return LlamaServerEmbedBackend(
            model_key=str(model_key),
            dims=int(config.get("dims", 2048)),
            url=str(config.get("url", DEFAULT_LLAMA_SERVER_URL)),
            timeout_s=float(config.get("timeout_s", DEFAULT_LLAMA_SERVER_TIMEOUT_S)),
            native_dims=int(config.get("native_dims", DEFAULT_LLAMA_NATIVE_DIMS)),
            n_ctx=int(config.get("n_ctx", DEFAULT_LLAMA_SERVER_N_CTX)),
            query_prompt=str(config.get("query_prompt", DEFAULT_QUERY_PROMPT)),
            tokenizer_model_path=config.get("tokenizer_model_path"),
            # Optional, and only ever used to spell the remedy in a refusal:
            # which [sidecars.<name>] serves this URL.
            sidecar_name=config.get("sidecar_name"),
        )
    if backend_name == OFFLOAD_BACKEND_NAME:
        # Lane L0-C: the embedding model runs on DEV. ``model_key``/``dims``
        # still have to be known HERE -- ``emb`` rows and the
        # ``vec_chunks__<model_key>`` table are keyed by them, and
        # ``run_index`` reads them off this object -- so the marker demands
        # them from the config rather than defaulting them.
        return OffloadMarker("embed", config)
    python_exe = config.get("python_exe")
    module_dir = config.get("module_dir")
    if not python_exe or not module_dir:
        raise ValueError(
            f"{table}.backend = {backend_name!r} requires {table}.python_exe and "
            f"{table}.module_dir in trialerror.toml"
        )
    return RealQwenEmbedBackend(
        python_exe=python_exe,
        module_dir=module_dir,
        model_key=backend_name,
        dims=config.get("dims", 2048),
        timeout_s=config.get("timeout_s", DEFAULT_EMBED_TIMEOUT_S),
        # ``[ingest.embed] session`` -- default TRUE (one resident driver,
        # one model load). ``false`` restores the pre-fix one-process-per-
        # batch protocol; nothing else in the config changes with it, which
        # is what makes it a safe escape hatch rather than a second mode to
        # keep in your head.
        session=bool(config.get("session", DEFAULT_EMBED_SESSION_MODE)),
    )


# --------------------------------------------------------------------------
# Query-side embed backend (lane F-1)
# --------------------------------------------------------------------------

#: The config table a program uses to name a SEPARATE backend for embedding
#: QUERY-TIME text (a search query, a hypothesis statement, an idea
#: statement), spelled once here so every message that has to tell an
#: operator where to change it spells it the same way.
QUERY_EMBED_TABLE = "ingest.embed.query"

#: ``QUERY_EMBED_TABLE``'s key inside the already-loaded ``[ingest.embed]``
#: dict -- TOML nests ``[ingest.embed.query]`` there.
QUERY_EMBED_SUBTABLE_KEY = "query"

#: The default ``[ingest.embed.query] backend`` value: "whatever the
#: document side uses". Resolution then returns the document backend
#: OBJECT itself, so an unconfigured program behaves byte-for-byte as it
#: did before this table existed.
SAME_AS_DOCUMENT = "same"

#: What :func:`query_embed_backend_name` reports for a ``[ingest.embed.query]``
#: that is not a table at all -- a config no backend can be built from, which
#: resolution refuses. A NAME rather than an exception because the reporter's
#: one caller is a doctor detail field and the refusal itself is reported by
#: the same check's message.
QUERY_EMBED_BACKEND_INVALID = "invalid"


class QueryEmbedBackendMismatchError(ValueError):
    """The configured query-side backend answers under a different
    ``model_key``/``dims`` than the document side.

    Fail-closed, and the one refusal this module cares most about: a query
    vector produced under key B looked up against ``vec_chunks__A`` does
    not error, it silently ranks 16,000 chunks by a number that means
    nothing. A ``ValueError`` subclass so the callers that already treat a
    bad config table as a ``ValueError`` keep doing so."""


def query_embed_backend_name(config: dict[str, Any]) -> str:
    """The ``[ingest.embed.query] backend`` value for an ``[ingest.embed]``
    table (:data:`SAME_AS_DOCUMENT` when the sub-table or the key is
    absent)."""
    sub = (config or {}).get(QUERY_EMBED_SUBTABLE_KEY) or {}
    if not isinstance(sub, dict):
        # NOT "same": `load_query_embed_backend` refuses this config, and a
        # doctor line reporting `query_backend: "same"` for a table that
        # cannot resolve names a backend nothing would ever build.
        return QUERY_EMBED_BACKEND_INVALID
    return str(sub.get("backend", SAME_AS_DOCUMENT) or SAME_AS_DOCUMENT)


def load_query_embed_backend(config: dict[str, Any]) -> EmbedBackend:
    """The backend that embeds QUERY-TIME text, from the program's
    ``[ingest.embed]`` table (``config``) and its optional
    ``[ingest.embed.query]`` sub-table.

    Why this exists at all: the DOCUMENT side of a two-machine program is
    ``backend = "offload"`` -- a stand-in whose ``embed_batch`` raises by
    design, because the model runs on the other machine. Retrieval, though,
    has to embed text that no document stage will ever see (the query
    itself), and it has to do it HERE. So the query side gets its own
    backend choice, and the default is the honest one: ``"same"``, the
    document backend object itself, which keeps a program that never
    configures this table on exactly the path it was on.

    Both sides must agree on ``model_key`` AND ``dims``, or this raises
    :class:`QueryEmbedBackendMismatchError` naming both keys -- see that
    class for why a mismatch is worse than a refusal. The sub-table
    therefore does not usually need to state either one: they DEFAULT to
    the document side's values (which is what makes
    ``[ingest.embed.query] backend = "llama_cpp"`` plus a ``model_path`` a
    complete configuration), and stating them differently is the case that
    refuses.
    """
    document = load_embed_backend(config)
    sub = (config or {}).get(QUERY_EMBED_SUBTABLE_KEY) or {}
    if not isinstance(sub, dict):
        raise QueryEmbedBackendMismatchError(
            f"[{QUERY_EMBED_TABLE}] must be a table (got {type(sub).__name__}); "
            f'write e.g. [{QUERY_EMBED_TABLE}] with backend = "{SAME_AS_DOCUMENT}"'
        )
    name = str(sub.get("backend", SAME_AS_DOCUMENT) or SAME_AS_DOCUMENT)
    if name == SAME_AS_DOCUMENT:
        return document

    query_config = dict(sub)
    query_config["backend"] = name
    # The identity fields default to the document side's -- see the
    # docstring. ``setdefault`` (not an overwrite) so a config that states
    # them still gets CHECKED against the document side below rather than
    # silently corrected.
    query_config.setdefault("model_key", document.model_key)
    query_config.setdefault("dims", document.dims)

    if name == "fake":
        # Built here rather than through ``load_embed_backend``, on purpose:
        # that loader's fake branch deliberately does NOT read
        # ``model_key`` from its table (an offload-era ``model_key`` line
        # left behind after a backend change must not steer the DOCUMENT
        # side's key -- see OPERATOR_GUIDE's ``embedding_stale`` note), and
        # this is the one place where a fake backend is SUPPOSED to answer
        # under an inherited key.
        backend: EmbedBackend = FakeEmbedBackend(
            dims=int(query_config["dims"]),
            model_key=str(query_config["model_key"]),
            delay_s=float(query_config.get("delay_s", 0.0)),
        )
    else:
        backend = load_embed_backend(query_config, table=QUERY_EMBED_TABLE)

    if str(backend.model_key) != str(document.model_key) or int(backend.dims) != int(document.dims):
        raise QueryEmbedBackendMismatchError(
            f"[{QUERY_EMBED_TABLE}] backend = {name!r} resolves to "
            f"model_key={backend.model_key!r} dims={int(backend.dims)}, but [ingest.embed] "
            f"(the key this program's stored vectors carry) resolves to "
            f"model_key={document.model_key!r} dims={int(document.dims)}. A query vector from a "
            f"different model_key would be ranked against vec_chunks__{document.model_key} rows "
            f"it shares no space with, which reads as a working search returning meaningless "
            f"order -- so this refuses instead. Make [{QUERY_EMBED_TABLE}] model_key/dims match "
            f"[ingest.embed], or drop them and inherit."
        )
    return backend


# --------------------------------------------------------------------------
# llama.cpp embed backend (lane F-1 item C) -- a CPU query-side encoder
# --------------------------------------------------------------------------

#: The ``[ingest.embed] backend`` / ``[ingest.embed.query] backend`` value
#: that selects :class:`LlamaCppEmbedBackend`.
LLAMA_CPP_BACKEND_NAME = "llama_cpp"

#: The retrieval instruction a Qwen3-Embedding model expects in FRONT of a
#: query (and never in front of a passage). Concatenated PLAINLY with the
#: query text -- no space, no newline added between them; the trailing
#: ``Query:`` is the separator, and adding a second one changes the
#: tokenisation of the first content token.
DEFAULT_QUERY_PROMPT = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
)

#: Native width of the encoder's own output, BEFORE the matryoshka slice to
#: the width a program actually stores (``dims``). Stated in config rather
#: than inferred, because a wrong value here is the difference between
#: normalising over the whole vector and normalising over part of it -- and
#: the slice-then-renormalise order only reproduces the reference recipe if
#: the first normalisation happened at the full width.
DEFAULT_LLAMA_NATIVE_DIMS = 2560

#: Context window the resident model is built with, and (minus one token of
#: EOS room) the hard truncation point for any single input.
DEFAULT_LLAMA_N_CTX = 2048

#: Where a Linux container's CPU quota is readable from. cgroup v2 puts one
#: file here (``cpu.max``); v1 puts a pair under a ``cpu`` (or
#: ``cpu,cpuacct``) controller directory. Parameterised on the reader below
#: so the parser can be tested against a fixture tree instead of the host.
_CGROUP_ROOT = "/sys/fs/cgroup"

#: Where this process's OWN cgroup path is readable from. Under a private
#: cgroup namespace (the ordinary container case) it reads ``0::/`` and the
#: root IS this process's cgroup; with ``cgroupns=host`` the process sits
#: several levels below the root, the root carries no ``cpu.max``, and
#: reading the root alone falls back to ``os.cpu_count()`` -- the exact
#: number whose use is the bug (VERIFY_f1b-sidecar.md V-9).
_PROC_SELF_CGROUP = "/proc/self/cgroup"


def cgroup_cpu_quota(
    *, cgroup_root: str | Path | None = None, proc_self_cgroup: str | Path | None = None
) -> int:
    """How many CPUs this process is actually ALLOWED to use -- the cgroup's
    quota, not the CPUs it can see -- falling back to ``os.cpu_count()``
    when nothing caps it or nothing is readable.

    The two numbers differ on every container, and the difference is the
    whole reason this function exists (see
    :class:`LlamaCppEmbedBackend`'s ``n_threads_batch`` note): a thread pool
    sized by the VISIBLE CPU count inside a quota smaller than it does not
    run slightly hot, it spin-waits at every synchronisation barrier and
    costs an order of magnitude.

    Readings, per directory, in order:

    - **v2** ``<dir>/cpu.max``: ``"<quota_us> <period_us>"``, or
      ``"max <period_us>"`` for no limit. ``quota/period`` is the CPU
      allowance (``"1000000 100000"`` = 10 CPUs).
    - **v1** ``<dir>[/cpu|/cpu,cpuacct]/cpu.cfs_quota_us`` plus
      ``cpu.cfs_period_us``; a quota of ``-1`` means no limit.
    - anything unreadable, malformed, or explicitly unlimited -> the next
      directory, and the fallback once they run out.

    WHICH directories, and why more than one (V-9): the cgroup root is this
    process's own cgroup only under a private cgroup namespace. With
    ``cgroupns=host`` the process sits below the root -- the root has no
    ``cpu.max`` at all -- so the reader starts at the path
    ``/proc/self/cgroup`` reports for this process and walks UP to the root,
    taking the first limit it finds. That is the quota that binds in the
    ordinary nested case; a parent tighter than its child is rare enough, and
    reporting a slightly generous number is the same failure the fallback
    already has.

    A fractional allowance FLOORS (``1.5`` CPUs -> ``1``), never rounds up:
    the value's only consumer is a thread count, and half a CPU cannot host
    the extra thread rounding up would create. The result is never below 1.
    """
    fallback = os.cpu_count() or 1
    root = Path(cgroup_root) if cgroup_root is not None else Path(_CGROUP_ROOT)
    own = _own_cgroup_paths(proc_self_cgroup)

    for base in _cgroup_candidates(root, own):
        cpus = _cgroup_quota_in(base)
        if cpus is None:  # nothing readable here, or explicitly unlimited
            continue
        return max(1, cpus)
    return fallback


def _own_cgroup_paths(proc_self_cgroup: str | Path | None = None) -> dict[str, str]:
    """This process's cgroup path per hierarchy, from ``/proc/self/cgroup``.

    ``{"": "/nested/path"}`` for v2 (the ``0::<path>`` line) and
    ``{"cpu": ...}`` / ``{"cpu,cpuacct": ...}`` for the v1 controllers this
    reader looks under. ``{}`` off Linux or when the file cannot be read,
    which puts the reader back on the root-only behaviour it had before."""
    path = Path(proc_self_cgroup) if proc_self_cgroup is not None else Path(_PROC_SELF_CGROUP)
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - no /proc, or not readable: root only
        return {}
    found: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _hierarchy, controllers, rel = parts
        rel = rel.strip()
        if not rel.startswith("/"):
            continue
        if controllers == "":  # the v2 unified hierarchy
            found[""] = rel
            continue
        for controller in controllers.split(","):
            if controller in {"cpu", "cpuacct"}:
                found[controllers] = rel
                break
    return found


def _cgroup_candidates(root: Path, own: dict[str, str]) -> list[Path]:
    """Every directory that might carry this process's CPU limit, nearest
    first: its own cgroup, each ancestor up to the root, and the v1
    controller directories -- with the plain root last, which is what the
    pre-V-9 reader looked at and what a fixture tree in a test provides."""
    candidates: list[Path] = []

    def _add_chain(base: Path, rel: str) -> None:
        current = base / rel.lstrip("/") if rel not in {"", "/"} else base
        while True:
            if current not in candidates:
                candidates.append(current)
            if current == base or base not in current.parents:
                break
            current = current.parent

    v2_rel = own.get("", "/")
    _add_chain(root, v2_rel)
    for controllers, rel in own.items():
        if controllers == "":
            continue
        for directory in (root / controllers, root / "cpu", root / "cpu,cpuacct"):
            _add_chain(directory, rel)
    for directory in (root / "cpu", root / "cpu,cpuacct", root):
        if directory not in candidates:
            candidates.append(directory)
    return candidates


def _cgroup_quota_in(base: Path) -> int | None:
    """The CPU allowance one cgroup directory declares, or ``None`` for
    "nothing readable here, or explicitly unlimited"."""
    try:
        fields = (base / "cpu.max").read_text(encoding="utf-8").split()
    except Exception:  # noqa: BLE001 - not v2, or not readable: try v1 below
        fields = []
    if fields:
        if fields[0].strip() == "max":
            return None  # unlimited at this level
        try:
            quota_us = float(fields[0])
            period_us = float(fields[1]) if len(fields) > 1 else 100000.0
        except ValueError:
            return None
        if quota_us <= 0 or period_us <= 0:
            return None
        return int(quota_us // period_us)

    try:
        quota_us = float((base / "cpu.cfs_quota_us").read_text(encoding="utf-8").strip())
        period_us = float((base / "cpu.cfs_period_us").read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001 - wrong layout or unreadable
        return None
    if quota_us <= 0 or period_us <= 0:
        return None  # -1 = no limit set on this controller
    return int(quota_us // period_us)


def embed_backend_runtime_details(backend: Any) -> dict[str, Any]:
    """What a backend says about the runtime it would embed through -- the
    in-process encoder's effective thread settings, the sidecar client's URL
    and last health reading -- as a plain dict for a doctor detail field.

    ``{}`` for a backend that does not implement ``runtime_details``, and
    ``{}`` rather than a raised exception for one whose implementation
    fails: the same reading :func:`embed_backend_runnable` takes of a
    ``runnable()`` that raises. A detail field is never worth breaking a
    check over."""
    probe = getattr(backend, "runtime_details", None)
    if probe is None:
        return {}
    try:
        details = probe()
    except Exception as exc:  # noqa: BLE001 - a detail field, not a verdict
        return {"runtime_details_error": f"{type(exc).__name__}: {exc}"}
    return dict(details) if isinstance(details, dict) else {}

#: ``llama.cpp``'s own ``enum llama_pooling_type`` values, by the name a
#: config uses. Looked up on the installed package FIRST
#: (:func:`_llama_pooling_type`) -- these literals are the fallback for a
#: build that does not export the constants, not the primary source.
_LLAMA_POOLING_TYPES = {
    "unspecified": -1,
    "none": 0,
    "mean": 1,
    "cls": 2,
    "last": 3,
    "rank": 4,
}

#: C0 controls and DEL, plus the C1 block, minus TAB and NEWLINE: what is
#: stripped out of any text before it reaches an embedding model. A stray
#: ``\x00`` reaches the tokeniser as a real token on some builds and truncates
#: the C string on others, so the same input can embed differently depending
#: on which -- removing them is what makes the recipe reproducible. Tab and
#: newline are KEPT: they are ordinary prose whitespace a tokenizer handles.
#:
#: CARRIAGE RETURN IS STRIPPED, and that is the point of lane F-1b item 3.
_MODEL_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

#: Lane FB-6 item 2. Everything the llama.cpp WHEEL writes goes to file
#: descriptor 1 on some builds -- the loader banner, and
#: ``llama_context: n_ctx_seq (512) > n_ctx_train (0) -- possible training
#: context overflow`` on every vocab-only tokenizer load. A CLI whose one
#: contract is a single JSON envelope on stdout then emits prose ahead of it
#: and every ``json.load`` of that output raises, which is exactly what a
#: live programme hit: an embedding command could not be read by the script
#: that called it.
#:
#: The redirect is at the FILE DESCRIPTOR, not at ``sys.stdout``, because the
#: writer is C code holding fd 1 directly and ``verbose=False`` does not
#: reach it. It is not the wheel's ``llama_log_set`` either: that callback's
#: signature and module path have moved between releases, and a version-
#: sensitive silencer that stops silencing after an upgrade is worse than
#: none. fd 1 and fd 2 are the contract that does not move.
#:
#: Nothing is discarded. The text lands on stderr, where an operator reading
#: a terminal sees it and a caller parsing stdout does not.
@contextlib.contextmanager
def native_logs_to_stderr() -> Any:
    """Send everything written to fd 1 to fd 2 for the duration.

    A no-op (and never a failure) where the descriptors cannot be duplicated
    -- an embedded interpreter, a closed stdout: the caller is embedding
    text, and a backend that raised because it could not redirect a log
    would be the worse outcome."""
    try:
        sys.stdout.flush()
    except (ValueError, OSError):
        pass
    try:
        saved = os.dup(1)
    except (OSError, ValueError, AttributeError):
        yield
        return
    try:
        os.dup2(2, 1)
        yield
    finally:
        try:
            sys.stdout.flush()
        except (ValueError, OSError):
            pass
        try:
            os.dup2(saved, 1)
        finally:
            os.close(saved)


#: One resident ``Llama`` per model file per PROCESS (keyed by the resolved
#: model path). A 4B-parameter GGUF costs seconds to load and hundreds of
#: megabytes to hold; a backend object is constructed per retrieval call in
#: places, so the model cannot be owned by the object.
_LLAMA_INSTANCES: dict[str, Any] = {}

#: The settings each resident above was actually CONSTRUCTED with. The cache
#: is keyed on the model path alone and deliberately stays that way -- keying
#: it on the settings would load a second multi-gigabyte copy of one GGUF the
#: first time two backends disagreed -- so the way to keep a report honest is
#: to remember what the first construction won with and say so
#: (VERIFY_f1b-sidecar.md V-8: ``runtime_details()`` reported the CONFIGURED
#: thread settings for a resident built with different ones).
_LLAMA_INSTANCE_SETTINGS: dict[str, dict[str, Any]] = {}


def embeddable_text(text: str) -> str:
    """``text`` as an embedding model must see it -- **the one function of
    record** (lane F-1b item 3), used by the corpus-producing path and by
    every query-side client.

    Two transformations, in this order, and nothing else: control characters
    removed (:data:`_MODEL_UNSAFE_RE` -- ``\\t`` and ``\\n`` kept, ``\\r``
    among those removed), and the EMPTY STRING when nothing but whitespace
    was left. It never strips, never collapses whitespace, never lowercases:
    the embedding of a passage has to be reproducible from the stored text.

    **Why there used to be two.** The offload worker (the path that produced
    this program's stored vectors) has always applied these semantics at the
    model boundary. The harness's query-side client had its own sanitiser
    that KEPT ``\\r`` and passed whitespace-only text through -- so the two
    were different functions, and the measurement that found it put numbers
    on the gap: a clean-room reference against the query-side version scored
    **0.959** on ``"line one\\r\\nline two\\r\\n"`` and **0.412** on
    ``"   \\n  "``, both far below the 0.99 agreement bar, while **3.1 % of
    one measured corpus (503 of 16,173 chunks) carries ``\\r``**. Those
    chunks were embedded WITHOUT their ``\\r``, because the producing path
    stripped it; a query-side client that kept it was encoding a different
    string than the corpus was encoded from. Real chunks carry a few ``\\r``
    in thousands of characters, so the effect on a whole chunk measured at
    the precision floor (0.9993) -- but it is a divergence with no upside,
    and the producing path is the one that cannot be changed retroactively.
    The query side therefore moved to it.

    The empty string is deliberate and is NOT a skip. On the DOCUMENT side,
    dropping a whitespace-only chunk would change the vector COUNT, which
    :func:`trialerror.offload.stage.offload_embed_vectors` checks against
    ``expect.chunk_count`` -- one whitespace-only chunk would fail a whole
    document's job. An empty string embeds to a real (if uninformative)
    vector, the counts line up, and the chunk stays searchable through the
    lexical tier. On the QUERY side the consequence is worth naming: a query
    of nothing but whitespace is embedded as the retrieval instruction alone,
    which is the honest answer to a query with no content in it.
    """
    cleaned = _MODEL_UNSAFE_RE.sub("", text)
    return cleaned if cleaned.strip() else ""


#: The name the query-side recipe used before lane F-1b item 3, kept as an
#: ALIAS of the function of record (not a second implementation) so existing
#: call sites and tests keep resolving -- and so nothing can quietly
#: reintroduce a second set of semantics under the old name.
_sanitise_for_embedding = embeddable_text


def _l2_normalise(values: Sequence[float]) -> list[float]:
    """L2-normalise in place-equivalent form. A zero vector is returned
    unchanged (never a division by zero): the empty string is a LEGAL input
    to this backend, and an encoder is entitled to answer it with whatever
    it answers it with."""
    norm = math.sqrt(sum(float(v) * float(v) for v in values))
    if norm == 0.0:
        return [float(v) for v in values]
    return [float(v) / norm for v in values]


def _flatten_pooled(raw: Any) -> list[float]:
    """One vector out of whatever an encoder handed back: a flat list of
    floats for a pooled model, a list of per-token vectors when pooling is
    off.

    Takes the LAST token vector in the second case -- that is what
    ``pooling = "last"`` asks for, and a build (or a server) that ignores the
    setting must not silently produce a mean of a list instead. Shared by
    both clients (lane F-1b item 2) so the two cannot drift on it."""
    values = list(raw)
    if values and isinstance(values[0], (list, tuple)):
        return [float(v) for v in values[-1]]
    return [float(v) for v in values]


def _prompt_overruns_context_message(prompt_ids: int, *, n_ctx: int, reserved: int) -> str:
    """Both numbers, named, the way the native-dims refusal names both
    widths -- an operator who set one of them has to be able to see which one
    to move. ``reserved`` is how many ids of the context this client keeps
    out of the content budget (1 in-process, 2 through the sidecar -- see
    :class:`LlamaServerEmbedBackend` for the server's off-by-one)."""
    return (
        f"{QUERY_EMBED_TABLE} query_prompt is {prompt_ids} tokens, which leaves no room for the "
        f"query itself inside n_ctx = {n_ctx} (the content budget is n_ctx - {reserved} - "
        f"{prompt_ids} tokens): raise n_ctx or shorten query_prompt"
    )


def _prepare_for_encoder(
    text: str,
    *,
    kind: str,
    query_prompt: str,
    prompt_id_count: Callable[[], int],
    budget: int,
    tokenize: Callable[[bytes], Sequence[int]],
    detokenize: Callable[[Sequence[int]], Any],
    overrun_message: Callable[[int], str],
) -> str:
    """Recipe steps 1-3, once, for both clients (lane F-1b item 2).

    Sanitise -> prefix the retrieval instruction for ``kind="query"`` only,
    by plain concatenation -> tokenise ``add_bos=False, special=False`` ->
    truncate to ``budget`` ids -> detokenise back to text.

    It is ONE function because the two clients embed against the same stored
    vectors: a prepared string that differs by a space, a prompt, or one
    token of truncation produces a vector that is individually plausible and
    collectively incomparable, and no downstream check would catch it. The
    only thing the callers are allowed to differ on is ``budget`` (the
    context arithmetic each runtime demands) and how the ids are produced."""
    prepared = embeddable_text(text)
    if kind == "query":
        # The prompt and the content share this budget, so a prompt that
        # fills it would truncate ITSELF and drop the query entirely -- a
        # search that silently ranked the corpus against an instruction.
        count = prompt_id_count()
        if count >= budget:
            raise EmbedBackendNotRunnable(overrun_message(count))
        prepared = query_prompt + prepared
    ids = list(tokenize(prepared.encode("utf-8")))
    if len(ids) > budget:
        ids = ids[:budget]
        detokenised = detokenize(ids)
        prepared = (
            detokenised.decode("utf-8", errors="ignore")
            if isinstance(detokenised, bytes)
            else str(detokenised)
        )
    return prepared


def _finalise_recipe_vector(values: Sequence[Any], *, native_dims: int, dims: int) -> list[float]:
    """Recipe step 5, once, for both clients: L2 at ``native_dims``, slice to
    ``dims``, L2 again, assert unit norm.

    The width check comes first because a vector of the wrong width sliced to
    ``dims`` is silently incomparable with the stored ones, and the unit-norm
    assertion exempts nothing -- zero included (see the comment below)."""
    vector = _flatten_pooled(values)
    if len(vector) != native_dims:
        raise EmbedBackendNotRunnable(
            f"the query-side encoder returned {len(vector)} floats, not the configured "
            f"native_dims = {native_dims}: a vector of the wrong width sliced to "
            f"{dims} would be silently incomparable with this program's stored vectors"
        )
    vector = _l2_normalise(vector)[:dims]
    vector = _l2_normalise(vector)
    norm = math.sqrt(sum(v * v for v in vector))
    if vector and abs(norm - 1.0) > 1e-5:
        # Zero is NOT exempt. `_l2_normalise` returns its input unchanged for
        # a zero vector (never a ZeroDivisionError), so an all-zero encoder
        # output -- or a matryoshka slice landing on a zero block -- would
        # otherwise pass the one assertion that exists to catch it and go on
        # to score 0.0 against every row, ranking the corpus by the id
        # tie-break alone. Exempting the single value that falsifies an
        # assertion is what stops it being an assertion.
        raise EmbedBackendNotRunnable(
            f"the query-side encoder produced a vector of norm {norm!r} after normalisation"
        )
    return vector


def _llama_pooling_type(name: str) -> int:
    """The integer ``llama.cpp`` wants for a pooling name, preferring the
    installed package's own constant over this module's literal table."""
    key = str(name or "last").strip().lower()
    if key not in _LLAMA_POOLING_TYPES:
        raise ValueError(
            f"ingest.embed pooling = {name!r} is not a llama.cpp pooling type "
            f"(choices: {sorted(_LLAMA_POOLING_TYPES)})"
        )
    try:
        import llama_cpp  # type: ignore[import-not-found]

        constant = getattr(llama_cpp, f"LLAMA_POOLING_TYPE_{key.upper()}", None)
        if constant is not None:
            return int(constant)
    except Exception:  # noqa: BLE001 - library absent/broken: fall back to the literal table
        pass
    return _LLAMA_POOLING_TYPES[key]


class LlamaCppEmbedBackend:
    """A GGUF encoder run through ``llama-cpp-python``, IN THIS PROCESS.

    The query-side answer for a program whose document embeddings are
    produced on another machine (``backend = "offload"``): the same
    ``model_key``, the same stored width, no GPU, one model load per
    process. Nothing on the document side has to change for it, and nothing
    here writes an ``emb`` row -- it exists to turn one short piece of text
    into one vector, on demand.

    **The recipe is the contract.** Every step below is fixed by the
    reference note that shipped with the model, in this order, and a
    deviation on any one of them produces vectors that are individually
    plausible and collectively incomparable with the corpus:

    1. sanitise control characters and whitespace-only text
       (:func:`embeddable_text`, the function of record);
    2. prepend :data:`DEFAULT_QUERY_PROMPT` (or the configured
       ``query_prompt``) for ``kind="query"`` ONLY, by plain concatenation;
    3. tokenise with ``add_bos=False, special=False`` and truncate to
       ``n_ctx - 1`` ids -- one token of EOS room -- then detokenise back to
       text, so the string handed to the encoder is exactly the string the
       token budget allows. The prompt of step 2 is INSIDE that budget (a
       query truncated first and then prefixed would overrun the context),
       which is why a query whose prompt alone fills the budget is refused
       rather than truncated: truncating the prompt would drop the query
       itself and rank the corpus against an instruction;
    4. ``embed(text, normalize=False)``;
    5. L2-normalise at ``native_dims``, slice to ``dims``, L2-normalise
       again, and assert the result is a unit vector.

    **``n_threads_batch`` is the setting that decides whether this backend
    is usable at all** (lane F-1b item 1). ``n_threads`` sizes the
    GENERATION pool and an embedding decode never touches it; the decode
    runs on the BATCH pool, which ``llama-cpp-python`` defaults to
    ``os.cpu_count()`` -- the CPUs this process can SEE. Inside a cgroup
    whose quota is smaller than that (measured: 12 visible threads, a
    10-CPU quota) the extra ggml workers spin-wait at every graph barrier,
    the cgroup is throttled in almost every scheduling period, and one
    64-token query costs **22.5 s instead of 3.2 s** for a vector identical
    to the bit. So the default here is ``min(n_threads,
    cgroup_cpu_quota())`` rather than the library's, and both numbers plus
    the quota are reported by :meth:`runtime_details` and by every
    :meth:`runnable` refusal, because the symptom (a slow encoder) does not
    otherwise name its cause.

    The empty string is a legal input and is embedded, not skipped: a
    caller that asked for the embedding of nothing gets the encoder's answer
    for nothing, rather than a silently missing row.

    ``_llama`` is a test-only seam (same precedent as
    :class:`FakeEmbedBackend`'s ``delay_s``): an object exposing
    ``tokenize``/``detokenize``/``embed`` stands in for the real
    ``Llama``, which is what lets the pure parts of the recipe above be
    tested without the library, a model file, or a CPU-minute.
    """

    def __init__(
        self,
        *,
        model_path: str,
        model_key: str,
        dims: int = 2048,
        native_dims: int = DEFAULT_LLAMA_NATIVE_DIMS,
        n_ctx: int = DEFAULT_LLAMA_N_CTX,
        n_threads: int | None = None,
        n_threads_batch: int | None = None,
        pooling: str = "last",
        query_prompt: str = DEFAULT_QUERY_PROMPT,
        _llama: Any = None,
    ):
        self.model_path = str(model_path)
        self.model_key = str(model_key)
        self.dims = int(dims)
        self.native_dims = int(native_dims)
        self.n_ctx = int(n_ctx)
        self.n_threads = int(n_threads) if n_threads else (os.cpu_count() or 1)
        #: What the cgroup actually allows, for the default below and for the
        #: runnable()/doctor report: an operator staring at a slow encoder has
        #: to be able to see the two numbers whose relationship is the bug.
        self.cpu_quota = cgroup_cpu_quota()
        #: THE thread setting that matters for an embedding decode (lane F-1b
        #: item 1). Defaults to the quota-capped thread count; an explicit
        #: ``[ingest.embed.query] n_threads_batch`` is taken verbatim,
        #: including a value above the quota -- an override that could not
        #: override the cap would not be one, and the reported numbers make
        #: the choice visible.
        if n_threads_batch is not None and int(n_threads_batch) <= 0:
            # V-7: `if n_threads_batch` read 0 as "unset" and passed a
            # negative through to the library, where llama.cpp resolves a
            # non-positive thread count from the VISIBLE CPU count -- the
            # 22.5 s behaviour this setting exists to remove, reported as the
            # number the operator typed. Neither value is a thread count.
            raise ValueError(
                f"ingest.embed n_threads_batch = {int(n_threads_batch)} is not a thread count: "
                f"leave the key unset for the default min(n_threads, cgroup cpu quota) = "
                f"{max(1, min(self.n_threads, self.cpu_quota))}, or give a positive number "
                f"(a non-positive one makes llama.cpp size the pool from the VISIBLE CPU count, "
                f"which inside a smaller quota is the slowdown this setting exists to remove)"
            )
        self.n_threads_batch = (
            int(n_threads_batch) if n_threads_batch else max(1, min(self.n_threads, self.cpu_quota))
        )
        self.pooling = str(pooling)
        self.query_prompt = str(query_prompt)
        self._llama = _llama
        self._prompt_ids: list[int] | None = None
        if self.dims > self.native_dims:
            raise ValueError(
                f"ingest.embed dims = {self.dims} exceeds native_dims = {self.native_dims}: "
                "the matryoshka slice can only ever narrow the encoder's own output"
            )
        if self.n_ctx < 2:
            raise ValueError(f"ingest.embed n_ctx = {self.n_ctx} leaves no room for content plus EOS")

    # -- availability ------------------------------------------------------

    def runtime_details(self) -> dict[str, Any]:
        """The effective runtime settings, for a doctor detail field
        (:func:`embed_backend_runtime_details`). The thread trio is the
        point: ``n_threads_batch`` versus the quota is the difference
        between a 3-second encoder and a 23-second one, and neither number
        appears anywhere else an operator looks."""
        details: dict[str, Any] = {
            "backend": LLAMA_CPP_BACKEND_NAME,
            "n_threads": self.n_threads,
            "n_threads_batch": self.n_threads_batch,
            "cgroup_cpu_quota": self.cpu_quota,
            "n_ctx": self.n_ctx,
            "pooling": self.pooling,
        }
        # V-8: the resident model is shared per GGUF per process, and the
        # FIRST construction wins. A second backend for the same file (the
        # document side and the query side of one program, say) would
        # otherwise report its own configured numbers for a model that was
        # not built with them -- which is the opposite of what a doctor
        # detail asking for the effective settings is for.
        resident = _LLAMA_INSTANCE_SETTINGS.get(self.model_path)
        if resident is not None and resident != self._construction_settings():
            details["effective"] = dict(resident)
            details["resident_differs"] = (
                "this process already holds one resident model for this GGUF, built by whichever "
                "backend reached it first; the settings above are this backend's CONFIGURED ones "
                "and `effective` are the ones the loaded model actually carries"
            )
        return details

    def _thread_note(self) -> str:
        """The thread trio as a suffix for a refusal reason. Every failure of
        this backend is read by someone deciding whether the encoder is
        configured correctly, and the throttling bug of lane F-1b item 1 is
        invisible in any other sentence the harness prints."""
        return (
            f" [n_threads={self.n_threads}, n_threads_batch={self.n_threads_batch}, "
            f"cgroup cpu quota={self.cpu_quota}]"
        )

    def runnable(self) -> tuple[bool, str]:
        """Whether this process can actually encode right now: the library
        imports, the model file is there, and the loader comes up.

        Every failure is returned as TEXT, with the effective thread
        settings appended (see :meth:`_thread_note`). The one that motivated
        the method is the loader's: a wheel built against one C library
        installed under another raises an ``OSError`` from deep inside
        ``ctypes`` on first construction, and an operator reading a doctor
        line needs that sentence, not a traceback out of a search call."""
        if self._llama is not None:
            return True, ""
        try:
            import llama_cpp  # type: ignore[import-not-found]  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - ImportError, but also an OSError from the bundled library
            return False, (
                f"the query-side encoder needs the 'llama_cpp' package, which is not importable in "
                f"this process ({type(exc).__name__}: {exc})" + self._thread_note()
            )
        if not Path(self.model_path).is_file():
            return False, (
                f"no model file at {self.model_path!r} (ingest.embed.query model_path)" + self._thread_note()
            )
        try:
            self._resident()
        except Exception as exc:  # noqa: BLE001 - a loader failure IS the answer, as text
            return False, (
                f"the query-side encoder failed to load: {type(exc).__name__}: {exc}" + self._thread_note()
            )
        return True, ""

    def _prompt_overruns_context_message(self, prompt_ids: int) -> str:
        """:func:`_prompt_overruns_context_message` for this client's context
        arithmetic (one id of EOS room).

        Deliberately NOT part of :meth:`runnable`: this backend is selectable
        on the document side too, where the query prompt is never used, and
        ``embed_batch`` asks ``runnable()`` on both sides -- so refusing a
        document embed over a query-only budget would be a new false
        refusal. The condition is raised where it actually bites, on a query,
        and :func:`trialerror.retrieve.engine.query_vector_or_reason` turns it
        into the same reason string every other query-side failure produces."""
        return _prompt_overruns_context_message(prompt_ids, n_ctx=self.n_ctx, reserved=1)

    def _query_prompt_ids(self) -> list[int]:
        """The prompt's own token ids, tokenised once per instance. Needed
        because the prompt and the content share one budget (step 3), so the
        prompt's length is the content's ceiling."""
        if self._prompt_ids is None:
            llama = self._resident()
            with native_logs_to_stderr():
                self._prompt_ids = list(
                    llama.tokenize(self.query_prompt.encode("utf-8"), add_bos=False, special=False)
                )
        return self._prompt_ids

    def _resident(self) -> Any:
        """The one ``Llama`` instance for this model file in this process,
        constructed on first use. ``n_batch``/``n_ubatch`` are pinned to
        ``n_ctx`` so a single full-context input is never split into
        micro-batches the pooling would then average across -- and, measured
        separately, so that ``Llama.embed(..., truncate=True)`` never
        silently truncates a long input to ``n_batch`` instead.

        ``n_threads_batch`` is passed explicitly: left unset the library
        sizes the decode's own thread pool from the VISIBLE CPU count, which
        inside a smaller cgroup quota is the 7x slowdown of lane F-1b item 1
        (see the class docstring)."""
        if self._llama is not None:
            return self._llama
        cached = _LLAMA_INSTANCES.get(self.model_path)
        if cached is not None:
            return cached
        from llama_cpp import Llama  # type: ignore[import-not-found]

        with native_logs_to_stderr():
            llama = Llama(
                model_path=self.model_path,
                embedding=True,
                n_ctx=self.n_ctx,
                n_batch=self.n_ctx,
                n_ubatch=self.n_ctx,
                n_threads=self.n_threads,
                n_threads_batch=self.n_threads_batch,
                pooling_type=_llama_pooling_type(self.pooling),
                verbose=False,
            )
        _LLAMA_INSTANCES[self.model_path] = llama
        _LLAMA_INSTANCE_SETTINGS[self.model_path] = self._construction_settings()
        return llama

    def _construction_settings(self) -> dict[str, Any]:
        """The settings that change the resident object -- what is compared
        against a cache hit so a report can say which numbers are EFFECTIVE."""
        return {
            "n_ctx": self.n_ctx,
            "n_threads": self.n_threads,
            "n_threads_batch": self.n_threads_batch,
            "pooling": self.pooling,
        }

    # -- the recipe --------------------------------------------------------

    def prepared_text(self, text: str, *, kind: str = "document") -> str:
        """Recipe steps 1-3: the exact string the encoder is handed.

        Public because it is the half of the recipe that can be checked
        without a model, and a test that checks it is the only thing
        standing between "the prompt is prepended" and "the prompt is
        prepended with a space in front of it"."""
        llama = self._resident()
        with native_logs_to_stderr():
            return _prepare_for_encoder(
                text,
                kind=kind,
                query_prompt=self.query_prompt,
                prompt_id_count=lambda: len(self._query_prompt_ids()),
                budget=self.n_ctx - 1,  # one id of EOS room
                tokenize=lambda raw: llama.tokenize(raw, add_bos=False, special=False),
                detokenize=llama.detokenize,
                overrun_message=self._prompt_overruns_context_message,
            )

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        ok, reason = self.runnable()
        if not ok:
            raise EmbedBackendNotRunnable(reason)
        return [self._embed_one(text, kind=kind) for text in texts]

    def _embed_one(self, text: str, *, kind: str) -> list[float]:
        llama = self._resident()
        prepared = self.prepared_text(text, kind=kind)
        with native_logs_to_stderr():
            raw = llama.embed(prepared, normalize=False)
        return _finalise_recipe_vector(raw, native_dims=self.native_dims, dims=self.dims)

    @staticmethod
    def _flatten(raw: Any) -> list[float]:
        """:func:`_flatten_pooled`, kept as a method because this class's own
        docstring and tests name it."""
        return _flatten_pooled(raw)


# --------------------------------------------------------------------------
# llama-server sidecar embed backend (lane F-1b item 2) -- the production
# query encoder
# --------------------------------------------------------------------------

#: The ``[ingest.embed] backend`` / ``[ingest.embed.query] backend`` value
#: that selects :class:`LlamaServerEmbedBackend`.
LLAMA_SERVER_BACKEND_NAME = "llama_server"

#: Where the sidecar is expected to be listening. Loopback, always: the
#: encoder is a process on this machine, and a config that could name a
#: remote host would be a way out of the sandbox with a queue attached.
DEFAULT_LLAMA_SERVER_URL = "http://127.0.0.1:8871"

#: Per-request ceiling. Generous on purpose: a 256-token query measured ~15 s
#: on the CPU this was sized for, and a timeout that fires mid-encode reads
#: to a caller exactly like a dead sidecar.
DEFAULT_LLAMA_SERVER_TIMEOUT_S = 60

#: ``MAX_SEQ + 1``. The server refuses a request of EXACTLY its context
#: length ("request (N tokens) exceeds the available context size (N
#: tokens)"), which killed a measurement run mid-corpus, so the sidecar is
#: built one id wider than the geometry and this client budgets against the
#: geometry -- see :class:`LlamaServerEmbedBackend` for the arithmetic.
DEFAULT_LLAMA_SERVER_N_CTX = 2049

#: The server's native embedding endpoint: it answers with the RAW pooled
#: vector (subject to ``--embd-normalize``), which is what this client's
#: two-step normalisation needs. Chosen over the OpenAI-compatible
#: ``/v1/embeddings`` deliberately -- see the class docstring.
LLAMA_SERVER_EMBED_PATH = "/embedding"

#: Liveness (200 = the model is loaded and serving; 503 = still loading).
LLAMA_SERVER_HEALTH_PATH = "/health"

#: Where the server describes what it loaded, when the build exposes it.
LLAMA_SERVER_PROPS_PATH = "/props"

#: One vocab-only tokenizer per GGUF path per process. ``vocab_only=True``
#: reads the model's vocabulary and NO weights, so this costs megabytes and
#: milliseconds rather than the 4 GB the encoder itself would -- but it is
#: still worth not paying twice.
_VOCAB_ONLY_INSTANCES: dict[str, Any] = {}

#: What :meth:`LlamaServerEmbedBackend.runtime_details` reports for the
#: served-model identity check, in the three states it can be in.
MODEL_CHECK_VERIFIED = "verified"
MODEL_CHECK_UNVERIFIED = "unverified"
MODEL_CHECK_MISMATCH = "mismatch"

#: The same three readings for the served CONTEXT check (lane F-1b stage 3,
#: VERIFY_f1b-sidecar.md V-3). The client's budget arithmetic is airtight on
#: its own side, but the property it buys -- never sending a request of
#: exactly the server's context length -- is JOINT with the ``-c`` the sidecar
#: was started with, and nothing checked that number.
CONTEXT_CHECK_VERIFIED = "verified"
CONTEXT_CHECK_UNVERIFIED = "unverified"
CONTEXT_CHECK_TOO_SMALL = "too_small"


class _JsonHttp:
    """The two HTTP calls this backend makes, over :mod:`urllib` -- no new
    dependency, and nothing that could follow a redirect off the loopback
    interface.

    Returns ``(status, parsed_body)``; a non-JSON body comes back as the
    decoded string, because an error page is as much of an answer as a
    vector is and the reason string has to be able to quote it. Raises for
    a connection that could not be made at all, which every caller turns
    into text."""

    def __init__(self, base_url: str, *, timeout_s: float):
        self.base_url = str(base_url).rstrip("/")
        self.timeout_s = float(timeout_s)

    def _request(self, path: str, *, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310 - loopback http, config-pathed
                status = int(getattr(response, "status", 0) or 0)
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:  # a 4xx/5xx IS an answer
            status = int(exc.code)
            body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, body

    def get_json(self, path: str) -> tuple[int, Any]:
        return self._request(path)

    def post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        return self._request(path, payload=payload)


def _served_model_name(props: Any) -> str | None:
    """The model file name a ``/props`` (or ``/health``) body reports, across
    the several shapes llama.cpp builds have used for it -- ``None`` when the
    build does not say.

    ``None`` is a real answer and is treated as one (a warning, not a
    refusal): a server that does not expose what it loaded cannot be checked,
    and refusing every such build would make the check a deployment blocker
    instead of a safety net."""
    if not isinstance(props, dict):
        return None
    for key in ("model_path", "model", "model_name"):
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = props.get("default_generation_settings")
    if isinstance(nested, dict):
        for key in ("model", "model_path"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _served_n_ctx(props: Any) -> int | None:
    """The context length a ``/props`` (or ``/health``) body reports for the
    slot a request will be served by -- ``None`` when the build does not say.

    ``default_generation_settings.n_ctx`` is the per-slot figure on the build
    family this client targets (a server started with ``-c N -np P`` gives
    each slot ``N / P``), which is exactly the number a single request is
    measured against; a top-level ``n_ctx`` is accepted for builds that report
    it there instead."""
    if not isinstance(props, dict):
        return None
    nested = props.get("default_generation_settings")
    candidates = []
    if isinstance(nested, dict):
        candidates.append(nested.get("n_ctx"))
    candidates.append(props.get("n_ctx"))
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
    return None


def _extract_server_embedding(body: Any) -> Any:
    """The vector out of a ``/embedding`` response body, across the shapes
    the endpoint has returned: ``{"embedding": [...]}``,
    ``[{"index": 0, "embedding": [[...]]}]`` (the pooled vector wrapped in a
    one-element matrix), and the OpenAI-compatible ``{"data": [{"embedding":
    [...]}]}``.

    Shape-tolerant on purpose, and safe to be: whatever comes out of here is
    width-checked against ``native_dims`` before it is normalised, so a
    misread shape refuses loudly instead of ranking a corpus against a
    truncated vector."""
    if isinstance(body, dict):
        if "embedding" in body:
            return body["embedding"]
        data = body.get("data")
        if isinstance(data, list) and data:
            return _extract_server_embedding(data[0])
        raise EmbedBackendNotRunnable(
            f"the embedding sidecar answered with no 'embedding' field (keys: {sorted(body)})"
        )
    if isinstance(body, list) and body:
        first = body[0]
        if isinstance(first, dict):
            return _extract_server_embedding(first)
        return body
    raise EmbedBackendNotRunnable(
        f"the embedding sidecar answered with {type(body).__name__}, not an embedding"
    )


class LlamaServerEmbedBackend:
    """The reference recipe with the forward pass in a SIDECAR: the model is
    loaded once by a long-lived ``llama-server`` process on loopback, and
    this client does everything else.

    Why it exists (lane F-1b, REQ-2026-09-11-02): measured against the same
    stored vectors, the in-process wheel and this client agree to 0.9998
    cosine with each other and ~0.9994 with the corpus, and the sidecar
    answers a 64-token query in **2.65 s** where the in-process route needs
    3.2 s at its best tuning and 22.9 s untuned. The model also stops being
    re-loaded per process: a CLI invocation that embeds one query pays an
    HTTP round trip instead of a 4 GB mmap.

    **The recipe is the contract, and it is the same recipe.** Steps 1-3 run
    through :func:`_prepare_for_encoder` and step 5 through
    :func:`_finalise_recipe_vector` -- the very functions the in-process
    backend uses -- so the two cannot drift. What differs is only:

    - **the tokenizer.** Pre-truncation needs the model's vocabulary in THIS
      process, so ``tokenizer_model_path`` names a GGUF opened with
      ``llama_cpp.Llama(model_path, vocab_only=True)``: the vocabulary, and
      **no model weights** (kilobytes of allocation, not gigabytes). The
      server's own ``/tokenize`` would be one more round trip per query and
      one more thing to be inconsistent about.
    - **the context arithmetic.** The server refuses a request of exactly
      its context length, so it is run one id wider than the geometry
      (``n_ctx`` defaults to 2049 = ``MAX_SEQ + 1``) while this client
      truncates content to ``n_ctx - 2`` = ``MAX_SEQ - 1`` = 2047 ids. The
      server appends EOS, giving 2048 ids for a full-length input -- one
      below its context, so the refusal is impossible by construction. With
      matching geometries the prepared string is byte-identical to the
      in-process client's (``n_ctx = 2048`` there, ``2049`` here).
    - **the normalisation the server may have done.** The sidecar is run
      with ``--embd-normalize -1`` (raw pooled vector), and this client
      additionally asks for it per request. It does not actually matter:
      every value that flag accepts is a POSITIVE SCALING of the pooled
      vector, and the client's own L2 at ``native_dims`` recovers the same
      unit vector from any of them. What would matter -- a server that
      sliced, or pooled differently -- is caught by the width check and by
      the served-model check in :meth:`runnable`.

    **Endpoint.** ``POST /embedding``, the server's native embedding route,
    which returns the raw pooled vector and takes ``{"content": ...}``.
    ``/v1/embeddings`` is the OpenAI-compatible wrapper over the same
    computation: it adds an ``encoding_format``/base64 contract, a
    ``"data"`` envelope, and (build-dependent) its own normalisation, none of
    which this recipe wants. The response parser accepts the shapes both
    routes have used anyway, because the cost of being wrong about one is a
    refusal either way (see :func:`_extract_server_embedding`).

    ``_http``/``_tokenizer`` are test-only seams (the precedent is
    :class:`LlamaCppEmbedBackend`'s ``_llama``): an object with
    ``get_json``/``post_json`` and one with ``tokenize``/``detokenize`` stand
    in for the server and the vocabulary, which is what lets every step of
    the recipe be tested with no model file, no wheel, and no live server.
    """

    def __init__(
        self,
        *,
        model_key: str,
        dims: int = 2048,
        url: str = DEFAULT_LLAMA_SERVER_URL,
        timeout_s: float = DEFAULT_LLAMA_SERVER_TIMEOUT_S,
        native_dims: int = DEFAULT_LLAMA_NATIVE_DIMS,
        n_ctx: int = DEFAULT_LLAMA_SERVER_N_CTX,
        query_prompt: str = DEFAULT_QUERY_PROMPT,
        tokenizer_model_path: str | None = None,
        sidecar_name: str | None = None,
        _http: Any = None,
        _tokenizer: Any = None,
    ):
        self.model_key = str(model_key)
        self.dims = int(dims)
        self.url = str(url).rstrip("/")
        self.timeout_s = float(timeout_s)
        self.native_dims = int(native_dims)
        self.n_ctx = int(n_ctx)
        self.query_prompt = str(query_prompt)
        self.tokenizer_model_path = str(tokenizer_model_path) if tokenizer_model_path else None
        #: Which ``[sidecars.<name>]`` serves this URL, when the program says
        #: so -- used ONLY to spell the remedy in a refusal. Absent, the
        #: refusal names the verb without inventing a name (V-10: it used to
        #: say "embed" whatever the sidecar was called).
        self.sidecar_name = str(sidecar_name) if sidecar_name else None
        self._http_client = _http
        self._tokenizer = _tokenizer
        self._prompt_ids: list[int] | None = None
        #: Last readings, for :meth:`runtime_details` (and therefore for the
        #: doctor line). A backend that cannot say what it last saw leaves an
        #: operator with nothing between "PASS" and a tcpdump.
        self.last_health: dict[str, Any] = {"checked": False}
        self.model_check: str = MODEL_CHECK_UNVERIFIED
        self.model_check_note: str = "not checked yet"
        #: The served CONTEXT reading (V-3), in the same three states and on
        #: the same fail-closed reading as the model check.
        self.context_check: str = CONTEXT_CHECK_UNVERIFIED
        self.context_check_note: str = "not checked yet"
        self.served_n_ctx: int | None = None
        if self.dims > self.native_dims:
            raise ValueError(
                f"{QUERY_EMBED_TABLE} dims = {self.dims} exceeds native_dims = {self.native_dims}: "
                "the matryoshka slice can only ever narrow the encoder's own output"
            )
        if self.n_ctx < 3:
            raise ValueError(
                f"{QUERY_EMBED_TABLE} n_ctx = {self.n_ctx} leaves no room for content plus EOS plus "
                "the one id the server keeps free (it refuses a request of exactly its context length)"
            )

    # -- the two runtimes this client needs ---------------------------------

    @property
    def content_budget(self) -> int:
        """``n_ctx - 2``: one id for the EOS the server appends, one so the
        request is never EXACTLY the context length the server refuses."""
        return self.n_ctx - 2

    def _http(self) -> Any:
        if self._http_client is None:
            self._http_client = _JsonHttp(self.url, timeout_s=self.timeout_s)
        return self._http_client

    def _vocab(self) -> Any:
        """The vocab-only tokenizer for ``tokenizer_model_path``, one per path
        per process. No weights are loaded: ``vocab_only=True`` is what makes
        it legitimate to open a 4 GB file in a CLI process that is about to
        send one HTTP request."""
        if self._tokenizer is not None:
            return self._tokenizer
        if not self.tokenizer_model_path:
            raise EmbedBackendNotRunnable(self._no_tokenizer_message())
        cached = _VOCAB_ONLY_INSTANCES.get(self.tokenizer_model_path)
        if cached is not None:
            return cached
        from llama_cpp import Llama  # type: ignore[import-not-found]

        # Lane FB-6 item 2: THIS is the construction that printed
        # "llama_context: n_ctx_seq (512) > n_ctx_train (0)" onto a CLI's
        # stdout ahead of its JSON envelope, on every embedding call.
        with native_logs_to_stderr():
            vocab = Llama(model_path=self.tokenizer_model_path, vocab_only=True, verbose=False)
        _VOCAB_ONLY_INSTANCES[self.tokenizer_model_path] = vocab
        return vocab

    def _no_tokenizer_message(self) -> str:
        return (
            f"the {LLAMA_SERVER_BACKEND_NAME} backend needs {QUERY_EMBED_TABLE} tokenizer_model_path "
            "(a GGUF whose VOCABULARY is used to pre-truncate the input -- no model weights are "
            "loaded for it): without it a query longer than the sidecar's context would be "
            "truncated by the server, at a point this client cannot reproduce"
        )

    # -- availability ------------------------------------------------------

    def runtime_details(self) -> dict[str, Any]:
        """URL, geometry, and the last health/identity readings, for a doctor
        detail field (:func:`embed_backend_runtime_details`)."""
        return {
            "backend": LLAMA_SERVER_BACKEND_NAME,
            "url": self.url,
            "embed_path": LLAMA_SERVER_EMBED_PATH,
            "timeout_s": self.timeout_s,
            "n_ctx": self.n_ctx,
            "content_budget": self.content_budget,
            "tokenizer_model_path": self.tokenizer_model_path,
            "health": dict(self.last_health),
            "model_check": self.model_check,
            "model_check_note": self.model_check_note,
            "context_check": self.context_check,
            "context_check_note": self.context_check_note,
            "served_n_ctx": self.served_n_ctx,
            "sidecar_name": self.sidecar_name,
        }

    def runnable(self) -> tuple[bool, str]:
        """Whether a query could be embedded right now: the tokenizer is
        available here, the sidecar answers ``/health`` within ``timeout_s``,
        and -- when the build says what it loaded -- it loaded the model this
        program's vectors were produced with.

        Every failure is text, including the one an operator hits first: a
        sidecar that is not running. The refusal names the verb that starts
        it.

        The served-model check is fail-CLOSED on a mismatch and a warning on
        silence: a server serving some other GGUF answers every request with
        a plausible vector from a different space, which is the same silent
        failure a ``model_key`` mismatch is refused for, while a build that
        does not expose its model name cannot be checked at all and must not
        be a deployment blocker."""
        tokenizer_ok, tokenizer_reason = self._tokenizer_runnable()
        if not tokenizer_ok:
            return False, tokenizer_reason

        started = time.perf_counter()
        try:
            status, body = self._http().get_json(LLAMA_SERVER_HEALTH_PATH)
        except Exception as exc:  # noqa: BLE001 - a dead sidecar is the normal failure, as text
            waited = time.perf_counter() - started
            self.last_health = {
                "checked": True, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "waited_s": round(waited, 3),
            }
            # The elapsed time, not the budget (V-10): a connection refused in
            # 10 ms reported as "did not answer within 60s" describes a wait
            # that never happened, and tells an operator to look at the wrong
            # thing (a slow model load rather than a process that is not there).
            return False, (
                f"the embedding sidecar at {self.url} did not answer {LLAMA_SERVER_HEALTH_PATH} "
                f"(gave up after {waited:.2f}s of a {self.timeout_s:g}s budget; "
                f"{type(exc).__name__}: {exc}) -- {self._start_hint()}"
            )
        self.last_health = {
            "checked": True, "ok": status == 200, "status": status, "body": body,
            "waited_s": round(time.perf_counter() - started, 3),
        }
        if status != 200:
            detail = body if isinstance(body, str) else json.dumps(body)[:200]
            loading = " (the model is still loading; try again shortly)" if status == 503 else ""
            return False, (
                f"the embedding sidecar at {self.url} answered {LLAMA_SERVER_HEALTH_PATH} with HTTP "
                f"{status}{loading}: {detail}"
            )
        return self._served_geometry_runnable()

    def _start_hint(self) -> str:
        """How to start the sidecar this client talks to, as a sentence.

        Names the configured sidecar when the program said which one serves
        this URL (``[ingest.embed.query] sidecar_name``) and otherwise points
        at the verb that lists them -- never a name this client made up."""
        if self.sidecar_name:
            return f"start it with `trialerror sidecar start {self.sidecar_name}`"
        return (
            "start it with `trialerror sidecar start <name>` (`trialerror sidecar status` lists "
            f"the sidecars this program configures; {QUERY_EMBED_TABLE} sidecar_name names the "
            "one that serves this URL)"
        )

    def _tokenizer_runnable(self) -> tuple[bool, str]:
        if self._tokenizer is not None:
            return True, ""
        if not self.tokenizer_model_path:
            return False, self._no_tokenizer_message()
        try:
            import llama_cpp  # type: ignore[import-not-found]  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - ImportError, but also an OSError out of ctypes
            return False, (
                f"the {LLAMA_SERVER_BACKEND_NAME} backend needs the 'llama_cpp' package for its "
                f"TOKENIZER only (opened with vocab_only=True -- no model weights are loaded), and it "
                f"is not importable in this process ({type(exc).__name__}: {exc}); the GGUF it would "
                f"read is {QUERY_EMBED_TABLE} tokenizer_model_path"
            )
        if not Path(self.tokenizer_model_path).is_file():
            return False, (
                f"no tokenizer model file at {self.tokenizer_model_path!r} "
                f"({QUERY_EMBED_TABLE} tokenizer_model_path)"
            )
        try:
            self._vocab()
        except Exception as exc:  # noqa: BLE001 - a loader failure IS the answer, as text
            return False, (
                f"the query-side tokenizer failed to load: {type(exc).__name__}: {exc} "
                f"({QUERY_EMBED_TABLE} tokenizer_model_path)"
            )
        return True, ""

    def _served_geometry_runnable(self) -> tuple[bool, str]:
        """What the server says it loaded, and how much context it serves it
        with: ``/props`` once, with ``/health``'s own body as the fallback for
        a build that answers there instead.

        Both checks are fail-CLOSED on a disagreement and silent on a build
        that does not report the fact -- a check that cannot run must not be a
        deployment blocker, and a check that CAN run must not be advisory."""
        props: Any = None
        try:
            status, body = self._http().get_json(LLAMA_SERVER_PROPS_PATH)
            if status == 200:
                props = body
        except Exception:  # noqa: BLE001 - no /props on this build: fall back, never refuse
            props = None
        ok, reason = self._served_model_runnable(props)
        if not ok:
            return ok, reason
        return self._served_context_runnable(props)

    def _served_context_runnable(self, props: Any) -> tuple[bool, str]:
        """Is the sidecar's own context at least as wide as the geometry this
        client budgets against? (VERIFY_f1b-sidecar.md V-3.)

        The client sends at most ``content_budget + 1`` = ``n_ctx - 1`` ids,
        and the server refuses a request of EXACTLY its context length, so the
        off-by-one that killed a measurement run is avoided only while the
        server's ``-c`` is >= this client's ``n_ctx``. A server one id too
        narrow passes ``/health``, passes the model check, and answers every
        short query -- it fails only on long inputs, mid-run, which is the
        failure this whole arithmetic exists to prevent. So it refuses here,
        where the fix is one flag or one config line."""
        served = _served_n_ctx(props)
        if served is None:
            served = _served_n_ctx(self.last_health.get("body"))
        self.served_n_ctx = served
        if served is None:
            self.context_check = CONTEXT_CHECK_UNVERIFIED
            self.context_check_note = (
                f"this build does not report its context length on {LLAMA_SERVER_PROPS_PATH} or "
                f"{LLAMA_SERVER_HEALTH_PATH}; the sidecar's -c is unverified (the client still "
                f"truncates to {self.content_budget} content ids)"
            )
            return True, ""
        if served < self.n_ctx:
            self.context_check = CONTEXT_CHECK_TOO_SMALL
            self.context_check_note = f"serving {served} tokens of context, needs >= {self.n_ctx}"
            return False, (
                f"the embedding sidecar at {self.url} serves {served} tokens of context, but this "
                f"client budgets against {QUERY_EMBED_TABLE} n_ctx = {self.n_ctx}: a full-length "
                f"input is {self.content_budget} content ids plus the EOS the server appends = "
                f"{self.content_budget + 1} ids, and a server refuses a request of exactly its "
                f"context length. Short queries would answer normally and long ones would fail "
                f"mid-run, so this refuses now: start the sidecar with -c {self.n_ctx} (or more), "
                f"or set {QUERY_EMBED_TABLE} n_ctx = {served} so both sides agree"
            )
        self.context_check = CONTEXT_CHECK_VERIFIED
        self.context_check_note = f"serving {served} tokens of context, client budget {self.n_ctx}"
        return True, ""

    def _served_model_runnable(self, props: Any = None) -> tuple[bool, str]:
        """The identity check, from the ``/props`` body its caller read (with
        ``/health``'s own body as the fallback for a build that answers
        there instead)."""
        served: str | None = _served_model_name(props)
        if served is None:
            served = _served_model_name(self.last_health.get("body"))
        if served is None:
            self.model_check = MODEL_CHECK_UNVERIFIED
            self.model_check_note = (
                f"this build does not report the model it loaded on {LLAMA_SERVER_PROPS_PATH} or "
                f"{LLAMA_SERVER_HEALTH_PATH}; the sidecar's model is unverified (it is still checked "
                "by the vector width and by model_key agreement)"
            )
            return True, ""

        expected = Path(self.tokenizer_model_path).name if self.tokenizer_model_path else None
        if expected and Path(served).name != expected:
            self.model_check = MODEL_CHECK_MISMATCH
            self.model_check_note = f"serving {Path(served).name!r}, expected {expected!r}"
            return False, (
                f"the embedding sidecar at {self.url} is serving {Path(served).name!r}, but this "
                f"program's vectors were produced with {expected!r} "
                f"({QUERY_EMBED_TABLE} tokenizer_model_path). A query embedded by a different model "
                f"is a vector in a different space: it does not error, it ranks the corpus in an "
                f"order that means nothing. Point the sidecar at the same GGUF, or fix the config"
            )
        self.model_check = MODEL_CHECK_VERIFIED
        self.model_check_note = f"serving {Path(served).name!r}"
        return True, ""

    # -- the recipe --------------------------------------------------------

    def _query_prompt_ids(self) -> list[int]:
        """The prompt's own token ids, tokenised once per instance -- the
        prompt shares the content's budget, so its length is the content's
        ceiling."""
        if self._prompt_ids is None:
            vocab = self._vocab()
            with native_logs_to_stderr():
                self._prompt_ids = list(
                    vocab.tokenize(self.query_prompt.encode("utf-8"), add_bos=False, special=False)
                )
        return self._prompt_ids

    def _prompt_overruns_context_message(self, prompt_ids: int) -> str:
        return _prompt_overruns_context_message(prompt_ids, n_ctx=self.n_ctx, reserved=2)

    def prepared_text(self, text: str, *, kind: str = "document") -> str:
        """Recipe steps 1-3: the exact string the sidecar is handed. Public
        for the same reason the in-process backend's is -- it is the half of
        the recipe that can be checked without an encoder, and the two
        clients' outputs are compared against each other in tests."""
        vocab = self._vocab()
        with native_logs_to_stderr():
            return _prepare_for_encoder(
                text,
                kind=kind,
                query_prompt=self.query_prompt,
                prompt_id_count=lambda: len(self._query_prompt_ids()),
                budget=self.content_budget,
                tokenize=lambda raw: vocab.tokenize(raw, add_bos=False, special=False),
                detokenize=vocab.detokenize,
                overrun_message=self._prompt_overruns_context_message,
            )

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        ok, reason = self.runnable()
        if not ok:
            raise EmbedBackendNotRunnable(reason)
        return [self._embed_one(text, kind=kind) for text in texts]

    def _embed_one(self, text: str, *, kind: str) -> list[float]:
        prepared = self.prepared_text(text, kind=kind)
        payload = {
            "content": prepared,
            # Asked for per request as well as on the server's command line:
            # -1 is "return the raw pooled vector", which is what this
            # client's own normalisation expects. Harmless if the build
            # ignores it -- every value the flag accepts is a positive
            # scaling, and the L2 below recovers the same unit vector.
            "embd_normalize": -1,
        }
        try:
            status, body = self._http().post_json(LLAMA_SERVER_EMBED_PATH, payload)
        except Exception as exc:  # noqa: BLE001 - a timeout or a dead sidecar mid-batch
            raise EmbedBackendNotRunnable(
                f"the embedding sidecar at {self.url} failed on {LLAMA_SERVER_EMBED_PATH} "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        if status != 200:
            detail = body if isinstance(body, str) else json.dumps(body)[:300]
            raise EmbedBackendNotRunnable(
                f"the embedding sidecar at {self.url} answered {LLAMA_SERVER_EMBED_PATH} with HTTP "
                f"{status}: {detail}"
            )
        raw = _extract_server_embedding(body)
        return _finalise_recipe_vector(raw, native_dims=self.native_dims, dims=self.dims)
