"""Per-tool unit tests for ``trialerror.mcp.ops`` — each of the 12
``trialerror-ops`` tool handlers, called directly (bypassing the JSON-RPC/stdio
transport, which ``tests/test_mcp_ops_protocol.py`` covers separately) so
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

import json

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


def seed_launch(store, *, account_id, session_id, state="PROVISIONAL", program_id="PROG-test"):
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": program_id,
            "session_id": session_id, "agent_kind": "mcp-ops-fixture", "model_class": "top",
            "model": "sonnet", "purpose": "fixture", "est_tokens": 100, "booked_ts": now(), "state": state,
        },
    )
    return launch_id


def seed_pool(store, *, account_id, model_class="top", cap_tokens=1_000_000):
    pool_id = new_id("POOL")
    insert(
        store, "budget_pool",
        {"pool_id": pool_id, "account_id": account_id, "model_class": model_class, "period": "weekly",
         "period_start": now(), "cap_tokens": cap_tokens, "updated_ts": now()},
    )
    return pool_id


def seed_ruling_and_digest(store, ruling_id="C-0001", version="v1"):
    insert(
        store, "ruling",
        {"ruling_id": ruling_id, "ts": now(), "summary": "fixture ruling", "status": "active",
         "ledger_sha256_after": "1" * 64},
    )
    insert(
        store, "law_digest",
        {"version": version, "generated_ts": now(), "content_sha256": "2" * 64, "rendered_path": "law/LAW_DIGEST.md"},
    )
    return ruling_id, version


def seed_gated_artifact_and_gate(store, *, launch_id):
    from trialerror.artifacts.gates import open_gate
    from trialerror.artifacts.registry import create_artifact

    insert(store, "template", {"type_key": "keystone", "title": "Keystone", "version": "1",
                                "path": "templates/keystone.md", "gated": 1})
    artifact = create_artifact(store, type_key="keystone", title="k", path="p", sha256="0" * 64, by_launch=launch_id)
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    return artifact, gate


def seed_thread(store, *, launch_id):
    thread_id = new_id("THR")
    insert(store, "thread", {"thread_id": thread_id, "title": "t", "created_ts": now(), "created_by_launch": launch_id})
    return thread_id


@pytest.fixture()
def tools(program_root, platform_root):
    return build_tools(program_root=program_root, platform_root=platform_root)


def test_tool_registry_has_exactly_the_12_named_tools(tools):
    assert len(tools) == TOOL_COUNT == 12
    assert set(tools) == {
        "session_status", "budget_status", "book_launch", "reconcile_launch",
        "append_event", "post_feed", "read_inbox", "law_lookup",
        "register_artifact", "gate_advance", "prereg_commit", "record_verdict",
    }
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
# 2. budget_status
# ---------------------------------------------------------------------------


def test_budget_status_derives_account_from_open_session(store, tools):
    account_id, session_id = seed_account_session(store)
    seed_pool(store, account_id=account_id)
    env = tools["budget_status"].handler({})
    assert env["ok"] is True
    assert env["result"]["account_id"] == account_id
    assert env["result"]["pools"][0]["model_class"] == "top"


def test_budget_status_no_open_session_is_structured_error(store, tools):
    env = tools["budget_status"].handler({})
    assert env["ok"] is False
    assert env["error"]["code"] == "no_open_session"


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


def test_book_launch_no_open_session_is_structured_error(store, tools):
    env = tools["book_launch"].handler(
        {"program_id": "PROG-test", "agent_kind": "tester", "model_class": "top",
         "model": "sonnet", "purpose": "fixture", "est_tokens": 500}
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "no_open_session"


# ---------------------------------------------------------------------------
# 4. reconcile_launch
# ---------------------------------------------------------------------------


def test_reconcile_launch_settles_actuals(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id, state="RUNNING")
    env = tools["reconcile_launch"].handler({"launch_id": launch_id, "actual_tokens": 321})
    assert env["ok"] is True
    assert env["result"]["state"] == "RECONCILED"
    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    assert row["actual_tokens"] == 321


def test_reconcile_launch_unknown_id_is_structured_error(store, tools):
    env = tools["reconcile_launch"].handler({"launch_id": "LNCH-nope", "actual_tokens": 1})
    assert env["ok"] is False
    assert env["error"]["code"] == "reconcile_refused"


# ---------------------------------------------------------------------------
# 5. append_event
# ---------------------------------------------------------------------------


def test_append_event_writes_a_row(store, tools):
    account_id, session_id = seed_account_session(store)
    env = tools["append_event"].handler(
        {"event_type": "test_event", "payload": {"k": "v"}, "session_id": session_id}
    )
    assert env["ok"] is True
    assert env["result"]["type"] == "test_event"
    assert env["result"]["session_id"] == session_id


def test_append_event_bad_launch_id_is_structured_error(store, tools):
    env = tools["append_event"].handler(
        {"event_type": "test_event", "payload": {}, "launch_id": "LNCH-does-not-exist"}
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "append_refused"


# ---------------------------------------------------------------------------
# 6. post_feed
# ---------------------------------------------------------------------------


def test_post_feed_derives_author_from_launch_id_never_accepts_one(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    thread_id = seed_thread(store, launch_id=launch_id)
    env = tools["post_feed"].handler({"thread_id": thread_id, "body": "full text here", "launch_id": launch_id})
    assert env["ok"] is True
    assert env["result"]["author"] == f"mcp-ops-fixture:{launch_id}"
    assert "author" not in tools["post_feed"].input_schema["properties"]  # never a caller-settable field


def test_post_feed_orchestrator_fallback_when_launch_id_omitted(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    thread_id = seed_thread(store, launch_id=launch_id)
    env = tools["post_feed"].handler({"thread_id": thread_id, "body": "orchestrator post"})
    assert env["ok"] is True
    assert env["result"]["author"] == f"orchestrator:{session_id}"


def test_post_feed_unknown_thread_is_structured_error(store, tools):
    env = tools["post_feed"].handler({"thread_id": "THR-nope", "body": "x"})
    assert env["ok"] is False
    assert env["error"]["code"] == "post_refused"


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


# ---------------------------------------------------------------------------
# 8. law_lookup
# ---------------------------------------------------------------------------


def test_law_lookup_search_only(store, tools):
    seed_ruling_and_digest(store)
    env = tools["law_lookup"].handler({"status": "active"})
    assert env["ok"] is True
    assert env["result"]["count"] == 1
    assert env["result"]["pin_check"] is None


def test_law_lookup_with_pin_attaches_verify_and_diff(store, tools):
    seed_ruling_and_digest(store, ruling_id="C-0001", version="v1")
    env = tools["law_lookup"].handler({"pin": "v1@" + now()[:10]})
    assert env["ok"] is True
    assert env["result"]["pin_check"] is not None
    assert env["result"]["foreign_since_pin"] == []


def test_law_lookup_bad_pin_reports_structured_not_a_crash(store, tools):
    env = tools["law_lookup"].handler({"pin": "v99@2099-01-01"})
    assert env["ok"] is True  # the lookup itself always succeeds; the pin diff failed cleanly
    assert env["result"]["foreign_since_pin_error"] is not None


# ---------------------------------------------------------------------------
# 9. register_artifact
# ---------------------------------------------------------------------------


def test_register_artifact_refuses_ungated_type_check(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    from trialerror.artifacts.registry import create_artifact

    insert(store, "template", {"type_key": "note", "title": "Note", "version": "1",
                                "path": "templates/note.md", "gated": 0})
    artifact = create_artifact(store, type_key="note", title="n", path="p", sha256="0" * 64, by_launch=launch_id)
    env = tools["register_artifact"].handler({"artifact_id": artifact["artifact_id"], "by_launch": launch_id})
    assert env["ok"] is True
    assert env["result"]["status"] == "registered"


def test_register_artifact_gated_without_union_applied_is_structured_error(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    artifact, gate = seed_gated_artifact_and_gate(store, launch_id=launch_id)
    env = tools["register_artifact"].handler({"artifact_id": artifact["artifact_id"], "by_launch": launch_id})
    assert env["ok"] is False
    assert env["error"]["code"] == "registration_refused"


# ---------------------------------------------------------------------------
# 10. gate_advance
# ---------------------------------------------------------------------------


def test_gate_advance_legal_transition(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    artifact, gate = seed_gated_artifact_and_gate(store, launch_id=launch_id)
    env = tools["gate_advance"].handler({"gate_id": gate["gate_id"], "to_state": "submitted", "by_launch": launch_id})
    assert env["ok"] is True
    assert env["result"]["state"] == "submitted"


def test_gate_advance_illegal_transition_is_structured_error(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    artifact, gate = seed_gated_artifact_and_gate(store, launch_id=launch_id)
    env = tools["gate_advance"].handler({"gate_id": gate["gate_id"], "to_state": "registered", "by_launch": launch_id})
    assert env["ok"] is False
    assert env["error"]["code"] == "transition_refused"


# ---------------------------------------------------------------------------
# 11. prereg_commit
# ---------------------------------------------------------------------------


def test_prereg_commit_hashes_and_escrows_outside_program_repo(store, tools, program_root, platform_root):
    env = tools["prereg_commit"].handler(
        {"title": "blind test", "procedure": "do the thing exactly this way", "params": {"n": 3, "a": "x"}}
    )
    assert env["ok"] is True
    row = env["result"]
    assert row["status"] == "committed"
    assert len(row["procedure_sha256"]) == 64
    assert len(row["params_sha256"]) == 64
    escrow_path = row["escrow_path"]
    assert str(platform_root) in escrow_path
    assert str(program_root) not in escrow_path  # the blind is OUTSIDE the program repo (design Sec 4.2)
    from pathlib import Path

    content = json.loads(Path(escrow_path).read_text(encoding="utf-8"))
    assert content["procedure"] == "do the thing exactly this way"
    db_row = get(store, "prereg", pk_column="prereg_id", pk_value=row["prereg_id"])
    assert db_row is not None and db_row["status"] == "committed"


def test_prereg_commit_empty_procedure_is_structured_error(store, tools):
    env = tools["prereg_commit"].handler({"title": "t", "procedure": "   "})
    assert env["ok"] is False
    assert env["error"]["code"] == "invalid_procedure"


# ---------------------------------------------------------------------------
# 12. record_verdict
# ---------------------------------------------------------------------------


def test_record_verdict_writes_the_knowledge_verdict_row(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    env = tools["record_verdict"].handler(
        {
            "subject_kind": "claim", "subject_id": "CLM-fixture", "procedure": "citecheck",
            "procedure_version": "1", "label": "PASS",
            "evidence": [{"anchor_id": "ANC-1", "stance": "supports"}],
            "issued_by_launch": launch_id,
        }
    )
    assert env["ok"] is True
    row = env["result"]
    assert row["subject_kind"] == "claim"
    assert json.loads(row["evidence"])[0]["anchor_id"] == "ANC-1"
    assert row["prereg_compliant"] is None


def test_record_verdict_bad_subject_kind_is_structured_error(store, tools):
    account_id, session_id = seed_account_session(store)
    launch_id = seed_launch(store, account_id=account_id, session_id=session_id)
    env = tools["record_verdict"].handler(
        {
            "subject_kind": "not-a-real-kind", "subject_id": "X", "procedure": "citecheck",
            "procedure_version": "1", "label": "PASS", "issued_by_launch": launch_id,
        }
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "invalid_subject_kind"


def test_record_verdict_unknown_launch_is_structured_error(store, tools):
    env = tools["record_verdict"].handler(
        {
            "subject_kind": "claim", "subject_id": "X", "procedure": "citecheck",
            "procedure_version": "1", "label": "PASS", "issued_by_launch": "LNCH-does-not-exist",
        }
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "record_refused"
