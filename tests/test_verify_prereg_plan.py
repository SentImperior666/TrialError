"""``commit_prereg`` / ``check_prereg_plan`` with a plan suite: refuse before
writing, the params hash unchanged, deviations only for failures found."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._rooms_fixtures import bootstrap_launch
from trialerror.eval.gate_suites import AIIF_MODEL_FLOORS
from trialerror.stores.errors import ValidationError
from trialerror.stores.writer import get, insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from trialerror.verify.errors import PlanCheckFailedError, PreregNotFoundError, UnknownPlanSuiteError
from trialerror.verify.prereg import canonical_json, check_prereg_plan, commit_prereg, sha256_hex

FIXTURE = Path(__file__).parent / "fixtures" / "plan_check" / "round0_like_params.json"
ROUND = "R-TEST-0"
HEX64 = "b" * 64
NEW_COLUMNS = ["round_id", "plan_suite", "parent_prereg_id", "plan_check", "plan_check_status", "plan_checked_ts"]
_DROP = object()


def round0_like(**changes):
    params = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is _DROP:
            params.pop(key, None)
        else:
            params[key] = value
    return params


@pytest.fixture()
def full_models(program_root):
    """A program whose ``[models]`` table meets every framework floor."""
    lines = ['[program]', 'id = "plan-test"', '', '[models]']
    lines += [f'{purpose} = "top"' for purpose in AIIF_MODEL_FLOORS]
    (program_root / "trialerror.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def escrow_files(store):
    escrow_dir = store.platform_root / "escrow"
    return sorted(escrow_dir.rglob("*.json")) if escrow_dir.exists() else []


def prereg_rows(store):
    return store.ops.execute("SELECT prereg_id FROM prereg").fetchall()


def commit(store, params, **kwargs):
    return commit_prereg(store, title="t", procedure="the procedure", params=params, **kwargs)


def test_round0_trap_is_refused_at_commit(store, full_models):
    with pytest.raises(PlanCheckFailedError) as exc:
        commit(store, round0_like(), plan_suite="aiif_round", round_id=ROUND)
    assert exc.value.result.must_failures == ["admission_escrow_planned"]
    assert "second_prereg_commit" in str(exc.value)
    assert escrow_files(store) == []
    assert prereg_rows(store) == []


def test_rooms_false_passes(store, full_models):
    params = round0_like(rooms=False)
    row = commit(store, params, plan_suite="aiif_round", round_id=ROUND)
    assert row["plan_check_status"] == "pass"
    assert row["round_id"] == ROUND and row["plan_suite"] == "aiif_round"
    record = json.loads(row["plan_check"])
    assert record["overall"] == "pass" and record["rooms_declared"] is False
    assert record["room_seed"] == "R-TEST-0-rooms-x"
    assert record["accepted_deviations"] == {} and row["plan_checked_ts"]


def test_declared_second_escrow_and_closed_pool_pass(store, full_models):
    row = commit(store, round0_like(admission_escrow={"by": "second_prereg_commit"}), plan_suite="aiif_round", round_id=ROUND)
    assert row["plan_check_status"] == "pass"
    row = commit(store, round0_like(admission_order_hash=HEX64), plan_suite="aiif_round", round_id=ROUND)
    assert row["plan_check_status"] == "pass"
    with pytest.raises(PlanCheckFailedError):
        commit(store, round0_like(admission_order_hash="b" * 63), plan_suite="aiif_round", round_id=ROUND)


def test_missing_models_table_refuses(store):
    # no trialerror.toml at all: the [models] table is empty, so the floors are unmet
    with pytest.raises(PlanCheckFailedError) as exc:
        commit(store, round0_like(rooms=False), plan_suite="aiif_round", round_id=ROUND)
    assert exc.value.result.must_failures == ["models_floors_met"]


def test_warnings_do_not_refuse(store, full_models):
    row = commit(
        store, round0_like(rooms=False, reference_set_hashes=_DROP), plan_suite="aiif_round", round_id=ROUND
    )
    assert row["plan_check_status"] == "pass_with_warnings"
    assert json.loads(row["plan_check"])["warnings"] == ["reference_sets_named"]


def test_params_hash_and_escrow_are_unchanged_by_the_check(store, full_models):
    params = round0_like(rooms=False)
    with_check = commit(store, params, plan_suite="aiif_round", round_id=ROUND)
    without = commit(store, params)
    assert with_check["params_sha256"] == without["params_sha256"] == sha256_hex(canonical_json(params))
    escrowed = json.loads(Path(with_check["escrow_path"]).read_text(encoding="utf-8"))
    assert escrowed["params"] == params
    assert set(escrowed) == {"prereg_id", "title", "procedure", "params", "committed_ts"}


def test_accepted_deviation(store, full_models):
    trap = round0_like()
    row = commit(
        store, trap, plan_suite="aiif_round", round_id=ROUND,
        accepted_deviations={"admission_escrow_planned": "reason"}, decided_by="DEC-1",
    )
    assert row["plan_check_status"] == "deviations_accepted"
    record = json.loads(row["plan_check"])
    assert record["accepted_deviations"] == {"admission_escrow_planned": "reason"}
    assert record["decided_by"] == "DEC-1"
    assert len(escrow_files(store)) == 1

    before = len(prereg_rows(store))
    with pytest.raises(ValidationError):  # a check that passed
        commit(store, trap, plan_suite="aiif_round", round_id=ROUND,
               accepted_deviations={"arm_mode_declared": "x", "admission_escrow_planned": "y"}, decided_by="DEC-1")
    with pytest.raises(ValidationError):  # a warn, or one that did not fail
        commit(store, round0_like(rooms=False, plants={}), plan_suite="aiif_round", round_id=ROUND,
               accepted_deviations={"plants_declared": "x"}, decided_by="DEC-1")
    with pytest.raises(ValidationError):  # no decision cited
        commit(store, trap, plan_suite="aiif_round", round_id=ROUND,
               accepted_deviations={"admission_escrow_planned": "y"})
    with pytest.raises(ValidationError):  # empty reason
        commit(store, trap, plan_suite="aiif_round", round_id=ROUND,
               accepted_deviations={"admission_escrow_planned": " "}, decided_by="DEC-1")
    assert len(prereg_rows(store)) == before and len(escrow_files(store)) == 1


def test_partial_deviation_cover_still_refuses(store):
    # both the models floors and the admission escrow fail; covering one is not enough
    with pytest.raises(PlanCheckFailedError) as exc:
        commit(store, round0_like(), plan_suite="aiif_round", round_id=ROUND,
               accepted_deviations={"admission_escrow_planned": "r"}, decided_by="DEC-1")
    assert set(exc.value.result.must_failures) == {"models_floors_met", "admission_escrow_planned"}
    assert escrow_files(store) == [] and prereg_rows(store) == []


def test_no_plan_suite_is_unchanged(store):
    params = {"a": 1}
    row = commit(store, params)
    assert not set(NEW_COLUMNS) & set(row)  # the returned row is exactly what it was
    stored = get(store, "prereg", pk_column="prereg_id", pk_value=row["prereg_id"])
    assert all(stored[c] is None for c in NEW_COLUMNS)
    escrowed = json.loads(Path(row["escrow_path"]).read_text(encoding="utf-8"))
    assert escrowed["params"] == params and set(escrowed) == {"prereg_id", "title", "procedure", "params", "committed_ts"}
    assert row["params_sha256"] == sha256_hex(canonical_json(params))
    with pytest.raises(ValidationError):
        commit(store, params, round_id="R-1")  # plan arguments without a suite are not silently dropped


def test_plan_suite_needs_round_id_and_known_suite(store, full_models):
    with pytest.raises(ValidationError):
        commit(store, round0_like(rooms=False), plan_suite="aiif_round")
    with pytest.raises(UnknownPlanSuiteError):
        commit(store, round0_like(rooms=False), plan_suite="nope", round_id=ROUND)
    assert escrow_files(store) == []


def test_check_prereg_plan_writes_nothing(store, full_models):
    result = check_prereg_plan(store, plan_suite="aiif_round", params=round0_like(), round_id=ROUND)
    assert result.overall == "fail" and result.must_failures == ["admission_escrow_planned"]
    assert escrow_files(store) == [] and prereg_rows(store) == []
    with pytest.raises(PreregNotFoundError):
        check_prereg_plan(store, plan_suite="aiif_round_admission", params={}, round_id=ROUND, parent_prereg_id="PREG-none")


# --- the second escrow ------------------------------------------------------


def parent_prereg(store, *, rooms=None, round_id=ROUND):
    extra = {} if rooms is None else {"rooms": rooms}
    params = round0_like(admission_escrow={"by": "second_prereg_commit"}, **extra)
    return commit(store, params, plan_suite="aiif_round", round_id=round_id)


def commit_admission(store, parent_id, hash_value=HEX64, round_id=ROUND):
    return commit(
        store, {"admission_order_hash": hash_value}, plan_suite="aiif_round_admission",
        round_id=round_id, parent_prereg_id=parent_id,
    )


def test_admission_suite(store, full_models):
    with pytest.raises(PreregNotFoundError):
        commit_admission(store, "PREG-missing")
    with pytest.raises(PlanCheckFailedError):  # no parent at all
        commit(store, {"admission_order_hash": HEX64}, plan_suite="aiif_round_admission", round_id=ROUND)

    no_rooms = parent_prereg(store, rooms=False)
    with pytest.raises(PlanCheckFailedError) as exc:
        commit_admission(store, no_rooms["prereg_id"])
    assert "no rooms" in str(exc.value)

    parent = parent_prereg(store)
    with pytest.raises(PlanCheckFailedError):
        commit_admission(store, parent["prereg_id"], round_id="R-TEST-9")
    with pytest.raises(PlanCheckFailedError):
        commit_admission(store, parent["prereg_id"], hash_value="xyz")
    plain = commit(store, {"a": 1})  # a parent that never had a plan check
    with pytest.raises(PlanCheckFailedError):
        commit_admission(store, plain["prereg_id"])

    row = commit_admission(store, parent["prereg_id"])
    assert row["parent_prereg_id"] == parent["prereg_id"]
    assert row["plan_check_status"] == "pass" and row["round_id"] == ROUND


def _consolidated_pool(store, round_id=ROUND, n=6):
    launch = bootstrap_launch(store, agent_kind="lens")
    for i in range(n):
        insert(
            store, "idea",
            {
                "idea_id": new_id("IDEA"), "round_id": round_id, "author_launch": launch,
                "body": f"idea {i}", "status": "consolidated", "created_ts": now(),
                "tier": "near" if i % 2 else "far", "recipe_card": "MISMATCH" if i % 3 else "TRANSFER",
                "home": "family/cell",
            },
        )


def test_admission_suite_recomputes_the_order_from_the_pool(store, full_models):
    from trialerror.rooms.api import build_admission_order, consolidated_ideas_for_admission

    parent = parent_prereg(store)
    _consolidated_pool(store)
    ideas = consolidated_ideas_for_admission(store, round_id=ROUND)
    good = build_admission_order(ideas, seed="R-TEST-0-rooms-x", enforce_batch_band=False)["hash"]

    row = commit_admission(store, parent["prereg_id"], hash_value=good)
    assert row["plan_check_status"] == "pass"
    item = next(i for i in json.loads(row["plan_check"])["items"] if i["check_id"] == "order_recomputed")
    assert item["status"] == "pass" and "matches" in item["message"]

    other = commit_admission(store, parent["prereg_id"], hash_value=HEX64)
    assert other["plan_check_status"] == "pass_with_warnings"
    loud = next(i for i in json.loads(other["plan_check"])["items"] if i["check_id"] == "order_recomputed")
    assert loud["status"] == "fail" and "DOES NOT MATCH" in loud["message"]
