"""Unit tests for :mod:`trialerror.retrieve.tantivysearch` -- the tantivy
lexical backend (C-0080). Covers the query contract it must share with
``fts_search``, the derived-state discipline (readiness sidecar, XOR
fingerprint, idempotent adds), and the crash-safety story: a directory
without a valid sidecar is never served.

Skipped wholesale when ``tantivy`` isn't importable -- the FTS5 fallback is
what that environment exercises instead (``test_retrieval_lexical.py``)."""

from __future__ import annotations

import json

import pytest

from tests import _retrieve_fixtures as fx
from trialerror.ingest.anchors import sha256_hex
from trialerror.retrieve import tantivysearch as tv
from trialerror.stores.writer import insert
from trialerror.util.timeutil import now

pytestmark = pytest.mark.skipif(not tv.tantivy_available(), reason="tantivy-py not installed")


def _index_dir(tmp_path):
    return tmp_path / "index" / "tantivy" / "chunks"


def _build(tmp_path, rows):
    index = tv.create_fulltext_index(_index_dir(tmp_path))
    index.add_chunks(rows)
    return index


ROWS = [
    ("CHK-1", "retry budgets bound tail latency during failover"),
    ("CHK-2", "completely unrelated content about spaceships"),
    ("CHK-3", "a retry budget appears again in this third chunk"),
]


# ---------------------------------------------------------------------------
# fingerprinting
# ---------------------------------------------------------------------------


def test_chunk_fingerprint_is_order_independent():
    assert tv.chunk_fingerprint(["a", "b", "c"]) == tv.chunk_fingerprint(["c", "a", "b"])


def test_chunk_fingerprint_distinguishes_different_id_sets():
    assert tv.chunk_fingerprint(["a", "b"]) != tv.chunk_fingerprint(["a", "c"])


def test_chunk_fingerprint_of_nothing_is_zero():
    assert set(tv.chunk_fingerprint([])) == {"0"}


def test_chunk_fingerprint_base_folds_incrementally():
    """The property :meth:`FulltextIndex.add_chunks` relies on: folding new
    ids into an existing fingerprint equals fingerprinting the union."""
    whole = tv.chunk_fingerprint(["a", "b", "c", "d"])
    incremental = tv.chunk_fingerprint(["c", "d"], base=tv.chunk_fingerprint(["a", "b"]))
    assert whole == incremental


def test_corpus_fingerprint_matches_the_same_ids_folded_by_hand(store):
    ids = [f"CHK-{i}" for i in range(5)]
    for i, cid in enumerate(ids):
        _insert_chunk(store, cid, f"text number {i}")
    count, fingerprint = tv.corpus_fingerprint(store.knowledge)
    assert count == 5
    assert fingerprint == tv.chunk_fingerprint(ids)


_SEQ = {"n": 0}


def _insert_chunk(store, chunk_id: str, text: str) -> None:
    """One ``chunk`` row (plus, on first call, the ``source``/``document``/
    ``element`` rows its FKs need). The fingerprint/reindex paths read only
    ``chunk_id``/``text`` and never join, so this is deliberately the
    thinnest FK-correct corpus a test can have -- richer, chunker-derived
    corpora live in ``tests/_retrieve_fixtures.py`` and are what the
    parity test uses."""
    row = store.knowledge.execute("SELECT doc_id FROM document LIMIT 1").fetchone()
    if row is None:
        launch_id = fx.bootstrap_launch(store)
        insert(
            store, "source",
            {"source_id": "SRC-tv", "kind": "report", "title": "tantivy unit fixture", "license_tier": "open",
             "acquisition_route": "web", "request_state": "indexed", "registered_ts": now(),
             "registered_by_launch": launch_id},
        )
        insert(
            store, "document",
            {"doc_id": "DOC-tv", "source_id": "SRC-tv", "rel_path": "a.md", "media_type": "md",
             "normalizer_id": "fixture", "normalizer_version": "1", "sha256": "0" * 64, "status": "indexed"},
        )
        insert(
            store, "element",
            {"element_id": "ELM-tv", "doc_id": "DOC-tv", "seq": 0, "type": "NarrativeText",
             "text": "root", "page_number": 1},
        )
    _SEQ["n"] += 1
    insert(
        store, "chunk",
        {"chunk_id": chunk_id, "doc_id": "DOC-tv", "seq": _SEQ["n"], "text": text, "token_count": len(text.split()),
         "element_first": "ELM-tv", "element_last": "ELM-tv", "page_start": 1, "page_end": 1,
         "sha256": sha256_hex(chunk_id), "chunker_id": "fixture", "chunker_version": "1", "created_ts": now()},
    )
    store.knowledge.commit()


# ---------------------------------------------------------------------------
# query contract (shared with ftssearch)
# ---------------------------------------------------------------------------


def test_search_finds_a_matching_chunk(tmp_path):
    index = _build(tmp_path, ROWS)
    assert [h["chunk_id"] for h in index.search("spaceships", limit=10)] == ["CHK-2"]


def test_search_ands_every_token_like_fts5_does(tmp_path):
    """``fts_query_string`` joins quoted tokens with FTS5's implicit AND;
    this backend must not silently OR them (which would be a recall
    difference visible to every caller)."""
    index = _build(tmp_path, ROWS)
    hits = {h["chunk_id"] for h in index.search("retry spaceships", limit=10)}
    assert hits == set()


def test_search_stems_like_the_indexed_text_does(tmp_path):
    """``budgets`` (query) must reach ``budget`` (CHK-3's text) and vice
    versa -- the analyzer runs over the query, not just the corpus."""
    index = _build(tmp_path, ROWS)
    assert {h["chunk_id"] for h in index.search("retry budgets", limit=10)} == {"CHK-1", "CHK-3"}


def test_search_survives_operator_like_tokens_without_raising(tmp_path):
    """The whole class of bug ``fts_query_string`` exists to prevent: raw
    user text carrying a query grammar's own metacharacters."""
    index = _build(tmp_path, ROWS)
    for hostile in ['spell-check AND "unbalanced quote OR NOT this', "field:value^3", "((((", "*", "+-!"]:
        index.search(hostile, limit=10)  # must not raise


def test_search_respects_the_limit(tmp_path):
    index = _build(tmp_path, [(f"CHK-{i}", "repeated keyword in every fixture row") for i in range(10)])
    assert len(index.search("repeated keyword", limit=3)) == 3


def test_search_empty_query_returns_empty_not_an_error(tmp_path):
    index = _build(tmp_path, ROWS)
    assert index.search("", limit=10) == []
    assert index.search("   ", limit=10) == []


def test_search_query_of_only_dropped_tokens_returns_empty(tmp_path):
    index = _build(tmp_path, ROWS)
    assert index.search("!!! ???", limit=10) == []


def test_search_respects_the_chunk_id_allowlist(tmp_path):
    index = _build(tmp_path, ROWS)
    hits = index.search("retry budgets", limit=10, chunk_id_allowlist=["CHK-3"])
    assert [h["chunk_id"] for h in hits] == ["CHK-3"]


def test_search_empty_allowlist_short_circuits_to_no_candidates(tmp_path):
    index = _build(tmp_path, ROWS)
    assert index.search("retry budgets", limit=10, chunk_id_allowlist=[]) == []


def test_search_bm25_uses_the_fts5_sign_convention_lower_is_better(tmp_path):
    """FTS5's ``bm25()`` returns the NEGATED score and ``fts_search``
    orders ascending; a backend returning the raw (higher-is-better)
    tantivy score under the same key name would be a trap for anyone who
    reads the value rather than the order."""
    index = _build(tmp_path, ROWS)
    hits = index.search("retry budgets", limit=10)
    assert len(hits) >= 2
    assert all(h["bm25"] <= 0 for h in hits)
    assert hits == sorted(hits, key=lambda h: h["bm25"])


# ---------------------------------------------------------------------------
# fix pass, finding D-1: punctuated tokens must require adjacency, exactly
# like FTS5's phrase-quoting of the same whole whitespace token
# ---------------------------------------------------------------------------

_PHRASE_ROWS = [
    # the words appear ADJACENT and IN ORDER -- what "state-of-the-art"
    # (one whitespace token, four analyzed terms) must match.
    ("CHK-adjacent", "our approach is a state of the art solution to the problem"),
    # the same four words, present but scattered and out of order -- FTS5's
    # phrase quoting of "state-of-the-art" does NOT match this, because
    # quoting the whole token is an adjacency requirement, not merely a
    # co-occurrence one. A flat conjunction of per-term TermQuery clauses
    # would match this (the D-1 bug); a phrase_query must not.
    ("CHK-scattered", "the art department is state of mind, not of the essence"),
]


def test_a_hyphenated_token_requires_adjacency_like_fts5_phrase_quoting(tmp_path):
    index = _build(tmp_path, _PHRASE_ROWS)
    hits = {h["chunk_id"] for h in index.search("state-of-the-art", limit=10)}
    assert hits == {"CHK-adjacent"}, (
        "a punctuated token that analyzes to multiple terms must require adjacency "
        "(FTS5 phrase semantics), not match on co-occurrence alone"
    )


def test_an_email_shaped_token_requires_adjacency_too(tmp_path):
    index = _build(
        tmp_path,
        [
            ("CHK-email-a", "please contact ops@example.com for the runbook"),
            # same three stemmed terms (op/exampl/com), scattered -- must NOT match
            ("CHK-email-b", "the exampl team uses a shared ops inbox, com support included"),
        ],
    )
    hits = {h["chunk_id"] for h in index.search("ops@example.com", limit=10)}
    assert hits == {"CHK-email-a"}


def test_a_single_term_token_is_unaffected_by_the_phrase_change(tmp_path):
    """The fix is scoped to MULTI-term whitespace tokens -- ordinary
    single-word queries must still AND independently, exactly as before."""
    index = _build(tmp_path, ROWS)
    assert {h["chunk_id"] for h in index.search("retry budgets", limit=10)} == {"CHK-1", "CHK-3"}


# ---------------------------------------------------------------------------
# fix pass, finding D-3: deterministic tie-break so a limit-truncated tied
# block picks the SAME subset every time (and the same one FTS5 picks)
# ---------------------------------------------------------------------------


def test_tied_scores_break_on_chunk_id_ascending(tmp_path):
    """Every row here scores identically (same term, same length text), so
    with no tie-break the returned subset under a limit is arbitrary. The
    fix makes it deterministic: ascending chunk_id, matching
    ``ftssearch.fts_search``'s new ``ORDER BY bm25 ASC, chunk_id ASC``."""
    rows = [(f"CHK-{i:02d}", "identical filler text repeated marker") for i in range(20)]
    index = _build(tmp_path, rows)
    hits = index.search("marker", limit=5)
    assert len({h["bm25"] for h in hits}) == 1, "the fixture must actually produce one tied score block"
    assert [h["chunk_id"] for h in hits] == sorted(h["chunk_id"] for h in hits)
    assert [h["chunk_id"] for h in hits] == [f"CHK-{i:02d}" for i in range(5)]


# ---------------------------------------------------------------------------
# derived state: readiness, idempotency, rebuild
# ---------------------------------------------------------------------------


def test_add_chunks_is_idempotent(tmp_path):
    index = _build(tmp_path, ROWS)
    before = tv.read_meta(index.index_dir)
    again = index.add_chunks(ROWS)
    assert again == {"added": 0, "skipped": 3, "chunk_count": 3}
    assert tv.read_meta(index.index_dir)["chunk_fingerprint"] == before["chunk_fingerprint"]


def test_add_chunks_appends_only_the_new_rows(tmp_path):
    index = _build(tmp_path, ROWS)
    outcome = index.add_chunks(ROWS + [("CHK-4", "a brand new fourth chunk about quorum")])
    assert outcome["added"] == 1
    assert outcome["chunk_count"] == 4
    assert tv.read_meta(index.index_dir)["chunk_fingerprint"] == tv.chunk_fingerprint(
        ["CHK-1", "CHK-2", "CHK-3", "CHK-4"]
    )


def test_open_returns_none_for_a_directory_that_was_never_built(tmp_path):
    assert tv.open_fulltext_index(tmp_path / "nope") is None


def test_open_returns_none_without_the_readiness_sidecar(tmp_path):
    """The crash-mid-rebuild case: index files on disk, no sidecar. A
    reader must refuse it (and fall back), never serve a partial corpus."""
    index = _build(tmp_path, ROWS)
    (index.index_dir / tv.META_FILENAME).unlink()
    assert tv.open_fulltext_index(index.index_dir) is None


def test_open_returns_none_for_a_sidecar_from_another_schema_version(tmp_path):
    index = _build(tmp_path, ROWS)
    meta_path = index.index_dir / tv.META_FILENAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["schema_version"] = tv.SCHEMA_VERSION + 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert tv.open_fulltext_index(index.index_dir) is None


def test_open_returns_none_for_a_torn_sidecar(tmp_path):
    index = _build(tmp_path, ROWS)
    (index.index_dir / tv.META_FILENAME).write_text("{not json", encoding="utf-8")
    assert tv.open_fulltext_index(index.index_dir) is None


def test_reindex_rebuilds_the_whole_corpus_from_knowledge_db(store, tmp_path):
    for i in range(6):
        _insert_chunk(store, f"CHK-{i}", f"synthetic chunk {i} about quorum reconfiguration and leases")
    out = tv.reindex(store.knowledge, _index_dir(tmp_path))
    assert out["chunks_indexed"] == 6
    assert out["indexed_docs"] == 6
    index = tv.open_fulltext_index(_index_dir(tmp_path))
    assert index is not None
    assert len(index.search("quorum", limit=50)) == 6


def test_reindex_is_repeatable_and_lands_on_the_same_fingerprint(store, tmp_path):
    for i in range(4):
        _insert_chunk(store, f"CHK-{i}", f"chunk {i}")
    first = tv.reindex(store.knowledge, _index_dir(tmp_path))
    second = tv.reindex(store.knowledge, _index_dir(tmp_path))
    assert first["chunk_fingerprint"] == second["chunk_fingerprint"]
    assert first["chunks_indexed"] == second["chunks_indexed"] == 4


def test_reindex_drops_documents_that_no_longer_exist_in_the_corpus(store, tmp_path):
    """A rebuild is the repair for skew in BOTH directions -- including
    the one an append-only incremental path can never fix on its own."""
    for i in range(3):
        _insert_chunk(store, f"CHK-{i}", f"chunk {i} mentions quorum")
    tv.reindex(store.knowledge, _index_dir(tmp_path))
    store.knowledge.execute("DELETE FROM chunk WHERE chunk_id = 'CHK-1'")
    store.knowledge.commit()
    out = tv.reindex(store.knowledge, _index_dir(tmp_path))
    assert out["chunks_indexed"] == 2
    index = tv.open_fulltext_index(_index_dir(tmp_path))
    assert {h["chunk_id"] for h in index.search("quorum", limit=50)} == {"CHK-0", "CHK-2"}


# ---------------------------------------------------------------------------
# index_status (what the doctor check reports on)
# ---------------------------------------------------------------------------


def test_index_status_reports_ok_when_index_matches_corpus(store, tmp_path):
    for i in range(3):
        _insert_chunk(store, f"CHK-{i}", f"chunk {i}")
    tv.reindex(store.knowledge, _index_dir(tmp_path))
    status = tv.index_status(store.knowledge, _index_dir(tmp_path))
    assert status["state"] == "ok"
    assert status["db_chunks"] == status["index_docs"] == status["meta_chunks"] == 3
    assert status["db_fingerprint"] == status["meta_fingerprint"]


def test_index_status_reports_missing_when_never_built(store, tmp_path):
    _insert_chunk(store, "CHK-1", "hello")
    assert tv.index_status(store.knowledge, _index_dir(tmp_path))["state"] == "missing"


def test_index_status_reports_stale_when_the_corpus_moved_on(store, tmp_path):
    for i in range(3):
        _insert_chunk(store, f"CHK-{i}", f"chunk {i}")
    tv.reindex(store.knowledge, _index_dir(tmp_path))
    _insert_chunk(store, "CHK-99", "a chunk written after the index was built")
    status = tv.index_status(store.knowledge, _index_dir(tmp_path))
    assert status["state"] == "stale"
    assert status["db_chunks"] == 4
    assert status["index_docs"] == 3


def test_index_status_reports_stale_when_the_sidecar_drifted_from_the_index(store, tmp_path):
    """The crash-between-commit-and-sidecar case: the index holds the right
    documents but the sidecar's claim is wrong. Comparing THREE numbers,
    not two, is what catches this."""
    for i in range(3):
        _insert_chunk(store, f"CHK-{i}", f"chunk {i}")
    tv.reindex(store.knowledge, _index_dir(tmp_path))
    meta_path = _index_dir(tmp_path) / tv.META_FILENAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["chunk_count"] = 99
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert tv.index_status(store.knowledge, _index_dir(tmp_path))["state"] == "stale"


def test_appending_to_an_index_with_no_sidecar_is_refused(tmp_path):
    """An interrupted rebuild leaves index files with no readiness
    sidecar. Appending to those would bless a fraction of the corpus as
    complete, and the serving path trusts a ready index -- so the append
    path refuses and names the rebuild instead."""
    index = _build(tmp_path, ROWS)
    (index.index_dir / tv.META_FILENAME).unlink()
    with pytest.raises(tv.FulltextIndexNotReadyError) as exc:
        tv.add_chunks(index.index_dir, [("CHK-4", "another chunk")])
    assert "reindex-fulltext" in str(exc.value)


def test_reindex_recovers_an_index_whose_sidecar_was_lost(tmp_path, store):
    """...and the rebuild it names does clear that state."""
    for i in range(3):
        _insert_chunk(store, f"CHK-{i}", f"chunk {i} about quorum")
    tv.reindex(store.knowledge, _index_dir(tmp_path))
    (_index_dir(tmp_path) / tv.META_FILENAME).unlink()
    assert tv.open_fulltext_index(_index_dir(tmp_path)) is None
    tv.reindex(store.knowledge, _index_dir(tmp_path))
    assert tv.open_fulltext_index(_index_dir(tmp_path)) is not None
