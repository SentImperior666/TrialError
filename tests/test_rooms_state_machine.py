"""``trialerror.rooms.state_machine`` — the room state graph, standalone (no
store needed: pure data + two pure functions). Same exhaustive-enumeration
approach ``tests/test_artifacts_state_machine.py`` uses (no ``hypothesis``
dependency declared in ``pyproject.toml``): with only 3 states, the full
3x3=9 pair matrix is enumerated directly rather than sampled.
"""

from __future__ import annotations

import itertools

import pytest

from trialerror.rooms.errors import IllegalRoomTransitionError
from trialerror.rooms.state_machine import (
    LEGAL_TRANSITIONS,
    STATES,
    TERMINAL_STATES,
    assert_legal_transition,
    is_legal_transition,
)


def test_states_are_exactly_open_converged_frozen():
    assert STATES == ("open", "converged", "frozen")


def test_converged_and_frozen_are_the_only_terminal_states():
    assert TERMINAL_STATES == {"converged", "frozen"}
    for s in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[s] == frozenset()


@pytest.mark.parametrize("from_state,to_state", [("open", "converged"), ("open", "frozen")])
def test_every_named_legal_edge_is_legal(from_state, to_state):
    assert is_legal_transition(from_state, to_state) is True
    assert_legal_transition(from_state, to_state)  # must not raise


def test_exhaustive_9_pair_matrix_agrees_with_the_legal_set():
    all_pairs = list(itertools.product(STATES, STATES))
    assert len(all_pairs) == 9

    legal_pairs = {(f, t) for f, targets in LEGAL_TRANSITIONS.items() for t in targets}
    illegal_pairs = set(all_pairs) - legal_pairs
    assert 0 < len(legal_pairs) < len(all_pairs)

    for pair in all_pairs:
        from_state, to_state = pair
        expected_legal = pair in legal_pairs
        assert is_legal_transition(from_state, to_state) is expected_legal
        if expected_legal:
            assert_legal_transition(from_state, to_state)
        else:
            with pytest.raises(IllegalRoomTransitionError):
                assert_legal_transition(from_state, to_state)

    assert illegal_pairs


def test_no_state_can_ever_transition_to_open():
    for from_state in STATES:
        assert not is_legal_transition(from_state, "open")


def test_open_has_exactly_two_legal_destinations():
    assert LEGAL_TRANSITIONS["open"] == frozenset({"converged", "frozen"})


def test_converged_cannot_reach_frozen_and_vice_versa():
    assert not is_legal_transition("converged", "frozen")
    assert not is_legal_transition("frozen", "converged")


def test_unknown_from_state_is_illegal_and_raises():
    assert is_legal_transition("bogus", "open") is False
    with pytest.raises(IllegalRoomTransitionError):
        assert_legal_transition("bogus", "open")


def test_unknown_to_state_raises_even_from_a_real_state():
    with pytest.raises(IllegalRoomTransitionError):
        assert_legal_transition("open", "bogus")


def test_a_state_can_never_transition_to_itself():
    for s in STATES:
        assert not is_legal_transition(s, s)
