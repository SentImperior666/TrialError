"""``trialerror.mcp`` — MCP server package. Design Section 5.1's two servers:
``trialerror/mcp/ops.py`` (the ``trialerror-ops`` Tool Orchestrator, 12 tools, design
Section 12 M14 row) and ``trialerror/mcp/knowledge.py`` (the ``trialerror-knowledge``
Resource Gateway, 11 tools, design Section 12 M8 row) — both built on the
shared standalone stdio JSON-RPC transport in ``trialerror/mcp/protocol.py``
(see that module's docstring for the dedup note between the two lanes)."""

from __future__ import annotations
