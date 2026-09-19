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

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from trialerror.lens.errors import MissingEmbeddingError
from trialerror.util import vecmath

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
    """``1 - cosine_similarity(a, b)``, plain Python.

    The arithmetic now comes from :func:`trialerror.util.vecmath.cosine_one`
    rather than being written out a second time here. The note this
    docstring used to carry — that M8's function returns similarity for
    RANKING and this one distance for STRATIFICATION, and that the two must
    never drift by one getting an edge-case fix the other doesn't — is
    exactly the argument for sharing the body: there is one cosine in this
    tree, and one place an edge case is decided. The DISTANCE convention
    stays this module's own.

    Degenerate (zero-norm or mismatched-length) inputs score a distance of
    ``1.0`` (maximally far — the same "never crash a ranking pass" posture
    as M8's ``cosine_similarity``, applied as "never crash a stratify pass"
    here). That is ``1 - 0.0`` and so needs no branch: the shared cosine
    scores exactly those cases ``0.0``."""
    return 1.0 - vecmath.cosine_one(a, b)


def score_candidates(
    candidates: Mapping[str, Sequence[float]],
    home: Mapping[str, Sequence[float]],
    *,
    config: Any = None,
) -> dict[str, float]:
    """``candidate_id -> mean cosine distance to every vector in `home`.``
    Raises :class:`~trialerror.lens.errors.MissingEmbeddingError` if either
    mapping is empty — an unscoreable pool is a caller data problem (see
    that error's docstring), not a result this function papers over.

    The scan is :func:`trialerror.util.vecmath.score_candidates`: one Python
    loop for a pool small enough that numpy would cost more than it saves,
    a blocked float64 matmul for one that isn't. It computes ``1 - cos`` per
    home vector and then the mean — the same algebraic form this function
    always used, not the cheaper ``1 - mean(cos)`` — so two candidates that
    genuinely tie still tie, and :func:`stratify`'s ``(score, id)`` cut
    lands in the same place either way."""
    if not candidates:
        raise MissingEmbeddingError("score_candidates: empty candidate vector set")
    if not home:
        raise MissingEmbeddingError("score_candidates: empty home/reference vector set")
    return vecmath.score_candidates(candidates, home, config=config)


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
