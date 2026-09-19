"""Lane F-1b stage 3: the in-process encoder's thread settings and the quota
reader, per ``docs/reviews/VERIFY_f1b-sidecar.md``.

- **V-7** -- ``int(n_threads_batch) if n_threads_batch else <default>`` read
  ``0`` as "unset" and passed a negative value verbatim to ``Llama``, where
  llama.cpp resolves a non-positive thread count from the VISIBLE CPU count:
  the override silently restored the 22.5 s-instead-of-3.2 s behaviour item 1
  exists to remove, while the report said ``-4``.
- **V-8** -- the resident-model cache is keyed on ``model_path`` alone, so a
  second backend for one GGUF reported ITS configured thread settings for a
  model the first backend had already built with different ones.
- **V-9** -- the quota reader looked at the cgroup ROOT only, which is this
  process's cgroup only under a private cgroup namespace; with
  ``cgroupns=host`` it fell back to ``os.cpu_count()``, the exact number whose
  use is the bug.

No wheel, no model file: ``Llama`` is a fake module and every cgroup reading
comes from a fixture tree.
"""

from __future__ import annotations

import os
import sys
import types
import unittest.mock as mock
from pathlib import Path

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import (
    LlamaCppEmbedBackend,
    cgroup_cpu_quota,
    load_embed_backend,
)


@pytest.fixture
def fake_llama(monkeypatch):
    """A ``llama_cpp`` module whose ``Llama`` records its kwargs."""
    built: list[dict] = []

    class FakeLlama:
        def __init__(self, **kwargs):
            built.append(kwargs)

    module = types.ModuleType("llama_cpp")
    module.Llama = FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    monkeypatch.setattr(backends, "_LLAMA_INSTANCES", {})
    monkeypatch.setattr(backends, "_LLAMA_INSTANCE_SETTINGS", {})
    return built


@pytest.fixture
def quota_10(monkeypatch):
    monkeypatch.setattr(backends, "cgroup_cpu_quota", lambda **kwargs: 10)


# ---------------------------------------------------------------------------
# V-7: zero and negative are not thread counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -4])
def test_a_non_positive_n_threads_batch_is_refused_at_construction(quota_10, value):
    with pytest.raises(ValueError) as caught:
        LlamaCppEmbedBackend(
            model_path="/x.gguf", model_key="k", dims=8, n_threads=12, n_threads_batch=value
        )
    message = str(caught.value)
    assert "n_threads_batch" in message and str(value) in message
    assert "10" in message, "the refusal names the default it would otherwise have used"


@pytest.mark.parametrize("value", [0, -4])
def test_the_config_key_is_refused_too(quota_10, value):
    with pytest.raises(ValueError):
        load_embed_backend(
            {
                "backend": "llama_cpp", "model_path": "/x.gguf", "model_key": "k",
                "dims": 8, "n_threads": 12, "n_threads_batch": value,
            }
        )


def test_an_unset_n_threads_batch_still_takes_the_default(quota_10):
    backend = LlamaCppEmbedBackend(model_path="/x.gguf", model_key="k", dims=8, n_threads=12)
    assert backend.n_threads_batch == 10


def test_a_positive_override_is_still_honoured_verbatim(quota_10):
    """Disclosed deviation 3 survives: an override above the quota is the
    operator's to make, and only a non-thread-count is refused."""
    backend = LlamaCppEmbedBackend(
        model_path="/x.gguf", model_key="k", dims=8, n_threads=12, n_threads_batch=99
    )
    assert backend.n_threads_batch == 99
    assert backend.runtime_details()["n_threads_batch"] == 99


# ---------------------------------------------------------------------------
# V-8: the report says which numbers the loaded model carries
# ---------------------------------------------------------------------------


def test_one_gguf_is_loaded_once_however_many_backends_want_it(quota_10, fake_llama):
    first = LlamaCppEmbedBackend(model_path="/same.gguf", model_key="k", dims=8, n_threads=12)
    second = LlamaCppEmbedBackend(
        model_path="/same.gguf", model_key="k", dims=8, n_threads=12, n_threads_batch=3
    )
    first._resident()
    second._resident()

    assert len(fake_llama) == 1, "a second multi-gigabyte load would be the worse bug"
    assert fake_llama[0]["n_threads_batch"] == 10


def test_the_second_backend_reports_the_settings_the_model_was_built_with(quota_10, fake_llama):
    """The V-8 repro, as a test."""
    first = LlamaCppEmbedBackend(model_path="/same.gguf", model_key="k", dims=8, n_threads=12)
    second = LlamaCppEmbedBackend(
        model_path="/same.gguf", model_key="k", dims=8, n_threads=12, n_threads_batch=3
    )
    first._resident()
    second._resident()

    details = second.runtime_details()
    assert details["n_threads_batch"] == 3, "its own configuration is still reported"
    assert details["effective"]["n_threads_batch"] == 10, "and so is what the model carries"
    assert "resident" in details["resident_differs"]


def test_a_backend_that_agrees_with_the_resident_says_nothing_extra(quota_10, fake_llama):
    first = LlamaCppEmbedBackend(model_path="/same.gguf", model_key="k", dims=8, n_threads=12)
    first._resident()
    same = LlamaCppEmbedBackend(model_path="/same.gguf", model_key="k", dims=8, n_threads=12)

    details = same.runtime_details()
    assert "effective" not in details and "resident_differs" not in details


def test_a_backend_with_no_resident_yet_says_nothing_extra(quota_10, fake_llama):
    backend = LlamaCppEmbedBackend(
        model_path="/never-loaded.gguf", model_key="k", dims=8, n_threads=12, n_threads_batch=3
    )
    assert "effective" not in backend.runtime_details()


def test_the_detail_stays_json_serialisable(quota_10, fake_llama):
    import json

    first = LlamaCppEmbedBackend(model_path="/same.gguf", model_key="k", dims=8, n_threads=12)
    first._resident()
    second = LlamaCppEmbedBackend(
        model_path="/same.gguf", model_key="k", dims=8, n_threads=12, n_threads_batch=3
    )
    json.dumps(backends.embed_backend_runtime_details(second))


# ---------------------------------------------------------------------------
# V-9: this process's own cgroup, not just the root
# ---------------------------------------------------------------------------


def _tree(tmp_path: Path, files: dict[str, str], self_cgroup: str) -> tuple[Path, Path]:
    root = tmp_path / "cgroup"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    proc = tmp_path / "self_cgroup"
    proc.write_text(self_cgroup, encoding="utf-8")
    return root, proc


def test_a_nested_v2_cgroup_is_read_rather_than_missed(tmp_path):
    """The V-9 repro, as a test: the limit is on the process's own cgroup and
    the root carries nothing."""
    root, proc = _tree(tmp_path, {"nested/cpu.max": "1000000 100000"}, "0::/nested\n")
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=proc) == 10


def test_an_ancestors_limit_is_found_by_walking_up(tmp_path):
    root, proc = _tree(tmp_path, {"a/cpu.max": "400000 100000"}, "0::/a/b/c\n")
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=proc) == 4


def test_the_nearest_limit_wins(tmp_path):
    root, proc = _tree(
        tmp_path,
        {"a/cpu.max": "1200000 100000", "a/b/cpu.max": "300000 100000"},
        "0::/a/b\n",
    )
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=proc) == 3


def test_an_unlimited_child_defers_to_a_limited_ancestor(tmp_path):
    root, proc = _tree(
        tmp_path,
        {"a/cpu.max": "500000 100000", "a/b/cpu.max": "max 100000"},
        "0::/a/b\n",
    )
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=proc) == 5


def test_a_nested_v1_controller_path_is_read(tmp_path):
    root, proc = _tree(
        tmp_path,
        {
            "cpu/docker/abc/cpu.cfs_quota_us": "600000",
            "cpu/docker/abc/cpu.cfs_period_us": "100000",
        },
        "12:cpu,cpuacct:/docker/abc\n",
    )
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=proc) == 6


def test_an_unreadable_proc_file_leaves_the_root_reading_intact(tmp_path):
    root, _proc = _tree(tmp_path, {"cpu.max": "800000 100000"}, "0::/\n")
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=tmp_path / "nope") == 8


def test_nothing_anywhere_is_still_the_documented_fallback(tmp_path):
    root, proc = _tree(tmp_path, {}, "0::/deeply/nested\n")
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=proc) == (os.cpu_count() or 1)


def test_the_live_host_reading_is_unchanged(tmp_path):
    """The one deliberate reading of the real host: it must still be the
    measured quota, not the visible CPU count."""
    live = cgroup_cpu_quota()
    assert live >= 1
    assert live == cgroup_cpu_quota(cgroup_root="/sys/fs/cgroup", proc_self_cgroup="/proc/self/cgroup")


def test_a_malformed_proc_line_is_ignored(tmp_path):
    root, proc = _tree(tmp_path, {"cpu.max": "200000 100000"}, "garbage\n0::relative-not-absolute\n")
    assert cgroup_cpu_quota(cgroup_root=root, proc_self_cgroup=proc) == 2
