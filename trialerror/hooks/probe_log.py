"""Shared machinery for appending one record-only line to
``~/.trialerror/probes/hook_events.jsonl`` (design Section 2.4). Used by both
:mod:`trialerror.hooks.subagent_probe` (``SubagentStart``/``SubagentStop``)
and :mod:`trialerror.hooks.session_start` -- design Section 3.3's
``hook_payload_keys`` row: "SessionStart has no record there yet: add one
line to session_start.py that records its payload's key names the same
way."

**Never records anything but key names, ids and the CC version** (trap 2):
this file is read directly by ``trialerror probes run``'s
``hook_payload_keys`` conformance probe (:mod:`trialerror.units.probes`), so
whatever it does NOT capture here is a value that probe can never see either
-- prompt text, tool inputs, and every other payload value are simply never
looked at, not merely filtered afterward.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from trialerror.util.timeutil import now

__all__ = ["probes_dir", "hook_events_path", "ROTATE_BYTES", "first", "append_hook_record"]

#: design Section 2.4: "Rotate the file at 10 MB, keeping one .1."
ROTATE_BYTES = 10 * 1024 * 1024

_PROBES_DIR_ENV = "TRIALERROR_PROBES_DIR"


def probes_dir() -> Path:
    """A MACHINE-wide directory, not a program one (design Section 2.4: "The
    file is per machine, not per program: te-* lanes have no program
    root")."""
    override = os.environ.get(_PROBES_DIR_ENV)
    return Path(override) if override else Path.home() / ".trialerror" / "probes"


def hook_events_path() -> Path:
    return probes_dir() / "hook_events.jsonl"


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= ROTATE_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            try:
                rotated.unlink()
            except OSError:
                pass
            path.replace(rotated)
    except OSError:
        pass


def first(payload: dict, *names: str) -> Any:
    """The first of ``names`` present as a top-level key in ``payload``, or
    ``None``. Read under BOTH a snake_case and Claude Code's known camelCase
    spelling (``meta.json``'s own convention) where a field name's real
    spelling in a given hook payload is unverified -- see
    :mod:`trialerror.hooks.subagent_probe`'s module docstring."""
    for name in names:
        if name in payload:
            return payload[name]
    return None


def append_hook_record(payload: dict, *, hook: str, extra: dict[str, Any] | None = None) -> None:
    """Append one JSONL line: ``{ts, hook, cc_version, keys, session_id,
    agent_id, agent_type, tool_use_id, **extra}``. Raises on a filesystem
    failure (an unwritable probes directory, a disk full) -- callers wrap
    this in their own hook's swallow-everything boundary (trap 1: a probe
    log write must never be allowed to break a session)."""
    record: dict[str, Any] = {
        "ts": now(),
        "hook": hook,
        "cc_version": first(payload, "cc_version", "version"),
        "keys": sorted(str(k) for k in payload.keys()),
        "session_id": first(payload, "session_id"),
        "agent_id": first(payload, "agent_id", "agentId"),
        "agent_type": first(payload, "agent_type", "agentType"),
        "tool_use_id": first(payload, "tool_use_id", "toolUseId"),
    }
    if extra:
        record.update(extra)

    path = hook_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
