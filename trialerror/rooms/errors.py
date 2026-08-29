"""Rooms-subsystem exceptions.

Mirrors ``trialerror.artifacts.errors``'s split (itself mirroring
``trialerror.stores.errors``/``trialerror.budget.errors``): a caller that only cares
"did the room operation fail" catches :class:`RoomsError`; a caller that
needs to branch on *why* catches the specific subclass. Reserved for
STRUCTURAL refusals this subsystem itself enforces on top of what
``trialerror.stores`` (FK/CHECK/XID) already catches — an illegal room-state
edge, a freeze/converge attempted from the wrong state, a convergence bar
not yet met, or a participant vetting a discussion point tied to an idea
they themselves authored.
"""

from __future__ import annotations

__all__ = [
    "RoomsError",
    "IllegalRoomTransitionError",
    "ConvergenceBarNotMetError",
    "OwnershipConflictError",
]


class RoomsError(Exception):
    """Base class for every error :mod:`trialerror.rooms` raises."""


class IllegalRoomTransitionError(RoomsError):
    """A room-state mutation (:func:`~trialerror.rooms.api.converge_room` /
    :func:`~trialerror.rooms.api.freeze_room`, or the low-level
    ``trialerror.rooms.state_machine.assert_legal_transition``) was asked to
    move a room along an edge the state machine does not allow — e.g.
    freezing an already-``converged`` room, or converging an already-
    ``frozen`` one. Design Section 9.8 / mission brief: "refusing illegal
    transitions like M10's gates"."""


class ConvergenceBarNotMetError(RoomsError):
    """:func:`~trialerror.rooms.api.converge_room` was refused because at least
    one discussion point is either unscored or below the fixed convergence
    bar (:data:`trialerror.rooms.api.CONVERGENCE_BAR_PCT`) — the room-level
    analogue of :class:`trialerror.artifacts.errors.GateEntryConditionError`."""


class OwnershipConflictError(RoomsError):
    """:func:`~trialerror.rooms.api.post_message` was refused because the
    posting launch is the same launch that authored the idea the target
    discussion point exists to vet — the NEITHER-ownership invariant
    (the origin-project requirements notes Section 1.8: "moderated multi-agent
    convergence"; participants must not own the ideas they vet)."""
