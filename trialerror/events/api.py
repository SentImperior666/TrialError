"""The events/feed/inbox write+read API. See ``trialerror/events/__init__.py``
for the design citations and the authorship-binding contract this module
implements. This file is the ONE place that contract is enforced — the CLI
group modules (``trialerror/cli/events.py``, ``trialerror/cli/feed.py``,
``trialerror/cli/inbox.py``) are thin argv/envelope shells over these functions,
never a second implementation of author derivation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trialerror.stores import get, insert, update
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.store import Store
from trialerror.util.atomic import atomic_write_text
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "append_event",
    "record_hook_alive_once",
    "tail_events",
    "export_events",
    "export_jsonl",
    "render_jsonl",
    "create_thread",
    "post_feed",
    "list_threads",
    "get_thread_posts",
    "post_inbox",
    "read_inbox",
]

# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def append_event(
    store: Store,
    *,
    event_type: str,
    payload: Any,
    session_id: str | None = None,
    launch_id: str | None = None,
    workpackage: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append one type-keyed event. Auto-timestamped (``trialerror.util.now()``
    unless ``ts`` is supplied, e.g. for deterministic tests/replay).
    ``payload`` may be a JSON-serializable Python object (dict/list/str/...)
    OR an already-encoded JSON string — either way, ``trialerror.stores.insert``
    applies the secret-redaction pass and (re-)serializes it to the stored
    JSON string; this function does not touch redaction or serialization
    itself (design Section 4.2; see module docstring). ``payload=None`` is
    refused by the ``event.payload NOT NULL`` constraint (surfaced here as
    :class:`~trialerror.stores.errors.ValidationError`) — pass ``{}`` for "no
    payload".

    Returns the row as written (post-redaction), including the generated
    ``event_id`` and the ``redactions`` count.
    """
    row = {
        "event_id": new_id("EVT"),
        "ts": ts or now(),
        "session_id": session_id,
        "launch_id": launch_id,
        "workpackage": workpackage,
        "type": event_type,
        "payload": payload,
    }
    return insert(store, "event", row)


def record_hook_alive_once(store: Store, *, session_id: str | None, hook_name: str) -> dict[str, Any] | None:
    """Emit one ``hook_alive`` event with ``payload == {"hook": hook_name}``
    -- but only the FIRST time this ``hook_name`` fires for ``session_id``.
    ``plugin/hooks/session_start.py`` records its own ``hook_alive`` event
    unconditionally (SessionStart fires at most a handful of times per
    session); ``spawn_gate.py``/``post_task.py`` fire on every matched tool
    call, so a caller there wants "at least one row proving this hook was
    armed this session", not one row per spawn -- hence the de-dupe here
    rather than in :func:`append_event` itself (which has no opinion on
    payload shape).

    FX-8 (C-0064 lens B EP-1 Bypass C): the close-refusal ladder's
    ``hooks_disabled``/``hooks_partial`` checks need to tell "SOME hook
    fired" (session_start's own ``{"hook": "session_start"}``) apart from
    "the SPAWN GATE specifically fired" -- this is what makes that
    distinction physically observable, rather than only inferable from
    ``session_start``'s single undifferentiated marker.

    Returns the newly-written event row, or ``None`` if a matching
    ``hook_alive`` row already existed for this ``(session_id, hook_name)``
    pair (no-op -- never writes a second row)."""
    rows = store.ops.execute(
        "SELECT payload FROM event WHERE type = 'hook_alive' AND session_id IS ?", (session_id,)
    ).fetchall()
    for row in rows:
        try:
            if json.loads(row["payload"]).get("hook") == hook_name:
                return None
        except (TypeError, ValueError):
            continue
    return append_event(store, event_type="hook_alive", session_id=session_id, payload={"hook": hook_name})


def _query_event_rows(
    store: Store,
    *,
    workpackage: str | None,
    session_id: str | None,
    event_type: str | None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if workpackage is not None:
        clauses.append("workpackage = ?")
        params.append(workpackage)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if event_type is not None:
        clauses.append("type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Deterministic order: ts first, then SQLite's own implicit rowid as
    # the tiebreaker -- true append order, unlike ordering by event_id
    # (its trailing bits are random, so two events appended within the
    # same millisecond, same ts, can otherwise sort either way; caught by
    # an adversarial test that appends several with default ts= in a tight
    # loop). This is what makes repeated exports byte-stable.
    rows = store.ops.execute(
        f"SELECT *, rowid AS _rowid FROM event {where} ORDER BY ts ASC, _rowid ASC", params
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]


def _event_row_to_obj(row: dict[str, Any]) -> dict[str, Any]:
    """Render one ``event`` row as the jsonl object shape: fixed key order,
    ``payload`` parsed back into a nested JSON object (not a string-escaped
    blob) — this is what origin-project's hand-authored per-agent jsonl files look
    like, and what a consumer reading the export expects."""
    payload = row["payload"]
    return {
        "event_id": row["event_id"],
        "ts": row["ts"],
        "session_id": row["session_id"],
        "launch_id": row["launch_id"],
        "workpackage": row["workpackage"],
        "type": row["type"],
        "payload": json.loads(payload) if payload is not None else None,
        "redactions": row["redactions"],
    }


def tail_events(
    store: Store,
    *,
    workpackage: str | None = None,
    session_id: str | None = None,
    event_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """The most recent (up to ``limit``) events matching the given
    filters, oldest-first (same ordering ``export_events`` uses) so a
    caller reading the tail sees them in append order."""
    rows = _query_event_rows(store, workpackage=workpackage, session_id=session_id, event_type=event_type)
    tail = rows[-limit:] if limit else rows
    return [_event_row_to_obj(r) for r in tail]


def export_events(
    store: Store,
    *,
    workpackage: str | None = None,
    session_id: str | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """All events matching the given filters, in the same deterministic
    (ts, event_id) order :func:`export_jsonl` renders — the read half of
    the export path, usable directly (e.g. by a future MCP/CLI reader)
    without going through a file."""
    rows = _query_event_rows(store, workpackage=workpackage, session_id=session_id, event_type=event_type)
    return [_event_row_to_obj(r) for r in rows]


def render_jsonl(objs: list[dict[str, Any]]) -> str:
    """Render a list of event objects (as produced by :func:`export_events`)
    as newline-delimited JSON: one compact line per object, key order fixed
    by :func:`_event_row_to_obj`'s construction (Python dict insertion
    order, preserved by ``json.dumps`` with ``sort_keys`` left at its
    default False). Given the same input list this is byte-identical on
    every call — no timestamps, ids, or other nondeterminism are added
    here that the stored rows didn't already carry."""
    lines = [json.dumps(o, ensure_ascii=False, separators=(",", ":")) for o in objs]
    return "\n".join(lines) + ("\n" if lines else "")


def export_jsonl(
    store: Store,
    *,
    out_path: Path | str,
    workpackage: str | None = None,
    session_id: str | None = None,
    event_type: str | None = None,
    split_by_workpackage: bool = False,
) -> dict[str, Any]:
    """Render matching events as jsonl at ``out_path`` (written atomically
    via ``trialerror.util.atomic.atomic_write_text``).

    Design Section 4.2: "Per-workpackage/per-session jsonl event files
    become per-consumer *exports* keyed by the ``workpackage`` column, not
    the canonical store." Two modes:

    - Scoped (``workpackage`` and/or ``session_id`` given, or
      ``split_by_workpackage=False``): one jsonl file at ``out_path``
      containing every matching event.
    - ``split_by_workpackage=True`` with NO ``workpackage``/``session_id``
      filter: ``out_path`` is treated as a directory; one
      ``<workpackage>.jsonl`` file is written per distinct ``workpackage``
      value present (events with a NULL workpackage land in
      ``_no_workpackage.jsonl``) — the generalized, store-backed
      replacement for origin-project's per-workpackage events/ directory.

    Re-running this against an unchanged store produces byte-identical
    file(s) ("jsonl export byte-stable", the M5 acceptance criterion).
    """
    objs = export_events(store, workpackage=workpackage, session_id=session_id, event_type=event_type)

    if split_by_workpackage and workpackage is None and session_id is None:
        out_dir = Path(out_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        groups: dict[str, list[dict[str, Any]]] = {}
        for obj in objs:
            key = obj["workpackage"] if obj["workpackage"] is not None else "_no_workpackage"
            groups.setdefault(key, []).append(obj)
        files: dict[str, dict[str, Any]] = {}
        for key in sorted(groups):
            group_objs = groups[key]
            file_path = out_dir / f"{key}.jsonl"
            atomic_write_text(file_path, render_jsonl(group_objs))
            files[key] = {"path": str(file_path), "count": len(group_objs)}
        return {"mode": "split_by_workpackage", "out_dir": str(out_dir), "files": files, "total": len(objs)}

    atomic_write_text(Path(out_path), render_jsonl(objs))
    return {"mode": "single_file", "path": str(out_path), "count": len(objs)}


# ---------------------------------------------------------------------------
# feed (threads + full-text posts) — authorship binding lives here
# ---------------------------------------------------------------------------


def _resolve_orchestrator_session(store: Store, session_id: str | None) -> str:
    """Resolve the session_id an orchestrator (no-launch) post/thread is
    bound to. If ``session_id`` is given it MUST name a currently OPEN
    session (design: "posts as ``orchestrator:<session_id>``, derived from
    the open session") — this is what keeps the orchestrator fallback from
    being a second free-text channel: you cannot claim session X unless X
    is real and open. If omitted, the single currently-open session (if
    any) is used."""
    conn = store.ops
    if session_id is not None:
        row = conn.execute(
            "SELECT session_id FROM session WHERE session_id = ? AND status = 'open'", (session_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"session_id={session_id!r} is not an open session in ops.session "
                "(orchestrator authorship derives from the currently open session, not an arbitrary string)"
            )
        return session_id
    row = conn.execute(
        "SELECT session_id FROM session WHERE status = 'open' ORDER BY opened_ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValidationError(
            "no launch_id given and no open session found in ops.session "
            "(cannot derive 'orchestrator:<session_id>' authorship with nothing open)"
        )
    return row["session_id"]


def _derive_author(store: Store, *, launch_id: str | None, session_id: str | None) -> str:
    """THE authorship-binding function (design F15 / Section 9.9). Every
    caller of this module reaches ``author`` only through here — there is
    no code path that accepts a caller-supplied display name. A
    ``launch_id`` is resolved against ``platform.launch`` (the same table
    the spawn gate's atomic booking-consumption claim writes to) to read
    that launch's own ``agent_kind``; a missing launch_id resolves to the
    open session instead. Either way the resulting string is fully
    determined by a real row this function looked up itself.
    """
    if launch_id is not None:
        launch_row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
        if launch_row is None:
            raise XidTargetMissingError(
                f"launch_id={launch_id!r} has no matching row in platform.launch (author cannot be derived)"
            )
        return f"{launch_row['agent_kind']}:{launch_id}"
    resolved_session = _resolve_orchestrator_session(store, session_id)
    return f"orchestrator:{resolved_session}"


def create_thread(store: Store, *, title: str, launch_id: str, ts: str | None = None) -> dict[str, Any]:
    """Open a new feed thread. ``thread.created_by_launch`` is
    ``NOT NULL`` in the M1-built schema (design Section 4.2 verbatim) —
    TRIALERROR-DEV-NOTE: unlike a feed post, a thread cannot be opened under the
    orchestrator's no-launch identity; every thread's opening act is
    booked to a real launch. This is a faithful-closest-reading of a
    schema this module (lane isolation) does not have license to alter —
    flagged for the M6/M14 builders in the build report, not silently
    special-cased here."""
    row = {
        "thread_id": new_id("THR"),
        "title": title,
        "created_ts": ts or now(),
        "created_by_launch": launch_id,
    }
    return insert(store, "thread", row)


def post_feed(
    store: Store,
    *,
    thread_id: str,
    body: str,
    launch_id: str | None = None,
    session_id: str | None = None,
    in_reply_to: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Post FULL TEXT into a thread (design/origin-project C-0047: never a summary).
    ``author`` is derived, never a parameter — see :func:`_derive_author`
    and the module docstring's authorship-binding contract. Passing
    ``launch_id=None`` posts as the orchestrator, bound to the currently
    open session (or the explicit ``session_id``, which must itself be
    open)."""
    author = _derive_author(store, launch_id=launch_id, session_id=session_id)
    row = {
        "post_id": new_id("POST"),
        "thread_id": thread_id,
        "author": author,
        "launch_id": launch_id,
        "ts": ts or now(),
        "body": body,
        "in_reply_to": in_reply_to,
    }
    return insert(store, "feed_post", row)


def list_threads(store: Store, *, limit: int = 50) -> list[dict[str, Any]]:
    """Threads, newest first (``created_ts`` with ``rowid`` DESC as the
    tiebreaker — see :func:`_query_event_rows`)."""
    rows = store.ops.execute(
        "SELECT *, rowid AS _rowid FROM thread ORDER BY created_ts DESC, _rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]


def get_thread_posts(store: Store, *, thread_id: str) -> list[dict[str, Any]]:
    """Full-text posts in one thread, oldest-first. Ordered by ``ts`` with
    SQLite's implicit ``rowid`` (true append order) as the tiebreaker --
    see :func:`_query_event_rows` for why ordering by the typed id column
    alone is not safe for two posts landing in the same millisecond."""
    rows = store.ops.execute(
        "SELECT *, rowid AS _rowid FROM feed_post WHERE thread_id = ? ORDER BY ts ASC, _rowid ASC", (thread_id,)
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


def post_inbox(store: Store, *, body: str, ts: str | None = None) -> dict[str, Any]:
    """The user's ONE API-backed inbox write path (design Section 4.2:
    "no hand-appended files, per P2"). ``source`` is always ``'user'`` —
    the ``inbox_item`` schema's own CHECK constraint permits no other
    value, so this function does not even expose it as a parameter."""
    row = {
        "item_id": new_id("INBX"),
        "ts": ts or now(),
        "body": body,
        "source": "user",
    }
    return insert(store, "inbox_item", row)


def read_inbox(
    store: Store,
    *,
    session_id: str | None = None,
    mark_read: bool = True,
) -> list[dict[str, Any]]:
    """Unread inbox items (``read_ts IS NULL``), oldest-first. By default
    marks every returned item read, stamping ``read_by_session`` with
    ``session_id`` (nullable — a caller that doesn't know its session id
    yet, e.g. before session boot finishes, may still read/mark without
    one). This is the function M6's boot bundle calls to surface the inbox
    at session start (design Section 5.4 / 9.9: "boot reads inbox").

    Ordered by ``ts`` with ``rowid`` as the tiebreaker (see
    :func:`_query_event_rows`)."""
    rows = store.ops.execute(
        "SELECT *, rowid AS _rowid FROM inbox_item WHERE read_ts IS NULL ORDER BY ts ASC, _rowid ASC"
    ).fetchall()
    items = [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]
    if mark_read and items:
        read_ts = now()
        for item in items:
            update(
                store,
                "inbox_item",
                pk_column="item_id",
                pk_value=item["item_id"],
                changes={"read_ts": read_ts, "read_by_session": session_id},
            )
            item["read_ts"] = read_ts
            item["read_by_session"] = session_id
    return items
