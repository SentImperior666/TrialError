"""The request queue. Design Section 6: "wanted -> requested -> delivered
-> verifying -> archived -> indexed (+ rejected on license, failed).
`requests/REQUESTS.md` is a rendered view; the user fulfills; `trialerror
ingest add --fulfills SRC-x` closes the loop. Every state change is an
event."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trialerror.ingest.errors import InvalidRequestTransitionError, SourceNotFoundError
from trialerror.stores.store import Store
from trialerror.stores.writer import get, insert
from trialerror.util.atomic import atomic_write_text
from trialerror.util.config import resolve_configured_path
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["TRANSITIONS", "transition", "render_requests_md", "write_requests_md", "DEFAULT_REQUESTS_PATH"]

#: from_state -> allowed to_states. ``indexed`` and ``rejected``/``failed``
#: are terminal (empty target sets) -- ``request_state``'s DDL CHECK
#: constraint already limits the value domain; this dict is the ORDERING
#: constraint on top of it.
TRANSITIONS: dict[str, frozenset[str]] = {
    "wanted": frozenset({"requested", "rejected"}),
    "requested": frozenset({"delivered", "rejected", "failed"}),
    "delivered": frozenset({"verifying", "rejected", "failed"}),
    "verifying": frozenset({"archived", "rejected", "failed"}),
    "archived": frozenset({"indexed", "failed"}),
    "indexed": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset({"requested"}),  # a failed acquisition can be retried
}


def transition(store: Store, source_id: str, to_state: str, *, launch_id: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Move ``source_id``'s ``request_state`` forward per :data:`TRANSITIONS`,
    logging the change as an ``event`` row (design: "every state change is
    an event") -- a plain ``event`` insert, not ``trialerror.events``' higher-level
    API (M5-owned, out of this build's lane); the ``event`` table's own
    write-API redaction pass (``trialerror.stores.writer``) still applies."""
    source = get(store, "source", pk_column="source_id", pk_value=source_id)
    if source is None:
        raise SourceNotFoundError(f"no such source: {source_id!r}")
    from_state = source["request_state"]
    allowed = TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidRequestTransitionError(
            f"source {source_id!r}: {from_state!r} -> {to_state!r} is not a permitted "
            f"request-queue transition (allowed from {from_state!r}: {sorted(allowed)!r})"
        )

    from trialerror.stores.writer import update

    changes: dict[str, Any] = {"request_state": to_state}
    if to_state == "requested":
        changes["requested_ts"] = now()
    elif to_state == "delivered":
        changes["delivered_ts"] = now()
    update(store, "source", pk_column="source_id", pk_value=source_id, changes=changes)

    insert(
        store,
        "event",
        {
            "event_id": new_id("EVT"),
            "ts": now(),
            "launch_id": launch_id,
            "workpackage": None,
            "type": "ingest_request_transition",
            "payload": _event_payload(source_id, from_state, to_state, note),
        },
    )
    updated = get(store, "source", pk_column="source_id", pk_value=source_id)
    assert updated is not None
    return updated


def _event_payload(source_id: str, from_state: str, to_state: str, note: str | None) -> str:
    import json

    return json.dumps({"source_id": source_id, "from": from_state, "to": to_state, "note": note}, ensure_ascii=False)


def render_requests_md(store: Store) -> str:
    """Design Section 6: "`requests/REQUESTS.md` is a rendered view" --
    a plain markdown table grouped by ``request_state``, pure function of
    the ``source`` table (never hand-edited; the write path is
    :func:`transition`/``trialerror ingest add``)."""
    rows = [dict(r) for r in store.knowledge.execute("SELECT * FROM source ORDER BY request_state, registered_ts").fetchall()]
    lines = ["# Requests", ""]
    by_state: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_state.setdefault(r["request_state"], []).append(r)
    for state in ("wanted", "requested", "delivered", "verifying", "archived", "indexed", "rejected", "failed"):
        entries = by_state.get(state, [])
        lines.append(f"## {state} ({len(entries)})")
        lines.append("")
        if entries:
            lines.append("| source_id | title | license_tier | acquisition_route |")
            lines.append("|---|---|---|---|")
            for e in entries:
                lines.append(f"| {e['source_id']} | {e['title']} | {e['license_tier']} | {e['acquisition_route']} |")
        lines.append("")
    return "\n".join(lines)


#: Design Section 3.2 per-program scaffold: "``requests/REQUESTS.md``".
DEFAULT_REQUESTS_PATH = "requests/REQUESTS.md"


def write_requests_md(store: Store, program_root: Path, config: dict[str, Any] | None = None) -> Path:
    """``[paths].requests_path`` overrides :data:`DEFAULT_REQUESTS_PATH`
    (the import-design notes (internal, not in this export) Sec 5 knob #4) -- ``config`` defaults to
    ``None``, identical to every pre-existing caller's behavior."""
    out_path = resolve_configured_path(program_root, config, "requests_path", DEFAULT_REQUESTS_PATH)
    atomic_write_text(out_path, render_requests_md(store))
    return out_path
