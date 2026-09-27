"""A lens is a NAME with several launches: seat identity in rooms, and
launch bindings that accumulate instead of overwriting (lane R0-B).

Two things broke the first time a round spawned one agent per turn / per
phase, so one lens or one room seat held SEVERAL launches:

- **rooms** counted an author by lens name where the booking declared one,
  but the only way to declare one was the lens-export path. A seat that is
  not a roster lens of the round had no name, so its round-2 turn -- posted
  under a fresh launch -- read as a different author and its ``closure`` was
  refused with "this author has not spoken on it yet".
- **bindings overwrote**: ``book_launch(assign_ids=...)`` wrote
  ``lens_assignment.lens_launch_id`` unconditionally, so a lens's second
  booking moved the join its first launch's records and feed post hang off.
  Booking the second launch without assign ids kept that join and left the
  second launch with no slice binding at all.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from trialerror.budget.errors import LensNameRefusedError
from trialerror.budget.pools import book_launch
from trialerror.cli import main
from trialerror.events.api import create_thread, post_feed
from trialerror.lens.checks import check_lens_citations_within_slice
from trialerror.lens.link import assign_ids_for_launch
from trialerror.retrieve.engine import launch_slice_doc_ids
from trialerror.rooms.api import (
    build_participant_turn_envelope,
    create_room,
    lens_name_of_launch,
    list_final_stances,
    list_room_turns,
    post_final_stance,
    post_message,
)
from trialerror.rooms.errors import (
    OwnershipConflictError,
    TurnKindRefusedError,
    UnseatedParticipantError,
)
from trialerror.stores import get, insert
from trialerror.stores.schema import ops
from trialerror.stores.store import open_store
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

ROUND_ID = "round-names"


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _open_session(store) -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    return session_id


@pytest.fixture()
def session_id(store) -> str:
    return _open_session(store)


def _book(store, session_id: str, **kwargs):
    booking = {
        "agent_kind": "lens", "model_class": "top", "model": "sonnet",
        "purpose": "ideation", "est_tokens": 100,
    }
    booking.update(kwargs)
    return book_launch(store, session_id=session_id, program_id="PROG-test", **booking)


def _seed_two_documents(store, *, launch_id: str) -> tuple[str, str]:
    """A source and two documents, so the citation audit can resolve a cited
    id back to a document that is (or is not) in the slice. Real minted ids:
    the audit only recognises a citation whose body is a ULID, so a readable
    placeholder would make every one of these tests pass by citing
    nothing."""
    insert(
        store, "source",
        {"source_id": new_id("SRC"), "kind": "paper", "title": "s", "license_tier": "open",
         "acquisition_route": "web", "request_state": "indexed", "registered_ts": now(),
         "registered_by_launch": launch_id},
    )
    source_id = store.knowledge.execute("SELECT source_id FROM source").fetchone()["source_id"]
    doc_ids = []
    for _ in range(2):
        doc_id = new_id("DOC")
        insert(
            store, "document",
            {"doc_id": doc_id, "source_id": source_id, "rel_path": f"archive/{doc_id}.md",
             "media_type": "text/markdown", "normalizer_id": "n", "normalizer_version": "1",
             "sha256": doc_id, "status": "indexed"},
        )
        doc_ids.append(doc_id)
    return doc_ids[0], doc_ids[1]


def _seed_lens(store, *, lens_name: str, doc_ids, roster_id: str | None = None) -> tuple[str, list[str]]:
    """One roster row plus one ``lens_assignment`` row per document, written
    directly -- this module never touches the seeded draw."""
    roster_id = roster_id or new_id("ROST")
    insert(
        store, "lens_roster",
        {"roster_id": roster_id, "round_id": ROUND_ID, "lens_name": lens_name, "vantage": "v",
         "seat": "standard", "model_class": "top", "created_ts": now()},
    )
    assign_ids = []
    for doc_id in doc_ids:
        assign_id = new_id("ASGN")
        insert(
            store, "lens_assignment",
            {"assign_id": assign_id, "roster_id": roster_id,
             "slice_spec": json.dumps({"candidate_id": doc_id, "arm": "near"}),
             "arm": "near", "seed": "s", "created_ts": now()},
        )
        assign_ids.append(assign_id)
    return roster_id, assign_ids


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


def _attrs(store, launch_id: str) -> dict:
    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    return json.loads(row["attrs"]) if row and row["attrs"] else {}


# ---------------------------------------------------------------------------
# 1 · a declared name at booking
# ---------------------------------------------------------------------------


def test_a_declared_name_is_recorded_on_the_launch(store, session_id):
    result = _book(store, session_id, lens_name="seat-1")
    assert result.ok
    assert _attrs(store, result.launch_id)["lens_name"] == "seat-1"
    assert lens_name_of_launch(store, result.launch_id) == "seat-1"


def test_a_booking_that_declares_nothing_is_unchanged(store, session_id):
    result = _book(store, session_id)
    assert result.ok
    row = get(store, "launch", pk_column="launch_id", pk_value=result.launch_id)
    assert row["attrs"] is None
    assert lens_name_of_launch(store, result.launch_id) is None


def test_a_name_matching_the_assignment_rows_is_accepted(store, session_id):
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A"])
    result = _book(store, session_id, assign_ids=assign_ids, lens_name="lens-a")
    assert result.ok
    assert _attrs(store, result.launch_id)["lens_name"] == "lens-a"


def test_a_name_disagreeing_with_the_assignment_rows_writes_no_launch_row(store, session_id):
    """Probe (b). The assignment rows are what the seeded draw wrote, so a
    launch claiming one lens while holding another's slice is refused --
    and refused BEFORE the launch row exists, so nothing is left holding
    pool headroom."""
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A"])
    before = store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"]
    with pytest.raises(LensNameRefusedError) as excinfo:
        _book(store, session_id, assign_ids=assign_ids, lens_name="lens-b")
    assert "lens-a" in str(excinfo.value) and "lens-b" in str(excinfo.value)
    assert store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"] == before
    assert store.ops.execute(
        "SELECT COUNT(*) AS n FROM lens_assignment_launch"
    ).fetchone()["n"] == 0


@pytest.mark.parametrize("bad", ["", "   ", "x" * 121, "two\nlines"])
def test_a_name_that_cannot_be_a_name_is_refused(store, session_id, bad):
    with pytest.raises(LensNameRefusedError):
        _book(store, session_id, lens_name=bad)


def test_a_name_is_stripped_but_otherwise_the_programmes_own(store, session_id):
    result = _book(store, session_id, lens_name="  seat-1  ")
    assert _attrs(store, result.launch_id)["lens_name"] == "seat-1"


def test_a_phase_without_assign_ids_is_refused(store, session_id):
    """``phase`` labels link rows, and a booking that binds nothing has
    none -- so the combination is refused rather than quietly dropped."""
    with pytest.raises(LensNameRefusedError, match="phase"):
        _book(store, session_id, phase="derivation")


def test_the_cli_declares_a_name_and_refuses_a_disagreeing_one(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    session = _open_session(store)
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A"])
    store.close()
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    book = [
        "budget", *common, "book", "--session-id", session, "--program-id", "PROG-test",
        "--agent-kind", "lens", "--model-class", "mid", "--model", "sonnet",
        "--purpose", "mechanical", "--est-tokens", "100",
    ]

    rc, env = _run_cli([*book, "--lens-name", "seat-1"])
    assert rc == 0, env
    launch_id = env["result"]["launch_id"]

    rc, env = _run_cli([*book, "--assign-id", assign_ids[0], "--lens-name", "lens-b"])
    assert rc == 1
    assert env["error"]["code"] == "lens_name_refused"

    rc, env = _run_cli([*book, "--phase", "derivation"])
    assert rc == 1 and env["error"]["code"] == "lens_name_refused"

    store = open_store(program_root, platform_root=platform_root)
    try:
        assert lens_name_of_launch(store, launch_id) == "seat-1"
        assert store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"] == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2 · rooms honour the declared name
# ---------------------------------------------------------------------------


SEATS = ["seat-1", "seat-2", "seat-3"]


def _room(store, **kwargs):
    return create_room(
        store, topic="t", participants=SEATS,
        discussion_points=[{"dp_id": "DP1", "prompt": "p"}, {"dp_id": "DP2", "prompt": "q"}],
        **kwargs,
    )


def test_one_seat_two_launches_is_one_author_across_its_turns(store, session_id):
    """The finding, closed: the round-2 turn is posted under its OWN launch
    (the true writer) and is still the same seat's second turn."""
    room = _room(store)
    l1 = _book(store, session_id, lens_name="seat-1").launch_id
    l2 = _book(store, session_id, lens_name="seat-1").launch_id
    assert l1 != l2

    post_message(store, room_id=room["room_id"], launch_id=l1, dp_id="DP1", body="my position", kind="position")
    turn = post_message(
        store, room_id=room["room_id"], launch_id=l2, dp_id="DP1", body="closing it", kind="closure"
    )
    assert turn["author_launch"] == l2

    turns = list_room_turns(store, room_id=room["room_id"], dp_id="DP1")
    assert [t["author_launch"] for t in turns] == [l1, l2]


def test_a_launch_with_no_name_is_still_refused_a_first_round_closure(store, session_id):
    room = _room(store)
    anonymous = _book(store, session_id).launch_id
    with pytest.raises(TurnKindRefusedError, match="round 1"):
        post_message(
            store, room_id=room["room_id"], launch_id=anonymous, dp_id="DP1", body="done", kind="closure"
        )


def test_the_blind_first_turn_envelope_reads_the_seat_not_the_launch(store, session_id):
    """A seat that has spoken is past its blind turn even when its next
    launch is a different row."""
    room = _room(store, blind_first_turn=True)
    l1 = _book(store, session_id, lens_name="seat-1").launch_id
    post_message(store, room_id=room["room_id"], launch_id=l1, dp_id="DP1", body="mine", kind="position")

    spoken = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1", participant="seat-1")
    assert spoken["blinded"] is False and spoken["prior_turns"]
    silent = build_participant_turn_envelope(store, room_id=room["room_id"], dp_id="DP1", participant="seat-2")
    assert silent["blinded"] is True and silent["prior_turns"] == []


def test_the_final_stance_path_takes_the_seats_later_launch(store, session_id):
    room = _room(store, rank_all=True)
    l1 = _book(store, session_id, lens_name="seat-1").launch_id
    l2 = _book(store, session_id, lens_name="seat-1").launch_id
    post_message(store, room_id=room["room_id"], launch_id=l1, dp_id="DP1", body="mine", kind="position")
    ranking = {"a": ["DP1", "DP2"], "b": ["DP2", "DP1"]}
    stances = {"DP1": {"a": "yes", "b": "yes"}, "DP2": {"a": "yes", "b": "yes"}}
    record = post_final_stance(
        store, room_id=room["room_id"], launch_id=l2, participant="seat-1",
        ranking=ranking, stances=stances,
    )
    assert record["participant"] == "seat-1"
    filed = list_final_stances(store, room_id=room["room_id"])
    assert list(filed) == ["seat-1"]
    assert filed["seat-1"]["launch_id"] == l2


def test_a_name_the_room_does_not_seat_is_refused_by_name(store, session_id):
    """The gap this lane closes: the room counts turns by the declared name,
    so a name nobody seated would post as an author the room does not
    have."""
    room = _room(store)
    stranger = _book(store, session_id, lens_name="seat-9").launch_id
    with pytest.raises(UnseatedParticipantError) as excinfo:
        post_message(store, room_id=room["room_id"], launch_id=stranger, dp_id="DP1", body="hello")
    message = str(excinfo.value)
    assert "seat-9" in message and "seat-1" in message and "seat-3" in message
    assert list_room_turns(store, room_id=room["room_id"]) == []


def test_a_launch_declaring_no_name_is_not_judged_on_seating(store, session_id):
    room = _room(store)
    anonymous = _book(store, session_id).launch_id
    turn = post_message(store, room_id=room["room_id"], launch_id=anonymous, dp_id="DP1", body="hi")
    assert turn["author_launch"] == anonymous


def test_a_room_that_seats_nobody_judges_no_name(store, session_id):
    room = create_room(
        store, topic="t", participants=[], enforce_participant_range=False,
        discussion_points=[{"dp_id": "DP1", "prompt": "p"}],
    )
    stranger = _book(store, session_id, lens_name="seat-9").launch_id
    turn = post_message(store, room_id=room["room_id"], launch_id=stranger, dp_id="DP1", body="hi")
    assert turn["author_launch"] == stranger


def test_neither_ownership_still_fires_on_a_declared_name(store, session_id):
    """A launch booked ``--lens-name X`` is refused on a point vetting an
    idea whose AUTHOR launch carries lens name X -- a different launch row,
    the same lens."""
    author = _book(store, session_id, lens_name="seat-1").launch_id
    idea_id = new_id("IDEA")
    insert(
        store, "idea",
        {"idea_id": idea_id, "round_id": ROUND_ID, "author_launch": author, "body": "mine",
         "status": "raw", "created_ts": now()},
    )
    room = create_room(
        store, topic="t", participants=["seat-2", "seat-3"],
        discussion_points=[{"dp_id": "DP1", "prompt": "p", "idea_id": idea_id}],
    )
    respawn = _book(store, session_id, lens_name="seat-1").launch_id
    with pytest.raises(OwnershipConflictError, match="checked by lens name"):
        post_message(store, room_id=room["room_id"], launch_id=respawn, dp_id="DP1", body="mine is great")


def test_the_room_cli_accepts_a_seats_second_launch_and_refuses_a_stranger(program_root, platform_root):
    """The same two facts through the CLI, which is how a round actually
    posts its turns."""
    store = open_store(program_root, platform_root=platform_root)
    session = _open_session(store)
    store.close()
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]

    def book(name: str) -> str:
        rc, env = _run_cli([
            "budget", *common, "book", "--session-id", session, "--program-id", "PROG-test",
            "--agent-kind", "lens", "--model-class", "mid", "--model", "sonnet",
            "--purpose", "mechanical", "--est-tokens", "100", "--lens-name", name,
        ])
        assert rc == 0, env
        return env["result"]["launch_id"]

    l1, l2, stranger = book("seat-1"), book("seat-1"), book("seat-9")
    pr = ["--program-root", str(program_root)]
    rc, env = _run_cli([
        "room", "create", *pr, "--topic", "cli room", "--participants", ",".join(SEATS),
        "--dps", json.dumps([{"dp_id": "DP1", "prompt": "p"}]),
    ])
    assert rc == 0, env
    room_id = env["result"]["room_id"]

    rc, env = _run_cli(
        ["room", "post", *pr, "--id", room_id, "--launch-id", l1, "--dp", "DP1",
         "--body", "my position", "--kind", "position"]
    )
    assert rc == 0, env
    rc, env = _run_cli(
        ["room", "post", *pr, "--id", room_id, "--launch-id", l2, "--dp", "DP1",
         "--body", "closing it", "--kind", "closure"]
    )
    assert rc == 0, env
    assert env["result"]["author_launch"] == l2

    rc, env = _run_cli(
        ["room", "post", *pr, "--id", room_id, "--launch-id", stranger, "--dp", "DP1", "--body", "hi"]
    )
    assert rc == 1
    assert env["error"]["code"] == "post_refused"
    assert "does not seat" in env["error"]["message"]


# ---------------------------------------------------------------------------
# 3 · bindings accumulate
# ---------------------------------------------------------------------------


def test_the_migration_is_the_next_contiguous_one(store):
    versions = [m.version for m in ops.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    v11 = next(m for m in ops.MIGRATIONS if m.version == 11)
    assert v11.name == "ops_v11_lens_assignment_launch"
    columns = {r["name"] for r in store.ops.execute("PRAGMA table_info(lens_assignment_launch)")}
    assert columns == {"assign_id", "launch_id", "phase", "bound_ts"}


def test_the_first_binding_wins_and_is_never_overwritten(store, session_id):
    """Probe (a)'s target. The column is the join a lens's records and its
    feed post hang off; a second booking used to move it."""
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A", "DOC-B"])
    first = _book(store, session_id, assign_ids=assign_ids, lens_name="lens-a").launch_id
    second = _book(store, session_id, assign_ids=assign_ids, lens_name="lens-a", phase="derivation").launch_id

    held = {
        row["assign_id"]: row["lens_launch_id"]
        for row in store.ops.execute("SELECT assign_id, lens_launch_id FROM lens_assignment")
    }
    assert set(held.values()) == {first}

    links = {
        (row["assign_id"], row["launch_id"]): row["phase"]
        for row in store.ops.execute("SELECT assign_id, launch_id, phase FROM lens_assignment_launch")
    }
    assert len(links) == 2 * len(assign_ids)
    assert links[(assign_ids[0], first)] is None
    assert links[(assign_ids[0], second)] == "derivation"


def test_re_binding_the_same_pair_is_a_no_op(store, session_id):
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A"])
    from trialerror.budget.pools import link_launch_to_assignments

    launch_id = _book(store, session_id, assign_ids=assign_ids).launch_id
    link_launch_to_assignments(store, launch_id=launch_id, assign_ids=assign_ids, phase="again")
    rows = store.ops.execute("SELECT phase FROM lens_assignment_launch").fetchall()
    assert len(rows) == 1 and rows[0]["phase"] is None


def test_a_phase_launch_resolves_to_the_same_slice(store, session_id):
    """The second consequence: the retrieval scope binds the later launch
    to the slice its first one holds, instead of to nothing."""
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A", "DOC-B"])
    first = _book(store, session_id, assign_ids=assign_ids).launch_id
    second = _book(store, session_id, assign_ids=assign_ids, phase="derivation").launch_id

    assert sorted(launch_slice_doc_ids(store, first) or []) == ["DOC-A", "DOC-B"]
    assert sorted(launch_slice_doc_ids(store, second) or []) == ["DOC-A", "DOC-B"]
    assert assign_ids_for_launch(store.ops, second) == sorted(assign_ids)


def test_a_refused_booking_binds_nothing(store, session_id):
    """The link rows follow the launch: a booking that was never created
    PROVISIONAL never ran, and a binding pointing at one would claim it
    did."""
    from trialerror.budget.pools import create_pool

    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A"])
    session = get(store, "session", pk_column="session_id", pk_value=session_id)
    create_pool(
        store, account_id=session["account_id"], model_class="top", period="weekly", cap_tokens=10,
    )
    result = _book(store, session_id, assign_ids=assign_ids, est_tokens=10_000_000)
    assert not result.ok
    assert store.ops.execute("SELECT COUNT(*) AS n FROM lens_assignment_launch").fetchone()["n"] == 0


def test_the_citation_audit_sees_a_post_by_the_second_launch(store, program_root, platform_root, session_id):
    """The third consequence, and the one the round audited by hand: a feed
    post made under a lens's DERIVATION launch is judged against the same
    slice as its first."""
    seed = _book(store, session_id).launch_id
    inside, _outside = _seed_two_documents(store, launch_id=seed)
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=[inside])
    first = _book(store, session_id, assign_ids=assign_ids).launch_id
    second = _book(store, session_id, assign_ids=assign_ids, phase="derivation").launch_id

    thread = create_thread(store, title="round thread", launch_id=first)
    post_feed(store, thread_id=thread["thread_id"], body=f"derived from {inside}", launch_id=second)
    store.close()

    ctx = DoctorContext(program_root=program_root, platform_root=platform_root)
    r = check_lens_citations_within_slice(ctx)
    assert r.status == "pass", r.message
    assert r.details["posts_checked"] == 1


def test_a_crossing_by_the_second_launch_is_reported(store, program_root, platform_root, session_id):
    seed = _book(store, session_id).launch_id
    inside, outside = _seed_two_documents(store, launch_id=seed)
    _roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=[inside])
    first = _book(store, session_id, assign_ids=assign_ids).launch_id
    second = _book(store, session_id, assign_ids=assign_ids, phase="derivation").launch_id

    thread = create_thread(store, title="round thread", launch_id=first)
    post_feed(store, thread_id=thread["thread_id"], body=f"but see {outside}", launch_id=second)
    store.close()

    ctx = DoctorContext(program_root=program_root, platform_root=platform_root)
    r = check_lens_citations_within_slice(ctx)
    assert r.status == "fail"
    assert r.details["offenders"][0]["launch_id"] == second


def test_lens_log_counts_both_of_a_lenss_launches(store, session_id):
    from trialerror.lens.export import lens_log

    roster_id, assign_ids = _seed_lens(store, lens_name="lens-a", doc_ids=["DOC-A"])
    first = _book(store, session_id, assign_ids=assign_ids).launch_id
    second = _book(store, session_id, assign_ids=assign_ids, phase="derivation").launch_id
    row = lens_log(store, round_id=ROUND_ID)["rows"][0]
    assert row["roster_id"] == roster_id
    assert sorted(row["launch_ids"]) == sorted([first, second])


# ---------------------------------------------------------------------------
# the migration's own backfill (probe (c))
# ---------------------------------------------------------------------------


def test_the_backfill_gives_every_bound_row_a_link_row(tmp_path):
    """Run the v1..v10 schema on a fresh file, bind some rows the pre-v11
    way, then apply v11 alone: one link row per non-NULL
    ``lens_launch_id``, and nothing else."""
    from trialerror.stores.connection import connect
    from trialerror.stores.migrate import apply_migrations, current_version

    path = tmp_path / "ops.db"
    conn = connect(path)
    apply_migrations(conn, [m for m in ops.MIGRATIONS if m.version <= 10])
    assert current_version(conn) == 10

    conn.execute(
        "INSERT INTO lens_roster (roster_id, round_id, lens_name, vantage, seat, model_class, created_ts) "
        "VALUES ('ROST-1', 'r', 'lens-a', 'v', 'standard', 'top', '2026-01-01T00:00:00Z')"
    )
    for i, launch in enumerate(("LNCH-1", "LNCH-1", "LNCH-2", None)):
        conn.execute(
            "INSERT INTO lens_assignment (assign_id, roster_id, slice_spec, arm, seed, created_ts, "
            "lens_launch_id) VALUES (?, 'ROST-1', '{}', 'near', 's', ?, ?)",
            (f"ASGN-{i}", f"2026-01-0{i + 1}T00:00:00Z", launch),
        )
    conn.commit()
    bound = conn.execute(
        "SELECT COUNT(*) AS n FROM lens_assignment WHERE lens_launch_id IS NOT NULL"
    ).fetchone()["n"]

    # this test is about v11's backfill: migrate exactly to v11, so a later migration does not move the pin
    apply_migrations(conn, [m for m in ops.MIGRATIONS if m.version <= 11])
    assert current_version(conn) == 11
    rows = conn.execute(
        "SELECT assign_id, launch_id, phase, bound_ts FROM lens_assignment_launch ORDER BY assign_id"
    ).fetchall()
    assert len(rows) == bound == 3
    assert [r["launch_id"] for r in rows] == ["LNCH-1", "LNCH-1", "LNCH-2"]
    assert all(r["phase"] is None for r in rows)
    assert [r["bound_ts"] for r in rows] == [
        "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"
    ]
    conn.close()
