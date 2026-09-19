"""The framework procedure inside ``trialerror.rooms.api``: blind first
turn, turn kinds, rank-all + final stances, computed ``agreement_pct`` with
the label first, the neutral extract pass, the buster's carried position,
NEITHER ownership by lens name, and the seeded (arm, card) admission order.

One fixture round runs through the whole procedure in
``test_a_full_fixture_round_runs_the_whole_procedure``; the rest of the
module takes each rule apart on its own, green and red.
"""

from __future__ import annotations

import json

import pytest

from trialerror.rooms.api import (
    ADMISSION_BATCH_ROOM_RANGE,
    CONVERGENCE_BAR_PCT,
    DP_LABELS,
    RANK_ALL_DP_ID,
    ROUND_ONE_TURN_KINDS,
    TURN_KINDS,
    admission_order_hash,
    build_admission_order,
    build_extract_envelope,
    build_moderator_scoring_envelope,
    build_participant_turn_envelope,
    build_rank_all_envelope,
    check_room_converged,
    compute_agreement_pct,
    consolidated_ideas_for_admission,
    converge_room,
    create_room,
    freeze_room,
    get_buster_position,
    get_dp_label,
    get_dp_score,
    lens_name_of_launch,
    list_final_stances,
    list_room_turns,
    list_turn_extracts,
    post_final_stance,
    post_message,
    record_buster_position,
    record_turn_extracts,
    render_room_markdown,
    score_dp,
    turn_kinds,
)
from trialerror.rooms.errors import (
    AdmissionOrderError,
    OwnershipConflictError,
    StanceIncompleteError,
    TurnKindRefusedError,
)
from trialerror.stores.writer import insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._rooms_fixtures import bootstrap_launch, seed_idea

# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _lens_launch(store, *, lens_name: str) -> str:
    """A launch booked the way ``lens export`` books one: with its lens name
    in ``attrs``. That attr is the whole seam the by-name NEITHER check
    reads, so a fixture that left it out would test the launch-id half
    twice."""
    launch_id = bootstrap_launch(store, agent_kind="lens")
    update(
        store, "launch", pk_column="launch_id", pk_value=launch_id,
        changes={"attrs": json.dumps({"lens_name": lens_name, "round_id": "ROUND-fix"})},
    )
    return launch_id


def _idea(store, *, author_launch: str, round_id: str = "ROUND-fix", tier: str = "near", card: str = "MISMATCH") -> str:
    idea_id = new_id("IDEA")
    insert(
        store,
        "idea",
        {
            "idea_id": idea_id, "round_id": round_id, "author_launch": author_launch,
            "body": "a state-transition sketch", "status": "consolidated", "created_ts": now(),
            "tier": tier, "recipe_card": card, "home": "family/cell",
        },
    )
    return idea_id


def _two_point_room(store, *, participants=("lens_a", "lens_b"), **kwargs):
    """A room over two ideas authored by a THIRD lens, so both participants
    are legitimately seated."""
    author = _lens_launch(store, lens_name="lens_author")
    dps = [
        {"dp_id": "DP1", "prompt": "does the mechanism generalize?", "idea_id": _idea(store, author_launch=author)},
        {"dp_id": "DP2", "prompt": "does it survive a hostile edit?", "idea_id": _idea(store, author_launch=author)},
    ]
    return create_room(store, topic="fixture room", discussion_points=dps, participants=list(participants), **kwargs)


def _full_stance(value_a="yes", value_b="yes"):
    return {
        "ranking": {"a": ["DP1", "DP2"], "b": ["DP2", "DP1"]},
        "stances": {"DP1": {"a": value_a, "b": value_b}, "DP2": {"a": value_a, "b": value_b}},
    }


# ---------------------------------------------------------------------------
# the whole procedure, once, end to end
# ---------------------------------------------------------------------------


def test_a_full_fixture_round_runs_the_whole_procedure(store):
    room = _two_point_room(store, blind_first_turn=True, rank_all=True, buster="lens_b")
    room_id = room["room_id"]
    a1, b1 = _lens_launch(store, lens_name="lens_a"), _lens_launch(store, lens_name="lens_b")
    moderator = bootstrap_launch(store, agent_kind="moderator")

    record_buster_position(store, room_id=room_id, participant="lens_b", text="ASSUME NOT: the economy is one.", by_launch=b1)

    # round 1: blind, position/question only
    env_a = build_participant_turn_envelope(store, room_id=room_id, dp_id="DP1", participant="lens_a")
    assert env_a["blinded"] is True and env_a["allowed_turn_kinds"] == ["position", "question"]
    post_message(store, room_id=room_id, launch_id=a1, dp_id="DP1", body="it generalizes", kind="position")
    env_b = build_participant_turn_envelope(store, room_id=room_id, dp_id="DP1", participant="lens_b")
    assert env_b["blinded"] is True and env_b["prior_turns"] == []
    assert env_b["buster_position"]["recorded_position"]["text"] == "ASSUME NOT: the economy is one."
    post_message(store, room_id=room_id, launch_id=b1, dp_id="DP1", body="only if bookkeeping holds", kind="question")

    # round 2: prior turns visible, closure admitted
    env_a2 = build_participant_turn_envelope(store, room_id=room_id, dp_id="DP1", participant="lens_a")
    assert env_a2["round_number"] == 2 and env_a2["blinded"] is False and len(env_a2["prior_turns"]) == 2
    assert "closure" in env_a2["allowed_turn_kinds"]
    a2 = _lens_launch(store, lens_name="lens_a")
    post_message(store, room_id=room_id, launch_id=a2, dp_id="DP1", body="settled", kind="closure")

    # DP2 gets its round 1 too, so both points are real
    for launch, kind in ((a1, "position"), (b1, "position")):
        post_message(store, room_id=room_id, launch_id=launch, dp_id="DP2", body="holds", kind=kind)

    # the neutral extract pass, then scoring on extracts and stances
    extract_env = build_extract_envelope(store, room_id=room_id, dp_id="DP1")
    assert [t["seq"] for t in extract_env["turns"]] == [1, 2, 3]
    assert all("author_launch" not in t for t in extract_env["turns"])
    record_turn_extracts(
        store, room_id=room_id, dp_id="DP1", by_launch=moderator,
        extracts=[
            {"seq": s, "claim": f"claim {s}", "anchors": ["DOC-1"], "stance_a": "yes", "stance_b": "yes",
             "residual_disagreement": "none"}
            for s in (1, 2, 3)
        ],
    )
    record_turn_extracts(
        store, room_id=room_id, dp_id="DP2", by_launch=moderator,
        extracts=[
            {"seq": s, "claim": f"claim {s}", "anchors": ["DOC-1"], "stance_a": "yes", "stance_b": "yes",
             "residual_disagreement": "none"}
            for s in (4, 5)
        ],
    )

    rank_env = build_rank_all_envelope(store, room_id=room_id, participant="lens_a")
    assert [p["dp_id"] for p in rank_env["points"]] == ["DP1", "DP2"]
    post_final_stance(store, room_id=room_id, launch_id=a1, participant="lens_a", **_full_stance())
    post_final_stance(store, room_id=room_id, launch_id=b1, participant="lens_b", **_full_stance())

    for dp_id in ("DP1", "DP2"):
        row = score_dp(
            store, room_id=room_id, dp_id=dp_id, by_launch=moderator, require_extracts=True,
            judge=lambda env: {"label": "MEETS-BOTH", "note": "unanimous"},
        )
        assert list(row)[0] == "label" and row["label"] == "MEETS-BOTH"
        assert row["agreement_pct"] == 100.0 and row["agreement_from"] == "structured_stances"
        assert row["converged"] is True

    status = check_room_converged(store, room_id)
    assert [d["dp_id"] for d in status["per_dp"]] == ["DP1", "DP2"]  # the rank-all point is not scored
    assert status["all_converged"] is True and status["rank_all"]["all_filed"] is True
    assert converge_room(store, room_id=room_id, by_launch=moderator)["state"] == "converged"
    assert get_dp_label(store, room_id=room_id, dp_id="DP1") == "MEETS-BOTH"

    doc = render_room_markdown(store, room_id)
    assert "**MEETS-BOTH**" in doc and "(closure)" in doc and "procedural point" in doc


# ---------------------------------------------------------------------------
# blind first turn
# ---------------------------------------------------------------------------


def test_blind_first_turn_withholds_round_one_and_says_so(store):
    room = _two_point_room(store, blind_first_turn=True)
    a1 = _lens_launch(store, lens_name="lens_a")
    post_message(store, room_id=room["room_id"], launch_id=a1, dp_id="DP1", body="first")
    env = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env["prior_turns"] == []
    assert env["blinded"] is True
    assert "carries no prior turns" in env["instructions"]


def test_without_the_flag_round_one_sees_the_prior_turn(store):
    room = _two_point_room(store)
    a1 = _lens_launch(store, lens_name="lens_a")
    post_message(store, room_id=room["room_id"], launch_id=a1, dp_id="DP1", body="first")
    env = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert [t["body"] for t in env["prior_turns"]] == ["first"]
    assert env["blinded"] is False


def test_the_blind_lifts_once_round_one_is_complete(store):
    room = _two_point_room(store, blind_first_turn=True)
    for name in ("lens_a", "lens_b"):
        post_message(store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name=name), dp_id="DP1", body=name)
    env = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env["round_number"] == 2
    assert [t["body"] for t in env["prior_turns"]] == ["lens_a", "lens_b"]
    assert env["blinded"] is False


def test_one_seat_posting_twice_does_not_lift_the_other_seats_blind(store):
    """The blind is decided per PARTICIPANT, not off the room's turn count.

    Two seats, and ``lens_a`` posts twice on the point before ``lens_b``
    writes at all: the turn count has reached 2 of 2, but ``lens_b`` has yet
    to open. Deciding blindness off the count handed ``lens_b`` both of
    ``lens_a``'s bodies verbatim on what is still its own first turn -- the
    barrier failing open in the one direction it exists to prevent.
    """
    room = _two_point_room(store, blind_first_turn=True)
    room_id = room["room_id"]
    for body in ("first", "again"):
        post_message(
            store, room_id=room_id, launch_id=_lens_launch(store, lens_name="lens_a"),
            dp_id="DP1", body=body, kind="position",
        )
    env_b = build_participant_turn_envelope(store, room_id=room_id, dp_id="DP1", participant="lens_b")
    assert env_b["blinded"] is True
    assert env_b["prior_turns"] == []
    assert env_b["writing_first_round"] is True
    assert env_b["allowed_turn_kinds"] == list(ROUND_ONE_TURN_KINDS)
    # And the point itself still reports how far it has got.
    assert env_b["round_number"] == 2 and env_b["turn_index"] == 3

    # The seat that HAS spoken is past its own round 1, on the same record.
    env_a = build_participant_turn_envelope(store, room_id=room_id, dp_id="DP1", participant="lens_a")
    assert env_a["blinded"] is False
    assert [t["body"] for t in env_a["prior_turns"]] == ["first", "again"]
    assert "closure" in env_a["allowed_turn_kinds"]


def test_with_no_seat_named_the_blind_holds_while_any_seat_still_owes_a_turn(store):
    """Asked about the POINT rather than a seat, round 1 is still running
    while a declared seat has not posted -- so a caller that does not know
    which seat it is building for cannot be handed the turns either."""
    room = _two_point_room(store, blind_first_turn=True)
    for body in ("first", "again"):
        post_message(
            store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"),
            dp_id="DP1", body=body,
        )
    env = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env["blinded"] is True and env["prior_turns"] == []


def test_the_envelope_offers_closure_only_once_this_seat_has_spoken(store):
    """``allowed_turn_kinds`` reads the same way ``post_message`` refuses: a
    seat that has not opened the point is offered position/question, and
    ``post_message`` would refuse the closure the old count-based reading
    advertised."""
    room = _two_point_room(store)
    room_id = room["room_id"]
    for body in ("first", "again"):
        post_message(store, room_id=room_id, launch_id=_lens_launch(store, lens_name="lens_a"), dp_id="DP1", body=body)
    env_b = build_participant_turn_envelope(store, room_id=room_id, dp_id="DP1", participant="lens_b")
    assert env_b["allowed_turn_kinds"] == list(ROUND_ONE_TURN_KINDS)
    b1 = _lens_launch(store, lens_name="lens_b")
    with pytest.raises(TurnKindRefusedError, match="refused in round 1"):
        post_message(store, room_id=room_id, launch_id=b1, dp_id="DP1", body="settled", kind="closure")


# ---------------------------------------------------------------------------
# turn kinds
# ---------------------------------------------------------------------------


def test_a_turn_records_its_kind_on_the_companion_event(store):
    room = _two_point_room(store)
    a1 = _lens_launch(store, lens_name="lens_a")
    row = post_message(store, room_id=room["room_id"], launch_id=a1, dp_id="DP1", body="q?", kind="question")
    assert row["kind"] == "question"
    assert turn_kinds(store, room_id=room["room_id"]) == {1: "question"}


def test_an_unknown_turn_kind_is_refused(store):
    room = _two_point_room(store)
    a1 = _lens_launch(store, lens_name="lens_a")
    with pytest.raises(TurnKindRefusedError, match="kind must be one of"):
        post_message(store, room_id=room["room_id"], launch_id=a1, dp_id="DP1", body="x", kind="verdict")
    assert turn_kinds(store, room_id=room["room_id"]) == {}


def test_closure_is_refused_in_the_authors_own_first_round(store):
    room = _two_point_room(store)
    a1 = _lens_launch(store, lens_name="lens_a")
    with pytest.raises(TurnKindRefusedError, match="refused in round 1"):
        post_message(store, room_id=room["room_id"], launch_id=a1, dp_id="DP1", body="done", kind="closure")


def test_closure_is_admitted_from_round_two_under_a_new_launch(store):
    """A re-spawned lens posts under a NEW launch id, which is exactly why
    "its own first round" is counted by lens name."""
    room = _two_point_room(store)
    post_message(store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"), dp_id="DP1", body="p")
    row = post_message(
        store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"), dp_id="DP1",
        body="settled", kind="closure",
    )
    assert row["kind"] == "closure"


def test_another_seats_first_turn_is_still_its_own_round_one(store):
    room = _two_point_room(store)
    post_message(store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"), dp_id="DP1", body="p")
    with pytest.raises(TurnKindRefusedError, match="refused in round 1"):
        post_message(
            store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_b"), dp_id="DP1",
            body="agreed, done", kind="closure",
        )


def test_a_turn_written_before_kinds_existed_reads_as_a_position(store):
    room = _two_point_room(store)
    a1 = _lens_launch(store, lens_name="lens_a")
    post_message(store, room_id=room["room_id"], launch_id=a1, dp_id="DP1", body="x")
    store.ops.execute(
        "UPDATE event SET payload = json_remove(payload, '$.kind') WHERE type = 'room_turn'"
    )
    assert turn_kinds(store, room_id=room["room_id"]) == {1: "position"}


# ---------------------------------------------------------------------------
# rank-all + final stances
# ---------------------------------------------------------------------------


def test_rank_all_appends_one_procedural_point_that_is_never_scored(store):
    room = _two_point_room(store, rank_all=True)
    config = json.loads(room["dps"])
    assert [d["dp_id"] for d in config["discussion_points"]] == ["DP1", "DP2", RANK_ALL_DP_ID]
    assert config["rank_all_dp_id"] == RANK_ALL_DP_ID
    with pytest.raises(ValueError, match="never scored"):
        score_dp(
            store, room_id=room["room_id"], dp_id=RANK_ALL_DP_ID,
            judge=lambda env: 95.0, by_launch=bootstrap_launch(store),
        )


def test_a_stance_record_needs_the_rank_all_point(store):
    room = _two_point_room(store)
    with pytest.raises(ValueError, match="no rank-all discussion point"):
        post_final_stance(
            store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"),
            participant="lens_a", **_full_stance(),
        )


def test_an_incomplete_stance_record_is_refused_whole(store):
    room = _two_point_room(store, rank_all=True)
    a1 = _lens_launch(store, lens_name="lens_a")
    with pytest.raises(StanceIncompleteError, match="missing"):
        post_final_stance(
            store, room_id=room["room_id"], launch_id=a1, participant="lens_a",
            ranking={"a": ["DP1", "DP2"], "b": ["DP1", "DP2"]},
            stances={"DP1": {"a": "yes", "b": "yes"}},  # DP2 absent
        )
    assert list_final_stances(store, room_id=room["room_id"]) == {}


def test_a_stance_value_outside_the_vocabulary_is_refused(store):
    room = _two_point_room(store, rank_all=True)
    a1 = _lens_launch(store, lens_name="lens_a")
    with pytest.raises(StanceIncompleteError, match="outside"):
        post_final_stance(
            store, room_id=room["room_id"], launch_id=a1, participant="lens_a",
            ranking={"a": ["DP1", "DP2"], "b": ["DP1", "DP2"]},
            stances={"DP1": {"a": "probably", "b": "yes"}, "DP2": {"a": "yes", "b": "yes"}},
        )


def test_a_partial_ranking_is_refused(store):
    room = _two_point_room(store, rank_all=True)
    a1 = _lens_launch(store, lens_name="lens_a")
    with pytest.raises(StanceIncompleteError, match="rank-all means every"):
        post_final_stance(
            store, room_id=room["room_id"], launch_id=a1, participant="lens_a",
            ranking={"a": ["DP1"], "b": ["DP1", "DP2"]},
            stances={"DP1": {"a": "yes", "b": "yes"}, "DP2": {"a": "yes", "b": "yes"}},
        )


def test_a_stance_posts_a_readable_turn_and_a_structured_event(store):
    room = _two_point_room(store, rank_all=True)
    a1 = _lens_launch(store, lens_name="lens_a")
    row = post_final_stance(store, room_id=room["room_id"], launch_id=a1, participant="lens_a", **_full_stance())
    assert row["kind"] == "closure"
    assert "FINAL STANCE — lens_a" in row["body"] and "DP1: (a) yes · (b) yes" in row["body"]
    filed = list_final_stances(store, room_id=room["room_id"])
    assert filed["lens_a"]["stances"]["DP2"] == {"a": "yes", "b": "yes"}
    assert filed["lens_a"]["ranking"]["b"] == ["DP2", "DP1"]


def test_a_refiled_stance_supersedes_the_earlier_one(store):
    room = _two_point_room(store, rank_all=True)
    a1 = _lens_launch(store, lens_name="lens_a")
    post_final_stance(store, room_id=room["room_id"], launch_id=a1, participant="lens_a", **_full_stance("yes", "yes"))
    post_final_stance(store, room_id=room["room_id"], launch_id=a1, participant="lens_a", **_full_stance("no", "no"))
    filed = list_final_stances(store, room_id=room["room_id"])
    assert filed["lens_a"]["stances"]["DP1"] == {"a": "no", "b": "no"}
    # the superseded turn is still in the append-only doc
    assert render_room_markdown(store, room["room_id"]).count("FINAL STANCE") == 2


def test_a_non_participant_cannot_file_a_stance(store):
    room = _two_point_room(store, rank_all=True)
    with pytest.raises(ValueError, match="is not a participant"):
        post_final_stance(
            store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_z"),
            participant="lens_z", **_full_stance(),
        )


def test_the_procedural_point_is_refused_by_every_pass_that_reads_turn_prose(store):
    """``score_dp`` refused the rank-all point; the extract pass and the
    moderator envelope took it.

    Its turns ARE the structured final stances, and their bodies open with
    ``FINAL STANCE — <participant>`` -- so extracting or scoring them hands
    the extractor and the moderator the seat identity those two passes exist
    to withhold.
    """
    room = _two_point_room(store, rank_all=True)
    room_id = room["room_id"]
    a1 = _lens_launch(store, lens_name="lens_a")
    post_final_stance(store, room_id=room_id, launch_id=a1, participant="lens_a", **_full_stance())
    assert "FINAL STANCE — lens_a" in list_room_turns(store, room_id=room_id, dp_id=RANK_ALL_DP_ID)[0]["body"]

    with pytest.raises(ValueError, match="never extracted or scored"):
        build_extract_envelope(store, room_id=room_id, dp_id=RANK_ALL_DP_ID)
    with pytest.raises(ValueError, match="never extracted or scored"):
        build_moderator_scoring_envelope(store, room_id=room_id, dp_id=RANK_ALL_DP_ID)
    with pytest.raises(ValueError, match="never extracted or scored"):
        record_turn_extracts(
            store, room_id=room_id, dp_id=RANK_ALL_DP_ID, by_launch=bootstrap_launch(store, agent_kind="moderator"),
            extracts=[{"seq": 1, "claim": "c", "anchors": ["DOC-1"], "stance_a": "yes", "stance_b": "yes",
                       "residual_disagreement": "none"}],
        )


def test_a_converged_room_cannot_be_rescored(store):
    """``post_message`` has the guard and ``score_dp`` did not: a converged
    room's label and share are the record of what it decided, and rewriting
    them in place would leave ``room.state`` saying nothing had changed."""
    room = _two_point_room(store)
    room_id = room["room_id"]
    moderator = bootstrap_launch(store, agent_kind="moderator")
    for dp_id in ("DP1", "DP2"):
        score_dp(store, room_id=room_id, dp_id=dp_id, by_launch=moderator, judge=lambda env: 95.0)
    assert converge_room(store, room_id=room_id, by_launch=moderator)["state"] == "converged"
    with pytest.raises(ValueError, match="not open"):
        score_dp(store, room_id=room_id, dp_id="DP1", by_launch=moderator, judge=lambda env: 10.0)
    assert get_dp_score(store, room_id=room_id, dp_id="DP1")["agreement_pct"] == 95.0


def test_a_frozen_room_cannot_be_rescored(store):
    room = _two_point_room(store)
    room_id = room["room_id"]
    moderator = bootstrap_launch(store, agent_kind="moderator")
    score_dp(store, room_id=room_id, dp_id="DP1", by_launch=moderator, judge=lambda env: 40.0)
    freeze_room(store, room_id=room_id, by_launch=moderator, reason="criterion (b) unresolved")
    with pytest.raises(ValueError, match="not open"):
        score_dp(store, room_id=room_id, dp_id="DP1", by_launch=moderator, judge=lambda env: 99.0)


# ---------------------------------------------------------------------------
# computed agreement_pct, label first
# ---------------------------------------------------------------------------


def test_agreement_is_the_modal_share_per_criterion_and_the_weaker_one_binds():
    stances = {
        "p1": {"stances": {"DP1": {"a": "yes", "b": "yes"}}},
        "p2": {"stances": {"DP1": {"a": "yes", "b": "no"}}},
        "p3": {"stances": {"DP1": {"a": "yes", "b": "yes"}}},
    }
    out = compute_agreement_pct(stances, dp_id="DP1")
    assert out["per_criterion"]["a"]["share_pct"] == 100.0
    assert out["per_criterion"]["b"]["share_pct"] == pytest.approx(66.666667, abs=1e-5)
    # both-or-eliminated: the weaker criterion is the point's number
    assert out["agreement_pct"] == pytest.approx(66.666667, abs=1e-5)


def test_a_two_way_split_is_fifty_percent_and_names_both_modes():
    stances = {
        "p1": {"stances": {"DP1": {"a": "yes", "b": "yes"}}},
        "p2": {"stances": {"DP1": {"a": "no", "b": "yes"}}},
    }
    out = compute_agreement_pct(stances, dp_id="DP1")
    assert out["per_criterion"]["a"]["share_pct"] == 50.0
    assert out["per_criterion"]["a"]["modal"] == ["no", "yes"]
    assert out["agreement_pct"] == 50.0


def test_unclear_is_its_own_stance_and_is_not_rounded_to_no():
    stances = {
        "p1": {"stances": {"DP1": {"a": "unclear", "b": "yes"}}},
        "p2": {"stances": {"DP1": {"a": "no", "b": "yes"}}},
    }
    out = compute_agreement_pct(stances, dp_id="DP1")
    assert out["per_criterion"]["a"]["counts"] == {"no": 1, "unclear": 1}
    assert out["per_criterion"]["a"]["share_pct"] == 50.0


def _stanced_room(store):
    room = _two_point_room(store, rank_all=True)
    a1, b1 = _lens_launch(store, lens_name="lens_a"), _lens_launch(store, lens_name="lens_b")
    post_final_stance(store, room_id=room["room_id"], launch_id=a1, participant="lens_a", **_full_stance())
    post_final_stance(store, room_id=room["room_id"], launch_id=b1, participant="lens_b", **_full_stance())
    return room


def test_the_judge_cannot_supply_agreement_pct_where_stances_exist(store):
    room = _stanced_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    with pytest.raises(ValueError, match="not the judge's to supply"):
        score_dp(
            store, room_id=room["room_id"], dp_id="DP1", by_launch=moderator,
            judge=lambda env: {"label": "MEETS-BOTH", "agreement_pct": 99.0},
        )


def test_the_label_is_required_on_the_stance_path(store):
    room = _stanced_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    with pytest.raises(ValueError, match="no label"):
        score_dp(store, room_id=room["room_id"], dp_id="DP1", by_launch=moderator, judge=lambda env: {"note": "hm"})


def test_a_label_outside_the_vocabulary_is_refused(store):
    room = _stanced_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    with pytest.raises(ValueError, match="label must be one of"):
        score_dp(
            store, room_id=room["room_id"], dp_id="DP1", by_launch=moderator,
            judge=lambda env: {"label": "CONVERGED"},
        )


def test_a_point_is_not_scored_until_every_seat_has_filed(store):
    room = _two_point_room(store, rank_all=True)
    a1 = _lens_launch(store, lens_name="lens_a")
    post_final_stance(store, room_id=room["room_id"], launch_id=a1, participant="lens_a", **_full_stance())
    with pytest.raises(StanceIncompleteError, match="have filed no final stance"):
        score_dp(
            store, room_id=room["room_id"], dp_id="DP1", by_launch=bootstrap_launch(store),
            judge=lambda env: {"label": "MEETS-BOTH"},
        )


def test_the_computed_number_is_below_bar_when_the_room_splits(store):
    room = _two_point_room(store, rank_all=True)
    a1, b1 = _lens_launch(store, lens_name="lens_a"), _lens_launch(store, lens_name="lens_b")
    post_final_stance(store, room_id=room["room_id"], launch_id=a1, participant="lens_a", **_full_stance("yes", "yes"))
    post_final_stance(store, room_id=room["room_id"], launch_id=b1, participant="lens_b", **_full_stance("no", "yes"))
    row = score_dp(
        store, room_id=room["room_id"], dp_id="DP1", by_launch=bootstrap_launch(store),
        judge=lambda env: {"label": "UNDECIDED"},
    )
    assert row["agreement_pct"] == 50.0
    assert row["converged"] is False and row["agreement_pct"] < CONVERGENCE_BAR_PCT
    assert row["agreement"]["per_criterion"]["b"]["share_pct"] == 100.0


def test_the_envelope_tells_the_judge_the_number_is_already_computed(store):
    room = _stanced_room(store)
    env = build_moderator_scoring_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env["agreement_pct_computed"] == 100.0
    assert "Do NOT return agreement_pct" in env["instructions"]
    assert env["labels"] == list(DP_LABELS)


def test_a_room_with_no_stances_keeps_the_pre_framework_path(store):
    room = _two_point_room(store)
    row = score_dp(
        store, room_id=room["room_id"], dp_id="DP1", by_launch=bootstrap_launch(store),
        judge=lambda env: {"agreement_pct": 92.5, "note": "close"},
    )
    assert row["agreement_pct"] == 92.5 and row["agreement_from"] == "judge" and row["label"] is None


def test_converge_refuses_while_the_rank_all_point_is_unfiled(store):
    """Both points are at bar and nobody has filed a stance — which is
    exactly the room charter §6(iv) refuses to let converge: rank-all comes
    before any verdict, and a room that scored its points without it has a
    verdict resting on prose."""
    room = _two_point_room(store, rank_all=True)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    for dp_id in ("DP1", "DP2"):
        score_dp(store, room_id=room["room_id"], dp_id=dp_id, by_launch=moderator, judge=lambda env: 95.0)
    status = check_room_converged(store, room["room_id"])
    assert status["all_converged"] is True
    assert status["rank_all"]["missing"] == ["lens_a", "lens_b"]
    with pytest.raises(StanceIncompleteError, match="rank-all point is complete"):
        converge_room(store, room_id=room["room_id"], by_launch=moderator)


# ---------------------------------------------------------------------------
# the neutral extract pass
# ---------------------------------------------------------------------------


def _one_turn_room(store):
    room = _two_point_room(store)
    post_message(store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"), dp_id="DP1", body="p")
    return room


def _extract(seq=1, **over):
    base = {
        "seq": seq, "claim": "c", "anchors": ["DOC-1"], "stance_a": "yes", "stance_b": "no",
        "residual_disagreement": "the bookkeeping cost",
    }
    base.update(over)
    return base


def test_extracts_replace_the_prose_in_the_moderator_envelope(store):
    room = _one_turn_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    record_turn_extracts(store, room_id=room["room_id"], dp_id="DP1", extracts=[_extract()], by_launch=moderator)
    env = build_moderator_scoring_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env["extracts_recorded"] is True
    assert "turns" not in env
    assert env["extracts"][0]["residual_disagreement"] == "the bookkeeping cost"
    assert "author_launch" not in json.dumps(env)


def test_a_partial_extract_pass_is_refused_rather_than_half_stored(store):
    room = _one_turn_room(store)
    post_message(store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_b"), dp_id="DP1", body="q")
    moderator = bootstrap_launch(store, agent_kind="moderator")
    with pytest.raises(ValueError, match="turns with no extract"):
        record_turn_extracts(store, room_id=room["room_id"], dp_id="DP1", extracts=[_extract()], by_launch=moderator)
    assert list_turn_extracts(store, room_id=room["room_id"], dp_id="DP1") == []


def test_an_extract_missing_a_field_is_refused(store):
    room = _one_turn_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    with pytest.raises(ValueError, match="missing"):
        record_turn_extracts(
            store, room_id=room["room_id"], dp_id="DP1",
            extracts=[_extract(residual_disagreement=None)], by_launch=moderator,
        )


def test_an_extract_for_a_turn_on_another_point_is_refused(store):
    room = _one_turn_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    with pytest.raises(ValueError, match="is not a turn on"):
        record_turn_extracts(
            store, room_id=room["room_id"], dp_id="DP1", extracts=[_extract(seq=99)], by_launch=moderator
        )


def test_require_extracts_refuses_scoring_on_prose(store):
    room = _one_turn_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    with pytest.raises(ValueError, match="no complete"):
        score_dp(
            store, room_id=room["room_id"], dp_id="DP1", by_launch=moderator, require_extracts=True,
            judge=lambda env: 95.0,
        )


def test_a_rerun_extract_pass_supersedes_the_previous_one_whole(store):
    room = _one_turn_room(store)
    moderator = bootstrap_launch(store, agent_kind="moderator")
    record_turn_extracts(store, room_id=room["room_id"], dp_id="DP1", extracts=[_extract(claim="first")], by_launch=moderator)
    record_turn_extracts(store, room_id=room["room_id"], dp_id="DP1", extracts=[_extract(claim="second")], by_launch=moderator)
    extracts = list_turn_extracts(store, room_id=room["room_id"], dp_id="DP1")
    assert [e["claim"] for e in extracts] == ["second"]


# ---------------------------------------------------------------------------
# the buster's carried position
# ---------------------------------------------------------------------------


def test_the_buster_position_is_re_injected_verbatim_every_turn(store):
    room = _two_point_room(store, buster="lens_b")
    text = "ASSUME NOT: players tolerate bookkeeping.\n\n  Two spaces and a trailing tab\t"
    b1 = _lens_launch(store, lens_name="lens_b")
    record_buster_position(store, room_id=room["room_id"], participant="lens_b", text=text, by_launch=b1)
    post_message(store, room_id=room["room_id"], launch_id=b1, dp_id="DP1", body="turn one")
    post_message(store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"), dp_id="DP1", body="x")
    env = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1", participant="lens_b")
    assert env["buster_position"]["recorded_position"]["text"] == text
    assert [s["body"] for s in env["buster_position"]["stances_so_far"]] == ["turn one"]


def test_a_non_buster_seat_gets_no_position_envelope(store):
    room = _two_point_room(store, buster="lens_b")
    b1 = _lens_launch(store, lens_name="lens_b")
    record_buster_position(store, room_id=room["room_id"], participant="lens_b", text="ASSUME NOT", by_launch=b1)
    env = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1", participant="lens_a")
    assert "buster_position" not in env


def test_the_first_recorded_position_wins_and_the_second_is_reported(store):
    room = _two_point_room(store, buster="lens_b")
    b1 = _lens_launch(store, lens_name="lens_b")
    record_buster_position(store, room_id=room["room_id"], participant="lens_b", text="first", by_launch=b1)
    record_buster_position(store, room_id=room["room_id"], participant="lens_b", text="re-argued", by_launch=b1)
    position = get_buster_position(store, room_id=room["room_id"])
    assert position["text"] == "first" and position["superseded_attempts"] == 1


def test_a_position_for_a_seat_that_is_not_the_buster_is_refused(store):
    room = _two_point_room(store, buster="lens_b")
    with pytest.raises(ValueError, match="declares its buster"):
        record_buster_position(
            store, room_id=room["room_id"], participant="lens_a", text="x",
            by_launch=_lens_launch(store, lens_name="lens_a"),
        )


def test_a_buster_that_is_not_at_the_table_is_refused_at_creation(store):
    with pytest.raises(ValueError, match="not one of this room's participants"):
        _two_point_room(store, buster="lens_z")


# ---------------------------------------------------------------------------
# NEITHER ownership by lens name
# ---------------------------------------------------------------------------


def test_the_owning_lens_cannot_be_seated_at_creation(store):
    author = _lens_launch(store, lens_name="lens_a")
    idea_id = _idea(store, author_launch=author)
    with pytest.raises(OwnershipConflictError, match="at room creation"):
        create_room(
            store, topic="t", participants=["lens_a", "lens_b"],
            discussion_points=[{"dp_id": "DP1", "prompt": "p", "idea_id": idea_id}],
        )


def test_a_respawned_owning_lens_is_refused_at_its_turn(store):
    """The launch-id comparison alone cannot catch this: the author's launch
    and the poster's launch are different rows with the same lens name."""
    author = _lens_launch(store, lens_name="lens_owner")
    idea_id = _idea(store, author_launch=author)
    room = create_room(
        store, topic="t", participants=["lens_a", "lens_b"],
        discussion_points=[{"dp_id": "DP1", "prompt": "p", "idea_id": idea_id}],
    )
    respawn = _lens_launch(store, lens_name="lens_owner")
    assert respawn != author and lens_name_of_launch(store, respawn) == "lens_owner"
    with pytest.raises(OwnershipConflictError, match="checked by lens name"):
        post_message(store, room_id=room["room_id"], launch_id=respawn, dp_id="DP1", body="my own idea is great")


def test_scoring_refuses_a_point_its_owner_posted_a_turn_on(store):
    author = _lens_launch(store, lens_name="lens_owner")
    idea_id = _idea(store, author_launch=author)
    room = create_room(
        store, topic="t", participants=["lens_a", "lens_b"],
        discussion_points=[{"dp_id": "DP1", "prompt": "p", "idea_id": idea_id}],
    )
    # A direct write bypassing post_message's own refusal -- what the
    # scoring-time check exists to catch.
    insert(
        store, "room_turn",
        {
            "room_id": room["room_id"], "seq": 1, "author_launch": _lens_launch(store, lens_name="lens_owner"),
            "dp_ref": f"{room['room_id']}::DP1", "body": "mine is great", "ts": now(),
        },
    )
    with pytest.raises(OwnershipConflictError, match="its owner's own vetting turn"):
        score_dp(
            store, room_id=room["room_id"], dp_id="DP1", by_launch=bootstrap_launch(store), judge=lambda env: 95.0
        )


def test_a_launch_with_no_lens_name_makes_no_name_level_claim(store):
    """An idea authored by a launch booked outside the lens export has no
    lens name, so only the launch-id half of the invariant applies — and it
    still applies."""
    author = bootstrap_launch(store)
    idea_id = seed_idea(store, author_launch=author)
    room = create_room(
        store, topic="t", participants=["lens_a", "lens_b"],
        discussion_points=[{"dp_id": "DP1", "prompt": "p", "idea_id": idea_id}],
    )
    with pytest.raises(OwnershipConflictError, match="cannot post a"):
        post_message(store, room_id=room["room_id"], launch_id=author, dp_id="DP1", body="x")
    post_message(store, room_id=room["room_id"], launch_id=_lens_launch(store, lens_name="lens_a"), dp_id="DP1", body="ok")


# ---------------------------------------------------------------------------
# dossier labels on the DP payload
# ---------------------------------------------------------------------------


def test_a_whole_dossier_is_reduced_to_its_labels(store):
    author = _lens_launch(store, lens_name="lens_author")
    idea_id = _idea(store, author_launch=author)
    dossier = {
        "idea_id": idea_id, "label_inventory": "variant", "label_corpus": "adjacent", "judged": True,
        "known_mechanic": {"row_id": "ROW-9", "similarity": 0.97},
        "distances": {"d_prov": 0.2, "leap": {"family": "other"}},
        "candidate_hits": {"R4": [{"text": "a retrieved passage"}]},
    }
    room = create_room(
        store, topic="t", participants=["lens_a", "lens_b"],
        discussion_points=[{"dp_id": "DP1", "prompt": "p", "idea_id": idea_id, "dossier": dossier}],
    )
    dp = json.loads(room["dps"])["discussion_points"][0]
    assert dp["dossier_labels"] == {"label_inventory": "variant", "label_corpus": "adjacent", "judged": True}
    env = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env["dossier_labels"] == dp["dossier_labels"]
    blob = json.dumps(room["dps"]) + json.dumps(env)
    for leaked in ("known_mechanic", "d_prov", "a retrieved passage"):
        assert leaked not in blob


def test_an_unexpected_explicit_label_key_is_refused(store):
    with pytest.raises(ValueError, match="dossier's labels only"):
        create_room(
            store, topic="t", participants=["lens_a", "lens_b"],
            discussion_points=[{"dp_id": "DP1", "prompt": "p", "dossier_labels": {"label_inventory": "variant", "rationale": "because"}}],
        )


def test_a_point_without_a_dossier_carries_no_label_key(store):
    room = _two_point_room(store)
    dp = json.loads(room["dps"])["discussion_points"][0]
    assert "dossier_labels" not in dp
    assert "dossier_labels" not in build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")


def test_the_reserved_rank_all_id_cannot_be_an_idea_point(store):
    with pytest.raises(ValueError, match="reserved for the procedural"):
        create_room(
            store, topic="t", participants=["lens_a", "lens_b"],
            discussion_points=[{"dp_id": RANK_ALL_DP_ID, "prompt": "p"}],
        )


# ---------------------------------------------------------------------------
# admission order
# ---------------------------------------------------------------------------


def _pool(n_per_cell=5):
    cells = [("near", "MISMATCH"), ("moderate", "MISMATCH"), ("far", "TRANSFER"),
             ("near", "TRANSFER"), ("moderate", "TRANSFER"), ("far", "MISMATCH")]
    return [
        {"idea_id": f"IDEA-{arm}-{card}-{i}", "arm": arm, "recipe_card": card}
        for arm, card in cells
        for i in range(n_per_cell)
    ]


def test_every_idea_is_roomed_exactly_once_across_rooms_and_remainder():
    pool = _pool()
    out = build_admission_order(pool, seed="seed-1")
    seated = [i for room in out["rooms"] for i in room] + out["remainder"]
    assert sorted(seated) == sorted(i["idea_id"] for i in pool)
    assert len(seated) == len(set(seated)) == 30


def test_the_order_is_reproducible_from_its_seed_and_changes_with_it():
    pool = _pool()
    a = build_admission_order(pool, seed="seed-1")
    b = build_admission_order(pool, seed="seed-1")
    c = build_admission_order(pool, seed="seed-2")
    assert a["order"] == b["order"] and a["hash"] == b["hash"]
    assert c["order"] != a["order"]


def test_the_hash_is_over_the_order_alone():
    pool = _pool()
    a = build_admission_order(pool, seed="seed-1", ideas_per_room=6, rooms_per_batch=4)
    b = build_admission_order(pool, seed="seed-1", ideas_per_room=5, rooms_per_batch=6)
    assert a["order"] == b["order"]
    assert a["hash"] == b["hash"] == admission_order_hash(a["order"])
    assert len(a["rooms"]) != len(b["rooms"])


def test_every_room_carries_a_mix_of_cells_rather_than_one_cell():
    """Stratified means interleaved: sorting by cell would make the first
    room one arm's ideas, and a batch a measurement of that arm."""
    pool = _pool()
    by_id = {i["idea_id"]: i for i in pool}
    out = build_admission_order(pool, seed="seed-1")
    for room in out["rooms"]:
        arms = {by_id[i]["arm"] for i in room}
        cards = {by_id[i]["recipe_card"] for i in room}
        assert len(arms) >= 2 and len(cards) == 2


def test_the_trailing_partial_room_is_the_carried_remainder():
    pool = _pool(n_per_cell=2)  # 12 ideas
    out = build_admission_order(pool, seed="seed-1", ideas_per_room=5, rooms_per_batch=4)
    assert [len(r) for r in out["rooms"]] == [5, 5]
    assert len(out["remainder"]) == 2
    # the remainder keeps its place in the order
    assert out["order"][-2:] == out["remainder"]


def test_a_batch_outside_the_charter_band_is_refused_unless_overridden():
    pool = _pool()
    low, high = ADMISSION_BATCH_ROOM_RANGE
    with pytest.raises(AdmissionOrderError, match="charter .6 batch band"):
        build_admission_order(pool, seed="s", rooms_per_batch=high + 1)
    out = build_admission_order(pool, seed="s", rooms_per_batch=1, enforce_batch_band=False)
    assert all(len(batch) == 1 for batch in out["batches"])
    assert low <= 4 <= high


def test_an_unseeded_or_duplicated_pool_is_refused():
    with pytest.raises(AdmissionOrderError, match="seed is required"):
        build_admission_order(_pool(), seed="")
    with pytest.raises(AdmissionOrderError, match="appears twice"):
        build_admission_order([{"idea_id": "IDEA-1"}, {"idea_id": "IDEA-1"}], seed="s", enforce_batch_band=False)
    with pytest.raises(AdmissionOrderError, match="carries no idea_id"):
        build_admission_order([{"arm": "near"}], seed="s", enforce_batch_band=False)


def test_the_same_pool_in_a_different_input_order_draws_the_same_order():
    """The draw is a function of the SET and the seed, not of the order the
    caller happened to hand the pool in -- each cell's members are sorted
    before the seeded shuffle. Without that, the same round's own pool read
    back the other way round hashed differently, and the hash is the thing
    the round escrows."""
    pool = _pool()
    reversed_pool = list(reversed(pool))
    forward = build_admission_order(pool, seed="seed-1")
    backward = build_admission_order(reversed_pool, seed="seed-1")
    assert forward["order"] == backward["order"]
    assert forward["hash"] == backward["hash"]


def test_a_pool_smaller_than_one_room_says_so_and_names_the_flag():
    """Round 0's two-idea dry run seats nobody at the charter room size, and
    used to say nothing at all about why."""
    pool = [{"idea_id": "IDEA-1", "arm": "near"}, {"idea_id": "IDEA-2", "arm": "far"}]
    out = build_admission_order(pool, seed="s", enforce_batch_band=False)
    assert out["rooms"] == []
    assert sorted(out["remainder"]) == ["IDEA-1", "IDEA-2"]
    assert "--ideas-per-room 2" in out["note"]

    seated = build_admission_order(pool, seed="s", ideas_per_room=2, enforce_batch_band=False)
    assert len(seated["rooms"]) == 1 and "note" not in seated


def test_records_with_no_arm_or_card_are_still_roomed():
    pool = [{"idea_id": "IDEA-1"}, {"idea_id": "IDEA-2", "arm": "near"}]
    out = build_admission_order(pool, seed="s", ideas_per_room=2, enforce_batch_band=False)
    assert sorted(out["rooms"][0]) == ["IDEA-1", "IDEA-2"]
    assert "unknown/none" in out["cells"]


def test_the_pool_is_the_rounds_consolidated_ideas_only(store):
    author = _lens_launch(store, lens_name="lens_author")
    consolidated = _idea(store, author_launch=author, tier="far", card="TRANSFER")
    other_round = _idea(store, author_launch=author, round_id="ROUND-other")
    raw_id = _idea(store, author_launch=author)
    update(store, "idea", pk_column="idea_id", pk_value=raw_id, changes={"status": "raw"})
    merged_id = _idea(store, author_launch=author)
    update(store, "idea", pk_column="idea_id", pk_value=merged_id, changes={"status": "merged"})

    pool = consolidated_ideas_for_admission(store, round_id="ROUND-fix")
    ids = [p["idea_id"] for p in pool]
    assert consolidated in ids
    assert raw_id not in ids and merged_id not in ids and other_round not in ids
    assert {"arm", "recipe_card"} <= set(pool[0])
    assert all(set(p) <= {"idea_id", "arm", "recipe_card", "created_ts"} for p in pool)


def test_no_dossier_field_is_a_parameter_of_the_admission_order():
    """The barrier is structural, not procedural: there is no argument
    through which a screen result could reach the order."""
    import inspect

    params = set(inspect.signature(build_admission_order).parameters)
    assert params == {"ideas", "seed", "ideas_per_room", "rooms_per_batch", "enforce_batch_band"}
    pool = [{"idea_id": "IDEA-1", "arm": "near", "recipe_card": "MISMATCH", "label_inventory": "new-mechanism"},
            {"idea_id": "IDEA-2", "arm": "near", "recipe_card": "MISMATCH", "label_inventory": "same"}]
    flipped = [dict(i, label_inventory=("same" if i["label_inventory"] == "new-mechanism" else "new-mechanism")) for i in pool]
    assert (
        build_admission_order(pool, seed="s", ideas_per_room=2, enforce_batch_band=False)["order"]
        == build_admission_order(flipped, seed="s", ideas_per_room=2, enforce_batch_band=False)["order"]
    )


def test_turn_kinds_vocabulary_is_the_charters_three():
    assert TURN_KINDS == ("position", "question", "closure")


def test_a_label_alone_on_a_stanceless_point_says_why_it_cannot_be_scored(store):
    room = _two_point_room(store, rank_all=True)
    with pytest.raises(StanceIncompleteError, match="nothing to compute agreement_pct from"):
        score_dp(
            store, room_id=room["room_id"], dp_id="DP1", by_launch=bootstrap_launch(store),
            judge=lambda env: {"label": "MEETS-BOTH"},
        )
