"""``trialerror hook subagent-start``/``subagent-stop`` -- design Section 2.4.
Record-only: keys and ids, never prompt/tool-input text; always exit 0; the
file rotates at 10 MB; a platform directory that cannot be written must never
break the hook (trap 1).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.hooks import subagent_probe


@pytest.fixture(autouse=True)
def probes_dir(tmp_path, monkeypatch):
    d = tmp_path / "probes"
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(d))
    return d


def _run(monkeypatch, payload: dict, which: str) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return subagent_probe.main_start() if which == "start" else subagent_probe.main_stop()


def _read_lines(probes_dir) -> list[dict]:
    path = probes_dir / "hook_events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_subagent_start_records_keys_and_ids_never_the_prompt(monkeypatch, probes_dir):
    payload = {
        "hook_event_name": "SubagentStart",
        "session_id": "SESS-1",
        "agent_id": "abc123",
        "agentType": "general-purpose",
        "prompt": "do not leak this text",
        "tool_input": {"prompt": "also secret"},
    }
    rc = _run(monkeypatch, payload, "start")
    assert rc == 0
    lines = _read_lines(probes_dir)
    assert len(lines) == 1
    line = lines[0]
    assert line["hook"] == "subagent_start"
    assert line["session_id"] == "SESS-1"
    assert line["agent_id"] == "abc123"
    assert line["agent_type"] == "general-purpose"
    assert set(line["keys"]) == set(payload.keys())
    dumped = json.dumps(line)
    assert "do not leak this text" not in dumped
    assert "also secret" not in dumped
    assert "prompt" not in line
    assert "tool_input" not in line


def test_subagent_stop_records_transcript_path_existence(monkeypatch, probes_dir, tmp_path):
    transcript = tmp_path / "agent-abc123.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    payload = {
        "hook_event_name": "SubagentStop",
        "session_id": "SESS-1",
        "agent_id": "abc123",
        "agent_transcript_path": str(transcript),
    }
    rc = _run(monkeypatch, payload, "stop")
    assert rc == 0
    line = _read_lines(probes_dir)[0]
    assert line["hook"] == "subagent_stop"
    assert line["agent_transcript_path_exists"] is True
    assert line["agent_transcript_path_size"] == transcript.stat().st_size


def test_subagent_stop_missing_transcript_is_false(monkeypatch, probes_dir):
    payload = {"hook_event_name": "SubagentStop", "session_id": "SESS-1", "agent_transcript_path": "/no/such/file"}
    _run(monkeypatch, payload, "stop")
    line = _read_lines(probes_dir)[0]
    assert line["agent_transcript_path_exists"] is False
    assert "agent_transcript_path_size" not in line


def test_absent_fields_are_recorded_as_null(monkeypatch, probes_dir):
    _run(monkeypatch, {"hook_event_name": "SubagentStart"}, "start")
    line = _read_lines(probes_dir)[0]
    assert line["session_id"] is None
    assert line["agent_id"] is None
    assert line["agent_type"] is None
    assert line["tool_use_id"] is None
    assert line["cc_version"] is None


def test_malformed_stdin_still_exits_zero(monkeypatch, probes_dir):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
    rc = subagent_probe.main_start()
    assert rc == 0


def test_empty_stdin_still_exits_zero(monkeypatch, probes_dir):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = subagent_probe.main_stop()
    assert rc == 0


def test_an_unwritable_probes_directory_never_breaks_the_hook(monkeypatch, tmp_path):
    """Trap 1: a hook must never break a session. Point TRIALERROR_PROBES_DIR
    at a path whose parent is a plain FILE, so mkdir(parents=True) cannot
    possibly succeed -- and still exit 0."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(blocker / "probes"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "S"})))
    rc = subagent_probe.main_start()
    assert rc == 0


def test_an_unwritable_probes_directory_never_breaks_subagent_stop(monkeypatch, tmp_path):
    """N-7 fix round: the review covered subagent_stop and session_start by
    reading the code, not by running them, since the existing test only
    covered subagent_start. Same shape, the other handler."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(blocker / "probes"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "S", "agent_id": "A"})))
    rc = subagent_probe.main_stop()
    assert rc == 0


def test_an_unwritable_probes_directory_never_breaks_session_start(tmp_path):
    """N-7 fix round: session_start.py's own _record_hook_keys call sits in
    the same try/except: pass boundary -- proven here by actually running
    it, not just reading the code."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    script = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "session_start.py"
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    env["TRIALERROR_PROBES_DIR"] = str(blocker / "probes")
    tree_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([tree_root, env["PYTHONPATH"]]) if env.get("PYTHONPATH") else tree_root
    payload = {"hook_event_name": "SessionStart", "session_id": "SESS-1", "cwd": str(program_root), "source": "startup"}
    proc = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr


def test_the_file_rotates_at_ten_megabytes(monkeypatch, probes_dir):
    probes_dir.mkdir(parents=True, exist_ok=True)
    big = probes_dir / "hook_events.jsonl"
    big.write_bytes(b"x" * (subagent_probe._ROTATE_BYTES + 1))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "S"})))
    rc = subagent_probe.main_start()
    assert rc == 0
    rotated = probes_dir / "hook_events.jsonl.1"
    assert rotated.exists()
    assert rotated.stat().st_size == subagent_probe._ROTATE_BYTES + 1
    fresh_lines = _read_lines(probes_dir)
    assert len(fresh_lines) == 1


def test_cli_hook_group_registers_the_new_actions():
    from trialerror.cli import hook as hook_cli

    assert "subagent-start" in hook_cli._HOOKS
    assert "subagent-stop" in hook_cli._HOOKS
    assert "spawn-failure" in hook_cli._HOOKS


def test_session_start_also_records_a_hook_payload_keys_line(tmp_path, probes_dir):
    """design Section 3.3's hook_payload_keys row: SessionStart gets the
    same treatment as SubagentStart/SubagentStop. Runs the real script as a
    subprocess (session_start.py needs a program root and platform store to
    boot, unlike subagent_probe's two handlers)."""
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    script = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "session_start.py"
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    env["TRIALERROR_PROBES_DIR"] = str(probes_dir)
    # See tests/test_hooks_usage_capture.py's own note: a bare `import
    # trialerror` in the subprocess otherwise resolves to whatever copy pip
    # installed, which in a git worktree can be a DIFFERENT checkout.
    tree_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([tree_root, env["PYTHONPATH"]]) if env.get("PYTHONPATH") else tree_root
    payload = {"hook_event_name": "SessionStart", "session_id": "SESS-1", "cwd": str(program_root), "source": "startup"}
    proc = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    lines = _read_lines(probes_dir)
    assert any(line["hook"] == "session_start" and line["session_id"] == "SESS-1" for line in lines)


# ---------------------------------------------------------------------------
# S-6: the fast path -- design Section 2.4's "within 300 ms" budget
# ---------------------------------------------------------------------------


def _run_via_module(argv: list[str], *, payload: dict, probes_dir: Path) -> tuple[subprocess.CompletedProcess, float]:
    """``python -m trialerror.cli <argv>`` against THIS worktree's own code
    -- not the installed console script, which in a git worktree can
    resolve to a different checkout (see this file's other subprocess
    tests' own note)."""
    import time

    env = dict(os.environ)
    env["TRIALERROR_PROBES_DIR"] = str(probes_dir)
    tree_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([tree_root, env["PYTHONPATH"]]) if env.get("PYTHONPATH") else tree_root
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "trialerror.cli", *argv], input=json.dumps(payload), capture_output=True,
        text=True, env=env, timeout=30,
    )
    elapsed = time.perf_counter() - t0
    return proc, elapsed


def test_hook_subagent_start_is_fast_against_the_lean_path(probes_dir):
    """Loose timing assertion (review's own suggestion): under 1s, against
    a measured pre-fix cost of 650-700ms from discover_groups() alone. Not
    a tight budget test -- process startup and import time vary by
    machine -- just a guard that the CLI's full group-discovery path isn't
    silently back on the hot path."""
    proc, elapsed = _run_via_module(["hook", "subagent-start"], payload={"session_id": "S"}, probes_dir=probes_dir)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.0, f"hook subagent-start took {elapsed:.3f}s -- the CLI's fast path may not be engaging"


def test_hook_subagent_stop_is_fast_against_the_lean_path(probes_dir):
    proc, elapsed = _run_via_module(["hook", "subagent-stop"], payload={"session_id": "S"}, probes_dir=probes_dir)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.0, f"hook subagent-stop took {elapsed:.3f}s -- the CLI's fast path may not be engaging"


def test_the_fast_path_never_calls_discover_groups(monkeypatch, probes_dir):
    """The deterministic proof (the timing tests above are a loose,
    machine-speed-dependent secondary signal per the review's own
    suggestion): discover_groups() -- the ~650-700ms cost -- must never run
    at all for `hook <action>`, regardless of how fast that happens to be
    on any given machine."""
    import trialerror.cli as cli_module

    calls = []
    monkeypatch.setattr(cli_module, "discover_groups", lambda *a, **k: (calls.append(1), [])[1])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "S"})))
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["hook", "subagent-start"])
    assert exc_info.value.code == 0
    assert calls == [], "discover_groups() was called -- the fast path did not engage"


def test_the_fast_path_only_engages_for_the_exact_two_token_shape():
    """`hook` alone, `hook --help`, or a global flag before `hook` must all
    still go through the ordinary envelope-aware path -- only the exact
    `["hook", "<action>"]`` shape hooks.json actually invokes is fast-pathed."""
    from trialerror.cli import _HOOK_FAST_PATH_ACTIONS

    assert "subagent-start" in _HOOK_FAST_PATH_ACTIONS
    assert "subagent-stop" in _HOOK_FAST_PATH_ACTIONS
    assert "spawn-failure" in _HOOK_FAST_PATH_ACTIONS
    assert "--help" not in _HOOK_FAST_PATH_ACTIONS
