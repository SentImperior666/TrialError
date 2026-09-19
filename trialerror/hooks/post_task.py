"""PostToolUse: Task hook: appends a ``subagent_return`` event (ids only,
response size and the host's own token usage — never transcript content)
after every subagent spawn. Design Section 5.4 (PostToolUse row): "appends
a subagent_return event (ids only, sizes, duration); flags RUNNING launches
for reconciliation."

TRIALERROR-DEV-NOTE (usage, D-FB-13 (a), verified 2026-09-16): the subagent
tool's result DOES carry a provider ``usage`` object — ``input_tokens``,
``cache_creation_input_tokens``, ``cache_read_input_tokens``,
``output_tokens`` — plus a ``totalTokens`` that equalled their sum in every
recorded sample. :func:`extract_usage` reduces it to that total and split
and nothing else, and every other shape degrades to ``None`` rather than
raising, because this hook runs after the tool already ran and must never
fail closed on a logging problem. ``budget reconcile --from-event`` is the
consumer; ``--actual-tokens`` remains the path for any launch whose host
sent no usage at all.

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

TRIALERROR-DEV-NOTE (Task->Agent rename, found 2026-09-05): this hook used
to gate on a bare ``tool_name != "Task"``. Live evidence on the sandbox
host (03:34Z, the sandbox container) showed Claude Code 2.1.261 invoking
the subagent tool as ``Agent``, not ``Task`` -- the old check silently
skipped the ``subagent_return`` event (and the ``hook_alive{hook=post_task}``
marker) for every real spawn. This now gates on
:data:`trialerror.hooks.SUBAGENT_TOOL_NAMES` (``("Task", "Agent")``),
matching :mod:`trialerror.hooks.spawn_gate`'s own fix and
``plugin/hooks/hooks.json``'s ``^(Task|Agent)$`` matcher.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from trialerror.hooks import SUBAGENT_TOOL_NAMES

#: The four numbers a provider-side ``usage`` object splits a turn into, in
#: the spelling Claude Code's own recorded subagent result uses (live shape,
#: see :func:`extract_usage`). Read in this order; written under these same
#: names so the payload never renames a number on its way into the store.
USAGE_SPLIT_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)

#: Where a total may be reported beside the split. Claude Code's subagent
#: result carries ``totalTokens``; a raw provider ``usage`` object carries
#: none, and the total is then the sum of the split.
_TOTAL_KEYS = ("totalTokens", "total_tokens")


def _as_int(value: Any) -> int | None:
    """``value`` as a non-negative int, or ``None`` for anything that is not
    one. Deliberately refuses ``bool`` (``True`` is an ``int`` in Python and
    a token count of ``True`` is not a reading anybody wants) and refuses a
    numeric STRING: a host that starts sending ``"11455"`` has changed its
    contract, and guessing would hide that rather than report it."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def extract_usage(tool_response: Any) -> dict[str, Any] | None:
    """The ``usage`` object a PostToolUse ``tool_response`` carries, reduced
    to a total plus the input / cache-creation / cache-read / output split —
    or ``None`` when this payload carries no usage reading at all.

    **The live shape (verified, 2026-09-16).** Claude Code records the
    subagent tool's own result with ``usage`` present::

        {"status": "completed", "agentType": ..., "resolvedModel": ...,
         "totalDurationMs": 1353, "totalTokens": 11461, "usage": {
             "input_tokens": 2, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 11455, "output_tokens": 4,
             "output_tokens_details": {...}, "server_tool_use": {...},
             "cache_creation": {...}, "iterations": [...]}}

    ``totalTokens`` equalled the sum of the four split numbers in every
    recorded sample, so the total is read from it when present and summed
    from the split when it is not; ``total_source`` says which happened, so
    a later reader never has to guess whether a total was reported or
    derived. Everything else in the object (the ``iterations`` array,
    ``service_tier``, the nested detail tables) is deliberately dropped:
    this is a token count for reconciliation, not a copy of the host's
    telemetry.

    **Degrading.** Every branch here returns ``None`` rather than raising,
    for every shape seen or plausible: no ``tool_response`` at all, a bare
    string or list where a mapping was expected, ``usage: null``, a
    ``usage`` that is a string, split values that are not non-negative ints
    (including ``bool`` and numeric strings — see :func:`_as_int`), and an
    object that carries a total but no split. ``usage: null`` in the written
    payload therefore means "this payload had no usage to read", which is a
    different statement from the key being absent — and the key is never
    absent, because :func:`_evaluate` always writes it.
    """
    if not isinstance(tool_response, Mapping):
        return None

    raw = tool_response.get("usage")
    split: dict[str, int | None] = {key: None for key in USAGE_SPLIT_KEYS}
    if isinstance(raw, Mapping):
        for key in USAGE_SPLIT_KEYS:
            split[key] = _as_int(raw.get(key))

    total: int | None = None
    total_source: str | None = None
    for source in _TOTAL_KEYS:
        candidate = _as_int(tool_response.get(source))
        if candidate is None and isinstance(raw, Mapping):
            candidate = _as_int(raw.get(source))
        if candidate is not None:
            total, total_source = candidate, source
            break
    if total is None:
        reported = [v for v in split.values() if v is not None]
        if reported:
            total, total_source = sum(reported), "sum(split)"

    if total is None and all(v is None for v in split.values()):
        return None
    return {"total_tokens": total, "total_source": total_source, **split}


def _evaluate(payload: dict) -> str | None:
    """Returns a stderr diagnostic, or ``None`` on success/no-op. Kept
    separate from ``main()`` for direct testing, mirroring
    ``plugin/hooks/spawn_gate.py``'s ``_evaluate``."""
    tool_name = payload.get("tool_name")
    if tool_name not in SUBAGENT_TOOL_NAMES:
        return None

    tool_input = payload.get("tool_input") or {}
    prompt_text = tool_input.get("prompt") or tool_input.get("description") or ""
    if not prompt_text and tool_input:
        # TRIALERROR-DEV-NOTE (tool_input schema assumption, FU-11 verification
        # finding FU11-V5): mirrors spawn_gate.py's own fallback -- if a
        # future rename moves the prompt text under some other key, scan the
        # WHOLE serialized tool_input for the `launch_id:` token rather than
        # recording a subagent_return with a NULL launch_id.
        prompt_text = json.dumps(tool_input, ensure_ascii=False)
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
            payload={
                "response_size_bytes": response_size,
                "duration_ms": None,
                # D-FB-13 (a). ALWAYS present, null when this payload carried
                # no usage reading: "the host sent nothing" and "nobody
                # looked" are different facts about a launch, and only one of
                # them can be fixed. `budget reconcile --from-event` reads
                # this and refuses on the null.
                "usage": extract_usage(tool_response),
            },
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

