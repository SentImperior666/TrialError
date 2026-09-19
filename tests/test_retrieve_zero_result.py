"""Lane FB-1 item F3: a zero result explains itself.

``per_term_candidates`` beside ``lexical_search``, the two additive
``stats`` keys, the re-run next action, and the MCP tool inheriting the keys
without its schema moving.
"""

from __future__ import annotations

import pytest

from trialerror.retrieve import engine, tantivysearch as tv
from trialerror.retrieve.lexical import (
    MAX_PER_TERM_CANDIDATES,
    Fts5Backend,
    per_term_candidates,
    resolve_backend,
)
from trialerror.stores import paths

from tests import _retrieve_fixtures as fx


@pytest.fixture()
def corpus(store):
    fx.build_small_corpus(store)
    store.knowledge.commit()
    (store.program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-zero"\n\n[retrieve]\nfulltext_backend = "fts5"\n', encoding="utf-8"
    )
    return store


def _pin(store, backend: str) -> None:
    (store.program_root / "trialerror.toml").write_text(
        f'[program]\nid = "PROG-zero"\n\n[retrieve]\nfulltext_backend = "{backend}"\n', encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# per_term_candidates
# ---------------------------------------------------------------------------


def test_one_count_per_term_naming_the_backend_that_produced_them(corpus):
    counts, backend = per_term_candidates(corpus, "schedulers zzzqqxnothing coordinator")
    assert backend == "fts5"
    assert set(counts) == {"schedulers", "zzzqqxnothing", "coordinator"}
    assert counts["zzzqqxnothing"] == 0
    assert counts["schedulers"] > 0
    assert counts["coordinator"] > 0


def test_terms_keep_query_order_and_duplicates_collapse(corpus):
    counts, _ = per_term_candidates(corpus, "coordinator schedulers coordinator")
    assert list(counts) == ["coordinator", "schedulers"]


def test_the_term_count_is_capped(corpus):
    query = " ".join(f"term{i}" for i in range(MAX_PER_TERM_CANDIDATES + 5))
    counts, _ = per_term_candidates(corpus, query)
    assert len(counts) == MAX_PER_TERM_CANDIDATES


def test_a_blank_query_counts_nothing(corpus):
    counts, _ = per_term_candidates(corpus, "   ")
    assert counts == {}


def test_an_empty_allowlist_makes_every_count_zero(corpus):
    counts, _ = per_term_candidates(corpus, "coordinator schedulers", chunk_id_allowlist=[])
    assert set(counts.values()) == {0}


@pytest.mark.skipif(not tv.tantivy_available(), reason="tantivy-py not installed")
def test_both_backends_give_the_same_counts_on_one_fixture(corpus):
    tv.reindex(corpus.knowledge, paths.fulltext_index_path(corpus.program_root))

    _pin(corpus, "fts5")
    fts_counts, fts_backend = per_term_candidates(corpus, "coordinator schedulers zzzqqxnothing")
    _pin(corpus, "tantivy")
    tv_counts, tv_backend = per_term_candidates(corpus, "coordinator schedulers zzzqqxnothing")

    assert (fts_backend, tv_backend) == ("fts5", "tantivy")
    assert fts_counts == tv_counts


@pytest.mark.skipif(not tv.tantivy_available(), reason="tantivy-py not installed")
def test_the_zero_verdict_is_the_same_on_both_backends_for_an_absent_term(corpus):
    """The counts themselves can diverge where two analyzers stem
    differently; ZERO cannot, and zero is what the diagnosis rests on."""
    tv.reindex(corpus.knowledge, paths.fulltext_index_path(corpus.program_root))
    for backend in ("fts5", "tantivy"):
        _pin(corpus, backend)
        counts, name = per_term_candidates(corpus, "zzzqqxnothing quuxnotpresent")
        assert name == backend
        assert set(counts.values()) == {0}, (backend, counts)


# ---------------------------------------------------------------------------
# the engine's two additive stats keys
# ---------------------------------------------------------------------------


def test_a_zero_result_search_names_the_terms_that_matched_nothing(corpus):
    result = engine.search(corpus, query="coordinator zzzqqxnothing", mode="fts")
    assert result["results"] == []
    assert result["stats"]["zero_result_terms"] == ["zzzqqxnothing"]
    assert result["stats"]["per_term_candidates"]["coordinator"] > 0


def test_nothing_is_computed_for_a_search_that_returned_rows(corpus):
    result = engine.search(corpus, query="coordinator", mode="fts")
    assert result["results"], "fixture query should match"
    assert "per_term_candidates" not in result["stats"]
    assert "zero_result_terms" not in result["stats"]


def test_nothing_is_computed_for_a_blank_query(corpus):
    result = engine.search(corpus, query="   ", mode="fts")
    assert "per_term_candidates" not in result["stats"]


def test_nothing_is_computed_when_the_fts_tier_was_not_requested(corpus):
    result = engine.search(corpus, query="zzzqqxnothing", mode="auto", tiers=["vector"])
    assert result["results"] == []
    assert "per_term_candidates" not in result["stats"]


def test_a_query_whose_every_term_is_absent_lists_them_all(corpus):
    result = engine.search(corpus, query="zzzqqxnothing quuxnotpresent", mode="fts")
    assert result["stats"]["zero_result_terms"] == ["zzzqqxnothing", "quuxnotpresent"]


def test_the_keys_are_additive_and_the_old_ones_untouched(corpus):
    result = engine.search(corpus, query="coordinator zzzqqxnothing", mode="fts")
    stats = result["stats"]
    for key in ("fts_candidates", "vector_scored", "fulltext_backend", "elapsed_ms"):
        assert key in stats


# ---------------------------------------------------------------------------
# the re-run next action (and only argv that parses)
# ---------------------------------------------------------------------------


def _search_env(program_root, platform_root, query, **kw):
    from trialerror.cli import query as cli_query

    class _Args:
        def __init__(self):
            self.program_root = str(program_root)
            self.platform_root = str(platform_root)
            self.query = query
            self.k = engine.DEFAULT_K
            self.mode = "auto"
            self.source_ids = None
            self.kinds = None
            self.license_tiers = None
            self.years = None
            self.unfenced = False
            self.launch_id = None
            for k, v in kw.items():
                setattr(self, k, v)

    return cli_query._run_search(_Args())


def test_the_next_action_re_runs_the_search_without_the_dead_terms(corpus, platform_root):
    program_root = corpus.program_root
    corpus.close()
    env = _search_env(program_root, platform_root, "coordinator zzzqqxnothing")
    assert env["ok"] is True
    argv = env["nextActions"][0]["argv"]
    assert argv[:3] == ["trialerror", "query", "search"]
    # The query is the last token, behind the "--" separator: everything
    # between is a flag (fix-accept V-3).
    assert argv[-2:] == ["--", "coordinator"]
    assert "zzzqqxnothing" in env["nextActions"][0]["description"]


def test_the_action_carries_the_fence_bypass_and_the_launch_it_was_asked_under(corpus, platform_root):
    """fix-accept V-2. A launch's declared slice becomes a forced doc_ids
    filter, and the per-term counts were computed inside that slice -- a
    re-run without --launch-id answers a wider question than the one that
    produced the counts, and the term it tells the operator to drop may have
    hits one document outside the slice. --unfenced is the same shape one
    level down. Both travel."""
    program_root = corpus.program_root
    corpus.close()
    env = _search_env(
        program_root, platform_root, "coordinator zzzqqxnothing",
        unfenced=True, launch_id="LNCH-01M29RPX59BD7EK9SN66BZ9GYC",
    )
    argv = env["nextActions"][0]["argv"]
    assert "--unfenced" in argv
    assert argv[argv.index("--launch-id") + 1] == "LNCH-01M29RPX59BD7EK9SN66BZ9GYC"
    assert argv[argv.index("--program-root") + 1] == str(program_root)


def test_a_surviving_term_that_starts_with_a_dash_still_parses(corpus, platform_root):
    """fix-accept V-3, the mechanical half: the promise item F10c exists to
    make is that every emitted action parses, and a positional beginning with
    "-" is read by argparse as an option unless the flags come first and a
    "--" ends them."""
    from trialerror.cli import build_parser

    program_root = corpus.program_root
    corpus.close()
    env = _search_env(program_root, platform_root, "-coordinator zzzqqxnothing", mode="fts")
    assert env["result"]["results"] == []
    argv = env["nextActions"][0]["argv"]
    parsed = build_parser().parse_args(argv[1:])
    assert parsed.query == "-coordinator"


def test_the_next_action_parses_under_the_top_level_parser(corpus, platform_root):
    from trialerror.cli import build_parser

    program_root = corpus.program_root
    corpus.close()
    env = _search_env(
        program_root, platform_root, "coordinator zzzqqxnothing", k=3, mode="fts", kinds=["paper"]
    )
    argv = env["nextActions"][0]["argv"]
    parsed = build_parser().parse_args(argv[1:])
    assert parsed.query == "coordinator"
    assert parsed.k == 3
    assert parsed.mode == "fts"
    assert parsed.kinds == ["paper"]


def test_no_action_when_every_term_is_dead(corpus, platform_root):
    program_root = corpus.program_root
    corpus.close()
    env = _search_env(program_root, platform_root, "zzzqqxnothing quuxnotpresent")
    assert env["nextActions"] == []


def test_no_action_when_the_emptiness_is_not_one_terms_fault(corpus, platform_root):
    """Every term matches something, the conjunction matches nothing: there
    is no subset to suggest, so no action is emitted."""
    program_root = corpus.program_root
    corpus.close()
    env = _search_env(program_root, platform_root, "coordinator heartbeat", mode="fts")
    if env["result"]["results"]:
        pytest.skip("fixture happens to match this conjunction")
    assert env["result"]["stats"]["zero_result_terms"] == []
    assert env["nextActions"] == []


# ---------------------------------------------------------------------------
# MCP: the keys are inherited, the schema is not touched
# ---------------------------------------------------------------------------


def test_the_mcp_search_tool_inherits_the_keys(corpus):
    from trialerror.mcp import knowledge as mcp_knowledge

    tools = mcp_knowledge.build_tools(program_root=corpus.program_root, platform_root=corpus.platform_root)
    envelope = tools["search"].handler({"query": "coordinator zzzqqxnothing", "mode": "fts"})
    assert envelope["ok"] is True
    assert envelope["result"]["stats"]["zero_result_terms"] == ["zzzqqxnothing"]


def test_the_mcp_search_schema_is_unchanged(corpus):
    """"Checked, not changed": the two keys are RESULT keys, and a caller
    must not have to ask for them with a new input parameter."""
    from trialerror.mcp import knowledge as mcp_knowledge

    tools = mcp_knowledge.build_tools(program_root=corpus.program_root, platform_root=corpus.platform_root)
    schema = tools["search"].input_schema
    assert set(schema["properties"]) == {
        "query", "k", "mode", "source_ids", "kind", "license_tier", "year", "tiers", "as_of",
    }
    assert schema["required"] == ["query"]
