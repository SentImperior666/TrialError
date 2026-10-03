"""Read L5 part A3's ``sessions.json`` (:func:`trialerror.obs.statusline_capture.
_update_sessions`'s own output) for the Remote Control pattern (design
Section 2.2, 2.4).

A missing or unreadable file means "no data", never an error (design
Section 2.2's own words) -- a host that has never run the statusLine
capture, or a scratch test directory with no quota dir at all, must not
make ``units scan`` fail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["SessionEntry", "read_sessions"]


@dataclass(frozen=True)
class SessionEntry:
    session_id: str
    first_ts: str | None
    last_ts: str | None
    cost: dict[str, Any] | None


def read_sessions(statusline_dir: Path | str) -> dict[str, SessionEntry]:
    """``{session_id: SessionEntry}`` from ``<statusline_dir>/sessions.json``.

    The real shape (``trialerror.obs.statusline_capture._update_sessions``):
    ``{"version": 1, "sessions": {<session_id>: {"first_seen_ts",
    "last_seen_ts", "last_seen_epoch", "model", "cc_version", "cost": {...},
    "window_pct": {...}}}}``. ``first_seen_ts``/``last_seen_ts`` (this
    module's ``first_ts``/``last_ts``) and ``cost`` are read -- everything
    else in an entry is the statusLine's own business, not this lane's.
    ``first_ts`` feeds S-2's ``statusline_total`` unit (design Section 2.2
    step 4: a session the status line saw with no transcript file at all).
    """
    path = Path(statusline_dir) / "sessions.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    sessions = doc.get("sessions") if isinstance(doc, dict) else None
    if not isinstance(sessions, dict):
        return {}
    out: dict[str, SessionEntry] = {}
    for sid, entry in sessions.items():
        if not isinstance(sid, str) or not isinstance(entry, dict):
            continue
        first_ts = entry.get("first_seen_ts")
        last_ts = entry.get("last_seen_ts")
        cost = entry.get("cost")
        out[sid] = SessionEntry(
            session_id=sid,
            first_ts=first_ts if isinstance(first_ts, str) else None,
            last_ts=last_ts if isinstance(last_ts, str) else None,
            cost=cost if isinstance(cost, dict) else None,
        )
    return out
