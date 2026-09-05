"""``trialerror.verify.hypothesis`` — the hypothesis-vs-literature pipeline
(design Section 8.2)."""

from __future__ import annotations

import pytest

from trialerror.ingest.anchors import build_chunk_anchor, sha256_hex
from trialerror.ingest.backends import FakeEmbedBackend
from trialerror.ingest.chunker import build_chunks
from trialerror.ingest.stream import stream_v1
from trialerror.retrieve import engine as retrieve_engine
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.stores.writer import get as store_get
from trialerror.stores.writer import insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from trialerror.verify.errors import VerifyError
from trialerror.verify.hypothesis import (
    aggregate_labels,
    build_hypothesis_judgment_envelope,
    run_hypothesis_verification,
    stratified_retrieve,
)
from trialerror.verify.prereg import commit_prereg

from tests._verify_fixtures import bootstrap_launch, build_small_corpus, seed_hypothesis

_QUERY = "distributed systems retry coordinator lock"


# ---------------------------------------------------------------------------
# NB-1 fixture helpers (fix-tier3, C-0064) -- not tests themselves.
# ---------------------------------------------------------------------------


def _build_three_chunk_fts_corpus(store, *, launch_id: str, query_term: str, dims: int = 16) -> dict:
    """Three single-paragraph documents (one chunk each -- a single short
    paragraph never triggers the chunker's recombine-undersized merge, so
    each document is guaranteed exactly one chunk), each containing
    ``query_term`` a different number of times so ``mode="fts"`` yields a
    clean, distinct 3-way rank order driven by term frequency alone. Used
    by the NB-1 rank-vs-distance divergence test, which then overwrites
    specific chunks' vectors directly rather than trusting
    ``FakeEmbedBackend``'s hash-derived vectors to happen to diverge from
    FTS rank on their own."""
    embed_backend = FakeEmbedBackend(dims=dims)
    model_key = embed_backend.model_key
    backend = ensure_vec_table(store.knowledge, model_key, dims)

    source_id = new_id("SRC")
    insert(
        store, "source",
        {
            "source_id": source_id, "kind": "paper", "title": "NB-1 FTS-vs-distance fixture corpus",
            "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
            "registered_ts": now(), "registered_by_launch": launch_id,
        },
    )

    texts = [
        " ".join([query_term] * 8 + ["umbrella", "filler", "alpha", "words", "padding"]) + ".",
        " ".join([query_term] * 3 + ["umbrella", "filler", "bravo", "words", "more", "padding", "content"]) + ".",
        " ".join([query_term] * 1 + ["umbrella", "filler", "charlie", "words", "padding", "content", "extra", "more"]) + ".",
    ]

    chunk_ids: list[str] = []
    for i, text in enumerate(texts):
        doc_id = new_id("DOC")
        insert(
            store, "document",
            {
                "doc_id": doc_id, "source_id": source_id, "rel_path": f"archive/fts_{i}.md", "media_type": "md",
                "normalizer_id": "fixture", "normalizer_version": "1", "sha256": "0" * 64, "status": "registered",
            },
        )
        element_row = {"element_id": new_id("ELM"), "doc_id": doc_id, "seq": 0, "type": "NarrativeText", "text": text, "page_number": 1}
        insert(store, "element", element_row)
        doc_sha256 = sha256_hex(stream_v1([element_row]))
        update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"sha256": doc_sha256, "status": "chunked"})

        drafts = build_chunks([element_row])
        assert len(drafts) == 1, "fixture expects exactly one chunk per single-paragraph document"
        draft = drafts[0]
        chunk_id = new_id("CHK")
        chunk_sha256 = sha256_hex(draft["text"])
        chunk_row = {
            "chunk_id": chunk_id, "doc_id": doc_id, "seq": draft["seq"], "text": draft["text"],
            "token_count": draft["token_count"], "element_first": draft["element_first"], "element_last": draft["element_last"],
            "page_start": draft["page_start"], "page_end": draft["page_end"], "sha256": chunk_sha256,
            "chunker_id": draft["chunker_id"], "chunker_version": draft["chunker_version"], "created_ts": now(),
        }
        insert(store, "chunk", chunk_row)

        anchor_draft = build_chunk_anchor(
            doc_id=doc_id, doc_sha256=doc_sha256, elements=[element_row], chunk_id=chunk_id,
            element_first=chunk_row["element_first"], element_last=chunk_row["element_last"], page_number=chunk_row["page_start"],
        )
        insert(store, "quote_anchor", {"anchor_id": new_id("ANC"), **anchor_draft, "created_by_launch": launch_id, "created_ts": now()})

        vector = list(embed_backend.embed_batch([draft["text"]], kind="document")[0])
        blob = serialize_vector_fallback(vector)
        table = vec_table_name(model_key)
        with store.knowledge:
            store.knowledge.execute("INSERT INTO chunk_fts(chunk_id, text) VALUES (?, ?)", (chunk_id, draft["text"]))
            if backend == VecBackend.SQLITE_VEC:
                store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, blob))
            else:
                store.knowledge.execute(f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)", (chunk_id, model_key, dims, blob))
        insert(store, "emb", {"chunk_sha256": chunk_sha256, "model_key": model_key, "dims": dims, "vector": blob, "created_ts": now()})
        update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"status": "indexed"})
        chunk_ids.append(chunk_id)

    return {"launch_id": launch_id, "model_key": model_key, "dims": dims, "source_id": source_id, "chunk_ids": chunk_ids}


def _overwrite_chunk_vector(store, *, model_key: str, dims: int, chunk_id: str, vector: list) -> None:
    """Directly replace ``chunk_id``'s stored vector under ``model_key`` --
    a test-only seam letting the NB-1 test place a candidate at an exact,
    known cosine distance from the query embedding, independent of
    whatever ``FakeEmbedBackend``'s hash-of-text would otherwise produce."""
    backend = ensure_vec_table(store.knowledge, model_key, dims)
    table = vec_table_name(model_key)
    blob = serialize_vector_fallback(vector)
    with store.knowledge:
        store.knowledge.execute(f"DELETE FROM {table} WHERE chunk_id = ?", (chunk_id,))
        if backend == VecBackend.SQLITE_VEC:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, blob))
        else:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)", (chunk_id, model_key, dims, blob))


# ---------------------------------------------------------------------------
# stratified_retrieve
# ---------------------------------------------------------------------------


def test_stratified_retrieve_is_deterministic_across_repeated_calls(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    a = stratified_retrieve(store, query=_QUERY, mode="vector")
    b = stratified_retrieve(store, query=_QUERY, mode="vector")
    assert [r["chunk_id"] for r in a["near"]] == [r["chunk_id"] for r in b["near"]]
    assert [r["chunk_id"] for r in a["moderate"]] == [r["chunk_id"] for r in b["moderate"]]
    assert [r["chunk_id"] for r in a["far"]] == [r["chunk_id"] for r in b["far"]]


def test_stratified_retrieve_returns_full_citation_rows(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    arms = stratified_retrieve(store, query=_QUERY, mode="vector")
    assert arms["all"], "expected at least one evidence chunk"
    for row in arms["all"]:
        assert row["citation"]["anchor"]["anchor_id"]
        assert row["chunk_id"]


def test_stratified_retrieve_empty_corpus_returns_empty_arms(store):
    arms = stratified_retrieve(store, query="anything at all", mode="vector")
    assert arms == {
        "near": [], "moderate": [], "far": [], "all": [],
        "query_id": arms["query_id"], "stratify_method": "empty",
    }


def test_stratified_retrieve_uses_distance_not_rank_when_vectors_exist(store):
    """NB-1 (C-0064 fix-tier3): design Section 8.2 stratifies over
    EMBEDDING DISTANCE, not the fused search rank. Fixture: three chunks
    whose FTS rank order is fully controlled by keyword repetition (mode=
    "fts", so retrieval RANK never touches a vector at all), then two of
    those chunks' vectors are overwritten post-index so rank order and
    distance order DIVERGE -- the best-FTS-ranked chunk is made embedding-
    FAR from the query and the worst-FTS-ranked chunk is made embedding-
    NEAR. Only a distance-based stratification (not the old rank-tercile)
    puts the best-ranked hit in "far" and the worst-ranked hit in "near"."""
    launch_id = bootstrap_launch(store)
    query = "dicekeyword"
    build = _build_three_chunk_fts_corpus(store, launch_id=launch_id, query_term=query)

    # Ground-truth FTS rank order (mode="fts" never touches vectors).
    fts_only = retrieve_engine.search(store, query=query, k=10, mode="fts")
    fts_order = [r["chunk_id"] for r in fts_only["results"]]
    assert len(fts_order) == 3, "fixture expects all three chunks to match the shared FTS term"
    best_fts_chunk_id, _mid_chunk_id, worst_fts_chunk_id = fts_order

    query_vector = FakeEmbedBackend(dims=build["dims"]).embed_batch([query], kind="query")[0]
    far_vector = [-x for x in query_vector]  # cosine distance 2.0 (maximal)
    near_vector = list(query_vector)  # cosine distance 0.0 (identical)
    _overwrite_chunk_vector(store, model_key=build["model_key"], dims=build["dims"], chunk_id=best_fts_chunk_id, vector=far_vector)
    _overwrite_chunk_vector(store, model_key=build["model_key"], dims=build["dims"], chunk_id=worst_fts_chunk_id, vector=near_vector)

    arms = stratified_retrieve(store, query=query, mode="fts", k_total=3, weights=(1, 1, 1), far_floor=1)

    assert arms["stratify_method"] == "distance"
    near_ids = [r["chunk_id"] for r in arms["near"]]
    far_ids = [r["chunk_id"] for r in arms["far"]]
    # The best-FTS-ranked chunk was made embedding-FAR -- distance-based
    # stratification must NOT treat it as "near" (what rank-tercile would
    # have done, since it was rank 0).
    assert best_fts_chunk_id not in near_ids
    assert best_fts_chunk_id in far_ids
    # The worst-FTS-ranked chunk was made embedding-NEAR -- distance-based
    # stratification must NOT treat it as "far" (what rank-tercile would
    # have done, since it was rank 2 of 2).
    assert worst_fts_chunk_id not in far_ids
    assert worst_fts_chunk_id in near_ids


def test_stratified_retrieve_falls_back_to_rank_tercile_when_no_vectors_exist(store):
    """NB-1 fallback path: a fresh corpus with FTS indexed but no vectors
    yet embedded (the ``vec_chunks__<model_key>`` table has zero rows for
    the retrieved candidates) must not raise or silently drop candidates --
    it falls back to the original rank-tercile split, and says so."""
    launch_id = bootstrap_launch(store)
    build = build_small_corpus(store, launch_id=launch_id)
    table = vec_table_name(build["model_key"])
    with store.knowledge:
        store.knowledge.execute(f"DELETE FROM {table}")

    # mode="fts" (single term, guaranteed to hit the open-doc chunk's own
    # text) so retrieval itself succeeds via the FTS tier alone -- proving
    # the fallback is specifically about the ABSENT vectors, not an empty
    # candidate pool (which would short-circuit to "empty" before this
    # function ever reaches the vector-lookup step).
    arms = stratified_retrieve(store, query="retry", mode="fts")

    assert arms["stratify_method"] == "rank_fallback"
    assert arms["all"], "fallback must still retrieve candidates via FTS"


def test_stratified_retrieve_rejects_a_weights_tuple_that_is_not_length_three(store):
    with pytest.raises(VerifyError):
        stratified_retrieve(store, query=_QUERY, weights=(1, 2))


# ---------------------------------------------------------------------------
# build_hypothesis_judgment_envelope
# ---------------------------------------------------------------------------


def test_judgment_envelope_carries_the_prompt_hypothesis_and_fixed_labels(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    arms = stratified_retrieve(store, query=_QUERY, mode="vector")
    envelope = build_hypothesis_judgment_envelope("retry budgets produce unpredictable outcomes", arms["all"][0])
    assert envelope["hypothesis"] == "retry budgets produce unpredictable outcomes"
    assert "retry budgets produce unpredictable outcomes" in envelope["prompt"]
    assert envelope["labels"][0] == "explicit contradiction"
    assert envelope["labels"][-1] == "explicit agreement"
    assert envelope["chunk_id"] == arms["all"][0]["chunk_id"]


# ---------------------------------------------------------------------------
# aggregate_labels
# ---------------------------------------------------------------------------


def test_aggregate_labels_all_agreement_side_is_supported():
    out = aggregate_labels([{"label": "agreement"}, {"label": "explicit agreement"}])
    assert out["status_proposal"] == "supported"
    assert out["n_contradicting"] == 0 and out["n_agreeing"] == 2


def test_aggregate_labels_all_contradiction_side_is_contradicted():
    out = aggregate_labels([{"label": "contradiction"}, {"label": "strong contradiction"}])
    assert out["status_proposal"] == "contradicted"


def test_aggregate_labels_mixed_polarities_is_mixed():
    out = aggregate_labels([{"label": "explicit agreement"}, {"label": "explicit contradiction"}])
    assert out["status_proposal"] == "mixed"


def test_aggregate_labels_all_lack_of_evidence_is_open():
    out = aggregate_labels([{"label": "lack of evidence"}, {"label": "lack of evidence"}])
    assert out["status_proposal"] == "open"


def test_aggregate_labels_empty_is_open():
    out = aggregate_labels([])
    assert out["status_proposal"] == "open"
    assert out["distribution"] == {}


# ---------------------------------------------------------------------------
# run_hypothesis_verification: end to end
# ---------------------------------------------------------------------------


def _agree_judge(_envelope):
    return "explicit agreement"


def test_run_hypothesis_verification_inline_text_records_a_supported_verdict(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    result = run_hypothesis_verification(
        store, hypothesis_text="rulebooks describe combat and dice mechanics", query=_QUERY,
        judge=_agree_judge, issued_by_launch=launch_id, mode="vector",
    )
    assert result["status"] == "supported"
    assert result["verdict"]["procedure"] == "contracrow"
    assert result["verdict"]["label"] == "supported"
    assert result["prereg_id"] is None
    assert result["prereg_compliant"] is None
    assert result["evidence"], "expected at least one evidence item recorded"


def test_run_hypothesis_verification_updates_an_existing_hypothesis_status(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    hyp_id = seed_hypothesis(store, launch_id=launch_id, text="rulebooks describe combat and dice mechanics")

    result = run_hypothesis_verification(store, hyp_id=hyp_id, query=_QUERY, judge=_agree_judge, issued_by_launch=launch_id, mode="vector")
    assert result["hyp_id"] == hyp_id
    updated = store_get(store, "hypothesis", pk_column="hyp_id", pk_value=hyp_id)
    assert updated["status"] == result["status"] == "supported"


def test_run_hypothesis_verification_requires_exactly_one_of_hyp_id_or_text(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(VerifyError):
        run_hypothesis_verification(store, judge=_agree_judge, issued_by_launch=launch_id)
    with pytest.raises(VerifyError):
        run_hypothesis_verification(store, hyp_id="HYP-x", hypothesis_text="also given", judge=_agree_judge, issued_by_launch=launch_id)


def test_run_hypothesis_verification_unknown_hyp_id_raises(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(VerifyError):
        run_hypothesis_verification(store, hyp_id="HYP-does-not-exist", judge=_agree_judge, issued_by_launch=launch_id)


def test_run_hypothesis_verification_judge_returns_label_outside_vocabulary_raises(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    with pytest.raises(VerifyError):
        run_hypothesis_verification(
            store, hypothesis_text="anything", query=_QUERY, judge=lambda env: "not a real label",
            issued_by_launch=launch_id, mode="vector",
        )


# ---------------------------------------------------------------------------
# prereg integration
# ---------------------------------------------------------------------------


def test_run_hypothesis_verification_prereg_true_commits_and_is_compliant(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    result = run_hypothesis_verification(
        store, hypothesis_text="rulebooks describe combat and dice mechanics", query=_QUERY,
        judge=_agree_judge, issued_by_launch=launch_id, mode="vector", prereg=True,
    )
    assert result["prereg_id"] is not None
    assert result["prereg_compliant"] is True
    assert result["verdict"]["prereg_id"] == result["prereg_id"]
    assert result["verdict"]["prereg_compliant"] == 1


def test_run_hypothesis_verification_prereg_reuses_existing_prereg_id_and_detects_noncompliance(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    decoy = commit_prereg(store, title="decoy procedure", procedure="a completely different, pre-committed procedure")
    hyp_id = seed_hypothesis(store, launch_id=launch_id, text="rulebooks describe combat and dice mechanics", prereg_id=decoy["prereg_id"])

    result = run_hypothesis_verification(
        store, hyp_id=hyp_id, query=_QUERY, judge=_agree_judge, issued_by_launch=launch_id, mode="vector", prereg=True,
    )
    # the hypothesis already had a prereg_id -- prereg=True must NOT commit a second one
    assert result["prereg_id"] == decoy["prereg_id"]
    # the actually-executed procedure/params don't match what was committed -> noncompliant
    assert result["prereg_compliant"] is False
