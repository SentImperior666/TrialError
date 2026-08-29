"""``trialerror.verify.independence`` — the hyperresearch union-find independence
clustering (design Section 8.2 step 4 / Section 11: "hypothesis pipeline
hardening ... independence clustering"), and its wiring into
``trialerror.verify.hypothesis.run_hypothesis_verification``.
"""

from __future__ import annotations

from trialerror.ingest.anchors import build_chunk_anchor, sha256_hex
from trialerror.ingest.backends import FakeEmbedBackend
from trialerror.ingest.chunker import build_chunks
from trialerror.ingest.stream import stream_v1
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.stores.writer import insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from trialerror.verify.hypothesis import run_hypothesis_verification
from trialerror.verify.independence import UnionFind, cluster_evidence, independence_stats, source_lineage_key

from tests._verify_fixtures import bootstrap_launch, build_small_corpus

_DIMS = 16


# ---------------------------------------------------------------------------
# UnionFind + source_lineage_key: pure-function unit tests
# ---------------------------------------------------------------------------


def test_union_find_groups_transitively_unioned_items():
    uf = UnionFind(["a", "b", "c", "d"])
    uf.union("a", "b")
    uf.union("b", "c")
    groups = uf.groups()
    assert sorted(groups) == ["a", "d"]
    assert sorted(groups["a"]) == ["a", "b", "c"]
    assert groups["d"] == ["d"]


def test_union_find_root_is_deterministic_regardless_of_union_order():
    uf1 = UnionFind(["z", "a", "m"])
    uf1.union("z", "a")
    uf1.union("a", "m")
    uf2 = UnionFind(["z", "a", "m"])
    uf2.union("m", "a")
    uf2.union("a", "z")
    assert uf1.find("z") == uf1.find("a") == uf1.find("m")
    assert uf1.find("z") == uf2.find("z")  # same root regardless of call order


def test_source_lineage_key_prefers_authors_and_venue():
    key = source_lineage_key({"authors": "Jane Doe", "venue": "Journal Of Games", "title": "A Paper"})
    assert key == "jane doe::journal of games"


def test_source_lineage_key_falls_back_to_authors_and_title_when_no_venue():
    key = source_lineage_key({"authors": "Jane Doe", "venue": None, "title": "A Self-Published Report"})
    assert key == "jane doe::a self-published report"


def test_source_lineage_key_none_when_no_authors_at_all():
    assert source_lineage_key({"authors": None, "venue": "Some Venue", "title": "Untitled"}) is None


# ---------------------------------------------------------------------------
# cluster_evidence / independence_stats: the planted same-source +
# same-lineage + embedding-proximity fixture (deliverable #4's own
# requirement: "tests with a planted same-source evidence fixture").
# ---------------------------------------------------------------------------


def _plant_source(store, *, launch_id, title, authors, venue):
    source_id = new_id("SRC")
    insert(
        store, "source",
        {
            "source_id": source_id, "kind": "paper", "title": title, "authors": authors, "venue": venue,
            "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
            "registered_ts": now(), "registered_by_launch": launch_id,
        },
    )
    return source_id


def _plant_chunk(store, *, launch_id, source_id, text, model_key, dims, vector, backend):
    """One document, one paragraph, one chunk -- with an explicit, caller-
    given vector (so embedding-proximity clustering is fully controlled,
    the same seam ``tests/test_verify_hypothesis.py``'s own NB-1 fixture
    uses for the identical reason: a real embed backend's hash-of-text
    vectors can't be trusted to land at a chosen cosine distance)."""
    doc_id = new_id("DOC")
    insert(
        store, "document",
        {
            "doc_id": doc_id, "source_id": source_id, "rel_path": f"archive/{doc_id}.md", "media_type": "md",
            "normalizer_id": "fixture", "normalizer_version": "1", "sha256": "0" * 64, "status": "registered",
        },
    )
    element_row = {"element_id": new_id("ELM"), "doc_id": doc_id, "seq": 0, "type": "NarrativeText", "text": text, "page_number": 1}
    insert(store, "element", element_row)
    doc_sha256 = sha256_hex(stream_v1([element_row]))
    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"sha256": doc_sha256, "status": "chunked"})

    drafts = build_chunks([element_row])
    assert len(drafts) == 1
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

    blob = serialize_vector_fallback(vector)
    table = vec_table_name(model_key)
    with store.knowledge:
        if backend == VecBackend.SQLITE_VEC:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, blob))
        else:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)", (chunk_id, model_key, dims, blob))
    insert(store, "emb", {"chunk_sha256": chunk_sha256, "model_key": model_key, "dims": dims, "vector": blob, "created_ts": now()})
    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"status": "indexed"})
    return chunk_id


def _independence_fixture(store, launch_id):
    embed_backend = FakeEmbedBackend(dims=_DIMS)
    model_key = embed_backend.model_key
    backend = ensure_vec_table(store.knowledge, model_key, _DIMS)

    # Source A and B share an author+venue lineage (a syndicated/duplicate
    # registration) but are DIFFERENT source_id rows.
    source_a = _plant_source(store, launch_id=launch_id, title="Combat Rules Explained", authors="A. Author", venue="Games Journal")
    source_b = _plant_source(store, launch_id=launch_id, title="Combat Rules Explained (reprint)", authors="A. Author", venue="Games Journal")
    # Source C has no author/venue overlap with A/B at all.
    source_c = _plant_source(store, launch_id=launch_id, title="An Unrelated Report", authors="Someone Else", venue="Other Venue")
    # Source D is fully unrelated too, and stays its own singleton cluster.
    source_d = _plant_source(store, launch_id=launch_id, title="Totally Independent Text", authors="Nobody Related", venue="Different Venue")

    base_vector = list(embed_backend.embed_batch(["seed text for the shared cluster"], kind="document")[0])
    # C3's and C5's own vectors are each derived from THEIR OWN distinct
    # text (FakeEmbedBackend: deterministic sha256-of-text -> effectively
    # uncorrelated unit vectors for different inputs, cosine similarity
    # near 0 with overwhelming probability at dims=16) -- distinct from
    # ``base_vector`` AND from each other, so proximity never accidentally
    # links C3<->C5 the way a shared literal negation would.
    c3_text = "The reprinted version of the same combat rules paragraph."
    c5_text = "A genuinely independent, unrelated fifth chunk of text."
    c3_vector = list(embed_backend.embed_batch([c3_text], kind="document")[0])
    c5_vector = list(embed_backend.embed_batch([c5_text], kind="document")[0])

    # C1, C2: same source_id (source A) -- trivially same lineage.
    c1 = _plant_chunk(store, launch_id=launch_id, source_id=source_a, text="First paragraph of the combat rules text.", model_key=model_key, dims=_DIMS, vector=base_vector, backend=backend)
    c2 = _plant_chunk(store, launch_id=launch_id, source_id=source_a, text="Second paragraph, still combat rules, same source.", model_key=model_key, dims=_DIMS, vector=base_vector, backend=backend)
    # C3: different source_id (source B) but same author+venue lineage as A.
    c3 = _plant_chunk(store, launch_id=launch_id, source_id=source_b, text=c3_text, model_key=model_key, dims=_DIMS, vector=c3_vector, backend=backend)
    # C4: source C -- no lineage overlap with A/B, but its VECTOR is planted
    # identical to the shared cluster's vector (embedding-proximity signal).
    c4 = _plant_chunk(store, launch_id=launch_id, source_id=source_c, text="An unrelated report that happens to restate the same content.", model_key=model_key, dims=_DIMS, vector=base_vector, backend=backend)
    # C5: source D -- no lineage overlap, no vector overlap -- stays a singleton.
    c5 = _plant_chunk(store, launch_id=launch_id, source_id=source_d, text=c5_text, model_key=model_key, dims=_DIMS, vector=c5_vector, backend=backend)

    return {"model_key": model_key, "c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5}


def test_cluster_evidence_unions_same_source_lineage_and_proximity_into_one_cluster(store):
    launch_id = bootstrap_launch(store)
    fx = _independence_fixture(store, launch_id)
    evidence = [{"chunk_id": fx[k]} for k in ("c1", "c2", "c3", "c4", "c5")]

    clustering = cluster_evidence(store, evidence=evidence, model_key=fx["model_key"])

    assert clustering["n_chunks"] == 5
    assert clustering["n_clusters"] == 2
    assert clustering["proximity_method"] == "embedding"

    root_of_c1 = clustering["cluster_of"][fx["c1"]]
    big_cluster = set(clustering["clusters"][root_of_c1])
    assert big_cluster == {fx["c1"], fx["c2"], fx["c3"], fx["c4"]}

    root_of_c5 = clustering["cluster_of"][fx["c5"]]
    assert root_of_c5 != root_of_c1
    assert clustering["clusters"][root_of_c5] == [fx["c5"]]


def test_independence_stats_reports_effective_count_below_raw_chunk_count(store):
    launch_id = bootstrap_launch(store)
    fx = _independence_fixture(store, launch_id)
    evidence = [{"chunk_id": fx[k]} for k in ("c1", "c2", "c3", "c4", "c5")]

    stats = independence_stats(store, evidence=evidence, model_key=fx["model_key"])

    assert stats["raw_chunk_count"] == 5
    assert stats["effective_independent_count"] == 2
    assert stats["largest_cluster_size"] == 4
    # "a claim 'supported by 12 chunks' from one book is 1 source" -- here,
    # 4 of the 5 chunks collapse into one source: a real, nonzero discount.
    assert stats["syndication_discount"] > 0.0


def test_cluster_evidence_empty_evidence_yields_zero_clusters(store):
    clustering = cluster_evidence(store, evidence=[])
    assert clustering == {"clusters": {}, "cluster_of": {}, "n_chunks": 0, "n_clusters": 0, "proximity_method": "lineage_only"}


def test_independence_stats_all_independent_chunks_yields_zero_discount(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    # open + restricted sources share no author/venue lineage and
    # build_small_corpus never plants overlapping vectors -- every chunk
    # should remain its own singleton cluster.
    evidence = [{"chunk_id": cid} for cid in corpus["open_chunk_ids"] + corpus["restricted_chunk_ids"]]
    stats = independence_stats(store, evidence=evidence, model_key=corpus["model_key"])
    assert stats["effective_independent_count"] == stats["raw_chunk_count"]
    assert stats["syndication_discount"] == 0.0


def test_cluster_evidence_falls_back_to_lineage_only_when_no_vectors_indexed_under_model_key(store):
    launch_id = bootstrap_launch(store)
    fx = _independence_fixture(store, launch_id)
    evidence = [{"chunk_id": fx[k]} for k in ("c1", "c2", "c3", "c4", "c5")]

    clustering = cluster_evidence(store, evidence=evidence, model_key="a-model-key-nothing-was-ever-indexed-under")

    assert clustering["proximity_method"] == "lineage_only"
    # lineage signal alone still unions c1/c2/c3 (same-source + shared
    # author+venue), but c4 -- reachable only via the (absent) proximity
    # signal -- stays separate.
    root_of_c1 = clustering["cluster_of"][fx["c1"]]
    assert set(clustering["clusters"][root_of_c1]) == {fx["c1"], fx["c2"], fx["c3"]}
    assert clustering["cluster_of"][fx["c4"]] != root_of_c1


# ---------------------------------------------------------------------------
# Wiring into run_hypothesis_verification's aggregation.
# ---------------------------------------------------------------------------


def test_run_hypothesis_verification_result_carries_independence_stats(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)

    result = run_hypothesis_verification(
        store, hypothesis_text="rulebooks describe combat and dice mechanics",
        query="game rules dice combat spell", judge=lambda env: "explicit agreement",
        issued_by_launch=launch_id, mode="vector",
    )

    assert result["independence"] is not None
    assert result["independence"]["raw_chunk_count"] == len(result["evidence"])
    assert result["independence"]["effective_independent_count"] <= result["independence"]["raw_chunk_count"]
    assert result["verdict"]["subject_kind"] == "hypothesis"  # unchanged: independence never touches the verdict row's own shape
