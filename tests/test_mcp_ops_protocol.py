"""Transport-level tests for the ``trialerror-ops`` MCP server: the JSON-RPC/
stdio plumbing in ``trialerror.mcp.protocol`` exercised through the real
``trialerror.mcp.ops`` tool registry (design Section 12 M14 row: "live Claude
Code smoke: book->spawn->reconcile round trip"; Section 5.1 cross-cutting
rule: errors returned as structured content, never exceptions; version
reported at initialize).

Two layers, mirroring ``tests/test_spawn_gate_hook.py``'s own precedent for
the "closest a non-live-Claude-Code pytest run can get" to a live-CC
integration item:

1. In-process, over ``io.StringIO`` pipes (deterministic, exhaustive): the
   full ``initialize`` -> ``notifications/initialized`` -> ``tools/list`` ->
   ``tools/call`` sequence a real MCP client performs, driven straight
   through ``trialerror.mcp.protocol.serve_stdio`` (not mocked).
2. A REAL subprocess (``python -m trialerror.cli mcp ops``), talking over actual
   OS pipes exactly as Claude Code's own MCP client would launch and speak
   to this server -- the "stdio smoke result" this build's report cites.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.mcp.ops import SERVER_NAME, TOOL_COUNT, build_server
from trialerror.mcp.protocol import MCP_PROTOCOL_VERSION, serve_stdio
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _lines(*messages: dict) -> str:
    return "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)


def _run(server, *messages: dict) -> list[dict]:
    """Feed ``messages`` through :func:`serve_stdio` over in-memory
    StringIO pipes and return every response line, parsed."""
    stdin = io.StringIO(_lines(*messages))
    stdout = io.StringIO()
    stderr = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
    out = stdout.getvalue()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@pytest.fixture()
def server(program_root, platform_root):
    return build_server(program_root=program_root, platform_root=platform_root)


# ---------------------------------------------------------------------------
# layer 1: in-process StringIO round trips
# ---------------------------------------------------------------------------


def test_initialize_reports_name_version_and_protocol_version(server):
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": {"name": "test-client", "version": "0.0.1"}}})
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert "tools" in result["capabilities"]


def test_initialized_notification_gets_no_response(server):
    responses = _run(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert responses == []


def test_tools_list_reports_exactly_12_tools_with_schemas(server):
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = resp["result"]["tools"]
    assert len(tools) == TOOL_COUNT == 12
    for t in tools:
        assert t["name"]
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_tools_call_happy_path_session_status(store, server):
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})

    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "session_status", "arguments": {}}},
    )
    result = resp["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["result"]["session"]["session_id"] == session_id
    # spec: "a tool that returns structured content SHOULD also return the
    # serialized JSON in a TextContent block"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_tools_call_missing_required_argument_is_a_protocol_error(server):
    """Design's two-tier error split: a schema-shape problem (missing a
    required argument) is a JSON-RPC *protocol* error, not a tool-execution
    ``isError`` result — the handler is never even invoked."""
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "book_launch", "arguments": {}}},
    )
    assert "result" not in resp
    assert resp["error"]["code"] == -32602
    assert "program_id" in resp["error"]["message"] or "program_id" in str(resp["error"].get("data"))


def test_tools_call_unknown_tool_is_a_protocol_error(server):
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
    )
    assert "result" not in resp
    assert resp["error"]["code"] == -32602


def test_tools_call_business_refusal_is_a_structured_tool_error_not_a_crash(server):
    """A schema-valid call that the underlying subsystem refuses (unknown
    launch_id) comes back as ``isError: true`` structured content -- never
    a JSON-RPC protocol error, never a raised exception reaching the
    transport."""
    [resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "reconcile_launch", "arguments": {"launch_id": "LNCH-nope", "actual_tokens": 1}}},
    )
    result = resp["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["ok"] is False
    assert result["structuredContent"]["error"]["code"] == "reconcile_refused"


def test_unknown_method_is_method_not_found(server):
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 7, "method": "totally/unknown"})
    assert resp["error"]["code"] == -32601


def test_ping(server):
    [resp] = _run(server, {"jsonrpc": "2.0", "id": 8, "method": "ping"})
    assert resp["result"] == {}


def test_full_book_spawn_reconcile_round_trip_over_the_wire(store, server):
    """The closest an in-process pytest run can get to design Section 12's
    M14 acceptance line "live Claude Code smoke: book->spawn->reconcile
    round trip": book_launch and reconcile_launch travel the REAL
    initialize/tools-call JSON-RPC wire (this file's whole point); the
    "spawn" leg is what a live PreToolUse hook does between those two MCP
    calls -- ``trialerror.budget.gate.evaluate_spawn_for_open_session`` IS that
    hook's own decision function (``plugin/hooks/spawn_gate.py`` is a thin
    stdin/exit-code shell over it, per M3's build), so calling it directly
    here exercises the identical atomic PROVISIONAL->RUNNING claim a real
    spawn would, without requiring an actual live Claude Code session."""
    from trialerror.budget.gate import evaluate_spawn_for_open_session
    from trialerror.stores import get

    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})

    [book_resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "book_launch",
                    "arguments": {"program_id": "PROG-test", "agent_kind": "tester", "model_class": "top",
                                  "model": "sonnet", "purpose": "fixture", "est_tokens": 500}}},
    )
    book_env = book_resp["result"]["structuredContent"]
    assert book_env["ok"] is True
    launch_id = book_env["result"]["launch_id"]
    assert get(store, "launch", pk_column="launch_id", pk_value=launch_id)["state"] == "PROVISIONAL"

    gate_result = evaluate_spawn_for_open_session(store, f"launch_id: {launch_id}")
    assert gate_result.allowed is True
    assert get(store, "launch", pk_column="launch_id", pk_value=launch_id)["state"] == "RUNNING"

    [reconcile_resp] = _run(
        server,
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
         "params": {"name": "reconcile_launch", "arguments": {"launch_id": launch_id, "actual_tokens": 777}}},
    )
    reconcile_env = reconcile_resp["result"]["structuredContent"]
    assert reconcile_env["ok"] is True
    final = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    assert final["state"] == "RECONCILED"
    assert final["actual_tokens"] == 777


# ---------------------------------------------------------------------------
# layer 2: a REAL subprocess talking real stdio -- "trialerror mcp ops" as
# Claude Code would actually launch it.
# ---------------------------------------------------------------------------


def test_stdio_smoke_real_subprocess_initialize_and_tools_list(tmp_path):
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-smoke"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"

    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)

    proc = subprocess.Popen(
        [sys.executable, "-m", "trialerror.cli", "mcp", "ops", "--program-root", str(program_root)],
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
        init_line = proc.stdout.readline()
        init_resp = json.loads(init_line)
        assert init_resp["result"]["serverInfo"]["name"] == SERVER_NAME
        assert init_resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
        proc.stdin.flush()
        list_resp = json.loads(proc.stdout.readline())
        assert len(list_resp["result"]["tools"]) == TOOL_COUNT

        proc.stdin.close()  # the documented stdio shutdown path
        returncode = proc.wait(timeout=30)
        assert returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
