"""``trialerror.artifacts.state_machine`` — the gate state graph, standalone
(no store needed: pure data + two pure functions).

Design Section 12, M10 row acceptance criterion: "illegal transition
refused (property-based test over state machine)". No ``hypothesis``
dependency is declared in ``pyproject.toml``, so the property is checked
the exhaustive way — every one of the 6x6=36 ``(from_state, to_state)``
pairs is asserted against the SAME predicate two independent ways
(``is_legal_transition`` and ``assert_legal_transition`` must always
agree), which is a full enumeration of the property "every illegal edge is
refused, every legal edge is not" rather than a sampled one.
"""

from __future__ import annotations

import itertools

import pytest

from trialerror.artifacts.errors import IllegalTransitionError
from trialerror.artifacts.state_machine import (
    LEGAL_TRANSITIONS,
    STATES,
    TERMINAL_PASS_STATE,
    TERMINAL_STATES,
    assert_legal_transition,
    is_legal_transition,
)


def test_states_are_exactly_the_six_ddl_values():
    assert STATES == ("draft", "submitted", "gated", "union_applied", "registered", "failed")


def test_terminal_pass_state_is_union_applied_only():
    """F10 resolution, verbatim: "the only gate state that permits
    registration is union_applied"."""
    assert TERMINAL_PASS_STATE == "union_applied"
    assert sum(1 for s in STATES if s == TERMINAL_PASS_STATE) == 1


def test_registered_and_failed_are_the_only_terminal_states():
    assert TERMINAL_STATES == {"registered", "failed"}
    for s in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[s] == frozenset()


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("draft", "submitted"),
        ("draft", "failed"),
        ("submitted", "gated"),
        ("submitted", "failed"),
        ("gated", "union_applied"),
        ("gated", "failed"),
        ("union_applied", "registered"),
    ],
)
def test_every_named_legal_edge_is_legal(from_state, to_state):
    assert is_legal_transition(from_state, to_state) is True
    assert_legal_transition(from_state, to_state)  # must not raise


def test_exhaustive_36_pair_matrix_agrees_with_the_legal_set():
    """The full property, enumerated: for EVERY (from_state, to_state) pair
    drawn from the 6 known states, ``is_legal_transition`` says True iff the
    pair is a real edge in ``LEGAL_TRANSITIONS``, and
    ``assert_legal_transition`` raises iff it says False — the two entry
    points can never disagree with each other or with the graph itself."""
    all_pairs = list(itertools.product(STATES, STATES))
    assert len(all_pairs) == 36

    legal_pairs = {(f, t) for f, targets in LEGAL_TRANSITIONS.items() for t in targets}
    illegal_pairs = set(all_pairs) - legal_pairs
    # Sanity: the graph is a proper forward pipeline, not a free-for-all —
    # strictly fewer legal edges than illegal ones out of all 36.
    assert 0 < len(legal_pairs) < len(all_pairs)

    for pair in all_pairs:
        from_state, to_state = pair
        expected_legal = pair in legal_pairs
        assert is_legal_transition(from_state, to_state) is expected_legal
        if expected_legal:
            assert_legal_transition(from_state, to_state)  # must not raise
        else:
            with pytest.raises(IllegalTransitionError):
                assert_legal_transition(from_state, to_state)

    assert illegal_pairs  # the refusal side of the property is non-vacuous


def test_no_state_can_ever_transition_to_draft():
    """draft is only ever the OPENING state (trialerror.artifacts.gates.open_gate
    inserts it directly) — nothing in the graph transitions INTO it."""
    for from_state in STATES:
        assert not is_legal_transition(from_state, "draft")


def test_union_applied_has_exactly_one_legal_destination():
    assert LEGAL_TRANSITIONS["union_applied"] == frozenset({"registered"})


def test_unknown_from_state_is_illegal_and_raises():
    assert is_legal_transition("bogus", "draft") is False
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition("bogus", "draft")


def test_unknown_to_state_raises_even_from_a_real_state():
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition("draft", "bogus")


def test_a_state_can_never_transition_to_itself():
    for s in STATES:
        assert not is_legal_transition(s, s)
