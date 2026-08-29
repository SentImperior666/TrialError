"""The core stratification algorithm. Design Section 9.6 / 12 (M13 row):
"tercile stratify over embeddings" — AMENDMENT-3 generalized: score a
candidate pool of documents by their distance from a "home" reference set,
cut the sorted scores at the empirical terciles into near/moderate/far arms.

TRIALERROR-DEV-NOTE (distance metric — a judgment call the design names but does
not spell out arithmetically, same posture as ``trialerror.budget.pools``'s own
"over-cap math" note): each candidate's distance score is the MEAN cosine
distance (``1 - cosine_similarity``) from its own doc-pooled vector
(:mod:`trialerror.lens.vectors`) to every vector in the ``home`` reference set —
"candidate literature sets scored by mean pairwise cosine distance over
document embeddings" read literally: for candidate *c* and home set *H*,
``score(c) = mean_{h in H}(1 - cos_sim(c, h))``. A single-document home set
degenerates to plain distance-to-that-document, which is the common case
this module is exercised with; the mean form is what generalizes to a
multi-document "home cluster" home set without a second code path.

Terciles are EMPIRICAL (rank-based over the actual candidate sample, not a
value cut against a fixed distance threshold): sort candidates ascending by
score, then split at ranks ``n // 3`` and ``(2 * n) // 3`` — near = lowest
third, far = highest third. Ties on score break on candidate_id (stable,
deterministic — same convention as
``trialerror.retrieve.vecsearch.rank_by_query_vector``'s own tie-break, applied
here so "stratify on fixture corpus reproduces byte-identical arms from
same seed" holds even with duplicate/degenerate scores).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from trialerror.lens.errors import MissingEmbeddingError

__all__ = ["Arm", "ARMS", "StratifiedCandidate", "cosine_distance", "score_candidates", "stratify"]

#: Arm names in near->far order — also the canonical processing/quota order
#: every downstream function (quota math, seeded draw) iterates in.
Arm = str  # "near" | "moderate" | "far"
ARMS: tuple[Arm, ...] = ("near", "moderate", "far")


@dataclass(frozen=True)
class StratifiedCandidate:
    candidate_id: str
    distance_score: float
    arm: Arm
    cluster_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "distance_score": self.distance_score,
            "arm": self.arm,
            "cluster_id": self.cluster_id,
        }


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """``1 - cosine_similarity(a, b)``, plain Python (no numpy — matches
    ``trialerror.retrieve.vecsearch.cosine_similarity``'s own convention this
    reimplements rather than imports, to keep this module's distance metric
    self-contained and independently testable: M8's function returns
    similarity for RANKING, this one returns distance for STRATIFICATION,
    and the two must never silently drift against each other by one
    sharing an edge case fix the other doesn't get). Degenerate (zero-norm
    or mismatched-length) inputs score a distance of ``1.0`` (maximally far
    — the same "never crash a ranking pass" posture as M8's
    ``cosine_similarity``, applied as "never crash a stratify pass" here)."""
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - (dot / (norm_a * norm_b))


def score_candidates(
    candidates: Mapping[str, Sequence[float]], home: Mapping[str, Sequence[float]]
) -> dict[str, float]:
    """``candidate_id -> mean cosine distance to every vector in `home`.``
    Raises :class:`~trialerror.lens.errors.MissingEmbeddingError` if either
    mapping is empty — an unscoreable pool is a caller data problem (see
    that error's docstring), not a result this function papers over."""
    if not candidates:
        raise MissingEmbeddingError("score_candidates: empty candidate vector set")
    if not home:
        raise MissingEmbeddingError("score_candidates: empty home/reference vector set")
    home_vectors = list(home.values())
    return {
        cid: sum(cosine_distance(vec, h) for h in home_vectors) / len(home_vectors)
        for cid, vec in candidates.items()
    }


def stratify(
    scores: Mapping[str, float], *, cluster_of: Mapping[str, str] | None = None
) -> list[StratifiedCandidate]:
    """Cut ``scores`` (``candidate_id -> distance_score``) at the empirical
    terciles into near/moderate/far arms. Returns candidates SORTED
    ascending by ``(distance_score, candidate_id)`` — the same order used
    to derive the cut points, so a caller can see exactly where each
    boundary fell; downstream quota/draw functions consume this order
    as-is rather than re-sorting (one sort, one place it can disagree with
    itself)."""
    ordered = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(ordered)
    cut1 = n // 3
    cut2 = (2 * n) // 3
    out: list[StratifiedCandidate] = []
    for i, (cid, score) in enumerate(ordered):
        arm: Arm = "near" if i < cut1 else ("moderate" if i < cut2 else "far")
        cluster_id = cluster_of.get(cid) if cluster_of else None
        out.append(StratifiedCandidate(candidate_id=cid, distance_score=score, arm=arm, cluster_id=cluster_id))
    return out
