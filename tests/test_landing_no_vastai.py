"""Landing: what the DEV worker does with each shape its program root can have.

The vast.ai OCR executor lands on a machine whose OCR worker is running right
now, from a program root that may still carry a canary's ``[vastai*]`` tables.
Three shapes, and what each one means for that worker once the landed code is
what it runs:

* **no vast.ai configuration at all** (the root before any canary) -- the
  plain local marker backend, and the worker never imports the vast.ai package;
* **the ``[vastai*]`` tables present, ``executor`` absent or ``"local"``** --
  exactly the same: the tables are read only by ``vastai`` verbs and by the
  doctor, never by the worker;
* **``executor = "vastai"``** -- OCR is routed to a rented GPU. Without a valid
  ``approve-ocr`` approval every OCR job is refused (class R) and left pending,
  burning no attempt (``tests/test_vastai_worker.py``,
  ``test_without_an_approval_the_job_is_refused_before_the_pull``): OCR stops on
  that machine, nothing is sent and nothing is spent.

So a worker that must stay local is switched back BEFORE it runs the landed
code; the existing end-to-end proof that the local path is unchanged is
``tests/test_offload_executor_absent_regression.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.ingest.backends import RealMarkerOcrBackend
from trialerror.offload.worker import ConfigDevBackends
from tests._vastai_fakes import write_key

FAKE_EXE = "landing-fake-marker-single"

#: The ``[vastai*]`` tables a canary leaves in a DEV root (the shape and caps of
#: the 2026-09-19 canary, with a synthetic document digest; the three paths are
#: relative to the root and name files this test never creates except the fake
#: API key).
CANARY_TABLES = {
    "api_key_path": "keys/vastai.key",
    "ssh_identity_path": "keys/vastai_ed25519",
    "approval_path": "keys/vastai-ocr.approval",
    "tier": "mid",
    "max_job_usd": 1.00,
    "max_run_usd": 2.00,
    "max_approval_usd": 25.00,
    "ocr": {"max_range_pages": 2, "max_leases_per_job": 1},
    "egress": {
        "allow_documents": ["0" * 63 + "1"],
        "require_datacenter": False,
        "require_verified": True,
        "remote_scratch": "shm",
        "require_approval": True,
        "approval_max_days": 7,
        "when_refused": "return",
    },
}


def _dev_config(*, executor: str | None, vastai: bool) -> dict:
    ocr = {"backend": "marker", "marker_single_exe": FAKE_EXE, "timeout_s": 60, "marker_version": "1.10.2"}
    if executor is not None:
        ocr["executor"] = executor
    config = {"program": {"id": "landing-dev-root"}, "ingest": {"require_real_backends": True, "ocr": ocr}}
    if vastai:
        config["vastai"] = json.loads(json.dumps(CANARY_TABLES))
    return config


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "devroot"
    write_key(root / "keys")
    return root


def _assert_plain_local(backends: ConfigDevBackends) -> None:
    backend = backends.ocr()
    assert type(backend) is RealMarkerOcrBackend
    for seam in ("admit", "on_pause", "result_fields", "startup", "runtime_report"):
        assert not hasattr(backend, seam), seam
    described = backends.describe()["stages"]["ocr"]
    assert described["executor"] == "local"
    assert "vastai" not in described


@pytest.mark.parametrize(
    "executor, vastai",
    [(None, False), (None, True), ("local", True)],
    ids=["no-vastai-config", "canary-tables-no-executor", "canary-tables-executor-local"],
)
def test_the_worker_stays_local_unless_the_executor_says_vastai(tmp_path, executor, vastai):
    root = _root(tmp_path)
    _assert_plain_local(ConfigDevBackends(_dev_config(executor=executor, vastai=vastai), root=root))


def test_executor_vastai_routes_ocr_to_the_rented_backend(tmp_path):
    """The shape the canary left: the landed worker builds the vast.ai
    backend -- which is why a root that must stay local is switched back
    before the landed code runs from it."""
    from trialerror.vastai.ocr import VastaiMarkerOcrBackend

    root = _root(tmp_path)
    backends = ConfigDevBackends(_dev_config(executor="vastai", vastai=True), root=root)
    assert backends.executor() == "vastai"
    assert isinstance(backends.ocr(), VastaiMarkerOcrBackend)


def test_a_root_without_executor_vastai_never_imports_the_vastai_package(tmp_path):
    """The worker's routing is lazy: building and describing the OCR backend of
    a root that does not name the executor -- tables present or not -- loads
    nothing from ``trialerror.vastai``. Run in a fresh interpreter, because
    this test process has imported the package already."""
    root = _root(tmp_path)
    configs = {
        "none": _dev_config(executor=None, vastai=False),
        "tables": _dev_config(executor="local", vastai=True),
    }
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from trialerror.offload.worker import ConfigDevBackends\n"
        "import trialerror.offload.stage\n"
        "configs = json.loads(sys.argv[1])\n"
        "for name, config in configs.items():\n"
        "    backends = ConfigDevBackends(config, root=Path(sys.argv[2]))\n"
        "    backends.ocr()\n"
        "    backends.describe()\n"
        "loaded = sorted(m for m in sys.modules if m == 'trialerror.vastai' or m.startswith('trialerror.vastai.'))\n"
        "print(json.dumps(loaded))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, json.dumps(configs), str(root)],
        capture_output=True, text=True, timeout=120, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
