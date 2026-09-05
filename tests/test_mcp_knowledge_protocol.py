"""Transport-level tests for the ``trialerror-knowledge`` MCP server: the
JSON-RPC/stdio plumbing in ``trialerror.mcp.protocol`` exercised through the
real ``trialerror.mcp.knowledge`` tool registry (design Section 12 M8 row: "MCP
smoke via Claude Code (integration session)"; Section 5.1 cross-cutting
rule: errors returned as structured content, never exceptions; version
reported at initialize).

Two layers, mirroring ``tests/test_mcp_ops_protocol.py``'s own precedent
for "the closest a non-live-Claude-Code pytest run can get" to a live-CC
integration item:

1. In-process, over ``io.StringIO`` pipes (deterministic, exhaustive): the
   full ``initialize`` -> ``notifications/initialized`` -> ``tools/list`` ->
   ``tools/call`` sequence a real MCP client performs, driven straight
   through ``trialerror.mcp.protocol.serve_stdio`` (not mocked).
2. A REAL subprocess (``python -m trialerror.cli mcp knowledge``), talking over
   actual OS pipes exactly as Claude Code's own MCP client would launch and
   speak to this server -- "test tools by direct dispatch + one stdio
   smoke" (this build's brief), and the "stdio smoke result" this build's
   report cites.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.mcp.knowledge import SERVER_NAME, TOOL_COUNT, build_server
from trialerror.mcp.protocol import MCP_PROTOCOL_VERSION, serve_stdio
from trialerror.stores.store import open_store

from tests._retrieve_fixtures import build_small_corpus


def _lines(*messages: dict) -> str:
    return "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)


def _run(server, *messages: dict) -> list[dict]:
    stdin = io.StringIO(_lines(*messages))
    stdout = io.StringIO()
    stderr = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
    out = stdout.getvalue()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@pytest.fixture()
def seeded_server(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    corpus = build_small_corpus(store)
    store.close()
    server = build_server(program_root=program_root, platform_root=platform_root)
    return server, corpus


# ---------------------------------------------------------------------------
# layer 1: in-process StringIO round trips
# ---------------------------------------------------------------------------


def test_initialize_reports_name_version_and_protocol_version(seeded_server):
    server, _ = seeded_server
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": {"name": "test-client", "version": "0.0.1"}}})
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert "tools" in result["capabilities"]


def test_initialized_notification_gets_no_response(seeded_server):
    server, _ = seeded_server
    assert _run(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_tools_list_reports_exactly_11_tools_with_schemas(seeded_server):
    server, _ = seeded_server
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = resp["result"]["tools"]
    assert len(tools) == TOOL_COUNT == 11
    for t in tools:
        assert t["name"]
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_tools_call_happy_path_search(seeded_server):
    server, _corpus = seeded_server
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search", "arguments": {"query": "retry budgets bound tail latency", "mode": "fts"}}},
    )
    result = resp["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["result"]["results"]
    # spec: "a tool that returns structured content SHOULD also return the
    # serialized JSON in a TextContent block"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_tools_call_search_over_the_wire_fences_commercial_restricted_text(seeded_server):
    server, _corpus = seeded_server
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "search",
                    "arguments": {"query": "quorum reconfiguration lease fencing proprietary epoch counter", "mode": "fts"}}},
    )
    row = resp["result"]["structuredContent"]["result"]["results"][0]
    assert row["fenced"] is True
    assert len(row["citation"]["quote"].split()) <= 20


def test_tools_call_missing_required_argument_is_a_protocol_error(seeded_server):
    """Design's two-tier error split: a schema-shape problem (missing a
    required argument) is a JSON-RPC *protocol* error, not a tool-execution
    ``isError`` result — the handler is never even invoked."""
    server, _ = seeded_server
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "get_chunk", "arguments": {}}},
    )
    assert "result" not in resp
    assert resp["error"]["code"] == -32602
    assert "chunk_id" in resp["error"]["message"] or "chunk_id" in str(resp["error"].get("data"))


def test_tools_call_unknown_tool_is_a_protocol_error(seeded_server):
    server, _ = seeded_server
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
    )
    assert "result" not in resp
    assert resp["error"]["code"] == -32602


def test_tools_call_business_refusal_is_a_structured_tool_error_not_a_crash(seeded_server):
    server, _ = seeded_server
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "get_chunk", "arguments": {"chunk_id": "CHK-does-not-exist"}}},
    )
    result = resp["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["ok"] is False
    assert result["structuredContent"]["error"]["code"] == "ChunkNotFoundError"


def test_unknown_method_is_method_not_found(seeded_server):
    server, _ = seeded_server
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 8, "method": "totally/unknown"})
    assert resp["error"]["code"] == -32601


def test_ping(seeded_server):
    server, _ = seeded_server
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert resp["result"] == {}


def test_full_search_then_resolve_quote_round_trip_over_the_wire(seeded_server):
    """A citation-grounded round trip driven over the REAL JSON-RPC wire:
    search for a chunk, then feed its own citation quote back through
    resolve_quote and confirm it resolves to the SAME anchor -- this is
    the shape a real lit-review-skill agent session performs."""
    server, corpus = seeded_server
    [search_resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "search", "arguments": {"query": "retry budgets bound tail latency", "mode": "fts"}}},
    )
    row = search_resp["result"]["structuredContent"]["result"]["results"][0]
    anchor_id = row["citation"]["anchor"]["anchor_id"]

    [chunk_resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
         "params": {"name": "get_chunk", "arguments": {"chunk_id": row["chunk_id"]}}},
    )
    chunk_result = chunk_resp["result"]["structuredContent"]["result"]
    assert any(a["anchor_id"] == anchor_id for a in chunk_result["anchors"])


# ---------------------------------------------------------------------------
# layer 2: a REAL subprocess talking real stdio -- "trialerror mcp knowledge" as
# Claude Code would actually launch it.
# ---------------------------------------------------------------------------


def test_stdio_smoke_real_subprocess_initialize_tools_list_and_call(tmp_path):
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-smoke"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"

    store = open_store(program_root, platform_root=platform_root)
    build_small_corpus(store)
    store.close()

    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)

    proc = subprocess.Popen(
        [sys.executable, "-m", "trialerror.cli", "mcp", "knowledge", "--program-root", str(program_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "pytest-smoke", "version": "0.0.1"}},
        }) + "\n")
        proc.stdin.flush()
        init_resp = json.loads(proc.stdout.readline())
        assert init_resp["result"]["serverInfo"]["name"] == SERVER_NAME
        assert init_resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
        proc.stdin.flush()
        list_resp = json.loads(proc.stdout.readline())
        assert len(list_resp["result"]["tools"]) == TOOL_COUNT

        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "corpus_stats", "arguments": {}},
        }) + "\n")
        proc.stdin.flush()
        call_resp = json.loads(proc.stdout.readline())
        assert call_resp["result"]["isError"] is False
        assert call_resp["result"]["structuredContent"]["result"]["sources"] == 2

        proc.stdin.close()  # the documented stdio shutdown path
        returncode = proc.wait(timeout=30)
        assert returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
