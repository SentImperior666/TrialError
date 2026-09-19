"""End-to-end tests of ``plugin/hooks/{session_start,stop_check,post_task}.py``
AS SCRIPTS — real subprocess, real stdin JSON, real exit code, exactly the
interface Claude Code's hook protocol uses. Mirrors
``tests/test_spawn_gate_hook.py``'s own precedent: the decision logic
already has direct-import unit coverage elsewhere
(``tests/test_session_lifecycle.py``); this file's job is narrower —
prove the stdin JSON -> exit-code/stdout plumbing each script does is
correct, since design Section 12 M6 row explicitly names "SessionStart
injects bundle" a live-CC-only acceptance item this is the closest a
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

HOOKS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "hooks"
SESSION_START = HOOKS_DIR / "session_start.py"
STOP_CHECK = HOOKS_DIR / "stop_check.py"
POST_TASK = HOOKS_DIR / "post_task.py"


def _run_hook(script: Path, payload: dict, *, platform_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60
    )


@pytest.fixture()
def roots(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    return platform_root, program_root


def _seed_account(platform_root, program_root) -> str:
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    store.close()
    return account_id


# ---------------------------------------------------------------------------
# session_start.py
# ---------------------------------------------------------------------------


def test_session_start_boots_single_account_and_injects_context(roots):
    platform_root, program_root = roots
    _seed_account(platform_root, program_root)

    payload = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(program_root)}
    proc = _run_hook(SESSION_START, payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "pre-loaded" in out["hookSpecificOutput"]["additionalContext"]

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT * FROM session WHERE status='open'").fetchall()
    assert len(rows) == 1
    hook_alive = store.ops.execute("SELECT COUNT(*) c FROM event WHERE type='hook_alive'").fetchone()["c"]
    assert hook_alive == 1
    store.close()


def test_session_start_records_hook_alive_even_when_boot_refused(roots):
    platform_root, program_root = roots
    _seed_account(platform_root, program_root)
    _seed_account_2 = open_store(program_root, platform_root=platform_root)
    insert(_seed_account_2, "account", {"account_id": new_id("ACC"), "label": "second", "created_ts": now()})
    _seed_account_2.close()

    payload = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(program_root)}
    proc = _run_hook(SESSION_START, payload, platform_root=platform_root)
    assert proc.returncode == 0
    assert "account_required" in proc.stderr
    assert proc.stdout.strip() == ""  # no context injected -- boot did not complete

    store = open_store(program_root, platform_root=platform_root)
    hook_alive = store.ops.execute("SELECT COUNT(*) c FROM event WHERE type='hook_alive'").fetchone()["c"]
    assert hook_alive == 1  # still recorded (session_id NULL)
    store.close()


def test_session_start_idempotent_on_repeated_fire(roots):
    platform_root, program_root = roots
    _seed_account(platform_root, program_root)
    payload = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(program_root)}

    first = _run_hook(SESSION_START, payload, platform_root=platform_root)
    assert first.returncode == 0
    second = _run_hook(SESSION_START, payload, platform_root=platform_root)
    assert second.returncode == 0

    store = open_store(program_root, platform_root=platform_root)
    open_sessions = store.ops.execute("SELECT * FROM session WHERE status='open'").fetchall()
    assert len(open_sessions) == 1  # /clear-style re-fire did not open a second session
    hook_alive = store.ops.execute("SELECT COUNT(*) c FROM event WHERE type='hook_alive'").fetchone()["c"]
    assert hook_alive == 2  # but each fire still recorded its own hook_alive
    store.close()


def test_session_start_unparseable_stdin_passes_through(roots):
    platform_root, program_root = roots
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    proc = subprocess.run(
        [sys.executable, str(SESSION_START)], input="not json {{{", capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# stop_check.py
# ---------------------------------------------------------------------------


def test_stop_check_allows_clean_session(roots):
    platform_root, program_root = roots
    _seed_account(platform_root, program_root)
    boot_payload = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(program_root)}
    _run_hook(SESSION_START, boot_payload, platform_root=platform_root)

    proc = _run_hook(STOP_CHECK, {"hook_event_name": "Stop", "cwd": str(program_root)}, platform_root=platform_root)
    assert proc.returncode == 0


def test_stop_check_blocks_once_on_dangling_launch_then_allows_with_stop_hook_active(roots):
    platform_root, program_root = roots
    account_id = _seed_account(platform_root, program_root)
    _run_hook(SESSION_START, {"hook_event_name": "SessionStart", "cwd": str(program_root)}, platform_root=platform_root)

    store = open_store(program_root, platform_root=platform_root)
    session_row = store.ops.execute("SELECT session_id FROM session WHERE status='open'").fetchone()
    book_launch(
        store, session_id=session_row["session_id"], program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=10,
    )
    store.close()

    first = _run_hook(STOP_CHECK, {"hook_event_name": "Stop", "cwd": str(program_root)}, platform_root=platform_root)
    assert first.returncode == 2
    assert "STOP CHECKLIST" in first.stderr

    second = _run_hook(
        STOP_CHECK, {"hook_event_name": "Stop", "cwd": str(program_root), "stop_hook_active": True}, platform_root=platform_root
    )
    assert second.returncode == 0  # never traps the user


def test_stop_check_no_open_session_passes_through(roots):
    platform_root, program_root = roots
    program_root_no_session = program_root
    proc = _run_hook(
        STOP_CHECK, {"hook_event_name": "Stop", "cwd": str(program_root_no_session)}, platform_root=platform_root
    )
    assert proc.returncode == 0


def test_stop_check_unparseable_stdin_passes_through(roots):
    platform_root, program_root = roots
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    proc = subprocess.run(
        [sys.executable, str(STOP_CHECK)], input="not json {{{", capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# post_task.py
# ---------------------------------------------------------------------------


def test_post_task_passes_through_non_task_tools(roots):
    platform_root, program_root = roots
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(program_root)}
    proc = _run_hook(POST_TASK, payload, platform_root=platform_root)
    assert proc.returncode == 0


def test_post_task_appends_subagent_return_event(roots):
    platform_root, program_root = roots
    _seed_account(platform_root, program_root)
    _run_hook(SESSION_START, {"hook_event_name": "SessionStart", "cwd": str(program_root)}, platform_root=platform_root)

    store = open_store(program_root, platform_root=platform_root)
    session_row = store.ops.execute("SELECT session_id FROM session WHERE status='open'").fetchone()
    booked = book_launch(
        store, session_id=session_row["session_id"], program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=10,
    )
    store.close()

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": f"do the thing. launch_id: {booked.launch_id}"},
        "tool_response": {"result": "ok", "ids": ["X-1", "X-2"]},
        "cwd": str(program_root),
    }
    proc = _run_hook(POST_TASK, payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT * FROM event WHERE type='subagent_return'").fetchall()
    assert len(rows) == 1
    assert rows[0]["launch_id"] == booked.launch_id
    payload_obj = json.loads(rows[0]["payload"])
    assert payload_obj["response_size_bytes"] > 0
    store.close()


def test_post_task_appends_subagent_return_event_for_renamed_agent_tool(roots):
    """Task->Agent rename (found 2026-09-05): Claude Code 2.1.x invokes the
    subagent tool as ``Agent``. Before the fix, ``tool_name != "Task"``
    made this hook a silent no-op for every real spawn -- no
    ``subagent_return`` event, no accounting. Mirrors
    ``test_post_task_appends_subagent_return_event`` above with
    ``tool_name: "Agent"``."""
    platform_root, program_root = roots
    _seed_account(platform_root, program_root)
    _run_hook(SESSION_START, {"hook_event_name": "SessionStart", "cwd": str(program_root)}, platform_root=platform_root)

    store = open_store(program_root, platform_root=platform_root)
    session_row = store.ops.execute("SELECT session_id FROM session WHERE status='open'").fetchone()
    booked = book_launch(
        store, session_id=session_row["session_id"], program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=10,
    )
    store.close()

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": f"do the thing. launch_id: {booked.launch_id}"},
        "tool_response": {"result": "ok", "ids": ["X-1", "X-2"]},
        "cwd": str(program_root),
    }
    proc = _run_hook(POST_TASK, payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT * FROM event WHERE type='subagent_return'").fetchall()
    assert len(rows) == 1
    assert rows[0]["launch_id"] == booked.launch_id
    payload_obj = json.loads(rows[0]["payload"])
    assert payload_obj["response_size_bytes"] > 0
    # FU-11 verification finding F3: the absent hook_alive{hook="post_task"}
    # row was half the live incident's signature -- assert it explicitly for
    # the Agent-named payload, not just transitively via the subagent_return
    # row above.
    hook_alive_rows = store.ops.execute("SELECT payload FROM event WHERE type='hook_alive'").fetchall()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in hook_alive_rows]
    assert "post_task" in hooks_seen, f"no hook_alive{{hook='post_task'}} row, got {hooks_seen!r}"
    store.close()


def test_post_task_records_a_post_task_hook_alive_marker_distinct_from_session_start(roots):
    """FX-8 (C-0064 lens B EP-1 Bypass C): post_task.py must leave its own
    ``payload.hook == "post_task"`` liveness marker (distinct from
    session_start's ``"session_start"``) at least once per session -- not
    once per Task return, hence the second fire below must NOT add a
    second row."""
    platform_root, program_root = roots
    _seed_account(platform_root, program_root)
    _run_hook(SESSION_START, {"hook_event_name": "SessionStart", "cwd": str(program_root)}, platform_root=platform_root)

    store = open_store(program_root, platform_root=platform_root)
    session_row = store.ops.execute("SELECT session_id FROM session WHERE status='open'").fetchone()
    booked = book_launch(
        store, session_id=session_row["session_id"], program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=10,
    )
    store.close()

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": f"do the thing. launch_id: {booked.launch_id}"},
        "tool_response": {"result": "ok"},
        "cwd": str(program_root),
    }
    first = _run_hook(POST_TASK, payload, platform_root=platform_root)
    assert first.returncode == 0, first.stderr
    second = _run_hook(POST_TASK, payload, platform_root=platform_root)
    assert second.returncode == 0, second.stderr

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'hook_alive' ORDER BY rowid ASC").fetchall()
    store.close()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in rows]
    assert hooks_seen == ["session_start", "post_task"], f"expected one of each marker, got {hooks_seen!r}"


def test_post_task_unparseable_stdin_passes_through(roots):
    platform_root, program_root = roots
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    proc = subprocess.run(
        [sys.executable, str(POST_TASK)], input="not json {{{", capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0
