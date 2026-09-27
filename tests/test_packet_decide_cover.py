"""``packet build`` leaves out a raw DECIDE entry that an open item of our own
covers: the item carries a ``--ref`` naming the entry's id (``DECIDE:<id>`` or
the bare ``<id>``). The build lists each entry it left out and the item that
covers it; an entry no item covers is printed as before."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest

from trialerror.cli import main
from trialerror.packet import build as pb
from trialerror.packet import store as ps
from trialerror.packet.store import packet_settings
from trialerror.rooms.api import close_room, create_room, freeze_room
from trialerror.stores.store import open_store

from tests._rooms_fixtures import bootstrap_launch

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def _decide(entry_id, what):
    return {
        "id": f"DECIDE:{entry_id}", "source": "decide", "label": pb.DECIDE_LABEL, "what": what, "why": "",
        "consequence": "It waits.", "options": [], "recommended": None, "if_undecided": "",
        "needed_by": "next-session", "est_minutes": pb.DECIDE_MINUTES, "priority": "blocking",
        "refs": [{"label": "room_escalation", "ref": entry_id}], "answerable": False,
    }


RAW_ROOM = _decide("ROOM-TEST-1", "raw room entry")
RAW_EDIT = _decide("CR-001::EDIT-1", "raw edit entry")


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    root = tmp_path / "prog"
    root.mkdir()
    (root / "trialerror.toml").write_text('[program]\nid = "demo"\n', encoding="utf-8")
    monkeypatch.setattr(pb, "_decide_entries", lambda _settings, _platform_root: ([dict(RAW_ROOM), dict(RAW_EDIT)], None))
    return packet_settings(root)


def _add(settings, what, refs, *, priority="normal"):
    raw = {
        "what": what, "why": "It unblocks the next step.",
        "options": [
            {"key": "a", "label": "Close it", "consequence": "It leaves the queue."},
            {"key": "b", "label": "Keep it", "consequence": "It stays."},
        ],
        "recommended": "a", "if_undecided": "It stays.", "needed_by": "next-session", "priority": priority,
        "refs": [{"label": label, "ref": ref} for label, ref in refs],
    }
    row, _ = ps.add_item(settings, raw, now=T0)
    return row


def _build(settings, **kw):
    return pb.build_packet(settings, "manual", dry_run=True, now=T0, **kw)


@pytest.mark.parametrize("ref", ["DECIDE:ROOM-TEST-1", "ROOM-TEST-1", "  ROOM-TEST-1  "])
def test_an_item_whose_ref_names_the_entry_covers_it(settings, ref):
    own = _add(settings, "Close the trial room from the first round?", [("the trial room", ref)])
    result = _build(settings)
    packet = result["packet"]
    ids = [i["id"] for i in packet["items"]] + [w["id"] for w in packet["waiting"]]
    assert "DECIDE:ROOM-TEST-1" not in ids
    assert "DECIDE:CR-001::EDIT-1" in ids  # not covered: printed as before
    assert own["id"] in ids
    assert packet["decide_covered"] == [{"id": "DECIDE:ROOM-TEST-1", "covered_by": own["id"]}]
    assert "raw room entry" not in result["markdown"]
    assert "raw edit entry" in result["markdown"]
    assert "**Also answers.** DECIDE:ROOM-TEST-1" in result["markdown"]


def test_the_minutes_count_only_what_is_printed(settings):
    _add(settings, "Close the trial room?", [("the trial room", "ROOM-TEST-1")])
    covered = _build(settings)["packet"]["minutes"]
    ps.withdraw_item(settings, ps.list_items(settings, open_only=True)["open"][0]["id"], "test")
    _add(settings, "Close the trial room?", [("a note", "notes/room.md")])
    uncovered = _build(settings)["packet"]["minutes"]
    assert uncovered - covered == pb.DECIDE_MINUTES


def test_an_edit_id_with_a_double_colon_is_covered_through_the_cli_ref(settings):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([
            "packet", "add", "--program-root", str(settings.program_root),
            "--what", "Accept the edit to the tally?", "--why", "The report waits for it.",
            "--option", "a=Accept::The report goes on.", "--option", "b=Refuse::The critic is asked again.",
            "--recommend", "a", "--needed-by", "next-session", "--if-undecided", "It waits.",
            "--ref", "the critic's edit to the tally::CR-001::EDIT-1",
        ])
    assert rc == 0, buf.getvalue()
    own_id = json.loads(buf.getvalue())["result"]["item"]["id"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["packet", "build", "--program-root", str(settings.program_root), "--trigger", "manual", "--dry-run"])
    assert rc == 0
    packet = json.loads(buf.getvalue())["result"]["packet"]
    assert packet["decide_covered"] == [{"id": "DECIDE:CR-001::EDIT-1", "covered_by": own_id}]
    assert "DECIDE:ROOM-TEST-1" in [i["id"] for i in packet["items"]]


@pytest.mark.parametrize("ref", ["ROOM-TEST-10", "ROOM-TEST", "DECIDE:ROOM-TEST-1x", "docs/ROOM-TEST-1.md"])
def test_only_an_exact_id_covers(settings, ref):
    _add(settings, "Something near it?", [("a look-alike", ref)])
    packet = _build(settings)["packet"]
    assert packet["decide_covered"] == []
    assert "DECIDE:ROOM-TEST-1" in [i["id"] for i in packet["items"]]


def test_an_answered_or_withdrawn_item_covers_nothing(settings):
    answered = _add(settings, "Close the trial room?", [("the trial room", "ROOM-TEST-1")])
    ps.answer_item(settings, answered["id"], "a")
    withdrawn = _add(settings, "Close the trial room again?", [("the trial room", "ROOM-TEST-1")])
    ps.withdraw_item(settings, withdrawn["id"], "asked twice")
    packet = _build(settings)["packet"]
    assert packet["decide_covered"] == []
    assert "DECIDE:ROOM-TEST-1" in [i["id"] for i in packet["items"]]


def test_the_first_item_in_packet_order_is_named_when_two_cover_one_entry(settings):
    _add(settings, "A normal one?", [("the trial room", "ROOM-TEST-1")])
    blocking = _add(settings, "A blocking one?", [("the trial room", "DECIDE:ROOM-TEST-1")], priority="blocking")
    packet = _build(settings)["packet"]
    assert packet["decide_covered"] == [{"id": "DECIDE:ROOM-TEST-1", "covered_by": blocking["id"]}]


def test_nothing_covered_prints_every_entry_as_before(settings):
    _add(settings, "An unrelated question?", [])
    packet = _build(settings)["packet"]
    assert packet["decide_covered"] == []
    decide = [i for i in packet["items"] if i["source"] == "decide"]
    assert [i["id"] for i in decide] == ["DECIDE:ROOM-TEST-1", "DECIDE:CR-001::EDIT-1"]
    assert all(i["label"] == pb.DECIDE_LABEL for i in decide)


# ---------------------------------------------------------------------------
# end to end: a real frozen room in a temporary store
# ---------------------------------------------------------------------------


def test_a_frozen_room_the_item_explains_is_left_out_and_closing_it_empties_the_queue(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    launch_id = bootstrap_launch(store)
    room = create_room(
        store, topic="a trial room", discussion_points=[{"prompt": "does it hold?"}], participants=["P1", "P2"],
        by_launch=launch_id,
    )
    freeze_room(store, room_id=room["room_id"], by_launch=launch_id, reason="the trial ended")
    store.close()
    settings = packet_settings(program_root)

    packet = _build(settings, platform_root=platform_root)["packet"]
    raw_id = f"DECIDE:{room['room_id']}"
    assert raw_id in [i["id"] for i in packet["items"]]  # no item explains it yet: printed as today

    own = _add(settings, "Close the trial room from the first round?", [("the trial room", room["room_id"])])
    packet = _build(settings, platform_root=platform_root)["packet"]
    assert raw_id not in [i["id"] for i in packet["items"]]
    assert packet["decide_covered"] == [{"id": raw_id, "covered_by": own["id"]}]

    store = open_store(program_root, platform_root=platform_root)
    close_room(store, room_id=room["room_id"], by_launch=launch_id, reason="it was a test", decided_by="DECISION-TEST-1")
    store.close()
    ps.answer_item(settings, own["id"], "a")
    packet = _build(settings, platform_root=platform_root)["packet"]
    assert raw_id not in [i["id"] for i in packet["items"]]
    assert packet["decide_covered"] == []


# ---------------------------------------------------------------------------
# S-1: under the minutes cap a covered blocking entry never drops out
# ---------------------------------------------------------------------------


def _capped(tmp_path, monkeypatch, max_minutes, entries):
    root = tmp_path / f"capped{max_minutes}"
    root.mkdir()
    (root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n[packet]\nmax_minutes = {max_minutes}\n', encoding="utf-8"
    )
    monkeypatch.setattr(pb, "_decide_entries", lambda _s, _p: ([dict(e) for e in entries], None))
    return packet_settings(root)


FOUR_ROOMS = [_decide(f"ROOM-TEST-{i}", f"raw room {i}") for i in range(1, 5)]


def test_a_covered_entry_whose_item_is_cut_by_the_cap_goes_back_in_its_place(tmp_path, monkeypatch):
    """The review's case (a): a cap of 10 minutes, four blocking entries of 3 minutes, and a normal item that
    covers the first. The item comes after every entry, so the cap cuts it; the entry it covers must then be
    printed where it would have been without the item, not dropped from the packet altogether."""
    settings = _capped(tmp_path, monkeypatch, 10, FOUR_ROOMS)
    without = [i["id"] for i in _build(settings)["packet"]["items"]]
    own = _add(settings, "Close the first trial room?", [("the room", "DECIDE:ROOM-TEST-1")])
    result = _build(settings)
    packet = result["packet"]
    assert [i["id"] for i in packet["items"]] == without == [
        "DECIDE:ROOM-TEST-1", "DECIDE:ROOM-TEST-2", "DECIDE:ROOM-TEST-3",
    ]
    assert [w["id"] for w in packet["waiting"]] == ["DECIDE:ROOM-TEST-4", own["id"]]
    assert packet["decide_covered"] == []
    assert "raw room 1" in result["markdown"]
    assert packet["minutes"] == 9


def test_a_covered_entry_is_left_out_when_its_item_fits_under_the_cap(tmp_path, monkeypatch):
    settings = _capped(tmp_path, monkeypatch, 10, FOUR_ROOMS)
    own = _add(settings, "Close the first trial room?", [("the room", "ROOM-TEST-1")], priority="blocking")
    packet = _build(settings)["packet"]
    assert [i["id"] for i in packet["items"]] == [own["id"], "DECIDE:ROOM-TEST-2", "DECIDE:ROOM-TEST-3"]
    assert [w["id"] for w in packet["waiting"]] == ["DECIDE:ROOM-TEST-4"]
    assert packet["decide_covered"] == [{"id": "DECIDE:ROOM-TEST-1", "covered_by": own["id"]}]


def test_every_blocking_entry_is_in_the_packet_or_waiting_whatever_covers_it(tmp_path, monkeypatch):
    settings = _capped(tmp_path, monkeypatch, 6, FOUR_ROOMS)
    _add(settings, "Close rooms 1 and 2?", [("room 1", "ROOM-TEST-1"), ("room 2", "ROOM-TEST-2")])
    _add(settings, "Close room 3?", [("room 3", "ROOM-TEST-3")], priority="low")
    packet = _build(settings)["packet"]
    shown = [i["id"] for i in packet["items"]] + [w["id"] for w in packet["waiting"]]
    covered = {c["id"] for c in packet["decide_covered"]}
    for entry in FOUR_ROOMS:
        assert entry["id"] in shown or entry["id"] in covered
    included = {i["id"] for i in packet["items"]}
    assert all(c["covered_by"] in included for c in packet["decide_covered"])
