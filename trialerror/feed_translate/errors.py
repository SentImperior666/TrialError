"""Feed-translator exceptions. Mirrors ``trialerror.summarize.errors``'s
split exactly (this whole package is modelled on ``trialerror.summarize`` --
see :mod:`trialerror.feed_translate.api`): a caller that only cares "did the
translation operation fail" catches :class:`FeedTranslateError`; a caller
that needs to branch on *why* catches the specific subclass.
"""

from __future__ import annotations

__all__ = [
    "FeedTranslateError",
    "PostNotFoundError",
    "InvalidStyleModeError",
    "TranslationNotFoundError",
    "TranslatorBackendError",
    "ClaimJudgmentMissingError",
]


class FeedTranslateError(Exception):
    """Base class for every error :mod:`trialerror.feed_translate` raises."""


class PostNotFoundError(FeedTranslateError):
    """``post_id`` names no row in ``ops.feed_post``, or the row it names
    has an empty body (nothing to translate)."""


class InvalidStyleModeError(FeedTranslateError):
    """``style_mode`` was not one of ``strict|flavored`` (the DDL's own
    CHECK constraint, re-verified in this package so a caller gets a typed
    refusal before ever reaching ``trialerror.stores.insert``)."""


class TranslationNotFoundError(FeedTranslateError):
    """No ``feed_post_translation`` row exists for the given lookup (an
    explicit ``translation_id``, or a ``post_id`` with no ``status=
    'current'`` row)."""


class TranslatorBackendError(FeedTranslateError):
    """A configured translator backend could not be constructed, or
    refused to run. Distinct from "the backend ran and produced text that
    failed the gate" -- that is not an error at all, it is a stored row
    with ``gate_status='fail'``
    (:mod:`trialerror.feed_translate.gate`)."""


class ClaimJudgmentMissingError(FeedTranslateError):
    """A ``claim_decomposition``/``claim_judgments`` table
    (:func:`trialerror.feed_translate.gate.judge_from_claim_table`) has no
    entry for a ``pair_id`` the judged faithfulness tier asked about --
    the same refusal ``trialerror.cli.verify``'s own ``_judge_from_table``
    raises for ``verify faithfulness``'s ``--decomposition-file``/
    ``--judgments-file``, ported here rather than reused (see FT-1's fix
    note) because that helper lives in ``trialerror.cli`` and this package
    does not import from the CLI layer."""
