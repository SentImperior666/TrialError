"""Retrieval exceptions. Mirrors ``trialerror.stores.errors``/``trialerror.ingest.errors``'s
pattern: a common base class every caller that only cares "did this
retrieval call fail" can catch, plus specific subclasses for callers that
need to branch on *why*."""

from __future__ import annotations

# A leaf module (no imports of its own), so this cannot cycle: the same
# class the query-side backends raise, so ONE `except` catches a query
# embedding that could not be produced, wherever it was refused.
from trialerror.ingest.errors import QueryEmbedUnavailable

__all__ = [
    "RetrievalError",
    "ChunkNotFoundError",
    "SourceNotFoundError",
    "DocumentNotFoundError",
    "EntityNotFoundError",
    "InvalidSearchModeError",
    "QueryEmbedBackendUnrunnableError",
    "QUERY_EMBED_UNRUNNABLE_CODE",
]

#: The envelope error code every surface reports a query-side embedding
#: failure under -- the SAME string the doctor check is named after
#: (``query_embed_backend_runnable``'s failing half), so an operator reading
#: an error and an operator reading a doctor line are reading one name.
QUERY_EMBED_UNRUNNABLE_CODE = "query_embed_backend_unrunnable"


class RetrievalError(Exception):
    """Base class for every error the ``trialerror.retrieve`` package raises.

    ``code`` is the envelope error code a surface should report this error
    under, for the cases where the class NAME is not the operator-facing
    name (lane F-1). ``None`` keeps the long-standing default -- the CLI and
    the MCP server both fall back to ``type(exc).__name__`` -- so every
    existing subclass reports exactly what it reported before."""

    code: str | None = None


class ChunkNotFoundError(RetrievalError):
    """No ``chunk`` row exists with the given ``chunk_id``."""


class SourceNotFoundError(RetrievalError):
    """No ``source`` row exists with the given ``source_id``."""


class DocumentNotFoundError(RetrievalError):
    """No ``document`` row exists with the given ``doc_id``."""


class EntityNotFoundError(RetrievalError):
    """No ``entity`` row exists with the given ``entity_id``."""


class InvalidSearchModeError(RetrievalError):
    """``SearchRequest.mode`` was not one of ``auto|fts|vector|hybrid|graph``."""


class QueryEmbedBackendUnrunnableError(RetrievalError, QueryEmbedUnavailable):
    """No query vector could be produced in this process, and the caller
    asked for something that cannot be answered without one.

    Raised only where there is genuinely nothing to return: ``mode="vector"``
    (the vector tier IS the whole search, so "ran the other tiers" is not a
    degraded answer, it is a different question). The two-stage modes degrade
    instead -- FTS-only results plus ``stats.vector_skipped_reason`` and an
    envelope warning -- because there, a lexical answer is the same answer
    this engine would have given before the vector tier existed."""

    code = QUERY_EMBED_UNRUNNABLE_CODE
