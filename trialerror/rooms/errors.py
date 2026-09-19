"""Rooms-subsystem exceptions.

Mirrors ``trialerror.artifacts.errors``'s split (itself mirroring
``trialerror.stores.errors``/``trialerror.budget.errors``): a caller that only cares
"did the room operation fail" catches :class:`RoomsError`; a caller that
needs to branch on *why* catches the specific subclass. Reserved for
STRUCTURAL refusals this subsystem itself enforces on top of what
``trialerror.stores`` (FK/CHECK/XID) already catches — an illegal room-state
edge, a freeze/converge attempted from the wrong state, a convergence bar
not yet met, or a participant vetting a discussion point tied to an idea
they themselves authored — and, since the framework procedure landed, a
turn kind the procedure refuses, an incomplete final-stance record, and an
admission order that could not be produced as specified.
"""

from __future__ import annotations

__all__ = [
    "RoomsError",
    "IllegalRoomTransitionError",
    "ConvergenceBarNotMetError",
    "OwnershipConflictError",
    "TurnKindRefusedError",
    "StanceIncompleteError",
    "AdmissionOrderError",
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
    convergence"; participants must not own the ideas they vet).

    Also raised by :func:`~trialerror.rooms.api.create_room` and
    :func:`~trialerror.rooms.api.score_dp` for the same invariant read by
    LENS NAME rather than by launch id: a re-spawned lens carries a new
    launch id every turn, so the launch comparison alone never fires for
    the same lens twice, and the owning lens is resolved through the
    launch's ``attrs.lens_name``."""


class TurnKindRefusedError(RoomsError):
    """A turn named a ``kind`` outside
    :data:`~trialerror.rooms.api.TURN_KINDS`, or named ``closure`` in its
    author's own first round on the discussion point — the weak-entailment-
    first rule (a participant's opening word on a point may state a
    position or ask a question, never close the point)."""


class StanceIncompleteError(RoomsError):
    """A final-stance record did not cover every idea discussion point on
    both criteria, or a discussion point was scored from structured stances
    that no participant has filed. ``agreement_pct`` is the selection
    variable the framework is evaluated on, so it is computed from complete
    stances or not at all — a share computed over whoever happened to
    answer is a different number wearing the same name."""


class AdmissionOrderError(RoomsError):
    """:func:`~trialerror.rooms.api.build_admission_order` was asked for an
    order it cannot produce honestly — a batch size outside the charter's
    own room band with no stated override, a non-positive room size, or an
    idea carrying no id to order."""
