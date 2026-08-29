"""``trialerror.lens.assign``: the pure planner's determinism (the literal
"stratify on fixture corpus reproduces byte-identical arms from same seed"
acceptance wording) plus the Store-backed write/read round trip."""

from __future__ import annotations

import json
import math

import pytest

from trialerror.lens.assign import build_assignment_plan, list_assignments, plan_to_json, run_assignment
from trialerror.lens.errors import InsufficientCandidatesError
from trialerror.lens.roster import add_lens
from tests._lens_fixtures import build_doc_pool


def _candidates_on_arc(n: int) -> dict[str, list[float]]:
    """``n`` 2D unit vectors at evenly-spaced angles 0..pi from [1,0] — a
    STRICTLY increasing cosine-distance-from-home sequence, so the tercile
    cut lands exactly where ``n // 3`` / ``(2*n)//3`` arithmetic predicts
    (see ``test_lens_stratify.py``), with no score ties to worry about."""
    denom = max(n - 1, 1)
    return {f"C{i}": [math.cos(i * math.pi / denom), math.sin(i * math.pi / denom)] for i in range(n)}


_HOME = {"H": [1.0, 0.0]}


def test_build_assignment_plan_byte_identical_for_same_seed():
    candidates = _candidates_on_arc(30)
    lenses = [{"roster_id": "ROST-1"}, {"roster_id": "ROST-2"}, {"roster_id": "ROST-3"}]
    plan1 = build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=5, seed="seed-A")
    plan2 = build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=5, seed="seed-A")
    assert plan_to_json(plan1) == plan_to_json(plan2)


def test_build_assignment_plan_different_seed_differs():
    candidates = _candidates_on_arc(30)
    lenses = [{"roster_id": "ROST-1"}, {"roster_id": "ROST-2"}]
    plan_a = build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=5, seed="seed-A")
    plan_b = build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=5, seed="seed-B")
    assert plan_to_json(plan_a) != plan_to_json(plan_b)


def test_build_assignment_plan_weights_and_floor_honored_per_lens():
    candidates = _candidates_on_arc(30)
    lenses = [{"roster_id": "ROST-1"}, {"roster_id": "ROST-2"}]
    plan = build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=5, seed="seed-A", weights=(40, 40, 20), far_floor=2)
    for lens_plan in plan["lenses"]:
        assert lens_plan["quota"] == {"near": 2, "moderate": 1, "far": 2}
        arms = [s["arm"] for s in lens_plan["slices"]]
        assert arms.count("near") == 2
        assert arms.count("moderate") == 1
        assert arms.count("far") == 2
        assert len(arms) == 5


def test_build_assignment_plan_no_duplicate_slices_across_lenses():
    candidates = _candidates_on_arc(30)
    lenses = [{"roster_id": f"ROST-{i}"} for i in range(4)]
    plan = build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=5, seed="seed-A")
    all_ids = [s["candidate_id"] for lp in plan["lenses"] for s in lp["slices"]]
    assert len(all_ids) == len(set(all_ids))


def test_build_assignment_plan_raises_when_pool_exhausted():
    candidates = _candidates_on_arc(6)  # too small for 2 lenses x 5 slices
    lenses = [{"roster_id": "ROST-1"}, {"roster_id": "ROST-2"}]
    with pytest.raises(InsufficientCandidatesError):
        build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=5, seed="seed-A")


def test_build_assignment_plan_inter_cluster_mandate_excludes_home_cluster_from_far():
    candidates = _candidates_on_arc(9)  # near=[0,1,2] moderate=[3,4,5] far=[6,7,8]
    cluster_of = {f"C{i}": ("home_cluster" if i in (6, 7) else "other_cluster") for i in range(9)}
    lenses = [{"roster_id": "ROST-1"}]
    plan = build_assignment_plan(
        candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=1, seed="seed-A",
        weights=(0, 0, 100), far_floor=1, inter_cluster_mandate=True, cluster_of=cluster_of, home_cluster="home_cluster",
    )
    far_ids = [s["candidate_id"] for s in plan["lenses"][0]["slices"] if s["arm"] == "far"]
    assert far_ids  # something was drawn
    assert "C6" not in far_ids and "C7" not in far_ids  # excluded: same cluster as home
    assert set(far_ids) <= {"C8"}


def test_build_assignment_plan_inter_cluster_mandate_requires_cluster_info():
    candidates = _candidates_on_arc(9)
    lenses = [{"roster_id": "ROST-1"}]
    with pytest.raises(ValueError):
        build_assignment_plan(
            candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=3, seed="seed-A",
            inter_cluster_mandate=True,
        )


def test_plan_to_json_is_sorted_keys_and_stable():
    candidates = _candidates_on_arc(6)
    lenses = [{"roster_id": "ROST-1"}]
    plan = build_assignment_plan(candidates=candidates, home=_HOME, lenses=lenses, slices_per_lens=2, seed="seed-A", far_floor=0)
    rendered = plan_to_json(plan)
    reparsed = json.loads(rendered)
    assert reparsed == plan
    assert plan_to_json(plan) == rendered  # calling again is still byte-identical


# ---------------------------------------------------------------------------
# Store-backed integration: run_assignment / list_assignments
# ---------------------------------------------------------------------------


def test_run_assignment_writes_rows_and_list_assignments_reads_them_back(store):
    pool = build_doc_pool(store, n_docs=12)
    launch_id = pool["launch_id"]
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="adversarial", model_class="top")

    home_id, *candidate_ids = pool["doc_ids"]
    result = run_assignment(
        store, round_id="round-1", model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": lens_row["roster_id"]}], slices_per_lens=5, seed="seed-A",
        far_floor=2, launch_id=launch_id,
    )
    assert len(result["rows"]) == 5
    for row in result["rows"]:
        assert row["roster_id"] == lens_row["roster_id"]
        assert row["seed"] == "seed-A"
        assert row["far_floor"] == 2
        assert json.loads(row["weights"]) == [40, 40, 20]
        spec = json.loads(row["slice_spec"])
        assert spec["round_id"] == "round-1"
        assert spec["candidate_id"] in candidate_ids

    logged = list_assignments(store, round_id="round-1")
    assert len(logged) == 5
    assert {r["assign_id"] for r in logged} == {r["assign_id"] for r in result["rows"]}
    assert all(r["lens_name"] == "skeptic" for r in logged)


def test_run_assignment_reproducible_logical_content_across_two_calls(store):
    pool = build_doc_pool(store, n_docs=12)
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="adversarial", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]

    kwargs = dict(
        store=store, round_id="round-1", model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": lens_row["roster_id"]}], slices_per_lens=5, seed="seed-A", far_floor=2,
    )
    result1 = run_assignment(**kwargs)
    result2 = run_assignment(**kwargs)

    def _logical(rows):
        return sorted(
            (json.loads(r["slice_spec"])["candidate_id"], r["arm"]) for r in rows
        )

    assert _logical(result1["rows"]) == _logical(result2["rows"])
    # DB identity differs run over run (fresh ULIDs/timestamps) -- never
    # claimed to be reproducible, only the assignment DECISIONS are.
    assert {r["assign_id"] for r in result1["rows"]}.isdisjoint({r["assign_id"] for r in result2["rows"]})


def test_run_assignment_no_duplicate_candidate_across_lenses_in_one_round(store):
    pool = build_doc_pool(store, n_docs=30)
    lens_rows = [
        add_lens(store, round_id="round-1", lens_name=f"lens-{i}", vantage="v", model_class="top")
        for i in range(4)
    ]
    home_id, *candidate_ids = pool["doc_ids"]
    result = run_assignment(
        store, round_id="round-1", model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": r["roster_id"]} for r in lens_rows], slices_per_lens=5, seed="seed-A",
    )
    candidate_hits = [json.loads(r["slice_spec"])["candidate_id"] for r in result["rows"]]
    assert len(candidate_hits) == len(set(candidate_hits))
