"""Giving a booking back when its spawn never started an agent -- and only then.

The spawn gate moves a booking PROVISIONAL -> RUNNING *before* Claude Code
checks the spawn's agent name and tools. A spawn that fails at that check has
no subagent behind it, and its booking would stay RUNNING for good. This
module holds the two decisions the ``spawn-failure`` hook and the
``budget release`` verb share:

* :func:`search_started_agent` -- did a subagent start for this spawn? A
  started subagent leaves ``<spawn_transcript_dir>/subagents/agent-<id>.meta.json``
  whose ``toolUseId`` equals the spawn's ``tool_use_id``. Only the top level
  of ``subagents/`` is read: the ``workflows/`` folders under it hold files
  that never carry a ``toolUseId``.
* :func:`release_running_launch` -- the one conditional UPDATE that moves
  RUNNING -> PROVISIONAL and clears the spawn identity.

**When in doubt, nothing is released.** A subagent that ran has a real cost;
zeroing it would hide it. So a search that ran out of time, or met a file it
could not read, reports ``complete=False`` and the caller must not release.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AgentSearch",
    "search_started_agent",
    "release_running_launch",
    "mark_failure_after_start",
    "SEARCH_SLACK_S",
]

#: A meta.json older than the spawn by more than this cannot belong to it.
SEARCH_SLACK_S = 60.0

#: Read at call time, so a test can stub the clock.
_clock = time.monotonic

_META_READ_LIMIT = 262144


@dataclass(frozen=True)
class AgentSearch:
    """``found`` is the matching meta.json's path, or ``None``. ``complete``
    is true only when the search finished inside its budget and read every
    candidate file it looked at. A caller may treat ``found is None`` as
    "no agent started" only when ``complete`` is true."""

    found: str | None
    complete: bool


def search_started_agent(
    spawn_transcript_dir: str,
    tool_use_id: str,
    *,
    not_before: float | None = None,
    budget_s: float,
) -> AgentSearch:
    """Look for the meta.json of the subagent a spawn started.

    ``not_before`` (epoch seconds), when given, skips files whose mtime is
    earlier. The caller passes ``spawn_ts - SEARCH_SLACK_S``, never
    ``booked_ts``: a heartbeat moves ``booked_ts`` to a later moment than the
    spawn, and a filter built on it would skip the very file being sought.
    """
    subagents = os.path.join(spawn_transcript_dir, "subagents")
    deadline = _clock() + budget_s
    complete = True
    try:
        scan = os.scandir(subagents)
    except (FileNotFoundError, NotADirectoryError):
        # No subagents folder: nothing ever started under this session.
        return AgentSearch(found=None, complete=True)
    except OSError:
        return AgentSearch(found=None, complete=False)
    with scan:
        while True:
            if _clock() > deadline:
                return AgentSearch(found=None, complete=False)
            try:
                entry = next(scan)
            except StopIteration:
                break
            except OSError:
                return AgentSearch(found=None, complete=False)
            name = entry.name
            if not (name.startswith("agent-") and name.endswith(".meta.json")):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not_before is not None and entry.stat(follow_symlinks=False).st_mtime < not_before:
                    continue
                with open(entry.path, "rb") as fh:
                    data = json.loads(fh.read(_META_READ_LIMIT).decode("utf-8", errors="replace"))
            except (OSError, ValueError):
                complete = False
                continue
            if isinstance(data, dict) and data.get("toolUseId") == tool_use_id:
                return AgentSearch(found=entry.path, complete=True)
    return AgentSearch(found=None, complete=complete)


def _attrs(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def release_running_launch(
    conn: sqlite3.Connection,
    launch_id: str,
    *,
    spawn_tool_use_id: str | None,
    attrs_key: str,
    entry: dict[str, Any],
) -> bool:
    """RUNNING -> PROVISIONAL in one conditional UPDATE. Clears the four
    spawn-identity columns and appends ``entry`` to ``attrs[attrs_key]``.
    Returns whether a row moved.

    The WHERE clause names the launch, ``RUNNING`` and the spawn's own
    ``tool_use_id`` (``IS`` so a launch gated before the identity columns
    existed matches its NULL), so a launch a later spawn has consumed again,
    or one already reconciled, is left alone. The pool's commitment is
    unchanged: PROVISIONAL and RUNNING both count as committed."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT attrs FROM launch WHERE launch_id = ? AND state = 'RUNNING' AND spawn_tool_use_id IS ?",
            (launch_id, spawn_tool_use_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return False
        attrs = _attrs(row[0])
        history = attrs.get(attrs_key)
        attrs[attrs_key] = (history if isinstance(history, list) else []) + [entry]
        cur = conn.execute(
            "UPDATE launch SET state = 'PROVISIONAL', spawn_tool_use_id = NULL, spawn_ts = NULL, "
            "spawn_transcript_dir = NULL, agent_id = NULL, attrs = ? "
            "WHERE launch_id = ? AND state = 'RUNNING' AND spawn_tool_use_id IS ?",
            (json.dumps(attrs, ensure_ascii=False), launch_id, spawn_tool_use_id),
        )
        moved = cur.rowcount == 1
        conn.execute("COMMIT")
        return moved
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def mark_failure_after_start(
    conn: sqlite3.Connection,
    launch_id: str,
    *,
    spawn_tool_use_id: str,
    record: dict[str, Any],
) -> bool:
    """Leave the launch RUNNING and note on ``attrs.spawn_failure_after_start``
    that its spawn failed although an agent may have started. Its real cost is
    filled from its transcript later."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT attrs FROM launch WHERE launch_id = ? AND state = 'RUNNING' AND spawn_tool_use_id = ?",
            (launch_id, spawn_tool_use_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return False
        attrs = _attrs(row[0])
        attrs["spawn_failure_after_start"] = record
        conn.execute(
            "UPDATE launch SET attrs = ? WHERE launch_id = ? AND state = 'RUNNING' AND spawn_tool_use_id = ?",
            (json.dumps(attrs, ensure_ascii=False), launch_id, spawn_tool_use_id),
        )
        conn.execute("COMMIT")
        return True
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
