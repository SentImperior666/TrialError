"""``trialerror.verify.plan_check`` -- the plan-time check, pure (no store)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trialerror.eval.gate_suites import AIIF_MODEL_FLOORS
from trialerror.verify.errors import UnknownPlanSuiteError
from trialerror.verify.plan_check import PlanContext, run_plan_check

FIXTURE = Path(__file__).parent / "fixtures" / "plan_check" / "round0_like_params.json"
FULL_MODELS = {purpose: "top" for purpose in AIIF_MODEL_FLOORS}
HEX64 = "a" * 64
_DROP = object()


def round0_like(**changes):
    params = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is _DROP:
            params.pop(key, None)
        else:
            params[key] = value
    return params


def run(params, models=FULL_MODELS, round_id="R-TEST-0"):
    return run_plan_check("aiif_round", PlanContext(params, models, round_id, None, None))


def item(result, check_id):
    return next(i for i in result.items if i.check_id == check_id)


def test_round0_trap_is_flagged():
    result = run(round0_like())
    assert result.overall == "fail"
    assert result.must_failures == ["admission_escrow_planned"]
    message = item(result, "admission_escrow_planned").message
    assert "second_prereg_commit" in message and "rooms" in message
    assert item(result, "declarations_readable").status == "pass"
    assert item(result, "card_cells_feasible").status == "pass"
    assert result.to_dict()["overall"] == "fail"
    json.dumps(result.to_dict())


def test_unknown_suite():
    with pytest.raises(UnknownPlanSuiteError):
        run_plan_check("nope", PlanContext({}, {}, None, None, None))


def test_rooms_false_passes():
    result = run(round0_like(rooms=False))
    assert item(result, "admission_escrow_planned").status == "not_applicable"
    assert result.overall == "pass"


def test_declared_second_escrow_passes():
    result = run(round0_like(admission_escrow={"by": "second_prereg_commit", "after": "judged_screen"}))
    assert item(result, "admission_escrow_planned").status == "pass"
    assert result.overall == "pass"


def test_closed_pool_hash_passes():
    assert run(round0_like(admission_order_hash=HEX64)).overall == "pass"
    bad = run(round0_like(admission_order_hash="a" * 63))
    assert item(bad, "admission_escrow_planned").status == "fail"


def test_upper_case_hash_is_refused_and_says_lower_case():
    """The gate compares the hash as a string, case-sensitively, and the room
    order's hash is printed in lower case: an upper-case one would fail there."""
    bad = item(run(round0_like(admission_order_hash="A" * 64)), "admission_escrow_planned")
    assert bad.status == "fail" and "lower-case" in bad.message
    assert run(round0_like(admission_order_hash="a" * 64)).overall == "pass"
    out = run_admission({"admission_order_hash": "A" * 64}, parent_row())
    assert out.must_failures == ["order_hash_present"]
    assert "lower-case" in item(out, "order_hash_present").message


def test_malformed_hash_messages_give_the_gate_tail_and_the_two_ways_forward():
    frame = item(run(round0_like(admission_order_hash="a" * 63)), "admission_escrow_planned").message
    assert "admission_order_hash_matches" in frame and "will fail after the data exists" in frame
    assert "rooms: false" in frame and "second_prereg_commit" in frame
    second = item(run_admission({"admission_order_hash": "abc"}, parent_row()), "order_hash_present").message
    assert "admission_order_hash_matches" in second and "will fail after the data exists" in second
    assert "room admission-order" in second and "deviation" in second


def test_rooms_without_seed_fails():
    params = round0_like(admission_escrow={"by": "second_prereg_commit"}, seeds={"assign": "x"})
    fail = item(run(params), "admission_escrow_planned")
    assert fail.status == "fail" and "seed" in fail.message
    # admission_seed is the other way to name it
    ok = run(round0_like(admission_escrow={"by": "second_prereg_commit"}, seeds={"assign": "x"}, admission_seed="s"))
    assert item(ok, "admission_escrow_planned").status == "pass"


@pytest.mark.parametrize(
    "changes,needle",
    [
        ({"design": "pair"}, "design="),
        ({"rooms": "yes"}, "rooms="),
        ({"control_count": "1"}, "control_count="),
        ({"control_count": True}, "control_count="),
        ({"report_p_values": "true"}, "report_p_values="),
    ],
)
def test_declarations_unreadable(changes, needle):
    fail = item(run(round0_like(**changes)), "declarations_readable")
    assert fail.status == "fail" and needle in fail.message


def test_declarations_unreadable_lists_all():
    fail = item(run(round0_like(design="pair", rooms="yes")), "declarations_readable")
    assert "design=" in fail.message and "rooms=" in fail.message


def plan_item(**changes):
    return item(run(round0_like(**changes)), "control_plan_consistent")


def test_control_plan_design_absent():
    # the amended real shape: two names, one a reserved spare -> passes, and says so
    real = plan_item()
    assert real.status == "pass" and "spare" in real.message and "exactly one seated control" in real.message
    assert plan_item(control_lens_names=["lens-e"]).status == "pass"
    assert plan_item(control_lens_names=_DROP).status == "pass"
    empty = plan_item(control_lens_names=[])
    assert empty.status == "fail" and "no control lens is named" in empty.message
    assert plan_item(control_lens_names="lens-e").status == "fail"  # a non-list stays a fail


@pytest.mark.parametrize("count", [0, 2, 3])
def test_control_plan_design_absent_control_count_must_be_one(count):
    fail = plan_item(control_count=count)
    assert fail.status == "fail"
    assert "control_seats" in fail.message and "control_count" in fail.message
    assert plan_item(control_count=_DROP).status == "pass"
    assert plan_item(control_count=1).status == "pass"


@pytest.mark.parametrize("count", [0, None])
def test_control_seats_count_missing_or_zero_fails(count):
    assert plan_item(design="control_seats", control_count=count).status == "fail"


def test_control_seats_names_against_count():
    seats = dict(design="control_seats", control_count=2)
    assert plan_item(**seats, control_lens_names=["a"]).status == "fail"
    assert plan_item(**seats, control_lens_names=["a", "b"]).status == "pass"
    more = plan_item(**seats, control_lens_names=["a", "b", "c"])
    assert more.status == "pass" and "spare" in more.message
    assert plan_item(**seats, control_lens_names=_DROP).status == "pass"
    assert plan_item(design="control_seats", control_count=True).status == "fail"


def test_control_plan_paired_and_none():
    assert plan_item(design="paired").status == "pass"
    assert plan_item(design="none").status == "not_applicable"


def test_models_floors_met():
    missing = dict(FULL_MODELS)
    missing.pop("gates")
    assert item(run(round0_like(), missing), "models_floors_met").status == "fail"
    small = dict(FULL_MODELS, screen="small")
    below = item(run(round0_like(), small), "models_floors_met")
    assert below.status == "fail" and "screen" in below.message
    assert item(run(round0_like(), {}), "models_floors_met").status == "fail"
    assert item(run(round0_like()), "models_floors_met").status == "pass"


@pytest.mark.parametrize("value", [_DROP, "", "  ", 3])
def test_arm_mode_declared(value):
    assert item(run(round0_like(arm_mode=value)), "arm_mode_declared").status == "fail"


def test_card_cells_feasible():
    one_holder = round0_like(cards={"L1": ["TRANSFER"], "L2": ["MISMATCH"], "L3": ["MISMATCH"], "B": ["NEGATE"]})
    assert item(run(one_holder), "card_cells_feasible").status == "fail"
    negate_ok = item(run(round0_like()), "card_cells_feasible")
    assert negate_ok.status == "pass"
    nocards = item(run(round0_like(cards=_DROP)), "card_cells_feasible")
    assert nocards.severity == "warn" and nocards.status == "fail"
    assert "cards not escrowed" in nocards.message
    assert item(run(round0_like(cards={"L1": "TRANSFER"})), "card_cells_feasible").status == "fail"


def test_warnings_do_not_make_overall_fail():
    result = run(round0_like(rooms=False, reference_set_hashes=_DROP))
    assert result.must_failures == []
    assert result.warnings == ["reference_sets_named"]
    assert result.overall == "pass_with_warnings"
    partial = run(round0_like(rooms=False, reference_set_hashes={"R1": "a"}))
    assert partial.warnings == ["reference_sets_named"]
    noplants = run(round0_like(rooms=False, plants={}))
    assert noplants.warnings == ["plants_declared"]


def parent_row(**changes):
    row = {
        "prereg_id": "PREG-1", "status": "committed", "plan_suite": "aiif_round", "round_id": "R-TEST-0",
        "plan_check": json.dumps({"rooms_declared": None, "room_seed": "seed-x"}),
    }
    row.update(changes)
    return row


def run_admission(params, parent, round_id="R-TEST-0", store=None):
    return run_plan_check("aiif_round_admission", PlanContext(params, FULL_MODELS, round_id, parent, store))


def test_admission_suite_pure():
    ok = run_admission({"admission_order_hash": HEX64}, parent_row())
    assert ok.overall == "pass"
    assert run_admission({"admission_order_hash": HEX64}, None).must_failures == ["parent_is_round_prereg"]
    no_rooms = run_admission({"admission_order_hash": HEX64}, parent_row(plan_check=json.dumps({"rooms_declared": False})))
    assert "no rooms" in item(no_rooms, "parent_is_round_prereg").message
    other = run_admission({"admission_order_hash": HEX64}, parent_row(round_id="R-TEST-9"))
    assert other.must_failures == ["parent_is_round_prereg"]
    wrong_suite = run_admission({"admission_order_hash": HEX64}, parent_row(plan_suite=None))
    assert wrong_suite.must_failures == ["parent_is_round_prereg"]
    voided = run_admission({"admission_order_hash": HEX64}, parent_row(status="voided"))
    assert voided.must_failures == ["parent_is_round_prereg"]
    bad_hash = run_admission({"admission_order_hash": "abc"}, parent_row())
    assert bad_hash.must_failures == ["order_hash_present"]
    assert item(ok, "order_recomputed").message == "not recomputed"
