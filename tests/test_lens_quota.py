"""Pure-function tests for ``trialerror.lens.quota``: apportionment math,
far-floor borrowing, and seeded-draw reproducibility."""

from __future__ import annotations

import random

import pytest

from trialerror.lens.errors import InsufficientCandidatesError
from trialerror.lens.quota import compute_quota_counts, derive_rng, draw_quota


def test_compute_quota_counts_default_weights_sum_to_total():
    # totals >= the default far_floor (2), where the floor is always
    # satisfiable by borrowing -- see the dedicated
    # test_compute_quota_counts_far_floor_exceeding_total_is_visible_not_papered_over
    # for the total < far_floor edge case this loop deliberately excludes.
    for total in (2, 3, 10, 17, 100, 101):
        counts = compute_quota_counts(total)
        assert sum(counts.values()) == total
        assert set(counts) == {"near", "moderate", "far"}


def test_compute_quota_counts_far_floor_exceeding_total_is_visible_not_papered_over():
    # far_floor=2 > total=0: unsatisfiable by construction (nothing to
    # borrow from). Module contract (quota.py docstring): this is left
    # VISIBLE as a counts dict that does not sum to `total`, rather than
    # silently capped -- draw_quota's own availability check is what turns
    # this into a loud InsufficientCandidatesError against a real pool.
    counts = compute_quota_counts(0, far_floor=2)
    assert counts["far"] == 2
    assert counts["near"] == 0
    assert counts["moderate"] == 0
    assert sum(counts.values()) != 0


def test_compute_quota_counts_exact_split():
    counts = compute_quota_counts(10, weights=(40, 40, 20), far_floor=0)
    assert counts == {"near": 4, "moderate": 4, "far": 2}


def test_compute_quota_counts_largest_remainder_rounding_is_deterministic():
    # 10 * 40/100 = 4.0 exactly, 10 * 20/100 = 2.0 exactly for weights
    # summing to 100 -- pick a case with real remainders to exercise the
    # largest-remainder tie-break: total=7, weights 40/40/20 ->
    # exact = [2.8, 2.8, 1.4], floors = [2,2,1] (sum 5), leftover=2 goes to
    # the two largest remainders (both 0.8, near then moderate by index tie-break).
    counts = compute_quota_counts(7, weights=(40, 40, 20), far_floor=0)
    assert counts == {"near": 3, "moderate": 3, "far": 1}
    assert sum(counts.values()) == 7


def test_compute_quota_counts_far_floor_borrows_from_moderate_then_near():
    # total=3, weights 40/40/20 -> exact [1.2,1.2,0.6] -> floors [1,1,0],
    # leftover=1 -> largest remainder is far (0.6) -> far becomes 1.
    # far_floor=2 forces a bump: far 1->2, borrow 1 from moderate.
    counts = compute_quota_counts(3, weights=(40, 40, 20), far_floor=2)
    assert counts["far"] == 2
    assert counts["near"] == 1
    assert counts["moderate"] == 0
    assert sum(counts.values()) == 3


def test_compute_quota_counts_far_floor_borrows_from_near_when_moderate_exhausted():
    counts = compute_quota_counts(2, weights=(100, 0, 0), far_floor=2)
    # exact = [2, 0, 0]; far bumped to floor 2, deficit 2: moderate has 0 to
    # give, near gives 2 (all of it).
    assert counts == {"near": 0, "moderate": 0, "far": 2}


def test_compute_quota_counts_rejects_bad_input():
    with pytest.raises(ValueError):
        compute_quota_counts(-1)
    with pytest.raises(ValueError):
        compute_quota_counts(5, weights=(1, 2))  # wrong arity
    with pytest.raises(ValueError):
        compute_quota_counts(5, weights=(0, 0, 0))


def test_derive_rng_is_a_plain_random_instance_never_touches_global_state():
    before = random.getstate()
    rng1 = derive_rng("seed-A")
    rng2 = derive_rng("seed-A")
    after = random.getstate()
    assert after == before  # global random module state untouched
    assert isinstance(rng1, random.Random)
    # Same seed -> same subsequent draws.
    assert rng1.sample(range(100), 5) == rng2.sample(range(100), 5)


def test_derive_rng_salt_changes_the_stream():
    base = derive_rng("seed-A")
    salted = derive_rng("seed-A", salt="lens-1")
    assert base.sample(range(1000), 10) != salted.sample(range(1000), 10)


def test_draw_quota_deterministic_for_same_seed():
    pools = {"near": [f"N{i}" for i in range(10)], "moderate": [f"M{i}" for i in range(10)], "far": [f"F{i}" for i in range(10)]}
    quota = {"near": 3, "moderate": 3, "far": 2}
    drawn1 = draw_quota(pools, quota, derive_rng("seed-X"))
    drawn2 = draw_quota(pools, quota, derive_rng("seed-X"))
    assert drawn1 == drawn2
    for arm, k in quota.items():
        assert len(drawn1[arm]) == k
        assert len(set(drawn1[arm])) == k  # no repeats within an arm
        assert set(drawn1[arm]) <= set(pools[arm])


def test_draw_quota_different_seed_usually_differs():
    pools = {"near": [f"N{i}" for i in range(50)], "moderate": [], "far": []}
    quota = {"near": 10, "moderate": 0, "far": 0}
    drawn1 = draw_quota(pools, quota, derive_rng("seed-A"))
    drawn2 = draw_quota(pools, quota, derive_rng("seed-B"))
    assert drawn1 != drawn2


def test_draw_quota_raises_on_insufficient_pool():
    pools = {"near": ["N0"], "moderate": [], "far": []}
    quota = {"near": 5, "moderate": 0, "far": 0}
    with pytest.raises(InsufficientCandidatesError, match="near"):
        draw_quota(pools, quota, derive_rng("seed-A"))
