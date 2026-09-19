"""SessionStart hook: injects the boot bundle as pre-loaded context and
records a ``hook_alive`` event proving hooks are armed this session.
Design Section 5.4 (SessionStart row): "injects boot bundle (pin status,
open session, dangling launches, inbox count, budget headroom, L0 memory
index) with the 'pre-loaded — do not re-fetch' instruction; records a
hook_alive event (close checks it ...)." Wired as ``trialerror hook
session-start`` in ``plugin/hooks/hooks.json``; design Section 12's M6 row
used to say "hook command lines invoke `python` explicitly (Windows)",
which is the wiring that failed with exit 127 on a stock Linux box -- see
:mod:`trialerror.hooks`.

Claude Code invokes this hook for every ``SessionStart`` event (session
start, ``/clear``, ``/compact``, resume — see the ``source`` field on the
stdin payload) with one JSON object on stdin
(``session_id``, ``cwd``, ``hook_event_name``, ``source``, ...). Output
protocol: a JSON object on stdout shaped
``{"hookSpecificOutput": {"hookEventName": "SessionStart",
"additionalContext": "..."}}`` is folded into the new session's context;
SessionStart has no blocking exit code in Claude Code's hook protocol, so
this script ALWAYS exits 0 — a failure degrades to "no injected context,
diagnostic on stderr", never to a blocked session start (mirrors
``plugin/hooks/spawn_gate.py``'s "deliberately thin adapter" shape: all
decision logic lives in :mod:`trialerror.sessions.lifecycle`, importable and
unit-tested directly — design Section 12 M6 row: "live-CC test =
orchestrator-executed integration item").

**hook_alive is recorded even when boot itself could not complete**
(e.g. an ambiguous multi-account bootstrap with no ``--account`` given):
the event's whole purpose is to prove HOOKS fired this session, which is
true regardless of whether the boot ritual itself succeeded — a session
close later checking "were hooks armed" must not be confused by an
unrelated boot-time refusal.

TRIALERROR-DEV-NOTE (cwd assumption, inherited from ``spawn_gate.py``'s own
note): ``find_program_root`` walks up from the hook payload's ``cwd``;
this assumes Claude Code's hook cwd is inside the program scaffold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _evaluate(payload: dict) -> tuple[dict | None, str | None]:
    """Returns ``(hook_output_dict_or_None, stderr_diagnostic_or_None)``.
    Kept separate from ``main()`` so a test can call it directly with a
    crafted payload dict instead of piping JSON through a real
    stdin/subprocess (mirrors ``plugin/hooks/spawn_gate.py``'s
    ``_evaluate``)."""
    cwd = payload.get("cwd") or "."

    from trialerror.events.api import append_event
    from trialerror.sessions.lifecycle import boot_session
    from trialerror.stores.store import open_store
    from trialerror.util.config import find_program_root

    program_root = find_program_root(cwd) or Path(cwd)
    try:
        store = open_store(program_root)
    except Exception as exc:  # noqa: BLE001 - SessionStart must never crash the session
        return None, f"session_start: could not open program stores at {program_root}: {exc}"

    try:
        result = boot_session(store, reuse_open=True)
        session_id = result.session_id  # may be None if boot itself refused (e.g. ambiguous account)

        # Recorded regardless of `result.ok` -- see module docstring.
        append_event(store, event_type="hook_alive", session_id=session_id, payload={"hook": "session_start"})

        if not result.ok:
            return None, f"session_start: boot did not complete ({result.code}): {result.message}"

        context_text = (
            f"[trialerror session boot] session {session_id} booted for account "
            f"{result.account_id}. Boot bundle (pre-loaded — do not re-fetch):\n\n"
            + json.dumps(result.bundle, ensure_ascii=False, indent=2)
        )
        return (
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context_text}},
            None,
        )
    finally:
        store.close()


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    try:
        output, diagnostic = _evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - SessionStart must never crash the session
        print(f"session_start: internal error: {exc}", file=sys.stderr)
        return 0

    if diagnostic:
        print(diagnostic, file=sys.stderr)
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0

