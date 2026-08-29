"""``trialerror.rooms.api`` — the rooms runtime. Uses the shared ``store``
fixture from ``tests/conftest.py`` (isolated platform+program roots) plus
``tests/_rooms_fixtures.py``'s self-contained launch/idea/template
builders.
"""

from __future__ import annotations

import json

import pytest

from trialerror.rooms.api import (
    CONVERGENCE_BAR_PCT,
    PARTICIPANT_RANGE,
    build_moderator_scoring_envelope,
    build_participant_turn_envelope,
    check_room_converged,
    converge_room,
    create_room,
    export_room,
    freeze_room,
    get_dp_score,
    get_freeze_reason,
    get_room,
    list_room_turns,
    post_message,
    register_room_deliverable,
    render_room_markdown,
    score_dp,
)
from trialerror.rooms.errors import ConvergenceBarNotMetError, IllegalRoomTransitionError, OwnershipConflictError
from trialerror.stores.errors import ValidationError, XidTargetMissingError

from tests._rooms_fixtures import bootstrap_launch, seed_idea, seed_template


def _basic_dps():
    return [{"prompt": "does the mechanism generalize?"}, {"prompt": "does it survive a hostile edit?"}]


def _room(store, *, launch_id=None, participants=("P1", "P2"), dps=None):
    return create_room(
        store,
        topic="test room",
        discussion_points=dps or _basic_dps(),
        participants=list(participants),
        by_launch=launch_id,
    )


# ---------------------------------------------------------------------------
# create_room
# ---------------------------------------------------------------------------


def test_create_room_happy_path(store):
    launch_id = bootstrap_launch(store)
    row = _room(store, launch_id=launch_id)
    assert row["state"] == "open"
    assert row["topic"] == "test room"
    config = json.loads(row["dps"])
    assert [d["dp_id"] for d in config["discussion_points"]] == ["DP1", "DP2"]
    assert config["participants"] == ["P1", "P2"]
    assert config["rounds_per_dp"] == 2
    assert config["convergence_bar_pct"] == CONVERGENCE_BAR_PCT

    events = store.ops.execute("SELECT type, payload FROM event WHERE type = 'room_created'").fetchall()
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["room_id"] == row["room_id"]
    assert payload["dp_ids"] == ["DP1", "DP2"]


def test_create_room_explicit_dp_ids_and_idea_link(store):
    launch_id = bootstrap_launch(store)
    idea_id = seed_idea(store, author_launch=launch_id)
    row = create_room(
        store,
        topic="vet an idea",
        discussion_points=[{"dp_id": "DP-idea", "prompt": "is this sound?", "idea_id": idea_id}],
        participants=["P1", "P2"],
    )
    config = json.loads(row["dps"])
    assert config["discussion_points"] == [{"dp_id": "DP-idea", "prompt": "is this sound?", "idea_id": idea_id}]


def test_create_room_populates_created_ts(store):
    """ops-v3 (build-v2-polish, module TRIALERROR-DEV-NOTE item 2, CLOSED):
    room.created_ts is now a real column, alongside the pre-existing
    room_created companion event."""
    row = _room(store)
    assert row["room_id"]
    stored = store.ops.execute("SELECT created_ts FROM room WHERE room_id = ?", (row["room_id"],)).fetchone()
    assert stored["created_ts"]
    event = store.ops.execute(
        "SELECT ts FROM event WHERE type = 'room_created'"
    ).fetchone()
    assert stored["created_ts"] == event["ts"]


def test_create_room_writes_room_link_for_dp_with_idea_id(store):
    """ops-v3 (module TRIALERROR-DEV-NOTE item 4, CLOSED): a discussion point
    carrying an idea_id gets a real room_link row, alongside (not instead
    of) the dps JSON entry -- and a DP with no idea_id gets no row."""
    launch_id = bootstrap_launch(store)
    idea_id = seed_idea(store, author_launch=launch_id)
    row = create_room(
        store,
        topic="vet an idea",
        discussion_points=[
            {"dp_id": "DP-idea", "prompt": "is this sound?", "idea_id": idea_id},
            {"dp_id": "DP-plain", "prompt": "no idea attached"},
        ],
        participants=["P1", "P2"],
    )
    links = store.ops.execute(
        "SELECT room_id, dp_id, idea_id FROM room_link WHERE room_id = ?", (row["room_id"],)
    ).fetchall()
    assert [dict(link_row) for link_row in links] == [
        {"room_id": row["room_id"], "dp_id": "DP-idea", "idea_id": idea_id}
    ]


def test_create_room_bad_idea_id_refused_before_any_write(store):
    """The room_link XID pre-check (:func:`trialerror.rooms.api._require_idea_exists`)
    refuses BEFORE the room row lands -- no half-written room."""
    with pytest.raises(XidTargetMissingError):
        create_room(
            store, topic="t",
            discussion_points=[{"dp_id": "DP1", "prompt": "p", "idea_id": "IDEA-bogus"}],
            participants=["P1", "P2"],
        )
    rows = store.ops.execute("SELECT room_id FROM room").fetchall()
    assert rows == []


@pytest.mark.parametrize("n", [0, 1, 4, 5])
def test_create_room_enforces_participant_range_by_default(store, n):
    with pytest.raises(ValueError, match="participants must number"):
        create_room(store, topic="t", discussion_points=_basic_dps(), participants=[f"P{i}" for i in range(n)])


def test_create_room_participant_range_boundaries_pass(store):
    for n in PARTICIPANT_RANGE:
        row = create_room(store, topic="t", discussion_points=_basic_dps(), participants=[f"P{i}" for i in range(n)])
        assert row["room_id"]


def test_create_room_participant_range_override(store):
    row = create_room(
        store, topic="t", discussion_points=_basic_dps(), participants=["P1"], enforce_participant_range=False
    )
    assert json.loads(row["dps"])["participants"] == ["P1"]


def test_create_room_duplicate_dp_id_refused(store):
    with pytest.raises(ValueError, match="duplicate dp_id"):
        create_room(
            store, topic="t",
            discussion_points=[{"dp_id": "DP1", "prompt": "a"}, {"dp_id": "DP1", "prompt": "b"}],
            participants=["P1", "P2"],
        )


def test_create_room_missing_prompt_refused(store):
    with pytest.raises(ValueError, match="missing a required 'prompt'"):
        create_room(store, topic="t", discussion_points=[{"dp_id": "DP1"}], participants=["P1", "P2"])


def test_create_room_no_discussion_points_refused(store):
    with pytest.raises(ValueError, match="at least one discussion point"):
        create_room(store, topic="t", discussion_points=[], participants=["P1", "P2"])


def test_create_room_rounds_per_dp_must_be_positive(store):
    with pytest.raises(ValueError, match="rounds_per_dp"):
        create_room(store, topic="t", discussion_points=_basic_dps(), participants=["P1", "P2"], rounds_per_dp=0)


def test_create_room_bad_by_launch_refused(store):
    with pytest.raises(XidTargetMissingError):
        create_room(store, topic="t", discussion_points=_basic_dps(), participants=["P1", "P2"], by_launch="LNCH-bogus")


def test_get_room_unknown_returns_none(store):
    assert get_room(store, "ROOM-bogus") is None


# ---------------------------------------------------------------------------
# post_message
# ---------------------------------------------------------------------------


def test_post_message_happy_path_and_seq_increments(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    t1 = post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="first turn")
    t2 = post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="second turn")
    assert t1["seq"] == 1
    assert t2["seq"] == 2
    assert t2["ts"]

    turns = list_room_turns(store, room_id=room["room_id"], dp_id="DP1")
    assert [t["body"] for t in turns] == ["first turn", "second turn"]
    assert all(t["author_launch"] == launch_id for t in turns)


def test_post_message_populates_ts_column(store):
    """ops-v3 (module TRIALERROR-DEV-NOTE item 2, CLOSED): room_turn.ts is now a
    real column -- matches the returned dict's own 'ts' and the companion
    room_turn event's ts (both already asserted elsewhere), byte-for-byte."""
    launch_id = bootstrap_launch(store)
    room = _room(store)
    turn = post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="hi")
    stored = store.ops.execute(
        "SELECT ts FROM room_turn WHERE room_id = ? AND seq = ?", (room["room_id"], turn["seq"])
    ).fetchone()
    assert stored["ts"] == turn["ts"]


def test_post_message_seq_is_per_room_not_global(store):
    launch_id = bootstrap_launch(store)
    room_a = _room(store)
    room_b = _room(store)
    post_message(store, room_id=room_a["room_id"], launch_id=launch_id, dp_id="DP1", body="a1")
    turn_b1 = post_message(store, room_id=room_b["room_id"], launch_id=launch_id, dp_id="DP1", body="b1")
    assert turn_b1["seq"] == 1  # not 2 -- room_b's own sequence, unaffected by room_a


def test_post_message_dp_ref_is_namespaced_per_room(store):
    launch_id = bootstrap_launch(store)
    room_a = _room(store)
    room_b = _room(store)
    post_message(store, room_id=room_a["room_id"], launch_id=launch_id, dp_id="DP1", body="a1")
    post_message(store, room_id=room_b["room_id"], launch_id=launch_id, dp_id="DP1", body="b1")
    turns_a = list_room_turns(store, room_id=room_a["room_id"])
    turns_b = list_room_turns(store, room_id=room_b["room_id"])
    assert turns_a[0]["dp_ref"] != turns_b[0]["dp_ref"]


def test_post_message_unknown_room_refused(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError, match="no such room"):
        post_message(store, room_id="ROOM-bogus", launch_id=launch_id, dp_id="DP1", body="x")


def test_post_message_unknown_dp_refused(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    with pytest.raises(ValueError, match="no discussion point"):
        post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP-nope", body="x")


def test_post_message_bad_launch_refused(store):
    room = _room(store)
    with pytest.raises(XidTargetMissingError):
        post_message(store, room_id=room["room_id"], launch_id="LNCH-bogus", dp_id="DP1", body="x")


def test_post_message_refused_once_room_not_open(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="stuck")
    with pytest.raises(ValueError, match="is not open"):
        post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="too late")


def test_post_message_neither_ownership_invariant(store):
    author_launch = bootstrap_launch(store)
    other_launch = bootstrap_launch(store)
    idea_id = seed_idea(store, author_launch=author_launch)
    room = create_room(
        store, topic="vet", discussion_points=[{"dp_id": "DP1", "prompt": "sound?", "idea_id": idea_id}],
        participants=["P1", "P2"],
    )
    # the idea's own author may not post a vetting turn on the DP that reviews it
    with pytest.raises(OwnershipConflictError, match="NEITHER-ownership"):
        post_message(store, room_id=room["room_id"], launch_id=author_launch, dp_id="DP1", body="self-vet")
    # a different launch may
    row = post_message(store, room_id=room["room_id"], launch_id=other_launch, dp_id="DP1", body="independent review")
    assert row["seq"] == 1


def test_post_message_no_ownership_check_when_dp_has_no_idea_id(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)  # DP1/DP2 carry no idea_id
    row = post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="fine")
    assert row["seq"] == 1


def test_post_message_emits_companion_event(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="hi")
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'room_turn'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload == {"room_id": room["room_id"], "dp_id": "DP1", "dp_ref": f"{room['room_id']}::DP1", "seq": 1}


# ---------------------------------------------------------------------------
# envelopes
# ---------------------------------------------------------------------------


def test_build_participant_turn_envelope_shape_and_round_number(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    env0 = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env0["kind"] == "room_participant_turn"
    assert env0["dp_id"] == "DP1"
    assert env0["prompt"] == "does the mechanism generalize?"
    assert env0["prior_turns"] == []
    assert env0["round_number"] == 1
    assert env0["rounds_per_dp"] == 2

    post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="turn one")
    env1 = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env1["round_number"] == 2
    assert env1["prior_turns"] == [{"seq": 1, "author_launch": launch_id, "body": "turn one"}]


def test_build_participant_turn_envelope_unknown_dp_refused(store):
    room = _room(store)
    with pytest.raises(ValueError, match="no discussion point"):
        build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="nope")


def test_build_moderator_scoring_envelope_shape(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="turn one")
    env = build_moderator_scoring_envelope(store, room_id=room["room_id"], dp_id="DP1")
    assert env["kind"] == "room_moderator_score"
    assert env["convergence_bar_pct"] == CONVERGENCE_BAR_PCT
    assert env["turns"] == [{"seq": 1, "author_launch": launch_id, "body": "turn one"}]


# ---------------------------------------------------------------------------
# score_dp
# ---------------------------------------------------------------------------


def test_score_dp_with_mapping_judge(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    row = score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: {"agreement_pct": 92.5, "note": "close"}, by_launch=launch_id)
    assert row["agreement_pct"] == 92.5
    assert row["note"] == "close"
    assert row["converged"] is True
    assert row["dp_id"] == "DP1"


def test_score_dp_with_bare_number_judge(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    row = score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 40.0, by_launch=launch_id)
    assert row["agreement_pct"] == 40.0
    assert row["converged"] is False
    assert row["note"] is None


def test_score_dp_upserts_not_duplicates(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 50.0, by_launch=launch_id)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    # ops-v3: room_score is now keyed by its real (room_id, dp_id) composite
    # PK, not the retired dp_ref namespacing convention (build-v2-polish,
    # trialerror/rooms/api.py module TRIALERROR-DEV-NOTE item 3).
    rows = store.ops.execute(
        "SELECT * FROM room_score WHERE room_id = ? AND dp_id = ?", (room["room_id"], "DP1")
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["agreement_pct"] == 95.0


@pytest.mark.parametrize("bad", [-1.0, 100.1, 101])
def test_score_dp_out_of_range_refused(store, bad):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    with pytest.raises(ValueError, match="within \\[0, 100\\]"):
        score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: bad, by_launch=launch_id)


def test_score_dp_non_numeric_refused(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    with pytest.raises(ValueError, match="non-numeric"):
        score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: "very agreeable", by_launch=launch_id)


def test_score_dp_bad_launch_refused_before_any_write(store):
    room = _room(store)
    with pytest.raises(XidTargetMissingError):
        score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch="LNCH-bogus")
    assert get_dp_score(store, room_id=room["room_id"], dp_id="DP1") is None


def test_score_dp_emits_companion_event(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'room_dp_scored'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["agreement_pct"] == 95.0
    assert payload["converged"] is True


# ---------------------------------------------------------------------------
# convergence / freeze
# ---------------------------------------------------------------------------


def test_check_room_converged_progresses_as_dps_get_scored(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    status0 = check_room_converged(store, room["room_id"])
    assert status0["all_scored"] is False
    assert status0["all_converged"] is False

    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    status1 = check_room_converged(store, room["room_id"])
    assert status1["all_scored"] is False  # DP2 still unscored
    assert status1["all_converged"] is False

    score_dp(store, room_id=room["room_id"], dp_id="DP2", judge=lambda env: 50.0, by_launch=launch_id)
    status2 = check_room_converged(store, room["room_id"])
    assert status2["all_scored"] is True
    assert status2["all_converged"] is False  # DP2 below bar

    score_dp(store, room_id=room["room_id"], dp_id="DP2", judge=lambda env: 91.0, by_launch=launch_id)
    status3 = check_room_converged(store, room["room_id"])
    assert status3["all_converged"] is True


def test_converge_room_refused_below_bar(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    with pytest.raises(ConvergenceBarNotMetError, match="DP2"):
        converge_room(store, room_id=room["room_id"], by_launch=launch_id)
    assert get_room(store, room["room_id"])["state"] == "open"


def test_converge_room_happy_path(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    score_dp(store, room_id=room["room_id"], dp_id="DP2", judge=lambda env: 90.0, by_launch=launch_id)  # exactly at bar
    row = converge_room(store, room_id=room["room_id"], by_launch=launch_id)
    assert row["state"] == "converged"

    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'room_converged'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["room_id"] == room["room_id"]


def test_converge_room_twice_refused_as_illegal_transition(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    score_dp(store, room_id=room["room_id"], dp_id="DP2", judge=lambda env: 95.0, by_launch=launch_id)
    converge_room(store, room_id=room["room_id"], by_launch=launch_id)
    with pytest.raises(IllegalRoomTransitionError):
        converge_room(store, room_id=room["room_id"], by_launch=launch_id)


def test_converge_room_bad_launch_refused(store):
    room = _room(store)
    with pytest.raises(XidTargetMissingError):
        converge_room(store, room_id=room["room_id"], by_launch="LNCH-bogus")


def test_freeze_room_requires_a_reason(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    with pytest.raises(ValueError, match="reason is required"):
        freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="")


def test_freeze_room_happy_path_and_reason_retrievable(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    row = freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="DP2 deadlocked after 2 rounds")
    assert row["state"] == "frozen"
    assert get_freeze_reason(store, room["room_id"]) == "DP2 deadlocked after 2 rounds"


def test_freeze_room_refused_once_converged(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    score_dp(store, room_id=room["room_id"], dp_id="DP2", judge=lambda env: 95.0, by_launch=launch_id)
    converge_room(store, room_id=room["room_id"], by_launch=launch_id)
    with pytest.raises(IllegalRoomTransitionError):
        freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="too late")


def test_get_freeze_reason_none_when_never_frozen(store):
    room = _room(store)
    assert get_freeze_reason(store, room["room_id"]) is None


# ---------------------------------------------------------------------------
# deliverable registration hook
# ---------------------------------------------------------------------------


def test_register_room_deliverable_refused_before_converged(store):
    launch_id = bootstrap_launch(store)
    seed_template(store)
    room = _room(store)
    with pytest.raises(ValueError, match="must be 'converged'"):
        register_room_deliverable(
            store, room_id=room["room_id"], type_key="room_theory_doc", title="t", path="artifacts/t.md",
            sha256="0" * 64, by_launch=launch_id,
        )


def test_register_room_deliverable_happy_path(store):
    launch_id = bootstrap_launch(store)
    seed_template(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    score_dp(store, room_id=room["room_id"], dp_id="DP2", judge=lambda env: 95.0, by_launch=launch_id)
    converge_room(store, room_id=room["room_id"], by_launch=launch_id)

    artifact = register_room_deliverable(
        store, room_id=room["room_id"], type_key="room_theory_doc", title="theory doc", path="artifacts/theory.md",
        sha256="1" * 64, by_launch=launch_id,
    )
    assert artifact["status"] == "draft"
    assert json.loads(artifact["attrs"])["room_id"] == room["room_id"]

    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'room_deliverable_registered'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["artifact_id"] == artifact["artifact_id"]


def test_register_room_deliverable_sets_room_deliverable_artifact_id(store):
    """ops-v3 (module TRIALERROR-DEV-NOTE item 5, CLOSED): room.
    deliverable_artifact_id is set alongside (not instead of) the
    pre-existing artifact.attrs.room_id / companion-event mirrors."""
    launch_id = bootstrap_launch(store)
    seed_template(store)
    room = _room(store)
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 95.0, by_launch=launch_id)
    score_dp(store, room_id=room["room_id"], dp_id="DP2", judge=lambda env: 95.0, by_launch=launch_id)
    converge_room(store, room_id=room["room_id"], by_launch=launch_id)

    artifact = register_room_deliverable(
        store, room_id=room["room_id"], type_key="room_theory_doc", title="theory doc", path="artifacts/theory.md",
        sha256="1" * 64, by_launch=launch_id,
    )
    stored = get_room(store, room["room_id"])
    assert stored["deliverable_artifact_id"] == artifact["artifact_id"]


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_render_room_markdown_includes_topic_scores_and_turns(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="opening position")
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: 77.0, by_launch=launch_id)

    text = render_room_markdown(store, room["room_id"])
    assert "# test room" in text
    assert "DP1: does the mechanism generalize?" in text
    assert "77.0%" in text
    assert "opening position" in text
    assert "(no turns yet)" in text  # DP2, never posted to


def test_render_room_markdown_includes_freeze_reason(store):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="deadlocked")
    text = render_room_markdown(store, room["room_id"])
    assert "## Freeze" in text
    assert "deadlocked" in text


def test_export_room_writes_file_atomically(store, tmp_path):
    launch_id = bootstrap_launch(store)
    room = _room(store)
    post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="hello room doc")
    out_path = tmp_path / "room_doc.md"
    result = export_room(store, room["room_id"], out_path=out_path)
    assert result["room_id"] == room["room_id"]
    assert out_path.is_file()
    text = out_path.read_text(encoding="utf-8")
    assert "hello room doc" in text
    assert result["bytes"] == len(text.encode("utf-8"))
