"""M13 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m10_acceptance.py`` convention: this file IS the
acceptance-criteria mapping, each test here re-running (not replacing) a
narrower assertion that already lives in its dedicated module.

    | Acceptance criterion (design Section 12, M13 row)                              | Test |
    |----------------------------------------------------------------------------------|------|
    | stratify on fixture corpus reproduces byte-identical arms from same seed         | test_stratify_reproduces_byte_identical_arms_from_same_seed (see test_lens_assign.py::test_build_assignment_plan_byte_identical_for_same_seed / test_lens_stratify.py for the pure-algorithm-level proof) |
    | weights/floor honored                                                            | test_weights_floor_honored_end_to_end (see test_lens_quota.py / test_lens_assign.py::test_build_assignment_plan_weights_and_floor_honored_per_lens) |
    | assignment rows logged                                                           | test_assignment_rows_logged_before_export (see test_lens_assign.py::test_run_assignment_writes_rows_and_list_assignments_reads_them_back) |
"""

from __future__ import annotations

import json

import pytest

from trialerror.lens.assign import build_assignment_plan, list_assignments, plan_to_json, run_assignment
from trialerror.lens.export import export_launch_bookable
from trialerror.lens.roster import add_lens
from tests._lens_fixtures import build_doc_pool

pytestmark = pytest.mark.acceptance


def _arc_candidates(n: int) -> dict[str, list[float]]:
    import math

    denom = max(n - 1, 1)
    return {f"C{i}": [math.cos(i * math.pi / denom), math.sin(i * math.pi / denom)] for i in range(n)}


def test_stratify_reproduces_byte_identical_arms_from_same_seed():
    """"stratify on fixture corpus reproduces byte-identical arms from same
    seed" — the literal wording. Two independent calls, same fixture
    corpus + seed, compared as canonical JSON strings (not just Python
    equality) so "byte-identical" is taken at face value."""
    candidates = _arc_candidates(45)
    home = {"H": [1.0, 0.0]}
    lenses = [{"roster_id": f"ROST-{i}"} for i in range(5)]

    plan_a = build_assignment_plan(candidates=candidates, home=home, lenses=lenses, slices_per_lens=6, seed="fixture-seed-42")
    plan_b = build_assignment_plan(candidates=candidates, home=home, lenses=lenses, slices_per_lens=6, seed="fixture-seed-42")
    assert plan_to_json(plan_a) == plan_to_json(plan_b)

    # A different seed must (with overwhelming probability, over 45
    # candidates split 5 ways) produce a different plan -- proving the seed
    # is actually load-bearing, not silently ignored.
    plan_c = build_assignment_plan(candidates=candidates, home=home, lenses=lenses, slices_per_lens=6, seed="fixture-seed-43")
    assert plan_to_json(plan_a) != plan_to_json(plan_c)

    # The near/moderate/far arm each candidate was cut into is itself part
    # of the byte-identical comparison above (plan["candidates"]), and is
    # independent of seed (tercile cut happens before the seeded draw).
    arms_a = {c["candidate_id"]: c["arm"] for c in plan_a["candidates"]}
    arms_c = {c["candidate_id"]: c["arm"] for c in plan_c["candidates"]}
    assert arms_a == arms_c


def test_weights_floor_honored_end_to_end(store):
    """Default weights (40/40/20) and far_floor (2) honored both in the
    pure plan and in the rows actually written to ``lens_assignment``."""
    pool = build_doc_pool(store, n_docs=20)
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="adversarial", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]

    result = run_assignment(
        store, round_id="round-1", model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=[{"roster_id": lens_row["roster_id"]}],
        slices_per_lens=5, seed="seed-A", weights=(40, 40, 20), far_floor=2,
    )
    rows = result["rows"]
    assert len(rows) == 5
    arm_counts = {"near": 0, "moderate": 0, "far": 0}
    for row in rows:
        arm_counts[row["arm"]] += 1
        assert json.loads(row["weights"]) == [40, 40, 20]
        assert row["far_floor"] == 2
    assert arm_counts == {"near": 2, "moderate": 1, "far": 2}
    assert arm_counts["far"] >= 2  # the floor, explicitly


def test_assignment_rows_logged_before_export(store):
    """"assignment rows logged" — logged means queryable back out of
    ``ops.lens_assignment`` (via ``list_assignments``), and available to
    ``export_launch_bookable`` for the orchestrator to book against, per
    the build brief's "assignment table logged BEFORE any spawn" framing:
    export only ever sees what's already durably written."""
    pool = build_doc_pool(store, n_docs=20)
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="adversarial", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]

    result = run_assignment(
        store, round_id="round-1", model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=[{"roster_id": lens_row["roster_id"]}],
        slices_per_lens=5, seed="seed-A",
    )
    written_ids = {r["assign_id"] for r in result["rows"]}

    logged = list_assignments(store, round_id="round-1")
    assert {r["assign_id"] for r in logged} == written_ids
    assert all(r["roster_id"] == lens_row["roster_id"] for r in logged)

    bookable = export_launch_bookable(store, round_id="round-1")
    assert len(bookable) == 1
    assert bookable[0]["attrs"]["assign_ids"] and set(bookable[0]["attrs"]["assign_ids"]) == written_ids
