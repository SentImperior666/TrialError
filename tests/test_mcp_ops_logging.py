"""M15 addition (INTEGRATION_NOTES.md item 13 / M8 flag): per-call event-row
logging parity for ``trialerror.mcp.ops`` -- ports ``trialerror.mcp.knowledge``'s
``_input_hash``/``_log_call`` pattern into ``trialerror-ops``'s own ``_wrap``.
Mirrors ``tests/test_mcp_knowledge_tools.py::test_every_tool_logs_exactly_one_mcp_tool_call_event``
for the ops server.

Self-contained fixture builder (a minimal account/session seed), same
convention ``tests/test_mcp_ops_tools.py`` documents for this lane: no
import from another module's own test-helper glob.

This is a NEW file, not a modification of the 43 pre-existing M14 tests in
``tests/test_mcp_ops_tools.py``/``tests/test_mcp_ops_protocol.py``/
``tests/test_m14_acceptance.py`` (all still pass unmodified -- see those
files, re-run green by this same build).
"""

from __future__ import annotations

import json

from trialerror.mcp.ops import build_tools
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _seed_open_session(store) -> tuple[str, str]:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    return account_id, session_id


def test_every_tool_logs_exactly_one_mcp_tool_call_event(program_root, platform_root):
    """Appendix B cross-cutting rule: "per-call log line (tool, input-hash,
    latency, output-size, error-code) -> events" -- proven on both a
    success and a failure call, same bar M8's own test holds
    ``trialerror-knowledge`` to."""
    store = open_store(program_root, platform_root=platform_root)
    try:
        _seed_open_session(store)
    finally:
        store.close()

    tools = build_tools(program_root=program_root, platform_root=platform_root)
    tools["session_status"].handler({})  # a success call (informational "no session_id given" path)
    tools["reconcile_launch"].handler({"launch_id": "LNCH-does-not-exist", "actual_tokens": 1})  # a failure call

    store = open_store(program_root, platform_root=platform_root)
    try:
        events = [dict(r) for r in store.ops.execute("SELECT * FROM event WHERE type = 'mcp_tool_call' ORDER BY ts")]
    finally:
        store.close()

    assert len(events) == 2
    payloads = [json.loads(e["payload"]) for e in events]
    assert payloads[0]["tool"] == "session_status"
    assert payloads[0]["error_code"] is None
    assert payloads[1]["tool"] == "reconcile_launch"
    assert payloads[1]["error_code"] == "reconcile_refused"
    for p in payloads:
        assert p["server"] == "trialerror-ops"
        assert isinstance(p["latency_ms"], (int, float))
        assert isinstance(p["output_size"], int) and p["output_size"] > 0
        assert p["input_hash"]


def test_input_hash_is_stable_for_equal_arguments_regardless_of_key_order():
    """``_input_hash`` sorts keys before hashing -- same substance proven
    for ``trialerror.mcp.knowledge`` (implicit in its own equality-of-behavior);
    exercised directly here since ``trialerror.mcp.ops`` now has its own copy."""
    from trialerror.mcp.ops import _input_hash

    assert _input_hash({"a": 1, "b": 2}) == _input_hash({"b": 2, "a": 1})
    assert _input_hash({"a": 1}) != _input_hash({"a": 2})


def test_log_call_failure_never_breaks_the_tool_call(program_root, platform_root, monkeypatch):
    """Best-effort logging: even if ``append_event`` explodes, the tool's
    own envelope is still returned unharmed (mirrors the module docstring's
    "a logging failure never fails the tool call itself")."""
    import trialerror.mcp.ops as ops_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("logging backend is down")

    monkeypatch.setattr(ops_mod, "append_event_api", _boom)

    tools = build_tools(program_root=program_root, platform_root=platform_root)
    env = tools["session_status"].handler({})
    assert env["ok"] is True
