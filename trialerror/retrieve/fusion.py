"""Reciprocal-rank fusion. Design Section 7 pipeline step 3, and the
``SearchResponse`` example's own ``"fusion": {"fts": 3, "vector": 1}``
comment: "per-tier ranks (RRF inputs)".

Standard RRF: a chunk's fused score is the sum, over every tier it appears
in, of ``1 / (K + rank)`` where ``rank`` is that chunk's 1-based position
within that tier's own ranking (``K=60`` is the constant from the original
Cormack/Clarke/Buettcher RRF paper and the conventional default almost
every hybrid-search implementation ports verbatim). A chunk absent from a
tier simply contributes nothing from that tier -- it is not penalized
beyond not receiving that tier's term.
"""

from __future__ import annotations

from typing import Sequence

__all__ = ["DEFAULT_RRF_K", "reciprocal_rank_fusion"]

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    tier_rankings: dict[str, Sequence[str]], *, k: int = DEFAULT_RRF_K
) -> list[tuple[str, float, dict[str, int]]]:
    """``tier_rankings`` = ``{tier_name: [chunk_id, ...]}``, each already
    ordered best-first. Returns ``[(chunk_id, fused_score, {tier: rank}), ...]``
    sorted best-first (highest fused score first; ties break on ``chunk_id``
    for deterministic ordering). ``rank`` in the per-chunk dict is 1-based,
    exactly the ``SearchResponse.results[].fusion`` shape design Section 7
    documents."""
    scores: dict[str, float] = {}
    per_tier_rank: dict[str, dict[str, int]] = {}
    for tier, ranking in tier_rankings.items():
        for idx, chunk_id in enumerate(ranking):
            rank = idx + 1
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            per_tier_rank.setdefault(chunk_id, {})[tier] = rank

    fused = [(cid, score, per_tier_rank[cid]) for cid, score in scores.items()]
    fused.sort(key=lambda row: (-row[1], row[0]))
    return fused
