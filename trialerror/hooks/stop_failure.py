"""``StopFailure`` hook: record-only limit/error detection (design
``L4_quota-policy.md`` Section 2.6, "Detection, StopFailure"). Wired as
``trialerror hook stop-failure`` in ``plugin/hooks/hooks.json`` -- there is
no ``StopFailure`` binding today (design Section 1: "The harness binds no
such hook today").

**Field-name uncertainty, stated rather than guessed away** (design Section
1: "The payload's exact field names are not yet observed on this
installation"; Section 2.6: "Until a real payload has been recorded, the
handler stores every top-level key name and the value of any field whose
name contains 'error' or 'reason', cut to 100 characters"). This module
follows that literally: :func:`_error_fields` copies every such field
(truncated), and :func:`_error_class` is a best-effort SHORT CODE read from
a handful of plausible candidate names -- ``None`` when none of them are
present, which the ``hook_payload_keys`` probe's ``keys`` list (recorded by
:func:`trialerror.hooks.probe_log.append_hook_record` regardless) is exactly
what a later reader uses to learn the real spelling and update this list,
mirroring :mod:`trialerror.hooks.subagent_probe`'s own precedent for
unverified payload shapes.

**Never blocks, never reads message text, always exits 0** (design Section
2.6: "Always exit 0, within 300 ms"; the harness's own hook trap 1/2). No
network I/O: a rate-limit/billing class writes a local flag file only --
``trialerror quota notify`` (a separate, cron-scheduled command, never
called from inside a hook) is what turns flag files into ``quota_notice``
rows and pushes them.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from trialerror.hooks.probe_log import append_hook_record, first, probes_dir
from trialerror.util.timeutil import now

__all__ = ["main"]

#: Plausible names for a short error/reason CODE (not a message) on a
#: StopFailure payload -- unverified (see module docstring). Checked in
#: order; the first present, non-container value wins.
_ERROR_CLASS_CANDIDATES = (
    # N3: "error" added -- likely the documented field per the review
    # (a StopFailure payload's own top-level error code), and it was
    # missing entirely: a payload shaped {"error": "rate_limit"} got
    # error_class=null despite being detected fine through the
    # error-field scan below.
    "error", "error_class", "errorClass", "error_type", "errorType",
    "reason", "reasonCode", "reason_code",
)

#: design Section 2.6: "A rate-limit or billing class writes a limit_hit
#: flag file." Matched against the error class and every captured
#: error/reason field, case-insensitively -- short codes are expected to
#: read like ``rate_limit_error`` or ``billing_error``, not prose, so a
#: substring match is enough and does not require the exact spelling.
#: N3: "overloaded" dropped -- a 529 server-capacity error is not a quota or
#: billing limit, and matching it made every overload push a false "Quota
#: limit hit" notification.
_LIMIT_HIT_MARKERS = ("rate_limit", "ratelimit", "rate-limit", "billing", "quota")

_ERROR_CLASS_MAX = 40
_FIELD_VALUE_MAX = 100


def _looks_like_error_or_reason(name: str) -> bool:
    lowered = name.lower()
    return "error" in lowered or "reason" in lowered


def _short(value: Any, limit: int) -> str:
    return str(value)[:limit]


def _error_fields(payload: dict) -> dict[str, str]:
    """Every top-level field whose NAME contains 'error' or 'reason', its
    value cut to 100 characters (design Section 2.6, verbatim) -- payload
    CONTENT is otherwise never read by this hook."""
    return {k: _short(v, _FIELD_VALUE_MAX) for k, v in payload.items() if _looks_like_error_or_reason(k)}


def _error_class(payload: dict) -> str | None:
    for name in _ERROR_CLASS_CANDIDATES:
        if name not in payload:
            continue
        value = payload[name]
        if isinstance(value, (dict, list)):
            continue  # a short CODE, never a nested structure that might carry text
        text = str(value).strip()
        if text:
            return _short(text, _ERROR_CLASS_MAX)
    return None


def _is_limit_hit(error_class: str | None, error_fields: dict[str, str]) -> bool:
    haystack = " ".join([error_class or "", *error_fields.values()]).lower()
    return any(marker in haystack for marker in _LIMIT_HIT_MARKERS)


def _write_limit_hit_flag(payload: dict, error_class: str | None) -> None:
    d = probes_dir() / "quota_limit_hit"
    d.mkdir(parents=True, exist_ok=True)
    ts = now()
    sid = first(payload, "session_id") or "unknown"
    safe_sid = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))[:64]
    safe_ts = "".join(c for c in ts if c.isalnum())
    path = d / f"{safe_ts}_{safe_sid}.flag"
    path.write_text(
        json.dumps({"ts": ts, "session_id": sid, "error_class": error_class}, ensure_ascii=False),
        encoding="utf-8",
    )


def _evaluate(payload: dict) -> None:
    error_class = _error_class(payload)
    extra = _error_fields(payload)
    if error_class:
        extra["error_class"] = error_class
    append_hook_record(payload, hook="stop_failure", extra=extra)
    if _is_limit_hit(error_class, extra):
        _write_limit_hit_flag(payload, error_class)


def _read_payload() -> dict:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    try:
        payload = _read_payload()
    except Exception:
        return 0
    try:
        _evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - trap 1: must never break the session
        print(f"stop_failure: internal error: {exc}", file=sys.stderr)
    return 0
