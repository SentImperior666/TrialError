"""Lane FB-7 item 4: a useful kind refusal, and a free class tag.

A live plants file spelled ``kind`` as the plant's CLASS -- ``present``,
``adjacent``, ``absent`` -- and was refused one plant at a time, by the
first offender, with a message that did not say what the four kinds were or
where a class was supposed to go. The author fixed one line and ran again,
twelve times.

Two changes, tested here. The refusal now reads the whole file, names every
offender (capped, with a count), names the kinds, and points at ``class``.
And ``class`` exists: free text, bounded, grouping the calibration card's
``by_class`` table -- and never, ever shown to a judge, because a judge told
"this one is an ABSENT plant" has been handed the answer.
"""

from __future__ import annotations

import pytest

from tests._novelty_fixtures import build_round
from trialerror.lens.novelty import (
    KIND_REFUSAL_CAP,
    PLANT_CLASS_MAX_CHARS,
    PLANT_KINDS,
    WITHHELD_FROM_JUDGE,
    NoveltyError,
    build_calibration_batch,
    load_external_plants,
    record_calibration,
)

SEED = "seed-class"


def _plant(pid, kind="area", **extra):
    return {
        "plant_id": pid,
        "kind": kind,
        "statement": f"A planted statement for {pid}.",
        "expected_labels": {"R3": ["same", "variant"]},
        **extra,
    }


# ---------------------------------------------------------------------------
# the refusal
# ---------------------------------------------------------------------------


def test_the_refusal_names_every_offender_not_only_the_first():
    declared = [
        _plant("P-1", kind="present"),
        _plant("P-2", kind="area"),
        _plant("P-3", kind="adjacent"),
        _plant("P-4", kind="absent"),
    ]
    with pytest.raises(NoveltyError) as excinfo:
        load_external_plants(declared, judged_sets=["R3"])
    message = str(excinfo.value)
    assert "3 plant(s)" in message
    for pid in ("P-1", "P-3", "P-4"):
        assert pid in message
    assert "P-2" not in message


def test_the_refusal_names_the_four_kinds_and_where_a_class_goes():
    with pytest.raises(NoveltyError) as excinfo:
        load_external_plants([_plant("P-1", kind="present")], judged_sets=["R3"])
    message = str(excinfo.value)
    for kind in PLANT_KINDS:
        assert kind in message
    assert "'class'" in message
    assert str(PLANT_CLASS_MAX_CHARS) in message
    assert "never shown to a judge" in message


def test_the_offender_list_is_capped_and_says_how_many_more():
    declared = [_plant(f"P-{i}", kind="present") for i in range(KIND_REFUSAL_CAP + 5)]
    with pytest.raises(NoveltyError) as excinfo:
        load_external_plants(declared, judged_sets=["R3"])
    message = str(excinfo.value)
    assert f"{KIND_REFUSAL_CAP + 5} plant(s)" in message
    assert "and 5 more" in message
    assert f"P-{KIND_REFUSAL_CAP + 4}" not in message


def test_a_plant_with_no_kind_at_all_is_an_offender_named_by_its_id():
    declared = [{"plant_id": "P-9", "statement": "x", "expected_labels": {"R3": ["same"]}}]
    with pytest.raises(NoveltyError, match="P-9"):
        load_external_plants(declared, judged_sets=["R3"])


def test_an_offender_with_no_plant_id_is_named_by_its_position():
    declared = [{"kind": "present", "statement": "x", "expected_labels": {"R3": ["same"]}}]
    with pytest.raises(NoveltyError, match=r"\(plant 0\)"):
        load_external_plants(declared, judged_sets=["R3"])


# ---------------------------------------------------------------------------
# the class tag
# ---------------------------------------------------------------------------


def test_class_is_optional_and_round_trips():
    loaded = load_external_plants(
        [_plant("P-1", **{"class": "absent"}), _plant("P-2")], judged_sets=["R3"]
    )
    assert loaded[0]["class"] == "absent"
    assert loaded[1]["class"] is None


def test_a_blank_class_reads_as_none_rather_than_an_empty_group():
    loaded = load_external_plants([_plant("P-1", **{"class": "   "})], judged_sets=["R3"])
    assert loaded[0]["class"] is None


def test_a_class_longer_than_the_bound_is_refused_by_name():
    long = "x" * (PLANT_CLASS_MAX_CHARS + 1)
    with pytest.raises(NoveltyError, match="at most 40"):
        load_external_plants([_plant("P-1", **{"class": long})], judged_sets=["R3"])
    # ...and exactly at the bound is fine.
    at_bound = "y" * PLANT_CLASS_MAX_CHARS
    loaded = load_external_plants([_plant("P-1", **{"class": at_bound})], judged_sets=["R3"])
    assert loaded[0]["class"] == at_bound


def test_class_is_on_the_withheld_list():
    assert "class" in WITHHELD_FROM_JUDGE


# ---------------------------------------------------------------------------
# on a batch and on a card
# ---------------------------------------------------------------------------


@pytest.fixture()
def classed(store, tmp_path):
    fixture = build_round(store)
    plants = [
        # Deliberately NOT the design's own label words: a blob check for a
        # leak would otherwise fire on the vocabulary the judge is correctly
        # shown, and prove nothing about the class.
        _plant("C-1", **{"class": "battery-alpha"}),
        _plant("C-2", **{"class": "battery-alpha"}),
        _plant("C-3", **{"class": "battery-omega"}),
    ]
    batch = build_calibration_batch(
        store, round_id=fixture["round_id"], external_plants=plants, seed=SEED,
        dossiers={}, batch_fail_on="area", out_dir=tmp_path,
    )
    return fixture, batch, tmp_path


def test_no_judge_view_carries_the_class(store, classed):
    _fixture, batch, _tmp = classed
    assert any(p.get("class") for p in batch["plants"]), "the fixture would prove nothing otherwise"
    for view in batch["judge_views"]:
        assert "class" not in view
        assert "class" not in view["record"]
        blob = repr(view)
        assert "battery-alpha" not in blob and "battery-omega" not in blob
    # ...and every withheld field stays withheld on this batch too.
    for view in batch["judge_views"]:
        for field in WITHHELD_FROM_JUDGE:
            assert field not in view and field not in view["record"]


def test_the_card_groups_by_class_when_the_plants_declare_one(store, classed):
    fixture, batch, tmp_path = classed
    labels_a = {
        "C-1": {"label_inventory": "same"},
        "C-2": {"label_inventory": "new-mechanism"},
        "C-3": {"label_inventory": "variant"},
    }
    card = record_calibration(
        store, round_id=fixture["round_id"], batch=batch,
        labels_a=labels_a, labels_b=dict(labels_a),
        issued_by_launch=fixture["launches"]["lens-1"], out_dir=tmp_path,
    )
    by_class = card["by_class"]["a"]
    assert set(by_class) == {"battery-alpha", "battery-omega"}
    assert by_class["battery-alpha"]["n"] == 2
    assert by_class["battery-omega"]["n"] == 1
    assert by_class["battery-alpha"]["given"]["R3"] == {"same": 1, "new-mechanism": 1}
    # by_expected is unaffected -- the two groupings are different questions.
    assert set(card["by_expected"]["a"]) == {"R3=same,variant"}


def test_a_card_whose_plants_declare_no_class_has_an_empty_by_class(store, tmp_path):
    fixture = build_round(store)
    batch = build_calibration_batch(
        store, round_id=fixture["round_id"], external_plants=[_plant("C-1")], seed=SEED,
        dossiers={}, batch_fail_on="area", out_dir=tmp_path,
    )
    card = record_calibration(
        store, round_id=fixture["round_id"], batch=batch,
        labels_a={"C-1": {"label_inventory": "same"}},
        labels_b={"C-1": {"label_inventory": "same"}},
        issued_by_launch=fixture["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["by_class"] == {"a": {}, "b": {}}
    assert card["by_expected"]["a"]
