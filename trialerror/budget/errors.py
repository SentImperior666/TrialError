"""Budget-subsystem exceptions.

Mirrors ``trialerror.stores.errors``'s split: a caller that only cares "did the
budget operation fail" catches :class:`BudgetError`; a caller that needs to
branch on *why* catches the specific subclass. These are reserved for
STRUCTURAL refusals (no open session, an override ruling id that doesn't
exist) - the ordinary "can't afford it" outcomes (over-cap, DEFERRED) are
NOT exceptions, they are first-class states on the returned result object
(design Section 5.1 cross-cutting rule: "errors returned as structured
content ... never exceptions" - applied here to the budget API itself, not
just the MCP/CLI surfaces wrapping it).
"""

from __future__ import annotations

__all__ = [
    "BudgetError",
    "NoOpenSessionError",
    "LaunchNotOwnedError",
    "ModelPolicyViolationError",
    "UnknownOverrideRulingError",
    "UnknownAssignmentError",
]


class BudgetError(Exception):
    """Base class for every error :mod:`trialerror.budget` raises."""


class NoOpenSessionError(BudgetError):
    """``book_launch`` was called for a ``session_id`` that is not OPEN in
    the program's ops.db (design Section 4.3 binding rule / review F13:
    "book_launch refuses unless the calling session is OPEN")."""


class LaunchNotOwnedError(BudgetError):
    """A verb that speaks FOR a launch (``budget heartbeat``) was called
    while the program's OPEN session is not the session that booked it. The
    same structural family as :class:`NoOpenSessionError` -- a claim about a
    booking may only come from the session holding it -- kept separate so a
    caller can say which of the two happened."""


class ModelPolicyViolationError(BudgetError):
    """A purpose's configured minimum model class (``trialerror.toml [models]``)
    was not met by the requested ``model_class``, and no valid override
    ruling id was supplied (design Section 5.4: "book_launch refuses a
    top-tier-required purpose on a cheap model unless the booking cites an
    override ruling id")."""


class UnknownOverrideRulingError(BudgetError):
    """An ``override_ruling_id`` was supplied but does not name an existing
    row in ``ops.ruling`` - an override must cite a real ruling, not an
    arbitrary string."""


class UnknownAssignmentError(BudgetError):
    """``book_launch`` was given an ``assign_id`` that names no row in
    ``ops.lens_assignment``.

    Refused rather than linked to nothing: the whole point of the link is
    that the retrieval scope and the citation audit can resolve this
    launch's slice from its assignment rows, and a booking that silently
    linked none would read as a lens launch that declares no slice -- which
    is the shape both of those treat as "unrestricted" and "not a lens",
    i.e. the barrier off."""
