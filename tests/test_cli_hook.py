"""Tests for the ``trialerror hook`` CLI group -- the portable replacement for
``python "${CLAUDE_PLUGIN_ROOT}/hooks/<name>.py"`` in the plugin manifest.

Why this file exists separately from ``tests/test_spawn_gate_hook.py`` and
``tests/test_session_hooks.py``: those two prove the *decision logic* and the
stdin-JSON-to-exit-code plumbing via the loose scripts, which still works
through the compatibility shims. What is new -- and what nothing else covers
-- is that the hook verdict survives the CLI's own envelope machinery. Every
other command group returns a dict that ``trialerror.cli.main`` serializes to
stdout and maps to exit 0/1; a hook that went through that path would be
unable to express exit 2 (BLOCK) and would corrupt SessionStart's stdout
contract with an envelope. So the assertions below are deliberately about the
*process interface* -- exit code and stream discipline -- not about gate
semantics.

These run the real console script as a subprocess, which is the only way to
observe what Claude Code actually observes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.budget.pools import book_launch
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _trialerror_exe() -> str:
    """The installed console script, preferring the one next to the running
    interpreter (the venv under test) over anything else on PATH."""
    bindir = Path(sys.executable).parent
    for name in ("trialerror.exe", "trialerror"):
        candidate = bindir / name
        if candidate.exists():
            return str(candidate)
    found = shutil.which("trialerror")
    if found is None:  # pragma: no cover - only on a non-installed checkout
        pytest.skip("trialerror console script not installed in this environment")
    return found


def _run(action: str, payload: dict, *, platform_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    return subprocess.run(
        [_trialerror_exe(), "hook", action],
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
def open_session(roots):
    """A program with an account and one OPEN session, so the hooks reach
    their real code paths instead of bailing at 'no program stores'."""
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()
    return platform_root, program_root, session_id


# ---------------------------------------------------------------------------
# the exit-code contract (this is the whole point of the group)
# ---------------------------------------------------------------------------
def test_spawn_gate_refuses_unbooked_spawn_with_exit_2(open_session):
    platform_root, program_root, _ = open_session
    proc = _run(
        "spawn-gate",
        {"tool_name": "Task", "tool_input": {"prompt": "go do research"}, "cwd": str(program_root)},
        platform_root=platform_root,
    )
    assert proc.returncode == 2, f"expected BLOCK, got {proc.returncode}: {proc.stderr}"
    assert "SPAWN REFUSED" in proc.stderr


def test_spawn_gate_refusal_writes_nothing_to_stdout(open_session):
    """The envelope every other group emits would be noise here -- Claude Code
    reads stdout as hook output, not as a CLI result."""
    platform_root, program_root, _ = open_session
    proc = _run(
        "spawn-gate",
        {"tool_name": "Task", "tool_input": {"prompt": "go do research"}, "cwd": str(program_root)},
        platform_root=platform_root,
    )
    assert proc.stdout == "", f"stdout must stay empty, got {proc.stdout!r}"


def test_spawn_gate_passes_through_non_task_tools(open_session):
    platform_root, program_root, _ = open_session
    proc = _run(
        "spawn-gate",
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(program_root)},
        platform_root=platform_root,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_stop_check_blocks_once_on_unready_session(open_session):
    """A dangling PROVISIONAL launch makes the session not close-ready, which
    the Stop hook must surface as exit 2 + a checklist on stderr."""
    platform_root, program_root, session_id = open_session
    store = open_store(program_root, platform_root=platform_root)
    # book_launch leaves the launch PROVISIONAL until it is reconciled --
    # i.e. exactly the "dangling booking" close_readiness complains about.
    book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="orchestrator",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=100,
    )
    store.close()

    proc = _run("stop-check", {"cwd": str(program_root)}, platform_root=platform_root)
    assert proc.returncode == 2, f"expected BLOCK, got {proc.returncode}: {proc.stderr}"
    assert "STOP CHECKLIST" in proc.stderr
    assert proc.stdout == ""


def test_stop_check_does_not_trap_the_user_on_a_second_stop(open_session):
    """``stop_hook_active`` is Claude Code telling us it already blocked once."""
    platform_root, program_root, _ = open_session
    proc = _run(
        "stop-check",
        {"cwd": str(program_root), "stop_hook_active": True},
        platform_root=platform_root,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# stdout IS the protocol for SessionStart
# ---------------------------------------------------------------------------
def test_session_start_emits_hook_specific_output_on_stdout(roots):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    insert(store, "account", {"account_id": new_id("ACC"), "label": "solo", "created_ts": now()})
    store.close()

    proc = _run("session-start", {"cwd": str(program_root)}, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "additionalContext" in payload["hookSpecificOutput"]


def test_post_task_never_blocks(open_session):
    platform_root, program_root, _ = open_session
    proc = _run(
        "post-task",
        {
            "tool_name": "Task",
            "tool_input": {"prompt": "launch_id: LNCH-nope"},
            "tool_response": {"content": "done"},
            "cwd": str(program_root),
        },
        platform_root=platform_root,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# unparseable stdin must never take the session down
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["session-start", "spawn-gate", "post-task", "stop-check"])
def test_unparseable_stdin_passes_through(action, roots):
    platform_root, _ = roots
    proc = subprocess.run(
        [_trialerror_exe(), "hook", action],
        input="not json at all",
        capture_output=True,
        text=True,
        env={**os.environ, "TRIALERROR_PLATFORM_ROOT": str(platform_root)},
        timeout=60,
    )
    assert proc.returncode == 0, f"{action} must fail open on garbage stdin"


# ---------------------------------------------------------------------------
# the group still behaves like a CLI group where that makes sense
# ---------------------------------------------------------------------------
def test_bare_group_returns_an_envelope_listing_the_hooks():
    """No action given is an ordinary CLI misuse, not a hook invocation, so
    here the normal envelope contract does apply."""
    proc = subprocess.run(
        [_trialerror_exe(), "hook"], capture_output=True, text=True, timeout=60
    )
    env = json.loads(proc.stdout)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_action"
    for name in ("session-start", "spawn-gate", "post-task", "stop-check"):
        assert name in env["error"]["message"]


def test_hooks_json_manifest_uses_the_console_script_not_a_bare_interpreter():
    """Regression guard for the Linux breakage this group exists to fix: a
    bare ``python`` in the manifest is exit 127 on a stock Debian/Ubuntu box,
    which silently turns the spawn gate into a no-op."""
    manifest_path = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "hooks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    commands = [
        h["command"]
        for entries in manifest["hooks"].values()
        for entry in entries
        for h in entry["hooks"]
    ]
    assert len(commands) == 4
    for command in commands:
        assert command.startswith("trialerror hook "), command
        assert "python" not in command, f"{command!r} names an interpreter again"
