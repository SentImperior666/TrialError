"""Lane F-1b stage 3: the sidecar's OWN context is checked, and two refusal
strings stop saying things that are not true.

``docs/reviews/VERIFY_f1b-sidecar.md``:

- **V-3** -- ``content_budget = n_ctx - 2`` makes the client side airtight, but
  the property it buys (never sending a request of exactly the server's context
  length) is JOINT with the ``-c`` the sidecar was started with. With the
  default client ``n_ctx = 2049`` and a sidecar started ``-c 2048``, a
  full-length request is 2048 ids = the server's exact context = precisely the
  refusal the arithmetic exists to avoid -- and it passes every check, failing
  only on long inputs, mid-run.
- **V-10** -- a connection refused in 10 ms was reported as "did not answer
  within 60.0s", and the remedy always named the sidecar ``embed`` whatever
  the program calls it.

No socket, no model file, no live server: the HTTP client and the tokenizer are
injected fakes.
"""

from __future__ import annotations

import time

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import (
    DEFAULT_LLAMA_SERVER_N_CTX,
    LLAMA_SERVER_HEALTH_PATH,
    LLAMA_SERVER_PROPS_PATH,
    LlamaServerEmbedBackend,
    load_embed_backend,
)

NATIVE = 8
DIMS = 4


class FakeVocab:
    def tokenize(self, text: bytes, add_bos: bool = True, special: bool = False):
        return list(text)

    def detokenize(self, ids) -> bytes:
        return bytes(ids)


class FakeHttp:
    """``/health`` and ``/props`` from attributes a test sets; nothing else is
    needed to drive ``runnable()``."""

    def __init__(self, *, props_body: object | None = None, health_body: object | None = None,
                 props_status: int = 200, fail_with: Exception | None = None):
        self.props_body = props_body
        self.health_body = {"status": "ok"} if health_body is None else health_body
        self.props_status = props_status
        self.fail_with = fail_with
        self.gets: list[str] = []

    def get_json(self, path: str):
        if self.fail_with is not None:
            raise self.fail_with
        self.gets.append(path)
        if path == LLAMA_SERVER_HEALTH_PATH:
            return 200, self.health_body
        if path == LLAMA_SERVER_PROPS_PATH:
            return self.props_status, self.props_body
        raise AssertionError(f"unexpected GET {path}")


def _backend(tmp_path, *, n_ctx: int = DEFAULT_LLAMA_SERVER_N_CTX, http=None, **kwargs):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"not a real gguf")
    return LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=n_ctx,
        tokenizer_model_path=str(model),
        _http=http if http is not None else FakeHttp(props_body={"model_path": str(model)}),
        _tokenizer=FakeVocab(),
        **kwargs,
    )


def _props(model_path: str, n_ctx: int | None, *, nested: bool = True) -> dict:
    body: dict = {"model_path": model_path}
    if n_ctx is None:
        return body
    if nested:
        body["default_generation_settings"] = {"n_ctx": n_ctx}
    else:
        body["n_ctx"] = n_ctx
    return body


# ---------------------------------------------------------------------------
# V-3: the served context
# ---------------------------------------------------------------------------


def test_a_server_one_id_too_narrow_is_refused(tmp_path):
    """The V-3 scenario exactly: client 2049, sidecar started `-c 2048`."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=2049,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(props_body=_props(str(model), 2048)),
    )
    ok, reason = backend.runnable()

    assert ok is False
    assert "2048" in reason and "2049" in reason
    assert "-c 2049" in reason, "the refusal names the flag that fixes it"
    assert backend.runtime_details()["context_check"] == backends.CONTEXT_CHECK_TOO_SMALL
    assert backend.runtime_details()["served_n_ctx"] == 2048


def test_a_server_wide_enough_is_verified(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=2049,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(props_body=_props(str(model), 2049)),
    )
    assert backend.runnable() == (True, "")
    assert backend.runtime_details()["context_check"] == backends.CONTEXT_CHECK_VERIFIED
    assert backend.runtime_details()["served_n_ctx"] == 2049


def test_a_wider_server_is_fine(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=2049,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(props_body=_props(str(model), 8192)),
    )
    assert backend.runnable() == (True, "")
    assert backend.runtime_details()["served_n_ctx"] == 8192


def test_a_top_level_n_ctx_is_read_too(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=2049,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(props_body=_props(str(model), 2048, nested=False)),
    )
    assert backend.runnable()[0] is False


def test_a_build_that_does_not_report_its_context_is_not_a_blocker(tmp_path):
    """Same reading as the model check: silence is a warning, not a refusal."""
    backend = _backend(tmp_path)
    assert backend.runnable() == (True, "")
    details = backend.runtime_details()
    assert details["context_check"] == backends.CONTEXT_CHECK_UNVERIFIED
    assert details["served_n_ctx"] is None
    assert "unverified" in details["context_check_note"]


def test_the_health_body_is_the_fallback_source(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    http = FakeHttp(
        props_body=None, props_status=404,
        health_body=_props(str(model), 1024),
    )
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=2049,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(), _http=http,
    )
    ok, reason = backend.runnable()
    assert ok is False and "1024" in reason


def test_props_is_fetched_once_for_both_checks(tmp_path):
    backend = _backend(tmp_path)
    http = backend._http_client
    backend.runnable()
    assert http.gets.count(LLAMA_SERVER_PROPS_PATH) == 1


def test_a_nonsense_context_value_is_ignored_rather_than_refused(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    for value in (0, -1, True, "2048", None):
        backend = LlamaServerEmbedBackend(
            model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=2049,
            tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
            _http=FakeHttp(props_body={"model_path": str(model),
                                       "default_generation_settings": {"n_ctx": value}}),
        )
        assert backend.runnable() == (True, ""), value
        assert backend.runtime_details()["context_check"] == backends.CONTEXT_CHECK_UNVERIFIED


def test_the_model_check_still_refuses_first(tmp_path):
    """A wrong model is refused whatever the context says: the order of the
    two checks is the order of their severity."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, n_ctx=2049,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(props_body=_props("/elsewhere/other.gguf", 8192)),
    )
    ok, reason = backend.runnable()
    assert ok is False and "other.gguf" in reason
    assert backend.runtime_details()["model_check"] == backends.MODEL_CHECK_MISMATCH


# ---------------------------------------------------------------------------
# V-10: two strings that were not true
# ---------------------------------------------------------------------------


def test_an_instant_refusal_is_not_reported_as_a_long_wait(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, timeout_s=60,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(fail_with=OSError("[Errno 111] Connection refused")),
    )
    started = time.perf_counter()
    ok, reason = backend.runnable()
    elapsed = time.perf_counter() - started

    assert ok is False and elapsed < 1.0
    assert "Connection refused" in reason
    assert "within 60" not in reason, "it claimed a wait that did not happen"
    assert "gave up after 0.0" in reason and "60s budget" in reason
    assert backend.runtime_details()["health"]["waited_s"] < 1.0


def test_the_remedy_never_invents_a_sidecar_name(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE,
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(fail_with=OSError("down")),
    )
    _ok, reason = backend.runnable()
    assert "sidecar start <name>" in reason
    assert "sidecar start embed" not in reason
    assert "sidecar_name" in reason, "it names the key that would let it be specific"


def test_a_configured_sidecar_name_is_the_one_named(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = LlamaServerEmbedBackend(
        model_key="k", dims=DIMS, native_dims=NATIVE, sidecar_name="encoder",
        tokenizer_model_path=str(model), _tokenizer=FakeVocab(),
        _http=FakeHttp(fail_with=OSError("down")),
    )
    _ok, reason = backend.runnable()
    assert "trialerror sidecar start encoder" in reason
    assert backend.runtime_details()["sidecar_name"] == "encoder"


def test_the_config_key_reaches_the_backend(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    backend = load_embed_backend(
        {
            "backend": "llama_server", "model_key": "k", "dims": DIMS,
            "sidecar_name": "encoder", "tokenizer_model_path": str(model),
        },
        table="ingest.embed.query",
    )
    assert backend.sidecar_name == "encoder"


def test_no_configured_name_leaves_it_unset(tmp_path):
    backend = load_embed_backend(
        {"backend": "llama_server", "model_key": "k", "dims": DIMS},
        table="ingest.embed.query",
    )
    assert backend.sidecar_name is None
    assert backend.runtime_details()["sidecar_name"] is None
