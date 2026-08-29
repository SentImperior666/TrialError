"""``trialerror.lens.ideas`` — the idea content-pipeline writer + the
``slice_ref`` JSON convention that carries home/assumed_circle/provenance/
tier/set_distance metadata (see module docstring for why it's packed into
``slice_ref`` rather than dedicated columns)."""

from __future__ import annotations

import json

import pytest

from trialerror.lens.ideas import build_slice_ref, link_idea_to_feed_post, write_idea
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._lens_fixtures import bootstrap_launch


def test_build_slice_ref_omits_none_fields():
    ref = build_slice_ref(assign_id="ASGN-1", arm="far")
    obj = json.loads(ref)
    assert obj == {"assign_id": "ASGN-1", "arm": "far"}


def test_build_slice_ref_carries_all_named_fields():
    ref = build_slice_ref(
        assign_id="ASGN-1", arm="far", distance_score=0.73, cluster_id="c9",
        home="hyp-home-doc", assumed_circle="skeptics", tier="tier2", set_distance=0.5,
        provenance={"source": "round-1"},
    )
    obj = json.loads(ref)
    assert obj["home"] == "hyp-home-doc"
    assert obj["assumed_circle"] == "skeptics"
    assert obj["tier"] == "tier2"
    assert obj["set_distance"] == 0.5
    assert obj["provenance"] == {"source": "round-1"}


def test_write_idea_round_trips(store):
    launch_id = bootstrap_launch(store)
    ref = build_slice_ref(assign_id="ASGN-1", arm="near")
    row = write_idea(store, round_id="round-1", author_launch=launch_id, body="full idea text here", slice_ref=ref)
    assert row["idea_id"].startswith("IDEA-")
    assert row["status"] == "raw"
    assert row["feed_post_ref"] is None
    assert json.loads(row["slice_ref"]) == {"assign_id": "ASGN-1", "arm": "near"}


def test_write_idea_writes_promoted_columns_directly(store):
    """schema-v2 (docs/INTEGRATION_NOTES.md item 14): home/assumed_circle/
    provenance/tier/set_distance land as real ``idea`` columns, not just
    inside slice_ref's JSON."""
    launch_id = bootstrap_launch(store)
    row = write_idea(
        store, round_id="round-1", author_launch=launch_id, body="idea body",
        home="hyp-home-doc", assumed_circle="skeptics", tier="far", set_distance=0.62,
        provenance={"source": "round-1"},
    )
    assert row["home"] == "hyp-home-doc"
    assert row["assumed_circle"] == "skeptics"
    assert row["tier"] == "far"
    assert row["set_distance"] == 0.62
    assert json.loads(row["provenance"]) == {"source": "round-1"}

    from trialerror.stores import get

    fetched = get(store, "idea", pk_column="idea_id", pk_value=row["idea_id"])
    assert fetched["home"] == "hyp-home-doc"
    assert fetched["tier"] == "far"


def test_write_idea_promoted_columns_also_land_in_deprecated_slice_ref(store):
    """Backward compat named in the schema-v2 mission: slice_ref stays
    populated for one version so a caller still reading it directly (the
    pre-schema-v2 convention) does not break."""
    launch_id = bootstrap_launch(store)
    row = write_idea(
        store, round_id="round-1", author_launch=launch_id, body="idea body",
        home="hyp-home-doc", tier="near",
    )
    assert row["slice_ref"] is not None
    assert json.loads(row["slice_ref"]) == {"home": "hyp-home-doc", "tier": "near"}


def test_write_idea_merges_promoted_fields_into_a_caller_supplied_slice_ref(store):
    """A caller carrying assign_id/arm/distance_score/cluster_id (never
    promoted, still slice_ref-only) via its own pre-built slice_ref still
    gets the promoted fields merged in too."""
    launch_id = bootstrap_launch(store)
    ref = build_slice_ref(assign_id="ASGN-1", arm="far", cluster_id="c9")
    row = write_idea(
        store, round_id="round-1", author_launch=launch_id, body="idea body",
        slice_ref=ref, home="hyp-home-doc", tier="far",
    )
    merged = json.loads(row["slice_ref"])
    assert merged == {"assign_id": "ASGN-1", "arm": "far", "cluster_id": "c9", "home": "hyp-home-doc", "tier": "far"}
    assert row["home"] == "hyp-home-doc"
    assert row["tier"] == "far"


def test_write_idea_no_promoted_fields_and_no_slice_ref_leaves_slice_ref_none(store):
    """Unchanged pre-schema-v2 behavior: a plain freeform idea with no
    assignment provenance at all still gets ``slice_ref = NULL``, not an
    empty-object JSON string."""
    launch_id = bootstrap_launch(store)
    row = write_idea(store, round_id="round-1", author_launch=launch_id, body="idea body")
    assert row["slice_ref"] is None
    assert row["home"] is None
    assert row["tier"] is None


def test_write_idea_rejects_unknown_tier(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError):
        write_idea(store, round_id="round-1", author_launch=launch_id, body="idea body", tier="bogus")


def test_write_idea_assumed_circle_plain_string_stored_verbatim_not_json_quoted(store):
    """The ``TEXT/json`` column convention: a plain string stays a plain
    string in the DB (never re-quoted into a JSON string literal)."""
    launch_id = bootstrap_launch(store)
    row = write_idea(store, round_id="round-1", author_launch=launch_id, body="x", assumed_circle="skeptics")
    assert row["assumed_circle"] == "skeptics"  # not '"skeptics"'


def test_write_idea_rejects_unknown_status(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError):
        write_idea(store, round_id="round-1", author_launch=launch_id, body="x", status="bogus")


def test_link_idea_to_feed_post_backfills_and_is_xid_validated(store):
    launch_id = bootstrap_launch(store)
    idea = write_idea(store, round_id="round-1", author_launch=launch_id, body="idea body")

    thread_id = new_id("THR")
    insert(store, "thread", {"thread_id": thread_id, "title": "t", "created_ts": now(), "created_by_launch": launch_id})
    post_id = new_id("POST")
    insert(store, "feed_post", {"post_id": post_id, "thread_id": thread_id, "author": f"launch:{launch_id}", "launch_id": launch_id, "ts": now(), "body": "idea body"})

    link_idea_to_feed_post(store, idea_id=idea["idea_id"], feed_post_ref=post_id)

    from trialerror.stores import get

    updated = get(store, "idea", pk_column="idea_id", pk_value=idea["idea_id"])
    assert updated["feed_post_ref"] == post_id


def test_link_idea_to_feed_post_refuses_unknown_post(store):
    from trialerror.stores.errors import XidTargetMissingError

    launch_id = bootstrap_launch(store)
    idea = write_idea(store, round_id="round-1", author_launch=launch_id, body="idea body")
    with pytest.raises(XidTargetMissingError):
        link_idea_to_feed_post(store, idea_id=idea["idea_id"], feed_post_ref="POST-nonexistent")
