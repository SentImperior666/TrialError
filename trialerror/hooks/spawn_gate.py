"""PreToolUse hook: the physical spawn gate. Design Section 5.4 (PreToolUse:
Task row) + Section 1 commitment 1 ("Enforcement over convention ...
Budget-at-spawn is a PreToolUse hook that refuses an unbooked `Task`
call... Nothing load-bearing is a prompt.").

Claude Code invokes this hook (via ``trialerror hook spawn-gate``, wired in
``plugin/hooks/hooks.json``; design Section 12's M6 row used to say "hook
command lines invoke `python` explicitly (Windows)", which is exactly the
wiring that failed with exit 127 on Linux -- see :mod:`trialerror.hooks`)
for every tool call the plugin's hook configuration matches
against; it receives one JSON object on stdin (the PreToolUse payload:
``session_id``, ``cwd``, ``hook_event_name``, ``tool_name``, ``tool_input``,
...) and communicates its verdict purely through the process exit code,
per Claude Code's hook protocol:

- exit 0 -> the tool call proceeds.
- exit 2 -> the tool call is BLOCKED; stderr is surfaced back to the agent
  as the refusal reason (design: "exit 2 (spawn REFUSED) with the exact
  `trialerror budget book` command to run").

All decision logic lives in :mod:`trialerror.budget.gate` (pytest imports and
calls it directly, with no stdin/subprocess involved - design Section 12 M3
row: "live-CC hook tests are orchestrator-executed integration items", i.e.
only the ACTUAL live-Claude-Code round trip needs a real session; the gate
logic itself is unit-tested here). This file is deliberately thin: parse
stdin, resolve the program/platform roots, call the gate, translate the
verdict to an exit code.

TRIALERROR-DEV-NOTE (matcher wiring): this hook assumes it is only invoked for
``tool_name in SUBAGENT_TOOL_NAMES`` (see :mod:`trialerror.hooks`).
``plugin/hooks/hooks.json`` now enforces that with a ``^(Task|Agent)$``
``PreToolUse`` matcher, so the assumption holds under the shipped
manifest. Belt and braces, THIS module still defends itself: any
``tool_name`` outside that set (or a payload it can't parse at all)
passes through (exit 0) rather than guessing - see ``_evaluate`` below.
Confirming that Claude Code actually applies the matcher, rather than
merely that the fast path works, remains a live-session item (see
``tests/acceptance/test_gpu_and_live_cc_journeys.py``).

TRIALERROR-DEV-NOTE (Task->Agent rename, found 2026-09-05): this docstring
used to say the matcher, and this module's own comparison, were
``"Task"``-only. Live evidence on the sandbox host (03:34Z, the sandbox container
container) showed Claude Code 2.1.261 invoking the subagent tool as
``Agent`` rather than ``Task`` -- the old ``tool_name != "Task"`` check
silently let every real spawn through unbooked (no refusal, no
``hook_alive{hook=spawn_gate}`` row). Both the manifest matcher above and
the comparison below now gate on ``SUBAGENT_TOOL_NAMES = ("Task", "Agent")``
(:mod:`trialerror.hooks`) so a future rename of either name is a one-line
fix in one place instead of a repeat of this incident.

TRIALERROR-DEV-NOTE (cwd assumption): ``trialerror.util.config.find_program_root``
walks up from the hook payload's ``cwd`` looking for ``trialerror.toml``. This
assumes Claude Code's hook cwd is inside (or at) the program scaffold - 
true once M6's session boot ritual is the thing that launched the session,
not guaranteed for an ad-hoc hook invocation. Flagged for the M6 builder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from trialerror.hooks import SUBAGENT_TOOL_NAMES


def _evaluate(payload: dict) -> tuple[int, str | None]:
    """Returns ``(exit_code, stderr_message)``. Kept separate from
    ``main()`` so a test can call it directly with a crafted payload dict
    instead of piping JSON through a real stdin/subprocess."""
    tool_name = payload.get("tool_name")
    if tool_name not in SUBAGENT_TOOL_NAMES:
        return 0, None

    tool_input = payload.get("tool_input") or {}
    prompt_text = tool_input.get("prompt") or tool_input.get("description") or ""
    if not prompt_text and tool_input:
        # TRIALERROR-DEV-NOTE (tool_input schema assumption, FU-11 verification
        # finding FU11-V5): the Task->Agent rename above proved the tool NAME
        # is not stable across Claude Code versions; the shape of tool_input
        # (that a subagent call carries "prompt" or "description") is an
        # equally unverified assumption. If a future rename moves the prompt
        # text under some other key, fall back to scanning the WHOLE
        # serialized tool_input for the `launch_id:` token rather than
        # treating the call as promptless -- cheap hardening, not a full fix
        # (a live Claude Code session must still confirm the real key name).
        prompt_text = json.dumps(tool_input, ensure_ascii=False)
    cwd = payload.get("cwd") or "."

    # Deferred imports: keep import cost off the (much more common)
    # not-a-subagent-call fast path above.
    from trialerror.budget.gate import evaluate_spawn_for_open_session, resolve_open_session
    from trialerror.events.api import record_hook_alive_once
    from trialerror.stores.store import open_store
    from trialerror.util.config import ConfigError, find_program_root, load_config

    program_root = find_program_root(cwd) or Path(cwd)

    policy: dict[str, str] | None = None
    try:
        config = load_config(program_root / "trialerror.toml")
        policy = dict(config.models) if config.models else None
    except ConfigError:
        policy = None

    try:
        store = open_store(program_root)
    except Exception as exc:  # noqa: BLE001 - this IS a subagent spawn call; cannot verify -> fail CLOSED
        return 2, f"spawn gate: could not open program stores at {program_root}: {exc}"

    try:
        # FX-8 (C-0064): a hook_alive row with payload.hook == "spawn_gate"
        # -- distinct from session_start.py's own "session_start" value --
        # is the physical liveness marker close_session's hooks_partial
        # check needs (see trialerror.sessions.lifecycle's own TRIALERROR-DEV-NOTE).
        # Recorded regardless of the gate's verdict below, same "the hook
        # fired" posture session_start.py's module docstring states.
        session = resolve_open_session(store)
        record_hook_alive_once(
            store, session_id=session["session_id"] if session is not None else None, hook_name="spawn_gate"
        )
        result = evaluate_spawn_for_open_session(store, prompt_text, policy=policy)
    finally:
        store.close()

    if result.allowed:
        return 0, None

    msg = f"SPAWN REFUSED [{result.code}]: {result.message}"
    if result.next_command:
        msg += "\nfix: " + " ".join(result.next_command)
    return 2, msg


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        # Can't even parse the hook payload -> can't tell if this is a
        # subagent spawn call -> pass through rather than blocking every tool.
        return 0

    try:
        code, message = _evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - an unexpected bug in the gate must not fail OPEN
        print(f"spawn gate: internal error evaluating spawn: {exc}", file=sys.stderr)
        return 2

    if message:
        print(message, file=sys.stderr)
    return code

