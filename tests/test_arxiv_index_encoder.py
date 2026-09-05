"""Unit tests for :mod:`trialerror.arxiv_index.encoder` -- the FakeQueryEncoder
determinism, cost estimate, and OpenAIQueryEncoder's request/response
handling (offline -- ``urllib.request.urlopen`` is monkeypatched, never a
real network call, same "tests use a fake encoder" / offline-testable
discipline this repo's litapi package uses for its own transport)."""

from __future__ import annotations

import json
import urllib.error

import pytest

from trialerror.arxiv_index.encoder import (
    OPENAI_MODEL,
    FakeQueryEncoder,
    OpenAIEncoderError,
    OpenAIQueryEncoder,
    estimate_query_cost_usd,
    estimate_token_count,
)


def test_fake_query_encoder_deterministic_same_text_same_vector():
    enc = FakeQueryEncoder(dims=32)
    a = enc.encode_query("hello world")
    b = enc.encode_query("hello world")
    assert a == b


def test_fake_query_encoder_different_text_different_vector():
    enc = FakeQueryEncoder(dims=32)
    a = enc.encode_query("hello world")
    b = enc.encode_query("goodbye world")
    assert a != b


def test_fake_query_encoder_l2_normalized():
    enc = FakeQueryEncoder(dims=64)
    v = enc.encode_query("normalize me")
    norm = sum(x * x for x in v) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_fake_query_encoder_default_dims_matches_real_model():
    enc = FakeQueryEncoder()
    assert enc.dims == 3072
    assert len(enc.encode_query("x")) == 3072


def test_fake_query_encoder_model_key_namespaced_by_dims():
    a = FakeQueryEncoder(dims=8)
    b = FakeQueryEncoder(dims=16)
    assert a.model_key != b.model_key


def test_estimate_token_count_grows_with_length():
    assert estimate_token_count("a") >= 1
    assert estimate_token_count("a" * 400) > estimate_token_count("a" * 4)


def test_estimate_query_cost_usd_is_small_and_positive():
    cost = estimate_query_cost_usd("a short search query about distributed systems architecture")
    assert 0 < cost < 0.001


def test_openai_query_encoder_rejects_empty_key():
    with pytest.raises(OpenAIEncoderError):
        OpenAIQueryEncoder(api_key="")


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_openai_query_encoder_encode_query_success(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        payload = {"data": [{"embedding": [0.1, 0.2, 0.3]}], "model": OPENAI_MODEL}
        return _FakeHTTPResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("trialerror.arxiv_index.encoder.urllib.request.urlopen", fake_urlopen)

    enc = OpenAIQueryEncoder(api_key="sk-test-key")
    vector = enc.encode_query("distributed systems architecture")

    assert vector == [0.1, 0.2, 0.3]
    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-key"
    assert captured["body"] == {"model": OPENAI_MODEL, "input": "distributed systems architecture"}


def test_openai_query_encoder_never_logs_or_returns_the_key_itself(monkeypatch):
    """The key must appear ONLY in the Authorization header, never echoed
    back in any exception message or return value."""

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("trialerror.arxiv_index.encoder.urllib.request.urlopen", fake_urlopen)
    enc = OpenAIQueryEncoder(api_key="sk-secret-do-not-leak")
    with pytest.raises(OpenAIEncoderError) as exc_info:
        enc.encode_query("q")
    assert "sk-secret-do-not-leak" not in str(exc_info.value)


def test_openai_query_encoder_http_error_raises_encoder_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        import io

        fp = io.BytesIO(b'{"error": {"message": "bad request"}}')
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, fp)

    monkeypatch.setattr("trialerror.arxiv_index.encoder.urllib.request.urlopen", fake_urlopen)
    enc = OpenAIQueryEncoder(api_key="sk-test")
    with pytest.raises(OpenAIEncoderError, match="HTTP 400"):
        enc.encode_query("q")


def test_openai_query_encoder_url_error_raises_encoder_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("trialerror.arxiv_index.encoder.urllib.request.urlopen", fake_urlopen)
    enc = OpenAIQueryEncoder(api_key="sk-test")
    with pytest.raises(OpenAIEncoderError):
        enc.encode_query("q")


def test_openai_query_encoder_malformed_response_raises_encoder_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"unexpected": "shape"}).encode("utf-8"))

    monkeypatch.setattr("trialerror.arxiv_index.encoder.urllib.request.urlopen", fake_urlopen)
    enc = OpenAIQueryEncoder(api_key="sk-test")
    with pytest.raises(OpenAIEncoderError, match="missing data"):
        enc.encode_query("q")
