"""Minimal standalone MCP stdio transport + JSON-RPC 2.0 plumbing.

TRIALERROR-DEV-NOTE (M14, standalone plumbing -- design Section 12 M14 row: "mirror
whatever M8's builder does for the server layer IF its shared plumbing has
landed by the time you build ...; otherwise implement standalone stdio
serving for your server and note the dedup opportunity for M15"). Checked at
build time (``git log --all`` + working-tree scan for ``trialerror/mcp/``,
``trialerror/retrieve/``): M8 (``trialerror-knowledge``, the Resource Gateway) has NOT
landed. There is no shared transport module to mirror, so this file IS that
standalone implementation.

It is deliberately kept generic — nothing in this module mentions
``trialerror-ops``-specific tool names or business logic — so that when M8 lands
it can ``from trialerror.mcp.protocol import ToolSpec, ToolServer, serve_stdio``
directly instead of re-implementing the same transport a second time.
**Dedup note for M15/the integration session:** point M8's
``trialerror/mcp/knowledge.py`` at this module rather than duplicating it; once
both servers exist, consider whether this file's name should change (it is
intentionally NOT named ``ops_protocol.py`` or similar, precisely so it reads
as shared infrastructure rather than one server's private helper).

Implements exactly the subset of the MCP spec a stdio tool-only server needs
(verified against the published spec at build time, 2025-06-18 revision):

- **Transport**: newline-delimited JSON-RPC 2.0 messages over stdin/stdout,
  one message per line, no embedded newlines
  (https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
  — "Messages are delimited by newlines, and MUST NOT contain embedded
  newlines... The server MUST NOT write anything to its stdout that is not a
  valid MCP message" — this module never ``print()``s; diagnostics go to
  stderr only).
- **Lifecycle**: ``initialize`` request/response + ``notifications/initialized``
  (https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle).
  Shutdown has no dedicated message — the client closes stdin and the server
  exits when its ``readline()`` hits EOF, exactly as the spec's stdio
  shutdown section describes.
- **Tools**: ``tools/list`` and ``tools/call``
  (https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
  No ``resources``/``prompts``/``sampling``/``roots`` capabilities — both
  ``trialerror-knowledge`` and ``trialerror-ops`` are tools-only servers per design
  Section 5.1's tool tables.
- **Errors**: per the spec's own two-tier split — an unknown tool name or a
  request missing a schema-``required`` argument is a *protocol* error
  (JSON-RPC error object, code -32602); a tool that runs but fails on its
  own terms (refused write, illegal transition, ...) is a *tool execution*
  error (``CallToolResult`` with ``isError: true`` — this module wraps every
  ``trialerror`` tool handler's own structured envelope, design Section 5.1
  cross-cutting rule: "errors returned as structured content ... never
  exceptions"). A handler that raises unexpectedly (a real bug, not a
  refusal) is ALSO caught here and turned into an ``isError: true`` result
  rather than a crash or a raw traceback on stdout — belt-and-suspenders on
  top of each handler's own try/except, so a bug in one tool can never take
  the whole server process down mid-session.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence, TextIO

from trialerror.util.envelope import to_json_line

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "JsonRpcErrorCode",
    "ToolSpec",
    "ToolServer",
    "envelope_to_call_result",
    "serve_stdio",
]

#: The MCP wire-protocol version this server negotiates (spec revision this
#: module was implemented against — see module docstring). Distinct from
#: ``trialerror.util.envelope.PROTOCOL_VERSION`` (the AgentEnvelope shape's OWN
#: version, carried inside every tool result's structured content) — the two
#: numbers answer different questions and are both reported, never confused
#: for one another.
MCP_PROTOCOL_VERSION = "2025-06-18"


class JsonRpcErrorCode:
    """Standard JSON-RPC 2.0 error codes this server emits."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool. ``handler`` takes the parsed ``arguments`` dict (already
    checked against ``input_schema``'s ``required`` list by
    :func:`serve_stdio`) and returns a ``trialerror.util.envelope`` dict
    (``{ok, command, protocolVersion, result|error, nextActions, meta}``) —
    the SAME shape every ``trialerror`` CLI command emits, reused here rather than
    inventing a second result convention (design Section 5.1 cross-cutting
    rule: structured errors on every surface)."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any]], Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }


@dataclass
class ToolServer:
    """A tools-only MCP server: name/version reported at ``initialize``
    (design Section 5.1: "version reported at initialize"), plus the tool
    registry ``tools/list``/``tools/call`` serve."""

    name: str
    version: str
    tools: Mapping[str, ToolSpec]
    instructions: str | None = None
    _tools_capability: dict[str, Any] = field(default_factory=lambda: {"listChanged": False})


def envelope_to_call_result(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a ``trialerror`` envelope as an MCP ``CallToolResult``: the envelope's
    compact JSON line as the required ``text`` content block (spec: "a tool
    that returns structured content SHOULD also return the serialized JSON
    in a TextContent block"), the envelope itself as ``structuredContent``,
    and ``isError`` set from the envelope's own ``ok`` flag — never a raised
    exception, exactly the cross-cutting rule this module's docstring cites."""
    return {
        "content": [{"type": "text", "text": to_json_line(envelope)}],
        "structuredContent": dict(envelope),
        "isError": not bool(envelope.get("ok", False)),
    }


def _missing_required(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    required: Sequence[str] = schema.get("required", []) or []
    return [f for f in required if f not in arguments]


def _write(stream: TextIO, obj: Mapping[str, Any]) -> None:
    """Write one JSON-RPC message as a single line (no embedded newlines —
    ``to_json_line``/``json.dumps`` never emit raw newlines inside a JSON
    string's escaped form) and flush immediately: a pipe reader blocks on
    exactly this line until it arrives."""
    stream.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")
    stream.flush()


def _rpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _rpc_result(id_: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": dict(result)}


def _handle_initialize(server: ToolServer, params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": dict(server._tools_capability)},
        "serverInfo": {"name": server.name, "version": server.version},
        **({"instructions": server.instructions} if server.instructions else {}),
    }


def _handle_tools_list(server: ToolServer, params: Mapping[str, Any]) -> dict[str, Any]:
    return {"tools": [spec.to_dict() for spec in server.tools.values()]}


def _handle_tools_call(
    server: ToolServer, params: Mapping[str, Any], *, stderr: TextIO
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Returns ``(result, error)`` — exactly one is not ``None``. ``error``
    is a JSON-RPC protocol-level error payload (code+message[+data]);
    ``result`` is a ``CallToolResult`` dict (possibly ``isError: true`` —
    that is a TOOL execution error, the other of the spec's two error
    tiers, see module docstring)."""
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or name not in server.tools:
        return None, {"code": JsonRpcErrorCode.INVALID_PARAMS, "message": f"Unknown tool: {name!r}"}
    spec = server.tools[name]
    if not isinstance(arguments, Mapping):
        return None, {
            "code": JsonRpcErrorCode.INVALID_PARAMS,
            "message": f"tool {name!r}: 'arguments' must be a JSON object",
        }
    missing = _missing_required(spec.input_schema, arguments)
    if missing:
        return None, {
            "code": JsonRpcErrorCode.INVALID_PARAMS,
            "message": f"tool {name!r}: missing required argument(s) {missing!r}",
            "data": {"missing": missing},
        }
    try:
        envelope = spec.handler(arguments)
    except Exception as exc:  # belt-and-suspenders: a handler bug must not kill the server (see module docstring)
        print(f"trialerror-ops: tool {name!r} raised {type(exc).__name__}: {exc}", file=stderr, flush=True)
        envelope = {
            "ok": False,
            "command": name,
            "error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
        }
    return envelope_to_call_result(envelope), None


def serve_stdio(
    server: ToolServer,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Run ``server`` over stdio until stdin hits EOF (the spec's stdio
    shutdown path: "the client SHOULD initiate shutdown by ... closing the
    input stream to the child process"). Blocking; call this last from a CLI
    handler (``trialerror mcp ops``)."""
    in_ = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    while True:
        line = in_.readline()
        if line == "":
            break  # EOF: client closed stdin -- clean shutdown, no message to send back
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(out, _rpc_error(None, JsonRpcErrorCode.PARSE_ERROR, f"invalid JSON: {exc}"))
            continue

        if not isinstance(msg, Mapping) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            _write(out, _rpc_error(msg.get("id") if isinstance(msg, Mapping) else None,
                                    JsonRpcErrorCode.INVALID_REQUEST, "not a valid JSON-RPC 2.0 request"))
            continue

        method = msg["method"]
        params = msg.get("params") or {}
        has_id = "id" in msg  # requests carry an id; notifications do not and get no response
        id_ = msg.get("id")

        if method == "notifications/initialized":
            continue  # notification: no response, nothing to do (handshake is complete on our side already)
        if method == "notifications/cancelled":
            continue  # nothing to cancel -- every handler here runs to completion synchronously

        if method == "ping":
            result: dict[str, Any] = {}
        elif method == "initialize":
            result = _handle_initialize(server, params)
        elif method == "tools/list":
            result = _handle_tools_list(server, params)
        elif method == "tools/call":
            call_result, rpc_err = _handle_tools_call(server, params, stderr=err)
            if rpc_err is not None:
                if has_id:
                    _write(out, _rpc_error(id_, rpc_err["code"], rpc_err["message"], rpc_err.get("data")))
                continue
            result = call_result  # type: ignore[assignment]
        else:
            if has_id:
                _write(out, _rpc_error(id_, JsonRpcErrorCode.METHOD_NOT_FOUND, f"unknown method: {method!r}"))
            continue

        if has_id:
            _write(out, _rpc_result(id_, result))
        # a notification-shaped request (no id) to a request-only method gets no reply, per spec
