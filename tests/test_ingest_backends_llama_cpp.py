"""Lane F-1 item C: the llama.cpp query-side embed backend.

The pure parts of the reference recipe -- prompt prefix, truncation point,
EOS room, normalisation order, the empty string -- are tested against an
INJECTED fake ``Llama`` (a class with ``tokenize``/``detokenize``/``embed``),
so this module needs no library, no model file and no CPU-minute. The one
test that needs the real package skips when it is absent.

Why the recipe is worth this much test surface: every deviation on it
produces vectors that look fine (finite, unit-norm, the right width) and are
incomparable with the corpus they will be ranked against. There is no
downstream check that would catch a missing prompt prefix or a
normalise-once-instead-of-twice -- search simply gets quietly worse.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import struct
import sys
import types

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import (
    DEFAULT_QUERY_PROMPT,
    LLAMA_CPP_BACKEND_NAME,
    EmbedBackendNotRunnable,
    LlamaCppEmbedBackend,
    RealBackendRequiredError,
    _llama_pooling_type,
    _sanitise_for_embedding,
    assert_real_backends_if_required,
    load_embed_backend,
    load_query_embed_backend,
)

NATIVE = 8
DIMS = 4


class FakeLlama:
    """One token per BYTE -- the simplest tokeniser whose truncation point
    is checkable by eye -- and a deterministic unnormalised embedding.

    Deliberately asserts the flags the recipe requires rather than
    recording them for the test to check later: a call with ``add_bos=True``
    is a recipe violation wherever it happens, including in some future
    refactor that never looks at this file."""

    def __init__(self, *, native_dims: int = NATIVE, scale: float = 7.5):
        self.native_dims = native_dims
        self.scale = scale
        self.embedded: list[str] = []
        self.tokenized: list[bytes] = []

    def tokenize(self, text: bytes, add_bos: bool = True, special: bool = False):
        assert add_bos is False, "the recipe tokenises with add_bos=False"
        assert special is False, "the recipe tokenises with special=False"
        self.tokenized.append(text)
        return list(text)

    def detokenize(self, ids) -> bytes:
        return bytes(ids)

    def embed(self, text: str, normalize: bool = True):
        assert normalize is False, "the recipe embeds with normalize=False and normalises itself"
        self.embedded.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        while len(digest) < self.native_dims * 4:
            digest += hashlib.sha256(digest).digest()
        raw = struct.unpack(f"<{self.native_dims}I", digest[: self.native_dims * 4])
        return [((v / 0xFFFFFFFF) * 2.0 - 1.0) * self.scale for v in raw]


def _backend(**kwargs) -> tuple[LlamaCppEmbedBackend, FakeLlama]:
    llama = FakeLlama()
    params = {
        "model_path": "/nonexistent/model.gguf",
        "model_key": "real-key",
        "dims": DIMS,
        "native_dims": NATIVE,
        "n_ctx": 8,
        "_llama": llama,
    }
    params.update(kwargs)
    return LlamaCppEmbedBackend(**params), llama


@pytest.fixture(autouse=True)
def _clear_resident_cache():
    backends._LLAMA_INSTANCES.clear()
    yield
    backends._LLAMA_INSTANCES.clear()


# ---------------------------------------------------------------------------
# the recipe, step by step
# ---------------------------------------------------------------------------


def test_the_query_prompt_is_prepended_by_plain_concatenation():
    backend, _llama = _backend(n_ctx=4096)
    assert backend.prepared_text("what is a quorum", kind="query") == (
        DEFAULT_QUERY_PROMPT + "what is a quorum"
    )
    # no separator of any kind was introduced between the two
    assert backend.prepared_text("x", kind="query").endswith("Query:x")


def test_a_document_never_gets_the_prompt():
    backend, _llama = _backend(n_ctx=4096)
    assert backend.prepared_text("a passage", kind="document") == "a passage"


def test_control_characters_are_removed_and_whitespace_is_not_collapsed():
    """Lane F-1b item 3: this is now the ONE sanitiser of record (the worker's
    semantics), so ``\\r`` goes with the other control characters and
    whitespace-only text becomes the empty string. Tab and newline stay, and
    nothing is stripped or collapsed. ``tests/test_ingest_embeddable_text.py``
    is where that function is tested properly."""
    assert _sanitise_for_embedding("a\x00b\x1fc") == "abc"
    assert _sanitise_for_embedding(" a\tb\nc\r ") == " a\tb\nc "
    assert _sanitise_for_embedding("  \r\n ") == ""


def test_content_is_truncated_to_n_ctx_minus_one_ids_and_detokenised():
    """EOS room: an ``n_ctx`` of 8 leaves 7 ids for content, never 8."""
    backend, llama = _backend(n_ctx=8)
    prepared = backend.prepared_text("abcdefghijkl", kind="document")
    assert prepared == "abcdefg"
    assert len(llama.tokenize(prepared.encode("utf-8"), add_bos=False, special=False)) == 7


def test_a_query_is_truncated_with_its_prompt_inside_the_same_budget():
    """The prompt is part of the content the budget governs -- a program
    that truncated the query first and then prepended a 100-token
    instruction would hand the encoder more tokens than its context."""
    backend, _llama = _backend(n_ctx=len(DEFAULT_QUERY_PROMPT) + 4)
    prepared = backend.prepared_text("abcdefghij", kind="query")
    assert len(prepared.encode("utf-8")) == len(DEFAULT_QUERY_PROMPT) + 3
    assert prepared.startswith(DEFAULT_QUERY_PROMPT)


def test_a_prompt_that_fills_the_budget_is_refused_instead_of_dropping_the_query():
    """V-3: the prompt and the content share one budget, so a prompt longer
    than it truncated ITSELF -- ``prepared_text("cats", kind="query")`` came
    back as ``"Instruct:"`` and the corpus was ranked against an instruction
    with the query silently gone. Reachable through config alone (a small
    ``n_ctx``, or a long custom ``query_prompt``)."""
    backend, _llama = _backend(n_ctx=10)
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.prepared_text("cats", kind="query")
    message = str(exc.value)
    assert "n_ctx = 10" in message
    assert str(len(DEFAULT_QUERY_PROMPT)) in message, "both numbers have to be named"
    assert "query_prompt" in message

    with pytest.raises(EmbedBackendNotRunnable):
        backend.embed_batch(["cats"], kind="query")
    # a DOCUMENT on the same backend is untouched: it never carries the prompt
    assert backend.prepared_text("abcdefghijkl", kind="document") == "abcdefghi"


def test_a_query_whose_prompt_just_fits_is_still_embedded():
    """The boundary, from the other side: one id of room for content is
    enough, and nothing is refused for being tight."""
    backend, _llama = _backend(n_ctx=len(DEFAULT_QUERY_PROMPT) + 2)
    prepared = backend.prepared_text("cats", kind="query")
    assert prepared.startswith(DEFAULT_QUERY_PROMPT)
    assert len(prepared.encode("utf-8")) == len(DEFAULT_QUERY_PROMPT) + 1


def test_a_prompt_overrun_reaches_a_query_as_a_reason_not_a_traceback(monkeypatch):
    """It is not in ``runnable()`` on purpose (this backend is selectable on
    the document side, where the prompt is never used, and ``embed_batch``
    asks ``runnable()`` on both sides) -- so the path that has to stay clean
    is the engine's, where every query-side failure becomes prose."""
    backend, _llama = _backend(n_ctx=10)
    assert backend.runnable() == (True, ""), "a document embed on this backend is not refused"

    from trialerror.retrieve import engine

    monkeypatch.setattr(engine, "_resolve_embed_backend", lambda _store, side="document": ("real-key", backend))
    vector, reason = engine.query_vector_or_reason(None, "cats")
    assert vector is None
    assert "query_prompt" in reason and "n_ctx = 10" in reason


def test_an_input_shorter_than_the_budget_is_passed_through_untouched():
    backend, llama = _backend(n_ctx=4096)
    backend.embed_batch(["a short passage"], kind="document")
    assert llama.embedded == ["a short passage"]


def test_the_vector_is_normalised_at_native_dims_then_sliced_then_normalised():
    backend, llama = _backend(n_ctx=4096)
    vector = backend.embed_batch(["a passage"], kind="document")[0]

    raw = FakeLlama().embed("a passage", normalize=False)
    norm = math.sqrt(sum(v * v for v in raw))
    once = [v / norm for v in raw][:DIMS]
    norm2 = math.sqrt(sum(v * v for v in once))
    expected = [v / norm2 for v in once]

    assert vector == expected, "the recipe's normalise/slice/normalise order is not reproduced"
    assert len(vector) == DIMS
    assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) < 1e-12


def test_the_empty_string_is_embedded_and_not_skipped():
    backend, llama = _backend(n_ctx=4096)
    vectors = backend.embed_batch(["", "x"], kind="document")
    assert len(vectors) == 2
    assert len(vectors[0]) == DIMS
    assert llama.embedded == ["", "x"]


def test_an_empty_query_still_carries_the_prompt():
    backend, _llama = _backend(n_ctx=4096)
    assert backend.prepared_text("", kind="query") == DEFAULT_QUERY_PROMPT


def test_a_vector_of_the_wrong_native_width_is_refused_naming_both_numbers():
    backend, _llama = _backend(n_ctx=4096, native_dims=NATIVE + 1)
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert str(NATIVE) in str(exc.value) and str(NATIVE + 1) in str(exc.value)


def test_an_all_zero_vector_is_refused_rather_than_exempted(monkeypatch):
    """V-4: the assertion used to read ``and norm != 0.0``, exempting the one
    value that falsifies it. A zero vector scores 0.0 against every row and
    ranks the corpus by the id tie-break alone."""

    class ZeroLlama(FakeLlama):
        def embed(self, text: str, normalize: bool = True):
            return [0.0] * self.native_dims

    backend = LlamaCppEmbedBackend(
        model_path="m", model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=4096, _llama=ZeroLlama()
    )
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert "norm" in str(exc.value)


def test_dims_wider_than_native_dims_is_refused_at_construction():
    with pytest.raises(ValueError):
        LlamaCppEmbedBackend(model_path="m", model_key="k", dims=4096, native_dims=2560)


def test_an_n_ctx_with_no_room_for_content_plus_eos_is_refused():
    with pytest.raises(ValueError):
        LlamaCppEmbedBackend(model_path="m", model_key="k", dims=4, native_dims=8, n_ctx=1)


def test_token_level_output_is_pooled_by_taking_the_last_vector():
    class TokenLevelLlama(FakeLlama):
        def embed(self, text: str, normalize: bool = True):
            flat = super().embed(text, normalize=normalize)
            return [[0.0] * self.native_dims, flat]

    llama = TokenLevelLlama()
    backend = LlamaCppEmbedBackend(
        model_path="m", model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=4096, _llama=llama
    )
    assert backend.embed_batch(["a passage"])[0] == _backend(n_ctx=4096)[0].embed_batch(["a passage"])[0]


# ---------------------------------------------------------------------------
# runnable(): every failure as text
# ---------------------------------------------------------------------------


def test_an_injected_llama_is_runnable():
    backend, _llama = _backend()
    assert backend.runnable() == (True, "")


@pytest.mark.skipif(
    importlib.util.find_spec("llama_cpp") is not None,
    # Lane F-1b: this guard used to read ``"llama_cpp" in sys.modules``, which
    # is not the same question -- on a machine where the wheel IS installed but
    # nothing has imported it yet, the guard passes, the import inside
    # ``runnable()`` succeeds, and the test fails on the next refusal down
    # (the missing model file) for a reason that has nothing to do with it.
    # That was failing at this lane's base commit; the assertion is unchanged.
    reason="llama_cpp really is installed here",
)
def test_a_missing_library_is_a_reason_not_an_import_error():
    backend = LlamaCppEmbedBackend(model_path="/nonexistent/model.gguf", model_key="k", dims=4, native_dims=8)
    ok, reason = backend.runnable()
    assert ok is False
    assert "llama_cpp" in reason and "not importable" in reason


def test_a_missing_model_file_is_a_reason_naming_the_path(monkeypatch):
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    backend = LlamaCppEmbedBackend(model_path="/nonexistent/model.gguf", model_key="k", dims=4, native_dims=8)
    ok, reason = backend.runnable()
    assert ok is False
    assert "/nonexistent/model.gguf" in reason


def test_a_loader_failure_surfaces_as_text_not_a_traceback(monkeypatch, tmp_path):
    """The case this method was written for: a wheel built against one C
    library, installed under another. ``ctypes`` raises on first
    construction, deep inside the package."""
    module = types.ModuleType("llama_cpp")

    def _explode(**_kwargs):
        raise OSError("Error loading shared library libllama.so: wrong ELF class")

    module.Llama = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)

    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    backend = LlamaCppEmbedBackend(model_path=str(model), model_key="k", dims=4, native_dims=8)
    ok, reason = backend.runnable()
    assert ok is False
    assert "failed to load" in reason and "wrong ELF class" in reason


def test_embed_batch_on_an_unrunnable_backend_raises_the_reason(monkeypatch):
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    backend = LlamaCppEmbedBackend(model_path="/nonexistent/model.gguf", model_key="k", dims=4, native_dims=8)
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["x"])
    assert "/nonexistent/model.gguf" in str(exc.value)


def test_one_resident_model_per_path_per_process(monkeypatch, tmp_path):
    constructed: list[dict] = []

    class RecordingLlama(FakeLlama):
        def __init__(self, **kwargs):
            super().__init__()
            constructed.append(kwargs)

    module = types.ModuleType("llama_cpp")
    module.Llama = RecordingLlama  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)

    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    first = LlamaCppEmbedBackend(model_path=str(model), model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=64)
    second = LlamaCppEmbedBackend(model_path=str(model), model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=64)
    first.embed_batch(["a"])
    second.embed_batch(["b"])
    assert len(constructed) == 1, "the model was loaded twice in one process"
    assert constructed[0]["n_batch"] == constructed[0]["n_ubatch"] == 64
    assert constructed[0]["embedding"] is True
    assert constructed[0]["n_ctx"] == 64


def test_pooling_names_resolve_to_llama_cpps_own_constants_when_it_exports_them(monkeypatch):
    module = types.ModuleType("llama_cpp")
    module.LLAMA_POOLING_TYPE_LAST = 99  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    assert _llama_pooling_type("last") == 99


def test_pooling_names_fall_back_to_the_literal_table(monkeypatch):
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    assert _llama_pooling_type("last") == 3
    assert _llama_pooling_type("mean") == 1
    with pytest.raises(ValueError):
        _llama_pooling_type("median")


# ---------------------------------------------------------------------------
# config wiring
# ---------------------------------------------------------------------------


def test_the_loader_builds_it_from_a_query_table_and_inherits_the_key():
    backend = load_query_embed_backend(
        {
            "backend": "offload",
            "model_key": "real-key",
            "dims": 2048,
            "query": {"backend": LLAMA_CPP_BACKEND_NAME, "model_path": "/nonexistent/model.gguf", "n_ctx": 1024},
        }
    )
    assert isinstance(backend, LlamaCppEmbedBackend)
    assert backend.model_key == "real-key" and backend.dims == 2048
    assert backend.n_ctx == 1024 and backend.native_dims == 2560
    assert backend.query_prompt == DEFAULT_QUERY_PROMPT


def test_the_loader_refuses_a_llama_cpp_table_with_no_model_path():
    with pytest.raises(ValueError) as exc:
        load_embed_backend({"backend": LLAMA_CPP_BACKEND_NAME, "model_key": "k"})
    assert "model_path" in str(exc.value)


def test_the_loader_refuses_a_llama_cpp_table_with_no_model_key():
    with pytest.raises(ValueError) as exc:
        load_embed_backend({"backend": LLAMA_CPP_BACKEND_NAME, "model_path": "/m.gguf"})
    assert "model_key" in str(exc.value)


def test_require_real_backends_refuses_a_fake_query_side():
    with pytest.raises(RealBackendRequiredError) as exc:
        assert_real_backends_if_required(
            {
                "ingest": {
                    "require_real_backends": True,
                    "ocr": {"backend": "offload"},
                    "embed": {"backend": "offload", "model_key": "k", "query": {"backend": "fake"}},
                }
            }
        )
    assert "ingest.embed.query" in str(exc.value)


def test_require_real_backends_accepts_a_llama_cpp_query_side():
    assert_real_backends_if_required(
        {
            "ingest": {
                "require_real_backends": True,
                "ocr": {"backend": "offload"},
                "embed": {
                    "backend": "offload",
                    "model_key": "k",
                    "query": {"backend": LLAMA_CPP_BACKEND_NAME, "model_path": "/m.gguf"},
                },
            }
        }
    )


def test_the_real_library_agrees_with_the_pooling_table():
    """The one test that needs the package itself. Skipped -- not failed --
    where it is absent, which is every machine that has not been given the
    runtime yet."""
    llama_cpp = pytest.importorskip("llama_cpp", reason="llama-cpp-python is not installed here")
    assert hasattr(llama_cpp, "Llama")
    # the pooling constant the recipe pins, read off the real package
    assert _llama_pooling_type("last") == int(getattr(llama_cpp, "LLAMA_POOLING_TYPE_LAST", 3))
