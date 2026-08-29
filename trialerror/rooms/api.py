"""``trialerror.rooms`` — the brainstorm-rooms RUNTIME. Design Section 9.8
(traceability row): "schema in v0 ... runtime skill in v1"; Section 11
names "the rooms runtime" as one of exactly two seductive v0 candidates
deliberately cut to v1 (the other being full KG extraction) — this module
is that deferred build, landing in v1 against the schema M1 already shipped
(``trialerror/stores/schema/ops.py``: ``room``/``room_turn``/``room_score``).

Mechanizes the origin-project requirements notes Section 1.8's origin-project mechanism:
"moderated multi-agent convergence in an append-only room doc; >90%
per-discussion-point agreement bar; freeze-and-escalate path; launch-booked;
deliverable = theory doc + plain-terms companion routed to the user" — plus
its evidence note (MN-033): "solo-generate-then-discuss matches the
group-creativity literature; room size prior 2-3; keep rooms as the cheap
filter."

**LLM-judgment boundary (same contract ``trialerror.verify`` states once and
applies twice — stated here, applies to both room roles):** this module
never calls an LLM itself. :func:`build_participant_turn_envelope` and
:func:`build_moderator_scoring_envelope` each assemble a plain-dict request
envelope (topic, prompt, prior turns, instructions); :func:`score_dp`
accepts a ``judge`` callable — ``judge(envelope) -> {"agreement_pct": ...,
"note"?: ...}`` — that a real subagent fills at runtime, or a deterministic
fake fills in tests (the exact ``trialerror.verify.hypothesis`` pattern:
:func:`~trialerror.verify.hypothesis.build_hypothesis_judgment_envelope` +
``run_hypothesis_verification(..., judge=...)``). A participant's TURN body
itself is not a classification — :func:`post_message` takes the already-
written ``body`` text directly, the same way ``trialerror.events.post_feed``
takes an already-written full-text post.

TRIALERROR-DEV-NOTE (schema gaps this module originally worked around without
touching ``trialerror/stores/schema/`` — schemav2 lane owned that file
concurrently at v1-rooms build time; items 2-5 below were subsequently
CLOSED by the ops-v3 migration, build-v2-polish, ``trialerror/stores/schema/
ops.py``'s ``_V3``/``Migration(version=3, ...)``. Item 1 remains open —
left for a future migration, out of that build's scope):

1. STILL OPEN. ``room`` carries no ``participants`` column, and no
   ``hyperparams``/``convergence_bar``/``rounds_per_dp`` columns — only
   ``room_id | topic | dps JSON | state | created_ts | deliverable_
   artifact_id`` (the last two added by ops-v3, see items 2/5 below). This
   module treats ``dps`` as the one flexible surface it has license to
   shape (the DDL says only "JSON", not a shape) and writes it as ONE JSON
   object: ``{"discussion_points": [{"dp_id","prompt","idea_id"?}, ...],
   "participants": [...], "rounds_per_dp": N, "convergence_bar_pct": 90.0}``
   — not a bare array. (``tests/_store_fixtures.py``'s own minimal
   ``"dps": "[]"`` fixture row is a schema-round-trip placeholder — "some
   valid JSON that satisfies NOT NULL" — not a shape contract; it never
   goes through this module's readers.) A future migration should promote
   ``participants``/``rounds_per_dp`` to real columns (or a child table) so
   they're queryable without a JSON parse.
2. CLOSED (ops-v3). ``room.created_ts``/``room_turn.ts`` are now real
   columns, populated by :func:`create_room`/:func:`post_message`
   ALONGSIDE (not instead of) the companion ``trialerror.events.append_event``
   row every room "moment" already got (see :func:`_emit_room_event`) — the
   event trail remains the ``rooms_stuck`` doctor check's (``trialerror/rooms/
   checks.py``) own data source, unchanged; the new columns are a
   convenience for a direct-SQL reader, not a replacement for it. A
   pre-ops-v3 row has ``created_ts``/``ts`` = ``NULL`` (nothing to backfill
   from at the DDL level); its history still lives in the event trail.
3. CLOSED (ops-v3). ``room_score`` now carries real ``room_id``/``dp_id``
   columns and a composite ``PRIMARY KEY (room_id, dp_id)`` — the
   ``"<room_id>::<dp_id>"`` ``dp_ref`` namespacing convention is RETIRED
   for this table specifically (every ``room_score`` read/write below now
   goes straight through ``room_id``+``dp_id``, via raw SQL rather than
   ``trialerror.stores.get``/``update``, since those only support a single
   ``pk_column``). :func:`_dp_ref` itself is UNCHANGED and still used for
   ``room_turn.dp_ref`` (that table's own composite PK was always
   ``(room_id, seq)``, never namespaced — item 3 never applied to it) and
   for constructing the same human-readable ``"<room_id>::<dp_id>"`` string
   this module's event payloads and CLI surface (``trialerror/cli/room.py``)
   already display.
4. CLOSED (ops-v3). A new per-discussion-point child table, ``room_link
   (room_id, dp_id, idea_id)`` — composite PK, ``idea_id`` a registered XID
   (``trialerror.stores.xid.XID_REGISTRY``) -> ``knowledge.idea`` — promotes the
   OPTIONAL ``idea_id`` a ``dps`` JSON entry may carry (point 1's own
   convention) to a real, queryable row. :func:`create_room` now writes one
   ``room_link`` row per discussion point that carries an ``idea_id``,
   ALONGSIDE (not instead of) the existing ``dps`` JSON entry. The
   NEITHER-ownership invariant itself (REQUIREMENTS Section 1.8;
   "participants must not own the ideas they vet") still enforces at the
   APPLICATION level against the ``dps`` JSON, unchanged — see
   :func:`_check_neither_ownership` — ``room_link`` is a new queryable
   audit surface, not a new source of truth for that check.
5. CLOSED (ops-v3). ``room.deliverable_artifact_id`` (a same-file FK ->
   ``artifact(artifact_id)`` — both tables live in ops.db, so this is NOT
   an XID, see ``trialerror.stores.xid``'s own module docstring on same-file FKs
   being non-members of that registry) now links a converged room straight
   to its deliverable. :func:`register_room_deliverable` sets it ALONGSIDE
   (not instead of) the pre-existing ``artifact.attrs.room_id`` /
   ``room_deliverable_registered`` event mirrors — belt-and-suspenders, not
   a replacement of either.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trialerror.artifacts._txn import raw_insert
from trialerror.artifacts.registry import create_artifact
from trialerror.events.api import append_event
from trialerror.rooms.errors import ConvergenceBarNotMetError, OwnershipConflictError
from trialerror.rooms.state_machine import assert_legal_transition
from trialerror.stores import get as store_get
from trialerror.stores import insert as store_insert
from trialerror.stores import update as store_update
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.store import Store
from trialerror.util.atomic import atomic_write_text
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "CONVERGENCE_BAR_PCT",
    "DEFAULT_ROUNDS_PER_DP",
    "PARTICIPANT_RANGE",
    "create_room",
    "get_room",
    "get_discussion_points",
    "list_room_turns",
    "get_dp_score",
    "post_message",
    "build_participant_turn_envelope",
    "build_moderator_scoring_envelope",
    "score_dp",
    "check_room_converged",
    "converge_room",
    "freeze_room",
    "get_freeze_reason",
    "register_room_deliverable",
    "render_room_markdown",
    "export_room",
]

#: FIXED (mission brief, verbatim: "convergence_bar=0.90 FIXED"; design
#: Section 9.8 / REQUIREMENTS Section 1.8: "≥90%"/">90%" agreement bar) —
#: unlike ``participants``/``rounds_per_dp`` below, this is not a
#: ``create_room`` parameter at all. Percent scale (0-100), matching
#: ``room_score.agreement_pct``'s own established convention
#: (``tests/_store_fixtures.py``'s fixture row: ``92.5``).
CONVERGENCE_BAR_PCT = 90.0

#: MN-033 evidence note default ("keep rooms as the cheap filter").
DEFAULT_ROUNDS_PER_DP = 2

#: MN-033 room-size prior ("room size prior 2-3") — soft-enforced by
#: :func:`create_room` (``enforce_participant_range=False`` overrides).
PARTICIPANT_RANGE: tuple[int, int] = (2, 3)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _dp_ref(room_id: str, dp_id: str) -> str:
    """Namespace a short, room-local ``dp_id`` into the globally-unique
    ``"<room_id>::<dp_id>"`` string this module still uses for
    ``room_turn.dp_ref`` and for display (event payloads, the CLI) — module
    TRIALERROR-DEV-NOTE item 3: ``room_score`` itself no longer needs this (its
    own ``room_id``/``dp_id`` columns disambiguate directly, since ops-v3),
    see :func:`_get_room_score_row`."""
    return f"{room_id}::{dp_id}"


def _get_room_score_row(store: Store, *, room_id: str, dp_id: str) -> dict[str, Any] | None:
    """``room_score`` lookup by its real ``(room_id, dp_id)`` composite PK
    (module TRIALERROR-DEV-NOTE item 3, CLOSED by ops-v3) — raw SQL because
    ``trialerror.stores.get`` only supports a single ``pk_column``."""
    row = store.ops.execute(
        "SELECT * FROM room_score WHERE room_id = ? AND dp_id = ?", (room_id, dp_id)
    ).fetchone()
    return dict(row) if row is not None else None


def _require_launch_exists(store: Store, launch_id: str, *, field_name: str) -> None:
    """Same manual XID pre-check ``trialerror.artifacts.gates`` uses (see that
    module's own helper of the same name) — needed here wherever a launch
    id is written via :func:`~trialerror.artifacts._txn.raw_insert` (which does
    NOT auto-validate XIDs) or where a state mutation must not land before
    a companion event's launch id is known-good (avoiding the "state
    changed, but the audit event silently failed" half-write)."""
    row = store.platform.execute("SELECT 1 FROM launch WHERE launch_id = ? LIMIT 1", (launch_id,)).fetchone()
    if row is None:
        raise XidTargetMissingError(
            f"{field_name} = {launch_id!r} has no matching row in platform.launch (XID refused)"
        )


def _require_launch_exists_if_given(store: Store, launch_id: str | None, *, field_name: str) -> None:
    if launch_id is not None:
        _require_launch_exists(store, launch_id, field_name=field_name)


def _require_idea_exists(store: Store, idea_id: str, *, field_name: str) -> None:
    """Same manual XID pre-check :func:`_require_launch_exists` uses, for
    ``knowledge.idea`` -- needed here because :func:`create_room` validates
    every discussion point's OPTIONAL ``idea_id`` (module TRIALERROR-DEV-NOTE
    item 4, CLOSED by ops-v3's ``room_link`` table) BEFORE writing the
    ``room`` row itself, the same "refuse before any write" discipline
    ``by_launch`` already gets (:func:`_require_launch_exists_if_given`) —
    ``trialerror.stores.insert``'s own XID validation would catch a bad
    ``idea_id`` too, but only once ``room_link`` is written, which is
    already after ``room`` -- too late to avoid a half-written room."""
    row = store.knowledge.execute("SELECT 1 FROM idea WHERE idea_id = ? LIMIT 1", (idea_id,)).fetchone()
    if row is None:
        raise XidTargetMissingError(
            f"{field_name} = {idea_id!r} has no matching row in knowledge.idea (XID refused)"
        )


def _emit_room_event(
    store: Store,
    *,
    event_type: str,
    room_id: str,
    launch_id: str | None = None,
    ts: str | None = None,
    payload_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every room "moment" this module cares about that the DDL doesn't
    timestamp gets mirrored here (module TRIALERROR-DEV-NOTE item 2) — the ONE
    place a room-related event is appended, so every event type below
    (``room_created``, ``room_turn``, ``room_dp_scored``, ``room_converged``,
    ``room_frozen``, ``room_deliverable_registered``) always carries
    ``payload.room_id``, which every reader (doctor checks included) relies
    on for grouping."""
    payload: dict[str, Any] = {"room_id": room_id}
    if payload_extra:
        payload.update(payload_extra)
    return append_event(store, event_type=event_type, payload=payload, launch_id=launch_id, ts=ts)


def _require_room(store: Store, room_id: str) -> dict[str, Any]:
    room = get_room(store, room_id)
    if room is None:
        raise ValueError(f"no such room: {room_id!r}")
    return room


def _room_config(room: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(room["dps"])


def _find_dp(config: Mapping[str, Any], dp_id: str, *, room_id: str, caller: str) -> dict[str, Any]:
    dp = next((d for d in config["discussion_points"] if d["dp_id"] == dp_id), None)
    if dp is None:
        raise ValueError(f"{caller}: room {room_id!r} has no discussion point {dp_id!r}")
    return dp


def _normalize_discussion_points(discussion_points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, dp in enumerate(discussion_points, start=1):
        dp_id = dp.get("dp_id") or f"DP{i}"
        if dp_id in seen_ids:
            raise ValueError(f"create_room: duplicate dp_id {dp_id!r}")
        seen_ids.add(dp_id)
        prompt = dp.get("prompt")
        if not prompt:
            raise ValueError(f"create_room: discussion point {dp_id!r} is missing a required 'prompt'")
        normalized.append({"dp_id": dp_id, "prompt": prompt, "idea_id": dp.get("idea_id")})
    if not normalized:
        raise ValueError("create_room: at least one discussion point is required")
    return normalized


def _check_neither_ownership(store: Store, *, dp: Mapping[str, Any], launch_id: str) -> None:
    """The NEITHER-ownership invariant (module TRIALERROR-DEV-NOTE item 4):
    refuses a turn where the posting launch is the same launch that
    authored the ``knowledge.idea`` row this discussion point exists to
    vet. A ``dp`` with no ``idea_id`` (or one that no longer resolves —
    ``knowledge.idea`` is a same-file FK-free landing zone, design Section
    4.1) has nothing to enforce and is silently allowed."""
    idea_id = dp.get("idea_id")
    if not idea_id:
        return
    idea_row = store_get(store, "idea", pk_column="idea_id", pk_value=idea_id)
    if idea_row is None:
        return
    if idea_row["author_launch"] == launch_id:
        raise OwnershipConflictError(
            f"post_message: launch {launch_id!r} authored idea {idea_id!r} and cannot post a "
            f"vetting turn on discussion point {dp['dp_id']!r}, which reviews that idea "
            "(NEITHER-ownership invariant, the origin-project requirements notes Section 1.8)"
        )


# ---------------------------------------------------------------------------
# room lifecycle
# ---------------------------------------------------------------------------


def create_room(
    store: Store,
    *,
    topic: str,
    discussion_points: Sequence[Mapping[str, Any]],
    participants: Sequence[str],
    rounds_per_dp: int = DEFAULT_ROUNDS_PER_DP,
    enforce_participant_range: bool = True,
    by_launch: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Open a new room at ``state='open'`` with its discussion points and
    hyperparameters sealed into ``room.dps`` (see module TRIALERROR-DEV-NOTE item
    1 for the JSON shape). ``participants`` is a list of participant
    labels/roles (e.g. lens names or seat ids) — soft-enforced to
    :data:`PARTICIPANT_RANGE` (MN-033's room-size prior) unless
    ``enforce_participant_range=False``.

    ``by_launch``, if given, must name an existing launch — validated
    up front so a bad id refuses BEFORE the room row is written (module
    TRIALERROR-DEV-NOTE item 2's companion ``room_created`` event always
    succeeds once the room row itself has landed)."""
    _require_launch_exists_if_given(store, by_launch, field_name="by_launch")
    if enforce_participant_range and not (PARTICIPANT_RANGE[0] <= len(participants) <= PARTICIPANT_RANGE[1]):
        raise ValueError(
            f"create_room: participants must number {PARTICIPANT_RANGE[0]}-{PARTICIPANT_RANGE[1]} "
            f"(MN-033 room-size prior), got {len(participants)} — pass enforce_participant_range=False "
            "to override with a stated reason"
        )
    if rounds_per_dp < 1:
        raise ValueError(f"create_room: rounds_per_dp must be >= 1, got {rounds_per_dp}")
    normalized_dps = _normalize_discussion_points(discussion_points)
    for dp in normalized_dps:
        if dp.get("idea_id"):
            _require_idea_exists(store, dp["idea_id"], field_name=f"discussion_points[{dp['dp_id']}].idea_id")

    dps_payload = {
        "discussion_points": normalized_dps,
        "participants": list(participants),
        "rounds_per_dp": rounds_per_dp,
        "convergence_bar_pct": CONVERGENCE_BAR_PCT,
    }
    ts = ts or now()
    row = {
        "room_id": new_id("ROOM"),
        "topic": topic,
        "dps": json.dumps(dps_payload, ensure_ascii=False),
        "state": "open",
        "created_ts": ts,
    }
    written = store_insert(store, "room", row)
    # module TRIALERROR-DEV-NOTE item 4 (CLOSED, ops-v3): one room_link row per
    # discussion point that carries an idea_id -- alongside, not instead of,
    # the dps JSON entry the NEITHER-ownership check itself still reads.
    for dp in normalized_dps:
        if dp.get("idea_id"):
            store_insert(
                store,
                "room_link",
                {"room_id": written["room_id"], "dp_id": dp["dp_id"], "idea_id": dp["idea_id"]},
            )
    _emit_room_event(
        store,
        event_type="room_created",
        room_id=written["room_id"],
        launch_id=by_launch,
        ts=ts,
        payload_extra={
            "topic": topic,
            "dp_ids": [d["dp_id"] for d in normalized_dps],
            "participants": list(participants),
            "rounds_per_dp": rounds_per_dp,
        },
    )
    return written


def get_room(store: Store, room_id: str) -> dict[str, Any] | None:
    return store_get(store, "room", pk_column="room_id", pk_value=room_id)


def get_discussion_points(store: Store, room_id: str) -> list[dict[str, Any]]:
    room = _require_room(store, room_id)
    return _room_config(room)["discussion_points"]


def list_room_turns(store: Store, *, room_id: str, dp_id: str | None = None) -> list[dict[str, Any]]:
    """Turns for ``room_id``, oldest-first (``seq`` order — the room_turn
    PK's own append-order column, no rowid tiebreak needed since ``seq`` is
    assigned strictly monotonically by :func:`post_message`). Restricts to
    one discussion point when ``dp_id`` is given (validated against the
    room's own discussion points, same as every other reader here)."""
    room = _require_room(store, room_id)
    if dp_id is not None:
        config = _room_config(room)
        _find_dp(config, dp_id, room_id=room_id, caller="list_room_turns")
        dp_ref = _dp_ref(room_id, dp_id)
        rows = store.ops.execute(
            "SELECT * FROM room_turn WHERE room_id = ? AND dp_ref = ? ORDER BY seq ASC", (room_id, dp_ref)
        ).fetchall()
    else:
        rows = store.ops.execute(
            "SELECT * FROM room_turn WHERE room_id = ? ORDER BY seq ASC", (room_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_dp_score(store: Store, *, room_id: str, dp_id: str) -> dict[str, Any] | None:
    room = _require_room(store, room_id)
    config = _room_config(room)
    _find_dp(config, dp_id, room_id=room_id, caller="get_dp_score")
    return _get_room_score_row(store, room_id=room_id, dp_id=dp_id)


# ---------------------------------------------------------------------------
# turn-taking
# ---------------------------------------------------------------------------


def post_message(
    store: Store,
    *,
    room_id: str,
    launch_id: str,
    dp_id: str,
    body: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append one turn to the room doc. Authorship via the same server-side
    derivation contract ``trialerror.events.post_feed`` uses (module docstring's
    LLM-judgment-boundary note doesn't apply here — a turn's TEXT is
    supplied by the caller, already written — but the AUTHOR is not: there
    is no ``author`` parameter, only ``launch_id``, validated to exist
    before anything is written (:func:`_require_launch_exists`) and stored
    verbatim in ``room_turn.author_launch`` (a registered XID column,
    ``trialerror.stores.xid.XID_REGISTRY``).

    Refuses (:class:`ValueError`) if the room is not ``open``, or if
    ``dp_id`` names no discussion point in this room; refuses
    (:class:`~trialerror.rooms.errors.OwnershipConflictError`) under the
    NEITHER-ownership invariant (see :func:`_check_neither_ownership`).

    ``seq`` is assigned as ``MAX(seq)+1`` for this room under a
    ``BEGIN IMMEDIATE`` write lock (the same race-safety convention
    ``trialerror.artifacts.gates`` uses for its own multi-statement writes) — a
    genuine concurrent-post race surfaces as a clean
    :class:`~trialerror.stores.errors.ValidationError`, never a silently
    duplicated ``seq``.

    Returns a dict merging the written ``room_turn`` row with ``ts`` (the
    companion event's timestamp — see module TRIALERROR-DEV-NOTE item 2:
    ``room_turn`` itself carries no ``ts`` column)."""
    room = _require_room(store, room_id)
    if room["state"] != "open":
        raise ValueError(
            f"post_message: room {room_id!r} is not open (state={room['state']!r}); no further turns accepted"
        )
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="post_message")
    _check_neither_ownership(store, dp=dp, launch_id=launch_id)
    _require_launch_exists(store, launch_id, field_name="launch_id")

    dp_ref = _dp_ref(room_id, dp_id)
    ts = ts or now()
    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM room_turn WHERE room_id = ?", (room_id,)
        ).fetchone()["n"]
        raw_insert(
            conn,
            "room_turn",
            {
                "room_id": room_id,
                "seq": next_seq,
                "author_launch": launch_id,
                "dp_ref": dp_ref,
                "body": body,
                "ts": ts,
            },
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(
            f"post_message: integrity violation on room {room_id!r} "
            f"(possible concurrent-post race on the same seq): {exc}"
        ) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise

    _emit_room_event(
        store,
        event_type="room_turn",
        room_id=room_id,
        launch_id=launch_id,
        ts=ts,
        payload_extra={"dp_id": dp_id, "dp_ref": dp_ref, "seq": next_seq},
    )
    return {"room_id": room_id, "seq": next_seq, "author_launch": launch_id, "dp_ref": dp_ref, "body": body, "ts": ts}


def build_participant_turn_envelope(store: Store, *, room_id: str, dp_id: str) -> dict[str, Any]:
    """A plain-dict request envelope for a participant about to write the
    NEXT turn on ``dp_id`` — topic, the discussion point's own prompt, every
    prior turn on it (the append-only room doc so far, oldest-first), and
    the round number this turn would be. Exactly the ``trialerror.verify.
    hypothesis.build_hypothesis_judgment_envelope`` pattern (module
    docstring's LLM-judgment-boundary note), except the "judgment" here is
    generative (write the next turn) rather than a classification — this
    function never calls an LLM and never writes anything; the caller reads
    the envelope, produces a turn body externally, then calls
    :func:`post_message` with it."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="build_participant_turn_envelope")
    prior_turns = list_room_turns(store, room_id=room_id, dp_id=dp_id)
    return {
        "kind": "room_participant_turn",
        "room_id": room_id,
        "topic": room["topic"],
        "dp_id": dp_id,
        "prompt": dp["prompt"],
        "prior_turns": [{"seq": t["seq"], "author_launch": t["author_launch"], "body": t["body"]} for t in prior_turns],
        "round_number": len(prior_turns) + 1,
        "rounds_per_dp": config["rounds_per_dp"],
        "instructions": (
            "Write this round's turn for the discussion point above, building on (or "
            "explicitly disagreeing with) prior turns rather than repeating them. Return "
            "plain text for the turn body — the room doc is append-only, so nothing written "
            "here can be edited later, only superseded by a later turn."
        ),
    }


# ---------------------------------------------------------------------------
# moderator scoring
# ---------------------------------------------------------------------------


def build_moderator_scoring_envelope(store: Store, *, room_id: str, dp_id: str) -> dict[str, Any]:
    """A plain-dict judgment-request envelope for the moderator scoring
    ``dp_id`` — topic, the discussion point's own prompt, every turn posted
    on it so far, and the fixed convergence bar. Same envelope-building
    role as :func:`~trialerror.verify.hypothesis.build_hypothesis_judgment_envelope`
    (module docstring's LLM-judgment-boundary note): this function never
    calls a judge itself — see :func:`score_dp`, which does, via an
    injected callable."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="build_moderator_scoring_envelope")
    turns = list_room_turns(store, room_id=room_id, dp_id=dp_id)
    return {
        "kind": "room_moderator_score",
        "room_id": room_id,
        "topic": room["topic"],
        "dp_id": dp_id,
        "prompt": dp["prompt"],
        "turns": [{"seq": t["seq"], "author_launch": t["author_launch"], "body": t["body"]} for t in turns],
        "convergence_bar_pct": CONVERGENCE_BAR_PCT,
        "instructions": (
            f"Read every turn above on this discussion point and judge what fraction of the "
            f"participating positions now agree. Return {{'agreement_pct': <0-100>, 'note'?: "
            f"<str>}} — the room converges on this discussion point at agreement_pct >= "
            f"{CONVERGENCE_BAR_PCT}; include a short note naming the specific disagreement "
            "when the score is below bar."
        ),
    }


def score_dp(
    store: Store,
    *,
    room_id: str,
    dp_id: str,
    judge: Callable[[Mapping[str, Any]], Any],
    by_launch: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Build the moderator's judgment envelope, call ``judge(envelope)``
    (never an LLM call made by this module itself — see module docstring),
    and upsert the resulting score into ``room_score`` (keyed by the
    namespaced :func:`_dp_ref`, module TRIALERROR-DEV-NOTE item 3).

    ``judge`` must return either a ``{"agreement_pct": <0-100>, "note"?:
    <str>}`` mapping or a bare number — refuses (:class:`ValueError`) on a
    non-numeric or out-of-range result. ``by_launch`` (the moderator's own
    launch) is validated to exist BEFORE any write (module TRIALERROR-DEV-NOTE
    item 2's half-write concern: a bad launch id must never leave a scored
    ``room_score`` row with a silently-failed audit event).

    Returns the written ``room_score`` row plus ``dp_id``/``note``/
    ``converged`` (``agreement_pct >= CONVERGENCE_BAR_PCT``) for
    convenience."""
    _require_launch_exists(store, by_launch, field_name="by_launch")
    envelope = build_moderator_scoring_envelope(store, room_id=room_id, dp_id=dp_id)
    result = judge(envelope)
    if isinstance(result, Mapping):
        agreement_pct = result.get("agreement_pct")
        note = result.get("note")
    else:
        agreement_pct = result
        note = None
    try:
        agreement_pct = float(agreement_pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"score_dp: judge returned a non-numeric agreement_pct: {agreement_pct!r}")
    if not (0.0 <= agreement_pct <= 100.0):
        raise ValueError(f"score_dp: agreement_pct must be within [0, 100], got {agreement_pct!r}")

    dp_ref = _dp_ref(room_id, dp_id)  # display/audit only now -- see _get_room_score_row
    ts = ts or now()
    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT 1 FROM room_score WHERE room_id = ? AND dp_id = ?", (room_id, dp_id)
        ).fetchone()
        if existing is None:
            raw_insert(conn, "room_score", {"room_id": room_id, "dp_id": dp_id, "agreement_pct": agreement_pct, "frozen": 0})
        else:
            conn.execute(
                "UPDATE room_score SET agreement_pct = ? WHERE room_id = ? AND dp_id = ?",
                (agreement_pct, room_id, dp_id),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    converged = agreement_pct >= CONVERGENCE_BAR_PCT
    _emit_room_event(
        store,
        event_type="room_dp_scored",
        room_id=room_id,
        launch_id=by_launch,
        ts=ts,
        payload_extra={"dp_id": dp_id, "dp_ref": dp_ref, "agreement_pct": agreement_pct, "note": note, "converged": converged},
    )
    row = _get_room_score_row(store, room_id=room_id, dp_id=dp_id)
    assert row is not None
    return {**row, "dp_id": dp_id, "note": note, "converged": converged}


# ---------------------------------------------------------------------------
# convergence / freeze
# ---------------------------------------------------------------------------


def check_room_converged(store: Store, room_id: str) -> dict[str, Any]:
    """Read-only: for every discussion point in the room, its current
    score (``None`` if never scored) and whether it individually meets
    :data:`CONVERGENCE_BAR_PCT`. ``all_converged`` is ``True`` only when
    EVERY discussion point is both scored and at/above bar — never a
    silent pass on an unscored DP."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    per_dp: list[dict[str, Any]] = []
    all_scored = True
    all_at_bar = True
    for dp in config["discussion_points"]:
        dp_ref = _dp_ref(room_id, dp["dp_id"])
        score_row = _get_room_score_row(store, room_id=room_id, dp_id=dp["dp_id"])
        if score_row is None:
            all_scored = False
            all_at_bar = False
            per_dp.append({"dp_id": dp["dp_id"], "dp_ref": dp_ref, "agreement_pct": None, "converged": False})
            continue
        converged = score_row["agreement_pct"] >= CONVERGENCE_BAR_PCT
        all_at_bar = all_at_bar and converged
        per_dp.append(
            {"dp_id": dp["dp_id"], "dp_ref": dp_ref, "agreement_pct": score_row["agreement_pct"], "converged": converged}
        )
    return {
        "room_id": room_id,
        "convergence_bar_pct": CONVERGENCE_BAR_PCT,
        "all_scored": all_scored,
        "all_converged": all_scored and all_at_bar,
        "per_dp": per_dp,
    }


def converge_room(store: Store, *, room_id: str, by_launch: str, ts: str | None = None) -> dict[str, Any]:
    """``open -> converged`` — refuses (:class:`~trialerror.rooms.errors.
    ConvergenceBarNotMetError`) unless :func:`check_room_converged` reports
    ``all_converged``; refuses (:class:`~trialerror.rooms.errors.
    IllegalRoomTransitionError`) if the room is already ``converged``/
    ``frozen`` (the state graph itself — ``trialerror.rooms.state_machine`` —
    has no outgoing edge from either terminal state, so this naturally
    also refuses converging an already-frozen room)."""
    room = _require_room(store, room_id)
    _require_launch_exists(store, by_launch, field_name="by_launch")
    status = check_room_converged(store, room_id)
    if not status["all_converged"]:
        unmet = [d["dp_id"] for d in status["per_dp"] if not d["converged"]]
        raise ConvergenceBarNotMetError(
            f"converge_room: room {room_id!r} has discussion point(s) below the "
            f"{CONVERGENCE_BAR_PCT}% bar or not yet scored: {unmet}"
        )
    assert_legal_transition(room["state"], "converged")
    ts = ts or now()
    store_update(store, "room", pk_column="room_id", pk_value=room_id, changes={"state": "converged"})
    _emit_room_event(store, event_type="room_converged", room_id=room_id, launch_id=by_launch, ts=ts, payload_extra={"per_dp": status["per_dp"]})
    return _require_room(store, room_id)


def freeze_room(store: Store, *, room_id: str, by_launch: str, reason: str, ts: str | None = None) -> dict[str, Any]:
    """``open -> frozen`` — origin-project's "freeze-and-escalate path". ``reason`` is
    required (a freeze with no stated reason defeats the point of
    escalating to a human) and is recorded on the companion ``room_frozen``
    event (module TRIALERROR-DEV-NOTE item 2: ``room_score`` has no per-room
    "reason" column, and ``room_turn`` is for discussion-point turns, not a
    room-level moderator act — the event trail is the faithful home for
    this). Refuses (:class:`~trialerror.rooms.errors.
    IllegalRoomTransitionError`) if the room is not currently ``open``."""
    if not reason:
        raise ValueError("freeze_room: reason is required (freeze-and-escalate needs something to escalate)")
    room = _require_room(store, room_id)
    _require_launch_exists(store, by_launch, field_name="by_launch")
    assert_legal_transition(room["state"], "frozen")
    ts = ts or now()
    store_update(store, "room", pk_column="room_id", pk_value=room_id, changes={"state": "frozen"})
    _emit_room_event(store, event_type="room_frozen", room_id=room_id, launch_id=by_launch, ts=ts, payload_extra={"reason": reason})
    return _require_room(store, room_id)


def get_freeze_reason(store: Store, room_id: str) -> str | None:
    """The ``reason`` recorded by the room's most recent ``room_frozen``
    event, or ``None`` if the room was never frozen (or is not itself
    ``frozen`` right now, though this reads history regardless of current
    state)."""
    rows = store.ops.execute(
        "SELECT payload FROM event WHERE type = 'room_frozen' ORDER BY ts DESC, rowid DESC"
    ).fetchall()
    for r in rows:
        payload = json.loads(r["payload"])
        if payload.get("room_id") == room_id:
            return payload.get("reason")
    return None


# ---------------------------------------------------------------------------
# deliverable registration hook
# ---------------------------------------------------------------------------


def register_room_deliverable(
    store: Store,
    *,
    room_id: str,
    type_key: str,
    title: str,
    path: str,
    sha256: str,
    by_launch: str,
    purpose: str = "room_deliverable",
) -> dict[str, Any]:
    """A converged room "owes" its theory-doc (+ plain-terms companion)
    artifact (REQUIREMENTS Section 1.8: "deliverable = theory doc + plain-
    terms companion routed to the user"). Refuses (:class:`ValueError`)
    unless the room is already ``converged``. Wires to
    ``trialerror.artifacts.registry.create_artifact`` (a DRAFT, unregistered/
    ungated artifact row — this module's job stops at "the deliverable now
    exists as a tracked artifact", after which the normal artifact/gate
    machinery in ``trialerror.artifacts`` takes over unchanged).

    ``type_key`` must already name a registered ``template`` row — same
    same-file-FK contract :func:`~trialerror.artifacts.registry.create_artifact`
    itself enforces; this module does not seed templates (out of lane, see
    module TRIALERROR-DEV-NOTE preamble).

    The room<->artifact link (no DDL column exists for it — module
    TRIALERROR-DEV-NOTE item 5) is recorded two ways: ``artifact.attrs.room_id``
    and a companion ``room_deliverable_registered`` event."""
    room = _require_room(store, room_id)
    if room["state"] != "converged":
        raise ValueError(
            f"register_room_deliverable: room {room_id!r} must be 'converged' before its deliverable "
            f"can be registered (state={room['state']!r})"
        )
    artifact = create_artifact(
        store, type_key=type_key, title=title, path=path, sha256=sha256, by_launch=by_launch,
        purpose=purpose, attrs={"room_id": room_id},
    )
    # module TRIALERROR-DEV-NOTE item 5 (CLOSED, ops-v3): same-file FK, alongside
    # (not instead of) the attrs.room_id / companion-event mirrors above.
    store_update(
        store, "room", pk_column="room_id", pk_value=room_id,
        changes={"deliverable_artifact_id": artifact["artifact_id"]},
    )
    _emit_room_event(
        store, event_type="room_deliverable_registered", room_id=room_id, launch_id=by_launch,
        payload_extra={"artifact_id": artifact["artifact_id"], "type_key": type_key},
    )
    return artifact


# ---------------------------------------------------------------------------
# export — the rendered "room doc" view
# ---------------------------------------------------------------------------


def render_room_markdown(store: Store, room_id: str) -> str:
    """Render the full append-only room transcript as markdown — the "room
    doc" the design's own §9.8 traceability row names. A pure view: every
    discussion point, its current score (or "not yet scored"), and every
    turn posted on it in order; the freeze reason (if any) as a trailing
    section. Deterministic given the same store contents (no wall-clock
    read other than what's already stored)."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    turns = list_room_turns(store, room_id=room_id)
    turns_by_dp_ref: dict[str, list[dict[str, Any]]] = {}
    for t in turns:
        turns_by_dp_ref.setdefault(t["dp_ref"], []).append(t)

    lines: list[str] = [f"# {room['topic']}", "", f"room_id: `{room_id}`  |  state: **{room['state']}**", ""]
    for dp in config["discussion_points"]:
        dp_ref = _dp_ref(room_id, dp["dp_id"])
        score_row = _get_room_score_row(store, room_id=room_id, dp_id=dp["dp_id"])
        score_str = f"{score_row['agreement_pct']:.1f}%" if score_row is not None else "not yet scored"
        lines.append(f"## {dp['dp_id']}: {dp['prompt']}")
        lines.append("")
        lines.append(f"_agreement: {score_str} (bar: {CONVERGENCE_BAR_PCT}%)_")
        lines.append("")
        dp_turns = turns_by_dp_ref.get(dp_ref, [])
        if not dp_turns:
            lines.append("_(no turns yet)_")
            lines.append("")
        for t in dp_turns:
            lines.append(f"**turn {t['seq']}** — `{t['author_launch']}`")
            lines.append("")
            lines.append(t["body"])
            lines.append("")

    if room["state"] == "frozen":
        reason = get_freeze_reason(store, room_id)
        lines.append("## Freeze")
        lines.append("")
        lines.append(reason or "_(no reason recorded)_")
        lines.append("")

    return "\n".join(lines)


def export_room(store: Store, room_id: str, *, out_path: Path | str) -> dict[str, Any]:
    """Render + write the room doc atomically (``trialerror.util.atomic.
    atomic_write_text`` — design's "rendered markdown files ... are views"
    convention, Section 3.2)."""
    text = render_room_markdown(store, room_id)
    out_path = Path(out_path)
    atomic_write_text(out_path, text)
    return {"room_id": room_id, "path": str(out_path), "bytes": len(text.encode("utf-8"))}
