"""``PostToolUseFailure`` hook for the subagent tool: record the failure, and
give the booking back only when the spawn never started an agent.

Wired as ``trialerror hook spawn-failure`` in ``plugin/hooks/hooks.json``.

The spawn gate (:mod:`trialerror.hooks.spawn_gate`) books a launch as RUNNING
before Claude Code has checked the spawn's agent name and tools. A spawn that
fails at that check leaves a RUNNING launch with no agent behind it. This
hook returns such a launch to PROVISIONAL, so the corrected retry passes the
gate with the same booking while its time-to-live has not run out.

**A subagent that ran is never zeroed.** The release happens only when every
one of these holds (see :func:`_evaluate`):

* the launch carries the gate's own ``spawn_ts`` and ``spawn_transcript_dir``
  (a launch gated before those columns existed is never released here);
* the search for a subagent ``meta.json`` carrying this spawn's ``tool_use_id``
  finished inside its budget, read every file it looked at, and found none;
* the failure is not an interrupt;
* the error class is a known "refused before it started" class, never
  ``other`` and never ``interrupted``.

Anything else leaves the launch RUNNING and notes
``attrs.spawn_failure_after_start``: its real cost is filled from its
transcript later.

**Never blocks, always exits 0** (hooks never break a session). It prints
nothing, except one stderr line naming the exception type when it fails.
**Never stores message text:** the ``error`` field may carry a subagent's own
output, so only a class and a length are kept, and ``tool_input.prompt`` is
never read into a record.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from trialerror.hooks import SUBAGENT_TOOL_NAMES

__all__ = ["main", "classify_error"]

#: How long the "did an agent start" search may take inside the hook.
SEARCH_BUDGET_S = 0.2

#: The store connections wait this long on a lock; the hook's whole target is
#: under one second.
BUSY_TIMEOUT_MS = 1000

#: Classes for which a spawn is known to have been refused before any agent
#: started, and only for texts Claude Code was seen to print. ``other`` and
#: ``interrupted`` are deliberately not among them. A permission denial fires
#: no failure hook, and a pattern that matches free text could release a spawn
#: that ran, so neither is here: they fall to ``budget release``.
_RELEASABLE_CLASSES = frozenset({"agent_type_unknown", "zero_tools"})

#: Anchored shapes of the error text, tried in order. Every releasable one was
#: observed: the unknown-name refusal live, the zero-tools refusal in Claude
#: Code's own message. A text that matches none is ``other``, and ``other`` is
#: never released.
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("agent_type_unknown", re.compile(r"Agent type '[^']+' not found\.")),
    ("zero_tools", re.compile(r"Agent '[^']+' would be spawned with zero tools\b")),
    ("interrupted", re.compile(r"\s*\[Request interrupted\b", re.IGNORECASE)),
)


def classify_error(error: Any, is_interrupt: Any = None) -> str:
    """One of ``agent_type_unknown``, ``zero_tools``, ``interrupted``,
    ``other``, from anchored prefixes of the error text. The text is read,
    never kept."""
    if is_interrupt is True:
        return "interrupted"
    if not isinstance(error, str):
        return "other"
    head = error[:200]
    for label, pattern in _ERROR_PATTERNS:
        if pattern.match(head):
            return label
    return "other"


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _record(payload: dict, *, error_class: str) -> None:
    from trialerror.hooks.probe_log import append_hook_record

    tool_input = payload.get("tool_input")
    error = payload.get("error")
    append_hook_record(
        payload,
        hook="spawn_failure",
        extra={
            "is_interrupt": _bool_or_none(payload.get("is_interrupt")),
            "run_in_background": _bool_or_none(tool_input.get("run_in_background"))
            if isinstance(tool_input, dict)
            else None,
            "error_class": error_class,
            "error_len": len(error) if isinstance(error, str) else None,
        },
    )


def _append_event(program_root: Path, launch: Any, event_type: str, payload: dict[str, Any]) -> None:
    """Update-then-event across two database files: the launch is already
    written, so a failure here costs one stderr line and nothing else.

    Both files are opened directly, with the hook's 1 s busy timeout and no
    migration (``open_store`` would connect with the 10 s default first). The
    program's stores must already exist: the gate created them."""
    from trialerror.events.api import append_event
    from trialerror.stores import paths
    from trialerror.stores.connection import connect
    from trialerror.stores.store import Store, _auto_load_paths_config

    ops_path = paths.ops_db_path(program_root, _auto_load_paths_config(program_root))
    if not ops_path.is_file():
        raise FileNotFoundError("the program's ops store does not exist")
    platform_conn = connect(paths.platform_db_path(), busy_timeout_ms=BUSY_TIMEOUT_MS)
    ops_conn = connect(ops_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    # Only the event table is written; its cross-file references are to platform.launch.
    store = Store(
        platform=platform_conn,
        ops=ops_conn,
        knowledge=ops_conn,
        jobs=ops_conn,
        program_root=program_root,
        platform_root=paths.platform_root(),
    )
    try:
        append_event(
            store,
            event_type=event_type,
            session_id=launch["session_id"],
            launch_id=launch["launch_id"],
            payload=payload,
        )
    finally:
        platform_conn.close()
        ops_conn.close()


def _evaluate(payload: dict) -> None:
    """Record, then (only when safe) release. Never raises to the caller's
    caller: :func:`main` swallows anything that escapes."""
    from trialerror.budget import spawn_release
    from trialerror.stores import paths
    from trialerror.stores.connection import connect
    from trialerror.util.config import find_program_root
    from trialerror.util.timeutil import now, parse

    is_interrupt = _bool_or_none(payload.get("is_interrupt"))
    error_class = classify_error(payload.get("error"), is_interrupt)

    # 1. Record (ids, flags, a class and a length -- never the text).
    try:
        _record(payload, error_class=error_class)
    except Exception:  # noqa: BLE001 - a probe-log failure must not stop the release
        pass

    if payload.get("tool_name") not in SUBAGENT_TOOL_NAMES:
        return
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return
    tool_input = payload.get("tool_input")
    run_in_background = (
        _bool_or_none(tool_input.get("run_in_background")) if isinstance(tool_input, dict) else None
    )

    # 2. Find the launch this spawn consumed. No platform store, or no
    #    booking carrying this tool_use_id (a spawn the gate did not book, or
    #    a gate older than the identity columns): nothing to do.
    db_path = paths.platform_db_path()
    if not db_path.is_file():
        return
    conn = connect(db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    try:
        launch = conn.execute(
            "SELECT launch_id, session_id, spawn_ts, spawn_transcript_dir "
            "FROM launch WHERE spawn_tool_use_id = ? AND state = 'RUNNING'",
            (tool_use_id,),
        ).fetchone()
        if launch is None:
            return

        # 3. Did an agent start?
        meta_found: bool | None = None
        search_complete = False
        spawn_ts_epoch: float | None = None
        spawn_dir = launch["spawn_transcript_dir"]
        if launch["spawn_ts"]:
            try:
                spawn_ts_epoch = parse(launch["spawn_ts"]).timestamp()
            except (ValueError, TypeError):
                spawn_ts_epoch = None
        can_search = spawn_ts_epoch is not None and isinstance(spawn_dir, str) and os.path.isabs(spawn_dir)
        if can_search:
            result = spawn_release.search_started_agent(
                spawn_dir,
                tool_use_id,
                not_before=spawn_ts_epoch - spawn_release.SEARCH_SLACK_S,
                budget_s=SEARCH_BUDGET_S,
            )
            meta_found = result.found is not None
            search_complete = result.complete

        # 4. Release only when nothing says an agent may have started.
        ts = now()
        releasable = (
            can_search
            and search_complete
            and meta_found is False
            and is_interrupt is not True
            and error_class in _RELEASABLE_CLASSES
        )
        event_payload = {
            "launch_id": launch["launch_id"],
            "tool_use_id": tool_use_id,
            "error_class": error_class,
            "meta_found": meta_found,
            "search_complete": search_complete,
            "is_interrupt": is_interrupt,
            "run_in_background": run_in_background,
        }
        if releasable:
            moved = spawn_release.release_running_launch(
                conn,
                launch["launch_id"],
                spawn_tool_use_id=tool_use_id,
                attrs_key="spawn_failures",
                entry={
                    "ts": ts,
                    "tool_use_id": tool_use_id,
                    "error_class": error_class,
                    "run_in_background": run_in_background,
                },
            )
            event_type = "launch_spawn_released"
            if not moved:
                return
        else:
            # 5. Do not release: note it, leave the launch RUNNING.
            spawn_release.mark_failure_after_start(
                conn,
                launch["launch_id"],
                spawn_tool_use_id=tool_use_id,
                record={
                    "ts": ts,
                    "tool_use_id": tool_use_id,
                    "error_class": error_class,
                    "meta_found": meta_found,
                    "search_complete": search_complete,
                    "is_interrupt": is_interrupt,
                },
            )
            event_type = "launch_spawn_failed_after_start"
        launch_row = {"launch_id": launch["launch_id"], "session_id": launch["session_id"]}
    finally:
        conn.close()

    # 6. One event in the program's ops store, after the launch is written.
    try:
        cwd = payload.get("cwd")
        program_root = find_program_root(cwd if isinstance(cwd, str) and cwd else ".") or Path(
            cwd if isinstance(cwd, str) and cwd else "."
        )
        _append_event(program_root, launch_row, event_type, event_payload)
    except Exception as exc:  # noqa: BLE001 - the launch update stands
        print(f"spawn_failure: could not write the {event_type} event: {type(exc).__name__}", file=sys.stderr)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
    except Exception:  # noqa: BLE001
        return 0
    try:
        _evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - never break a session
        # One line, the exception's type only (its message could carry text
        # this hook must not keep). The exit code and the spawn are untouched.
        print(f"spawn_failure: internal error: {type(exc).__name__}", file=sys.stderr)
    return 0
