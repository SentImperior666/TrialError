"""``trialerror.lens.roster`` — ``lens_roster`` writes/reads."""

from __future__ import annotations

import pytest

from trialerror.lens.roster import add_lens, list_roster


def test_add_lens_writes_a_lens_roster_row(store):
    row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="adversarial", model_class="top")
    assert row["round_id"] == "round-1"
    assert row["lens_name"] == "skeptic"
    assert row["seat"] == "standard"
    assert row["roster_id"].startswith("ROST-")


def test_add_lens_assumption_buster_seat(store):
    row = add_lens(
        store, round_id="round-1", lens_name="devil's advocate", vantage="assumption",
        model_class="top", seat="assumption_buster",
    )
    assert row["seat"] == "assumption_buster"


def test_add_lens_rejects_unknown_seat(store):
    with pytest.raises(ValueError):
        add_lens(store, round_id="round-1", lens_name="x", vantage="v", model_class="top", seat="bogus")


def test_list_roster_scoped_to_round_id_in_insertion_order(store):
    add_lens(store, round_id="round-1", lens_name="A", vantage="v1", model_class="top")
    add_lens(store, round_id="round-2", lens_name="B", vantage="v2", model_class="mid")
    add_lens(store, round_id="round-1", lens_name="C", vantage="v3", model_class="top")

    round1 = list_roster(store, round_id="round-1")
    assert [r["lens_name"] for r in round1] == ["A", "C"]

    round2 = list_roster(store, round_id="round-2")
    assert [r["lens_name"] for r in round2] == ["B"]

    assert list_roster(store, round_id="round-nonexistent") == []
