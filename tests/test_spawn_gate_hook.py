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


# ---------------------------------------------------------------------------
# Task->Agent rename (found 2026-09-05, C-0064-era live evidence): Claude
# Code 2.1.x invokes the subagent tool as "Agent", not "Task". The gate
# must treat both names identically -- these mirror the "Task" tests above
# with ``tool_name: "Agent"`` substituted in, one per booking-lifecycle
# state so the fix is proven at each of the same points the rename broke.
# ---------------------------------------------------------------------------


def test_hook_refuses_agent_with_no_launch_id_token(roots):
    platform_root, program_root = roots
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
        "tool_name": "Agent",
        "tool_input": {"prompt": "go do research, no ids anywhere"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "SPAWN REFUSED" in proc.stderr
    assert "no_launch_id_token" in proc.stderr


def test_hook_allows_booked_agent_and_consumes_the_token(roots, booked):
    """The exact live-evidence shape: a subagent spawn issued as the
    renamed ``Agent`` tool with a booked launch_id must be gated (and
    consumed) exactly like ``Task`` was -- this is the case that regressed
    silently (spawn went through with no booking check at all)."""
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    from trialerror.stores import get

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    # FU-11 verification findings F3/FU11-V6: the absent hook_alive{hook=
    # "spawn_gate"} row was HALF the live incident's own signature (the
    # other half being the missing booking check proven above) -- assert it
    # explicitly for the Agent-named payload, not just transitively via the
    # exit code/launch-state assertions.
    hook_alive_rows = store.ops.execute(
        "SELECT payload FROM event WHERE type='hook_alive'"
    ).fetchall()
    store.close()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in hook_alive_rows]
    assert "spawn_gate" in hooks_seen, f"no hook_alive{{hook='spawn_gate'}} row, got {hooks_seen!r}"
    assert row["state"] == "RUNNING"


def test_hook_refuses_the_same_token_reused_on_a_second_agent_spawn(roots, booked):
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }

    first = _run_hook(payload, platform_root=platform_root)
    assert first.returncode == 0, first.stderr

    second = _run_hook(payload, platform_root=platform_root)
    assert second.returncode == 2
    assert "SPAWN REFUSED" in second.stderr
    assert "token_not_provisional" in second.stderr


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


# ---------------------------------------------------------------------------
# FU-11 verification finding FU11-V5: the tool_name rename (Task -> Agent)
# proved an assumption about Claude Code's tool surface can go stale
# unnoticed; the sibling assumption -- that the launch_id-bearing prompt
# text lives under tool_input["prompt"]/["description"] -- was never
# checked. These prove the cheap fallback (scan the whole serialized
# tool_input) actually finds a launch_id token stashed under some other
# key, rather than only asserting it by reading the source.
# ---------------------------------------------------------------------------


def test_hook_finds_launch_id_token_under_an_unexpected_tool_input_key(roots, booked):
    """If a future Claude Code tool-input schema change moves the prompt
    text off ``prompt``/``description`` (unverified sibling assumption to
    the Task->Agent rename), the gate must still find a `launch_id:` token
    embedded in the tool_input rather than refusing every real spawn."""
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"instructions": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    from trialerror.stores import get

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    store.close()
    assert row["state"] == "RUNNING"


def test_hook_refuses_agent_when_tool_input_has_no_token_anywhere(roots):
    """The fallback must not manufacture a false match: an ``Agent`` call
    whose tool_input carries fields but no `launch_id:` token anywhere
    still refuses as ``no_launch_id_token``."""
    platform_root, program_root = roots
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
        "tool_name": "Agent",
        "tool_input": {"instructions": "go do research, no ids anywhere"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "no_launch_id_token" in proc.stderr


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
