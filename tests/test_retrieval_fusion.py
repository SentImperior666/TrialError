"""Unit tests for :mod:`trialerror.retrieve.fusion` (reciprocal-rank fusion)."""

from __future__ import annotations

from trialerror.retrieve.fusion import DEFAULT_RRF_K, reciprocal_rank_fusion


def test_single_tier_ranking_is_preserved():
    fused = reciprocal_rank_fusion({"fts": ["A", "B", "C"]})
    assert [cid for cid, _, _ in fused] == ["A", "B", "C"]
    assert fused[0][2] == {"fts": 1}
    assert fused[1][2] == {"fts": 2}


def test_a_chunk_ranked_first_in_both_tiers_wins():
    fused = reciprocal_rank_fusion({"fts": ["A", "B", "C"], "vector": ["A", "C", "B"]})
    assert fused[0][0] == "A"
    # A's fusion dict carries both tiers' 1-based ranks
    a_rank = next(row[2] for row in fused if row[0] == "A")
    assert a_rank == {"fts": 1, "vector": 1}


def test_score_formula_matches_1_over_k_plus_rank():
    fused = reciprocal_rank_fusion({"fts": ["A"]}, k=60)
    [(cid, score, ranks)] = fused
    assert cid == "A"
    assert score == 1.0 / (60 + 1)
    assert ranks == {"fts": 1}


def test_a_chunk_absent_from_a_tier_only_contributes_from_tiers_it_is_in():
    fused = reciprocal_rank_fusion({"fts": ["A", "B"], "vector": ["B"]})
    by_id = {cid: (score, ranks) for cid, score, ranks in fused}
    expected_b = 1.0 / (DEFAULT_RRF_K + 2) + 1.0 / (DEFAULT_RRF_K + 1)  # fts rank 2, vector rank 1
    expected_a = 1.0 / (DEFAULT_RRF_K + 1)  # fts rank 1 only
    assert by_id["B"][0] == expected_b
    assert by_id["A"][0] == expected_a
    assert by_id["B"][1] == {"fts": 2, "vector": 1}
    assert by_id["A"][1] == {"fts": 1}
    # B outranks A because it appears in both tiers
    assert [cid for cid, _, _ in fused][0] == "B"


def test_empty_tier_rankings_produce_no_results():
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"fts": []}) == []


def test_tie_breaks_deterministically_on_chunk_id():
    fused = reciprocal_rank_fusion({"fts": ["Z"], "vector": ["A"]})  # both rank 1 -> tied score
    ids = [cid for cid, _, _ in fused]
    assert ids == ["A", "Z"]  # lexicographic tie-break, not insertion order
