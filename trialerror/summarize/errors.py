"""Summarize-subsystem exceptions. Mirrors ``trialerror.verify.errors``/
``trialerror.retrieve.errors``'s split: a caller that only cares "did the
summarize operation fail" catches :class:`SummarizeError`; a caller that
needs to branch on *why* catches the specific subclass.
"""

from __future__ import annotations

__all__ = [
    "SummarizeError",
    "InvalidSubjectKindError",
    "SubjectNotFoundError",
    "SummaryNotFoundError",
    "SummaryFenceViolationError",
]


class SummarizeError(Exception):
    """Base class for every error :mod:`trialerror.summarize` raises."""


class InvalidSubjectKindError(SummarizeError):
    """``subject_kind`` was not one of ``document|collection`` (the DDL's
    CHECK constraint, re-verified here so this package returns a clean,
    typed refusal before ever reaching ``trialerror.stores.insert``)."""


class SubjectNotFoundError(SummarizeError):
    """A ``document`` subject named a ``doc_id`` with no matching row, a
    ``collection`` subject resolved to zero member ``doc_id``s, or a
    ``document`` subject has no ``element`` rows yet (not normalized) so
    there is nothing to summarize."""


class SummaryNotFoundError(SummarizeError):
    """No ``summary`` row exists for the given lookup (an explicit
    ``summary_id``, or a ``(subject_kind, subject_id)`` pair with no
    ``status='current'`` row)."""


class SummaryFenceViolationError(SummarizeError):
    """:func:`trialerror.summarize.api.store_summary` refused a summary body:
    at least one of its cited source documents is
    ``commercial_restricted``, and the body contains an embedded quoted
    run longer than the D-COC-1 20-word cap
    (``trialerror.retrieve.fence.MAX_FENCED_EXCERPT_WORDS``). An L1 overview is
    EXTRACTION, not verbatim reproduction — the overview text itself is
    fine at any length — but a literal quoted excerpt embedded inside it is
    held to the same fence every other verbatim excerpt in this codebase
    is held to."""
