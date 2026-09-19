"""Read side of the plan-quota feed captured by
:mod:`trialerror.obs.statusline_capture`.

Claude Code (>= 2.1.80, claude.ai subscriptions) reports plan rate-limit
windows in its statusLine JSON; the capture script tees them into
``<quota_dir>/latest.json``. This module turns that file into a budget
answer: which windows exist, how used they are, when they reset, and
whether the reading is fresh enough to trust.

Trust ordering (design Section 4.3 unchanged): a user screenshot ingested
as ``quota_snapshot(source=screenshot)`` remains the override ground
truth; this feed lands as ``source=api`` — authoritative-when-fresh,
subordinate to an operator screenshot on any conflict.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

DEFAULT_FRESH_WITHIN_S = 900

#: ``trialerror.toml`` knob for the freshness bar every surface uses:
#: ``[budget] quota_max_age_s`` (default :data:`DEFAULT_FRESH_WITHIN_S`).
#: Lane FB-3 item 8, custodian observation (b), 2026-09-15: the capture is
#: written by the statusLine, which fires on a UI tick, so a reading goes
#: stale exactly when the orchestrator is idle -- which is when it is most
#: likely to be consulted before booking. 15 minutes is a default, not a
#: law; a program whose operator works in long silent stretches wants a
#: different number, and a booking refusal is the wrong place to discover
#: that it is not configurable.
CONFIG_MAX_AGE_KEY = "quota_max_age_s"


def _default_quota_dir() -> str:
    # Mirrors trialerror/obs/statusline_capture.py::quota_dir — kept as a copy,
    # not an import, so the budget package never pulls in trialerror.obs (and
    # the capture script stays a bare stdlib file). Change both together.
    d = os.environ.get("TRIALERROR_QUOTA_DIR")
    if not d:
        d = os.path.join(os.path.expanduser("~"), ".trialerror", "quota")
    return d


def read_latest_quota(quota_dir: str | None = None) -> dict[str, Any] | None:
    """Return the raw latest snapshot dict, or ``None`` when never captured
    (or unreadable — a torn file reads as absence, never an exception)."""
    path = os.path.join(quota_dir or _default_quota_dir(), "latest.json")
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        return snap if isinstance(snap, dict) else None
    except (OSError, ValueError):
        return None


def quota_status(
    quota_dir: str | None = None,
    *,
    now_epoch: float | None = None,
    fresh_within_s: int = DEFAULT_FRESH_WITHIN_S,
) -> dict[str, Any]:
    """Summarize the captured plan quota for budget surfaces.

    Always returns a dict with ``available`` (a snapshot exists) and
    ``fresh`` (younger than ``fresh_within_s``); window entries pass
    through whatever Claude Code reported (``five_hour``/``seven_day``
    today, forward-compatible with any extra windows)."""
    snap = read_latest_quota(quota_dir)
    if snap is None:
        return {
            "available": False,
            "fresh": False,
            "age_s": None,
            "captured_ts": None,
            "windows": {},
            "note": "no statusline quota captured - wire statusLine per USER_SETUP.md, or rely on screenshots",
        }
    now = time.time() if now_epoch is None else now_epoch
    try:
        age = max(0.0, now - float(snap.get("epoch", 0)))
    except (TypeError, ValueError):
        age = None
    windows: dict[str, Any] = {}
    raw = snap.get("rate_limits")
    if isinstance(raw, dict):
        for key, win in raw.items():
            if isinstance(win, dict):
                windows[key] = {
                    "used_percentage": win.get("used_percentage"),
                    "resets_at": win.get("resets_at"),
                }
    return {
        "available": True,
        "fresh": age is not None and age <= fresh_within_s,
        "age_s": age,
        "captured_ts": snap.get("captured_ts"),
        "model": snap.get("model"),
        "session_id": snap.get("session_id"),
        "account_hint": snap.get("account_hint"),
        "windows": windows,
    }


def resolve_max_age_s(config: Any | None, *, override: int | None = None) -> int:
    """The freshness bar in seconds: ``override`` (a ``--fresh-within-s``
    flag) beats ``[budget] quota_max_age_s`` beats
    :data:`DEFAULT_FRESH_WITHIN_S`.

    ``config`` is the plain ``ProgramConfig.raw`` dict (or ``None``), the
    same convention every other config consumer in this codebase takes. A
    value that is not a positive integer is ignored rather than raising: a
    typo in one knob must not make ``budget book`` unusable, and the default
    it falls back to is the documented one."""
    if isinstance(override, int) and not isinstance(override, bool) and override > 0:
        return override
    table = (config or {}).get("budget") if isinstance(config, dict) else None
    if isinstance(table, dict):
        value = table.get(CONFIG_MAX_AGE_KEY)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return DEFAULT_FRESH_WITHIN_S


def staleness(status: dict[str, Any], *, max_age_s: int) -> dict[str, Any]:
    """Turn a :func:`quota_status` reading into the one question a booking
    gate asks: may this reading be trusted?

    Three outcomes, deliberately distinct:

    - ``absent``: nothing has ever been captured. NOT stale -- a program that
      has not wired the statusLine has no reading to be out of date, and
      refusing its bookings would make an optional feed mandatory by
      accident. The ``budget check``/``budget quota`` surfaces already say
      the reading is missing.
    - ``stale``: a capture exists and is older than ``max_age_s``. This is
      the one a booking gate refuses on, because a number that WAS true is
      the kind a reader trusts without checking its age.
    - ``fresh``: within the bar.

    ``age_s`` is carried in every case (``None`` when absent, or when the
    snapshot's own epoch could not be read), because "stale" without a
    number is a verdict an operator cannot argue with."""
    if not status.get("available"):
        return {"standing": "absent", "stale": False, "age_s": None, "max_age_s": max_age_s}
    age = status.get("age_s")
    stale = not status.get("fresh")
    return {
        "standing": "stale" if stale else "fresh",
        "stale": stale,
        "age_s": age,
        "max_age_s": max_age_s,
        "captured_ts": status.get("captured_ts"),
    }


def stale_capture_message(reading: dict[str, Any]) -> str:
    """The one sentence both the booking refusal and the doctor check print,
    so the two cannot describe the same reading differently."""
    age = reading.get("age_s")
    age_text = f"{age:.0f}s old" if isinstance(age, (int, float)) else "of unknown age"
    return (
        f"the plan-quota capture is {age_text}, past the {reading['max_age_s']}s freshness bar "
        "([budget] quota_max_age_s) -- the statusLine capture "
        "(trialerror/obs/statusline_capture.py, wired per USER_SETUP.md) writes it on a Claude Code "
        "UI tick, so a reading goes stale exactly while a session sits idle"
    )


def booking_quota_reading(
    program_root: Any,
    *,
    quota_dir: str | None = None,
    override: int | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """The staleness reading a booking is judged against: this program's
    ``[budget] quota_max_age_s`` applied to the capture on disk.

    One function for both booking surfaces (``trialerror budget book`` and the
    ``book_launch`` MCP tool), because a refusal that fired on only one of
    them would be a refusal an orchestrator routes around without noticing.
    A missing/unreadable ``trialerror.toml`` means the documented default,
    never a failure: a booking is not the moment to discover a config typo."""
    raw: dict[str, Any] | None = None
    try:
        from trialerror.util.config import CONFIG_FILENAME, ConfigError, load_config

        raw = load_config(os.path.join(str(program_root), CONFIG_FILENAME)).raw
    except (ConfigError, OSError):
        # Fix pass V-8: named exceptions only. An absent or unparseable
        # trialerror.toml still means "no knob set" here -- a booking is not
        # the moment to discover a config typo, and on both shipped surfaces
        # the store open has already raised on an unparseable file long
        # before this runs (design D13's rule is enforced there) -- but a
        # bare `except Exception` also swallowed ImportError and every bug in
        # the lines above it, which is not the same tolerance.
        raw = None
    max_age = resolve_max_age_s(raw, override=override)
    status = quota_status(quota_dir, fresh_within_s=max_age, now_epoch=now_epoch)
    reading = staleness(status, max_age_s=max_age)
    reading["windows"] = status.get("windows") or {}
    return reading


def booking_refusal(reading: dict[str, Any], *, flag: str = "--allow-stale-quota") -> str | None:
    """The refusal text for a stale capture, or ``None`` when the reading is
    fresh or absent. Names the override rather than hiding it: the point is
    that an operator who has decided to book anyway does so on the record."""
    if not reading.get("stale"):
        return None
    return (
        f"{stale_capture_message(reading)}. Booking against a stale reading is a decision, not a "
        f"default: pass {flag} to make it, and it will be recorded on the launch"
    )
