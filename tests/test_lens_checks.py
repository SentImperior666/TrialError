"""``trialerror.lens.checks`` — doctor checks over ``lens_assignment``. Adversarial
scenarios are planted via a raw ``trialerror.stores.insert`` bypassing
``trialerror.lens.assign`` entirely (same pattern
``tests/test_artifacts_checks.py``-equivalent files use for M10's own
checks trio: the normal write path structurally cannot produce these
states, so the check's job is catching a DIRECT write that did)."""

from __future__ import annotations

import json

import pytest

from trialerror.lens.assign import run_assignment
from trialerror.lens.checks import check_cluster_coverage, check_far_arm_floor_honored, check_no_duplicate_slice
from trialerror.lens.roster import add_lens
from trialerror.stores import insert
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._lens_fixtures import build_doc_pool


@pytest.fixture()
def ctx(program_root) -> DoctorContext:
    return DoctorContext(program_root=program_root)


def test_checks_skip_when_ops_db_absent(tmp_path):
    ctx = DoctorContext(program_root=tmp_path / "no-such-program")
    for fn in (check_far_arm_floor_honored, check_no_duplicate_slice, check_cluster_coverage):
        result = fn(ctx)
        assert result.status == "skip"


def test_checks_skip_when_no_assignment_rows(store, ctx):
    # store fixture creates ops.db (via open_store) but writes nothing.
    for fn in (check_far_arm_floor_honored, check_no_duplicate_slice, check_cluster_coverage):
        result = fn(ctx)
        assert result.status == "skip"


def test_far_arm_floor_honored_passes_for_real_assignment(store, ctx):
    pool = build_doc_pool(store, n_docs=12)
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="v", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]
    run_assignment(
        store, round_id="round-1", model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=[{"roster_id": lens_row["roster_id"]}],
        slices_per_lens=5, seed="seed-A", far_floor=2,
    )
    result = check_far_arm_floor_honored(ctx)
    assert result.status == "pass"


def test_far_arm_floor_honored_fails_on_direct_write_bypassing_assign(store, ctx):
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="v", model_class="top")
    # Plant a single 'near' assignment row claiming far_floor=2 with zero
    # far-arm rows -- unreachable via trialerror.lens.assign.run_assignment.
    insert(
        store, "lens_assignment",
        {
            "assign_id": new_id("ASGN"), "roster_id": lens_row["roster_id"],
            "slice_spec": json.dumps({"round_id": "round-1", "candidate_id": "DOC-x"}),
            "arm": "near", "weights": "[40,40,20]", "far_floor": 2, "inter_cluster_mandate": 0,
            "seed": "seed-A", "created_ts": now(),
        },
    )
    result = check_far_arm_floor_honored(ctx)
    assert result.status == "fail"
    assert result.details["offenders"][0]["roster_id"] == lens_row["roster_id"]


def test_no_duplicate_slice_passes_for_real_assignment(store, ctx):
    pool = build_doc_pool(store, n_docs=30)
    lens_rows = [
        add_lens(store, round_id="round-1", lens_name=f"lens-{i}", vantage="v", model_class="top")
        for i in range(3)
    ]
    home_id, *candidate_ids = pool["doc_ids"]
    run_assignment(
        store, round_id="round-1", model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=[{"roster_id": r["roster_id"]} for r in lens_rows],
        slices_per_lens=5, seed="seed-A",
    )
    result = check_no_duplicate_slice(ctx)
    assert result.status == "pass"


def test_no_duplicate_slice_fails_on_direct_write_double_assignment(store, ctx):
    lens1 = add_lens(store, round_id="round-1", lens_name="lens-1", vantage="v", model_class="top")
    lens2 = add_lens(store, round_id="round-1", lens_name="lens-2", vantage="v", model_class="top")
    for lens in (lens1, lens2):
        insert(
            store, "lens_assignment",
            {
                "assign_id": new_id("ASGN"), "roster_id": lens["roster_id"],
                "slice_spec": json.dumps({"round_id": "round-1", "candidate_id": "DOC-shared"}),
                "arm": "near", "weights": "[40,40,20]", "far_floor": 2, "inter_cluster_mandate": 0,
                "seed": "seed-A", "created_ts": now(),
            },
        )
    result = check_no_duplicate_slice(ctx)
    assert result.status == "fail"
    assert result.details["offenders"][0]["candidate_id"] == "DOC-shared"
    assert len(result.details["offenders"][0]["assign_ids"]) == 2


def test_cluster_coverage_skips_when_no_cluster_labels(store, ctx):
    pool = build_doc_pool(store, n_docs=12)
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="v", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]
    run_assignment(
        store, round_id="round-1", model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=[{"roster_id": lens_row["roster_id"]}],
        slices_per_lens=5, seed="seed-A",
    )
    result = check_cluster_coverage(ctx)
    assert result.status == "skip"


def test_cluster_coverage_warns_on_under_represented_cluster(store, ctx):
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="v", model_class="top")
    insert(
        store, "lens_assignment",
        {
            "assign_id": new_id("ASGN"), "roster_id": lens_row["roster_id"],
            "slice_spec": json.dumps({"round_id": "round-1", "candidate_id": "DOC-x", "cluster_id": "lonely-cluster"}),
            "arm": "far", "weights": "[40,40,20]", "far_floor": 2, "inter_cluster_mandate": 0,
            "seed": "seed-A", "created_ts": now(),
        },
    )
    result = check_cluster_coverage(ctx, min_sets=2)
    assert result.status == "warn"
    assert result.details["under_covered"][0]["cluster_id"] == "lonely-cluster"
