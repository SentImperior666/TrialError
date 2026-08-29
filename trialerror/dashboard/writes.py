"""Stage 3 of the dashboard V2 build -- OPERATOR WRITE ACTIONS. The write
half of ``trialerror.dashboard.data``'s read-only panel layer: one dispatch
table, ``WRITABLE_ACTIONS``, naming every write action the dashboard UI is
allowed to perform, each a thin wrapper calling straight through to the SAME
module-level business-logic function the equivalent ``trialerror <group>`` CLI
verb already calls (``trialerror.artifacts.gates``, ``trialerror.ingest.extract``,
``trialerror.ingest.requests``, ``trialerror.rooms.api``, ``trialerror.events.api``) --
never raw SQL, never a second implementation of a rule those modules already
enforce (design constraint #1 of this build's brief).

**Store discipline** (design constraint #2): every action here opens a REAL
(read-write) :class:`~trialerror.stores.store.Store` via
:func:`trialerror.stores.store.open_store`, does exactly one business-logic call,
and closes it in a ``finally`` -- the same open/close-per-operation shape
every ``trialerror/cli/<group>.py`` handler already uses (see e.g.
``trialerror.cli.gate._run_verify_edit``). Nothing here ever holds a write
connection open across requests; concurrency is left entirely to the
business-logic layer's own ``BEGIN IMMEDIATE`` transactions (SQLite WAL +
short transactions, the house norm -- see ``trialerror.artifacts.gates``/
``trialerror.rooms.api`` docstrings) -- this module adds no locking of its own.

**Authority model note** (rooms): ``trialerror.rooms.api`` has no concept of a
dashboard "operator" role distinct from an agent launch -- ``post_message``/
``score_dp``/``freeze_room`` each only require an EXISTING ``platform.launch``
row (``launch_id``/``by_launch``), validated the same way for every caller.
There is no participant-membership check either (module docstring TRIALERROR-DEV-
NOTE item 1: ``participants`` is informational, not enforced). This means an
operator acting through the dashboard is, to this subsystem, simply another
launch -- exactly as legitimate a participant/moderator as any agent, PROVIDED
they supply a real ``launch_id`` that already exists in ``platform.launch``
(the dashboard has no separate "operator identity" to substitute -- unlike
``trialerror.events.post_feed``, which has an explicit ``launch_id=None ->
orchestrator:<session>`` fallback, rooms has none). Every room write action
below therefore REQUIRES the caller to name a real launch, the same as the
``trialerror room`` CLI's own ``--launch-id``/``--by-launch`` flags.

**Feed posting IS the operator-directive path** (design brief: "feed posting
-- operator directives; authorship is server-derived"): :func:`_do_feed_post`
always calls ``trialerror.events.api.post_feed`` with ``launch_id=None`` -- it
NEVER accepts a caller-supplied author, and always posts as
``orchestrator:<open session>`` (:func:`trialerror.events.api._derive_author`'s
own fallback). Opening a NEW thread is deliberately not offered here:
``trialerror.events.api.create_thread`` requires a real ``launch_id``
(``thread.created_by_launch NOT NULL`` -- that module's own docstring notes
this is a schema constraint this lane has no license to relax), which an
orchestrator-identity post has none of; posting into an EXISTING thread is
the one legitimate no-launch write this subsystem supports.

Every function below returns a plain, JSON-serializable ``dict`` -- never an
:mod:`trialerror.util.envelope` ``AgentEnvelope`` (that shape is CLI/argv
plumbing this HTTP layer does not share) -- via :func:`dispatch`:
``{"ok": True, "result": {...}}`` on success, or ``{"ok": False, "message":
"<the refusing module's own str(exc), verbatim>"}`` on a clean business
refusal. An unexpected exception is deliberately NOT caught here -- it
propagates to the HTTP layer (``trialerror.dashboard.serve``), which reports it
as a 500 rather than silently degrading it to a fake "ok": False (design
constraint #4: never a generic "failed" -- a genuine bug should look like a
bug, not a refusal).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from trialerror.artifacts import gates as gates_api
from trialerror.artifacts.errors import ArtifactsError
from trialerror.events import api as events_api
from trialerror.ingest import extract as extract_api
from trialerror.ingest import requests as ingest_requests
from trialerror.ingest.errors import IngestError
from trialerror.rooms import api as rooms_api
from trialerror.rooms.errors import RoomsError
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store

__all__ = ["WRITABLE_ACTIONS", "REQUIRED_FIELDS", "dispatch"]

#: Every exception a business-logic call below can legitimately raise as a
#: clean refusal (module docstring: "the refusing module's own str(exc)").
#: ``ExtractError``/every ``trialerror.ingest`` subclass is already covered by
#: ``IngestError``; ``ValidationError``/``XidTargetMissingError`` are already
#: covered by ``StoreError`` (see ``trialerror/stores/errors.py``).
_EXPECTED_ERRORS: tuple[type[Exception], ...] = (
    ArtifactsError, IngestError, RoomsError, StoreError, ValueError,
)


def _clean(value: Any) -> Any:
    """``""`` / whitespace-only strings collapse to ``None`` -- an HTML form
    field left blank should behave the same as the field being omitted
    entirely (matches every optional CLI flag's own ``default=None``
    behavior); a non-string value (e.g. a JSON number) passes through
    unchanged."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def _do_verify_edit(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return gates_api.verify_edit(
        store,
        gate_id=body["gate_id"],
        edit_id=body["edit_id"],
        by_launch=body["by_launch"],
        verified_note=_clean(body.get("verified_note")),
    )


def _do_merge_accept(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return extract_api.accept(store, body["prop_id"], by_launch=body["by_launch"])


def _do_merge_reject(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return extract_api.reject(store, body["prop_id"], by_launch=body["by_launch"])


def _do_acquisition_delivered(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """The ONE acquisition transition this build wires (design brief names
    only ``acquisition-delivered``, matching the determinations panel's own
    ``"ACQUISITIONS · ONLY YOU CAN DELIVER THESE"`` framing -- a human
    physically/digitally delivering a requested source is the one
    request-queue step that is genuinely the operator's job; every other
    transition (reject/archive/index/retry) stays on ``trialerror ingest
    request --to <state>``). ``trialerror.ingest.requests.TRANSITIONS`` itself
    still enforces the legal-from-state check -- ``request_state='requested'
    -> 'delivered'`` is the only edge this ever succeeds on; any other
    starting state refuses with :class:`~trialerror.ingest.errors.
    InvalidRequestTransitionError`, surfaced verbatim."""
    return ingest_requests.transition(
        store,
        body["source_id"],
        "delivered",
        launch_id=_clean(body.get("launch_id")),
        note=_clean(body.get("note")),
    )


def _do_room_turn(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return rooms_api.post_message(
        store,
        room_id=body["room_id"],
        launch_id=body["launch_id"],
        dp_id=body["dp_id"],
        body=body["body"],
    )


def _do_room_score(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    try:
        agreement_pct = float(body["agreement_pct"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"room-score: agreement_pct must be a number, got {body.get('agreement_pct')!r}") from exc
    note = _clean(body.get("note"))
    # The CLI never calls an LLM (trialerror/cli/room.py's own module docstring)
    # -- the caller (here: the operator, via the dashboard form) already
    # produced this number; `judge` just hands it through, the exact
    # `trialerror room score --agreement-pct` pattern.
    judge = lambda _envelope: {"agreement_pct": agreement_pct, "note": note}  # noqa: E731
    return rooms_api.score_dp(
        store, room_id=body["room_id"], dp_id=body["dp_id"], judge=judge, by_launch=body["by_launch"],
    )


def _do_room_freeze(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return rooms_api.freeze_room(store, room_id=body["room_id"], by_launch=body["by_launch"], reason=body["reason"])


def _do_feed_post(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """Post into an EXISTING thread only -- see module docstring for why a
    ``feed-post`` action never opens a new thread. ``launch_id`` is always
    ``None``: authorship is server-derived, never caller-settable (matches
    ``trialerror.events.api._derive_author``'s own contract, which this function
    does not and must not work around)."""
    post = events_api.post_feed(
        store,
        thread_id=body["thread_id"],
        body=body["body"],
        launch_id=None,
        session_id=_clean(body.get("session_id")),
        in_reply_to=_clean(body.get("in_reply_to")),
    )
    return {
        "post_id": post["post_id"],
        "thread_id": post["thread_id"],
        "author": post["author"],
        "ts": post["ts"],
    }


#: action name -> (handler, required body fields). Required fields are
#: checked BEFORE opening a store connection (a missing field is a client
#: bug, not a business refusal -- no write connection should be opened for
#: one). Optional fields are read with ``.get()`` inside each handler.
WRITABLE_ACTIONS: dict[str, Callable[[Store, dict[str, Any]], dict[str, Any]]] = {
    "verify-edit": _do_verify_edit,
    "merge-accept": _do_merge_accept,
    "merge-reject": _do_merge_reject,
    "acquisition-delivered": _do_acquisition_delivered,
    "room-turn": _do_room_turn,
    "room-score": _do_room_score,
    "room-freeze": _do_room_freeze,
    "feed-post": _do_feed_post,
}

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "verify-edit": ("gate_id", "edit_id", "by_launch"),
    "merge-accept": ("prop_id", "by_launch"),
    "merge-reject": ("prop_id", "by_launch"),
    "acquisition-delivered": ("source_id",),
    "room-turn": ("room_id", "launch_id", "dp_id", "body"),
    "room-score": ("room_id", "dp_id", "agreement_pct", "by_launch"),
    "room-freeze": ("room_id", "by_launch", "reason"),
    "feed-post": ("thread_id", "body"),
}


def _missing_fields(action: str, body: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in REQUIRED_FIELDS.get(action, ()):
        value = body.get(field)
        if value is None:
            missing.append(field)
        elif isinstance(value, str) and not value.strip():
            missing.append(field)
    return missing


def dispatch(
    action: str,
    *,
    program_root: Path | str | None,
    platform_root: Path | str | None,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Validate + execute one write action. Never raises for an EXPECTED
    refusal (unknown action, no program selected, missing required field, or
    any :data:`_EXPECTED_ERRORS` the business-logic call itself raises) --
    each of those is reported as ``{"ok": False, "message": ...}``. Any
    OTHER exception propagates (module docstring: a genuine bug must look
    like one, never a disguised refusal)."""
    handler = WRITABLE_ACTIONS.get(action)
    if handler is None:
        return {"ok": False, "status": "unknown_action", "message": f"no such write action: {action!r}"}
    if program_root is None:
        return {
            "ok": False, "status": "no_program_root",
            "message": "no program is selected on this dashboard (no --program-root) -- writes need a real program",
        }
    missing = _missing_fields(action, body)
    if missing:
        return {
            "ok": False, "status": "missing_fields",
            "message": f"missing required field(s) for {action!r}: {', '.join(missing)}",
        }

    store = open_store(Path(program_root), platform_root=Path(platform_root) if platform_root else None)
    try:
        result = handler(store, body)
    except _EXPECTED_ERRORS as exc:
        return {"ok": False, "status": type(exc).__name__, "message": str(exc)}
    finally:
        store.close()
    return {"ok": True, "result": result}
