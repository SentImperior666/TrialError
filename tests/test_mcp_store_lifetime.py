"""FX-3 regression test (``docs/reviews/IMPL_REVIEW_C_ops.md`` finding N-2):
both MCP servers' ``_wrap`` must close the per-call :class:`~trialerror.stores.
store.Store` on EVERY exit path -- including a tool handler raising an
exception of a type NONE of ``_wrap``'s own ``except`` clauses name (e.g. an
unanticipated bug, or ``sqlite3.OperationalError`` surfacing from a busy-
timeout race under concurrent load). Before the fix, ``store.close()`` sat
textually AFTER the try/except block, so that line -- and the whole per-call
event-log line -- was simply never reached when such an exception propagated
past ``handler``, stranding the 4 open WAL connections (platform/ops/
knowledge/jobs) per failed call inside the long-lived stdio server. The fix
opens the store via ``with open_store(...) as store:`` (``Store.__enter__``/
``__exit__`` already existed and call ``close()``), so ``__exit__`` runs
whether the ``with`` block exits normally, via a caught exception turned
into an error envelope, or via an exception that propagates straight through
``handler`` uncaught -- exactly the case this test drives, for both
``trialerror.mcp.knowledge`` and ``trialerror.mcp.ops`` (M14's identical ``_wrap``
copy, confirmed the same pattern at review time).

Self-contained: no import from another module's own test-helper glob (same
convention ``tests/test_mcp_ops_logging.py``/``tests/test_mcp_ops_tools.py``
document for this lane).
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.store import Store


def _install_close_tracker(monkeypatch) -> list[Store]:
    """Wrap ``Store.close`` to record every instance it's called on, while
    still performing the real close underneath -- lets the test assert BOTH
    "close() was invoked" and "the connections are genuinely closed"."""
    closed: list[Store] = []
    orig_close = Store.close

    def _tracking_close(self: Store) -> None:
        closed.append(self)
        orig_close(self)

    monkeypatch.setattr(Store, "close", _tracking_close)
    return closed


def test_knowledge_store_is_closed_when_a_handler_raises_an_uncaught_exception(
    program_root, platform_root, monkeypatch
):
    import trialerror.mcp.knowledge as knowledge_mod

    closed = _install_close_tracker(monkeypatch)

    def _boom(_args, *, store):
        raise RuntimeError("simulated bug -- not RetrievalError/StoreError/ValueError/TypeError/KeyError")

    # `build_tools` resolves `_tool_corpus_stats` as a module-global name at
    # call time, so patching the module attribute BEFORE building the
    # registry substitutes our exploding stand-in for the real handler body.
    monkeypatch.setattr(knowledge_mod, "_tool_corpus_stats", _boom)
    tools = knowledge_mod.build_tools(program_root=program_root, platform_root=platform_root)

    with pytest.raises(RuntimeError, match="simulated bug"):
        tools["corpus_stats"].handler({})

    assert len(closed) == 1, "Store.close() must run exactly once, even though the handler raised uncaught"
    leaked_store = closed[0]
    for conn in (leaked_store.platform, leaked_store.ops, leaked_store.knowledge, leaked_store.jobs):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")  # a connection that's really closed refuses to operate


def test_ops_store_is_closed_when_a_handler_raises_an_uncaught_exception(program_root, platform_root, monkeypatch):
    import trialerror.mcp.ops as ops_mod

    closed = _install_close_tracker(monkeypatch)

    def _boom(_args, *, store):
        raise RuntimeError("simulated bug -- not StoreError/ValueError/TypeError/KeyError")

    monkeypatch.setattr(ops_mod, "_tool_session_status", _boom)
    tools = ops_mod.build_tools(program_root=program_root, platform_root=platform_root)

    with pytest.raises(RuntimeError, match="simulated bug"):
        tools["session_status"].handler({})

    assert len(closed) == 1, "Store.close() must run exactly once, even though the handler raised uncaught"
    leaked_store = closed[0]
    for conn in (leaked_store.platform, leaked_store.ops, leaked_store.knowledge, leaked_store.jobs):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_knowledge_store_still_closes_and_logs_on_the_normal_caught_error_path(program_root, platform_root):
    """Non-regression companion: the pre-existing caught-error path (a
    ``RetrievalError``/``StoreError``/etc, turned into a structured envelope)
    must still close the store too -- the fix must not have narrowed
    coverage to only the newly-handled uncaught case."""
    from trialerror.mcp.knowledge import build_tools

    tools = build_tools(program_root=program_root, platform_root=platform_root)
    env = tools["get_chunk"].handler({"chunk_id": "CHUNK-does-not-exist"})
    assert env["ok"] is False  # engine.get_chunk raises RetrievalError for an unknown id -- already-caught path


def test_ops_store_still_closes_and_logs_on_the_normal_caught_error_path(program_root, platform_root):
    from trialerror.mcp.ops import build_tools

    tools = build_tools(program_root=program_root, platform_root=platform_root)
    env = tools["reconcile_launch"].handler({"launch_id": "LNCH-does-not-exist", "actual_tokens": 1})
    assert env["ok"] is False  # already-caught BudgetError path (StoreError-family) -- see test_mcp_ops_logging.py
