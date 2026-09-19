"""Pluggable translator backends -- the seam that decides WHO turns a
dense Feed post into plain English.

Shaped after :mod:`trialerror.ingest.backends` (one small
:class:`~typing.Protocol`, a deterministic ``Fake`` used by default and in
every test, a factory ``load_translator_backend(config)`` reading the
program's ``trialerror.toml`` so a third backend is a config value away and
never a code edit), and after :mod:`trialerror.summarize` for the
LLM-judgment boundary itself.

Three implementations ship:

- :class:`FakeTranslatorBackend` -- deterministic, zero-dependency,
  zero-network. Rewrites the envelope's ``original_body`` by a fixed set
  of substitutions (a jargon glossary plus sentence splitting) that
  PRESERVES every id, number, date and hedge, so a fake translation passes
  :mod:`trialerror.feed_translate.style` by construction. This is what the
  end-to-end job test drives (m2, fix pass: NOT what a fresh program
  scaffold gets by default -- see :class:`PendingTranslatorBackend` below,
  which is).
- :class:`PendingTranslatorBackend` -- the DEFAULT for any program that
  has not configured a model backend. It never produces text: it returns
  ``None``, which parks the built envelope PENDING in the job's checkpoint
  for a caller with an agent handy to fill and resubmit (``trialerror feed
  translate --judgments-file ...``). This is the exact
  ``trialerror.summarize.handlers.run_summarize`` contract, and it is why
  this package can ship a real, useful job handler in an offline
  jobs/CLI layer that has no LLM in it.
- :class:`ModelTranslatorBackend` -- the shape a real model-backed
  translator takes when one lands. It is deliberately NOT wired to a
  provider SDK in this build, because none exists anywhere in this repo
  (the only "real" backend that ships, ``trialerror.ingest.backends.
  RealQwenEmbedBackend``, is an EMBEDDING model reached by subprocess, and
  there is no generative counterpart -- ``docs/reviews/
  AISPEAK_TRANSLATOR_DESIGN.md`` Section 3 records that absence as a build
  gap, not an oversight). Constructing it succeeds; calling
  :meth:`~ModelTranslatorBackend.translate` raises
  :class:`~trialerror.feed_translate.errors.TranslatorBackendError` naming
  the missing driver. What it DOES carry today is the whole budget-law
  contract (:attr:`requires_booking`), so the day a driver is added, the
  spawn law is already enforced around it rather than bolted on after.

**Budget law, structurally (design constraint: never bypass the spawn
law).** Every backend declares :attr:`TranslatorBackend.requires_booking`.
A backend that would spend real tokens sets it ``True``, and
:func:`trialerror.feed_translate.handlers.run_feed_translate` then REFUSES to
call it unless the job payload's ``created_by_launch`` resolves to a real
``platform.launch`` row -- i.e. unless a ``trialerror.budget.pools.book_launch``
already happened and the spawn gate already consumed that booking. The
fake and pending backends set it ``False``: they spend nothing, so
requiring a booking for them would be theatre, and would make the
orchestrator's own zero-cost live-session path (design Section 3, "the
orchestrator's own open session is not a new booking") impossible to
express.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from trialerror.feed_translate.errors import TranslatorBackendError

__all__ = [
    "TranslatorBackend",
    "FakeTranslatorBackend",
    "PendingTranslatorBackend",
    "ModelTranslatorBackend",
    "DEFAULT_TRANSLATOR_MODEL",
    "load_translator_backend",
]

#: ``[feed.translator] model`` default (the orchestrator's design
#: constraint). Only ever read by :class:`ModelTranslatorBackend`; the fake
#: and pending backends have no model.
DEFAULT_TRANSLATOR_MODEL = "claude-sonnet-5"


class TranslatorBackend(Protocol):
    name: str
    version: str
    #: Whether a real, booked ``platform.launch`` must exist before this
    #: backend may be called (module docstring, "Budget law").
    requires_booking: bool

    def translate(self, envelope: Mapping[str, Any]) -> str | None:
        """Plain-English text for ``envelope["original_body"]``, or
        ``None`` to park the envelope PENDING (the caller stores nothing
        and records the envelope in the job checkpoint instead)."""
        ...


# ---------------------------------------------------------------------------
# fake
# ---------------------------------------------------------------------------

#: Ops-jargon -> plain-English substitutions the fake applies. Every entry
#: is a phrase, never an id/number/date pattern, so
#: :func:`trialerror.feed_translate.style.check_style`'s fidelity tier is
#: untouched by construction. Case-insensitive, longest-first at match
#: time.
_FAKE_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("launch-booked", "booked in the budget ledger"),
    ("launch booked", "booked in the budget ledger"),
    ("reconciled", "matched up"),
    ("reconcile", "match up"),
    ("escrow commit", "held-back commit"),
    ("stratified", "split into bands"),
    ("tercile", "third"),
    ("keystone", "milestone"),
    ("gate verdict", "gate result"),
    ("gate transition", "gate move"),
    ("workpackage", "work package"),
    ("envelope", "request form"),
    ("idempotent", "safe to repeat"),
    ("checkpoint", "saved progress marker"),
    ("backoff", "wait-and-retry"),
    ("XID", "cross-database reference"),
    ("corrections ledger", "rules log"),
)

_FAKE_SPLIT_RE = re.compile(r"\s*;\s*")


@dataclass
class FakeTranslatorBackend:
    """Deterministic translator stand-in. Same job in this package that
    :class:`trialerror.ingest.backends.FakeEmbedBackend` does in that one:
    make the whole path (enqueue -> worker -> backend -> gate -> stored
    row -> dashboard) exercisable with zero network, zero model, zero cost
    -- the build brief's "no network calls to LLM APIs from tests", met
    structurally rather than by mocking.

    The rewrite is intentionally shallow (a jargon glossary plus a
    semicolon split), because its job is to be FAITHFUL, not clever: it
    must pass :func:`trialerror.feed_translate.style.check_style` on any
    input so a test that sees ``gate_status='fail'`` knows the gate caught
    something real. Tests that need a FAILING translation supply the bad
    text directly through the ``judgments`` payload instead of asking this
    backend to misbehave.
    """

    name: str = "fake"
    version: str = "1"
    requires_booking: bool = False

    def translate(self, envelope: Mapping[str, Any]) -> str:
        text = str(envelope.get("original_body") or "")
        for jargon, plain in sorted(_FAKE_GLOSSARY, key=lambda kv: -len(kv[0])):
            text = re.sub(re.escape(jargon), plain, text, flags=re.IGNORECASE)
        # §4.5 rule 4: no semicolons -- split each into its own sentence.
        parts = [p.strip() for p in _FAKE_SPLIT_RE.split(text) if p.strip()]
        rebuilt = " ".join(p if p.endswith((".", "!", "?")) else p + "." for p in parts)
        return rebuilt or text


# ---------------------------------------------------------------------------
# pending (the house envelope contract)
# ---------------------------------------------------------------------------


@dataclass
class PendingTranslatorBackend:
    """The default backend: produce nothing, park the envelope.

    This is not a stub standing in for missing work -- it is the shipped
    contract every other judgment-consuming subsystem in this codebase
    uses (``trialerror summarize run --judgments-file``, ``trialerror verify
    citecheck --judgments-file``, ``trialerror verify faithfulness``). The
    envelope it declines to fill is durably recorded in the job's
    checkpoint, so a live agent session (or the orchestrator's own turn,
    which costs no new booking) can fill it later without the job or its
    context being lost.
    """

    name: str = "pending"
    version: str = "1"
    requires_booking: bool = False

    def translate(self, envelope: Mapping[str, Any]) -> None:
        return None


# ---------------------------------------------------------------------------
# model-backed (seam only in this build)
# ---------------------------------------------------------------------------


@dataclass
class ModelTranslatorBackend:
    """A model-backed translator, wired for the budget law and NOT wired
    to a provider (module docstring). ``model`` comes from
    ``trialerror.toml``'s ``[feed.translator] model``, defaulting to
    :data:`DEFAULT_TRANSLATOR_MODEL`.

    :meth:`translate` raises rather than silently degrading to the pending
    behaviour: a program that configured ``backend = "model"`` asked for
    real translations, and quietly giving it empty envelopes instead would
    hide the missing driver behind a stream of "translation pending"
    cards.
    """

    model: str = DEFAULT_TRANSLATOR_MODEL
    name: str = "model"
    version: str = "1"
    requires_booking: bool = True

    def translate(self, envelope: Mapping[str, Any]) -> str:
        raise TranslatorBackendError(
            f"[feed.translator] backend = 'model' (model={self.model!r}) has no generation driver in this "
            "build -- no generative model backend ships in this repo (docs/reviews/"
            "AISPEAK_TRANSLATOR_DESIGN.md Section 3 records the gap). Use backend = 'pending' (the default) "
            "and fill the parked envelopes with 'trialerror feed translate --judgments-file ...', or "
            "backend = 'fake' for a deterministic offline rendering."
        )


def load_translator_backend(config: Mapping[str, Any] | None) -> TranslatorBackend:
    """``config`` = the program's ``trialerror.toml`` ``[feed.translator]``
    table (``None``/absent is fine). Defaults to
    :class:`PendingTranslatorBackend` -- the zero-cost, zero-dependency
    behaviour a fresh scaffold gets, matching
    :func:`trialerror.ingest.backends.load_embed_backend`'s own
    "defaults to the fake backend when unconfigured" convention.
    """
    table = dict(config or {})
    backend_name = str(table.get("backend", "pending"))
    if backend_name == "pending":
        return PendingTranslatorBackend()
    if backend_name == "fake":
        return FakeTranslatorBackend()
    if backend_name == "model":
        return ModelTranslatorBackend(model=str(table.get("model", DEFAULT_TRANSLATOR_MODEL)))
    raise TranslatorBackendError(
        f"unknown [feed.translator] backend {backend_name!r} -- expected one of 'pending', 'fake', 'model'"
    )
