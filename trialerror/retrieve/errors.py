"""Retrieval exceptions. Mirrors ``trialerror.stores.errors``/``trialerror.ingest.errors``'s
pattern: a common base class every caller that only cares "did this
retrieval call fail" can catch, plus specific subclasses for callers that
need to branch on *why*."""

from __future__ import annotations

__all__ = [
    "RetrievalError",
    "ChunkNotFoundError",
    "SourceNotFoundError",
    "DocumentNotFoundError",
    "EntityNotFoundError",
    "InvalidSearchModeError",
]


class RetrievalError(Exception):
    """Base class for every error the ``trialerror.retrieve`` package raises."""


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
