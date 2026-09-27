"""``trialerror room close`` — frozen -> closed on an operator decision.

The API (``trialerror.rooms.api.close_room``), the CLI verb, and every reader
of ``room.state`` that has to read a closed room sensibly: the dashboard's
DECIDE queue (a closed room leaves it), its rooms panel, its session
timeline, its since-you-left feed, the room doc, and the rooms doctor checks.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

import pytest

from trialerror.cli import main
from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.rooms.api import (
    close_room,
    converge_room,
    create_room,
    freeze_room,
    get_close_record,
    get_freeze_reason,
    get_room,
    list_room_turns,
    post_message,
    render_room_markdown,
    score_dp,
)
from trialerror.rooms.errors import IllegalRoomTransitionError
from trialerror.stores import insert
from trialerror.stores.errors import XidTargetMissingError
from trialerror.stores.store import open_store
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now_dt

from tests._rooms_fixtures import bootstrap_launch
from tests._store_fixtures import populate_one_of_everything

DECISION = "DECISION-TEST-1"


def _room(store, launch_id):
    return create_room(
        store,
        topic="a trial room",
        discussion_points=[{"prompt": "does it hold?"}],
        participants=["P1", "P2"],
        by_launch=launch_id,
    )


def _frozen_room(store, launch_id):
    room = _room(store, launch_id)
    post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="a first position")
    freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="the trial ended")
    return room


def _events(store, room_id):
    return [
        (r["type"], json.loads(r["payload"]))
        for r in store.ops.execute(
            "SELECT type, payload FROM event WHERE json_extract(payload, '$.room_id') = ? ORDER BY ts, rowid",
            (room_id,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------


def test_close_moves_a_frozen_room_to_closed_and_records_the_decision(store):
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    row = close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="it was a test", decided_by=DECISION)
    assert row["state"] == "closed"
    record = get_close_record(store, room["room_id"])
    assert record["reason"] == "it was a test"
    assert record["decided_by"] == DECISION
    assert record["launch_id"] == launch_id
    closed = [p for t, p in _events(store, room["room_id"]) if t == "room_closed"]
    assert closed == [
        {"room_id": room["room_id"], "from_state": "frozen", "reason": "it was a test", "decided_by": DECISION}
    ]


def test_close_is_append_only(store):
    """Nothing earlier is rewritten or deleted: the turns, the freeze event
    and its reason are as they were, and exactly one event is added."""
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    before_events = _events(store, room["room_id"])
    before_turns = list_room_turns(store, room_id=room["room_id"])
    before_row = get_room(store, room["room_id"])
    close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="it was a test", decided_by=DECISION)
    after_events = _events(store, room["room_id"])
    assert after_events[: len(before_events)] == before_events
    assert [t for t, _ in after_events[len(before_events):]] == ["room_closed"]
    assert list_room_turns(store, room_id=room["room_id"]) == before_turns
    assert get_freeze_reason(store, room["room_id"]) == "the trial ended"
    after_row = get_room(store, room["room_id"])
    assert {k: v for k, v in after_row.items() if k != "state"} == {k: v for k, v in before_row.items() if k != "state"}


@pytest.mark.parametrize("state", ["open", "converged", "closed"])
def test_close_refuses_any_room_that_is_not_frozen(store, state):
    launch_id = bootstrap_launch(store)
    room = _room(store, launch_id)
    if state == "converged":
        score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda _e: 95.0, by_launch=launch_id)
        converge_room(store, room_id=room["room_id"], by_launch=launch_id)
    elif state == "closed":
        freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="stuck")
        close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="first close", decided_by=DECISION)
    closes_before = [t for t, _ in _events(store, room["room_id"])].count("room_closed")
    with pytest.raises(IllegalRoomTransitionError):
        close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="again", decided_by=DECISION)
    assert get_room(store, room["room_id"])["state"] == state
    assert [t for t, _ in _events(store, room["room_id"])].count("room_closed") == closes_before


@pytest.mark.parametrize("field,value", [("reason", ""), ("reason", "   "), ("decided_by", ""), ("decided_by", "  ")])
def test_close_refuses_without_a_reason_or_a_decision(store, field, value):
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    kwargs = {"reason": "it was a test", "decided_by": DECISION, field: value}
    with pytest.raises(ValueError, match=field):
        close_room(store, room_id=room["room_id"], by_launch=launch_id, **kwargs)
    assert get_room(store, room["room_id"])["state"] == "frozen"
    assert get_close_record(store, room["room_id"]) is None


def test_close_refuses_an_unknown_room_and_an_unknown_launch(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError, match="no such room"):
        close_room(store, room_id="ROOM-TEST-1", by_launch=launch_id, reason="r", decided_by=DECISION)
    room = _frozen_room(store, launch_id)
    with pytest.raises(XidTargetMissingError):
        close_room(store, room_id=room["room_id"], by_launch="LNCH-TEST-MISSING", reason="r", decided_by=DECISION)
    assert get_room(store, room["room_id"])["state"] == "frozen"


def test_a_closed_room_takes_no_turn_and_cannot_be_frozen_again(store):
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="it was a test", decided_by=DECISION)
    with pytest.raises(ValueError, match="not open"):
        post_message(store, room_id=room["room_id"], launch_id=launch_id, dp_id="DP1", body="late")
    with pytest.raises(IllegalRoomTransitionError):
        freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="again")


def test_the_room_doc_shows_the_freeze_and_the_close(store):
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="it was a test", decided_by=DECISION)
    text = render_room_markdown(store, room["room_id"])
    assert "state: **closed**" in text
    assert "## Freeze" in text and "the trial ended" in text
    assert "## Closed" in text and "it was a test" in text and DECISION in text
    assert "a first position" in text


def test_the_rooms_doctor_checks_read_a_closed_room_as_neither_stuck_nor_owing(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="it was a test", decided_by=DECISION)
    discover_and_register_checks()
    results = {
        r.name: r
        for r in run_checks(
            DoctorContext(program_root=program_root, platform_root=platform_root),
            only=["rooms_stuck", "rooms_unregistered_deliverables"],
        )
    }
    assert results["rooms_stuck"].status == "pass"
    assert results["rooms_unregistered_deliverables"].status == "pass"


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


@pytest.fixture()
def seeded_frozen(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    open_room = _room(store, launch_id)
    store.close()
    return program_root, launch_id, room["room_id"], open_room["room_id"]


def test_cli_close_envelope(seeded_frozen):
    program_root, launch_id, room_id, _ = seeded_frozen
    rc, env = _run_cli([
        "room", "close", "--program-root", str(program_root), "--id", room_id, "--reason", "it was a test",
        "--decided-by", DECISION, "--by-launch", launch_id,
    ])
    assert rc == 0, env
    assert env["result"]["room"]["state"] == "closed"
    assert env["result"]["closed"]["decided_by"] == DECISION
    assert env["result"]["closed"]["reason"] == "it was a test"
    rc, env = _run_cli(["room", "status", "--program-root", str(program_root), "--id", room_id])
    assert rc == 0
    assert env["result"]["room"]["state"] == "closed"
    assert env["result"]["closed"]["decided_by"] == DECISION


def test_cli_close_refuses_an_open_room(seeded_frozen):
    program_root, launch_id, _, open_room_id = seeded_frozen
    rc, env = _run_cli([
        "room", "close", "--program-root", str(program_root), "--id", open_room_id, "--reason", "r",
        "--decided-by", DECISION, "--by-launch", launch_id,
    ])
    assert rc != 0
    assert env["error"]["code"] == "close_refused"


@pytest.mark.parametrize("drop", ["--decided-by", "--reason", "--by-launch"])
def test_cli_close_requires_its_flags(seeded_frozen, drop):
    program_root, launch_id, room_id, _ = seeded_frozen
    flags = {"--reason": "r", "--decided-by": DECISION, "--by-launch": launch_id}
    flags.pop(drop)
    argv = ["room", "close", "--program-root", str(program_root), "--id", room_id]
    for k, v in flags.items():
        argv += [k, v]
    with pytest.raises(SystemExit) as exc:
        with redirect_stdout(io.StringIO()):
            main(argv)
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# the dashboard
# ---------------------------------------------------------------------------


def test_a_closed_room_leaves_the_decide_queue_and_reads_as_closed(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    store.close()

    ro = open_store_ro(program_root, platform_root=platform_root)
    try:
        ids = [i["id"] for i in data.build_determinations_panel(ro)["items"] if i["kind"] == "room_escalation"]
        assert room["room_id"] in ids
    finally:
        ro.close()

    store = open_store(program_root, platform_root=platform_root)
    close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="it was a test", decided_by=DECISION)
    store.close()

    ro = open_store_ro(program_root, platform_root=platform_root)
    try:
        ids = [i["id"] for i in data.build_determinations_panel(ro)["items"] if i["kind"] == "room_escalation"]
        assert room["room_id"] not in ids
        panel = data.build_rooms_panel(ro, room_id=room["room_id"])
        assert panel["detail_error"] is None
        assert panel["active_room"]["state"] == "closed"
        # S-2: a closed room is not frozen now -- no freeze_reason for the page's red FROZEN line; the
        # freeze is history (frozen_earlier_reason), and the close is what the header shows
        assert panel["freeze_reason"] is None
        assert panel["frozen_earlier_reason"] == "the trial ended"
        assert panel["close_record"]["decided_by"] == DECISION
        assert panel["close_record"]["reason"] == "it was a test"
        assert "room_closed" in [e["type"] for e in panel["moderator_events"]]
        since = data.build_since_you_left_panel(ro, since="2000-01-01T00:00:00.000Z")
        summaries = [i["summary"] for i in since["items"]]
        assert any("closed by operator decision" in s and DECISION in s for s in summaries)
    finally:
        ro.close()


def test_the_session_timeline_settles_a_closed_room(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    room_id = new_id("ROOM")
    base = now_dt()
    for step, (event_type, payload) in enumerate((
        ("room_created", {"room_id": room_id, "question": "does it hold?"}),
        ("room_frozen", {"room_id": room_id}),
        ("room_closed", {"room_id": room_id, "decided_by": DECISION}),
    )):
        ts = (base + timedelta(milliseconds=step)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        insert(
            store, "event",
            {"event_id": new_id("EVT"), "ts": ts, "session_id": ids["session"], "type": event_type,
             "payload": json.dumps(payload)},
        )
    store.close()
    ro = open_store_ro(program_root, platform_root=platform_root)
    try:
        spans = data.build_session_panel(ro)["open_session"]["timeline"]["spans"]
    finally:
        ro.close()
    room_span = next(s for s in spans if s["kind"] == "room")
    assert room_span["status"] == "closed"
    assert room_span["end_ts"] is not None


def test_a_frozen_room_keeps_its_freeze_reason_and_has_no_close_record(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    launch_id = bootstrap_launch(store)
    room = _frozen_room(store, launch_id)
    store.close()
    ro = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_rooms_panel(ro, room_id=room["room_id"])
    finally:
        ro.close()
    assert panel["freeze_reason"] == "the trial ended"
    assert panel["frozen_earlier_reason"] is None
    assert panel["close_record"] is None


# ---------------------------------------------------------------------------
# S-2: the page's own header lines, the shipped source run under node
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = REPO_ROOT / "trialerror" / "dashboard" / "static" / "dashboard.html"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _room_header_notes(panel):
    """Run the SHIPPED ``roomHeaderNotes`` (extracted from dashboard.html's inline script) on one panel."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    m = re.search(r"\n  function roomHeaderNotes\(p\) \{\n.*?\n  \}\n", html, re.S)
    assert m is not None, "dashboard.html has no roomHeaderNotes(p)"
    script = m.group(0) + "\nprocess.stdout.write(JSON.stringify(roomHeaderNotes(" + json.dumps(panel) + ")));"
    proc = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@requires_node
def test_the_page_heads_a_closed_room_closed_in_the_settled_colour():
    notes = _room_header_notes({
        "freeze_reason": None,
        "frozen_earlier_reason": "the trial ended",
        "close_record": {"reason": "it was a test", "decided_by": DECISION, "ts": "t", "launch_id": "L"},
    })
    assert notes == [
        {"tone": "settled", "text": f"CLOSED: it was a test (decided by {DECISION})"},
        {"tone": "plain", "text": "frozen earlier: the trial ended"},
    ]
    assert not any("FROZEN" in n["text"] for n in notes)


@requires_node
def test_the_page_still_heads_a_frozen_room_frozen_in_the_critical_colour():
    notes = _room_header_notes({"freeze_reason": "stuck", "frozen_earlier_reason": None, "close_record": None})
    assert notes == [{"tone": "crit", "text": "FROZEN: stuck"}]
    assert _room_header_notes({"freeze_reason": None, "frozen_earlier_reason": None, "close_record": None}) == []


def test_the_header_renders_each_note_in_its_tone():
    """Structural: the header takes its lines from roomHeaderNotes and maps each tone to a colour, and no
    other code in renderRooms prints the freeze reason."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    body = html[html.index("function renderRooms(p)"):]
    body = body[: body.index("\n  function ", 1)]
    assert "roomHeaderNotes(p).forEach" in body
    assert "p.freeze_reason" not in body
    assert '"var(--settled)"' in html and '"var(--crit-text)"' in html


def test_the_guide_describes_the_closed_room_view():
    guide = (REPO_ROOT / "docs" / "OPERATOR_GUIDE.md").read_text(encoding="utf-8")
    assert "CLOSED: <reason> (decided by <ref>)" in guide
    assert "frozen earlier" in guide
