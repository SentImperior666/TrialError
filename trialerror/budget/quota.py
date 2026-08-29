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
