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

from trialerror.util.config import ProgramRootIsHarnessError


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
        # check_same_thread=False: the canary probes this hook runs
        # (_run_canaries) execute on trialerror.probes.registry's own
        # per-probe timeout thread.
        store = open_store(program_root, check_same_thread=False)
    except Exception as exc:  # noqa: BLE001 - SessionStart must never crash the session
        return None, f"session_start: could not open program stores at {program_root}: {exc}"

    try:
        result = boot_session(store, reuse_open=True)
        session_id = result.session_id  # may be None if boot itself refused (e.g. ambiguous account)

        # Recorded regardless of `result.ok` -- see module docstring.
        append_event(store, event_type="hook_alive", session_id=session_id, payload={"hook": "session_start"})

        if not result.ok:
            return None, f"session_start: boot did not complete ({result.code}): {result.message}"

        degraded_line = _run_canaries(store, session_id=session_id)

        context_text = (
            f"[trialerror session boot] session {session_id} booted for account "
            f"{result.account_id}. Boot bundle (pre-loaded — do not re-fetch):\n\n"
            + json.dumps(result.bundle, ensure_ascii=False, indent=2)
        )
        if degraded_line:
            context_text = degraded_line + "\n\n" + context_text
        return (
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context_text}},
            None,
        )
    finally:
        store.close()


def _run_canaries(store, *, session_id: str | None) -> str | None:
    """design Section 3.4: the fast full-text canary runs synchronously at
    SessionStart (1.5s budget); the vector canary is enqueued as a
    background job, at most once per hour. Best-effort throughout -- a
    canary failure degrades to a context line, never a broken session.

    B-1 fix round: the full-text canary now runs through
    :func:`trialerror.probes.registry.run_probes` (not a direct call to
    ``probe_fulltext_canary``), so its result is actually RECORDED in
    ``probe_run`` (the row every answer stamp is built from) and the
    registry's own 1.5 s timeout applies (S-1) -- a direct call bypassed
    both. B-2: ``ProbeContext.program_id`` is set from this program's own
    config, so the recorded row -- and every later stamp that reads it --
    is scoped to THIS program, not averaged across every program a host
    happens to run."""
    try:
        from trialerror.probes.registry import ProbeContext, discover_and_register_probes, run_probes
        from trialerror.retrieve.handlers import enqueue_vector_canary_if_due
        from trialerror.retrieve.probes import fulltext_canary_degraded_line
        from trialerror.util.config import resolve_program_id

        discover_and_register_probes()
        host = _host_label()
        program_root = store.program_root
        program_id = resolve_program_id(program_root) if program_root is not None else None
        ctx = ProbeContext(
            host=host, platform_store=store, store=store,
            program_root=program_root, program_id=program_id,
        )
        rows = run_probes(ctx, names=["fulltext_canary"])
        row = rows[0] if rows else None
        degraded_line = fulltext_canary_degraded_line(row.status, row.detail) if row is not None else None
        enqueue_vector_canary_if_due(store, host=host)
        return degraded_line
    except Exception:  # noqa: BLE001 - canaries must never break session start
        return None


def _host_label() -> str:
    import socket

    try:
        return socket.gethostname() or "unknown-host"
    except OSError:
        return "unknown-host"


def _record_hook_keys(payload: dict) -> None:
    """L3 (design Section 3.3, ``hook_payload_keys`` row): "SessionStart has
    no record there yet: add one line to session_start.py that records its
    payload's key names the same way" every SubagentStart/SubagentStop
    record does (:mod:`trialerror.hooks.probe_log`). Best-effort and
    swallowed at the call site -- this must never affect session boot."""
    from trialerror.hooks.probe_log import append_hook_record

    append_hook_record(payload, hook="session_start")


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    try:
        _record_hook_keys(payload)
    except Exception:  # noqa: BLE001 - trap 1: must never break the session
        pass

    try:
        output, diagnostic = _evaluate(payload)
    except ProgramRootIsHarnessError:
        # cosmetic (review fix check, 2026-09-29): expected for a session
        # whose cwd is the harness checkout, not a bug -- a plain note, not
        # "internal error", matches the spawn gate's own wording for the
        # same case.
        print("session_start: no program root; skipping", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 - SessionStart must never crash the session
        print(f"session_start: internal error: {exc}", file=sys.stderr)
        return 0

    if diagnostic:
        print(diagnostic, file=sys.stderr)
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0

