"""Lane F-1b item 2: the ``llama_server`` query-side embed backend.

Two things are tested here, and the second is the important one.

1. The client's own behaviour: the endpoint it POSTs to, the response shapes
   it accepts, the normalisation it does itself, and every way ``runnable()``
   can say no (sidecar down, still loading, HTTP error, wrong model, no
   tokenizer, no wheel).
2. **Recipe fidelity against the in-process backend.** The two clients embed
   into the same vector space and are ranked against the same stored rows, so
   a prepared string that differs by one token, one space or one prompt is a
   silent retrieval regression. The fidelity tests run the SAME fixture
   tokenizer through both classes and compare byte for byte.

No model file, no wheel, no live server: the HTTP client and the tokenizer are
both injected fakes. Nothing in this module opens a socket.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
import types
import warnings

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import (
    DEFAULT_LLAMA_SERVER_N_CTX,
    DEFAULT_LLAMA_SERVER_TIMEOUT_S,
    DEFAULT_LLAMA_SERVER_URL,
    DEFAULT_QUERY_PROMPT,
    LLAMA_SERVER_BACKEND_NAME,
    LLAMA_SERVER_EMBED_PATH,
    LLAMA_SERVER_HEALTH_PATH,
    LLAMA_SERVER_PROPS_PATH,
    EmbedBackendNotRunnable,
    LlamaCppEmbedBackend,
    LlamaServerEmbedBackend,
    embed_backend_runtime_details,
    load_embed_backend,
    load_query_embed_backend,
)

#: Captured before any test's autouse fixture monkeypatches
#: ``backends._slot_erase_lock_path`` -- the one test of that function's own
#: URL-normalisation logic needs the REAL implementation, not the tmp_path
#: stand-in every other test gets.
_REAL_SLOT_ERASE_LOCK_PATH = backends._slot_erase_lock_path

NATIVE = 8
DIMS = 4


class FakeVocab:
    """One token per BYTE -- the same fixture tokenizer the in-process
    backend's tests use, so a prepared string is checkable by eye and the two
    clients can be compared on identical ids.

    Asserts the recipe's flags rather than recording them: a call with
    ``add_bos=True`` is a violation wherever it happens."""

    def __init__(self):
        self.tokenized: list[bytes] = []

    def tokenize(self, text: bytes, add_bos: bool = True, special: bool = False):
        assert add_bos is False, "the recipe tokenises with add_bos=False"
        assert special is False, "the recipe tokenises with special=False"
        self.tokenized.append(text)
        return list(text)

    def detokenize(self, ids) -> bytes:
        return bytes(ids)


def _raw_vector(text: str, *, native_dims: int = NATIVE, scale: float = 7.5) -> list[float]:
    """The same deterministic unnormalised vector the in-process fake
    produces, so a fidelity test can compare the two clients' OUTPUT as well
    as their input."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    while len(digest) < native_dims * 4:
        digest += hashlib.sha256(digest).digest()
        continue
    raw = struct.unpack(f"<{native_dims}I", digest[: native_dims * 4])
    return [((v / 0xFFFFFFFF) * 2.0 - 1.0) * scale for v in raw]


class FakeHttp:
    """A ``llama-server`` stand-in: records every request, answers ``/health``
    and ``/props`` from attributes a test can set, and ``/embedding`` with the
    deterministic raw vector above.

    ``shape`` covers the response shapes the endpoint has actually returned
    across builds; ``fail_with`` makes the transport raise, which is what a
    dead sidecar looks like from here."""

    def __init__(
        self,
        *,
        health_status: int = 200,
        health_body: object | None = None,
        props_status: int = 200,
        props_body: object | None = None,
        embed_status: int = 200,
        shape: str = "flat",
        fail_with: Exception | None = None,
        native_dims: int = NATIVE,
        slot_erase_status: int = 200,
        slot_erase_body: object | None = None,
    ):
        self.health_status = health_status
        self.health_body = {"status": "ok"} if health_body is None else health_body
        self.props_status = props_status
        self.props_body = {"model_path": "/somewhere/model.gguf"} if props_body is None else props_body
        self.embed_status = embed_status
        self.shape = shape
        self.fail_with = fail_with
        self.native_dims = native_dims
        self.slot_erase_status = slot_erase_status
        self.slot_erase_body = {"id_slot": 0, "n_erased": 1} if slot_erase_body is None else slot_erase_body
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get_json(self, path: str):
        if self.fail_with is not None:
            raise self.fail_with
        self.gets.append(path)
        if path == LLAMA_SERVER_HEALTH_PATH:
            return self.health_status, self.health_body
        if path == LLAMA_SERVER_PROPS_PATH:
            return self.props_status, self.props_body
        return 404, "not found"

    def post_json(self, path: str, payload: dict):
        if self.fail_with is not None:
            raise self.fail_with
        self.posts.append((path, payload))
        if path.startswith("/slots/"):
            return self.slot_erase_status, self.slot_erase_body
        if self.embed_status != 200:
            return self.embed_status, {"error": {"message": "context overflow"}}
        vector = _raw_vector(str(payload.get("content", "")), native_dims=self.native_dims)
        if self.shape == "flat":
            return 200, {"embedding": vector}
        if self.shape == "matrix":
            return 200, [{"index": 0, "embedding": [vector]}]
        if self.shape == "oai":
            return 200, {"data": [{"index": 0, "embedding": vector}]}
        if self.shape == "token_level":
            return 200, {"embedding": [[0.0] * self.native_dims, vector]}
        if self.shape == "narrow":
            return 200, {"embedding": vector[:-1]}
        if self.shape == "zeros":
            return 200, {"embedding": [0.0] * self.native_dims}
        if self.shape == "garbage":
            return 200, {"nothing": "here"}
        raise AssertionError(f"unknown fake shape {self.shape!r}")


def _server(**kwargs) -> tuple[LlamaServerEmbedBackend, FakeHttp, FakeVocab]:
    http = kwargs.pop("http", None) or FakeHttp()
    vocab = kwargs.pop("vocab", None) or FakeVocab()
    params = {
        "model_key": "real-key",
        "dims": DIMS,
        "native_dims": NATIVE,
        "n_ctx": 9,
        "tokenizer_model_path": "/somewhere/model.gguf",
        "_http": http,
        "_tokenizer": vocab,
    }
    params.update(kwargs)
    return LlamaServerEmbedBackend(**params), http, vocab


@pytest.fixture(autouse=True)
def _clear_caches():
    backends._LLAMA_INSTANCES.clear()
    backends._VOCAB_ONLY_INSTANCES.clear()
    backends._SLOT_ERASE_UNAVAILABLE_WARNED.clear()
    yield
    backends._LLAMA_INSTANCES.clear()
    backends._VOCAB_ONLY_INSTANCES.clear()
    backends._SLOT_ERASE_UNAVAILABLE_WARNED.clear()


@pytest.fixture(autouse=True)
def _slot_erase_lock_in_tmp_path(monkeypatch, tmp_path):
    """Every ``LlamaServerEmbedBackend`` built in this module,
    even one constructed with no explicit ``_lock_path``, must not touch
    the harness's real DEV-local scratch root. A test of the lock's own
    behaviour still passes its own ``_lock_path`` explicitly (that override
    wins), so this only changes what an OMITTED one resolves to."""
    monkeypatch.setattr(backends, "_slot_erase_lock_path", lambda url: tmp_path / "slot_erase.lock")


# ---------------------------------------------------------------------------
# the request: endpoint, payload, geometry
# ---------------------------------------------------------------------------


def test_the_client_posts_the_prepared_text_to_the_native_embedding_endpoint():
    backend, http, _vocab = _server(n_ctx=4096)
    backend.embed_batch(["what is a quorum"], kind="query")
    embed_path, payload = http.posts[-1]
    assert embed_path == LLAMA_SERVER_EMBED_PATH
    assert payload["content"] == DEFAULT_QUERY_PROMPT + "what is a quorum"
    assert payload["embd_normalize"] == -1, "the client normalises; the server must not"


def test_health_is_probed_before_anything_is_embedded():
    backend, http, _vocab = _server(n_ctx=4096)
    backend.embed_batch(["x"])
    assert http.gets[0] == LLAMA_SERVER_HEALTH_PATH


def test_the_content_budget_is_n_ctx_minus_two():
    """One id for the EOS the server appends, one so the request is never
    EXACTLY the context length the server refuses."""
    backend, _http, _vocab = _server(n_ctx=9)
    assert backend.content_budget == 7
    assert backend.prepared_text("abcdefghijkl", kind="document") == "abcdefg"


def test_the_default_geometry_is_max_seq_plus_one_and_budgets_to_max_seq_minus_one():
    """The measured refusal: a request of exactly 2048 ids against a 2048
    context. The server runs at 2049, the client truncates content to 2047,
    the server's EOS makes 2048 -- one below the context, by construction."""
    backend, _http, _vocab = _server(n_ctx=DEFAULT_LLAMA_SERVER_N_CTX)
    assert DEFAULT_LLAMA_SERVER_N_CTX == 2049
    assert backend.content_budget == 2047
    assert backend.content_budget + 1 < DEFAULT_LLAMA_SERVER_N_CTX


def test_a_request_of_exactly_the_context_length_is_unreachable_by_construction():
    """Property, not example: for every geometry the class accepts, content
    plus EOS is strictly below n_ctx."""
    for n_ctx in range(3, 40):
        backend, _http, _vocab = _server(n_ctx=n_ctx)
        longest = backend.prepared_text("x" * (n_ctx * 3), kind="document")
        ids_sent = len(longest.encode("utf-8"))
        assert ids_sent == n_ctx - 2
        assert ids_sent + 1 < n_ctx


def test_an_n_ctx_with_no_room_is_refused_at_construction():
    with pytest.raises(ValueError) as exc:
        LlamaServerEmbedBackend(model_key="k", dims=4, native_dims=8, n_ctx=2)
    assert "n_ctx" in str(exc.value)


def test_dims_wider_than_native_dims_is_refused_at_construction():
    with pytest.raises(ValueError):
        LlamaServerEmbedBackend(model_key="k", dims=4096, native_dims=2560)


def test_a_prompt_that_fills_the_budget_is_refused_naming_both_numbers():
    backend, _http, _vocab = _server(n_ctx=10)
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.prepared_text("cats", kind="query")
    message = str(exc.value)
    assert "n_ctx = 10" in message and "query_prompt" in message
    assert str(len(DEFAULT_QUERY_PROMPT)) in message
    # a document on the same backend never carries the prompt and is fine
    assert backend.prepared_text("abcdefghijkl", kind="document") == "abcdefgh"


def test_a_document_never_gets_the_prompt():
    backend, _http, _vocab = _server(n_ctx=4096)
    assert backend.prepared_text("a passage", kind="document") == "a passage"


def test_the_empty_string_is_embedded_and_not_skipped():
    backend, http, _vocab = _server(n_ctx=4096)
    vectors = backend.embed_batch(["", "x"], kind="document")
    assert len(vectors) == 2 and len(vectors[0]) == DIMS
    embed_posts = [payload for path, payload in http.posts if path == LLAMA_SERVER_EMBED_PATH]
    assert [payload["content"] for payload in embed_posts] == ["", "x"]


def test_an_empty_query_still_carries_the_prompt():
    backend, _http, _vocab = _server(n_ctx=4096)
    assert backend.prepared_text("", kind="query") == DEFAULT_QUERY_PROMPT


# ---------------------------------------------------------------------------
# the response: shapes, normalisation, refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["flat", "matrix", "oai"])
def test_every_response_shape_yields_the_same_vector(shape):
    expected = None
    backend, _http, _vocab = _server(n_ctx=4096, http=FakeHttp(shape="flat"))
    expected = backend.embed_batch(["a passage"])[0]
    backend, _http, _vocab = _server(n_ctx=4096, http=FakeHttp(shape=shape))
    assert backend.embed_batch(["a passage"])[0] == expected


def test_a_token_level_response_is_pooled_by_taking_the_last_vector():
    backend, _http, _vocab = _server(n_ctx=4096, http=FakeHttp(shape="token_level"))
    pooled, _http2, _vocab2 = _server(n_ctx=4096, http=FakeHttp(shape="flat"))
    assert backend.embed_batch(["a passage"])[0] == pooled.embed_batch(["a passage"])[0]


def test_the_vector_is_normalised_at_native_dims_then_sliced_then_normalised():
    backend, _http, _vocab = _server(n_ctx=4096)
    vector = backend.embed_batch(["a passage"])[0]

    raw = _raw_vector("a passage")
    norm = math.sqrt(sum(v * v for v in raw))
    once = [v / norm for v in raw][:DIMS]
    norm2 = math.sqrt(sum(v * v for v in once))
    expected = [v / norm2 for v in once]

    assert vector == expected, "the recipe's normalise/slice/normalise order is not reproduced"
    assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) < 1e-12


def test_an_already_normalised_server_answer_produces_the_same_unit_vector():
    """The sidecar runs with ``--embd-normalize -1``, but every value that
    flag accepts is a positive SCALING of the pooled vector -- so a build that
    ignored the request cannot move the client's answer."""
    plain, _http, _vocab = _server(n_ctx=4096)
    expected = plain.embed_batch(["a passage"])[0]

    class ScalingHttp(FakeHttp):
        def post_json(self, path: str, payload: dict):
            status, body = super().post_json(path, payload)
            if path != LLAMA_SERVER_EMBED_PATH:
                return status, body
            vector = body["embedding"]
            norm = math.sqrt(sum(v * v for v in vector))
            return status, {"embedding": [v / norm for v in vector]}

    scaled, _http2, _vocab2 = _server(n_ctx=4096, http=ScalingHttp())
    got = scaled.embed_batch(["a passage"])[0]
    assert all(abs(a - b) < 1e-12 for a, b in zip(got, expected))


def test_a_vector_of_the_wrong_width_is_refused_naming_both_numbers():
    backend, _http, _vocab = _server(n_ctx=4096, http=FakeHttp(shape="narrow"))
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert str(NATIVE) in str(exc.value) and str(NATIVE - 1) in str(exc.value)


def test_an_all_zero_answer_is_refused_rather_than_exempted():
    backend, _http, _vocab = _server(n_ctx=4096, http=FakeHttp(shape="zeros"))
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert "norm" in str(exc.value)


def test_a_body_with_no_embedding_field_is_refused():
    backend, _http, _vocab = _server(n_ctx=4096, http=FakeHttp(shape="garbage"))
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert "embedding" in str(exc.value)


def test_an_http_error_on_the_embed_call_carries_the_status_and_the_body():
    backend, _http, _vocab = _server(n_ctx=4096, http=FakeHttp(embed_status=500))
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert "500" in str(exc.value) and "context overflow" in str(exc.value)


def test_a_transport_failure_mid_batch_is_a_reason_not_a_traceback():
    http = FakeHttp()
    backend, _http, _vocab = _server(n_ctx=4096, http=http)
    backend.embed_batch(["warm"])  # health probed, all well
    http.fail_with = OSError("connection reset by peer")
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["now what"])
    assert "connection reset" in str(exc.value)


# ---------------------------------------------------------------------------
# runnable(): health, model identity, tokenizer
# ---------------------------------------------------------------------------


def test_a_healthy_sidecar_with_the_expected_model_is_runnable():
    backend, _http, _vocab = _server()
    assert backend.runnable() == (True, "")
    assert backend.runtime_details()["model_check"] == backends.MODEL_CHECK_VERIFIED


def test_a_sidecar_that_does_not_answer_is_a_reason_naming_the_start_verb():
    backend, _http, _vocab = _server(http=FakeHttp(fail_with=OSError("Connection refused")))
    ok, reason = backend.runnable()
    assert ok is False
    assert "Connection refused" in reason
    assert "sidecar start" in reason and DEFAULT_LLAMA_SERVER_URL.split("//")[-1] in reason


def test_a_sidecar_still_loading_its_model_says_so():
    backend, _http, _vocab = _server(
        http=FakeHttp(health_status=503, health_body={"status": "loading model"})
    )
    ok, reason = backend.runnable()
    assert ok is False
    assert "503" in reason and "still loading" in reason


def test_a_health_endpoint_answering_an_error_is_a_reason():
    backend, _http, _vocab = _server(http=FakeHttp(health_status=500, health_body="internal error"))
    ok, reason = backend.runnable()
    assert ok is False and "500" in reason and "internal error" in reason


def test_a_sidecar_serving_a_different_model_is_refused():
    backend, _http, _vocab = _server(
        http=FakeHttp(props_body={"model_path": "/elsewhere/some-other-model.gguf"})
    )
    ok, reason = backend.runnable()
    assert ok is False
    assert "some-other-model.gguf" in reason and "model.gguf" in reason
    assert backend.runtime_details()["model_check"] == backends.MODEL_CHECK_MISMATCH


def test_a_build_that_does_not_report_its_model_warns_rather_than_refusing():
    backend, _http, _vocab = _server(http=FakeHttp(props_status=404, props_body="not found"))
    assert backend.runnable() == (True, "")
    details = backend.runtime_details()
    assert details["model_check"] == backends.MODEL_CHECK_UNVERIFIED
    assert "unverified" in details["model_check_note"]


def test_the_model_name_is_also_read_out_of_a_health_body_that_carries_it():
    backend, _http, _vocab = _server(
        http=FakeHttp(
            props_status=404,
            props_body="not found",
            health_body={"status": "ok", "model": "/wherever/model.gguf"},
        )
    )
    assert backend.runnable() == (True, "")
    assert backend.runtime_details()["model_check"] == backends.MODEL_CHECK_VERIFIED


def test_a_nested_default_generation_settings_model_is_read_too():
    backend, _http, _vocab = _server(
        http=FakeHttp(props_body={"default_generation_settings": {"model": "/x/model.gguf"}})
    )
    assert backend.runnable() == (True, "")


def test_a_props_call_that_raises_does_not_defeat_a_healthy_sidecar():
    class PropsExplodes(FakeHttp):
        def get_json(self, path: str):
            if path == LLAMA_SERVER_PROPS_PATH:
                raise OSError("no such route")
            return super().get_json(path)

    backend, _http, _vocab = _server(http=PropsExplodes())
    assert backend.runnable() == (True, "")
    assert backend.runtime_details()["model_check"] == backends.MODEL_CHECK_UNVERIFIED


def test_a_missing_tokenizer_model_path_is_a_reason_naming_the_key():
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=64, _http=FakeHttp()
    )
    ok, reason = backend.runnable()
    assert ok is False
    assert "tokenizer_model_path" in reason and "no model weights are loaded" in reason


def test_an_absent_wheel_is_a_reason_naming_the_key_and_the_vocab_only_load(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "llama_cpp", None)  # import llama_cpp -> ImportError
    backend = LlamaServerEmbedBackend(
        model_key="k",
        dims=DIMS,
        native_dims=NATIVE,
        n_ctx=64,
        tokenizer_model_path="/nonexistent/model.gguf",
        _http=FakeHttp(),
    )
    ok, reason = backend.runnable()
    assert ok is False
    assert "llama_cpp" in reason and "vocab_only=True" in reason
    assert "tokenizer_model_path" in reason


def test_a_missing_tokenizer_file_is_a_reason_naming_the_path(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    backend = LlamaServerEmbedBackend(
        model_key="k",
        dims=DIMS,
        native_dims=NATIVE,
        n_ctx=64,
        tokenizer_model_path="/nonexistent/model.gguf",
        _http=FakeHttp(),
    )
    ok, reason = backend.runnable()
    assert ok is False and "/nonexistent/model.gguf" in reason


def test_the_tokenizer_is_opened_vocab_only_and_cached_per_path(monkeypatch, tmp_path):
    import sys

    constructed: list[dict] = []

    class RecordingLlama(FakeVocab):
        def __init__(self, **kwargs):
            super().__init__()
            constructed.append(kwargs)

    module = types.ModuleType("llama_cpp")
    module.Llama = RecordingLlama  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)

    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    first = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=64,
        tokenizer_model_path=str(gguf), _http=FakeHttp(props_body={"model": str(gguf)}),
    )
    second = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=64,
        tokenizer_model_path=str(gguf), _http=FakeHttp(props_body={"model": str(gguf)}),
    )
    first.embed_batch(["a"])
    second.embed_batch(["b"])
    assert len(constructed) == 1, "the vocabulary was opened twice in one process"
    assert constructed[0]["vocab_only"] is True, "no model weights may be loaded for a tokenizer"
    assert constructed[0]["model_path"] == str(gguf)


def test_embed_batch_on_an_unrunnable_backend_raises_the_reason():
    backend, _http, _vocab = _server(http=FakeHttp(fail_with=OSError("Connection refused")))
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["x"])
    assert "Connection refused" in str(exc.value)


def test_runtime_details_carry_the_url_and_the_last_health_reading():
    backend, _http, _vocab = _server()
    backend.runnable()
    details = embed_backend_runtime_details(backend)
    assert details["backend"] == LLAMA_SERVER_BACKEND_NAME
    assert details["url"] == DEFAULT_LLAMA_SERVER_URL
    assert details["embed_path"] == LLAMA_SERVER_EMBED_PATH
    assert details["health"]["ok"] is True and details["health"]["status"] == 200
    assert details["content_budget"] == backend.content_budget
    # a detail field has to survive json.dumps -- a doctor envelope is JSON
    json.dumps(details)


# ---------------------------------------------------------------------------
# recipe fidelity against the in-process client
# ---------------------------------------------------------------------------


class _WheelWithSameVocab:
    """The in-process backend driven by the SAME fixture tokenizer, so the two
    clients' prepared strings are comparable id for id."""

    def __init__(self, vocab: FakeVocab, *, n_ctx: int):
        self.vocab = vocab
        self.backend = LlamaCppEmbedBackend(
            model_path="m", model_key="real-key", dims=DIMS, native_dims=NATIVE,
            n_ctx=n_ctx, _llama=self,
        )

    # the three methods LlamaCppEmbedBackend asks of its `_llama`
    def tokenize(self, text: bytes, add_bos: bool = True, special: bool = False):
        return self.vocab.tokenize(text, add_bos=add_bos, special=special)

    def detokenize(self, ids) -> bytes:
        return self.vocab.detokenize(ids)

    def embed(self, text: str, normalize: bool = True):
        assert normalize is False
        return _raw_vector(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "what is a quorum",
        "a\x00b\x1fc control characters",
        "line one\r\nline two\r\n",
        "   \n  ",
        "curly quotes, em dash, cafe, naive, CJK",
        "x" * 500,
    ],
)
@pytest.mark.parametrize("kind", ["document", "query"])
def test_the_two_clients_prepare_byte_identical_text(text, kind):
    """The geometries differ by exactly one (2048 in process, 2049 through the
    server) precisely so that the CONTENT budget is the same; the prepared
    string must therefore be identical."""
    wheel_ctx = 2048
    vocab = FakeVocab()
    wheel = _WheelWithSameVocab(vocab, n_ctx=wheel_ctx).backend
    server, _http, _vocab = _server(n_ctx=wheel_ctx + 1, vocab=vocab)
    assert server.content_budget == wheel_ctx - 1
    assert server.prepared_text(text, kind=kind) == wheel.prepared_text(text, kind=kind)


@pytest.mark.parametrize("length", [1, 100, 2046, 2047, 2048, 2049, 4000])
def test_the_two_clients_truncate_at_the_same_point(length):
    vocab = FakeVocab()
    wheel = _WheelWithSameVocab(vocab, n_ctx=2048).backend
    server, _http, _vocab = _server(n_ctx=2049, vocab=vocab)
    text = "y" * length
    assert server.prepared_text(text, kind="document") == wheel.prepared_text(text, kind="document")


def test_the_two_clients_produce_the_same_vector_from_the_same_raw_output():
    """Same prepared string, same raw 8-float encoder answer, same recipe tail
    -- so the vectors have to be identical, not merely close."""
    vocab = FakeVocab()
    wheel = _WheelWithSameVocab(vocab, n_ctx=2048).backend
    server, _http, _vocab = _server(n_ctx=2049, vocab=vocab)
    for text in ("what is a quorum", "", "x" * 3000):
        for kind in ("document", "query"):
            assert server.embed_batch([text], kind=kind)[0] == wheel.embed_batch([text], kind=kind)[0]


def test_both_clients_run_the_same_prepare_and_finalise_functions():
    """Fidelity by construction rather than by comparison: if either client
    grows its own copy of a recipe step, this is the test that notices."""
    import inspect

    wheel_src = inspect.getsource(LlamaCppEmbedBackend.prepared_text)
    server_src = inspect.getsource(LlamaServerEmbedBackend.prepared_text)
    for src in (wheel_src, server_src):
        assert "_prepare_for_encoder(" in src
    for src in (
        inspect.getsource(LlamaCppEmbedBackend._embed_one),
        inspect.getsource(LlamaServerEmbedBackend._embed_one),
    ):
        assert "_finalise_recipe_vector(" in src


# ---------------------------------------------------------------------------
# config wiring
# ---------------------------------------------------------------------------


def test_the_loader_builds_it_from_a_query_table_and_inherits_the_key():
    backend = load_query_embed_backend(
        {
            "backend": "offload",
            "model_key": "real-key",
            "dims": 2048,
            "query": {
                "backend": LLAMA_SERVER_BACKEND_NAME,
                "tokenizer_model_path": "/somewhere/model.gguf",
            },
        }
    )
    assert isinstance(backend, LlamaServerEmbedBackend)
    assert backend.model_key == "real-key" and backend.dims == 2048
    assert backend.url == DEFAULT_LLAMA_SERVER_URL
    assert backend.timeout_s == float(DEFAULT_LLAMA_SERVER_TIMEOUT_S)
    assert backend.n_ctx == DEFAULT_LLAMA_SERVER_N_CTX
    assert backend.native_dims == 2560
    assert backend.query_prompt == DEFAULT_QUERY_PROMPT
    assert backend.tokenizer_model_path == "/somewhere/model.gguf"


def test_every_config_key_is_honoured():
    backend = load_query_embed_backend(
        {
            "backend": "offload",
            "model_key": "real-key",
            "dims": 2048,
            "query": {
                "backend": LLAMA_SERVER_BACKEND_NAME,
                "url": "http://127.0.0.1:9999/",
                "timeout_s": 12,
                "native_dims": 2560,
                "n_ctx": 1025,
                "query_prompt": "Instruct: something else\nQuery:",
                "tokenizer_model_path": "/x/model.gguf",
            },
        }
    )
    assert backend.url == "http://127.0.0.1:9999"
    assert backend.timeout_s == 12.0
    assert backend.n_ctx == 1025 and backend.content_budget == 1023
    assert backend.query_prompt.endswith("something else\nQuery:")


def test_the_loader_refuses_a_llama_server_table_with_no_model_key():
    with pytest.raises(ValueError) as exc:
        load_embed_backend({"backend": LLAMA_SERVER_BACKEND_NAME})
    assert "model_key" in str(exc.value)


def test_a_query_side_key_mismatch_is_still_refused():
    from trialerror.ingest.backends import QueryEmbedBackendMismatchError

    with pytest.raises(QueryEmbedBackendMismatchError):
        load_query_embed_backend(
            {
                "backend": "offload",
                "model_key": "real-key",
                "dims": 2048,
                "query": {"backend": LLAMA_SERVER_BACKEND_NAME, "model_key": "some-other-key"},
            }
        )


def test_the_table_resolves_without_the_tokenizer_being_present_yet():
    """A GGUF that is not mounted yet must not turn every CLI command into a
    config error -- it turns into a doctor line naming the key."""
    backend = load_query_embed_backend(
        {
            "backend": "offload",
            "model_key": "real-key",
            "dims": 2048,
            "query": {"backend": LLAMA_SERVER_BACKEND_NAME},
        }
    )
    assert backend.tokenizer_model_path is None
    ok, reason = backend.runnable()
    assert ok is False and "tokenizer_model_path" in reason


# ---------------------------------------------------------------------------
# REQ-2026-09-27-09: erase the slot before every embed
# ---------------------------------------------------------------------------


def test_the_slot_is_erased_before_every_embed():
    backend, http, _vocab = _server(n_ctx=4096)
    backend.embed_batch(["a", "b"], kind="document")
    assert [path for path, _payload in http.posts] == [
        "/slots/0?action=erase",
        LLAMA_SERVER_EMBED_PATH,
        "/slots/0?action=erase",
        LLAMA_SERVER_EMBED_PATH,
    ]
    # an empty body -- the erase carries no content, only the query string
    assert http.posts[0][1] == {}


def test_the_erase_covers_every_slot_the_props_endpoint_reports():
    backend, http, _vocab = _server(
        n_ctx=4096,
        http=FakeHttp(props_body={"model_path": "/somewhere/model.gguf", "total_slots": 2}),
    )
    backend.embed_batch(["a passage"])
    assert [path for path, _payload in http.posts] == [
        "/slots/0?action=erase",
        "/slots/1?action=erase",
        LLAMA_SERVER_EMBED_PATH,
    ]


def test_a_501_from_slot_erase_warns_once_and_still_embeds():
    """The guide's "Determinism needs `--slot-save-path`" paragraph: a build
    started without that flag answers 501 on the erase route. That must not
    fail the embed, must warn exactly once (not once per document), and
    must not keep paying for a round trip that can only ever fail again for
    the rest of this instance's life."""
    http = FakeHttp(slot_erase_status=501, slot_erase_body={"error": "not supported"})
    backend, _http, _vocab = _server(n_ctx=4096, http=http)

    with pytest.warns(RuntimeWarning, match="501"):
        vector = backend.embed_batch(["a passage"])[0]
    assert len(vector) == DIMS
    assert backend.runtime_details()["slot_erase_unsupported"] is True

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a second warning would raise here
        backend.embed_batch(["another passage"])

    posts = [path for path, _payload in http.posts]
    assert posts.count("/slots/0?action=erase") == 1, "no erase attempt after the first 501"
    assert posts.count(LLAMA_SERVER_EMBED_PATH) == 2, "both embeds still went through"


@pytest.mark.parametrize("status", [404, 405])
def test_a_404_or_405_from_slot_erase_warns_once_and_still_embeds(status):
    """A 404 (no /slots route at all -- an older build, or a
    proxy in front of the sidecar that does not forward it) or a 405 (the
    same route, wrong method) means "the erase is unavailable" exactly as
    much as a 501 does. The embed that follows would have worked, so this
    must not fail it either."""
    http = FakeHttp(slot_erase_status=status, slot_erase_body={"error": "not found"})
    backend, _http, _vocab = _server(n_ctx=4096, http=http)
    with pytest.warns(RuntimeWarning, match=str(status)):
        vector = backend.embed_batch(["a passage"])[0]
    assert len(vector) == DIMS
    assert backend.runtime_details()["slot_erase_unsupported"] is True


def test_a_5xx_erase_failure_is_not_silently_absorbed():
    backend, _http, _vocab = _server(
        n_ctx=4096, http=FakeHttp(slot_erase_status=500, slot_erase_body={"error": "boom"})
    )
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert "500" in str(exc.value) and "boom" in str(exc.value)


def test_a_transport_failure_on_the_erase_call_is_a_reason_not_a_traceback():
    class EraseExplodes(FakeHttp):
        def post_json(self, path: str, payload: dict):
            if path.startswith("/slots/"):
                raise OSError("connection reset by peer")
            return super().post_json(path, payload)

    backend, _http, _vocab = _server(n_ctx=4096, http=EraseExplodes())
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert "connection reset" in str(exc.value) and "/slots/0" in str(exc.value)


def test_the_switch_turns_off_the_erase_and_the_lock():
    http = FakeHttp()
    backend, _http, _vocab = _server(n_ctx=4096, http=http, slot_erase=False)
    vector = backend.embed_batch(["a passage"])[0]
    assert [path for path, _payload in http.posts] == [LLAMA_SERVER_EMBED_PATH]
    assert len(vector) == DIMS


def test_the_lock_is_held_across_the_erase_and_the_embed(tmp_path):
    """Two harness processes sharing one sidecar must not interleave between
    the erase and the embed -- simulated here by holding the SAME lock file
    externally (:func:`trialerror.offload.lock.single_instance_lock`, the
    exact primitive the backend itself uses) while a second thread tries to
    embed through it."""
    lock_path = tmp_path / "slot_erase.lock"
    http = FakeHttp()
    backend, _http, _vocab = _server(n_ctx=4096, http=http, _lock_path=lock_path)

    results: list[list[float]] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(backend.embed_batch(["a passage"])[0])
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors` for the assertion below
            errors.append(exc)

    with backends.single_instance_lock(lock_path):
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=0.3)
        assert thread.is_alive(), "the backend must wait for the externally-held lock"
        assert http.posts == [], "neither the erase nor the embed may start before the lock is free"

    thread.join(timeout=5.0)
    assert not thread.is_alive(), "the backend must proceed once the lock is released"
    assert errors == []
    assert len(results) == 1 and len(results[0]) == DIMS
    assert [path for path, _payload in http.posts] == ["/slots/0?action=erase", LLAMA_SERVER_EMBED_PATH]


def test_the_lock_is_held_without_a_break_from_the_erase_to_the_embed(tmp_path):
    """The test above proves the lock is taken BEFORE the erase.
    It does not prove the lock stays held continuously through to the
    embed -- an implementation that released and re-took it between the two
    calls would pass it too. Here the fake HTTP client itself tries
    `single_instance_lock` on the SAME path from inside every `post_json`
    call (both the erase and the embed), and must get `WorkerAlreadyRunning`
    every time: if the real guard ever let go of the lock between the two
    calls, one of these inner attempts would succeed instead."""
    lock_path = tmp_path / "slot_erase.lock"

    class LockProbingHttp(FakeHttp):
        def post_json(self, path: str, payload: dict):
            with pytest.raises(backends.WorkerAlreadyRunning):
                with backends.single_instance_lock(lock_path):
                    pass
            return super().post_json(path, payload)

    http = LockProbingHttp()
    backend, _http, _vocab = _server(n_ctx=4096, http=http, _lock_path=lock_path)
    backend.embed_batch(["a passage"])
    assert [path for path, _payload in http.posts] == ["/slots/0?action=erase", LLAMA_SERVER_EMBED_PATH]


def test_the_lock_is_released_after_a_failed_erase(tmp_path):
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(
        n_ctx=4096,
        http=FakeHttp(slot_erase_status=500, slot_erase_body={"error": "boom"}),
        _lock_path=lock_path,
    )
    with pytest.raises(EmbedBackendNotRunnable):
        backend.embed_batch(["a passage"])
    with backends.single_instance_lock(lock_path):
        pass  # WorkerAlreadyRunning here would mean the guard never let go


def test_the_lock_is_released_after_a_failed_embed(tmp_path):
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(
        n_ctx=4096, http=FakeHttp(embed_status=500), _lock_path=lock_path
    )
    with pytest.raises(EmbedBackendNotRunnable):
        backend.embed_batch(["a passage"])
    with backends.single_instance_lock(lock_path):
        pass  # WorkerAlreadyRunning here would mean the guard never let go


def test_the_lock_has_a_deadline_naming_the_file_and_the_holder_pid(tmp_path):
    """A holder that is alive but not moving (SIGSTOP, a breakpoint, a
    suspended VM) must not block every embed on this sidecar forever with
    no message: this holder never releases, so it is refused once the
    deadline passes with no new holder. ``_lock_deadline_s`` is a test-only
    seam so this does not have to wait out the real default deadline
    (:meth:`backends.LlamaServerEmbedBackend._lock_deadline_duration_s`).

    This holder takes the raw OS lock directly, never through
    ``_slot_erase_guard``, so it never writes the pid-owner file either --
    exactly the "unknown" case :func:`backends._lock_holder_pid` falls back
    to (a holder from before that file existed, or any process that took
    the lock some other way). The real, common case -- a holder that IS
    another instance of this same backend -- is
    ``test_the_deadline_refusal_names_the_pid_the_real_holder_wrote``,
    below. Either way the lock file itself is always named, which is
    enough to act on."""
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path, _lock_deadline_s=0.3)
    with backends.single_instance_lock(lock_path):
        with pytest.raises(EmbedBackendNotRunnable) as exc:
            backend.embed_batch(["a passage"])
    message = str(exc.value)
    assert str(lock_path) in message
    assert "unknown" in message
    assert "slot_erase = false" in message


_HOLDER_SCRIPT = r"""
import os, sys, time
from pathlib import Path
from trialerror.ingest import backends

lock_path, pid_file = Path(sys.argv[1]), Path(sys.argv[2])


class Http:
    def get_json(self, path):
        return 200, ({"model_path": "/somewhere/model.gguf"} if path == "/props" else {"status": "ok"})

    def post_json(self, path, payload):
        if path == "/embedding":
            pid_file.write_text(str(os.getpid()))  # inside the lock: the guard is held now
            time.sleep(30)
        return 200, {"embedding": [0.5] * 8}


class Vocab:
    def tokenize(self, text, add_bos=True, special=False):
        return list(text)

    def detokenize(self, ids):
        return bytes(ids)


backends.LlamaServerEmbedBackend(
    model_key="k", dims=4, native_dims=8, n_ctx=4096, tokenizer_model_path="/somewhere/model.gguf",
    _http=Http(), _tokenizer=Vocab(), _lock_path=lock_path,
).embed_batch(["x"])
"""


def test_the_deadline_refusal_names_the_pid_the_real_holder_wrote(tmp_path):
    """A holder that goes through ``_slot_erase_guard`` (the only kind that
    exists in production) writes its own pid into the lock's sibling
    ``.owner`` file right after it acquires the lock -- a file no mandatory
    lock covers on Windows, unlike the lock file itself -- so a waiter's
    timeout refusal can always name it. The holder runs in a SUBPROCESS and
    reports its own ``os.getpid()``, so a waiter that named itself (or any
    other process) would fail this."""
    import subprocess

    lock_path = tmp_path / "slot_erase.lock"
    pid_file = tmp_path / "holder.pid"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {key: value for key, value in os.environ.items() if not key.startswith("TRIALERROR_")}
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(lock_path), str(pid_file)],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30.0
        # wait for the pid itself, not the file: the holder creates the file before it writes to it
        while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
            assert holder.poll() is None, "the holder process died before taking the lock"
            time.sleep(0.05)
        assert pid_file.exists(), "the holder never reached its embed"
        holder_pid = pid_file.read_text().strip()
        assert holder_pid and holder_pid != str(os.getpid())

        waiter, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path, _lock_deadline_s=0.3)
        with pytest.raises(EmbedBackendNotRunnable) as exc:
            waiter.embed_batch(["another passage"])
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert f"pid last written to it: {holder_pid})" in str(exc.value)
    assert str(os.getpid()) not in str(exc.value).replace(str(lock_path), "")


def test_a_stale_owner_file_never_names_the_wrong_pid(tmp_path):
    """A holder that writes no owner file (one running code from before it
    existed, or one whose owner write failed) leaves the PREVIOUS holder's
    record behind. The record carries the lock file's mtime from its own
    acquisition; it no longer matches, so the refusal says "unknown"
    rather than naming a process that is not the holder."""
    lock_path = tmp_path / "slot_erase.lock"
    lock_path.write_text("", encoding="ascii")
    backends._lock_owner_path(lock_path).write_text("99999 1\n", encoding="ascii")
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path, _lock_deadline_s=0.2)
    with backends.single_instance_lock(lock_path):
        with pytest.raises(EmbedBackendNotRunnable) as exc:
            backend.embed_batch(["a passage"])
    assert "99999" not in str(exc.value)
    assert "unknown" in str(exc.value)


def test_a_lock_refusal_names_ingest_embed_by_default():
    backend, _http, _vocab = _server(n_ctx=4096)
    assert backend.config_table == "ingest.embed"


def test_a_lock_refusal_names_the_backends_own_config_table(tmp_path):
    """A query-side backend's lock refusal must say `[ingest.embed.query]
    slot_erase`, not always `[ingest.embed]` -- that is the key that
    actually turns ITS erase off."""
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(
        n_ctx=4096, _lock_path=lock_path, _lock_deadline_s=0.2, config_table="ingest.embed.query"
    )
    with backends.single_instance_lock(lock_path):
        with pytest.raises(EmbedBackendNotRunnable) as exc:
            backend.embed_batch(["a passage"])
    assert "[ingest.embed.query] slot_erase = false" in str(exc.value)


def test_the_query_side_loader_sets_the_query_side_config_table():
    backend = load_query_embed_backend(
        {
            "backend": "offload",
            "model_key": "real-key",
            "dims": 2048,
            "query": {
                "backend": LLAMA_SERVER_BACKEND_NAME,
                "tokenizer_model_path": "/x/model.gguf",
            },
        }
    )
    assert backend.config_table == "ingest.embed.query"


def test_the_document_side_loader_sets_the_document_side_config_table():
    backend = load_embed_backend(
        {
            "backend": LLAMA_SERVER_BACKEND_NAME,
            "model_key": "real-key",
            "tokenizer_model_path": "/somewhere/model.gguf",
        }
    )
    assert backend.config_table == "ingest.embed"


class _TimeProxy:
    """The ``time`` module as ``backends`` sees it, with ``time`` and
    ``sleep`` overridden and everything else (``monotonic``, ``perf_counter``,
    ...) passed through to the real module -- so a test can drive the clock
    and record sleeps without patching ``time.time`` itself, which would
    reach pytest's own internals."""

    def __init__(self, *, now=None, sleep=None):
        self._now = now
        self._sleep = sleep

    def time(self):
        return self._now() if self._now is not None else time.time()

    def sleep(self, seconds):
        return self._sleep(seconds) if self._sleep is not None else time.sleep(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


def _fake_time_module(now_holder, sleeps):
    return _TimeProxy(now=lambda: now_holder[0], sleep=sleeps.append)


def test_the_step_aside_sleeps_exactly_when_a_want_is_fresh(tmp_path, monkeypatch):
    """``_step_aside_for_waiter`` against a fake clock: no ``.want`` -> no
    sleep; a ``.want`` younger than ``_WAITER_FRESH_S`` -> one sleep of
    ``_STEP_ASIDE_S``; an older one -> none. No real time is involved."""
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path)
    now = [1_000_000.0]
    sleeps: list[float] = []
    monkeypatch.setattr(backends, "time", _fake_time_module(now, sleeps))
    want = backends._lock_want_path(lock_path)

    backend._step_aside_for_waiter()
    assert sleeps == [], "no .want: nobody is waiting"

    want.write_bytes(b"")
    for age, sleeps_expected in [(0.0, True), (0.05, True), (0.19, True), (0.21, False), (60.0, False)]:
        os.utime(want, (now[0] - age, now[0] - age))
        sleeps.clear()
        backend._step_aside_for_waiter()
        assert sleeps == ([backends._STEP_ASIDE_S] if sleeps_expected else []), f"age {age}"


def test_a_want_stamped_in_the_future_is_not_a_waiter(tmp_path, monkeypatch):
    """A clock that stepped back (an NTP correction) or a skewed file server
    leaves a ``.want`` newer than "now"; its age is negative, and it must
    not count as fresh -- it would make every text boundary sleep."""
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path)
    now = [1_000_000.0]
    sleeps: list[float] = []
    monkeypatch.setattr(backends, "time", _fake_time_module(now, sleeps))
    want = backends._lock_want_path(lock_path)
    want.write_bytes(b"")
    os.utime(want, (now[0] + 3600.0, now[0] + 3600.0))
    backend._step_aside_for_waiter()
    assert sleeps == []


def test_a_former_waiter_does_not_step_aside_for_its_own_touch(tmp_path, monkeypatch):
    """A process that waited touched ``.want`` on every poll, so its last
    touch is still fresh when it finally takes the lock. Taking the lock
    sets ``.want`` back to the epoch, so the first boundaries of its own
    batch do not sleep for nobody."""
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path)
    sleeps: list[float] = []
    want = backends._lock_want_path(lock_path)
    want.write_bytes(b"")  # touched just now, as a polling waiter would
    assert time.time() - want.stat().st_mtime < backends._WAITER_FRESH_S
    monkeypatch.setattr(backends, "time", _TimeProxy(sleep=sleeps.append))
    backend.embed_batch(["one", "two", "three"])
    assert sleeps == [], "the batch stepped aside for its own stale-by-now touch"
    assert want.stat().st_mtime == 0


def test_the_step_aside_does_nothing_when_the_erase_switch_is_off(tmp_path, monkeypatch):
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path, slot_erase=False)
    now = [1_000_000.0]
    sleeps: list[float] = []
    monkeypatch.setattr(backends, "time", _fake_time_module(now, sleeps))
    want = backends._lock_want_path(lock_path)
    want.write_bytes(b"")
    os.utime(want, (now[0], now[0]))
    backend._step_aside_for_waiter()
    assert sleeps == []


def test_a_busy_poll_touches_the_want_signal(tmp_path):
    """The other half of the step-aside: a waiter that finds the lock busy
    must leave ``<lock>.want`` behind for the holder to see."""
    lock_path = tmp_path / "slot_erase.lock"
    want = backends._lock_want_path(lock_path)
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path, _lock_deadline_s=0.2)
    assert not want.exists()
    with backends.single_instance_lock(lock_path):
        with pytest.raises(EmbedBackendNotRunnable):
            backend.embed_batch(["a passage"])
    assert want.exists()


def test_a_waiter_is_served_between_two_texts_of_a_batch_in_that_order(tmp_path, monkeypatch):
    """Ordering, with events and no timing: the holder's text k, then the
    waiter's erase and embed, then the holder's text k+1. The holder's
    step-aside sleep is replaced by "wait until the waiter's erase POST has
    happened" (5 s cap), and any existing ``.want`` counts as fresh, so a
    slow thread cannot change the outcome."""
    lock_path = tmp_path / "slot_erase.lock"
    log: list[str] = []
    log_lock = threading.Lock()
    waiter_erased = threading.Event()
    holder_in_text = {name: threading.Event() for name in ("t0", "t1", "t2")}
    holder_may_finish = {name: threading.Event() for name in ("t0", "t1", "t2")}

    def record(entry: str) -> None:
        with log_lock:
            log.append(entry)

    class HolderHttp(FakeHttp):
        def post_json(self, path: str, payload: dict):
            if path.startswith("/slots/"):
                record("holder erase")
            else:
                name = str(payload["content"])
                record(f"holder embed {name}")
                holder_in_text[name].set()
                assert holder_may_finish[name].wait(10.0), f"the test never released {name}"
            return super().post_json(path, payload)

    class WaiterHttp(FakeHttp):
        def post_json(self, path: str, payload: dict):
            if path.startswith("/slots/"):
                record("waiter erase")
                waiter_erased.set()
            else:
                record("waiter embed")
            return super().post_json(path, payload)

    holder, _h, _v = _server(n_ctx=4096, http=HolderHttp(), _lock_path=lock_path)
    waiter, _h2, _v2 = _server(n_ctx=4096, http=WaiterHttp(), _lock_path=lock_path)
    holder_thread = threading.Thread(target=lambda: holder.embed_batch(["t0", "t1", "t2"]))
    real_sleep = time.sleep

    def sleep(seconds: float) -> None:
        if threading.current_thread() is holder_thread:
            assert waiter_erased.wait(5.0), "the waiter never got the lock while the holder stepped aside"
        else:
            real_sleep(seconds)

    monkeypatch.setattr(backends, "time", _TimeProxy(sleep=sleep))
    monkeypatch.setattr(backends, "_WAITER_FRESH_S", 3600.0)

    waiter_thread = threading.Thread(target=lambda: waiter.embed_batch(["q"]))
    try:
        holder_thread.start()
        assert holder_in_text["t0"].wait(10.0), "the holder never reached its first text"
        waiter_thread.start()
        deadline = time.monotonic() + 10.0
        while not backends._lock_want_path(lock_path).exists():
            assert time.monotonic() < deadline, "the waiter never signalled that it is waiting"
            real_sleep(0.01)
        holder_may_finish["t0"].set()
        assert holder_in_text["t1"].wait(10.0), "the holder never reached its second text"
    finally:
        for event in holder_may_finish.values():
            event.set()
        holder_thread.join(timeout=10.0)
        waiter_thread.join(timeout=10.0)

    assert not holder_thread.is_alive() and not waiter_thread.is_alive()
    assert log[:6] == [
        "holder erase",
        "holder embed t0",
        "waiter erase",
        "waiter embed",
        "holder erase",
        "holder embed t1",
    ]


def test_smoke_a_waiter_behind_a_real_time_batch_does_not_wait_for_the_batch(tmp_path):
    """A real-time smoke test only -- the deterministic tests above carry the
    proof; this one just shows the pieces work together on real clocks.
    Without the ``.want`` signal a query arriving mid-batch waits for the
    WHOLE batch. Here the batch is 12 texts of 0.15 s each (1.8 s); the
    waiter, arriving 0.3 s in, must be through well inside half of it (a
    loose bound, so a slow runner does not flake it; without the signal the
    wait is about 1.5 s)."""
    lock_path = tmp_path / "slot_erase.lock"
    hold_s = 0.15

    class SlowEmbedHttp(FakeHttp):
        def post_json(self, path: str, payload: dict):
            if path == LLAMA_SERVER_EMBED_PATH:
                time.sleep(hold_s)
            return super().post_json(path, payload)

    holder, _http, _vocab = _server(n_ctx=4096, http=SlowEmbedHttp(), _lock_path=lock_path)
    texts = [f"statement {index}" for index in range(12)]
    holder_thread = threading.Thread(target=lambda: holder.embed_batch(texts))
    holder_thread.start()
    time.sleep(0.3)

    waiter, _http2, _vocab2 = _server(n_ctx=4096, _lock_path=lock_path)
    started = time.monotonic()
    vector = waiter.embed_batch(["a query"])[0]
    waited = time.monotonic() - started
    holder_thread.join(timeout=10.0)

    assert len(vector) == DIMS
    assert not holder_thread.is_alive()
    assert waited < 0.9, f"the waiter waited {waited:.2f}s -- for the batch, not for a text"


def test_an_uncontended_batch_leaves_no_want_signal_and_does_not_sleep(tmp_path):
    lock_path = tmp_path / "slot_erase.lock"
    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path)
    started = time.monotonic()
    backend.embed_batch([f"text {index}" for index in range(20)])
    elapsed = time.monotonic() - started
    assert not backends._lock_want_path(lock_path).exists()
    assert elapsed < 1.5, "twenty fake embeds with nobody waiting must not pay the step-aside sleep"


def test_a_busy_holder_that_keeps_reacquiring_never_refuses_the_waiter(tmp_path):
    """The deadline must count time WITHOUT PROGRESS,
    not total wait time. A holder that keeps re-acquiring the lock (a batch
    embedding one text after another, each hold a millisecond or two) is
    healthy, not stuck -- a waiter must never be refused while that keeps
    happening, even once the total wait passes the nominal deadline."""
    lock_path = tmp_path / "slot_erase.lock"
    stop = threading.Event()
    acquisitions = 0

    def hold_and_release_repeatedly() -> None:
        nonlocal acquisitions
        while not stop.is_set():
            try:
                with backends.single_instance_lock(lock_path):
                    acquisitions += 1
                    time.sleep(0.01)
            except backends.WorkerAlreadyRunning:
                # the waiter thread won this particular race for the lock --
                # not this test's concern, just try again
                continue

    holder = threading.Thread(target=hold_and_release_repeatedly)
    holder.start()

    backend, _http, _vocab = _server(n_ctx=4096, _lock_path=lock_path, _lock_deadline_s=0.2)
    results: list[list[float]] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(backend.embed_batch(["a passage"])[0])
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors` for the assertion below
            errors.append(exc)

    waiter = threading.Thread(target=run)
    waiter.start()

    # Keep the holder busy well past the 0.2s nominal deadline before giving
    # the waiter a clear shot at the lock.
    time.sleep(0.6)
    stop.set()
    holder.join(timeout=5.0)
    waiter.join(timeout=5.0)

    assert not waiter.is_alive()
    assert errors == [], f"the waiter must not be refused while the holder keeps making progress: {errors}"
    assert len(results) == 1 and len(results[0]) == DIMS
    assert acquisitions > 1, "the holder must have re-acquired more than once for this to test anything"


def test_a_non_contention_lock_error_is_not_retried_forever(monkeypatch):
    """trialerror.offload.lock.single_instance_lock chains the
    underlying OSError onto WorkerAlreadyRunning. An errno that is not
    contention (EINVAL, ENOLCK, ...) means the lock call itself is broken
    -- for example a filesystem with no real flock -- and retrying it can
    only ever fail the same way again, so this must refuse at once rather
    than spin until a deadline."""
    if sys.platform == "win32":
        import msvcrt

        def _boom(fd, mode, nbytes):
            raise OSError(errno.EINVAL, "invalid argument")

        monkeypatch.setattr(msvcrt, "locking", _boom)
    else:
        import fcntl

        def _boom(fd, operation):
            raise OSError(errno.ENOLCK, "no locks available")

        monkeypatch.setattr(fcntl, "flock", _boom)

    backend, _http, _vocab = _server(n_ctx=4096, _lock_deadline_s=60.0)
    started = time.monotonic()
    with pytest.raises(EmbedBackendNotRunnable) as exc:
        backend.embed_batch(["a passage"])
    assert time.monotonic() - started < 5.0, "a non-contention error must not wait for the deadline"
    assert "not lock contention" in str(exc.value)
    assert "slot_erase = false" in str(exc.value)


def test_the_lock_path_is_normalised_for_scheme_case_and_localhost():
    """Two harness processes that spell the same sidecar
    differently (`localhost` vs `127.0.0.1`, or a different-cased scheme)
    must still serialise through the SAME lock file."""
    same_a = _REAL_SLOT_ERASE_LOCK_PATH("http://localhost:8871")
    same_b = _REAL_SLOT_ERASE_LOCK_PATH("http://127.0.0.1:8871")
    same_c = _REAL_SLOT_ERASE_LOCK_PATH("HTTP://127.0.0.1:8871")
    different = _REAL_SLOT_ERASE_LOCK_PATH("http://127.0.0.1:9999")
    assert same_a == same_b == same_c
    assert different != same_a


# ---------------------------------------------------------------------------
# REQ-2026-09-27-09: the [ingest.embed] / [ingest.embed.query] slot_erase switch
# ---------------------------------------------------------------------------


def test_slot_erase_defaults_to_on_through_the_loader():
    backend = load_embed_backend(
        {
            "backend": LLAMA_SERVER_BACKEND_NAME,
            "model_key": "real-key",
            "tokenizer_model_path": "/somewhere/model.gguf",
        }
    )
    assert backend.slot_erase is True


def test_slot_erase_false_in_config_reaches_the_backend():
    backend = load_embed_backend(
        {
            "backend": LLAMA_SERVER_BACKEND_NAME,
            "model_key": "real-key",
            "tokenizer_model_path": "/somewhere/model.gguf",
            "slot_erase": False,
        }
    )
    assert backend.slot_erase is False


def test_slot_erase_the_string_false_is_parsed_as_false_not_coerced_true():
    """bool("false") is True (any non-empty string is truthy),
    which used to turn this switch's "off" spelling silently into "on"."""
    backend = load_embed_backend(
        {
            "backend": LLAMA_SERVER_BACKEND_NAME,
            "model_key": "real-key",
            "tokenizer_model_path": "/somewhere/model.gguf",
            "slot_erase": "false",
        }
    )
    assert backend.slot_erase is False


@pytest.mark.parametrize("spelling", ["true", "TRUE", "True", "1", "yes", "on"])
def test_slot_erase_recognises_the_usual_true_spellings(spelling):
    backend = load_embed_backend(
        {
            "backend": LLAMA_SERVER_BACKEND_NAME,
            "model_key": "real-key",
            "tokenizer_model_path": "/somewhere/model.gguf",
            "slot_erase": spelling,
        }
    )
    assert backend.slot_erase is True


@pytest.mark.parametrize("spelling", ["false", "FALSE", "False", "0", "no", "off"])
def test_slot_erase_recognises_the_usual_false_spellings(spelling):
    backend = load_embed_backend(
        {
            "backend": LLAMA_SERVER_BACKEND_NAME,
            "model_key": "real-key",
            "tokenizer_model_path": "/somewhere/model.gguf",
            "slot_erase": spelling,
        }
    )
    assert backend.slot_erase is False


def test_slot_erase_an_unrecognised_value_raises_naming_the_key():
    with pytest.raises(ValueError) as exc:
        load_embed_backend(
            {
                "backend": LLAMA_SERVER_BACKEND_NAME,
                "model_key": "real-key",
                "tokenizer_model_path": "/somewhere/model.gguf",
                "slot_erase": "sort of",
            }
        )
    assert "ingest.embed.slot_erase" in str(exc.value) and "sort of" in str(exc.value)


def test_slot_erase_an_unrecognised_value_message_says_the_values_are_strings():
    """The accepted spellings ('0', '1', 'yes', ...) are strings, not the
    bare integers/words they look like -- the message should say so, not
    just show a quoted list a reader could take as decoration."""
    with pytest.raises(ValueError) as exc:
        load_embed_backend(
            {
                "backend": LLAMA_SERVER_BACKEND_NAME,
                "model_key": "real-key",
                "tokenizer_model_path": "/somewhere/model.gguf",
                "slot_erase": "sort of",
            }
        )
    assert "string" in str(exc.value).lower()


def test_slot_erase_a_bare_integer_is_not_coerced():
    """1/0 look like booleans but are not TOML booleans -- a config typo
    that swapped quotes for none must still refuse rather than guess."""
    with pytest.raises(ValueError):
        load_embed_backend(
            {
                "backend": LLAMA_SERVER_BACKEND_NAME,
                "model_key": "real-key",
                "tokenizer_model_path": "/somewhere/model.gguf",
                "slot_erase": 1,
            }
        )


def test_slot_erase_is_honoured_on_the_query_side_table_too():
    backend = load_query_embed_backend(
        {
            "backend": "offload",
            "model_key": "real-key",
            "dims": 2048,
            "query": {
                "backend": LLAMA_SERVER_BACKEND_NAME,
                "tokenizer_model_path": "/x/model.gguf",
                "slot_erase": False,
            },
        }
    )
    assert backend.slot_erase is False


def test_a_slot_erase_key_is_harmless_for_the_fake_backend():
    """Nothing changes for other backends: `load_embed_backend` never reads
    `slot_erase` outside the llama_server branch, so a config carrying the
    key for any other backend is simply ignored, as any other unrelated key
    already is."""
    backend = load_embed_backend({"backend": "fake", "dims": 4, "slot_erase": False})
    assert isinstance(backend, backends.FakeEmbedBackend)
    assert not hasattr(backend, "slot_erase")
