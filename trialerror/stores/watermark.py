"""Read side of the import watermark -- the boundary every grandfathering doctor
check compares against.

A program whose history was imported from an earlier record carries one row in
``ops.db``'s ``meta`` table (:data:`IMPORT_WATERMARK_KEY`) naming the import's
timestamp. Doctor checks over sessions, events and artifacts treat rows at or
before that boundary as imported history rather than as live-harness defects.

This module holds ONLY the read side (a bare connection in, a timestamp out) so
that the checks can import it without depending on the import tooling that
writes the watermark: that tooling is program-specific and lives outside the
public distribution, while these checks ship everywhere. The write side keeps
re-exporting these names, so nothing that imported them from there breaks.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

__all__ = [
    "IMPORT_WATERMARK_KEY",
    "read_import_watermark",
    "import_ts_from_conn",
    "at_or_before",
]

#: ``meta`` key under which the import tooling records the watermark. The
#: string is historical (it names the tool that first wrote it) and is part of
#: existing stores, so it stays as is.
IMPORT_WATERMARK_KEY = "the (excluded) tenant-migration module.import_watermark"


def read_import_watermark(conn: sqlite3.Connection) -> dict | None:
    """The watermark from a bare (typically read-only) ops.db connection --
    what the doctor checks have. Tolerates a pre-v5 ops.db with no ``meta``
    table at all (returns ``None``): a program that predates the import
    simply has no imported history to grandfather."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (IMPORT_WATERMARK_KEY,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _decode(row[0])


def import_ts_from_conn(conn: sqlite3.Connection) -> str | None:
    """Just the boundary timestamp, or ``None`` -- the one value a doctor
    check needs to decide whether a row is exempt."""
    wm = read_import_watermark(conn)
    if wm is None:
        return None
    ts = wm.get("import_ts")
    return ts if isinstance(ts, str) and ts else None


def _decode(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def at_or_before(row_ts: str | None, boundary_ts: str | None) -> bool:
    """``True`` iff ``row_ts`` is a real timestamp at or before
    ``boundary_ts`` -- the ONE comparison every grandfathering check uses,
    so all of them agree on what "imported" means.

    Returns ``False`` (never exempt) when either side is missing or
    unparseable: an exemption must be provable, and a row whose own
    timestamp cannot be read is not proof of anything. Timestamps in an
    imported corpus are not uniformly shaped (``...Z`` millisecond stamps
    beside raw ``+00:00``/``+02:00`` offsets), so this parses both rather
    than comparing strings -- ``'2026-08-29T21:26:49.826199+00:00'`` and
    ``'2026-08-29T19:26:49.000Z'`` sort differently as text than as
    instants.
    """
    a = _parse_ts(row_ts)
    b = _parse_ts(boundary_ts)
    if a is None or b is None:
        return False
    return a <= b


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt
