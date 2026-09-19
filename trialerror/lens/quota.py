"""Quota math + seeded draw. Design Section 9.6/12: "configurable weights
(40/40/20 default) + far-arm floor" and "seeded + logged in
``lens_assignment`` rows" — AMENDMENT-3's mix-quota-with-a-floor mechanic,
generalized.

Per this build's brief: seeded randomness is stdlib ``random.Random(seed)``
ONLY — never the module-level ``random`` functions (which mutate shared
global state a concurrent caller could observe/perturb). Every function
here that draws takes its own :class:`random.Random` instance explicitly.
"""

from __future__ import annotations

import random
from typing import Mapping, Sequence

from trialerror.lens.errors import InsufficientCandidatesError
from trialerror.lens.stratify import ARMS, Arm

__all__ = ["compute_quota_counts", "derive_rng", "draw_quota"]


def compute_quota_counts(
    total: int, *, weights: Sequence[int] = (40, 40, 20), far_floor: int = 2
) -> dict[Arm, int]:
    """``total`` slices split across (near, moderate, far) by ``weights``
    (percentages, need not literally sum to 100 — normalized here), then the
    far arm is bumped up to ``far_floor`` if short, borrowing the deficit
    from moderate first, then near (in that fixed order, each floored at 0
    so a borrow never goes negative — deterministic, no ambiguity about
    which arm "pays" for the floor).

    Rounding: largest-remainder (Hamilton) apportionment — each arm's exact
    share ``weights[i]/sum(weights) * total`` is floored, then the
    ``total - sum(floors)`` leftover seats go one at a time to the arms with
    the largest fractional remainder, ties broken by arm order
    (near, moderate, far) — this is what makes the three counts always sum
    to exactly ``total`` regardless of rounding, deterministically, with no
    RNG involved (the seeded RNG is reserved for WHICH candidates fill a
    quota, never how large the quota is)."""
    if total < 0:
        raise ValueError(f"compute_quota_counts: total must be >= 0, got {total}")
    if len(weights) != 3 or any(w < 0 for w in weights):
        raise ValueError(f"compute_quota_counts: weights must be 3 non-negative numbers, got {weights!r}")
    weight_sum = sum(weights)
    if weight_sum == 0:
        raise ValueError("compute_quota_counts: weights must not sum to zero")

    exact = [w / weight_sum * total for w in weights]
    floors = [int(x) for x in exact]
    remainders = [x - f for x, f in zip(exact, floors)]
    leftover = total - sum(floors)
    # Largest-remainder first; ties broken by arm index (near, moderate, far).
    order = sorted(range(3), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        floors[i] += 1
    counts: dict[Arm, int] = dict(zip(ARMS, floors))

    if counts["far"] < far_floor:
        deficit = far_floor - counts["far"]
        counts["far"] = far_floor
        for donor in ("moderate", "near"):
            take = min(deficit, counts[donor])
            counts[donor] -= take
            deficit -= take
            if deficit == 0:
                break
        # A deficit that survives both donors means far_floor > total —
        # left visible (counts won't sum to `total`) rather than papering
        # over it; draw_quota's own availability check catches the
        # resulting shortfall against the candidate pool with a named error.

    return counts


def derive_rng(seed: str, *, salt: str | None = None) -> random.Random:
    """One ``random.Random`` instance, deterministically derived from
    ``seed`` (and an optional ``salt`` — e.g. a lens's own ``roster_id``, so
    each lens in a roster gets an independent-but-reproducible draw stream
    from one round-level seed without any lens's draw order affecting
    another's content). Never touches the global ``random`` module state."""
    return random.Random(f"{seed}::{salt}" if salt is not None else seed)


def draw_quota(
    pools: Mapping[Arm, Sequence[str]],
    quota: Mapping[Arm, int],
    rng: random.Random,
) -> dict[Arm, list[str]]:
    """Draw ``quota[arm]`` candidate ids from ``pools[arm]`` for each arm,
    via ``rng.sample`` (uniform without replacement) over a pool the CALLER
    has already sorted deterministically (this function never re-sorts —
    ``random.Random.sample``'s result depends on the input sequence's
    order, so a stable input order is exactly what makes the draw
    reproducible for a given seed). Raises
    :class:`~trialerror.lens.errors.InsufficientCandidatesError` if any arm's
    pool is smaller than its quota (see :func:`compute_quota_counts` for
    the one case — ``far_floor > total`` — where the quota itself can
    already exceed any achievable pool)."""
    out: dict[Arm, list[str]] = {}
    shortfalls: dict[str, dict[str, int]] = {}
    for arm in ARMS:
        pool = list(pools.get(arm, ()))
        k = quota.get(arm, 0)
        if k > len(pool):
            shortfalls[arm] = {"quota": k, "available": len(pool)}
            continue
        out[arm] = rng.sample(pool, k) if k else []
    if shortfalls:
        raise InsufficientCandidatesError(
            f"draw_quota: quota exceeds available candidates in arm(s) {sorted(shortfalls)!r}: {shortfalls!r}"
        )
    return out
