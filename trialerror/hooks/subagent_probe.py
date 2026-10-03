"""SubagentStart / SubagentStop hooks: record-only conformance evidence for
F5's ``hook_payload_keys``/``subagent_stop_fires``/``subagent_transcript_
written`` probes (design Section 2.4). Wired as ``trialerror hook
subagent-start``/``subagent-stop`` in ``plugin/hooks/hooks.json`` -- there
were no ``SubagentStart``/``SubagentStop`` bindings before this lane (design
Section 1's harness pointers).

**Never blocks, never reads message text** (traps 1 and 2). Both handlers
read the payload's TOP-LEVEL KEY NAMES only (never a value that could be
prompt/response text; the write path is shared with
:mod:`trialerror.hooks.session_start` via :mod:`trialerror.hooks.probe_log`)
and always exit 0, with no stdout, swallowing every exception.

**Field-name uncertainty, stated rather than guessed away** (an open item
in the design's list of unknowns: "Whether the SubagentStart payload carries
tool_use_id" --
unverified because the traps forbid reading a payload's non-key CONTENT to
find out by trial, and no live-payload sample was available when this was
written). Each of
``agent_type``/``tool_use_id``/``cc_version`` is read under both a snake_case
and the camelCase spelling ``meta.json`` is known to use (design Section 1),
because guessing wrong would silently record ``null`` for a value the host
did send under the other spelling -- and the very ``hook_payload_keys``
probe this file feeds is what will tell a later reader which spelling (if
either) actually arrives, from the raw ``keys`` list every line already
carries regardless of which alias resolved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from trialerror.hooks.probe_log import ROTATE_BYTES as _ROTATE_BYTES
from trialerror.hooks.probe_log import append_hook_record, first

__all__ = ["main_start", "main_stop"]


def _transcript_exists_info(payload: dict) -> tuple[bool, int | None]:
    raw = first(payload, "agent_transcript_path", "transcript_path", "transcriptPath")
    if not isinstance(raw, str) or not raw:
        return False, None
    try:
        p = Path(raw)
        if p.is_file():
            return True, p.stat().st_size
    except OSError:
        pass
    return False, None


def _evaluate(payload: dict, *, hook: str) -> None:
    extra: dict[str, Any] = {}
    if hook == "subagent_stop":
        exists, size = _transcript_exists_info(payload)
        extra["agent_transcript_path_exists"] = exists
        if exists:
            extra["agent_transcript_path_size"] = size
    append_hook_record(payload, hook=hook, extra=extra)


def _read_payload() -> dict:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    return payload if isinstance(payload, dict) else {}


def _run(hook: str) -> int:
    try:
        payload = _read_payload()
    except Exception:
        return 0
    try:
        _evaluate(payload, hook=hook)
    except Exception as exc:  # noqa: BLE001 - trap 1: must never break the session
        print(f"{hook}: internal error: {exc}", file=sys.stderr)
    return 0


def main_start() -> int:
    return _run("subagent_start")


def main_stop() -> int:
    return _run("subagent_stop")
