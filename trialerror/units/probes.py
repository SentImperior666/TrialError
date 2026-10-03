"""Conformance probes v0 (design Section 3.3, ``kind='conformance'``).
Auto-discovered by :func:`trialerror.probes.registry.discover_and_register_
probes` exactly like a doctor ``checks.py`` module.

**Deviation from a literal reading of the design** (recorded here):
``subagent_stop_fires``,
``subagent_transcript_written``, ``main_transcript_written`` and
``usage_final_line`` read the ALREADY-SCANNED ``unit`` table in
``platform.db`` (populated by a prior ``trialerror units scan``) rather than
independently re-walking ``~/.claude/projects``. :mod:`trialerror.units.scan`
already extracts exactly the fields each of these probes needs (agent_id,
agent_type, first_ts as a stand-in for "the last 7 days", conversation_last_ts,
the Remote Control flag, transcript_path) from precisely the same files a
second file-walk would read -- duplicating that walk here would be a second,
divergent implementation of the same classification/reading rules for no
behavioural gain, and these probes are explicitly "no model cost" checks, not
a second transcript scanner. The practical effect: these four probes are only
as fresh as the last ``units scan`` -- a documented dependency, not a silent
assumption.

``cc_version_seen`` and ``hook_payload_keys`` read ``hook_events.jsonl``
(:mod:`trialerror.hooks.probe_log`) and ``ctx.cc_version``, which callers are
expected to populate from a real ``claude --version`` (design: "at no model
cost" -- this is a local subprocess call, not a Claude Code turn).
"""

from __future__ import annotations

import json
from typing import Any

from trialerror.hooks.probe_log import hook_events_path
from trialerror.probes.registry import ProbeContext, ProbeResult, register_probe
from trialerror.units.reader import SYNTHETIC_MODEL
from trialerror.util.timeutil import now, parse

__all__ = []

_SEVEN_DAYS_S = 7 * 24 * 3600
_ONE_HOUR_S = 3600

#: design Section 3.3's ``hook_payload_keys`` row, verbatim: which raw
#: payload key the ledger depends on, per hook.
_REQUIRED_KEYS_BY_HOOK: dict[str, set[str]] = {
    "session_start": {"session_id"},
    "spawn_gate": {"session_id", "tool_use_id"},
    "post_task": {"session_id", "tool_use_id"},
    "spawn_failure": {"session_id", "tool_use_id"},
    "subagent_start": {"session_id", "agent_id"},
    "subagent_stop": {"session_id", "agent_id", "agent_transcript_path"},
}


def _read_hook_events() -> list[dict[str, Any]]:
    path = hook_events_path()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _seconds_ago(ts_a: str, ts_b: str) -> float:
    """``ts_a - ts_b`` in seconds. Returns +inf on a bad timestamp, so a
    comparison against it never accidentally reads as "recent"."""
    try:
        return (parse(ts_a) - parse(ts_b)).total_seconds()
    except ValueError:
        return float("inf")


# ---------------------------------------------------------------------------
# cc_version_seen
# ---------------------------------------------------------------------------


@register_probe("cc_version_seen", kind="conformance", timeout_s=3.0)
def probe_cc_version_seen(ctx: ProbeContext) -> ProbeResult:
    current = ctx.cc_version
    if not current:
        return ProbeResult(status="skip", detail={"reason": "claude --version not available to this probe run"})

    previous = None
    if ctx.platform_store is not None:
        row = ctx.platform_store.platform.execute(
            "SELECT detail FROM probe_run WHERE name = 'cc_version_seen' ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            try:
                previous = json.loads(row["detail"]).get("current")
            except (TypeError, ValueError):
                previous = None

    if previous is None:
        return ProbeResult(status="pass", detail={"current": current, "previous": None})
    if previous == current:
        return ProbeResult(status="pass", detail={"current": current, "previous": previous})
    return ProbeResult(
        status="warn",
        detail={
            "current": current,
            "previous": previous,
            "message": "the Claude Code version changed since the last recorded run; "
            "run `probes run --kind conformance --live` once",
        },
    )


# ---------------------------------------------------------------------------
# hook_payload_keys
# ---------------------------------------------------------------------------


@register_probe("hook_payload_keys", kind="conformance", timeout_s=3.0)
def probe_hook_payload_keys(ctx: ProbeContext) -> ProbeResult:
    events = _read_hook_events()
    if not events:
        return ProbeResult(status="skip", detail={"reason": "no hook_events.jsonl records yet"})

    current_version = ctx.cc_version
    # latest record per (hook, cc_version), and the two most recent distinct
    # versions per hook, so the "differs from the previous version" check
    # has something to compare against even without ctx.cc_version.
    latest_by_hook_version: dict[tuple[str, str | None], dict[str, Any]] = {}
    for rec in events:
        hook = rec.get("hook")
        if not isinstance(hook, str):
            continue
        version = rec.get("cc_version")
        key = (hook, version)
        prior = latest_by_hook_version.get(key)
        if prior is None or (rec.get("ts") or "") >= (prior.get("ts") or ""):
            latest_by_hook_version[key] = rec

    missing: dict[str, list[str]] = {}
    diffs: dict[str, dict[str, list[str]]] = {}

    hooks_seen = sorted({h for h, _v in latest_by_hook_version})
    for hook in hooks_seen:
        versions_for_hook = sorted(
            {v for (h, v) in latest_by_hook_version if h == hook and v is not None}
        )
        target_version = current_version if current_version in versions_for_hook else (
            versions_for_hook[-1] if versions_for_hook else None
        )
        current_rec = latest_by_hook_version.get((hook, target_version)) or latest_by_hook_version.get((hook, None))
        if current_rec is None:
            continue
        current_keys = set(current_rec.get("keys") or [])

        required = _REQUIRED_KEYS_BY_HOOK.get(hook)
        if required:
            gap = sorted(required - current_keys)
            if gap:
                missing[hook] = gap

        earlier_versions = [v for v in versions_for_hook if v != target_version]
        if earlier_versions:
            previous_rec = latest_by_hook_version[(hook, earlier_versions[-1])]
            previous_keys = set(previous_rec.get("keys") or [])
            if previous_keys != current_keys:
                diffs[hook] = {
                    "added": sorted(current_keys - previous_keys),
                    "removed": sorted(previous_keys - current_keys),
                }

    detail = {"missing_required_keys": missing, "key_set_diffs": diffs, "hooks_seen": hooks_seen}
    if missing:
        return ProbeResult(status="fail", detail=detail)
    if diffs:
        return ProbeResult(status="warn", detail=detail)
    return ProbeResult(status="pass", detail=detail)


# ---------------------------------------------------------------------------
# subagent_stop_fires
# ---------------------------------------------------------------------------


def _recent_subagent_units(ctx: ProbeContext) -> list[dict[str, Any]]:
    horizon = now()
    rows = ctx.platform_store.platform.execute(
        "SELECT * FROM unit WHERE host = ? AND kind IN ('subagent','workflow_agent')", (ctx.host,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        first_ts = d.get("first_ts")
        if first_ts and _seconds_ago(horizon, first_ts) <= _SEVEN_DAYS_S:
            out.append(d)
    return out


@register_probe("subagent_stop_fires", kind="conformance", timeout_s=5.0)
def probe_subagent_stop_fires(ctx: ProbeContext) -> ProbeResult:
    if ctx.platform_store is None:
        return ProbeResult(status="skip", detail={"reason": "no platform store given"})
    metas = _recent_subagent_units(ctx)
    if not metas:
        return ProbeResult(status="skip", detail={"reason": "no subagent/workflow-agent units in the last 7 days"})

    stopped_agent_ids = {
        rec.get("agent_id") for rec in _read_hook_events() if rec.get("hook") == "subagent_stop" and rec.get("agent_id")
    }
    matched = [m for m in metas if m.get("agent_id") in stopped_agent_ids]
    matched_keys = {m["unit_key"] for m in matched}
    total = len(metas)
    pct = round(100.0 * len(matched) / total, 1)

    by_agent_type: dict[str, int] = {}
    for m in metas:
        if m["unit_key"] not in matched_keys:
            key = m.get("agent_type") or "(unknown)"
            by_agent_type[key] = by_agent_type.get(key, 0) + 1

    detail = {"total": total, "matched": len(matched), "pct_matched": pct, "unmatched_by_agent_type": by_agent_type}
    return ProbeResult(status="pass" if pct >= 100 else "warn", detail=detail)


# ---------------------------------------------------------------------------
# subagent_transcript_written
# ---------------------------------------------------------------------------


@register_probe("subagent_transcript_written", kind="conformance", timeout_s=5.0)
def probe_subagent_transcript_written(ctx: ProbeContext) -> ProbeResult:
    if ctx.platform_store is None:
        return ProbeResult(status="skip", detail={"reason": "no platform store given"})
    metas = _recent_subagent_units(ctx)
    if not metas:
        return ProbeResult(status="skip", detail={"reason": "no subagent/workflow-agent units in the last 7 days"})

    stop_exists_by_agent = {}
    for rec in _read_hook_events():
        if rec.get("hook") == "subagent_stop" and rec.get("agent_id"):
            stop_exists_by_agent[rec["agent_id"]] = bool(rec.get("agent_transcript_path_exists"))

    total = len(metas)
    missing = 0
    by_project: dict[str, int] = {}
    by_version: dict[str, int] = {}
    for m in metas:
        has_jsonl = m.get("usage_source") != "none"
        hook_says_exists = stop_exists_by_agent.get(m.get("agent_id"))
        written = has_jsonl or bool(hook_says_exists)
        if not written:
            missing += 1
            proj = m.get("project_slug") or "(unknown)"
            by_project[proj] = by_project.get(proj, 0) + 1
            ver = m.get("cc_version") or "(unknown)"
            by_version[ver] = by_version.get(ver, 0) + 1

    pct_missing = round(100.0 * missing / total, 1)
    detail = {"total": total, "missing": missing, "pct_missing": pct_missing, "missing_by_project": by_project, "missing_by_version": by_version}
    return ProbeResult(status="pass" if missing == 0 else "warn", detail=detail)


# ---------------------------------------------------------------------------
# main_transcript_written
# ---------------------------------------------------------------------------


@register_probe("main_transcript_written", kind="conformance", timeout_s=5.0)
def probe_main_transcript_written(ctx: ProbeContext) -> ProbeResult:
    if ctx.platform_store is None:
        return ProbeResult(status="skip", detail={"reason": "no platform store given"})
    rows = ctx.platform_store.platform.execute(
        "SELECT session_id, usage_source FROM unit WHERE host = ? AND kind = 'main'", (ctx.host,)
    ).fetchall()
    total = len(rows)
    rc = [r["session_id"] for r in rows if r["usage_source"] in ("transcript_partial", "statusline_total")]
    detail = {"total_main_units": total, "remote_control_sessions": sorted(rc)}
    if rc:
        return ProbeResult(status="fail", detail=detail)
    return ProbeResult(status="pass" if total else "skip", detail=detail)


# ---------------------------------------------------------------------------
# usage_final_line
# ---------------------------------------------------------------------------


def _first_vs_stop_reason_output(path: str) -> list[tuple[int, int]]:
    """``(first_seen_output, stop_reason_line_output)`` for every message id
    that appears on more than one line AND has a ``stop_reason`` line --
    counts only, never message text (trap 2).

    N-1 fix round: this used to keep only pairs whose first/last value
    DIFFERED, which makes "the share where first < final" 1.0 (or 0.0) by
    construction -- it cannot report anything else, regardless of what the
    transcripts actually show. It also compared against "the last line
    written" rather than the ``stop_reason`` line specifically, which the
    fixed rule (:mod:`trialerror.units.reader`) does not treat as
    equivalent (a stop_reason line with null usage is not "the final
    line"). Every multi-line message WITH a stop_reason line is now kept,
    whatever its first/final values are."""
    first: dict[str, int] = {}
    stop_reason_output: dict[str, int] = {}
    line_count: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                message = rec.get("message")
                if not isinstance(message, dict) or message.get("model") == SYNTHETIC_MODEL:
                    continue
                mid = message.get("id") or rec.get("requestId") or rec.get("uuid")
                if not mid:
                    continue
                usage = message.get("usage")
                output = usage.get("output_tokens") if isinstance(usage, dict) else None
                if not isinstance(output, int):
                    continue
                line_count[mid] = line_count.get(mid, 0) + 1
                if mid not in first:
                    first[mid] = output
                if message.get("stop_reason") is not None:
                    stop_reason_output[mid] = output  # the LAST stop_reason line wins, matching the fixed rule
    except OSError:
        return []
    return [
        (first[mid], stop_reason_output[mid])
        for mid, n in line_count.items()
        if n >= 2 and mid in stop_reason_output
    ]


@register_probe("usage_final_line", kind="conformance", timeout_s=10.0)
def probe_usage_final_line(ctx: ProbeContext) -> ProbeResult:
    """Always ``pass`` (design: "It confirms the rule still applies") --
    informational, over up to 20 recent multi-line subagent messages."""
    if ctx.platform_store is None:
        return ProbeResult(status="pass", detail={"reason": "no platform store given", "messages_checked": 0})

    rows = ctx.platform_store.platform.execute(
        "SELECT transcript_path FROM unit WHERE host = ? AND kind = 'subagent' AND transcript_path IS NOT NULL "
        "ORDER BY scanned_ts DESC LIMIT 20",
        (ctx.host,),
    ).fetchall()

    pairs: list[tuple[int, int]] = []
    for row in rows:
        pairs.extend(_first_vs_stop_reason_output(row["transcript_path"]))
        if len(pairs) >= 20:
            break
    pairs = pairs[:20]

    below = sum(1 for first_v, stop_v in pairs if first_v < stop_v)
    share = round(below / len(pairs), 3) if pairs else None
    return ProbeResult(status="pass", detail={"messages_checked": len(pairs), "share_first_below_final": share})
