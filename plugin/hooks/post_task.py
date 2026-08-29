#!/usr/bin/env python
"""PostToolUse: Task hook: appends a ``subagent_return`` event (ids only,
response size — never transcript content) after every subagent spawn.
Design Section 5.4 (PostToolUse row): "appends a subagent_return event
(ids only, sizes, duration); flags RUNNING launches for reconciliation."

TRIALERROR-DEV-NOTE (duration, "flags ... for reconciliation"): Claude Code's
PostToolUse payload carries no start-time/duration field to compute a
duration from (unlike ``tool_input``/``tool_response``, which it does
carry) — this script records ``None`` for it rather than fabricating a
value. "Flags RUNNING launches for reconciliation" is satisfied
STRUCTURALLY, not by a new schema state: a launch this hook just returned
from is, by construction, either already ``RECONCILED`` or still
``RUNNING`` — and a still-``RUNNING`` launch is exactly what
``trialerror.sessions.lifecycle.evaluate_close_readiness``'s dangling-launch
check (Stop hook + ``session close``) and
``trialerror.budget.checks.check_budget_dangling_launches`` (TTL-based, doctor)
already surface. This event's job is the audit-trail entry (a durable
record that a return happened, with the response's ids/size), not a new
detection mechanism.

Always exits 0 — PostToolUse fires AFTER the tool already ran; this hook
is pure observability and must never fail closed on a logging problem.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _evaluate(payload: dict) -> str | None:
    """Returns a stderr diagnostic, or ``None`` on success/no-op. Kept
    separate from ``main()`` for direct testing, mirroring
    ``plugin/hooks/spawn_gate.py``'s ``_evaluate``."""
    tool_name = payload.get("tool_name")
    if tool_name != "Task":
        return None

    tool_input = payload.get("tool_input") or {}
    prompt_text = tool_input.get("prompt") or tool_input.get("description") or ""
    tool_response = payload.get("tool_response")
    cwd = payload.get("cwd") or "."

    from trialerror.budget.gate import extract_launch_id_token, resolve_open_session
    from trialerror.events.api import append_event, record_hook_alive_once
    from trialerror.stores.store import open_store
    from trialerror.util.config import find_program_root

    program_root = find_program_root(cwd) or Path(cwd)
    try:
        store = open_store(program_root)
    except Exception as exc:  # noqa: BLE001
        return f"post_task: could not open program stores at {program_root}: {exc}"

    try:
        launch_id = extract_launch_id_token(prompt_text)
        session = resolve_open_session(store)
        # FX-8 (C-0064): payload.hook == "post_task" -- distinct from
        # session_start.py's "session_start" and spawn_gate.py's own
        # "spawn_gate" marker (see trialerror.events.api.record_hook_alive_once).
        record_hook_alive_once(
            store, session_id=session["session_id"] if session is not None else None, hook_name="post_task"
        )
        response_size = len(json.dumps(tool_response, ensure_ascii=False)) if tool_response is not None else 0
        append_event(
            store,
            event_type="subagent_return",
            session_id=session["session_id"] if session is not None else None,
            launch_id=launch_id,
            payload={"response_size_bytes": response_size, "duration_ms": None},
        )
        return None
    finally:
        store.close()


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    try:
        diagnostic = _evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - observability only, must never block
        print(f"post_task: internal error: {exc}", file=sys.stderr)
        return 0

    if diagnostic:
        print(diagnostic, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
