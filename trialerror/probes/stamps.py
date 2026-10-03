"""Answer stamps and the degraded state (design Section 3.5).
``answer_stamp(store) -> dict`` is read from ``platform.probe_run`` in one
pass each -- "There is no search inside the stamp" (design, verbatim): this
module never calls into :mod:`trialerror.retrieve`, so attaching a stamp to a
search result costs nothing beyond the query it already ran.
"""

from __future__ import annotations

from typing import Any

from trialerror.util.build import build_id
from trialerror.util.timeutil import now, parse

__all__ = ["answer_stamp"]

#: design Section 3.5: "vector is stale when its last run is older than 24h"
#: and "degraded is true when the latest ... run within 24h failed".
_STALE_SECONDS = 24 * 3600

_DISPLAY_STATUSES = ("pass", "fail", "skip")

#: N-4 fix round: statuses that read as a canary FAILURE for the purposes of
#: `degraded` -- 'fail' (the canary ran and found nothing) and 'error' (it
#: timed out or raised). `_display_status` already showed an 'error' row as
#: "fail" to a caller; `degraded` used to check the RAW status against
#: 'fail' only, so a timed-out canary could show `fulltext: fail, degraded:
#: false` -- the display and the boolean disagreeing about the same row.
_FAILURE_STATUSES = ("fail", "error")


def _seconds_since(ts: str, *, reference: str) -> float:
    try:
        return (parse(reference) - parse(ts)).total_seconds()
    except ValueError:
        return float("inf")


def _latest_canary_run(store, name: str, *, program_id: str | None) -> dict[str, Any] | None:
    """B-2: filtered by ``program_id`` -- ``probe_run`` lives in the
    machine-wide ``platform.db``, so an unfiltered query returned whichever
    PROGRAM's canary happened to run last, regardless of which program's
    search this stamp is actually describing."""
    row = store.platform.execute(
        "SELECT status, started_ts FROM probe_run WHERE name = ? AND program_id = ? "
        "ORDER BY started_ts DESC LIMIT 1",
        (name, program_id),
    ).fetchone()
    return dict(row) if row is not None else None


def _display_status(row: dict[str, Any] | None, *, reference: str, allow_stale: bool) -> str:
    if row is None:
        return "never"
    if allow_stale and _seconds_since(row["started_ts"], reference=reference) > _STALE_SECONDS:
        return "stale"
    return row["status"] if row["status"] in _DISPLAY_STATUSES else "fail"


def _coverage_embedded_pct(store) -> float | None:
    total = store.knowledge.execute("SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
    if not total:
        return None
    embedded = store.knowledge.execute(
        "SELECT COUNT(DISTINCT c.chunk_id) AS n FROM chunk c JOIN emb e ON e.chunk_sha256 = c.sha256"
    ).fetchone()["n"]
    return round(100.0 * embedded / total, 1)


def answer_stamp(store, *, program_id: str | None = None) -> dict[str, Any]:
    """``{build, coverage_embedded_pct, canary: {fulltext, vector, last_ts},
    degraded, degraded_reason}`` -- design Section 3.5's exact shape.

    B-2 fix round: scoped to ``program_id``, defaulting to
    ``resolve_program_id(store.program_root)`` when the caller doesn't pass
    one explicitly -- so an existing call site like ``trialerror.mcp.
    knowledge``'s ``_attach_stamp(store, result)`` needs no change to gain
    the scoping. Before this, the stamp read whichever program's canary ran
    LAST, machine-wide: one program's failure could mark every other
    program's stamp ``degraded: true``, and a later pass in any program
    would clear it again."""
    if program_id is None:
        from trialerror.util.config import resolve_program_id

        program_id = resolve_program_id(store.program_root)

    reference = now()
    fulltext_row = _latest_canary_run(store, "fulltext_canary", program_id=program_id)
    vector_row = _latest_canary_run(store, "vector_canary", program_id=program_id)

    fulltext_display = _display_status(fulltext_row, reference=reference, allow_stale=False)
    vector_display = _display_status(vector_row, reference=reference, allow_stale=True)

    last_ts_candidates = [r["started_ts"] for r in (fulltext_row, vector_row) if r is not None]
    last_ts = max(last_ts_candidates) if last_ts_candidates else None

    degraded = False
    degraded_reason = None
    for name, row in (("fulltext_canary", fulltext_row), ("vector_canary", vector_row)):
        if row is None or row["status"] not in _FAILURE_STATUSES:
            continue
        if _seconds_since(row["started_ts"], reference=reference) <= _STALE_SECONDS:
            degraded = True
            degraded_reason = f"{name} failed at {row['started_ts']}"
            break

    return {
        "build": build_id(),
        "coverage_embedded_pct": _coverage_embedded_pct(store),
        "canary": {"fulltext": fulltext_display, "vector": vector_display, "last_ts": last_ts},
        "degraded": degraded,
        "degraded_reason": degraded_reason,
    }
