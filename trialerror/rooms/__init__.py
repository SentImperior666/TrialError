"""``trialerror.rooms`` — the brainstorm-rooms runtime. Design Section 2
(subsystem table, row L, as amended by the build brief that assigned this
lane): "rooms (schema v0, runtime v1)." Design Section 9.8 (traceability
map): "schema in v0 ... runtime skill in v1; >=90% agreement bar and
freeze-and-escalate encoded as room_score transitions; room size prior 2-3
(MN-033) as config default." Design Section 11 names the rooms runtime as
one of exactly two v0 candidates deliberately deferred to v1 (the schema
shipped in M1; this package is the deferred runtime landing against it).

Mechanizes the origin-project requirements notes Section 1.8's origin-project mechanism:
moderated multi-agent convergence in an append-only room doc, a fixed >=90%
per-discussion-point agreement bar, a freeze-and-escalate path, and a
theory-doc (+ plain-terms companion) deliverable — see
``trialerror/rooms/api.py``'s own module docstring for the full schema-gap
TRIALERROR-DEV-NOTE (five items, all v3-migration candidates) this build worked
around without touching ``trialerror/stores/schema/`` (the concurrent schemav2
lane's file).

Public surface
--------------
State machine (:mod:`trialerror.rooms.state_machine`):

- :data:`~trialerror.rooms.state_machine.STATES` (``open``/``converged``/
  ``frozen``), :data:`~trialerror.rooms.state_machine.LEGAL_TRANSITIONS`,
  :data:`~trialerror.rooms.state_machine.TERMINAL_STATES` — the graph itself,
  importable standalone (e.g. for an exhaustive test over every
  ``(from_state, to_state)`` pair, mirroring ``trialerror.artifacts.
  state_machine``'s own test).
- :func:`~trialerror.rooms.state_machine.is_legal_transition` /
  :func:`~trialerror.rooms.state_machine.assert_legal_transition`.

Runtime (:mod:`trialerror.rooms.api`):

- :func:`~trialerror.rooms.api.create_room` — open a room with its discussion
  points + hyperparameters (2-3 participants default, 2 rounds/dp default,
  a FIXED 90% convergence bar).
- :func:`~trialerror.rooms.api.post_message` — append one turn (launch-backed
  authorship, no ``author`` parameter — the same server-side derivation
  contract ``trialerror.events.post_feed`` uses); refuses under the NEITHER-
  ownership invariant when a discussion point names the idea it vets.
- :func:`~trialerror.rooms.api.build_participant_turn_envelope` /
  :func:`~trialerror.rooms.api.build_moderator_scoring_envelope` — plain-dict
  request envelopes (the ``trialerror.verify.hypothesis`` judgment-envelope
  pattern; this package never calls an LLM itself).
- :func:`~trialerror.rooms.api.score_dp` — records a moderator's judgment
  (injected ``judge`` callable, exactly like
  :func:`~trialerror.verify.hypothesis.run_hypothesis_verification`) into
  ``room_score``.
- :func:`~trialerror.rooms.api.check_room_converged` /
  :func:`~trialerror.rooms.api.converge_room` /
  :func:`~trialerror.rooms.api.freeze_room` — the state machine's two real
  transitions, entry-condition-checked (mirrors ``trialerror.artifacts.gates``'s
  "refuses illegal transitions" contract).
- :func:`~trialerror.rooms.api.register_room_deliverable` — wires a converged
  room to its theory-doc artifact via
  :func:`~trialerror.artifacts.registry.create_artifact`.
- :func:`~trialerror.rooms.api.render_room_markdown` /
  :func:`~trialerror.rooms.api.export_room` — the rendered append-only "room
  doc" view (atomic write).

``trialerror/rooms/checks.py`` registers this module's ``trialerror doctor`` checks
(``rooms_stuck``, ``rooms_unregistered_deliverables``) by the same
auto-discovery convention every other subsystem uses.
"""

from __future__ import annotations

from trialerror.rooms.api import (
    CONVERGENCE_BAR_PCT,
    DEFAULT_ROUNDS_PER_DP,
    PARTICIPANT_RANGE,
    build_moderator_scoring_envelope,
    build_participant_turn_envelope,
    check_room_converged,
    converge_room,
    create_room,
    export_room,
    freeze_room,
    get_discussion_points,
    get_dp_score,
    get_freeze_reason,
    get_room,
    list_room_turns,
    post_message,
    register_room_deliverable,
    render_room_markdown,
    score_dp,
)
from trialerror.rooms.errors import (
    ConvergenceBarNotMetError,
    IllegalRoomTransitionError,
    OwnershipConflictError,
    RoomsError,
)
from trialerror.rooms.state_machine import (
    LEGAL_TRANSITIONS,
    STATES,
    TERMINAL_STATES,
    assert_legal_transition,
    is_legal_transition,
)

__all__ = [
    "RoomsError",
    "IllegalRoomTransitionError",
    "ConvergenceBarNotMetError",
    "OwnershipConflictError",
    "STATES",
    "TERMINAL_STATES",
    "LEGAL_TRANSITIONS",
    "is_legal_transition",
    "assert_legal_transition",
    "CONVERGENCE_BAR_PCT",
    "DEFAULT_ROUNDS_PER_DP",
    "PARTICIPANT_RANGE",
    "create_room",
    "get_room",
    "get_discussion_points",
    "list_room_turns",
    "get_dp_score",
    "post_message",
    "build_participant_turn_envelope",
    "build_moderator_scoring_envelope",
    "score_dp",
    "check_room_converged",
    "converge_room",
    "freeze_room",
    "get_freeze_reason",
    "register_room_deliverable",
    "render_room_markdown",
    "export_room",
]
