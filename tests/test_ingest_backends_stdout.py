"""Lane FB-6 item 2: the llama.cpp wheel's own log never reaches stdout.

Observed on a live programme: every embedding call printed

    llama_context: n_ctx_seq (512) > n_ctx_train (0) -- possible training
    context overflow

to file descriptor 1 as the vocab-only tokenizer loaded, ahead of the JSON
envelope the CLI then wrote there. Any ``json.load`` of that output raised,
so a script that called an embedding command could not read its answer.

The text is not discarded -- it goes to stderr, where an operator reading a
terminal still sees it. What the tests below hold is the CLI's one contract:
**exactly one JSON object on stdout, and nothing else.**

The noisy loader is a fake that writes to fd 1 exactly as the wheel does, so
these tests need no model file, no wheel and no GPU.
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest

from trialerror.cli import main
from trialerror.ingest import backends
from trialerror.ingest.backends import LlamaServerEmbedBackend, native_logs_to_stderr
from trialerror.lens.ideas import write_idea
from trialerror.retrieve import engine as retrieve_engine
from trialerror.stores.store import open_store

from tests._inventory_fixtures import bootstrap_launch, build_corpus_with_inventory
from tests.test_ingest_backends_llama_server import DIMS, NATIVE, FakeHttp, FakeVocab

#: Verbatim, because the point of the item is this exact line.
NOISE = b"llama_context: n_ctx_seq (512) > n_ctx_train (0) -- possible training context overflow\n"


class NoisyVocab(FakeVocab):
    """A tokenizer that writes to fd 1 on construction, exactly as the wheel
    does. ``os.write`` rather than ``print``: the real writer is C code
    holding the descriptor, and a fake that went through ``sys.stdout``
    would be caught by a redirect that the real one walks past."""

    def __init__(self, **kwargs):
        super().__init__()
        os.write(1, NOISE)


@pytest.fixture(autouse=True)
def _clear_caches():
    backends._LLAMA_INSTANCES.clear()
    backends._VOCAB_ONLY_INSTANCES.clear()
    yield
    backends._LLAMA_INSTANCES.clear()
    backends._VOCAB_ONLY_INSTANCES.clear()


@pytest.fixture()
def noisy_wheel(monkeypatch, tmp_path):
    module = types.ModuleType("llama_cpp")
    module.Llama = NoisyVocab  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    return gguf


# ---------------------------------------------------------------------------
# the redirect itself
# ---------------------------------------------------------------------------


def test_the_redirect_moves_descriptor_one_to_descriptor_two(capfd):
    os.write(1, b"before\n")
    with native_logs_to_stderr():
        os.write(1, b"inside\n")
    os.write(1, b"after\n")
    out, err = capfd.readouterr()
    assert out == "before\nafter\n"
    assert err == "inside\n"


def test_the_redirect_is_undone_even_when_the_body_raises(capfd):
    with pytest.raises(RuntimeError):
        with native_logs_to_stderr():
            raise RuntimeError("boom")
    os.write(1, b"still mine\n")
    out, _err = capfd.readouterr()
    assert out == "still mine\n"


def test_nothing_is_discarded(capfd):
    with native_logs_to_stderr():
        os.write(1, NOISE)
    _out, err = capfd.readouterr()
    assert "n_ctx_seq" in err, "the operator must still be able to read the warning"


# ---------------------------------------------------------------------------
# through the backend
# ---------------------------------------------------------------------------


def test_the_vocab_only_load_writes_nothing_to_stdout(noisy_wheel, capfd):
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=512,
        tokenizer_model_path=str(noisy_wheel),
        _http=FakeHttp(props_body={"model": str(noisy_wheel)}),
    )
    vectors = backend.embed_batch(["a query to embed"], kind="query")
    assert len(vectors) == 1 and len(vectors[0]) == DIMS
    out, err = capfd.readouterr()
    assert out == "", f"the wheel's loader reached stdout: {out!r}"
    assert "n_ctx_seq" in err


# ---------------------------------------------------------------------------
# through the CLI -- the contract that was actually broken
# ---------------------------------------------------------------------------


@pytest.fixture()
def program_with_a_noisy_backend(tmp_path, monkeypatch, noisy_wheel):
    """A program whose query-side embed backend is the sidecar client over a
    tokenizer that prints on load -- the live shape, with the socket and the
    GGUF faked out."""
    platform_root = tmp_path / "platform_root"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir(parents=True, exist_ok=True)

    store = open_store(program_root, platform_root=platform_root)
    corpus = build_corpus_with_inventory(store)
    model_key, _real = retrieve_engine._resolve_embed_backend(store, side="query")
    launch = bootstrap_launch(
        store, attrs={"lens_name": "lens-1", "slice_doc_ids": corpus["corpus_doc_ids"][:1]},
        purpose="ideation",
    )
    write_idea(
        store, round_id="round-noisy", author_launch=launch,
        body="A statement the mechanical screen has to embed before it can say anything.",
        home="family-a/row-1", tier="near",
        provenance={"docs": corpus["corpus_doc_ids"][:1]},
        operation_declared="bridge/synthesis-unify",
    )
    store.close()

    noisy = LlamaServerEmbedBackend(
        model_key=model_key, dims=DIMS, native_dims=NATIVE, n_ctx=512,
        tokenizer_model_path=str(noisy_wheel),
        _http=FakeHttp(props_body={"model": str(noisy_wheel)}),
    )
    monkeypatch.setattr(
        retrieve_engine, "_resolve_embed_backend",
        lambda _store, side="document": (model_key, noisy),
    )
    monkeypatch.setattr(retrieve_engine, "query_embed_runnable", lambda _store: (True, ""))
    return program_root


def test_a_cli_command_that_embeds_emits_exactly_one_json_object_on_stdout(
    program_with_a_noisy_backend, capfd
):
    exit_code = main([
        "lens", "--program-root", str(program_with_a_noisy_backend), "screen",
        "--round-id", "round-noisy", "--mechanical",
    ])
    out, err = capfd.readouterr()
    # The contract, asserted the way a caller consumes it: one json.loads of
    # the WHOLE stream. A single stray line ahead of the envelope fails here,
    # which is precisely the live failure.
    envelope = json.loads(out)
    assert isinstance(envelope, dict)
    assert exit_code == 0, envelope
    assert envelope["result"]["mechanical"]["n_screened"] == 1
    assert "n_ctx_seq" in err, "the warning is moved, not swallowed"
