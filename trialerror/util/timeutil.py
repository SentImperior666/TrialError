"""The one clock. Design Section 4: "All timestamps are ISO-8601 UTC written
by ``trialerror.util.now()`` (python-datetime; ``os.popen('date')``-class calls
are lint-blocked)."

Every store write path in the harness must call :func:`now` (or :func:`now_dt`)
rather than reaching for ``datetime.utcnow()``, ``time.strftime``, or a shelled
out ``date`` command directly — the latter is explicitly named in the design as
a banned pattern (it was a real bug class in the origin-project orchestrator's own
history: an ``os.popen('date')`` trap recorded in project memory).
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["now", "now_dt", "parse"]


def now_dt() -> datetime:
    """Current time as a timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


def now() -> str:
    """Current time as an ISO-8601 UTC string with millisecond precision.

    Format: ``YYYY-MM-DDTHH:MM:SS.mmmZ`` — always UTC, always ``Z``-suffixed,
    always millisecond precision (not microsecond) so timestamps sort and
    compare predictably across the platform.
    """
    dt = now_dt()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse(value: str) -> datetime:
    """Parse a string produced by :func:`now` (or any ISO-8601 UTC string)
    back into a timezone-aware ``datetime``."""
    v = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(v)
