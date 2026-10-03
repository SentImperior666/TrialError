"""L10 part B: every DECIDE item names its owner and, per its owner, either
the operator's full plain words (what/why/options/recommended/if_undecided/
needed_by) or the shorter what/why/next_step every other owner gets (design
``L10_plain-words-resolver-packet-outbox.md`` §3, table B2)."""

from __future__ import annotations

import json

import pytest

from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.rooms.api import freeze_room
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert as store_insert
from trialerror.stores.writer import update as store_update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def owners_seeded(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    freeze_room(store, room_id=ids["room"], by_launch=ids["launch"], reason="the round ended")
    store_update(
        store, "idea", pk_column="idea_id", pk_value=ids["idea"], changes={"status": "eliminated"}
    )
    store_insert(
        store, "source",
        {
            "source_id": new_id("SRC"), "kind": "paper", "title": "wanted paper",
            "license_tier": "unknown", "acquisition_route": "web", "request_state": "wanted",
            "registered_ts": now(), "registered_by_launch": ids["launch"],
        },
    )
    store_update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={
            "state": "submitted",
            "edits": json.dumps([{"edit_id": "E1", "text": "fix the tally", "blocking": True, "verified": False}]),
            "critic_launch": ids["launch"], "verdict_ts": now(), "reproduction_status": "unrun",
        },
    )
    store.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    yield rostore, ids
    rostore.close()


def _by_kind(panel, kind):
    return next(i for i in panel["items"] if i["kind"] == kind)


def test_every_item_names_its_owner(owners_seeded):
    rostore, ids = owners_seeded
    panel = data.build_determinations_panel(rostore)
    owners = {i["kind"]: i["owner"] for i in panel["items"]}
    assert owners["room_escalation"] == "operator"
    assert owners["acquisition"] == "operator"
    assert owners["gate_edit"] == "orchestrator"
    assert owners["kg_merge"] == "orchestrator"  # fixture's draft merge_proposal


def test_room_escalation_carries_full_plain_words_and_recommends_close(owners_seeded):
    """The linked idea was eliminated (the closest thing this schema has to
    "its round has a failed result") -- B2: recommend close in that case."""
    rostore, ids = owners_seeded
    panel = data.build_determinations_panel(rostore)
    item = _by_kind(panel, "room_escalation")
    assert item["what"] == "Close or keep the stopped discussion room 'test room'"
    assert "frozen" in item["why"] and "the round ended" in item["why"]
    keys = {o["key"] for o in item["options"]}
    assert keys == {"close", "keep"}
    assert all(o["consequence"] for o in item["options"])
    assert item["recommended"] == "close"
    assert item["if_undecided"] and item["needed_by"] == "next-session"


def test_acquisition_carries_full_plain_words(owners_seeded):
    rostore, ids = owners_seeded
    panel = data.build_determinations_panel(rostore)
    item = _by_kind(panel, "acquisition")
    assert item["what"] == "Deliver the source 'wanted paper'"
    assert "only you can obtain it" in item["why"]
    assert {o["key"] for o in item["options"]} == {"deliver", "not_now", "drop"}
    assert item["recommended"] in {o["key"] for o in item["options"]}
    assert item["needed_by"] == "next-session"


def test_orchestrator_owned_items_get_what_why_next_step_not_options(owners_seeded):
    rostore, ids = owners_seeded
    panel = data.build_determinations_panel(rostore)
    gate_item = _by_kind(panel, "gate_edit")
    assert gate_item["owner"] == "orchestrator"
    assert "fix the tally" in gate_item["what"]
    assert gate_item["why"]
    assert gate_item["next_step"] == "the orchestrator checks that the review's correction was applied"
    assert "options" not in gate_item or not gate_item.get("options")

    merge_item = _by_kind(panel, "kg_merge")
    assert merge_item["owner"] == "orchestrator"
    assert merge_item["next_step"] == "the orchestrator accepts or rejects the merge"


def test_term_kinds_are_not_in_the_decide_panel_any_more(owners_seeded):
    rostore, ids = owners_seeded
    panel = data.build_determinations_panel(rostore)
    assert "term_conflict" not in panel["counts_by_kind"]
    assert "term_duplicate" not in panel["counts_by_kind"]


def test_s4_a_failed_gates_edit_is_excluded(program_root, platform_root):
    """Review S-4 / design §3 B2: "excludes gates in failed, whose edits can
    no longer be acted on" -- verify_edit refuses a non-gated gate, so a
    failed gate's unverified edit would otherwise sit in DECIDE for good."""
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store_update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={
            "state": "failed",
            "edits": json.dumps([{"edit_id": "E1", "text": "fix the tally", "blocking": True, "verified": False}]),
            "critic_launch": ids["launch"], "verdict_ts": now(), "reproduction_status": "mismatch",
        },
    )
    store.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_determinations_panel(rostore)
    finally:
        rostore.close()
    assert panel["counts_by_kind"].get("gate_edit", 0) == 0
    assert all(i["kind"] != "gate_edit" for i in panel["items"])
