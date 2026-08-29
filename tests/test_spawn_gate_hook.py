"""End-to-end tests of ``plugin/hooks/spawn_gate.py`` AS A SCRIPT — real
subprocess, real stdin JSON, real exit code, exactly the interface Claude
Code's PreToolUse hook protocol uses. ``trialerror.budget.gate`` (imported and
called directly, no subprocess) already covers the decision logic
exhaustively; this file's job is narrower and different: prove the stdin
JSON -> exit-code plumbing this specific script does is correct, since
design Section 12 M3 row explicitly calls out that "live-CC hook tests are
orchestrator-executed integration items" — this is the closest a
non-live-Claude-Code pytest run can get to that same round trip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.budget.pools import book_launch
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

HOOK_PATH = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "spawn_gate.py"


def _run_hook(payload: dict, *, platform_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@pytest.fixture()
def roots(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    return platform_root, program_root


@pytest.fixture()
def booked(roots):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    result = book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=100,
    )
    store.close()
    return result.launch_id


def test_hook_passes_through_non_task_tools(roots):
    platform_root, program_root = roots
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0


def test_hook_refuses_task_with_no_launch_id_token(roots):
    platform_root, program_root = roots
    # An open session must exist first -- otherwise the gate refuses on
    # "no_open_session" before it ever gets to look for a token (correctly:
    # no session means no spawn regardless of what the prompt says).
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    insert(
        store, "session",
        {"session_id": new_id("SESS"), "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": "go do research, no ids anywhere"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "SPAWN REFUSED" in proc.stderr
    assert "no_launch_id_token" in proc.stderr


def test_hook_refuses_task_with_no_open_session_at_all(roots):
    platform_root, program_root = roots
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": "launch_id: " + new_id("LNCH")},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "no_open_session" in proc.stderr


def test_hook_allows_booked_task_and_consumes_the_token(roots, booked):
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    from trialerror.stores import get

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    store.close()
    assert row["state"] == "RUNNING"


def test_hook_refuses_the_same_token_reused_on_a_second_spawn(roots, booked):
    """Adversarial token-reuse case, exercised through the actual script a
    live Claude Code session would invoke."""
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }

    first = _run_hook(payload, platform_root=platform_root)
    assert first.returncode == 0, first.stderr

    second = _run_hook(payload, platform_root=platform_root)
    assert second.returncode == 2
    assert "SPAWN REFUSED" in second.stderr
    assert "token_not_provisional" in second.stderr


def test_hook_records_a_spawn_gate_hook_alive_marker_distinct_from_session_start(roots, booked):
    """FX-8 (C-0064 lens B EP-1 Bypass C): the spawn gate must leave its own
    ``payload.hook == "spawn_gate"`` liveness marker, distinct from
    ``session_start.py``'s ``"session_start"`` value -- this is the marker
    ``close_session``'s ``hooks_partial`` check looks for. Recorded even
    when the gate itself REFUSES (a no-token Task call), same "the hook
    fired" posture as session_start.py."""
    platform_root, program_root = roots
    launch_id = booked

    refused = _run_hook(
        {
            "hook_event_name": "PreToolUse", "tool_name": "Task",
            "tool_input": {"prompt": "no launch_id token here at all"},
            "cwd": str(program_root),
        },
        platform_root=platform_root,
    )
    assert refused.returncode == 2

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'hook_alive'").fetchall()
    store.close()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in rows]
    assert hooks_seen == ["spawn_gate"], f"expected exactly one spawn_gate marker, got {hooks_seen!r}"

    # A second Task call (this time consuming the real booking) must NOT
    # add a second marker -- first-fire-per-session only.
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Task",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    consumed = _run_hook(payload, platform_root=platform_root)
    assert consumed.returncode == 0, consumed.stderr

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'hook_alive'").fetchall()
    store.close()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in rows]
    assert hooks_seen == ["spawn_gate"], f"expected still exactly one marker (de-duped), got {hooks_seen!r}"


def test_hook_unparseable_stdin_passes_through(roots):
    platform_root, program_root = roots
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not json at all {{{",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0
