"""Acceptance criterion: "bi-temporal edge invalidation correctness (as_of
queries return the right edge version)" — the Graphiti 4-timestamp pattern
implemented natively over ``claim``/``relation``.
"""

from __future__ import annotations

import pytest

from trialerror.stores import insert
from trialerror.stores.bitemporal import as_of, assert_fact, end_fact_validity, expire_fact, supersede_fact
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def world(store):
    """A store with one of everything already inserted (source/document/
    quote_anchor/launch etc.) so bi-temporal tests only need to touch
    claim/relation themselves."""
    ids = populate_one_of_everything(store)
    return store, ids


def test_assert_fact_defaults_all_four_timestamps(world):
    store, ids = world
    claim_id = new_id("CLM")
    row = assert_fact(
        store,
        "claim",
        {
            "claim_id": claim_id,
            "text": "a new claim",
            "kind": "finding",
            "anchor_id": ids["quote_anchor"],
            "created_by_launch": ids["launch"],
        },
    )
    assert row["expired_at"] is None
    assert row["invalid_at"] is None
    assert row["created_at"] is not None
    assert row["valid_at"] == row["created_at"]  # defaulted to the same "now"


def test_expire_fact_does_not_touch_invalid_at(world):
    store, ids = world
    claim_id = new_id("CLM")
    assert_fact(
        store, "claim", {"claim_id": claim_id, "text": "x", "kind": "finding",
                          "anchor_id": ids["quote_anchor"], "created_by_launch": ids["launch"]},
    )
    expire_fact(store, "claim", claim_id)
    row = store.knowledge.execute("SELECT * FROM claim WHERE claim_id = ?", (claim_id,)).fetchone()
    assert row["expired_at"] is not None
    assert row["invalid_at"] is None


def test_end_fact_validity_does_not_touch_expired_at(world):
    store, ids = world
    claim_id = new_id("CLM")
    assert_fact(
        store, "claim", {"claim_id": claim_id, "text": "x", "kind": "finding",
                          "anchor_id": ids["quote_anchor"], "created_by_launch": ids["launch"]},
    )
    end_fact_validity(store, "claim", claim_id, event_at="2025-01-01T00:00:00.000Z")
    row = store.knowledge.execute("SELECT * FROM claim WHERE claim_id = ?", (claim_id,)).fetchone()
    assert row["invalid_at"] == "2025-01-01T00:00:00.000Z"
    assert row["expired_at"] is None


def test_supersede_fact_correct_edge_version_across_tx_time(world):
    store, ids = world
    T1, T2 = "2026-01-01T00:00:00.000Z", "2026-06-01T00:00:00.000Z"
    c1 = new_id("CLM")
    assert_fact(
        store,
        "claim",
        {
            "claim_id": c1,
            "text": "v1",
            "kind": "finding",
            "anchor_id": ids["quote_anchor"],
            "created_by_launch": ids["launch"],
            "created_at": T1,
        },
        valid_at="2020-01-01T00:00:00.000Z",
    )
    c2 = new_id("CLM")
    supersede_fact(
        store,
        "claim",
        c1,
        {"text": "v2", "kind": "finding", "anchor_id": ids["quote_anchor"], "created_by_launch": ids["launch"]},
        new_id_column="claim_id",
        new_id_value=c2,
        valid_at="2020-01-01T00:00:00.000Z",
        tx_at=T2,
    )

    # current belief (no tx_at given): only the replacement is current
    current = as_of(store, "claim", where="claim_id IN (?, ?)", params=(c1, c2))
    assert [r["claim_id"] for r in current] == [c2]

    # as the DB believed strictly before the supersede: only the original
    before = as_of(store, "claim", tx_at="2026-03-01T00:00:00.000Z", where="claim_id IN (?, ?)", params=(c1, c2))
    assert [r["claim_id"] for r in before] == [c1]
    assert before[0]["text"] == "v1"

    # as the DB believed strictly after: only the replacement
    after = as_of(store, "claim", tx_at="2026-09-01T00:00:00.000Z", where="claim_id IN (?, ?)", params=(c1, c2))
    assert [r["claim_id"] for r in after] == [c2]
    assert after[0]["text"] == "v2"

    # superseded_by link recorded on the old row
    old_row = store.knowledge.execute("SELECT * FROM claim WHERE claim_id = ?", (c1,)).fetchone()
    assert old_row["superseded_by"] == c2
    assert old_row["expired_at"] == T2


def test_as_of_event_time_axis_independent_of_tx_time(world):
    """A fact valid 2020-2021 (event time) should not appear when querying
    valid_at outside that window, even though the DB's current (tx-time)
    belief includes the row."""
    store, ids = world
    claim_id = new_id("CLM")
    assert_fact(
        store,
        "claim",
        {
            "claim_id": claim_id,
            "text": "seasonal",
            "kind": "finding",
            "anchor_id": ids["quote_anchor"],
            "created_by_launch": ids["launch"],
        },
        valid_at="2020-01-01T00:00:00.000Z",
    )
    end_fact_validity(store, "claim", claim_id, event_at="2021-01-01T00:00:00.000Z")

    inside = as_of(store, "claim", valid_at="2020-06-01T00:00:00.000Z", where="claim_id = ?", params=(claim_id,))
    assert len(inside) == 1

    outside = as_of(store, "claim", valid_at="2022-01-01T00:00:00.000Z", where="claim_id = ?", params=(claim_id,))
    assert outside == []


def test_as_of_relation_uses_rel_id_pk(world):
    store, ids = world
    # populate_one_of_everything already inserted one `relation` row (not
    # bi-temporal-asserted, but present); confirm as_of finds it under
    # current belief with no explicit valid_at/tx_at.
    rows = as_of(store, "relation", where="rel_id = ?", params=(ids["relation"],))
    assert len(rows) == 1
    assert rows[0]["rel_id"] == ids["relation"]


def test_as_of_rejects_non_bitemporal_table(world):
    store, _ = world
    with pytest.raises(ValueError, match="not a bi-temporal table"):
        as_of(store, "account")
