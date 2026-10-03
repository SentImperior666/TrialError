"""L10 part C: the packet carries only explained operator decisions. These
tests build a REAL store (not a monkeypatched ``_decide_entries``, unlike
``tests/test_packet_decide_cover.py``) so the whole chain -- DECIDE builders
(Part B) -> ``_decide_entries`` -> the packet -- is exercised end to end,
matching the design's own acceptance test (§7 item 2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from trialerror.packet import build as pb
from trialerror.packet.store import packet_settings
from trialerror.rooms.api import freeze_room
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert as store_insert
from trialerror.stores.writer import update as store_update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def _build(program_root, platform_root, **kw):
    settings = packet_settings(program_root)
    return pb.build_packet(settings, "manual", dry_run=True, platform_root=platform_root, now=T0, **kw)


def test_no_raw_decide_entry_reaches_the_packet(program_root, platform_root):
    """The design's own worked example: the operator's stopped room reads in
    plain words with a resolved ref, and the orchestrator's raw gate_edit
    (blocking, unexplained by any operator wording) never appears at all --
    the central trap this whole lane exists to close."""
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    freeze_room(store, room_id=ids["room"], by_launch=ids["launch"], reason="the round ended")
    store_update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={
            "state": "submitted",
            "edits": json.dumps([{"edit_id": "E1", "text": "fix the tally", "blocking": True, "verified": False}]),
            "critic_launch": ids["launch"], "verdict_ts": now(), "reproduction_status": "unrun",
        },
    )
    store.close()

    result = _build(program_root, platform_root)
    decide = [i for i in result["packet"]["items"] if i["source"] == "decide"]
    assert len(decide) == 1
    room_entry = decide[0]
    assert room_entry["id"] == f"DECIDE:{ids['room']}"
    assert room_entry["what"] == "Close or keep the stopped discussion room 'test room'"
    assert room_entry["options"] and room_entry["recommended"] in {o["key"] for o in room_entry["options"]}
    assert room_entry["priority"] == "blocking"
    # design §4 item 2: the ref reads through describe(), id last in brackets
    ref = room_entry["refs"][0]
    assert ref["resolved"] and "discussion room" in ref["resolved"] and ref["resolved"].endswith(f"[{ids['room']}]")
    assert "test room" in result["markdown"]
    # the orchestrator's gate_edit never becomes a decision, in the payload or the prose
    assert not any("fix the tally" in i.get("what", "") for i in decide)
    assert "fix the tally" not in result["markdown"]


def test_an_unexplained_operator_item_goes_to_needs_explaining(program_root, platform_root, monkeypatch):
    """An operator-owned item missing a required plain-words field is never
    rendered as a decision (design §4 item 1); it is counted instead, and
    the count reaches both the result and the markdown."""
    from trialerror.dashboard import data as dash_data

    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    real_panel = dash_data.build_determinations_panel

    def _broken_panel(rostore):
        panel = real_panel(rostore)
        panel["items"] = [
            {"kind": "acquisition", "id": "SRC-broken", "owner": "operator", "title": "a paper",
             "what": "Deliver the source 'a paper'", "blocking": False},  # missing why/options/recommended/...
        ]
        return panel

    monkeypatch.setattr(dash_data, "build_determinations_panel", _broken_panel)
    result = _build(program_root, platform_root)
    decide = [i for i in result["packet"]["items"] if i["source"] == "decide"]
    assert decide == []
    needs = result["packet"]["needs_explaining"]
    assert len(needs) == 1 and needs[0]["id"] == "SRC-broken" and needs[0]["kind"] == "acquisition"
    assert "why" in needs[0]["reason"]
    assert "1 more decision is waiting for an explanation" in result["markdown"]
    assert "the custodian has been told" in result["markdown"]


def test_several_wanted_sources_become_one_acquisition_item(program_root, platform_root):
    """Design §4 item 1: "non-blocking items of one kind become one item...
    many small items then cannot push other decisions past the cap." """
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    for title in ("Paper One", "Paper Two", "Paper Three"):
        store_insert(
            store, "source",
            {
                "source_id": new_id("SRC"), "kind": "paper", "title": title, "license_tier": "unknown",
                "acquisition_route": "web", "request_state": "wanted", "registered_ts": now(),
                "registered_by_launch": ids["launch"],
            },
        )
    store.close()

    result = _build(program_root, platform_root)
    decide = [i for i in result["packet"]["items"] if i["source"] == "decide"]
    acq = [i for i in decide if "sources are waiting" in i["what"]]
    assert len(acq) == 1
    assert "Paper One" in acq[0]["what"] and "Paper Two" in acq[0]["what"] and "Paper Three" in acq[0]["what"]
    assert "3 sources" in acq[0]["what"]


def test_s5_a_large_combined_acquisition_is_bounded_with_resolvable_refs(program_root, platform_root):
    """Review S-5 / probe Q3 shape: 20 open sources must produce a BOUNDED
    heading (never one title per source), a stable id, and one resolvable
    ref per shown source -- never a raw ``ACQ:<id>,<id>,...`` list."""
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    for i in range(20):
        store_insert(
            store, "source",
            {
                "source_id": new_id("SRC"), "kind": "paper", "title": f"Paper Number {i}",
                "license_tier": "unknown", "acquisition_route": "web", "request_state": "wanted",
                "registered_ts": now(), "registered_by_launch": ids["launch"],
            },
        )
    store.close()

    result = _build(program_root, platform_root)
    decide = [i for i in result["packet"]["items"] if i["source"] == "decide"]
    acq = [i for i in decide if "sources are waiting" in i["what"]]
    assert len(acq) == 1
    item = acq[0]
    assert len(item["what"]) < 500  # never one title per source (probe Q3: 5,000 -> 58,937 chars)
    assert "and 15 more" in item["what"]
    assert item["id"] == "DECIDE:ACQ:open"  # stable across builds, so a --ref cover stays attached
    assert len(item["refs"]) == 5  # capped the same way as the heading
    for ref in item["refs"]:
        assert ref.get("resolved"), ref  # every ref describe() can read, never a bare id list
        assert "a source" in ref["resolved"]
    assert "ACQ:" not in result["markdown"]  # the raw combined id never reaches the operator's page
    assert "Related" in result["markdown"]


def test_lint_warnings_are_reported_for_every_built_item(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()
    result = _build(program_root, platform_root)
    assert isinstance(result["packet"]["lint_warnings"], list)


def test_n10_needs_explaining_and_lint_warnings_surface_in_the_cli_envelope(
    program_root, platform_root, monkeypatch, capsys
):
    """Review N-10: lint_warnings and needs_explaining sat only in the
    result JSON, not the envelope's own warnings -- easy to miss on a
    `packet build` a human runs by hand. Both must reach `warnings` too."""
    import io
    import json as _json
    from contextlib import redirect_stdout

    from trialerror.cli import main
    from trialerror.dashboard import data as dash_data

    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    real_panel = dash_data.build_determinations_panel

    def _broken_panel(rostore):
        panel = real_panel(rostore)
        panel["items"] = [
            {"kind": "acquisition", "id": "SRC-broken", "owner": "operator", "title": "a paper",
             "what": "Deliver the source 'a paper'", "blocking": False},
        ]
        return panel

    monkeypatch.setattr(dash_data, "build_determinations_panel", _broken_panel)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([
            "packet", "build", "--program-root", str(program_root),
            "--trigger", "manual", "--dry-run",
        ])
    assert rc == 0
    env = _json.loads(buf.getvalue())
    codes = {w["code"] for w in env["warnings"]}
    assert "needs_explaining" in codes
    explaining = next(w for w in env["warnings"] if w["code"] == "needs_explaining")
    assert "SRC-broken" in explaining["message"] and "1 operator decision" in explaining["message"]
