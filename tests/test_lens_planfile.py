"""``trialerror.lens.planfile``: a plan file becomes a round's
``lens_assignment`` rows in ``lens assign``'s own shape, or nothing at all.

- the round trip: plan -> rows -> the projection hash the planner recorded;
- one refusal per check V1-V8, each writing nothing and naming what failed;
- the control seat: the quota is over the non-control seats, and the control
  sits in that quota's modal arm;
- the rollback: rows that do not read back as planned leave no row behind.

The CLI mode, and the doctor checks over a round it wrote, are in
``tests/test_lens_planfile_cli.py``.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from trialerror.lens.assign import LENS_NAME_SALT_SCHEME, list_assignments, row_salt_scheme
from trialerror.lens.planfile import (
    PlanFileRefusedError,
    load_plan_file,
    plan_projection,
    plan_sha256,
    sha256_of,
    write_plan,
)
from trialerror.lens.quota import compute_quota_counts

from tests._planfile_fixtures import (
    LENSES,
    ROUND_ID,
    ROWS_PER_LENS,
    SEED,
    add_documents,
    build_round,
    edited,
    lens_entry,
    rehash,
    write_plan_file,
)

#: The columns ``run_assignment`` writes on every row (lens/assign.py), which
#: the plan-file verb must write too and nothing else.
ASSIGN_COLUMNS = {
    "assign_id", "roster_id", "slice_spec", "arm", "weights", "far_floor", "arm_mode",
    "far_lens_floor", "recipe_cards", "inter_cluster_mandate", "seed", "launch_id", "created_ts",
}


@pytest.fixture()
def fixture_round(store):
    return build_round(store)


def _write(store, tmp_path, plan, *, launch_id, round_id=ROUND_ID, **kwargs):
    path = write_plan_file(tmp_path / "plan.json", plan)
    return write_plan(store, plan_path=path, round_id=round_id, launch_id=launch_id, **kwargs)


def _refused(store, tmp_path, plan, *, launch_id, **kwargs) -> PlanFileRefusedError:
    with pytest.raises(PlanFileRefusedError) as excinfo:
        _write(store, tmp_path, plan, launch_id=launch_id, **kwargs)
    assert _row_count(store) == 0, "a refused plan must write nothing"
    return excinfo.value


def _row_count(store) -> int:
    return store.ops.execute("SELECT COUNT(*) AS n FROM lens_assignment").fetchone()["n"]


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------


def test_round_trip_plan_to_rows_to_the_planners_projection_hash(store, tmp_path, fixture_round):
    plan = fixture_round["plan"]
    result = _write(store, tmp_path, plan, launch_id=fixture_round["launch_id"])

    assert result["n_rows"] == ROWS_PER_LENS * len(LENSES)
    assert result["n_lenses"] == len(LENSES)
    # The planner recorded sha(projection) in its annex; the read-back reproduces it.
    assert result["projection_sha256"] == plan["annex"]["projection_sha256"]
    assert result["projection_sha256"] == sha256_of(plan_projection(plan))
    raw = (tmp_path / "plan.json").read_bytes()
    assert result["plan_file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert set(result) == {"n_rows", "n_lenses", "projection_sha256", "plan_file_sha256", "assign_ids"}

    rows = list_assignments(store, round_id=ROUND_ID)
    assert len(rows) == result["n_rows"]
    by_id = {r["assign_id"]: r for r in rows}
    roster = fixture_round["roster"]
    for name, _seat, _cards, arm in LENSES:
        ids = result["assign_ids"][name]
        assert len(ids) == ROWS_PER_LENS
        for rank, assign_id in enumerate(ids):
            row = by_id[assign_id]
            spec = json.loads(row["slice_spec"])
            planned = lens_entry(plan, name)["rows"][rank]
            assert spec == {
                "round_id": ROUND_ID, "candidate_id": planned["candidate_id"],
                "distance_score": planned["distance_score"], "cluster_id": planned["cluster_id"],
                "rank": rank, "salt_scheme": "lens-name", "plan_sha256": plan["plan_sha256"],
                **planned["extra"],
            }
            assert row["roster_id"] == roster[name]["roster_id"]
            assert row["arm"] == arm
            assert row["far_floor"] == (ROWS_PER_LENS if arm == "far" else 0)
            assert row["weights"] == json.dumps([40, 40, 20])
            assert row["arm_mode"] == "per_lens"
            assert row["far_lens_floor"] == 2
            assert row["recipe_cards"] == roster[name]["recipe_cards"]
            assert row["inter_cluster_mandate"] == 1
            assert row["seed"] == SEED
            assert row["launch_id"] == fixture_round["launch_id"]
            assert row["lens_launch_id"] is None
            assert row_salt_scheme(row) == LENS_NAME_SALT_SCHEME
            written = {k for k in ASSIGN_COLUMNS if row.get(k) is not None}
            assert written == ASSIGN_COLUMNS - ({"recipe_cards"} if roster[name]["recipe_cards"] is None else set())


def test_rows_are_written_in_lens_name_then_rank_order(store, tmp_path, fixture_round):
    _write(store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"])
    rows = list_assignments(store, round_id=ROUND_ID)  # insertion order
    order = [(r["lens_name"], json.loads(r["slice_spec"])["rank"]) for r in rows]
    assert order == sorted(order)


def test_row_order_in_the_file_does_not_matter(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    plan["lenses"].reverse()
    for lens in plan["lenses"]:
        lens["rows"].reverse()
    rehash(plan)
    result = _write(store, tmp_path, plan, launch_id=fixture_round["launch_id"])
    assert result["projection_sha256"] == fixture_round["plan"]["annex"]["projection_sha256"]


def test_an_expected_hash_that_matches_is_accepted(store, tmp_path, fixture_round):
    plan = fixture_round["plan"]
    result = _write(
        store, tmp_path, plan, launch_id=fixture_round["launch_id"],
        expect_plan_sha256=plan["plan_sha256"].upper(),
    )
    assert result["n_rows"] == ROWS_PER_LENS * len(LENSES)


# ---------------------------------------------------------------------------
# V1 .. V8, one refusal each
# ---------------------------------------------------------------------------


def test_v1_refuses_another_format(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    plan["format"] = "trialerror-plan-file/2"
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_format_mismatch", "V1")
    assert "trialerror-plan-file/2" in str(err)


def test_v1_refuses_another_format_before_the_shape_check(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    plan["format"] = "trialerror-plan-file/2"
    plan["new_key"] = 1
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_format_mismatch", "V1")


def test_v1_refuses_a_plan_for_another_round(store, tmp_path, fixture_round):
    err = _refused(
        store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"], round_id="R-TEST-2"
    )
    assert (err.code, err.check) == ("plan_format_mismatch", "V1")
    assert "R-TEST-2" in str(err) and ROUND_ID in str(err)


def test_v2_refuses_a_roster_lens_the_plan_leaves_out(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    plan["lenses"] = [lens for lens in plan["lenses"] if lens["lens_name"] != "lens-c"]
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_roster_mismatch", "V2")
    assert "lens 'lens-c': on the roster, not in the plan" in err.items


def test_v2_refuses_rows_for_a_name_the_roster_does_not_hold(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-c")["lens_name"] = "lens-z"
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert err.check == "V2"
    assert "lens 'lens-z': in the plan, not on the roster" in err.items


def test_v2_refuses_a_lens_with_no_rows(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-a")["rows"] = []
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert err.check == "V2"
    assert "lens 'lens-a': no rows" in err.items


def test_v3_refuses_a_candidate_placed_twice(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    twice = lens_entry(plan, "lens-a")["rows"][0]["candidate_id"]
    lens_entry(plan, "lens-b")["rows"][0]["candidate_id"] = twice
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_candidate_refused", "V3")
    assert any(twice in item and "appears 2 times" in item for item in err.items)


def test_v3_refuses_a_candidate_that_names_no_document(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-a")["rows"][3]["candidate_id"] = "DOC-00000000000000000000000000"
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert err.check == "V3"
    assert any("names no document" in item and "lens-a#3" in item for item in err.items)


def test_v3_refuses_an_inventory_document(store, tmp_path, fixture_round):
    (inventory_doc,) = add_documents(store, launch_id=fixture_round["launch_id"], n=1, kind="inventory")
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-b")["rows"][5]["candidate_id"] = inventory_doc
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert err.check == "V3"
    assert any(inventory_doc in item and "'inventory'" in item for item in err.items)


def test_v3_a_plan_is_written_once(store, tmp_path, fixture_round):
    _write(store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"])
    before = _row_count(store)
    with pytest.raises(PlanFileRefusedError) as excinfo:
        _write(store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"])
    assert (excinfo.value.code, excinfo.value.check) == ("plan_round_already_assigned", "V3")
    assert _row_count(store) == before


def test_v4_refuses_an_assumption_buster_outside_the_far_arm(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    # Swap arms with a near lens, so the quota counts still hold.
    lens_entry(plan, "lens-f")["arm"], lens_entry(plan, "lens-c")["arm"] = "near", "far"
    lens_entry(plan, "lens-f")["far_floor"], lens_entry(plan, "lens-c")["far_floor"] = 0, ROWS_PER_LENS
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_arms_refused", "V4")
    assert any("lens-f" in item and "assumption-buster" in item for item in err.items)


def test_v4_refuses_arm_counts_off_the_quota(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-d")["arm"] = "near"
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert err.check == "V4"
    assert any(item.startswith("arm 'near': 4 non-control") for item in err.items)
    assert any(item.startswith("arm 'moderate': 0 non-control") for item in err.items)


def test_v4_refuses_fewer_than_two_far_lenses_whatever_the_declared_floor(store, tmp_path):
    """far_lens_floor=1 makes the quota 3/2/1, and a plan that follows it
    exactly still seats one far lens: the hard floor of two refuses it."""
    lenses = (
        ("lens-a", "standard", ["CARD-A", "CARD-B"], "near"),
        ("lens-b", "standard", ["CARD-C", "CARD-D"], "near"),
        ("lens-c", "standard", ["CARD-A", "CARD-C"], "near"),
        ("lens-d", "standard", ["CARD-B", "CARD-D"], "moderate"),
        ("lens-e", "standard", ["CARD-A", "CARD-D"], "moderate"),
        ("lens-f", "assumption_buster", ["NEGATE"], "far"),
    )
    assert compute_quota_counts(6, weights=(40, 40, 20), far_floor=1) == {"near": 3, "moderate": 2, "far": 1}
    built = build_round(store, lenses, far_lens_floor=1)
    err = _refused(store, tmp_path, built["plan"], launch_id=built["launch_id"])
    assert err.check == "V4"
    assert err.items == [
        "far lenses: 1 non-control lens(es) are far, at least 2 must be "
        "(the harder of far_lens_floor=1 and the hard floor 2)"
    ]


def test_v5_refuses_a_far_floor_that_is_not_the_row_count(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-e")["far_floor"] = ROWS_PER_LENS - 1
    lens_entry(plan, "lens-a")["far_floor"] = 2
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_far_floor_mismatch", "V5")
    assert err.items == [
        "lens 'lens-a' (near): far_floor 2, expected 0",
        f"lens 'lens-e' (far): far_floor {ROWS_PER_LENS - 1}, expected {ROWS_PER_LENS}",
    ]


@pytest.mark.parametrize("ranks", [[0, 1, 2, 3, 4, 5, 6, 8], [0, 1, 2, 3, 4, 5, 6, 6], [1, 2, 3, 4, 5, 6, 7, 8]])
def test_v6_refuses_ranks_that_are_not_zero_to_n_minus_one(store, tmp_path, fixture_round, ranks):
    plan = edited(fixture_round["plan"])
    for row, rank in zip(lens_entry(plan, "lens-b")["rows"], ranks):
        row["rank"] = rank
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_ranks_refused", "V6")
    assert err.items == [f"lens 'lens-b': ranks {sorted(ranks)}, expected 0..7"]


@pytest.mark.parametrize("key", ["round_id", "candidate_id", "distance_score", "cluster_id", "rank", "salt_scheme", "plan_sha256"])
def test_v7_refuses_an_extra_key_the_verb_writes_itself(store, tmp_path, fixture_round, key):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-d")["rows"][2]["extra"][key] = "x"
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_extra_key_reserved", "V7")
    assert err.items == [f"lens 'lens-d' rank 2: extra uses [{key!r}]"]


def test_v8_refuses_a_plan_edited_after_it_was_hashed(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    plan["annex"]["planner"]["version"] = "edited"  # annex is hashed, never read
    err = _refused(store, tmp_path, plan, launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_sha256_mismatch", "V8")
    assert plan_sha256(plan) in str(err)


def test_v8_refuses_a_plan_the_caller_did_not_expect(store, tmp_path, fixture_round):
    err = _refused(
        store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"],
        expect_plan_sha256="0" * 64,
    )
    assert err.check == "V8"
    assert any(item.startswith("--expect-plan-sha256") for item in err.items)


# ---------------------------------------------------------------------------
# the file's shape, and the launch
# ---------------------------------------------------------------------------


def test_the_shape_check_names_missing_and_unknown_keys(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    del plan["salt_scheme"]
    plan["salt"] = "lens-name"
    err = _refused(store, tmp_path, plan, launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_malformed", "shape")
    assert err.items == ["plan: missing key 'salt_scheme'", "plan: unknown key 'salt'"]


def test_the_shape_check_refuses_a_per_slice_plan_and_a_bad_row(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    plan["arm_mode"] = "per_slice"
    lens_entry(plan, "lens-a")["rows"][1]["rank"] = "1"
    err = _refused(store, tmp_path, plan, launch_id=fixture_round["launch_id"])  # shape runs before V8
    assert err.check == "shape"
    assert any(item.startswith("arm_mode: 'per_slice'") for item in err.items)
    assert "lens 'lens-a'.rows[1].rank: not a non-negative integer" in err.items


@pytest.mark.parametrize(
    "text, fragment",
    [
        ('{"a": 1, "a": 2}', "appears twice"), ('{"a": NaN}', "not a JSON number"), ("[1, 2]", "not a JSON object"),
        ('{"a": {"b": 1e400}}', "too large for a float"), ('{"a": "\\ud800"}', "lone surrogate"),
    ],
)
def test_the_reader_is_strict_json(tmp_path, text, fragment):
    path = tmp_path / "plan.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PlanFileRefusedError) as excinfo:
        load_plan_file(path)
    assert fragment in str(excinfo.value)


def test_an_unknown_launch_writes_nothing(store, tmp_path, fixture_round):
    err = _refused(store, tmp_path, fixture_round["plan"], launch_id="LNCH-00000000000000000000000000")
    assert (err.code, err.check) == ("plan_launch_unknown", "launch")


# ---------------------------------------------------------------------------
# the control seat
# ---------------------------------------------------------------------------


def test_the_control_seat_is_outside_the_quota_and_in_its_modal_arm(store, tmp_path, fixture_round):
    """Six non-control lenses at 40/40/20, floor 2: the quota is 3/1/2, modal
    near, and the control sits near beside it -- 4/1/2 on the whole roster.
    Counting the control against the quota, as ``lens assign``'s own draw
    does, would make it compute_quota_counts(7) = 3/2/2 instead."""
    assert compute_quota_counts(6, weights=(40, 40, 20), far_floor=2) == {"near": 3, "moderate": 1, "far": 2}
    assert compute_quota_counts(7, weights=(40, 40, 20), far_floor=2) == {"near": 3, "moderate": 2, "far": 2}
    _write(store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"])
    rows = list_assignments(store, round_id=ROUND_ID)
    arms = {r["lens_name"]: r["arm"] for r in rows}
    assert arms["lens-g"] == "near" and next(r["seat"] for r in rows if r["lens_name"] == "lens-g") == "control"
    assert sorted(arms[name] for name, seat, _c, _a in LENSES if seat != "control") == [
        "far", "far", "moderate", "near", "near", "near",
    ]


def test_a_plan_that_counts_the_control_against_the_quota_is_refused(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-c")["arm"] = "moderate"  # 3/2/2 over all seven, control near
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert err.check == "V4"
    assert any(item.startswith("arm 'near': 2 non-control") for item in err.items)


def test_a_control_outside_the_modal_arm_is_refused(store, tmp_path, fixture_round):
    plan = edited(fixture_round["plan"])
    lens_entry(plan, "lens-g")["arm"] = "moderate"
    err = _refused(store, tmp_path, rehash(plan), launch_id=fixture_round["launch_id"])
    assert err.check == "V4"
    assert err.items == [
        "lens 'lens-g': a control seat sits in the modal arm of the non-control quota ('near'), "
        "the plan says 'moderate'"
    ]


def test_the_modal_arm_follows_the_weights(store, tmp_path):
    """At 20/60/20 the six non-control lenses split 1/3/2 and the modal arm
    is moderate: the control belongs there, not near."""
    assert compute_quota_counts(6, weights=(20, 60, 20), far_floor=2) == {"near": 1, "moderate": 3, "far": 2}
    lenses = (
        ("lens-a", "standard", ["CARD-A", "CARD-B"], "near"),
        ("lens-b", "standard", ["CARD-C", "CARD-D"], "moderate"),
        ("lens-c", "standard", ["CARD-A", "CARD-C"], "moderate"),
        ("lens-d", "standard", ["CARD-B", "CARD-D"], "moderate"),
        ("lens-e", "standard", ["CARD-A", "CARD-D"], "far"),
        ("lens-f", "assumption_buster", ["NEGATE"], "far"),
        ("lens-g", "control", None, "moderate"),
    )
    built = build_round(store, lenses, weights=(20, 60, 20))
    result = _write(store, tmp_path, built["plan"], launch_id=built["launch_id"])
    assert result["n_lenses"] == 7

    store.ops.execute("DELETE FROM lens_assignment")
    store.ops.commit()
    plan = edited(built["plan"])
    lens_entry(plan, "lens-g")["arm"] = "near"
    err = _refused(store, tmp_path, rehash(plan), launch_id=built["launch_id"])
    assert err.check == "V4" and "('moderate')" in err.items[0]


# ---------------------------------------------------------------------------
# the rollback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        # the store moves a far row to near after the insert
        "UPDATE lens_assignment SET arm = 'near' WHERE assign_id = NEW.assign_id AND NEW.arm = 'far'",
        # the store rewrites a slice_spec after the insert
        "UPDATE lens_assignment SET slice_spec = replace(NEW.slice_spec, '\"C1\"', '\"C9\"') "
        "WHERE assign_id = NEW.assign_id",
    ],
)
def test_a_read_back_mismatch_rolls_everything_back(store, tmp_path, fixture_round, tamper):
    store.ops.execute(f"CREATE TRIGGER tamper AFTER INSERT ON lens_assignment BEGIN {tamper}; END")
    store.ops.commit()

    err = _refused(store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"])
    assert (err.code, err.check) == ("plan_readback_mismatch", "read-back")
    assert any(item.startswith("projection:") for item in err.items)
    assert store.ops.in_transaction is False

    # Nothing was left behind: with the tampering gone, the same plan is
    # written in full (the write-once check sees an unassigned round).
    store.ops.execute("DROP TRIGGER tamper")
    store.ops.commit()
    result = _write(store, tmp_path, fixture_round["plan"], launch_id=fixture_round["launch_id"])
    assert result["n_rows"] == _row_count(store) == ROWS_PER_LENS * len(LENSES)
