"""The translator was retired in Phase 0. ``style`` and ``errors`` remain
because a held branch (``ideas/land-2026-09-26``) imports them. Remove them
when that branch lands without them or is abandoned.

- :mod:`~trialerror.feed_translate.errors` -- this package's exceptions.
- :mod:`~trialerror.feed_translate.style` -- the plain-register contract as an
  executable checker, split into ``fidelity`` rules and ``register`` rules.
"""

from __future__ import annotations

from trialerror.feed_translate.errors import (
    FeedTranslateError,
    InvalidStyleModeError,
    PostNotFoundError,
    TranslationNotFoundError,
    TranslatorBackendError,
)

__all__ = [
    "FeedTranslateError",
    "PostNotFoundError",
    "InvalidStyleModeError",
    "TranslationNotFoundError",
    "TranslatorBackendError",
]
