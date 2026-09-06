"""``trialerror.feed_translate`` -- the AI-Speak -> plain-English Feed
translator. Design of record:
``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` (Sections 4.1-4.5, build
steps in Section 5). Operator ask, verbatim: "for Feed messages only (for
now) we want to have AI Speak -> Human English converter, so that I can
see side-by-side original message and translated into simpler language."

Six submodules:

- :mod:`~trialerror.feed_translate.errors` -- this package's exceptions.
- :mod:`~trialerror.feed_translate.style` -- Section 4.5's plain-register
  contract as an executable checker, split into ``fidelity`` rules (which
  fail the gate) and ``register`` rules (which are flagged).
- :mod:`~trialerror.feed_translate.backends` -- the translator seam: a
  ``Protocol`` plus a deterministic fake, the default envelope-parking
  backend, and a model backend wired for the budget law.
- :mod:`~trialerror.feed_translate.gate` -- the fail-closed faithfulness
  guard (Section 4.3): always-on deterministic fidelity tier, optional
  judged tier anchored on the original post body.
- :mod:`~trialerror.feed_translate.api` -- envelope building, versioned
  storage in ``ops.feed_post_translation``, lookups, and the shared
  staleness key.
- :mod:`~trialerror.feed_translate.handlers` -- the ``feed_translate`` job
  handler (translation is a JOB, never an inline call).
- :mod:`~trialerror.feed_translate.checks` -- the
  ``feed_translation_failures`` and ``feed_translations_stale`` doctor
  checks.

**Two invariants this package will not break.**

1. *The original is never touched.* ``ops.feed_post`` is append-only and
   its ``author`` is server-derived (``trialerror.events.api._derive_author``,
   audited by ``trialerror.events.checks.check_feed_author_integrity``).
   A translation is a SIDECAR row in a separate table; nothing here
   inserts, updates or deletes a ``feed_post``, so no author-derivation
   rule can be bent by translating.
2. *Fail closed.* A translation that fails the guard is stored (so it can
   be counted and read) and never served. The dashboard keeps showing the
   original with an explicit "withheld" note -- never a plausible-looking
   plain-English rendering of a gate verdict that says the opposite of
   what happened.

**LLM-judgment boundary (the house pattern).** Nothing in this package
calls an LLM. It builds envelopes and stores the text a caller hands
back: a backend, a live agent session filling a parked envelope, or a
deterministic fake in tests.
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
