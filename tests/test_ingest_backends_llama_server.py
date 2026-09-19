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

import hashlib
import json
import math
import struct
import types

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
    ):
        self.health_status = health_status
        self.health_body = {"status": "ok"} if health_body is None else health_body
        self.props_status = props_status
        self.props_body = {"model_path": "/somewhere/model.gguf"} if props_body is None else props_body
        self.embed_status = embed_status
        self.shape = shape
        self.fail_with = fail_with
        self.native_dims = native_dims
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
    yield
    backends._LLAMA_INSTANCES.clear()
    backends._VOCAB_ONLY_INSTANCES.clear()


# ---------------------------------------------------------------------------
# the request: endpoint, payload, geometry
# ---------------------------------------------------------------------------


def test_the_client_posts_the_prepared_text_to_the_native_embedding_endpoint():
    backend, http, _vocab = _server(n_ctx=4096)
    backend.embed_batch(["what is a quorum"], kind="query")
    assert [path for path, _payload in http.posts] == [LLAMA_SERVER_EMBED_PATH]
    payload = http.posts[0][1]
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
    assert [payload["content"] for _path, payload in http.posts] == ["", "x"]


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
