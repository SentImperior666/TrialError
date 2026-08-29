"""``trialerror.rooms`` acceptance criteria, gathered in one place — mirrors
``tests/test_m9_acceptance.py``'s/``tests/test_m10_acceptance.py``'s
convention: this file IS the acceptance-criteria mapping for the v1-rooms
build brief's own test list, each test here exercising the FULL, realistic
path (not a narrower unit already covered in ``tests/test_rooms_api.py``,
though it necessarily re-touches the same functions).

    | Build-brief acceptance item                                                            | Test |
    |------------------------------------------------------------------------------------------|------|
    | full lifecycle fixture (create -> turns -> scoring -> converged -> deliverable hook)      | test_full_lifecycle_create_turns_scoring_converged_deliverable |
    | freeze path                                                                               | test_freeze_path_escalates_with_reason_and_blocks_further_turns |
    | NEITHER-ownership invariant (enforced at the application level against ``dps`` JSON's own ``idea_id`` convention -- unchanged by ops-v3's ``room_link`` table, a queryable audit mirror, not the enforcement source) | test_neither_ownership_invariant_blocks_self_vetting_and_allows_independent_review |
"""

from __future__ import annotations

import pytest

from trialerror.rooms.api import (
    build_moderator_scoring_envelope,
    build_participant_turn_envelope,
    check_room_converged,
    converge_room,
    create_room,
    freeze_room,
    get_freeze_reason,
    list_room_turns,
    post_message,
    register_room_deliverable,
    score_dp,
)
from trialerror.rooms.errors import OwnershipConflictError

from tests._rooms_fixtures import bootstrap_launch, seed_idea, seed_template

pytestmark = pytest.mark.acceptance


# ---------------------------------------------------------------------------
# criterion 1: full lifecycle — create -> turns -> scoring -> converged ->
# deliverable hook
# ---------------------------------------------------------------------------


def test_full_lifecycle_create_turns_scoring_converged_deliverable(store):
    moderator = bootstrap_launch(store, agent_kind="moderator")
    lens_a = bootstrap_launch(store, agent_kind="lens_a")
    lens_b = bootstrap_launch(store, agent_kind="lens_b")
    seed_template(store)

    # 1. create — a real 2-discussion-point room, MN-033 2-participant default.
    room = create_room(
        store,
        topic="does the room mechanism itself generalize past origin-project?",
        discussion_points=[
            {"prompt": "does the >=90% bar produce real convergence, not rubber-stamping?"},
            {"prompt": "does the freeze-and-escalate path actually get exercised in practice?"},
        ],
        participants=["lens_a", "lens_b"],
        by_launch=moderator,
    )
    assert room["state"] == "open"

    # 2. turns — both lenses post to both discussion points, reading the
    # participant envelope before writing (the real usage shape: envelope
    # -> externally-generated body -> post_message).
    for dp_id in ("DP1", "DP2"):
        for launch, stance in ((lens_a, "yes, with caveats"), (lens_b, "yes")):
            envelope = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id=dp_id)
            assert envelope["dp_id"] == dp_id
            post_message(store, room_id=room["room_id"], launch_id=launch, dp_id=dp_id, body=f"{launch}: {stance}")

    turns = list_room_turns(store, room_id=room["room_id"])
    assert len(turns) == 4
    assert [t["seq"] for t in turns] == [1, 2, 3, 4]  # one monotonic sequence, not per-dp

    # 3. scoring — moderator reads the scoring envelope, judges, records.
    for dp_id in ("DP1", "DP2"):
        envelope = build_moderator_scoring_envelope(store, room_id=room["room_id"], dp_id=dp_id)
        assert len(envelope["turns"]) == 2
        score_dp(store, room_id=room["room_id"], dp_id=dp_id, judge=lambda env: 94.0, by_launch=moderator)

    status = check_room_converged(store, room["room_id"])
    assert status["all_converged"] is True

    # 4. converged.
    converged_room = converge_room(store, room_id=room["room_id"], by_launch=moderator)
    assert converged_room["state"] == "converged"

    # 5. deliverable hook — the converged room owes its theory-doc artifact.
    artifact = register_room_deliverable(
        store, room_id=room["room_id"], type_key="room_theory_doc",
        title="does the room mechanism itself generalize past origin-project? — theory doc",
        path="artifacts/room-mechanism-theory.md", sha256="a" * 64, by_launch=moderator,
    )
    assert artifact["status"] == "draft"
    assert artifact["type"] == "room_theory_doc"


# ---------------------------------------------------------------------------
# criterion 2: freeze path
# ---------------------------------------------------------------------------


def test_freeze_path_escalates_with_reason_and_blocks_further_turns(store):
    moderator = bootstrap_launch(store, agent_kind="moderator")
    lens_a = bootstrap_launch(store, agent_kind="lens_a")

    room = create_room(
        store, topic="a genuinely deadlocked discussion point",
        discussion_points=[{"prompt": "irreconcilable framing dispute"}],
        participants=["lens_a", "lens_b"], by_launch=moderator,
    )
    post_message(store, room_id=room["room_id"], launch_id=lens_a, dp_id="DP1", body="position A")
    # two rounds of scoring, neither reaching the bar -- the origin-project freeze
    # trigger ("stuck after N rounds"), enacted here as the moderator's own
    # judgment call rather than an automatic round-counter (the room's
    # config carries rounds_per_dp for a caller to check against, but
    # freeze_room itself is not auto-triggered -- a human/moderator decides).
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 30.0, by_launch=moderator)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 35.0, by_launch=moderator)
    status = check_room_converged(store, room["room_id"])
    assert status["all_converged"] is False

    frozen_room = freeze_room(
        store, room_id=room["room_id"], by_launch=moderator,
        reason="DP1 stuck at ~30-35% agreement after 2 rounds; framing dispute needs human resolution",
    )
    assert frozen_room["state"] == "frozen"
    assert "framing dispute" in get_freeze_reason(store, room["room_id"])

    # frozen is terminal — no further turns, and converge_room's own state
    # graph refuses converging a frozen room (see test_rooms_api.py's
    # dedicated illegal-transition coverage for the state-machine level).
    with pytest.raises(ValueError, match="is not open"):
        post_message(store, room_id=room["room_id"], launch_id=lens_a, dp_id="DP1", body="too late")


# ---------------------------------------------------------------------------
# criterion 3: NEITHER-ownership invariant
# ---------------------------------------------------------------------------


def test_neither_ownership_invariant_blocks_self_vetting_and_allows_independent_review(store):
    """The invariant is enforced at the APPLICATION level, keyed off an
    OPTIONAL ``idea_id`` this module's own ``dps`` JSON convention allows a
    discussion point to carry (module TRIALERROR-DEV-NOTE item 1). ops-v3 (item 4)
    later added ``room_link`` as a real, queryable mirror of that same
    ``idea_id`` -- ``_check_neither_ownership`` itself still reads the
    ``dps`` JSON, unchanged, so this test's enforcement path is unaffected."""
    idea_author = bootstrap_launch(store, agent_kind="lens_a")
    independent_reviewer = bootstrap_launch(store, agent_kind="lens_b")
    moderator = bootstrap_launch(store, agent_kind="moderator")
    idea_id = seed_idea(store, author_launch=idea_author, body="a genuinely novel proposal")

    room = create_room(
        store, topic="vet lens_a's proposal",
        discussion_points=[{"dp_id": "DP-vet", "prompt": "is the proposal sound?", "idea_id": idea_id}],
        participants=["lens_a", "lens_b"], by_launch=moderator,
    )

    with pytest.raises(OwnershipConflictError):
        post_message(store, room_id=room["room_id"], launch_id=idea_author, dp_id="DP-vet", body="my own idea is great")

    turn = post_message(
        store, room_id=room["room_id"], launch_id=independent_reviewer, dp_id="DP-vet", body="reviewed independently: sound"
    )
    assert turn["author_launch"] == independent_reviewer
