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
from trialerror.stores.writer import get, insert, require_xid_targets
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
    write-API redaction pass (``trialerror.stores.writer``) still applies.

    **Concurrency (WA-1, sweep batch W3).** The legality check and the
    write are now one atomic step: :func:`_cas_request_state` re-reads the
    row under a ``BEGIN IMMEDIATE`` write lock on ``knowledge.db`` and
    updates it with a compare-and-swap ``UPDATE ... WHERE source_id = ?
    AND request_state = ?``. Before this, two concurrent ``requested ->
    delivered`` calls both passed the check and both "succeeded", writing
    two ``ingest_request_transition`` events for one real transition; now
    exactly one wins and every loser raises
    :class:`~trialerror.ingest.errors.InvalidRequestTransitionError`
    naming the state actually found.

    **Where the atomicity stops.** ``source`` lives in ``knowledge.db``
    and ``event`` in ``ops.db`` -- two files, so two transactions. The
    event insert lands AFTER the knowledge commit: a crash in the gap
    loses an EVENT, never a transition, and never writes an event for a
    transition that did not happen (the reverse order would be worse --
    an audit row claiming a state change that then failed). What the gap
    does NOT cover any more is a BAD ``launch_id``: that is checked up
    front (see the comment below), so an unknown launch refuses with
    nothing written rather than moving the row and then failing its own
    audit."""
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

    # Refuse an unknown ``launch_id`` BEFORE the state change, not after
    # (lane C, finding F1). ``event.launch_id`` is an XID column, so the
    # insert below would refuse a launch id naming no ``platform.launch``
    # row -- but only once ``request_state`` had already committed, leaving
    # the caller a refusal for a transition that DID happen and no audit
    # row for it. This is the same "validate the identity before you
    # mutate" discipline ``artifacts.gates`` and ``rooms.api`` apply with
    # their own ``_require_launch_exists``; here the pre-flight is the
    # write API's own check, run against the exact row written below, so
    # the message is identical to the one the deferred insert would raise.
    require_xid_targets(store, "event", {"launch_id": launch_id})

    _cas_request_state(store, source_id=source_id, from_state=from_state, to_state=to_state)

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


#: ``to_state`` -> the ``source`` timestamp column that state stamps (states
#: not listed here stamp nothing). Kept beside :func:`_cas_request_state`,
#: which is the only writer of either column.
_TS_COLUMN_FOR_STATE: dict[str, str] = {"requested": "requested_ts", "delivered": "delivered_ts"}


def _cas_request_state(store: Store, *, source_id: str, from_state: str, to_state: str) -> None:
    """The compare-and-swap half of :func:`transition` -- WA-1's fix.

    ``trialerror.stores.writer.update`` builds ``UPDATE <table> SET ...
    WHERE <pk> = ?`` with no room for an extra predicate, so this issues
    the statement directly on ``store.knowledge`` (a plain column write:
    ``request_state``/``requested_ts``/``delivered_ts`` carry no XID and no
    redaction, the two things the generic writer adds). Both the re-read
    and the write happen inside one ``BEGIN IMMEDIATE`` transaction, so no
    other writer can interleave between them."""
    ts_column = _TS_COLUMN_FOR_STATE.get(to_state)
    conn = store.knowledge
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = conn.execute("SELECT request_state FROM source WHERE source_id = ?", (source_id,)).fetchone()
        if fresh is None:
            raise SourceNotFoundError(f"no such source: {source_id!r}")
        assignments = ["request_state = ?"]
        params: list[Any] = [to_state]
        if ts_column is not None:
            assignments.append(f"{ts_column} = ?")
            params.append(now())
        params.extend([source_id, from_state])
        cur = conn.execute(
            f"UPDATE source SET {', '.join(assignments)} WHERE source_id = ? AND request_state = ?",
            params,
        )
        if cur.rowcount == 0:
            found = conn.execute("SELECT request_state FROM source WHERE source_id = ?", (source_id,)).fetchone()
            found_state = found["request_state"] if found is not None else "<row disappeared>"
            raise InvalidRequestTransitionError(
                f"source {source_id!r}: {from_state!r} -> {to_state!r} did not apply — the row "
                f"is now {found_state!r} (a concurrent writer moved it first; nothing was written)"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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
