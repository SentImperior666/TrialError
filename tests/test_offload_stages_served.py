"""O5: a worker's start-up check covers only the stages it serves.

``--stages ocr`` (the OCR-only worker) needs no runnable ``[ingest.embed]``;
``--stages embed`` builds no OCR backend -- with ``executor = "vastai"``, no
vast.ai backend and no vast.ai call. With every stage served (the default)
the start-up check is exactly what it was, refusal text and call included.

The OCR side runs DEV's own marker path through the platform-neutral fake
``marker_single`` of the executor-absent regression test (imported, not
copied). No network, no ssh, no GPU.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import trialerror.ingest.backends as backends_mod
from tests._ocr_range_fixtures import A4_PT, make_pdf
from tests._offload_fixtures import queue_chunks
from tests._vastai_fakes import dev_toml, isolated_state, network_tripwire  # noqa: F401 - fixtures
from tests._vastai_shell_fakes import RecordingTransport, ssh_tripwire  # noqa: F401 - fixture
from tests.test_offload_executor_absent_regression import FAST_BEAT, JOB, FakeMarker, _dev_config, _queue_pdf
from trialerror.offload import protocol
from trialerror.offload.worker import (
    STAGE_NOT_SERVED,
    ConfigDevBackends,
    WorkerConfigError,
    run_worker,
    unrunnable_backend_message,
)

EMBED_JOB = "JOB-served-embed-1"
OCR_JOB = "JOB-served-ocr-1"


@pytest.fixture(autouse=True)
def _no_network_no_ssh(network_tripwire, isolated_state, ssh_tripwire):  # noqa: F811
    yield


class _Built(Exception):
    """Raised by the spy the moment anything constructs a vast.ai backend."""


def _ocr_only_config(tmp_path: Path) -> dict[str, Any]:
    """DEV's own marker for OCR and NO ``[ingest.embed]`` table at all."""
    config = _dev_config(tmp_path)
    del config["ingest"]["embed"]
    assert set(config["ingest"]) == {"ocr"}
    return config


def _count_calls(monkeypatch, name: str) -> list[int]:
    """Count the calls of one ``ConfigDevBackends`` resolver."""
    calls: list[int] = []
    original = getattr(ConfigDevBackends, name)

    def spy(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(ConfigDevBackends, name, spy)
    return calls


def _run(root: Path, tmp_path: Path, backends: Any, transport: Any, **kwargs: Any) -> dict:
    return run_worker(transport=transport, backends=backends, work_root=tmp_path / "work",
                      heartbeat_interval_s=FAST_BEAT, **kwargs)


# ---------------------------------------------------------------------------
# (a) --stages ocr on a root with no [ingest.embed]
# ---------------------------------------------------------------------------
def test_an_ocr_only_worker_starts_without_an_embed_table_runs_ocr_and_returns_embed_unrun(tmp_path, monkeypatch):
    pages = 2
    root = protocol.ensure_layout(tmp_path / "offload")
    _queue_pdf(root, make_pdf(tmp_path / "doc.pdf", [A4_PT] * pages), page_count=pages)
    queue_chunks(root, EMBED_JOB, count=3)
    fake = FakeMarker(pages)
    monkeypatch.setattr(backends_mod.subprocess, "run", fake)
    embed_built = _count_calls(monkeypatch, "embed")
    transport = RecordingTransport(root)

    summary = _run(root, tmp_path, ConfigDevBackends(_ocr_only_config(tmp_path), root=tmp_path), transport,
                   stages=("ocr",))

    assert summary["published"] == [JOB] and summary["failed"] == []
    assert len(fake.argv) == 1, "the OCR job ran on the (fake) local marker"
    assert [(e["job_id"], e["reason_code"]) for e in summary["refused"]] == [(EMBED_JOB, STAGE_NOT_SERVED)]
    assert ("pull", EMBED_JOB) not in transport.verbs and ("return", EMBED_JOB) in transport.verbs
    assert transport.list_jobs() == [EMBED_JOB], "the embed job is left pending, no attempt burned"
    assert "1 stage-not-served" in summary["message"]
    assert embed_built == [], "an OCR-only worker never constructs an embed backend"


# ---------------------------------------------------------------------------
# (b) the same root, every stage served: refused at start exactly as before
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stages", [None, "ocr,embed", ("embed", "ocr")], ids=["default", "both-named", "reordered"])
def test_the_same_root_serving_every_stage_is_refused_at_start_exactly_as_before(tmp_path, monkeypatch, stages):
    pages = 2
    root = protocol.ensure_layout(tmp_path / "offload")
    _queue_pdf(root, make_pdf(tmp_path / "doc.pdf", [A4_PT] * pages), page_count=pages)
    fake = FakeMarker(pages)
    monkeypatch.setattr(backends_mod.subprocess, "run", fake)
    ocr_built = _count_calls(monkeypatch, "ocr")
    transport = RecordingTransport(root)
    kwargs = {} if stages is None else {"stages": stages}

    with pytest.raises(WorkerConfigError) as refused:
        _run(root, tmp_path, ConfigDevBackends(_ocr_only_config(tmp_path), root=tmp_path), transport, **kwargs)

    assert str(refused.value) == unrunnable_backend_message("embed", "fake")
    assert transport.verbs == [], "refused before the first claim"
    assert ocr_built == [] and fake.argv == [], "the name check still comes before any construction"


def test_serving_every_stage_calls_validate_with_no_argument_and_fewer_passes_them(tmp_path):
    """C5: the default run's start-up call is byte for byte the old one."""

    class Recording:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def validate(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

        def ocr(self) -> Any:  # pragma: no cover - the queue is empty
            raise AssertionError("not reached")

        embed = ocr

    root = protocol.ensure_layout(tmp_path / "offload")
    seen: list[dict[str, Any]] = []
    for stages in (None, "ocr,embed", "ocr", "embed"):
        backends = Recording()
        kwargs = {} if stages is None else {"stages": stages}
        summary = _run(root, tmp_path, backends, RecordingTransport(root), **kwargs)
        assert summary["published"] == [] and "refused" not in summary
        seen.extend(backends.calls)
    assert seen == [{}, {}, {"stages": ("ocr",)}, {"stages": ("embed",)}]


# ---------------------------------------------------------------------------
# (c) --stages embed with [ingest.ocr] executor = "vastai"
# ---------------------------------------------------------------------------
def test_an_embed_only_worker_builds_no_vastai_backend_and_makes_no_vastai_call(
    tmp_path, monkeypatch, network_tripwire, isolated_state  # noqa: F811
):
    from trialerror.vastai.ocr import VastaiMarkerOcrBackend

    built: list[int] = []

    def refuse_to_build(cls, *args: Any, **kwargs: Any) -> None:
        built.append(1)
        raise _Built("a vast.ai OCR backend was being built")

    monkeypatch.setattr(VastaiMarkerOcrBackend, "from_toml", classmethod(refuse_to_build))
    ocr_built = _count_calls(monkeypatch, "ocr")
    module_dir = tmp_path / "embeddings"
    module_dir.mkdir()
    config = dev_toml()
    assert config["ingest"]["ocr"]["executor"] == "vastai"
    config["ingest"]["embed"] = {"backend": "stub-real-embed", "python_exe": sys.executable,
                                 "module_dir": str(module_dir), "model_key": "stub-real-embed", "dims": 8}
    root = protocol.ensure_layout(tmp_path / "offload")
    protocol.queue_marker(
        root, job_id=OCR_JOB, stage="ocr", doc_id="DOC-served-1",
        expect={"stage": "ocr", "backend": "marker", "outputs": ["pages.json"], "input_name": "input.pdf",
                "license_tier": "open"},
        config_hash="c", inputs=[("input.pdf", b"%PDF-1.4 not sent anywhere\n")],
    )
    transport = RecordingTransport(root)

    summary = _run(root, tmp_path, ConfigDevBackends(config, root=tmp_path / "devroot"), transport, stages=("embed",))

    assert [(e["job_id"], e["reason_code"]) for e in summary["refused"]] == [(OCR_JOB, STAGE_NOT_SERVED)]
    assert ("pull", OCR_JOB) not in transport.verbs and ("return", OCR_JOB) in transport.verbs
    assert built == [] and ocr_built == [], "no OCR backend of any kind was built, so no vast.ai one"
    assert network_tripwire == [], "no vast.ai call"
    assert not isolated_state.exists() or list(isolated_state.iterdir()) == [], (
        "no vast.ai start-up ran (no runtime files, no reap, no ledger)"
    )

    # The contrast: the same root serving OCR does reach the vast.ai backend at start.
    with pytest.raises(_Built):
        ConfigDevBackends(config, root=tmp_path / "devroot").validate()
    assert built == [1]
