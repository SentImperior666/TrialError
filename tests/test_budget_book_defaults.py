"""Lane FB-7 item 6: ``budget book`` finds its session and its program.

``budget status`` and ``budget reconcile`` already resolve the open session
(lane FB-1's F7 pattern). ``book`` -- the verb an operator runs most --
refused without ``--session-id`` and ``--program-id``, two values the
harness already holds: the session is bound at ``session boot`` and the
program id is ``trialerror.toml``'s ``[program] id``.

The refusals are the point of the change, not the convenience. Two open
sessions is exactly the state that produces bookings nothing can reconcile,
so it is named rather than picked between; and the envelope says which ids
were resolved rather than given, because a booking that silently chose its
own session is otherwise indistinguishable from one that was told.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from trialerror.budget.errors import NoOpenSessionError
from trialerror.budget.gate import resolve_booking_identity
from trialerror.cli import main
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

PROGRAM_TOML = '[program]\nid = "PROG-from-config"\n'


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


@pytest.fixture()
def roots(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text(PROGRAM_TOML, encoding="utf-8")
    return platform_root, program_root


def _open_session(program_root, platform_root, *, status="open"):
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": status},
    )
    store.close()
    return account_id, session_id


def _book(program_root, platform_root, *rest):
    return _run_cli([
        "budget", "--program-root", str(program_root), "--platform-root", str(platform_root), "book",
        "--agent-kind", "lens", "--model-class", "mid", "--model", "sonnet",
        "--purpose", "mechanical", "--est-tokens", "100", *rest,
    ])


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------


def test_resolver_takes_the_open_session_and_the_config_id(roots):
    platform_root, program_root = roots
    _account_id, session_id = _open_session(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        resolved_session, resolved_program, resolved_from = resolve_booking_identity(store)
    finally:
        store.close()
    assert resolved_session == session_id
    assert resolved_program == "PROG-from-config"
    assert resolved_from == {"session_id": "open_session", "program_id": "config"}


def test_explicit_values_win(roots):
    platform_root, program_root = roots
    _open_session(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        resolved_session, resolved_program, resolved_from = resolve_booking_identity(
            store, session_id="SESS-explicit", program_id="PROG-explicit"
        )
    finally:
        store.close()
    assert (resolved_session, resolved_program) == ("SESS-explicit", "PROG-explicit")
    assert resolved_from == {"session_id": "flag", "program_id": "flag"}


def test_no_open_session_names_both_ways_out(roots):
    platform_root, program_root = roots
    _open_session(program_root, platform_root, status="closed")
    store = open_store(program_root, platform_root=platform_root)
    try:
        with pytest.raises(NoOpenSessionError) as excinfo:
            resolve_booking_identity(store)
    finally:
        store.close()
    message = str(excinfo.value)
    assert "--session-id" in message and "session boot" in message


def test_two_open_sessions_raise_rather_than_pick(roots):
    platform_root, program_root = roots
    _open_session(program_root, platform_root)
    _open_session(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        with pytest.raises(RuntimeError, match="OPEN simultaneously"):
            resolve_booking_identity(store)
    finally:
        store.close()


def test_a_program_with_no_config_refuses_by_name(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    _open_session(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        with pytest.raises(ValueError, match="--program-id"):
            resolve_booking_identity(store)
        # ...and an explicit one still books on a program with no config.
        assert resolve_booking_identity(store, program_id="PROG-x")[1] == "PROG-x"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# through the CLI
# ---------------------------------------------------------------------------


def test_book_with_one_open_session_and_no_flags(roots):
    platform_root, program_root = roots
    _account_id, session_id = _open_session(program_root, platform_root)
    rc, env = _book(program_root, platform_root)
    assert rc == 0, env
    assert env["ok"] is True
    assert env["result"]["state"] == "PROVISIONAL"
    assert env["result"]["resolved_from"] == {"session_id": "open_session", "program_id": "config"}

    store = open_store(program_root, platform_root=platform_root)
    try:
        row = store.platform.execute(
            "SELECT session_id, program_id FROM launch WHERE launch_id = ?", (env["result"]["launch_id"],)
        ).fetchone()
    finally:
        store.close()
    assert row["session_id"] == session_id
    assert row["program_id"] == "PROG-from-config"


def test_two_open_sessions_are_refused_by_name(roots):
    platform_root, program_root = roots
    _open_session(program_root, platform_root)
    _open_session(program_root, platform_root)
    rc, env = _book(program_root, platform_root)
    assert env["ok"] is False
    assert env["error"]["code"] == "multiple_open_sessions"
    assert "--session-id" in env["error"]["message"]


def test_explicit_flags_still_win_through_the_cli(roots):
    platform_root, program_root = roots
    _open_session(program_root, platform_root)
    _account_id, second = _open_session(program_root, platform_root)
    rc, env = _book(program_root, platform_root, "--session-id", second, "--program-id", "PROG-named")
    assert rc == 0, env
    assert env["result"]["resolved_from"] == {"session_id": "flag", "program_id": "flag"}
    store = open_store(program_root, platform_root=platform_root)
    try:
        row = store.platform.execute(
            "SELECT session_id, program_id FROM launch WHERE launch_id = ?", (env["result"]["launch_id"],)
        ).fetchone()
    finally:
        store.close()
    assert row["session_id"] == second
    assert row["program_id"] == "PROG-named"


def test_no_open_session_is_still_refused(roots):
    platform_root, program_root = roots
    _open_session(program_root, platform_root, status="closed")
    rc, env = _book(program_root, platform_root)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_open_session"
    assert "--session-id" in env["error"]["message"]


def test_a_program_with_no_program_id_refuses_by_name(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    _open_session(program_root, platform_root)
    rc, env = _book(program_root, platform_root)
    assert env["ok"] is False
    assert env["error"]["code"] == "program_id_unresolved"
    assert "--program-id" in env["error"]["message"]


# ---------------------------------------------------------------------------
# and the MCP tool, which is the surface an orchestrator books through
# ---------------------------------------------------------------------------


def test_the_mcp_tool_takes_the_same_defaults(roots):
    from trialerror.mcp.ops import _tool_book_launch

    platform_root, program_root = roots
    _account_id, session_id = _open_session(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        envelope = _tool_book_launch(
            {"agent_kind": "lens", "model_class": "mid", "model": "sonnet",
             "purpose": "mechanical", "est_tokens": 100},
            store=store,
        )
    finally:
        store.close()
    assert envelope["ok"] is True, envelope
    assert envelope["result"]["resolved_from"] == {"session_id": "open_session", "program_id": "config"}


def test_the_mcp_tool_no_longer_requires_program_id(roots):
    from trialerror.mcp.ops import build_tools

    platform_root, program_root = roots
    spec = build_tools(program_root=program_root, platform_root=platform_root)["book_launch"]
    required = spec.input_schema["required"]
    assert "program_id" not in required
    assert "session_id" not in required
    assert "agent_kind" in required


# ---------------------------------------------------------------------------
# fix pass V-8: the two new clauses cover the resolver, not the booking
# ---------------------------------------------------------------------------


def test_a_value_error_from_the_booking_body_is_not_reported_as_a_config_fault(roots, monkeypatch):
    """The clauses shipped wrapping the WHOLE booking body, so any
    ``RuntimeError`` or ``ValueError`` raised by the policy load, the
    preconditions, the quota reader or ``book_launch`` came back as
    ``program_id_unresolved`` or ``multiple_open_sessions`` -- sending the
    operator to fix a session or a config that is fine. A
    ``json.JSONDecodeError`` IS a ``ValueError``, so this was one malformed
    file away from being live. The MCP handler always wrapped only the
    resolve call; this is the CLI reading the same."""
    import trialerror.cli.budget as budget_cli

    platform_root, program_root = roots
    _open_session(program_root, platform_root)

    def _boom(*a, **kw):
        raise ValueError("a capture file that is not JSON")

    monkeypatch.setattr(budget_cli, "book_launch", _boom)
    with pytest.raises(ValueError, match="not JSON"):
        _book(program_root, platform_root)


def test_a_runtime_error_from_the_booking_body_is_not_reported_as_two_sessions(roots, monkeypatch):
    import trialerror.cli.budget as budget_cli

    platform_root, program_root = roots
    _open_session(program_root, platform_root)

    def _boom(*a, **kw):
        raise RuntimeError("the ledger is locked by another process")

    monkeypatch.setattr(budget_cli, "book_launch", _boom)
    with pytest.raises(RuntimeError, match="locked by another process"):
        _book(program_root, platform_root)


def test_the_resolver_s_own_two_faults_are_still_named(roots, tmp_path):
    """The narrowing must not have taken the diagnoses with it."""
    platform_root, program_root = roots
    _open_session(program_root, platform_root)
    _open_session(program_root, platform_root)
    _rc, envelope = _book(program_root, platform_root)
    assert envelope["error"]["code"] == "multiple_open_sessions"

    bare_platform = tmp_path / "bare-platform"
    bare_program = tmp_path / "bare-program"
    bare_program.mkdir()
    _open_session(bare_program, bare_platform)
    _rc, envelope = _book(bare_program, bare_platform)
    assert envelope["error"]["code"] == "program_id_unresolved"
