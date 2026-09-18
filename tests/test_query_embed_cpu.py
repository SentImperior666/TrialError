"""CPU query embedding for offload programs (docs/VASTAI_EMBED_DESIGN.md
section 6; TRIALERROR_FEEDBACK.md item 15).

The real Qwen3-Embedding-4B is NEVER loaded here: ``module_dir`` points at
a stub ``embed_backend.py`` exposing the same ``_REGISTRY`` shape, whose
vectors reproduce ``FakeEmbedBackend``'s -- i.e. the "chunk" vectors the
fixture corpus stored. The real driver subprocess still runs, so the
device override, ``CUDA_VISIBLE_DEVICES`` scrubbing, and the calibration
probe are exercised end to end. A real-model check is left to the operator.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests._retrieve_fixtures import build_small_corpus
from trialerror.ingest.backends import CpuQueryEmbedBackend, QueryEmbedUnavailable, load_query_embed_backend
from trialerror.retrieve import engine

STUB = '''
import hashlib, json, os, struct
from types import SimpleNamespace

SALT = {salt!r}

class _Stub:
    def __init__(self):
        self.cfg = SimpleNamespace(device="cuda", dtype="bfloat16", quant_4bit=False)
    def load(self):
        with open(os.path.join(os.path.dirname(__file__), "seen.json"), "w") as f:
            json.dump({{"device": self.cfg.device, "dtype": self.cfg.dtype,
                       "cuda_visible": os.environ.get("CUDA_VISIBLE_DEVICES")}}, f)
        return self
    def embed_batch(self, texts, kind="document"):
        return [self._one(SALT + t) for t in texts]
    def _one(self, text, dims=16):
        need, digest, c = dims * 4, b"", 0
        while len(digest) < need:
            digest += hashlib.sha256(f"{{text}}::{{c}}".encode("utf-8")).digest(); c += 1
        raw = struct.unpack(f"<{{dims}}I", digest[:need])
        fl = [(v / 0xFFFFFFFF) * 2.0 - 1.0 for v in raw]
        n = sum(x * x for x in fl) ** 0.5 or 1.0
        return [x / n for x in fl]

_REGISTRY = {{"fake-16": lambda: _Stub()}}
'''


def _stub_module(tmp_path: Path, *, salt: str = "") -> Path:
    d = tmp_path / f"embeddings_local{salt}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "embed_backend.py").write_text(STUB.format(salt=salt), encoding="utf-8")
    return d


def _toml(program_root: Path, module_dir: Path | None) -> None:
    lines = [
        "[program]", 'id = "qembed-test"', "",
        "[ingest.embed]", 'backend = "offload"', 'model_key = "fake-16"', "dims = 16", 'gpu = "vastai"', "",
    ]
    if module_dir is not None:
        lines += [
            "[ingest.embed.query]",
            f"python_exe = {json.dumps(sys.executable)}",
            f"module_dir = {json.dumps(str(module_dir))}",
            "",
        ]
    (program_root / "trialerror.toml").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


def test_auto_vector_and_hybrid_work_with_query_embedded_on_cpu(store, corpus, tmp_path):
    mod = _stub_module(tmp_path)
    _toml(store.program_root, mod)
    for mode in ("auto", "hybrid", "vector"):
        r = engine.search(store, query="retry budgets", mode=mode)
        assert "vector" in r["tiers_used"], (mode, r["stats"])
        assert "vector_unavailable" not in r["stats"]
    seen = json.loads((mod / "seen.json").read_text())
    assert seen["device"] == "cpu"
    assert seen["cuda_visible"] == ""  # the GPU is never visible to the query path
    assert seen["dtype"] == "bfloat16"
    cal = json.loads((store.program_root / "offload" / "query-embed-calibration.json").read_text())
    assert cal["passed"] is True and cal["min_cosine"] > 0.9999


def test_incomparable_query_embedder_is_refused_and_auto_degrades(store, corpus, tmp_path, capsys):
    _toml(store.program_root, _stub_module(tmp_path, salt="DRIFT"))  # different vectors from the stored ones
    r = engine.search(store, query="retry budgets", mode="auto")
    assert r["tiers_used"] == ["fts"]
    assert "not comparable" in r["stats"]["vector_unavailable"]
    assert "vector tier unavailable" in capsys.readouterr().err
    with pytest.raises(QueryEmbedUnavailable):
        engine.search(store, query="retry budgets", mode="vector")


def test_unconfigured_query_embedder_no_longer_crashes_auto(store, corpus):
    """Item 15's regression: a bare `query search` in an offload program."""
    _toml(store.program_root, None)
    r = engine.search(store, query="retry budgets", mode="auto")
    assert r["ok"] is True and r["tiers_used"] == ["fts"]
    assert "[ingest.embed.query]" in r["stats"]["vector_unavailable"]
    with pytest.raises(QueryEmbedUnavailable):
        engine.search(store, query="retry budgets", mode="vector")


def test_query_backend_inherits_model_key_and_dims_and_rejects_quantised_precision(tmp_path):
    cfg = {"backend": "offload", "model_key": "qwen3-4b", "dims": 2048, "gpu": "vastai",
           "query": {"python_exe": "py", "module_dir": str(tmp_path)}}
    b = load_query_embed_backend(cfg)
    assert isinstance(b, CpuQueryEmbedBackend)
    assert (b.model_key, b.dims, b.precision) == ("qwen3-4b", 2048, "bfloat16")
    with pytest.raises(QueryEmbedUnavailable):
        load_query_embed_backend({**cfg, "query": {**cfg["query"], "precision": "int8"}})
    # non-offload programs keep today's behaviour exactly
    assert type(load_query_embed_backend({"backend": "fake", "dims": 16})).__name__ == "FakeEmbedBackend"
