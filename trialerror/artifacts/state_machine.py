"""The gate state machine's legal-transition graph, in one place. Design
Section 4.2 DDL: ``gate.state`` CHECK constraint enumerates exactly six
values (``draft|submitted|gated|union_applied|registered|failed``); the
prose ("``trialerror gate advance`` is the ONLY mutation path and refuses
illegal transitions", "the only gate state that permits registration is
``union_applied``", "Registration closes a gate; never the reverse") pins
the forward pipeline and its one terminal-pass state but does not spell out
every edge (review finding F10, non-blocking, resolved by fixing exactly
this: "name the exact accepted states and the ordering").

TRIALERROR-DEV-NOTE (the graph this module encodes, and why): the design gives
four hard facts — (1) six states exist; (2) ``union_applied`` is the ONLY
state ``artifact register`` accepts a gate in; (3) the transition INTO
``union_applied`` enforces verdict/blocking-edits/reproduction (see
:mod:`trialerror.artifacts.gates`); (4) registration is one-directional ("never
the reverse"). What is NOT stated is where ``failed`` attaches. The
faithful-closest reading used here: ``failed`` is the reject/abandon exit
available from every REVIEW-IN-PROGRESS state (``draft``, ``submitted``,
``gated``) — a human/critic can abandon a review before a verdict is ever
issued (``draft -> failed`` or ``gated -> failed``, plain
:func:`~trialerror.artifacts.gates.advance_gate`, no verdict fields touched).

OB-1 correction (C-0064 fix-tier3): a critic verdict of ``FAIL`` does
**not** land in ``gated`` for a caller to advance on afterward — this note
previously said it did, contradicting ``trialerror.artifacts.gates.
record_verdict``'s actual routing, which lands FAIL in ``failed`` directly,
``submitted -> failed``, in the SAME call that records the verdict (see
that function's own docstring: "Destination state: ``gated`` for
``PASS``/``PASS_WITH_EDITS``, ``failed`` for ``FAIL``"). Both readings were
legal under the encoded graph below (``submitted``'s legal destinations are
``{gated, failed}`` either way) — this note's WORDING was wrong, not the
graph or the code. ``failed`` and ``registered`` are BOTH terminal (no
outgoing edges) — once a gate is union_applied its only legal destination
is ``registered`` (fact 4 above forbids inventing a "failed after
union_applied" escape hatch the design never names).
"""

from __future__ import annotations

__all__ = [
    "STATES",
    "TERMINAL_PASS_STATE",
    "TERMINAL_STATES",
    "LEGAL_TRANSITIONS",
    "is_legal_transition",
    "assert_legal_transition",
]

#: Exactly the six values ``gate.state``'s CHECK constraint permits
#: (``trialerror/stores/schema/ops.py``), in pipeline order.
STATES: tuple[str, ...] = ("draft", "submitted", "gated", "union_applied", "registered", "failed")

#: Design Section 4.2, F10 resolution: "the only gate state that permits
#: registration is ``union_applied``".
TERMINAL_PASS_STATE = "union_applied"

#: States with no legal outgoing edge at all.
TERMINAL_STATES: frozenset[str] = frozenset({"registered", "failed"})

#: ``from_state -> {legal to_state, ...}``. See module TRIALERROR-DEV-NOTE for
#: the reasoning behind every edge (and every state's exclusion once here).
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"submitted", "failed"}),
    "submitted": frozenset({"gated", "failed"}),
    "gated": frozenset({"union_applied", "failed"}),
    "union_applied": frozenset({"registered"}),
    "registered": frozenset(),
    "failed": frozenset(),
}

# Every state named in the CHECK constraint has an (possibly empty) entry
# above, and every entry's targets are themselves valid states — asserted
# once at import time so a future edit to either set can't silently drift.
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
    """Raise :class:`~trialerror.artifacts.errors.IllegalTransitionError` unless
    ``from_state -> to_state`` is a legal edge. Separated from
    :func:`is_legal_transition` so the one call site that needs to refuse
    (``trialerror.artifacts.gates``) gets a message naming the exact legal
    destinations, without every caller re-deriving that message."""
    from trialerror.artifacts.errors import IllegalTransitionError

    if from_state not in LEGAL_TRANSITIONS:
        raise IllegalTransitionError(f"gate: unknown from_state {from_state!r} (not one of {STATES!r})")
    if to_state not in STATES:
        raise IllegalTransitionError(f"gate: unknown to_state {to_state!r} (not one of {STATES!r})")
    legal = LEGAL_TRANSITIONS[from_state]
    if to_state not in legal:
        raise IllegalTransitionError(
            f"gate: illegal transition {from_state!r} -> {to_state!r} "
            f"(legal destinations from {from_state!r}: {sorted(legal) or 'none — terminal state'})"
        )
