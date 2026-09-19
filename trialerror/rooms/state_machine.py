"""The room state graph, in one place — mirrors
``trialerror.artifacts.state_machine``'s shape (pure data + two pure functions,
no store needed) for the same reason: design Section 9.8 / the mission
brief's own words, "refusing illegal transitions like M10's gates".

TRIALERROR-DEV-NOTE (no DDL CHECK constraint to pin against): unlike
``gate.state`` (six values enumerated in a CHECK constraint M10's module
docstring quotes verbatim), the M1-built ``room`` DDL
(``trialerror/stores/schema/ops.py``) declares ``state TEXT NOT NULL`` with NO
CHECK constraint — room state legality is enforced ENTIRELY by this module
(and by every ``trialerror.rooms.api`` write going through it), not by SQLite
itself. Flagged for a v3 migration: a ``CHECK (state IN ('open',
'converged','frozen'))`` constraint would make this a belt-and-suspenders
invariant instead of a purely application-level one, matching every other
state-carrying table in the schema (``gate.state``, ``job.state``,
``artifact.status``, ...) — see ``trialerror/rooms/__init__.py``'s module
docstring for the full list of schema gaps this build worked around.

The graph itself (design Section 9.8 traceability row + REQUIREMENTS_
from_ute_lessons.md Section 1.8): ``open -> converged`` (all discussion
points reach the convergence bar) and ``open -> frozen`` (moderator
escalation, origin-project's "freeze-and-escalate path"). Both ``converged`` and
``frozen`` are TERMINAL in v0/v1 scope — the design names no "reopen a
frozen room" or "unconverge" mechanism, and inventing one is exactly the
kind of unstated edge ``trialerror.artifacts.state_machine``'s own TRIALERROR-DEV-NOTE
warns against manufacturing. A human who wants to retry a frozen room's
substance opens a NEW room (a fresh, auditable room_id) rather than
resurrecting the old one — the append-only room doc stays a true history.
"""

from __future__ import annotations

__all__ = [
    "STATES",
    "TERMINAL_STATES",
    "LEGAL_TRANSITIONS",
    "is_legal_transition",
    "assert_legal_transition",
]

#: The three values ``room.state`` is used with (design Section 9.8: "state
#: machine: open -> converged (all DPs >= bar) | frozen (moderator
#: escalation w/ reason)"). No DDL CHECK constraint enumerates these (see
#: module TRIALERROR-DEV-NOTE) — this tuple is the sole source of truth.
STATES: tuple[str, ...] = ("open", "converged", "frozen")

#: Both non-``open`` states are terminal in v0/v1 scope — see module
#: docstring for why reopening is deliberately not modeled.
TERMINAL_STATES: frozenset[str] = frozenset({"converged", "frozen"})

#: ``from_state -> {legal to_state, ...}``.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"converged", "frozen"}),
    "converged": frozenset(),
    "frozen": frozenset(),
}

# Every state has an entry above, and every entry's targets are themselves
# valid states — asserted once at import time so a future edit to either
# set can't silently drift (same self-check trialerror.artifacts.state_machine
# performs on its own graph).
assert set(LEGAL_TRANSITIONS) == set(STATES)
assert all(target in STATES for targets in LEGAL_TRANSITIONS.values() for target in targets)
assert TERMINAL_STATES == {s for s, targets in LEGAL_TRANSITIONS.items() if not targets}


def is_legal_transition(from_state: str, to_state: str) -> bool:
    """``True`` iff ``from_state -> to_state`` is a real edge in the graph.
    An unknown ``from_state`` is treated as illegal (returns ``False``)
    rather than raising — callers that need the loud form use
    :func:`assert_legal_transition`."""
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


def assert_legal_transition(from_state: str, to_state: str) -> None:
    """Raise :class:`~trialerror.rooms.errors.IllegalRoomTransitionError` unless
    ``from_state -> to_state`` is a legal edge, naming the exact legal
    destinations (mirrors ``trialerror.artifacts.state_machine.
    assert_legal_transition``'s message shape)."""
    from trialerror.rooms.errors import IllegalRoomTransitionError

    if from_state not in LEGAL_TRANSITIONS:
        raise IllegalRoomTransitionError(f"room: unknown from_state {from_state!r} (not one of {STATES!r})")
    if to_state not in STATES:
        raise IllegalRoomTransitionError(f"room: unknown to_state {to_state!r} (not one of {STATES!r})")
    legal = LEGAL_TRANSITIONS[from_state]
    if to_state not in legal:
        raise IllegalRoomTransitionError(
            f"room: illegal transition {from_state!r} -> {to_state!r} "
            f"(legal destinations from {from_state!r}: {sorted(legal) or 'none — terminal state'})"
        )
