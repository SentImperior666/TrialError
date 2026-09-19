"""Lane F-1 item F: the two doctor checks the rest of the lane points at.

Every degrade warning, every refusal message and the operator guide all name
``query_embed_backend_runnable``. This module is what makes that name resolve
to something an operator can run, in each of its four states, plus
``vecmatrix_stale``'s three.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from tests._retrieve_fixtures import build_bulk_corpus, build_small_corpus
from trialerror.ingest.backends import LLAMA_CPP_BACKEND_NAME
from trialerror.retrieve import vecmatrix
from trialerror.retrieve.checks import check_query_embed_backend_runnable, check_vecmatrix_stale
from trialerror.util.doctor import DoctorContext

_OFFLOAD = '[program]\nid = "PROG-test"\n\n[ingest.embed]\nbackend = "offload"\nmodel_key = "real-key"\ndims = 16\n'


def _ctx(program_root) -> DoctorContext:
    return DoctorContext(repo_root=program_root, program_root=program_root)


def _write(program_root, text: str) -> None:
    (program_root / "trialerror.toml").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# query_embed_backend_runnable
# ---------------------------------------------------------------------------


def test_it_skips_without_a_knowledge_db(tmp_path):
    result = check_query_embed_backend_runnable(_ctx(tmp_path))
    assert result.status == "skip"
    assert result.category == "retrieve"


def test_it_skips_a_corpus_with_no_embeddings(store, program_root):
    _write(program_root, _OFFLOAD)
    result = check_query_embed_backend_runnable(_ctx(program_root))
    assert result.status == "skip"
    assert result.details["embeddings"] == 0


def test_it_passes_on_the_zero_setup_default(store, program_root):
    build_small_corpus(store)
    store.knowledge.commit()
    result = check_query_embed_backend_runnable(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["query_backend"] == "same"
    assert result.details["model_key"] == "fake-16"


def test_it_warns_on_an_offload_program_with_no_query_side_backend(store, program_root):
    build_small_corpus(store)
    store.knowledge.commit()
    _write(program_root, _OFFLOAD)
    result = check_query_embed_backend_runnable(_ctx(program_root))
    assert result.status == "warn"
    assert "DEV GPU worker" in result.message
    assert "ingest.embed.query" in result.message
    assert result.details["reason"]
    assert result.details["document_backend"] == "offload"


def test_it_passes_once_a_runnable_query_side_backend_is_named(store, program_root):
    build_small_corpus(store)
    store.knowledge.commit()
    _write(program_root, _OFFLOAD + '\n[ingest.embed.query]\nbackend = "fake"\n')
    result = check_query_embed_backend_runnable(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["query_backend"] == "fake"
    assert result.details["model_key"] == "real-key", "it answers under the corpus's own key"


def test_it_warns_rather_than_raising_on_a_mismatched_table(store, program_root):
    build_small_corpus(store)
    store.knowledge.commit()
    _write(program_root, _OFFLOAD + '\n[ingest.embed.query]\nbackend = "fake"\nmodel_key = "other"\n')
    result = check_query_embed_backend_runnable(_ctx(program_root))
    assert result.status == "warn"
    assert "could not be resolved" in result.message


def test_it_reports_a_missing_encoder_runtime_as_the_reason(store, program_root, monkeypatch):
    """The state the live program is in while the CPU runtime is being
    delivered: the table is right, the library is not there yet."""
    build_small_corpus(store)
    store.knowledge.commit()
    _write(
        program_root,
        _OFFLOAD + f'\n[ingest.embed.query]\nbackend = "{LLAMA_CPP_BACKEND_NAME}"\nmodel_path = "/nonexistent/m.gguf"\nnative_dims = 16\n',
    )
    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    monkeypatch.setattr("builtins.__import__", _blocking_import("llama_cpp"))
    result = check_query_embed_backend_runnable(_ctx(program_root))
    assert result.status == "warn"
    assert "llama_cpp" in result.details["reason"]


def _blocking_import(name: str):
    real_import = __import__

    def _import(module, *args, **kwargs):
        if module == name:
            raise ImportError(f"No module named {name!r}")
        return real_import(module, *args, **kwargs)

    return _import


def test_a_loader_failure_is_reported_as_a_warning_with_its_text(store, program_root, monkeypatch, tmp_path):
    build_small_corpus(store)
    store.knowledge.commit()
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF")
    # as_posix(), because a native Windows path interpolated into a TOML
    # BASIC string is not the path: every backslash starts an escape, so
    # "C:\Users\..." is a TOMLDecodeError, the config reads as {}, and the
    # check then answers about the default backend instead of this one --
    # a pass, not the warn under test. Forward slashes open the same file
    # on both platforms.
    _write(
        program_root,
        _OFFLOAD
        + f'\n[ingest.embed.query]\nbackend = "{LLAMA_CPP_BACKEND_NAME}"\nmodel_path = "{model.as_posix()}"\nnative_dims = 16\n',
    )
    module = types.ModuleType("llama_cpp")

    def _explode(**_kwargs):
        raise OSError("Error loading shared library libllama.so")

    module.Llama = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    result = check_query_embed_backend_runnable(_ctx(program_root))
    assert result.status == "warn"
    assert "libllama.so" in result.details["reason"]


# ---------------------------------------------------------------------------
# vecmatrix_stale
# ---------------------------------------------------------------------------


def test_vecmatrix_skips_without_a_knowledge_db(tmp_path):
    assert check_vecmatrix_stale(_ctx(tmp_path)).status == "skip"


def test_vecmatrix_skips_a_program_with_no_cache(store, program_root):
    build_small_corpus(store)
    store.knowledge.commit()
    result = check_vecmatrix_stale(_ctx(program_root))
    assert result.status == "skip"


def test_vecmatrix_passes_on_a_current_cache(store, program_root):
    pytest.importorskip("numpy")
    built = build_bulk_corpus(store, n_chunks=2_200, n_docs=4, dims=16)
    store.knowledge.commit()
    vecmatrix.clear_process_cache()
    assert vecmatrix.top_ranked(store, built["model_key"], [0.1] * 16, k=5) is not None
    result = check_vecmatrix_stale(_ctx(program_root))
    assert result.status == "pass"


def test_vecmatrix_warns_when_the_fingerprint_has_drifted(store, program_root):
    pytest.importorskip("numpy")
    built = build_bulk_corpus(store, n_chunks=2_200, n_docs=4, dims=16)
    store.knowledge.commit()
    vecmatrix.clear_process_cache()
    vecmatrix.top_ranked(store, built["model_key"], [0.1] * 16, k=5)

    table = f"vec_chunks__{built['model_key'].replace('-', '_')}"
    store.knowledge.execute(f"DELETE FROM {table} WHERE chunk_id = (SELECT chunk_id FROM {table} LIMIT 1)")
    store.knowledge.commit()

    result = check_vecmatrix_stale(_ctx(program_root))
    assert result.status == "warn"
    assert built["model_key"] in result.message
    assert "rebuild" in result.message


def test_both_checks_are_discovered_by_the_generic_doctor_sweep():
    from trialerror.util.doctor import discover_and_register_checks, registered_checks

    discover_and_register_checks()
    registry = registered_checks()
    for name in ("query_embed_backend_runnable", "vecmatrix_stale"):
        assert name in registry and registry[name][0] == "retrieve"


def test_the_checks_are_json_serialisable_like_every_other_one(store, program_root):
    build_small_corpus(store)
    store.knowledge.commit()
    _write(program_root, _OFFLOAD)
    for result in (check_query_embed_backend_runnable(_ctx(program_root)), check_vecmatrix_stale(_ctx(program_root))):
        json.dumps(result.to_dict())
