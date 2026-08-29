#!/usr/bin/env python
"""Stop hook: blocks stop ONCE with a checklist message if the open
session has dangling PROVISIONAL/RUNNING launches or a stale law pin;
allows a second stop immediately after. Design Section 5.4 (Stop row):
"blocks stop once with a checklist message (second stop allowed — never
traps the user)."

Claude Code's Stop hook protocol sets ``stop_hook_active: true`` on stdin
when this Stop cycle already blocked once before (the transcript's last
turn already came from a Stop-hook block) — precisely the mechanism that
makes "block once, never trap" achievable without this script needing to
track any state of its own: it simply always allows through when that
flag is set. Exit 2 blocks (stderr surfaced to the agent as the reason to
keep going); exit 0 allows the stop.

All decision logic (the {dangling launches, stale pin} check) lives in
:func:`trialerror.sessions.lifecycle.evaluate_close_readiness`, imported and
called directly — no subprocess, fully unit-testable without a live
Claude Code session (design Section 12 M6 row: "live-CC test =
orchestrator-executed integration item" applies only to the actual
stdin/exit-code round trip, exercised here via subprocess in
``tests/test_session_hooks.py``, mirroring
``tests/test_spawn_gate_hook.py``'s own precedent).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _evaluate(payload: dict) -> tuple[int, str | None]:
    if payload.get("stop_hook_active"):
        return 0, None

    cwd = payload.get("cwd") or "."

    from trialerror.budget.gate import resolve_open_session
    from trialerror.sessions.lifecycle import evaluate_close_readiness
    from trialerror.stores.store import open_store
    from trialerror.util.config import find_program_root

    program_root = find_program_root(cwd) or Path(cwd)
    try:
        store = open_store(program_root)
    except Exception:  # noqa: BLE001 - cannot verify -> do not trap the user
        return 0, None

    try:
        session = resolve_open_session(store)
        if session is None:
            return 0, None
        readiness = evaluate_close_readiness(store, session["session_id"])
        if readiness.ready:
            return 0, None
        checklist = "\n".join(f"  - {p}" for p in readiness.problems)
        message = (
            "STOP CHECKLIST (session not close-ready):\n"
            + checklist
            + "\n\nRun `trialerror session close` when ready, or stop again to override."
        )
        return 2, message
    finally:
        store.close()


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    try:
        code, message = _evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - Stop must fail open, never trap the user on a bug
        print(f"stop_check: internal error: {exc}", file=sys.stderr)
        return 0

    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
