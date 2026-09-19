"""Backend parity: the tantivy lexical tier must be a DROP-IN for the FTS5
one (C-0080 design constraint 5). Not "similar results" -- the same
``SearchResponse`` rows, byte for byte, including the anchors and the
fenced citation quotes, over a real chunker-built fixture corpus.

Everything here runs the FULL engine (``trialerror.retrieve.engine.search``)
twice over ONE store, flipping ``trialerror.toml``'s ``[retrieve]
fulltext_backend`` between the runs -- not the backends' own ``search``
functions in isolation. Anchors and quotes are derived from
``knowledge.db`` by rank-ordered chunk_id, so a row-level diff is the only
thing that actually proves callers cannot tell the two apart.

**The three documented, tested tolerances** -- all are consequences of BM25
being computed by two different engines (or, for tolerance 3, of tantivy-py
shipping no filter that reproduces FTS5's exact folding table), and all are
bounded:

1. *Stemmer divergence.* FTS5 tokenizes with ``porter unicode61`` (the
   original 1980 Porter stemmer); tantivy's Snowball ``english`` filter is
   Porter2. :func:`stem_stable_terms` measures the disagreement against the
   live corpus rather than assuming it, and
   :func:`test_stemmer_divergence_stays_negligible` pins how small it is
   (measured on this corpus: 2 of ~1,200 distinct terms). Query terms are
   drawn only from the agreeing set -- a term the two tokenizers reduce
   differently is a different query on each side, so comparing its results
   would test nothing.
2. *Score ties.* Two chunks a backend scores EXACTLY equal have no
   defined order between them, so the two engines may emit them in
   different orders -- the tolerance the C-0080 constraint itself names
   ("rank-order differences only where scores tie").
   :func:`assert_row_parity` allows a swap only when at least one backend
   calls the pair a tie, and it defeats the related trap by never
   truncating: ``k`` is the query's full candidate count, so a cut through
   a tied block can't turn "arbitrary order" into "different hit set". The
   one place truncation is unavoidable is the ``limit`` cap itself, when a
   term appears in more than 500 chunks with IDF ~ 0; those queries are
   refused by :func:`assert_row_parity` and covered explicitly by
   :func:`test_a_saturating_tied_query_truncates_on_both_backends` instead,
   so the case is documented rather than quietly skipped. Below that cap,
   :mod:`trialerror.retrieve.ftssearch`/:mod:`trialerror.retrieve.tantivysearch`
   now both break ties on ``chunk_id`` ascending (finding D-3), so a
   ``limit`` that cuts through a tied block keeps the SAME subset on both
   backends rather than an arbitrary one each.
3. *ASCII-folding divergence.* ``Filter.ascii_fold()`` folds every Latin
   letter to its ASCII base (``Ø`` -> ``o``); FTS5 unicode61's diacritic
   folding removes combining marks but not a distinct base letter, so
   composed diacritics agree (``café``/``cafe`` match on both) while a
   letter like ``Ø`` does not (finding D-2). :func:`fold_stable_terms`
   measures this the same way :func:`stem_stable_terms` measures tolerance
   1, and :func:`test_ascii_fold_divergence_is_measured_and_one_directional`
   pins it: every divergent term WIDENS tantivy's recall relative to FTS5,
   never the reverse.

Outside those three, parity is exact: same hit sets, same anchors, same
fenced quotes, and -- over the whole query set below -- not one pair of
chunks the two backends rank in opposite directions.
"""

from __future__ import annotations

import collections
import re
import sqlite3

import pytest

from tests import _retrieve_fixtures as fx
from trialerror.ingest.anchors import sha256_hex
from trialerror.retrieve import engine, tantivysearch as tv
from trialerror.retrieve.ftssearch import DEFAULT_FTS_CANDIDATE_LIMIT, fts_search
from trialerror.stores import paths
from trialerror.stores.writer import insert
from trialerror.util.timeutil import now

pytestmark = pytest.mark.skipif(not tv.tantivy_available(), reason="tantivy-py not installed")

#: Small enough to build in a couple of seconds, big enough that the
#: bulk-corpus marker terms (``ALPHA<n>``, ``R<n>``, ``topic-<n>``) span
#: selective and non-selective frequencies.
_BULK_CHUNKS = 600
_BULK_DOCS = 6

#: Hand-written queries over the ``build_small_corpus`` documents -- the
#: ones with real chunker-derived anchors and a ``commercial_restricted``
#: source, i.e. where fencing and quote-capping are actually exercised.
_SMALL_CORPUS_QUERIES = [
    "retry budgets",
    "distributed schedulers failover",
    "coordinator lock conflicts",
    "quorum reconfiguration",
    "epoch counter",
    "leader election timeouts",
    "heartbeat intervals",
    "proprietary publisher verbatim",
]

#: Selective marker terms from ``build_bulk_corpus``'s synthetic text.
_BULK_QUERIES = ["ALPHA7", "ALPHA31", "R42", "R101 marker", "ALPHA12 reference"]

#: Fix pass, finding D-1: chunks that let a punctuated-token parity query
#: distinguish "matched the adjacent phrase" from "matched the same words
#: scattered anywhere in the chunk". Before the fix, tantivy's flat
#: conjunction of per-term ``TermQuery`` clauses matched BOTH; FTS5's
#: phrase-quoting of the whole whitespace token matches only the first.
_PUNCTUATED_TOKEN_CHUNKS = [
    ("CHK-punct-adjacent", "our approach here is a state of the art solution to the problem"),
    ("CHK-punct-scattered", "the art department is state of mind, not of the essence"),
    ("CHK-punct-email-a", "please contact ops@example.com for the runbook"),
    ("CHK-punct-email-b", "the example team uses a shared ops inbox, com support included"),
]

#: Fix pass, finding D-2: composed diacritics fold the same on both
#: backends (agreement case); a distinct base Latin letter does not
#: (``ascii_fold`` folds it, unicode61's diacritic removal does not).
_NON_ASCII_CHUNKS = [
    ("CHK-diacritic-cafe", "we met at a small cafe near the station"),
    ("CHK-diacritic-nonascii", "we met at a small café near the station"),
    ("CHK-oresund-bridge", "the Øresund bridge links Denmark and Sweden"),
]

_PUNCTUATED_QUERIES = ["state-of-the-art", "ops@example.com"]


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


def _insert_raw_chunk(store, chunk_id: str, text: str) -> None:
    """One thinnest FK-correct ``chunk`` row -- reusing whatever
    document/element ``build_small_corpus`` already created -- plus its
    ``chunk_fts`` shadow row. Enough for both ``fts_search`` and a tantivy
    ``reindex()`` to see it; deliberately skips the anchor/citation
    machinery ``_add_document`` builds, because these fixtures are for
    TIER-level parity checks (:func:`fts_search` / ``FulltextIndex.search``
    directly), never ``engine.search``'s citation-row building."""
    (doc_id,) = store.knowledge.execute("SELECT doc_id FROM document LIMIT 1").fetchone()
    (element_id,) = store.knowledge.execute("SELECT element_id FROM element LIMIT 1").fetchone()
    insert(
        store, "chunk",
        {
            "chunk_id": chunk_id, "doc_id": doc_id, "seq": 9000, "text": text,
            "token_count": len(text.split()), "element_first": element_id, "element_last": element_id,
            "page_start": 1, "page_end": 1, "sha256": sha256_hex(chunk_id),
            "chunker_id": "fixture", "chunker_version": "1", "created_ts": now(),
        },
    )
    store.knowledge.execute("INSERT INTO chunk_fts(chunk_id, text) VALUES (?, ?)", (chunk_id, text))


@pytest.fixture()
def parity_store(store):
    """One store carrying both fixture corpora, with the tantivy index
    built from ``knowledge.db`` exactly the way the shipped
    ``trialerror ingest reindex-fulltext`` command builds it."""
    fx.build_small_corpus(store)
    fx.build_bulk_corpus(store, n_chunks=_BULK_CHUNKS, n_docs=_BULK_DOCS)
    for chunk_id, text in _PUNCTUATED_TOKEN_CHUNKS + _NON_ASCII_CHUNKS:
        _insert_raw_chunk(store, chunk_id, text)
    store.knowledge.commit()
    tv.reindex(store.knowledge, paths.fulltext_index_path(store.program_root))
    return store


def pin_backend(store, backend: str) -> None:
    (store.program_root / "trialerror.toml").write_text(
        f'[program]\nid = "PROG-parity"\n\n[retrieve]\nfulltext_backend = "{backend}"\n',
        encoding="utf-8",
    )


def porter_terms(text: str) -> list[str]:
    """What FTS5's own ``porter unicode61`` tokenizer makes of ``text`` --
    read back out of SQLite through an ``fts5vocab`` shadow table, so this
    is the REAL tokenizer the incumbent backend indexes with, not a
    reimplementation of it."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='porter unicode61')")
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, row)")
        conn.execute("INSERT INTO t(text) VALUES (?)", (text,))
        return sorted(r[0] for r in conn.execute("SELECT term FROM v"))
    finally:
        conn.close()


def corpus_vocabulary(store) -> collections.Counter:
    vocab: collections.Counter = collections.Counter()
    for (text,) in store.knowledge.execute("SELECT text FROM chunk"):
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", text):
            vocab[word.lower()] += 1
    return vocab


def stem_stable_terms(store, index) -> tuple[list[str], list[str]]:
    """Split the corpus vocabulary into terms the two analyzers agree on
    and terms they don't. Measured against the live corpus every run, so
    this never goes stale the way a hard-coded exclusion list would."""
    stable, divergent = [], []
    for word in corpus_vocabulary(store):
        (stable if sorted(index.analyze(word)) == porter_terms(word) else divergent).append(word)
    return sorted(stable), sorted(divergent)


#: Fix pass, finding D-2 -- a small, deliberately non-ASCII probe vocabulary.
#: ``corpus_vocabulary``'s ``[A-Za-z][A-Za-z0-9]{3,}`` regex is ASCII-only
#: by construction (it also drives tolerance 1's wide net), so it structurally
#: cannot surface this divergence -- these terms are hand-picked instead.
_ASCII_FOLD_PROBE_TERMS = ["cafe", "café", "naifs", "naïfs", "Øresund", "oresund", "Grüße", "gruesse", "grusse"]


def fold_stable_terms(index) -> tuple[list[str], list[str]]:
    """Tolerance 3's counterpart to :func:`stem_stable_terms`: which of
    :data:`_ASCII_FOLD_PROBE_TERMS` fold to the SAME analyzed term on both
    backends (``porter_terms`` doubles as the read-back of FTS5's real
    unicode61 folding here, same as it does for stemming), and which don't."""
    stable, divergent = [], []
    for word in _ASCII_FOLD_PROBE_TERMS:
        (stable if sorted(index.analyze(word)) == porter_terms(word) else divergent).append(word)
    return sorted(stable), sorted(divergent)


def _rank_independent(row: dict) -> dict:
    """A result row minus the three fields that encode WHERE it landed --
    everything a caller reads about the chunk itself, which must be
    identical no matter which engine found it."""
    return {k: v for k, v in row.items() if k not in ("rank", "score", "fusion")}


def tie_ranks(rows: list[dict]) -> dict[str, int]:
    """``{chunk_id: tie_block_index}`` for one backend's own scored rows.
    Chunks sharing a block scored EXACTLY equal on that backend, so their
    relative order there is arbitrary and carries no information."""
    by_score: dict[float, list[str]] = {}
    for hit in rows:
        by_score.setdefault(round(hit["bm25"], 6), []).append(hit["chunk_id"])
    return {cid: i for i, score in enumerate(sorted(by_score)) for cid in by_score[score]}


def assert_row_parity(store, query: str, *, mode: str = "fts") -> list[dict]:
    """Run ``engine.search`` under both backends and assert the responses
    are indistinguishable. Returns the fts5 run's result list.

    ``k`` is set to the query's FULL candidate count rather than a fixed
    number, deliberately: a fixed ``k`` that happens to cut through a
    score-tied block would make the two backends return different (equally
    correct) subsets of that block, and the test would be measuring the
    arbitrary half of BM25 rather than the backends. With every candidate
    returned, the hit SET must match exactly, every ROW PAYLOAD must match
    exactly, and the only freedom left is order inside a tie block --
    which is checked against each backend's own tie structure.

    Refuses -- loudly -- to compare a query whose lexical tier saturated
    ``DEFAULT_FTS_CANDIDATE_LIMIT`` (module docstring tolerance 2)."""
    tier_hits = fts_search(store, query, limit=DEFAULT_FTS_CANDIDATE_LIMIT)
    assert len(tier_hits) < DEFAULT_FTS_CANDIDATE_LIMIT, (
        f"query {query!r} saturates the {DEFAULT_FTS_CANDIDATE_LIMIT}-candidate cap; "
        "pick a more selective parity query (see the module docstring)"
    )

    k = max(len(tier_hits), 1)
    pin_backend(store, "fts5")
    left = engine.search(store, query=query, k=k, mode=mode)
    pin_backend(store, "tantivy")
    right = engine.search(store, query=query, k=k, mode=mode)

    assert left["stats"]["fulltext_backend"] == "fts5"
    assert right["stats"]["fulltext_backend"] == "tantivy"
    assert left["tiers_used"] == right["tiers_used"]
    assert left["stats"]["fts_candidates"] == right["stats"]["fts_candidates"]

    by_id_left = {r["chunk_id"]: r for r in left["results"]}
    by_id_right = {r["chunk_id"]: r for r in right["results"]}
    assert set(by_id_left) == set(by_id_right), (
        f"{query!r}: hit SETS differ\n  only fts5:    {sorted(set(by_id_left) - set(by_id_right))}"
        f"\n  only tantivy: {sorted(set(by_id_right) - set(by_id_left))}"
    )
    for chunk_id, row in by_id_left.items():
        assert _rank_independent(row) == _rank_independent(by_id_right[chunk_id]), (
            f"{query!r}: row payload differs for {chunk_id}"
        )

    # order: no pair may be ranked in OPPOSITE directions unless at least
    # one backend scored them as a tie.
    lex_left, lex_right = tie_ranks(tier_hits), tie_ranks(
        tv.open_fulltext_index(paths.fulltext_index_path(store.program_root)).search(
            query, limit=DEFAULT_FTS_CANDIDATE_LIMIT
        )
    )
    order_left = [r["chunk_id"] for r in left["results"]]
    order_right = [r["chunk_id"] for r in right["results"]]
    position_right = {cid: i for i, cid in enumerate(order_right)}
    for i, first in enumerate(order_left):
        for second in order_left[i + 1 :]:
            if lex_left[first] == lex_left[second] or lex_right[first] == lex_right[second]:
                continue  # tied on one side: order between them is arbitrary
            assert position_right[first] < position_right[second], (
                f"{query!r}: fts5 ranks {first} above {second} (neither side calls it a tie), "
                "tantivy inverts it"
            )
    return left["results"]


# ---------------------------------------------------------------------------
# the parity assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", _SMALL_CORPUS_QUERIES)
def test_hand_written_queries_return_identical_rows(parity_store, query):
    assert_row_parity(parity_store, query)


@pytest.mark.parametrize("query", _BULK_QUERIES)
def test_bulk_corpus_marker_queries_return_identical_rows(parity_store, query):
    assert_row_parity(parity_store, query)


def test_punctuated_tokens_require_adjacency_on_both_backends(parity_store):
    """Finding D-1, closed. Before the fix, a whitespace token that
    analyzes to more than one term (a hyphenated word, an email address)
    matched on tantivy as a same-chunk conjunction -- a strict SUPERSET of
    FTS5's phrase-quoted, adjacency-required match. Measured at the tier
    level (:func:`fts_search` / ``FulltextIndex.search`` directly) against
    :data:`_PUNCTUATED_TOKEN_CHUNKS`, which are built exactly to
    distinguish "the words are adjacent and in order" from "the words are
    merely present somewhere in the chunk"."""
    index = tv.open_fulltext_index(paths.fulltext_index_path(parity_store.program_root))
    for query in _PUNCTUATED_QUERIES:
        left = {h["chunk_id"] for h in fts_search(parity_store, query, limit=DEFAULT_FTS_CANDIDATE_LIMIT)}
        right = {h["chunk_id"] for h in index.search(query, limit=DEFAULT_FTS_CANDIDATE_LIMIT)}
        assert left == right, f"{query!r}: hit sets differ -- fts5 {sorted(left)}, tantivy {sorted(right)}"
    # and pinned to the SPECIFIC chunk each query must (and must only) hit,
    # so a future change that widens the match silently is caught even if
    # it happens to widen both backends' sets identically.
    assert {h["chunk_id"] for h in fts_search(parity_store, "state-of-the-art", limit=DEFAULT_FTS_CANDIDATE_LIMIT)} == {
        "CHK-punct-adjacent"
    }
    assert {h["chunk_id"] for h in fts_search(parity_store, "ops@example.com", limit=DEFAULT_FTS_CANDIDATE_LIMIT)} == {
        "CHK-punct-email-a"
    }


def test_every_stem_stable_corpus_term_returns_an_identical_hit_set(parity_store):
    """The wide net: instead of trusting a hand-picked query list, take
    the corpus's OWN vocabulary, keep every term the two analyzers reduce
    identically, and demand the same hit set from both backends for each.
    Compares at the tier level (not the engine's top-k) so the assertion
    covers the whole candidate set, not just what survived truncation to
    ``k``."""
    index = tv.open_fulltext_index(paths.fulltext_index_path(parity_store.program_root))
    stable, _divergent = stem_stable_terms(parity_store, index)
    assert len(stable) > 200, "the parity corpus should have a substantial vocabulary"

    checked = 0
    for term in stable:
        left = fts_search(parity_store, term, limit=DEFAULT_FTS_CANDIDATE_LIMIT)
        if len(left) >= DEFAULT_FTS_CANDIDATE_LIMIT:
            continue  # tolerance 2: a truncated tied block, compared separately
        right = index.search(term, limit=DEFAULT_FTS_CANDIDATE_LIMIT)
        assert {h["chunk_id"] for h in left} == {h["chunk_id"] for h in right}, f"hit set differs for {term!r}"
        checked += 1
    assert checked > 200, f"only {checked} terms were actually compared"


def test_no_pair_of_chunks_is_ranked_in_opposite_directions(parity_store):
    """Order parity, stated as the property that actually matters: for
    every pair of chunks both backends returned, they never disagree about
    which comes first -- except inside a block the backend itself scored as
    a tie, where the order is arbitrary by definition."""
    index = tv.open_fulltext_index(paths.fulltext_index_path(parity_store.program_root))

    for query in _SMALL_CORPUS_QUERIES + _BULK_QUERIES:
        left = fts_search(parity_store, query, limit=DEFAULT_FTS_CANDIDATE_LIMIT)
        right = index.search(query, limit=DEFAULT_FTS_CANDIDATE_LIMIT)
        if len(left) >= DEFAULT_FTS_CANDIDATE_LIMIT:
            continue
        rank_left, rank_right = tie_ranks(left), tie_ranks(right)
        ids = [h["chunk_id"] for h in left]
        for i, first in enumerate(ids):
            for second in ids[i + 1 :]:
                if rank_left[first] < rank_left[second]:
                    assert rank_right[first] <= rank_right[second], (
                        f"{query!r}: fts5 ranks {first} above {second}, tantivy inverts it"
                    )


def test_fenced_rows_are_capped_identically_on_both_backends(parity_store):
    """C-0080 design constraint 4: the F3 serving-path fence
    (``retrieve/fence.py``) must behave identically per backend. It runs
    downstream of the tier, so this is really a regression sentinel
    against a backend that somehow returned a different chunk under the
    same query -- which is exactly the failure that would silently widen a
    20-word cap into a verbatim leak."""
    results = assert_row_parity(parity_store, "quorum reconfiguration")
    fenced = [r for r in results if r["fenced"]]
    assert fenced, "the parity corpus's commercial_restricted document should be reachable by this query"
    for row in fenced:
        assert row["citation"]["license_tier"] == "commercial_restricted"
        assert len(row["citation"]["quote"].split()) <= 20


def test_parity_holds_for_the_full_hybrid_pipeline_not_just_the_lexical_tier(parity_store):
    """``mode="auto"`` runs fts -> vector -> RRF. Identical rows here is
    what proves C-0080 design constraint 8 -- no behaviour change to the
    vector tier or the fusion weights -- rather than merely asserting it."""
    for query in ("retry budgets", "epoch counter", "ALPHA7"):
        results = assert_row_parity(parity_store, query, mode="auto")
        assert results


def test_stemmer_divergence_stays_negligible(parity_store):
    """Tolerance 1, pinned rather than hand-waved. If a tantivy upgrade
    swaps the Snowball implementation, this is the test that notices."""
    index = tv.open_fulltext_index(paths.fulltext_index_path(parity_store.program_root))
    stable, divergent = stem_stable_terms(parity_store, index)
    total = len(stable) + len(divergent)
    assert total > 200
    assert len(divergent) / total < 0.02, f"{len(divergent)}/{total} terms stem differently: {divergent[:20]}"


def test_ascii_fold_divergence_is_measured_and_one_directional(parity_store):
    """Tolerance 3, pinned rather than hand-waved (finding D-2). This
    vocabulary is hand-picked, not scanned from the live corpus like
    tolerance 1's -- :func:`corpus_vocabulary`'s ASCII-only regex
    structurally cannot surface a non-ASCII divergence, which is exactly
    how this gap went undisclosed in the first place."""
    index = tv.open_fulltext_index(paths.fulltext_index_path(parity_store.program_root))
    stable, divergent = fold_stable_terms(index)
    assert set(stable) >= {"cafe", "café", "naifs", "naïfs", "oresund", "gruesse", "grusse"}
    assert set(divergent) == {"Øresund", "Grüße"}
    for word in divergent:
        tantivy_terms = index.analyze(word)
        fts_terms = porter_terms(word)
        assert tantivy_terms != fts_terms
        assert all(term.isascii() for term in tantivy_terms), "ascii_fold must fully ascii-fold tantivy's term"
        assert not all(term.isascii() for term in fts_terms), "unicode61 must NOT fold this letter for FTS5"


def test_ascii_fold_divergence_widens_tantivy_recall_never_narrows_it(parity_store):
    """The concrete search-level consequence of tolerance 3: querying the
    plain-ASCII spelling reaches the non-ASCII chunk on tantivy but not on
    FTS5. Documented and measured, not silently divergent -- and, per the
    finding, one-directional: tantivy's hit set is always a SUPERSET of
    FTS5's for this class of query, never a subset."""
    index = tv.open_fulltext_index(paths.fulltext_index_path(parity_store.program_root))
    left = {h["chunk_id"] for h in fts_search(parity_store, "oresund", limit=DEFAULT_FTS_CANDIDATE_LIMIT)}
    right = {h["chunk_id"] for h in index.search("oresund", limit=DEFAULT_FTS_CANDIDATE_LIMIT)}
    assert "CHK-oresund-bridge" not in left, "FTS5 unicode61 must not fold 'Ø' to 'o'"
    assert "CHK-oresund-bridge" in right, "tantivy's ascii_fold DOES fold 'Ø' to 'o'"
    assert right >= left, "tantivy's hit set must be a superset of FTS5's here, never a subset"


def test_a_saturating_tied_query_truncates_on_both_backends(parity_store):
    """Tolerance 2, made explicit. ``fixture`` appears in every bulk chunk,
    so IDF ~ 0 and the whole corpus ties; both backends must return exactly
    the candidate cap, and neither is 'wrong' about which tied rows it
    picked."""
    index = tv.open_fulltext_index(paths.fulltext_index_path(parity_store.program_root))
    left = fts_search(parity_store, "fixture", limit=DEFAULT_FTS_CANDIDATE_LIMIT)
    right = index.search("fixture", limit=DEFAULT_FTS_CANDIDATE_LIMIT)
    assert len(left) == len(right) == DEFAULT_FTS_CANDIDATE_LIMIT
    assert len({round(h["bm25"], 6) for h in left}) == 1
    assert len({round(h["bm25"], 6) for h in right}) == 1
