"""The QUERY-time encoder seam. Build brief item 4: "encode the query with
OpenAI text-embedding-3-large via an encoder seam -- config points at a key
FILE path ... NEVER read, print, or copy its contents -- the code reads it
at call time, tests use a fake encoder."

Mirrors ``trialerror.ingest.backends``'s ``EmbedBackend`` Protocol split
(``Fake``/``Real*``) exactly -- same reason: tests must never need a live
API key or network call. The one difference: this dataset's vectors are
already precomputed (module docstring, ``trialerror.arxiv_index`` package doc),
so this seam is QUERY-only (one short string per call), never a bulk
document-embedding path -- there is no ``ingest.embed`` job kind here,
only ``trialerror lit arxiv-semantic``'s single query call.

Key handling reuses :func:`trialerror.litapi.config.resolve_api_key` directly
(not reimplemented) -- that function's contract ("read ONLY from the
configured file path ... never an environment-variable or inline-config
fallback ... never raises for a missing key, only for an unreadable
EXISTING file") is exactly the discipline this seam needs, and it is
already duck-typed against any object with an ``api_key_path`` attribute
(see that function's own docstring: "Accepts AlphaxivConfig too ... both
dataclasses share the same api_key_path-only-source shape") --
:class:`trialerror.litapi.config.ArxivIndexConfig` is a third such dataclass,
reusing the identical shape rather than re-deriving the "never log it"
discipline a third time.
"""

from __future__ import annotations

import hashlib
import json
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "OPENAI_EMBEDDINGS_URL",
    "OPENAI_MODEL",
    "OPENAI_PRICE_PER_1M_TOKENS_USD",
    "QueryEncoder",
    "FakeQueryEncoder",
    "OpenAIQueryEncoder",
    "OpenAIEncoderError",
    "estimate_token_count",
    "estimate_query_cost_usd",
]

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
OPENAI_MODEL = "text-embedding-3-large"

#: docs/reviews/ALL_ARXIV_SEARCH.md Sec 2 (WebSearch, 2026-08-29, sourced --
#: not re-verified live by THIS build): standard (non-batch) per-1M-input-
#: token price for text-embedding-3-large. A single query is "tens of
#: tokens" per that same review -- this constant plus
#: :func:`estimate_token_count`'s rough heuristic is a documentation/
#: cost-transparency aid, not a billing-accurate calculation (OpenAI's own
#: tokenizer, tiktoken, is not a dependency this build adds just for a
#: cost estimate print).
OPENAI_PRICE_PER_1M_TOKENS_USD = 0.13


class OpenAIEncoderError(RuntimeError):
    """Raised by :class:`OpenAIQueryEncoder` on a missing key, a transport
    failure, or a malformed response -- never silently returns a zero/fake
    vector for a real-backend call."""


class QueryEncoder(Protocol):
    model_key: str
    dims: int

    def encode_query(self, text: str) -> list[float]: ...


def estimate_token_count(text: str) -> int:
    """Rough, documented-as-rough heuristic (~4 chars/token, the same
    order-of-magnitude approximation OpenAI's own docs use informally for
    English text) -- good enough for the cost-transparency print this
    module exists to support, not a billing-accurate tokenizer."""
    return max(1, (len(text) + 3) // 4)


def estimate_query_cost_usd(text: str) -> float:
    tokens = estimate_token_count(text)
    return (tokens / 1_000_000.0) * OPENAI_PRICE_PER_1M_TOKENS_USD


@dataclass
class FakeQueryEncoder:
    """Deterministic, zero-network stand-in -- same ``sha256(text)``-
    expanded-to-``dims``-floats, L2-normalized construction
    ``trialerror.ingest.backends.FakeEmbedBackend`` uses (same text -> same
    vector, always), so a test can build a fixture's stored vectors AND its
    query vectors through the identical deterministic function and get
    meaningful (non-random) cosine-similarity orderings out of a synthetic
    corpus. ``dims`` defaults to the REAL dataset's 3072 so a test can
    exercise full-width vectors without any live API call; pass a smaller
    value for fast unit tests that don't care about width."""

    dims: int = 3072
    model_key: str = "fake-openai-text-embedding-3-large"

    def __post_init__(self) -> None:
        self.model_key = f"fake-openai-text-embedding-3-large-{self.dims}"

    def encode_query(self, text: str) -> list[float]:
        needed_bytes = self.dims * 4
        digest = b""
        counter = 0
        while len(digest) < needed_bytes:
            digest += hashlib.sha256(f"{text}::{counter}".encode("utf-8")).digest()
            counter += 1
        raw = struct.unpack(f"<{self.dims}I", digest[:needed_bytes])
        floats = [(v / 0xFFFFFFFF) * 2.0 - 1.0 for v in raw]
        norm = sum(f * f for f in floats) ** 0.5 or 1.0
        return [f / norm for f in floats]


class OpenAIQueryEncoder:
    """The real backend: one POST to ``https://api.openai.com/v1/embeddings``
    per :meth:`encode_query` call, stdlib ``urllib.request`` only (same
    zero-new-dependency choice ``trialerror.litapi.transport.UrllibTransport``
    documents for the identical reason -- this module does not reuse that
    class directly because ``ProviderTransport`` is GET-only by design
    (that Protocol's own docstring) and OpenAI's embeddings endpoint is
    POST+JSON+bearer-auth; a self-contained client here is a smaller
    surface than widening a four-provider-shared Protocol for one POST
    caller).

    ``api_key`` is passed in ALREADY RESOLVED (this class never reads a
    file itself) -- the caller (``trialerror.arxiv_index.query``'s CLI-facing
    factory) is the one place that calls
    ``trialerror.litapi.config.resolve_api_key``, so "never read a key except
    from a configured path" stays enforced at exactly one call site, same
    as every other provider client in this repo.
    """

    model_key = "openai-text-embedding-3-large"
    dims = 3072

    def __init__(self, *, api_key: str, model: str = OPENAI_MODEL, timeout_s: float = 30.0):
        if not api_key:
            raise OpenAIEncoderError("OpenAIQueryEncoder requires a non-empty api_key")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    def encode_query(self, text: str) -> list[float]:
        body = json.dumps({"model": self._model, "input": text}).encode("utf-8")
        request = urllib.request.Request(
            OPENAI_EMBEDDINGS_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as resp:  # noqa: S310 - deliberate: this IS the http client
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OpenAIEncoderError(f"OpenAI embeddings call failed: HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise OpenAIEncoderError(f"OpenAI embeddings call failed: {exc}") from exc

        try:
            vector = payload["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAIEncoderError(f"OpenAI embeddings response missing data[0].embedding: {payload!r}") from exc
        return [float(v) for v in vector]
