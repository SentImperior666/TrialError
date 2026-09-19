"""Lane F-1b item 1: the CPU quota reader, and the thread setting an
embedding decode actually runs on.

Why this file exists at all: the in-process encoder was measured at 22.5 s
for a 64-token query and 3.2 s for the same query with one kwarg changed,
and the kwarg the harness was passing (``n_threads``) is not the one the
decode uses (``n_threads_batch``). Nothing about that failure is visible in
an output -- the vector is identical to the bit -- so the only place it can
be caught is here, in a test that reads the kwargs the loader was handed and
the quota the default was derived from.

No model, no library, no CPU-minute: the quota reader takes a fixture tree
and the ``Llama`` constructor is a recording stand-in.
"""

from __future__ import annotations

import os
import types

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import (
    LLAMA_CPP_BACKEND_NAME,
    LlamaCppEmbedBackend,
    cgroup_cpu_quota,
    embed_backend_runtime_details,
    load_embed_backend,
    load_query_embed_backend,
)


@pytest.fixture(autouse=True)
def _clear_resident_cache():
    backends._LLAMA_INSTANCES.clear()
    yield
    backends._LLAMA_INSTANCES.clear()


def _cpu_count() -> int:
    return os.cpu_count() or 1


# ---------------------------------------------------------------------------
# cgroup_cpu_quota: v2, v1, unlimited, unreadable
# ---------------------------------------------------------------------------


def test_v2_cpu_max_is_read_as_quota_over_period(tmp_path):
    """The container measured in lane F-1: ``1000000 100000`` = 10 CPUs."""
    (tmp_path / "cpu.max").write_text("1000000 100000\n", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == 10


def test_v2_max_means_unlimited_and_falls_back_to_the_visible_cpus(tmp_path):
    (tmp_path / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == _cpu_count()


def test_v2_a_fractional_allowance_floors_and_never_reaches_zero(tmp_path):
    """1.5 CPUs is one thread's worth of room, not two -- and a quota
    smaller than a single period still has to leave one thread."""
    (tmp_path / "cpu.max").write_text("150000 100000", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == 1
    (tmp_path / "cpu.max").write_text("50000 100000", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == 1


def test_v2_a_period_less_line_defaults_to_the_kernel_period(tmp_path):
    (tmp_path / "cpu.max").write_text("400000", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == 4


def test_v1_reads_the_cfs_quota_and_period_pair(tmp_path):
    controller = tmp_path / "cpu"
    controller.mkdir()
    (controller / "cpu.cfs_quota_us").write_text("600000\n", encoding="utf-8")
    (controller / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == 6


def test_v1_also_reads_the_combined_cpu_cpuacct_layout(tmp_path):
    controller = tmp_path / "cpu,cpuacct"
    controller.mkdir()
    (controller / "cpu.cfs_quota_us").write_text("200000", encoding="utf-8")
    (controller / "cpu.cfs_period_us").write_text("100000", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == 2


def test_v1_quota_minus_one_is_unlimited(tmp_path):
    controller = tmp_path / "cpu"
    controller.mkdir()
    (controller / "cpu.cfs_quota_us").write_text("-1", encoding="utf-8")
    (controller / "cpu.cfs_period_us").write_text("100000", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == _cpu_count()


def test_an_unreadable_cgroup_tree_falls_back_to_the_visible_cpus(tmp_path):
    assert cgroup_cpu_quota(cgroup_root=tmp_path / "nothing-here") == _cpu_count()


def test_a_malformed_cpu_max_falls_back_rather_than_raising(tmp_path):
    (tmp_path / "cpu.max").write_text("not-a-number 100000", encoding="utf-8")
    assert cgroup_cpu_quota(cgroup_root=tmp_path) == _cpu_count()


def test_the_real_host_reading_is_a_positive_int():
    """Whatever this machine's cgroup says, the reader answers with a usable
    thread count -- the fallback path included."""
    quota = cgroup_cpu_quota()
    assert isinstance(quota, int) and quota >= 1


# ---------------------------------------------------------------------------
# the default, and what reaches Llama(...)
# ---------------------------------------------------------------------------


def _recording_llama_module(constructed: list[dict]) -> types.ModuleType:
    class RecordingLlama:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def tokenize(self, text: bytes, add_bos: bool = True, special: bool = False):
            return list(text)

        def detokenize(self, ids) -> bytes:
            return bytes(ids)

        def embed(self, text: str, normalize: bool = True):
            return [0.5] * 8

    module = types.ModuleType("llama_cpp")
    module.Llama = RecordingLlama  # type: ignore[attr-defined]
    return module


def test_n_threads_batch_defaults_to_the_quota_capped_thread_count(monkeypatch):
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **_kw: 10)
    backend = LlamaCppEmbedBackend(model_path="m", model_key="k", dims=4, native_dims=8, n_threads=12)
    assert backend.n_threads == 12
    assert backend.n_threads_batch == 10, "12 threads inside a 10-CPU quota is the measured pathology"
    assert backend.cpu_quota == 10


def test_the_thread_cap_never_exceeds_the_quota_for_any_visible_cpu_count(monkeypatch):
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **_kw: 3)
    for n_threads in (1, 2, 3, 4, 8, 12, 64):
        backend = LlamaCppEmbedBackend(
            model_path="m", model_key="k", dims=4, native_dims=8, n_threads=n_threads
        )
        assert backend.n_threads_batch <= 3
        assert backend.n_threads_batch == min(n_threads, 3)


def test_a_quota_above_the_thread_count_does_not_inflate_the_pool(monkeypatch):
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **_kw: 64)
    backend = LlamaCppEmbedBackend(model_path="m", model_key="k", dims=4, native_dims=8, n_threads=4)
    assert backend.n_threads_batch == 4


def test_an_explicit_config_value_overrides_the_default(monkeypatch):
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **_kw: 10)
    backend = load_query_embed_backend(
        {
            "backend": "offload",
            "model_key": "real-key",
            "dims": 2048,
            "query": {
                "backend": LLAMA_CPP_BACKEND_NAME,
                "model_path": "/nonexistent/model.gguf",
                "n_threads": 12,
                "n_threads_batch": 7,
            },
        }
    )
    assert backend.n_threads_batch == 7, "[ingest.embed.query] n_threads_batch has to win"


def test_the_kwarg_actually_reaches_the_library(monkeypatch):
    """The whole bug was a kwarg that was never passed. Assert on the
    constructor's own arguments, not on the backend's attribute."""
    constructed: list[dict] = []
    monkeypatch.setitem(__import__("sys").modules, "llama_cpp", _recording_llama_module(constructed))
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **_kw: 10)

    backend = load_embed_backend(
        {
            "backend": LLAMA_CPP_BACKEND_NAME,
            "model_path": "/nonexistent/model.gguf",
            "model_key": "k",
            "dims": 4,
            "native_dims": 8,
            "n_ctx": 64,
            "n_threads": 12,
        }
    )
    backend._resident()
    assert len(constructed) == 1
    assert constructed[0]["n_threads"] == 12
    assert constructed[0]["n_threads_batch"] == 10


# ---------------------------------------------------------------------------
# reporting: the numbers reach a reason string and a doctor detail
# ---------------------------------------------------------------------------


def test_runtime_details_carry_the_thread_trio(monkeypatch):
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **_kw: 10)
    backend = LlamaCppEmbedBackend(
        model_path="m", model_key="k", dims=4, native_dims=8, n_ctx=64, n_threads=12
    )
    details = embed_backend_runtime_details(backend)
    assert details["backend"] == LLAMA_CPP_BACKEND_NAME
    assert details["n_threads"] == 12
    assert details["n_threads_batch"] == 10
    assert details["cgroup_cpu_quota"] == 10
    assert details["n_ctx"] == 64


def test_a_refusal_reason_names_the_thread_settings(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "llama_cpp", types.ModuleType("llama_cpp"))
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **_kw: 10)
    backend = LlamaCppEmbedBackend(
        model_path="/nonexistent/model.gguf", model_key="k", dims=4, native_dims=8, n_threads=12
    )
    ok, reason = backend.runnable()
    assert ok is False
    assert "n_threads=12" in reason and "n_threads_batch=10" in reason and "quota=10" in reason


def test_runtime_details_of_a_backend_without_the_method_is_empty():
    assert embed_backend_runtime_details(backends.FakeEmbedBackend()) == {}


def test_runtime_details_never_raises_out_of_a_broken_backend():
    class Broken:
        def runtime_details(self):
            raise RuntimeError("no")

    assert "runtime_details_error" in embed_backend_runtime_details(Broken())
