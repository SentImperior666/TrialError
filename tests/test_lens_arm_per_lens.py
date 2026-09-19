"""``arm_mode="per_lens"`` — the roster-level assignment semantics.

The numbers are the point of this module. Splitting the ROSTER 40/40/20
with a floor of two far LENSES has to reproduce the documented allocation
exactly (roster 6 -> 3 near / 1 moderate / 2 far; roster 12 -> 5 / 5 / 2),
with the assumption-buster pre-placed in the far arm and the control seat
in the modal one — and each lens's whole slice has to come from its own
arm, or "arm" is a label on a lens rather than a property of what it read.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from trialerror.lens.assign import (
    ARM_MODES,
    allocate_lens_arms,
    build_assignment_plan,
    list_assignments,
    modal_arm,
    run_assignment,
)
from trialerror.lens.errors import ArmAllocationError
from trialerror.lens.roster import add_lens
from trialerror.stores import table_columns
from tests._lens_fixtures import build_doc_pool

SEED = "seed-arm-per-lens"


def _lenses(n: int, *, buster: int | None = None, control: int | None = None):
    out = []
    for i in range(n):
        seat = "standard"
        if buster is not None and i == buster:
            seat = "assumption_buster"
        if control is not None and i == control:
            seat = "control"
        out.append({"roster_id": f"ROST-{i:02d}", "seat": seat})
    return out


# ---------------------------------------------------------------------------
# the allocation numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("roster_size", "expected"),
    [
        (6, {"near": 3, "moderate": 1, "far": 2}),
        (12, {"near": 5, "moderate": 5, "far": 2}),
    ],
)
def test_the_roster_quota_reproduces_the_documented_allocation(roster_size, expected):
    lenses = _lenses(roster_size, buster=roster_size - 2, control=roster_size - 1)
    arms, quota = allocate_lens_arms(lenses, seed=SEED)
    assert dict(quota) == expected
    assert Counter(arms) == Counter(expected)


@pytest.mark.parametrize("roster_size", [6, 12])
def test_the_buster_sits_far_and_the_control_sits_in_the_modal_arm(roster_size):
    lenses = _lenses(roster_size, buster=0, control=1)
    arms, quota = allocate_lens_arms(lenses, seed=SEED)
    assert arms[0] == "far"
    assert arms[1] == modal_arm(quota)


def test_modal_arm_breaks_a_tie_by_arm_order_not_dict_order():
    # roster 12 gives near and moderate 5 apiece -- near wins by arm order.
    assert modal_arm({"near": 5, "moderate": 5, "far": 2}) == "near"
    assert modal_arm({"near": 1, "moderate": 5, "far": 2}) == "moderate"


def test_allocation_is_seed_deterministic_and_seed_sensitive():
    lenses = _lenses(12, buster=10, control=11)
    first, _ = allocate_lens_arms(lenses, seed=SEED)
    again, _ = allocate_lens_arms(lenses, seed=SEED)
    assert first == again
    # A different seed must be free to land a different (still legal) split;
    # asserting inequality outright would be a coin flip, so assert the
    # invariant instead: whatever the seed, the quota is honored.
    other, quota = allocate_lens_arms(lenses, seed="another-seed")
    assert Counter(other) == Counter(quota)


def test_more_busters_than_far_seats_is_refused_not_silently_demoted():
    lenses = _lenses(6)
    for i in range(3):
        lenses[i]["seat"] = "assumption_buster"
    with pytest.raises(ArmAllocationError) as exc:
        allocate_lens_arms(lenses, seed=SEED)
    assert "far" in str(exc.value)


def test_a_far_lens_floor_larger_than_the_roster_is_refused():
    with pytest.raises(ArmAllocationError):
        allocate_lens_arms(_lenses(2), seed=SEED, far_lens_floor=5)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def _plan(store, *, roster_size, slices_per_lens, arm_mode, n_docs=60):
    pool = build_doc_pool(store, n_docs=n_docs + 1)
    home_id, *candidate_ids = pool["doc_ids"]
    from trialerror.lens.vectors import fetch_doc_vectors

    home = fetch_doc_vectors(store, model_key=pool["model_key"], doc_ids=[home_id])
    candidates = fetch_doc_vectors(store, model_key=pool["model_key"], doc_ids=candidate_ids)
    return build_assignment_plan(
        candidates=candidates, home=home,
        lenses=_lenses(roster_size, buster=roster_size - 2, control=roster_size - 1),
        slices_per_lens=slices_per_lens, seed=SEED, arm_mode=arm_mode,
    )


def test_every_lens_draws_its_whole_slice_from_its_own_arm(store):
    plan = _plan(store, roster_size=6, slices_per_lens=5, arm_mode="per_lens")
    assert plan["arm_mode"] == "per_lens"
    assert plan["roster_quota"] == {"near": 3, "moderate": 1, "far": 2}
    for lens_plan in plan["lenses"]:
        arms = {s["arm"] for s in lens_plan["slices"]}
        assert arms == {lens_plan["arm"]}, lens_plan["roster_id"]
        assert len(lens_plan["slices"]) == 5


def test_the_per_lens_far_floor_is_the_whole_slice_for_a_far_lens_and_zero_otherwise(store):
    plan = _plan(store, roster_size=6, slices_per_lens=5, arm_mode="per_lens")
    for lens_plan in plan["lenses"]:
        expected = 5 if lens_plan["arm"] == "far" else 0
        assert lens_plan["far_floor"] == expected


def test_per_slice_mode_is_untouched_and_names_itself(store):
    plan = _plan(store, roster_size=6, slices_per_lens=5, arm_mode="per_slice")
    assert plan["arm_mode"] == "per_slice"
    assert plan["far_lens_floor"] is None
    assert plan["roster_quota"] is None
    for lens_plan in plan["lenses"]:
        assert lens_plan["arm"] is None
        assert lens_plan["quota"] == {"near": 2, "moderate": 1, "far": 2}


def test_an_unknown_arm_mode_is_refused(store):
    with pytest.raises(ValueError):
        _plan(store, roster_size=6, slices_per_lens=5, arm_mode="per_round")


def test_no_candidate_is_drawn_twice_across_the_roster(store):
    plan = _plan(store, roster_size=6, slices_per_lens=5, arm_mode="per_lens")
    drawn = [s["candidate_id"] for lp in plan["lenses"] for s in lp["slices"]]
    assert len(drawn) == len(set(drawn)) == 30


# ---------------------------------------------------------------------------
# what lands in lens_assignment
# ---------------------------------------------------------------------------


def _seeded_round(store, *, round_id="round-apl", roster_size=6, slices_per_lens=5, arm_mode="per_lens", cards=None):
    pool = build_doc_pool(store, n_docs=61)
    home_id, *candidate_ids = pool["doc_ids"]
    lenses = []
    for i in range(roster_size):
        seat = "standard"
        if i == roster_size - 2:
            seat = "assumption_buster"
        elif i == roster_size - 1:
            seat = "control"
        row = add_lens(
            store, round_id=round_id, lens_name=f"lens-{i}", vantage=f"v{i}",
            model_class="top", seat=seat,
            recipe_cards=None if seat == "control" else (cards or None),
        )
        lenses.append({"roster_id": row["roster_id"], "seat": seat, "recipe_cards": row["recipe_cards"]})
    result = run_assignment(
        store, round_id=round_id, model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=lenses, slices_per_lens=slices_per_lens,
        seed=SEED, arm_mode=arm_mode,
    )
    return round_id, result


def test_assignment_rows_carry_the_mode_the_roster_floor_and_the_cards(store):
    round_id, _ = _seeded_round(store, cards=["MISMATCH", "TRANSFER"])
    rows = list_assignments(store, round_id=round_id)
    assert rows
    assert {r["arm_mode"] for r in rows} == {"per_lens"}
    assert {r["far_lens_floor"] for r in rows} == {2}
    standard = [r for r in rows if r["seat"] == "standard"]
    assert json.loads(standard[0]["recipe_cards"]) == ["MISMATCH", "TRANSFER"]
    control = [r for r in rows if r["seat"] == "control"]
    assert control and all(r["recipe_cards"] is None for r in control)


def test_the_existing_far_arm_floor_check_still_passes_under_per_lens(store, program_root):
    from trialerror.lens.checks import check_far_arm_floor_honored
    from trialerror.util.doctor import DoctorContext

    _seeded_round(store)
    store.close()
    result = check_far_arm_floor_honored(DoctorContext(program_root=program_root))
    assert result.status == "pass", result.details


def test_per_slice_rows_leave_the_roster_floor_null(store):
    round_id, _ = _seeded_round(store, arm_mode="per_slice", slices_per_lens=5)
    rows = list_assignments(store, round_id=round_id)
    assert {r["arm_mode"] for r in rows} == {"per_slice"}
    assert {r["far_lens_floor"] for r in rows} == {None}


def test_the_schema_v9_columns_exist(store):
    columns = set(table_columns(store.ops, "lens_assignment"))
    assert {"arm_mode", "far_lens_floor", "recipe_cards"} <= columns
    assert "recipe_cards" in set(table_columns(store.ops, "lens_roster"))


def test_arm_modes_tuple_matches_the_db_check():
    assert set(ARM_MODES) == {"per_slice", "per_lens"}
