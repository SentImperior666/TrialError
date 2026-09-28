"""Per-tool unit tests for ``trialerror.mcp.ops`` — each of the 3
``trialerror-ops`` tool handlers (the other nine were retired in Phase 0),
called directly (bypassing the JSON-RPC/stdio transport, which ``tests/test_mcp_ops_protocol.py`` covers separately) so
each test isolates exactly one tool's own request-shaping + landed-API-call
+ envelope-shaping logic (design Section 12 M14 row: "each tool
structured-error on bad input").

Self-contained fixture builders (a launch/session/ruling/template/artifact
seed helper) are defined locally in this file rather than imported from
another module's own test-helper file (``tests/_budget_fixtures.py``,
``tests/test_session_helpers.py``, ...) — this build's lane isolation is
``trialerror/mcp/`` + this file's own glob + ``tests/test_m14_acceptance.py``,
and 2 other builders are concurrently editing their own lanes' files.
"""

from __future__ import annotations

import pytest

from trialerror.mcp.ops import TOOL_COUNT, build_tools
from trialerror.stores import get, insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


# ---------------------------------------------------------------------------
# local, self-contained seed helpers
# ---------------------------------------------------------------------------


def seed_account_session(store, *, boot_pin_version=None):
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open",
         "boot_pin_version": boot_pin_version},
    )
    return account_id, session_id


@pytest.fixture()
def tools(program_root, platform_root):
    return build_tools(program_root=program_root, platform_root=platform_root)


def test_tool_registry_has_exactly_the_3_named_tools(tools):
    assert len(tools) == TOOL_COUNT == 3
    assert set(tools) == {"session_status", "book_launch", "read_inbox"}
    for spec in tools.values():
        assert spec.description
        assert spec.input_schema.get("type") == "object"


# ---------------------------------------------------------------------------
# 1. session_status
# ---------------------------------------------------------------------------


def test_session_status_reports_the_open_session(store, tools):
    account_id, session_id = seed_account_session(store)
    env = tools["session_status"].handler({})
    assert env["ok"] is True
    assert env["result"]["open"] is True
    assert env["result"]["session"]["session_id"] == session_id


def test_session_status_unknown_session_id_is_structured_not_a_crash(store, tools):
    env = tools["session_status"].handler({"session_id": "SESS-does-not-exist"})
    assert env["ok"] is True  # a read that legitimately found nothing is not a call FAILURE
    assert env["result"]["open"] is False
    assert "error" in env["result"]



# ---------------------------------------------------------------------------
# 3. book_launch
# ---------------------------------------------------------------------------


def test_book_launch_creates_provisional_booking_without_account_id_param(store, tools):
    account_id, session_id = seed_account_session(store)
    env = tools["book_launch"].handler(
        {"program_id": "PROG-test", "agent_kind": "tester", "model_class": "top",
         "model": "sonnet", "purpose": "fixture", "est_tokens": 500}
    )
    assert env["ok"] is True
    assert env["result"]["state"] == "PROVISIONAL"
    row = get(store, "launch", pk_column="launch_id", pk_value=env["result"]["launch_id"])
    assert row["account_id"] == account_id  # derived, never accepted as a param
    assert row["session_id"] == session_id
    assert env["meta"]["prompt_fragment"] == f"launch_id: {env['result']['launch_id']}"


def test_book_launch_non_numeric_est_tokens_is_structured_error(store, tools):
    """A malformed argument TYPE (not just a business refusal) still comes
    back structured -- ``trialerror.mcp.ops._wrap``'s own bad_input catch, one
    layer below ``trialerror.mcp.protocol``'s required-field pre-check."""
    seed_account_session(store)
    env = tools["book_launch"].handler(
        {"program_id": "PROG-test", "agent_kind": "tester", "model_class": "top",
         "model": "sonnet", "purpose": "fixture", "est_tokens": "not-a-number"}
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_input"


def test_book_launch_assign_ids_as_a_bare_string_is_bad_input_and_books_nothing(store, tools):
    """Fix pass B-1. ``"ASGN-1"`` used to be comprehended into one id per
    character; the eight bogus ids then raised AFTER the launch row was
    written, leaving a dangling PROVISIONAL booking."""
    seed_account_session(store)
    before = store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"]
    env = tools["book_launch"].handler(
        {"program_id": "PROG-test", "agent_kind": "lens", "model_class": "top",
         "model": "sonnet", "purpose": "fixture", "est_tokens": 500,
         "assign_ids": "ASGN-1"}
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_input"
    assert store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"] == before


def test_book_launch_unknown_assign_id_refuses_and_books_nothing(store, tools):
    seed_account_session(store)
    before = store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"]
    env = tools["book_launch"].handler(
        {"program_id": "PROG-test", "agent_kind": "lens", "model_class": "top",
         "model": "sonnet", "purpose": "fixture", "est_tokens": 500,
         "assign_ids": ["ASGN-does-not-exist"]}
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "unknown_assignment"
    assert store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"] == before


def test_book_launch_no_open_session_is_structured_error(store, tools):
    env = tools["book_launch"].handler(
        {"program_id": "PROG-test", "agent_kind": "tester", "model_class": "top",
         "model": "sonnet", "purpose": "fixture", "est_tokens": 500}
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "no_open_session"





# ---------------------------------------------------------------------------
# 7. read_inbox
# ---------------------------------------------------------------------------


def test_read_inbox_returns_and_marks_read_by_default(store, tools):
    insert(store, "inbox_item", {"item_id": new_id("INBX"), "ts": now(), "body": "hi", "source": "user"})
    env = tools["read_inbox"].handler({})
    assert env["ok"] is True
    assert env["result"]["count"] == 1
    # second read finds nothing unread left
    env2 = tools["read_inbox"].handler({})
    assert env2["result"]["count"] == 0


def test_read_inbox_mark_read_false_peeks_only(store, tools):
    insert(store, "inbox_item", {"item_id": new_id("INBX"), "ts": now(), "body": "hi", "source": "user"})
    env = tools["read_inbox"].handler({"mark_read": False})
    assert env["result"]["count"] == 1
    env2 = tools["read_inbox"].handler({"mark_read": False})
    assert env2["result"]["count"] == 1  # still unread






