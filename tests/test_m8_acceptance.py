"""M8 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m14_acceptance.py``/``tests/test_m6_acceptance.py``
convention: this file IS the acceptance-criteria mapping, each test here
re-running (not replacing) a narrower assertion that already lives in its
dedicated module.

    | Acceptance criterion (design Section 12, M8 row)                                                  | Test |
    |-----------------------------------------------------------------------------------------------------|------|
    | every result row schema-validates w/ non-null citation                                              | test_every_result_row_schema_validates_with_a_non_null_citation (see also test_retrieval_engine.py::test_search_returns_a_non_null_citation_block_on_every_result) |
    | known-quote query returns its anchor page/span                                                      | test_known_quote_query_returns_its_anchor_page_and_span (see also test_retrieval_engine.py::test_resolve_quote_exact_match_returns_page_and_span) |
    | fenced-corpus fixture: commercial_restricted search/get_chunk return no verbatim run >20 words, fenced:true, anchor still resolves | test_fenced_corpus_search_and_get_chunk_never_serve_a_verbatim_run_over_20_words |
    | 15k-chunk fixture p95 latency <500ms (fixture vectors synthetic — no GPU in-lane)                    | test_15k_chunk_fixture_p95_search_latency_is_under_500ms (16-dim, fast sanity check); test_15k_chunk_fixture_p95_search_latency_is_under_500ms_at_production_dims (FX-7: same criterion at 2048-dim, the real Qwen3-4B production width) |
    | MCP smoke via Claude Code (integration session)                                                     | test_mcp_smoke_via_claude_code_is_an_integration_session_item (marks the live-CC step; the closest an offline pytest run can get is test_mcp_knowledge_protocol.py's full StringIO wire round trip PLUS its real-subprocess stdio smoke) |
"""

from __future__ import annotations

import math
import time

import pytest

from trialerror.accept.journeys import GPU_LIVE_CC_ITEMS
from trialerror.retrieve import engine
from trialerror.util.ids import split_id

from tests._retrieve_fixtures import build_bulk_corpus, build_small_corpus

pytestmark = pytest.mark.acceptance


# ---------------------------------------------------------------------------
# criterion 1: every result row schema-validates w/ non-null citation
# ---------------------------------------------------------------------------


def _assert_valid_search_response_row(row: dict) -> None:
    """Design Section 7's SearchResponse.results[] shape: "a result row
    without a citation block is a bug, enforced by the response schema
    (non-nullable fields)"."""
    for field in ("rank", "score", "fusion", "chunk_id", "doc_id", "source_id", "text", "fenced"):
        assert field in row, f"missing field {field!r}"
    assert row["rank"] >= 1
    assert isinstance(row["fenced"], bool)
    assert row["text"]

    citation = row["citation"]
    assert citation is not None, "result row has no citation block"
    for field in ("source_id", "title", "license_tier", "anchor", "quote"):
        assert citation.get(field) is not None, f"citation missing non-null {field!r}: {citation!r}"
    anchor = citation["anchor"]
    assert anchor["anchor_id"] is not None
    assert anchor["char_start"] is not None
    assert anchor["char_end"] is not None
    # ids are typed (design Section 4: "typed prefixes") -- a malformed id
    # would fail this parse, catching a shape regression cheaply.
    split_id(row["chunk_id"])
    split_id(row["doc_id"])
    split_id(row["source_id"])
    split_id(anchor["anchor_id"])


def test_every_result_row_schema_validates_with_a_non_null_citation(store):
    build_small_corpus(store)
    for query, mode in [
        ("retry budgets bound tail latency", "hybrid"),
        ("quorum reconfiguration lease fencing proprietary epoch counter", "fts"),
        ("coordinator arbitrates lock conflicts", "vector"),
    ]:
        r = engine.search(store, query=query, mode=mode)
        assert r["ok"] is True
        assert r["results"], f"expected at least one result for query={query!r} mode={mode!r}"
        for row in r["results"]:
            _assert_valid_search_response_row(row)


# ---------------------------------------------------------------------------
# criterion 2: known-quote query returns its anchor page/span
# ---------------------------------------------------------------------------


def test_known_quote_query_returns_its_anchor_page_and_span(store):
    corpus = build_small_corpus(store)
    anchor = dict(
        store.knowledge.execute(
            "SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)
        ).fetchone()
    )
    result = engine.resolve_quote(store, anchor["quote_text"])
    assert result["found"] is True
    assert result["match_type"] == "exact"
    match = result["matches"][0]
    assert match["anchor_id"] == anchor["anchor_id"]
    assert match["page"] == anchor["page_number"]
    assert match["char_start"] == anchor["char_start"]
    assert match["char_end"] == anchor["char_end"]


# ---------------------------------------------------------------------------
# criterion 3: fenced-corpus fixture
# ---------------------------------------------------------------------------


def _longest_common_word_run(a_words: list[str], b_words: list[str]) -> int:
    """Longest contiguous word-sequence shared between ``a_words`` and
    ``b_words`` (classic longest-common-substring DP, over words rather
    than characters) — the rigorous form of "no verbatim run >N words":
    rather than trusting this module's OWN excerpt-length bookkeeping, this
    independently re-derives the longest run the SERVED text shares with
    the ORIGINAL unfenced source text, word by word."""
    la, lb = len(a_words), len(b_words)
    if la == 0 or lb == 0:
        return 0
    prev = [0] * (lb + 1)
    best = 0
    for i in range(1, la + 1):
        curr = [0] * (lb + 1)
        for j in range(1, lb + 1):
            if a_words[i - 1] == b_words[j - 1]:
                curr[j] = prev[j - 1] + 1
                best = max(best, curr[j])
        prev = curr
    return best


def test_fenced_corpus_search_and_get_chunk_never_serve_a_verbatim_run_over_20_words(store):
    corpus = build_small_corpus(store)
    restricted_chunk_id = corpus["restricted_chunk_ids"][0]
    original_text = dict(
        store.knowledge.execute("SELECT text FROM chunk WHERE chunk_id = ?", (restricted_chunk_id,)).fetchone()
    )["text"]
    original_words = original_text.split()
    assert len(original_words) > 40, "fixture paragraph must be long enough for a >20-word violation to be possible"

    # --- search() ---
    r = engine.search(store, query="quorum reconfiguration lease fencing proprietary epoch counter", mode="fts")
    search_row = next(row for row in r["results"] if row["chunk_id"] == restricted_chunk_id)
    assert search_row["fenced"] is True
    served_words = search_row["text"].split()
    assert _longest_common_word_run(served_words, original_words) <= 20
    quote_words = search_row["citation"]["quote"].split()
    assert _longest_common_word_run(quote_words, original_words) <= 20
    # anchor still resolves precisely -- the anchor_id names a real, live row
    anchor_id = search_row["citation"]["anchor"]["anchor_id"]
    anchor_row = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE anchor_id = ?", (anchor_id,)).fetchone())
    assert anchor_row["char_start"] == search_row["citation"]["anchor"]["char_start"]
    assert anchor_row["char_end"] == search_row["citation"]["anchor"]["char_end"]

    # --- get_chunk() ---
    gc = engine.get_chunk(store, restricted_chunk_id)
    assert gc["fenced"] is True
    gc_words = gc["text"].split()
    assert _longest_common_word_run(gc_words, original_words) <= 20
    for a in gc["anchors"]:
        assert _longest_common_word_run((a["quote"] or "").split(), original_words) <= 20
        # anchor still resolves precisely
        assert dict(store.knowledge.execute("SELECT 1 FROM quote_anchor WHERE anchor_id = ?", (a["anchor_id"],)).fetchone())


def test_fenced_corpus_fixture_open_source_is_never_fenced_control_case(store):
    """Control: the OPEN-license sibling document in the same fixture is
    never fenced -- proves the fence is license-tier-driven, not a blanket
    truncation of every result."""
    corpus = build_small_corpus(store)
    r = engine.search(store, query="retry budgets bound tail latency", mode="fts")
    row = next(x for x in r["results"] if x["chunk_id"] == corpus["open_chunk_ids"][0])
    assert row["fenced"] is False
    assert "license-fenced" not in row["text"]


# ---------------------------------------------------------------------------
# criterion 4: 15k-chunk fixture p95 latency <500ms
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = max(0, math.ceil(pct * len(ordered)) - 1)
    return ordered[idx]


def test_15k_chunk_fixture_p95_search_latency_is_under_500ms(store):
    """Design Section 12 M8 row: "15k-chunk fixture p95 latency <500ms
    (fixture vectors synthetic -- no GPU in-lane)". The fixture (``tests.
    _retrieve_fixtures.build_bulk_corpus``) uses ``FakeEmbedBackend``
    (hash-derived, zero-model, zero-GPU synthetic vectors, design Section
    13 flag F18) and the default ``auto`` search mode (FTS prefilter ->
    vector rerank -> RRF), the realistic default path every caller
    actually takes."""
    n_chunks = 15_000
    corpus = build_bulk_corpus(store, n_chunks=n_chunks, n_docs=30)
    assert corpus["n_chunks"] == n_chunks

    queries = [f"topic-{i}" for i in range(0, 37)] + [f"ALPHA{i}" for i in range(0, 53, 2)] + [
        "synthetic fixture chunk", "latency retrieval testing", "unique reference marker",
    ]

    timings_ms: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        r = engine.search(store, query=q)
        timings_ms.append((time.perf_counter() - t0) * 1000)
        assert r["ok"] is True

    p95 = _percentile(timings_ms, 0.95)
    assert p95 < 500, f"p95 search latency over {n_chunks} chunks was {p95:.1f}ms (>= 500ms bound); all timings: {sorted(timings_ms)}"


#: The production vector width this build's real embedding backend
#: produces: ``trialerror.ingest.backends.load_embed_backend`` defaults
#: ``RealQwenEmbedBackend``'s ``dims`` to 2048 (matryoshka-truncated
#: Qwen3-4B, per the origin-project ``embed_backend.py`` C-0060 pin its own docstring
#: cites) whenever ``trialerror.toml``'s ``[ingest.embed]`` table configures a
#: real (non-"fake") backend. FX-7 (IMPL_REVIEW_VERDICT.md Tier 2 /
#: IMPL_REVIEW_C_ops.md WEAK): the acceptance-criterion test above runs at
#: ``FakeEmbedBackend``'s own default of 16 dims -- fine for exercising
#: FTS/RRF fusion logic cheaply, but a p95 bound meant to gate real-world
#: latency should also be measured at the width production queries will
#: actually carry through the vector tier (bigger vectors -> more bytes
#: scanned per candidate in the ``vec_chunks__<model_key>`` table).
PRODUCTION_VECTOR_DIMS = 2048


def test_15k_chunk_fixture_p95_search_latency_is_under_500ms_at_production_dims(store):
    """FX-7: same acceptance criterion and same fixture shape as
    ``test_15k_chunk_fixture_p95_search_latency_is_under_500ms`` above --
    deliberately NOT replacing it (that test stays as the cheap, fast
    sanity check; this one is the width-realistic gate) -- but built with
    ``FakeEmbedBackend(dims=PRODUCTION_VECTOR_DIMS)`` (still synthetic,
    still zero-GPU/zero-model per design Section 13 flag F18 -- only the
    vector WIDTH changes, not the backend) so the latency bound reflects
    what a real Qwen3-4B-configured program's vector tier actually scans.
    TRIALERROR-DEV-NOTE: kept as an addition rather than a dims= parametrization
    of the existing test so the 16-dim test's name/identity (and its
    place in the acceptance-criteria mapping table at the top of this
    file) doesn't change under existing callers/CI history.
    """
    n_chunks = 15_000
    corpus = build_bulk_corpus(store, n_chunks=n_chunks, n_docs=30, dims=PRODUCTION_VECTOR_DIMS)
    assert corpus["n_chunks"] == n_chunks
    assert corpus["dims"] == PRODUCTION_VECTOR_DIMS

    queries = [f"topic-{i}" for i in range(0, 37)] + [f"ALPHA{i}" for i in range(0, 53, 2)] + [
        "synthetic fixture chunk", "latency retrieval testing", "unique reference marker",
    ]

    timings_ms: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        r = engine.search(store, query=q)
        timings_ms.append((time.perf_counter() - t0) * 1000)
        assert r["ok"] is True

    p95 = _percentile(timings_ms, 0.95)
    assert p95 < 500, (
        f"p95 search latency over {n_chunks} chunks at {PRODUCTION_VECTOR_DIMS}-dim was {p95:.1f}ms "
        f"(>= 500ms bound); all timings: {sorted(timings_ms)}"
    )


def test_15k_chunk_fixture_p95_get_chunk_latency_is_under_500ms(store):
    """Same 15k-chunk fixture, the ``get_chunk`` tool's own latency (a
    point lookup, expected far under the search bound -- included so the
    acceptance suite covers more than just ``search``)."""
    corpus = build_bulk_corpus(store, n_chunks=15_000, n_docs=30)
    sample_ids = corpus["chunk_ids"][::750]  # 20 evenly-spaced ids

    timings_ms: list[float] = []
    for cid in sample_ids:
        t0 = time.perf_counter()
        engine.get_chunk(store, cid)
        timings_ms.append((time.perf_counter() - t0) * 1000)

    p95 = _percentile(timings_ms, 0.95)
    assert p95 < 500, f"p95 get_chunk latency was {p95:.1f}ms; all timings: {sorted(timings_ms)}"


# ---------------------------------------------------------------------------
# criterion 5: MCP smoke via Claude Code (integration session)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_mcp_smoke_knowledge_server"])
def test_mcp_smoke_via_claude_code_is_an_integration_session_item():
    """Design Section 12 M8 row names this "MCP smoke via Claude Code
    (integration session)" -- same class of item as M3's/M6's/M14's own
    live-CC hook/MCP acceptance rows ("live-CC hook tests are
    orchestrator-executed integration items"): genuinely live only inside
    an actual Claude Code session with this server registered.

    The closest a pytest run can get:

    - ``tests/test_mcp_knowledge_protocol.py``'s in-process layer drives
      the full ``initialize`` -> ``tools/list`` -> ``tools/call`` sequence
      over the REAL JSON-RPC wire (not mocked), including a search ->
      get_chunk round trip proving citation ids resolve end to end.
    - ``tests/test_mcp_knowledge_protocol.py::
      test_stdio_smoke_real_subprocess_initialize_tools_list_and_call``
      launches ``python -m trialerror.cli mcp knowledge`` as a REAL subprocess
      and speaks real stdio to it -- this build's "stdio smoke result".

    FX-6 (IMPL_REVIEW_VERDICT.md Tier 2 / IMPL_REVIEW_C_ops.md HOLLOW):
    this was a literal ``assert True`` marker/pointer with no assertions of
    its own -- honest per its own docstring ("marker/pointer only") but
    indistinguishable from a real pass in a green suite. Converted to an
    explicit ``@pytest.mark.skip`` naming the exact live-CC step (the SAME
    ``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS`` message ``trialerror accept``'s
    own doctor-shaped summary reports, so there is one source of truth for
    this item's status, not two that could drift) -- the mapping table
    above still has a row to point at, now truthfully marked not-yet-run
    rather than fake-passing. The two real, already-landed proxy tests
    named in this docstring are NOT duplicative of this one (they assert
    real things over a real wire); nothing was deleted.
    """
