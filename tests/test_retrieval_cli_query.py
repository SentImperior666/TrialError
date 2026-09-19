"""Tests for the ``trialerror query`` CLI group (``trialerror/cli/query.py``) --
design Section 5.2's "search, quote, similar, stats | same engine as MCP",
including the ``--unfenced`` non-agent escape hatch (design Section 7)."""

from __future__ import annotations

import trialerror.cli as cli
from trialerror.stores.store import open_store

from tests._retrieve_fixtures import build_small_corpus


def _build_parser():
    return cli.build_parser()


def test_query_group_is_auto_discovered():
    parser = _build_parser()
    help_text = parser.format_help()
    assert "query" in help_text


def test_query_search_happy_path(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    build_small_corpus(store)
    store.close()

    parser = _build_parser()
    args = parser.parse_args([
        "query", "search", "retry budgets bound tail latency", "--mode", "fts",
        "--program-root", str(program_root), "--platform-root", str(platform_root),
    ])
    env = args.handler(args)
    assert env["ok"] is True
    assert env["result"]["results"]
    assert env["result"]["results"][0]["fenced"] is False


def test_query_search_no_program_root_is_a_clean_error(monkeypatch, tmp_path):
    """No ``--program-root`` given, and no ``trialerror.toml`` discoverable
    upward from CWD -- the CLI's "no_program_root" refusal, not a store
    auto-creating itself somewhere unexpected. Chdir into an isolated empty
    tmp_path (the same pattern test_ingest_cli.py / test_jobs_cli.py use for
    this exact scenario) rather than relying on the repo root itself having
    no discoverable trialerror.toml -- the interim program config that now
    lives at the repo root (the migration-guide notes (internal, not in this export)'s boot ritual runs the
    CLI from there with no --program-root flag) makes that assumption false."""
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["query", "search", "x"])
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_program_root"


def test_query_search_unfenced_bypasses_the_fence_and_logs_an_event(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_small_corpus(store)
    store.close()

    parser = _build_parser()
    args = parser.parse_args([
        "query", "search", "quorum reconfiguration lease fencing proprietary epoch counter", "--mode", "fts",
        "--unfenced", "--launch-id", corpus["launch_id"],
        "--program-root", str(program_root), "--platform-root", str(platform_root),
    ])
    env = args.handler(args)
    assert env["ok"] is True
    assert env["result"]["results"][0]["fenced"] is False

    store2 = open_store(program_root, platform_root=platform_root)
    try:
        events = list(store2.ops.execute("SELECT * FROM event WHERE type = 'retrieval_unfenced_bypass'"))
        assert len(events) == 1
    finally:
        store2.close()


def test_query_search_without_unfenced_still_fences(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    build_small_corpus(store)
    store.close()

    parser = _build_parser()
    args = parser.parse_args([
        "query", "search", "quorum reconfiguration lease fencing proprietary epoch counter", "--mode", "fts",
        "--program-root", str(program_root), "--platform-root", str(platform_root),
    ])
    env = args.handler(args)
    assert env["result"]["results"][0]["fenced"] is True


def test_query_quote_happy_path(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_small_corpus(store)
    anchor = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)).fetchone())
    store.close()

    parser = _build_parser()
    args = parser.parse_args([
        "query", "quote", anchor["quote_text"],
        "--program-root", str(program_root), "--platform-root", str(platform_root),
    ])
    env = args.handler(args)
    assert env["ok"] is True
    assert env["result"]["match_type"] == "exact"


def test_query_quote_not_found_is_a_clean_error(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    build_small_corpus(store)
    store.close()

    parser = _build_parser()
    args = parser.parse_args([
        "query", "quote", "this text is nowhere in the corpus at all",
        "--program-root", str(program_root), "--platform-root", str(platform_root),
    ])
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "not_found"


def test_query_similar_happy_path(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_small_corpus(store)
    store.close()

    parser = _build_parser()
    args = parser.parse_args([
        "query", "similar", "--id", corpus["open_chunk_ids"][0],
        "--program-root", str(program_root), "--platform-root", str(platform_root),
    ])
    env = args.handler(args)
    assert env["ok"] is True
    assert "results" in env["result"]


def test_query_stats_happy_path(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    build_small_corpus(store)
    store.close()

    parser = _build_parser()
    args = parser.parse_args(["query", "stats", "--program-root", str(program_root), "--platform-root", str(platform_root)])
    env = args.handler(args)
    assert env["ok"] is True
    assert env["result"]["sources"] == 2


def test_query_no_action_is_a_clean_error():
    parser = _build_parser()
    args = parser.parse_args(["query"])
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_action"
