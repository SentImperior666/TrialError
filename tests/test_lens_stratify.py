"""Pure-function tests for ``trialerror.lens.stratify`` — no Store, no
embeddings model, hand-picked distance values so tercile boundaries are
exactly predictable."""

from __future__ import annotations

import pytest

from trialerror.lens.errors import MissingEmbeddingError
from trialerror.lens.stratify import cosine_distance, score_candidates, stratify


def test_cosine_distance_identical_orthogonal_opposite():
    assert cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    assert cosine_distance([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(2.0)


def test_cosine_distance_degenerate_inputs_never_crash():
    assert cosine_distance([], [1.0]) == 1.0
    assert cosine_distance([1.0, 2.0], [1.0]) == 1.0  # mismatched length
    assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 1.0  # zero norm


def test_score_candidates_mean_pairwise_distance_over_home_set():
    # home = two vectors; candidate is equidistant-ish from both by design.
    home = {"H1": [1.0, 0.0], "H2": [0.0, 1.0]}
    candidates = {"C1": [1.0, 0.0]}  # distance 0 to H1, 1 to H2 -> mean 0.5
    scores = score_candidates(candidates, home)
    assert scores["C1"] == pytest.approx(0.5)


def test_score_candidates_refuses_empty_pools():
    with pytest.raises(MissingEmbeddingError):
        score_candidates({}, {"H1": [1.0, 0.0]})
    with pytest.raises(MissingEmbeddingError):
        score_candidates({"C1": [1.0, 0.0]}, {})


def test_stratify_empirical_tercile_cut_and_ordering():
    # 9 candidates, evenly spread distance scores 0..8 -> exact thirds of 3.
    scores = {f"C{i}": float(i) for i in range(9)}
    result = stratify(scores)
    assert [sc.candidate_id for sc in result] == [f"C{i}" for i in range(9)]  # ascending order
    arms = [sc.arm for sc in result]
    assert arms == ["near"] * 3 + ["moderate"] * 3 + ["far"] * 3


def test_stratify_tie_break_is_deterministic_on_candidate_id():
    scores = {"C3": 1.0, "C1": 1.0, "C2": 1.0}
    result = stratify(scores)
    assert [sc.candidate_id for sc in result] == ["C1", "C2", "C3"]


def test_stratify_carries_cluster_id_through():
    scores = {"C1": 0.1, "C2": 0.9}
    result = stratify(scores, cluster_of={"C1": "clusterA"})
    by_id = {sc.candidate_id: sc for sc in result}
    assert by_id["C1"].cluster_id == "clusterA"
    assert by_id["C2"].cluster_id is None


@pytest.mark.parametrize("n", [0, 1, 2, 4, 10, 37])
def test_stratify_arm_sizes_sum_to_total_for_various_n(n):
    scores = {f"C{i}": float(i) for i in range(n)}
    result = stratify(scores)
    assert len(result) == n
    near = sum(1 for sc in result if sc.arm == "near")
    moderate = sum(1 for sc in result if sc.arm == "moderate")
    far = sum(1 for sc in result if sc.arm == "far")
    assert near + moderate + far == n
    # Empirical-tercile cut points, transcribed from the implementation's
    # own contract (design: "cut at empirical terciles") -- re-derived here
    # independently rather than importing the module's private cut logic.
    assert near == n // 3
    assert moderate == (2 * n) // 3 - n // 3
    assert far == n - (2 * n) // 3
