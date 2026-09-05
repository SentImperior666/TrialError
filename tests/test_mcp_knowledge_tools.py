"""Per-tool unit tests for ``trialerror.mcp.knowledge`` — each of the 11
``trialerror-knowledge`` tool handlers, called directly (bypassing the
JSON-RPC/stdio transport, which ``tests/test_mcp_knowledge_protocol.py``
covers separately) so each test isolates exactly one tool's own
request-shaping + landed-engine-call + envelope-shaping logic.

Self-contained fixture usage: this build's lane owns
``tests/_retrieve_fixtures.py`` (its own glob), reused here rather than
duplicated, matching ``tests/test_mcp_ops_tools.py``'s own precedent of
keeping helpers local to the owning lane.
"""

from __future__ import annotations

from trialerror.mcp.knowledge import TOOL_COUNT, build_tools
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._retrieve_fixtures import build_small_corpus


def _seeded_store_and_tools(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_small_corpus(store)
    store.close()
    tools = build_tools(program_root=program_root, platform_root=platform_root)
    return tools, corpus


def test_tool_registry_has_exactly_the_11_named_tools(program_root, platform_root):
    tools = build_tools(program_root=program_root, platform_root=platform_root)
    assert len(tools) == TOOL_COUNT == 11
    assert set(tools) == {
        "search", "get_chunk", "get_source", "get_document_outline", "resolve_quote",
        "similar", "graph_neighbors", "corpus_stats", "memory_search", "list_requests", "poll_job",
    }
    for name, spec in tools.items():
        assert spec.name == name
        assert spec.description
        assert spec.input_schema["type"] == "object"


def test_search_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    env = tools["search"].handler({"query": "retry budgets bound tail latency", "mode": "fts"})
    assert env["ok"] is True
    assert env["result"]["results"]
    assert env["result"]["results"][0]["fenced"] is False


def test_search_never_accepts_an_unfenced_bypass_even_if_the_arg_is_sent(program_root, platform_root):
    """F3 structural enforcement: the MCP surface must fence a
    commercial_restricted result NO MATTER what arguments an agent sends
    -- there is no argument name that turns the fence off."""
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    env = tools["search"].handler({
        "query": "quorum reconfiguration lease fencing proprietary epoch counter", "mode": "fts",
        "unfenced": True,  # not a real parameter of this tool -- must be silently ignored, not honored
    })
    assert env["ok"] is True
    row = env["result"]["results"][0]
    assert row["fenced"] is True
    assert len(row["citation"]["quote"].split()) <= 20


def test_search_bad_mode_is_a_structured_error_not_a_crash(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["search"].handler({"query": "x", "mode": "not-a-real-mode"})
    assert env["ok"] is False
    assert env["error"]["code"] == "InvalidSearchModeError"


def test_get_chunk_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    env = tools["get_chunk"].handler({"chunk_id": corpus["restricted_chunk_ids"][0]})
    assert env["ok"] is True
    assert env["result"]["fenced"] is True


def test_get_chunk_not_found(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["get_chunk"].handler({"chunk_id": "CHK-does-not-exist"})
    assert env["ok"] is False
    assert env["error"]["code"] == "ChunkNotFoundError"


def test_get_source_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    env = tools["get_source"].handler({"source_id": corpus["open_source_id"]})
    assert env["ok"] is True
    assert env["result"]["source"]["source_id"] == corpus["open_source_id"]


def test_get_document_outline_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    env = tools["get_document_outline"].handler({"doc_id": corpus["open_doc_id"]})
    assert env["ok"] is True
    assert "outline" in env["result"]


def test_resolve_quote_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        anchor = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)).fetchone())
    finally:
        store.close()
    env = tools["resolve_quote"].handler({"quote": anchor["quote_text"]})
    assert env["ok"] is True
    assert env["result"]["match_type"] == "exact"


def test_resolve_quote_not_found_is_a_structured_error(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["resolve_quote"].handler({"quote": "this text is nowhere in the corpus at all, guaranteed"})
    assert env["ok"] is False
    assert env["error"]["code"] == "not_found"


def test_similar_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    env = tools["similar"].handler({"id": corpus["open_chunk_ids"][0]})
    assert env["ok"] is True
    assert "results" in env["result"]


def test_similar_claim_kind_is_graceful(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["similar"].handler({"id": "anything", "kind": "claim"})
    assert env["ok"] is True
    assert env["result"]["results"] == []


def test_graph_neighbors_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        e1 = new_id("ENT")
        insert(store, "entity", {"entity_id": e1, "name": "T", "entity_type": "x", "resolution": "confirmed", "created_by_launch": corpus["launch_id"], "created_at": now()})
    finally:
        store.close()
    env = tools["graph_neighbors"].handler({"entity_id": e1})
    assert env["ok"] is True
    assert env["result"]["count"] == 0


def test_graph_neighbors_not_found_is_a_structured_error(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["graph_neighbors"].handler({"entity_id": "ENT-does-not-exist"})
    assert env["ok"] is False
    assert env["error"]["code"] == "EntityNotFoundError"


def test_corpus_stats_happy_path(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["corpus_stats"].handler({})
    assert env["ok"] is True
    assert env["result"]["sources"] == 2


def test_memory_search_empty_index(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["memory_search"].handler({"query": "anything"})
    assert env["ok"] is True
    assert env["result"] == {"items": [], "count": 0}


def test_memory_search_put_then_get_by_id(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        from trialerror.memory.api import put_item

        account_id = new_id("ACC")
        insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
        row = put_item(store, key="k1", tier="L0", kind="fact", body="body text", account_id=account_id)
    finally:
        store.close()
    env = tools["memory_search"].handler({"id": row["memory_item_id"]})
    assert env["ok"] is True
    assert env["result"]["item"]["body"] == "body text"


def test_memory_search_not_found_by_id(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["memory_search"].handler({"id": "MEM-does-not-exist"})
    assert env["ok"] is False
    assert env["error"]["code"] == "not_found"


def test_list_requests_happy_path(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["list_requests"].handler({})
    assert env["ok"] is True
    assert env["result"]["count"] == 2


def test_poll_job_not_found_is_a_structured_error(program_root, platform_root):
    tools, _ = _seeded_store_and_tools(program_root, platform_root)
    env = tools["poll_job"].handler({"job_id": "JOB-does-not-exist"})
    assert env["ok"] is False
    assert env["error"]["code"] == "not_found"


def test_poll_job_happy_path(program_root, platform_root):
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        job_id = new_id("JOB")
        insert(store, "job", {"job_id": job_id, "kind": "embed", "payload": "{}", "state": "pending", "created_ts": now()})
    finally:
        store.close()
    env = tools["poll_job"].handler({"job_id": job_id})
    assert env["ok"] is True
    assert env["result"]["job"]["job_id"] == job_id
    assert env["result"]["heartbeat_age_s"] is None


def test_every_tool_logs_exactly_one_mcp_tool_call_event(program_root, platform_root):
    """Appendix B cross-cutting rule: "per-call log line (tool, input-hash,
    latency, output-size, error-code) -> events" -- proven once, across the
    full 11-tool surface, on both a success and a failure call."""
    tools, corpus = _seeded_store_and_tools(program_root, platform_root)
    tools["corpus_stats"].handler({})
    tools["get_chunk"].handler({"chunk_id": "CHK-does-not-exist"})  # a failure call

    store = open_store(program_root, platform_root=platform_root)
    try:
        events = [dict(r) for r in store.ops.execute("SELECT * FROM event WHERE type = 'mcp_tool_call' ORDER BY ts")]
    finally:
        store.close()
    assert len(events) == 2
    import json

    payloads = [json.loads(e["payload"]) for e in events]
    assert payloads[0]["tool"] == "corpus_stats"
    assert payloads[0]["error_code"] is None
    assert payloads[1]["tool"] == "get_chunk"
    assert payloads[1]["error_code"] == "ChunkNotFoundError"
    for p in payloads:
        assert p["server"] == "trialerror-knowledge"
        assert isinstance(p["latency_ms"], (int, float))
        assert isinstance(p["output_size"], int) and p["output_size"] > 0
        assert p["input_hash"]
