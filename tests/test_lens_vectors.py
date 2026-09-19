"""``trialerror.lens.vectors``: mean+L2-renorm doc pooling (pure), and the
Store-backed doc-pooled fetch over a real chunk/emb/vec_chunks corpus."""

from __future__ import annotations

import math

import pytest

from trialerror.lens.vectors import fetch_doc_vector, fetch_doc_vectors, mean_pool_l2
from tests._lens_fixtures import build_doc_pool


def test_mean_pool_l2_empty_is_none():
    assert mean_pool_l2([]) is None


def test_mean_pool_l2_single_vector_is_l2_normalized():
    result = mean_pool_l2([[3.0, 4.0]])
    assert result == pytest.approx([0.6, 0.8])
    norm = math.sqrt(sum(x * x for x in result))
    assert norm == pytest.approx(1.0)


def test_mean_pool_l2_mean_then_renormalizes():
    result = mean_pool_l2([[1.0, 0.0], [0.0, 1.0]])
    # element-wise mean = [0.5, 0.5], L2 norm sqrt(0.5) -> renormalized to unit length
    assert result == pytest.approx([1 / math.sqrt(2), 1 / math.sqrt(2)])


def test_mean_pool_l2_zero_vector_result_is_none():
    assert mean_pool_l2([[1.0, 0.0], [-1.0, 0.0]]) is None


def test_mean_pool_l2_rejects_inconsistent_dims():
    with pytest.raises(ValueError):
        mean_pool_l2([[1.0, 0.0], [1.0, 0.0, 0.0]])


def test_fetch_doc_vector_roundtrips_through_real_chunk_emb_vecindex(store):
    pool = build_doc_pool(store, n_docs=3)
    vec = fetch_doc_vector(store, model_key=pool["model_key"], doc_id=pool["doc_ids"][0])
    assert vec is not None
    assert len(vec) == pool["dims"]
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_fetch_doc_vector_none_for_unknown_doc_or_model(store):
    pool = build_doc_pool(store, n_docs=1)
    assert fetch_doc_vector(store, model_key="nonexistent-model", doc_id=pool["doc_ids"][0]) is None
    assert fetch_doc_vector(store, model_key=pool["model_key"], doc_id="DOC-nope") is None


def test_fetch_doc_vectors_missing_ids_are_simply_absent(store):
    pool = build_doc_pool(store, n_docs=2)
    result = fetch_doc_vectors(store, model_key=pool["model_key"], doc_ids=[*pool["doc_ids"], "DOC-nope"])
    assert set(result) == set(pool["doc_ids"])
