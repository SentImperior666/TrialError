"""C5: with ``[ingest.ocr] executor`` ABSENT, the DEV worker and the local OCR
backend behave exactly as before the vast.ai executor existed.

The seams the vast.ai lane added to ``trialerror/offload/worker.py`` and
``trialerror/ingest/backends.py`` (the admission step before the pull, the
``refused`` bucket and skip set, the ``on_pause`` hook, the ``result_fields``
merge, the overridable peak-RSS probe, the executor routing) must all be
inert for a DEV root that never names the executor. Live workers run from
the code these seams touch, so this is proved here end to end: the real
``ConfigDevBackends`` over a real DEV-shaped config, the real
``RealMarkerOcrBackend``, ``run_worker`` over a ``LocalTransport`` queue.

**This file runs natively on Windows.** The older OCR range suites skip there
because their fake ``marker_single`` is a shebang script. Here the fake is a
replacement for ``subprocess.run`` as ``trialerror.ingest.backends`` sees it,
which writes marker's paginated markdown (absolute ``{N}`` numbering, as
marker-pdf 1.10.x does under ``--page_range``) into the output directory the
real backend named -- so the argv, the planner, the range cache, the numbering
and the page-count check are all the real code.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

import trialerror.ingest.backends as backends_mod
from trialerror.ingest.backends import (
    DEFAULT_BOUNDED_DPI,
    RealMarkerOcrBackend,
    load_ocr_backend,
)
from trialerror.offload import protocol
from trialerror.offload.transport import LocalTransport
from trialerror.offload.worker import (
    IDLE_MESSAGE,
    OCR_RANGE_CACHE_DIRNAME,
    STOPPED_MESSAGE,
    ConfigDevBackends,
    ResidentBackends,
    run_worker,
)
from tests._ocr_range_fixtures import A4_PT, make_pdf
from tests._offload_fixtures import ControlTransport

FAKE_EXE = "c5-fake-marker-single"
JOB = "JOB-c5-ocr-1"
FAST_BEAT = 0.02
#: Three A4 pages at the planning DPI with the planner's safety factor: a
#: ten-page document plans to four ranges (0-2, 3-5, 6-8, 9-9).
BUDGET = (595.0 / 72.0 * DEFAULT_BOUNDED_DPI) * (842.0 / 72.0 * DEFAULT_BOUNDED_DPI) * 3 * 1.1

#: What a result manifest carried before the executor existed.
BASE_RESULT_KEYS = {"schema", "job_id", "stage", "worker_id", "finished_ts", "outputs", "backend", "version"}
CHUNKED_RESULT_KEYS = BASE_RESULT_KEYS | {
    "page_count",
    "page_range_numbering",
    "planning_dpi",
    "planning_dpi_source",
    "page_range_numbering_decided_by",
    "ranges",
}
RANGE_RECORD_KEYS = {
    "first_page",
    "last_page",
    "sha256",
    "chars",
    "range_wall_s",
    "cached",
    "peak_rss_bytes",
    "peak_rss_source",
}
SUMMARY_KEYS = {"claimed", "published", "failed", "lost", "stopped", "polls", "message", "control"}


class FakeMarker:
    """``subprocess.run`` for :data:`FAKE_EXE`; every other command goes to the
    real one. Records each invocation's argv (``--help`` probes counted
    apart) and writes ``<out>/<stem>/<stem>.md`` with one ``{N}`` block per page
    of the invocation's range, ``N`` absolute."""

    def __init__(self, total_pages: int, *, on_range: Callable[[int, int], Any] | None = None):
        self.total_pages = total_pages
        self.on_range = on_range
        self.argv: list[list[str]] = []
        self.help_probes = 0
        self._real = subprocess.run

    def __call__(self, cmd, *args, **kwargs):
        if not isinstance(cmd, (list, tuple)) or not cmd or cmd[0] != FAKE_EXE:
            return self._real(cmd, *args, **kwargs)
        cmd = [str(c) for c in cmd]
        if cmd[1:] == ["--help"]:
            self.help_probes += 1
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Usage: marker_single [OPTIONS] FPATH\n  --page_range TEXT  pages to convert\n", stderr=""
            )
        self.argv.append(cmd)
        input_path = Path(cmd[1])
        out_dir = Path(cmd[cmd.index("--output_dir") + 1])
        if "--page_range" in cmd:
            first, last = (int(x) for x in cmd[cmd.index("--page_range") + 1].split("-"))
        else:
            first, last = 0, self.total_pages - 1
        if self.on_range is not None:
            self.on_range(first, last)
        body = "".join(
            f"{{{n}}}------------------------------------------------\n\nbody of page {n}\n\n"
            for n in range(first, last + 1)
        )
        target = out_dir / input_path.stem
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{input_path.stem}.md").write_text(body, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def ranges_run(self) -> list[str]:
        return [a[a.index("--page_range") + 1] for a in self.argv if "--page_range" in a]


def _dev_config(tmp_path: Path, **ocr: Any) -> dict[str, Any]:
    """A DEV-shaped config, both stages naming local installs, with NO
    ``executor`` key anywhere."""
    module_dir = tmp_path / "embeddings"
    module_dir.mkdir(exist_ok=True)
    table = {"backend": "marker", "marker_single_exe": FAKE_EXE, "timeout_s": 60, **ocr}
    assert "executor" not in table
    return {
        "program": {"id": "c5-dev-root"},
        "ingest": {
            "ocr": table,
            "embed": {
                "backend": "stub-real-embed",
                "python_exe": sys.executable,
                "module_dir": str(module_dir),
                "model_key": "stub-real-embed",
                "dims": 8,
            },
        },
    }


def _queue_pdf(root: Path, pdf: Path, *, page_count: int | None) -> dict:
    return protocol.queue_marker(
        root,
        job_id=JOB,
        stage="ocr",
        doc_id="DOC-c5",
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


def _published(root: Path) -> dict:
    return json.loads((protocol.done_dir(root) / JOB / protocol.RESULT_FILENAME).read_text(encoding="utf-8"))


def _pages(root: Path) -> list[dict]:
    return json.loads((protocol.done_dir(root) / JOB / "pages.json").read_text(encoding="utf-8"))["pages"]


@pytest.fixture
def admissions(monkeypatch) -> list[Any]:
    """Every backend the admission step resolved. With the executor absent
    none of them may carry an ``admit``: nothing is asked of any of them."""
    seen: list[Any] = []
    original = ResidentBackends.admission_backend

    def spy(self, stage):
        backend = original(self, stage)
        seen.append(backend)
        return backend

    monkeypatch.setattr(ResidentBackends, "admission_backend", spy)
    return seen


def _worker(root: Path, tmp_path: Path, backends: ConfigDevBackends, transport=None, **kwargs) -> dict:
    return run_worker(
        transport=transport or LocalTransport(root),
        backends=backends,
        work_root=tmp_path / "work",
        heartbeat_interval_s=FAST_BEAT,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# the routing itself
# ---------------------------------------------------------------------------
def test_an_absent_executor_builds_the_plain_local_marker_backend(tmp_path):
    backends = ConfigDevBackends(_dev_config(tmp_path), root=tmp_path)
    backend = backends.ocr()
    assert type(backend) is RealMarkerOcrBackend
    for seam in ("admit", "on_pause", "result_fields", "startup", "runtime_report"):
        assert not hasattr(backend, seam), seam
    assert backend.marker_single_exe == FAKE_EXE
    described = backends.describe()["stages"]["ocr"]
    assert described["constructed"] is True and described["executor"] == "local"
    assert "vastai" not in described


def test_load_ocr_backend_is_unchanged_without_an_executor_and_refuses_an_unknown_one():
    table = {"backend": "marker", "marker_single_exe": FAKE_EXE}
    assert type(load_ocr_backend(table)) is RealMarkerOcrBackend
    assert type(load_ocr_backend({**table, "executor": "local"})) is RealMarkerOcrBackend
    with pytest.raises(ValueError, match="unknown ingest.ocr.executor 'gpu-cloud'"):
        load_ocr_backend({**table, "executor": "gpu-cloud"})
    with pytest.raises(ValueError, match="runs only inside the DEV offload worker"):
        load_ocr_backend({**table, "executor": "vastai"})


def test_the_probe_seam_still_returns_this_machines_probe():
    backend = RealMarkerOcrBackend(marker_single_exe=FAKE_EXE)
    assert isinstance(backend._peak_rss_probe(), backends_mod._PeakRssProbe)


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------
def test_a_single_range_job_runs_and_publishes_exactly_as_before(tmp_path, monkeypatch, admissions):
    pages = 2
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * pages)
    _queue_pdf(root, pdf, page_count=pages)
    fake = FakeMarker(pages)
    monkeypatch.setattr(backends_mod.subprocess, "run", fake)

    summary = _worker(root, tmp_path, ConfigDevBackends(_dev_config(tmp_path), root=tmp_path))

    assert set(summary) == SUMMARY_KEYS, "no `refused` key on a run that refused nothing"
    assert summary["published"] == [JOB] and summary["failed"] == [] and summary["stopped"] == []
    assert summary["message"] == IDLE_MESSAGE
    # One invocation, the unchunked command byte for byte, and no --help probe.
    assert fake.help_probes == 0
    assert len(fake.argv) == 1
    argv = fake.argv[0]
    assert argv[0] == FAKE_EXE and Path(argv[1]).name == "input.pdf"
    assert argv[2:4] == ["--paginate_output", "--output_dir"]
    assert argv[5:] == ["--disable_tqdm", "--disable_multiprocessing"]
    assert _pages(root) == [
        {"page_number": 0, "text": "body of page 0"},
        {"page_number": 1, "text": "body of page 1"},
    ]
    result = _published(root)
    assert set(result) == BASE_RESULT_KEYS
    assert (result["backend"], result["version"]) == ("marker", "1.10.2")
    assert admissions and all(not hasattr(b, "admit") for b in admissions)


def test_a_multi_range_job_plans_numbers_caches_and_publishes_exactly_as_before(tmp_path, monkeypatch, admissions):
    pages = 10
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * pages)
    _queue_pdf(root, pdf, page_count=pages)
    fake = FakeMarker(pages)
    monkeypatch.setattr(backends_mod.subprocess, "run", fake)
    lines: list[str] = []

    summary = _worker(
        root, tmp_path, ConfigDevBackends(_dev_config(tmp_path, max_range_pixels=BUDGET), root=tmp_path), log=lines.append
    )

    assert set(summary) == SUMMARY_KEYS
    assert summary["published"] == [JOB] and summary["message"] == IDLE_MESSAGE
    assert fake.help_probes == 1
    assert fake.ranges_run() == ["0-2", "3-5", "6-8", "9-9"]
    assert [p["page_number"] for p in _pages(root)] == list(range(pages))
    assert [p["text"] for p in _pages(root)] == [f"body of page {n}" for n in range(pages)]
    result = _published(root)
    assert set(result) == CHUNKED_RESULT_KEYS, "no vastai block, nothing new"
    assert result["page_count"] == pages
    assert result["page_range_numbering"] == "absolute"
    assert (result["planning_dpi"], result["planning_dpi_source"]) == (DEFAULT_BOUNDED_DPI, "bounded_dpi")
    assert [(r["first_page"], r["last_page"]) for r in result["ranges"]] == [(0, 2), (3, 5), (6, 8), (9, 9)]
    assert all(set(r) == RANGE_RECORD_KEYS and r["cached"] is False for r in result["ranges"])
    # The finished document's range cache is removed, as before.
    assert not (tmp_path / "work" / OCR_RANGE_CACHE_DIRNAME / JOB).exists()
    assert any("ocr planned as 4 range(s) of up to 3 page(s) over 10 page(s)" in line for line in lines)
    assert admissions and all(not hasattr(b, "admit") for b in admissions)


def test_a_stopped_multi_range_job_resumes_from_the_cache_exactly_as_before(tmp_path, monkeypatch):
    pages = 10
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * pages)
    _queue_pdf(root, pdf, page_count=pages)
    config = _dev_config(tmp_path, max_range_pixels=BUDGET)

    # Run 1: a stop that is certainly in hand at the first range boundary.
    transport = ControlTransport(root, word_for=lambda beat, job, payload: "stop" if job == JOB else "none")
    first = FakeMarker(pages, on_range=lambda a, b: transport.wait_for_beats(JOB, 2) if a == 0 else None)
    monkeypatch.setattr(backends_mod.subprocess, "run", first)
    stopped = _worker(root, tmp_path, ConfigDevBackends(config, root=tmp_path), transport=transport)
    assert set(stopped) == SUMMARY_KEYS
    assert stopped["stopped"] == [JOB] and stopped["message"] == STOPPED_MESSAGE
    assert first.ranges_run() == ["0-2"]
    assert (tmp_path / "work" / OCR_RANGE_CACHE_DIRNAME / JOB / "range-000000-000002.md").is_file()

    # Run 2: only the ranges never produced are invoked; the first comes from the cache.
    second = FakeMarker(pages)
    monkeypatch.setattr(backends_mod.subprocess, "run", second)
    resumed = _worker(root, tmp_path, ConfigDevBackends(config, root=tmp_path))
    assert resumed["published"] == [JOB] and resumed["message"] == IDLE_MESSAGE
    assert second.ranges_run() == ["3-5", "6-8", "9-9"]
    assert [r["cached"] for r in _published(root)["ranges"]] == [True, False, False, False]
    assert [p["page_number"] for p in _pages(root)] == list(range(pages))


def test_an_empty_queue_still_says_the_machine_is_free(tmp_path):
    root = protocol.ensure_layout(tmp_path / "offload")
    summary = _worker(root, tmp_path, ConfigDevBackends(_dev_config(tmp_path), root=tmp_path))
    assert summary == {
        "claimed": [],
        "published": [],
        "failed": [],
        "lost": [],
        "stopped": [],
        "control": [],
        "polls": 1,
        "message": IDLE_MESSAGE,
    }


def test_a_marker_failure_still_burns_an_attempt_rather_than_refusing(tmp_path, monkeypatch):
    """Settlement class E is unchanged: a failing invocation publishes an
    ``error.json`` (the queue side counts the attempt); it is not a refusal."""
    root = protocol.ensure_layout(tmp_path / "offload")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * 2)
    _queue_pdf(root, pdf, page_count=2)
    real = subprocess.run

    def failing(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == FAKE_EXE:
            return subprocess.CompletedProcess(list(cmd), 1, stdout="", stderr="Traceback: boom")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(backends_mod.subprocess, "run", failing)
    summary = _worker(root, tmp_path, ConfigDevBackends(_dev_config(tmp_path), root=tmp_path))
    assert set(summary) == SUMMARY_KEYS
    assert summary["failed"] == [JOB]
    where = [d / JOB / protocol.ERROR_FILENAME for d in (protocol.done_dir(root), protocol.failed_dir(root))]
    found = [p for p in where if p.is_file()]
    assert found, "the failure travelled back as an error.json"
    assert "marker_single exited 1" in json.loads(found[0].read_text(encoding="utf-8"))["error"]
