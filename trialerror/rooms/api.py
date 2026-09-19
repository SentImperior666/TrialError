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
6. STILL OPEN, and the reason the framework procedure below carries so
   much in the event trail: ``room_turn`` has no ``kind`` column and
   ``room_score`` has no ``label`` column. A turn's KIND, a participant's
   structured FINAL STANCE, a turn's neutral EXTRACT and a scored point's
   discrete LABEL therefore ride on the companion events this module
   already emits for every room moment — the same home item 2 gave a
   freeze reason, read back by the readers below
   (:func:`turn_kinds`, :func:`list_final_stances`,
   :func:`list_turn_extracts`, :func:`get_dp_label`) rather than by a
   column. This is also what the design asks for in so many words: the
   label goes "first in ``score_dp``'s result/event", not into a new
   column, so no migration is taken here.

**The framework procedure (AIIF), add-only on top of everything above.**
The charter's own §6 amendment adds seven rules to this runtime, and the
design's integration table names this module for all of them. What landed,
rule by rule, and what each one is enforced BY:

- **admission order** — :func:`build_admission_order`: every consolidated
  idea is roomed, ordered by a seeded draw stratified on (arm, card),
  packed into rooms of the charter size in charter-size batches, remainder
  carried in that same order. Nothing in a dossier gates or orders it, and
  :func:`admission_order_hash` is the digest a round pre-registers so the
  order it ran can be compared with the order it escrowed.
- **turn kinds** — :data:`TURN_KINDS` validated by :func:`post_message`,
  with ``closure`` refused in a participant's own first round on the point.
- **blind first turn** — ``blind_first_turn=True`` at :func:`create_room`;
  :func:`build_participant_turn_envelope` withholds every prior turn while
  the point is still in round 1, so round 1 is simultaneous rather than
  sequential.
- **rank-all + final stance** — ``rank_all=True`` appends one procedural
  discussion point; each participant files one structured stance record on
  it (:func:`post_final_stance`), and :func:`converge_room` refuses until
  every participant has.
- **agreement_pct computed, label first** — :func:`score_dp` computes the
  number from those structured stances whenever a point has them, and
  REFUSES a judge that tries to supply it. The judge's job is the discrete
  ``label`` (:data:`DP_LABELS`), which is stated first in both the result
  and the event. :data:`CONVERGENCE_BAR_PCT` and the both-or-eliminated
  rule are untouched: what changed is what the number is computed from.
- **neutral extraction** — :func:`build_extract_envelope` +
  :func:`record_turn_extracts`, and a moderator scoring envelope that
  carries the extracts, drops ``author_launch`` outright, and withholds raw
  turn prose once every turn has an extract. This is NEW code and not
  ``trialerror.summarize``: that subsystem summarizes documents and
  collections, and neither a turn nor a stance is either.
- **buster position** — :func:`record_buster_position` stores the
  assumption-buster's own output once, and every later envelope for that
  seat re-injects it VERBATIM together with the buster's own turns so far,
  so its position is consistent by record rather than re-argued per spawn.
- **NEITHER by lens name** — :func:`create_room` and :func:`score_dp` both
  resolve an idea's owning LENS through its author launch's
  ``attrs.lens_name`` (:func:`lens_name_of_launch`) and refuse the room
  outright; :func:`post_message`'s own check now fires on the lens name as
  well as the launch id, which is what makes it fire for a re-spawned lens
  at all.

One reading recorded rather than assumed: the charter says "a FINAL-STANCE
decision point per participant" and the design says "a final DP has each
participant rank every idea". This module implements ONE procedural
discussion point per room carrying one stance RECORD per participant —
discussion points are about ideas, and N parallel procedural points would
make the stance records unorderable against each other.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trialerror.artifacts._txn import raw_insert
from trialerror.artifacts.registry import create_artifact
from trialerror.events.api import append_event, append_event_in_txn
from trialerror.lens.quota import derive_rng
from trialerror.lens.stratify import ARMS
from trialerror.rooms.errors import (
    AdmissionOrderError,
    ConvergenceBarNotMetError,
    IllegalRoomTransitionError,
    OwnershipConflictError,
    StanceIncompleteError,
    TurnKindRefusedError,
)
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
    "TURN_KINDS",
    "ROUND_ONE_TURN_KINDS",
    "DEFAULT_TURN_KIND",
    "DP_LABELS",
    "CRITERIA",
    "STANCE_VALUES",
    "DOSSIER_LABEL_KEYS",
    "RANK_ALL_DP_ID",
    "EXTRACT_FIELDS",
    "DEFAULT_IDEAS_PER_ROOM",
    "DEFAULT_ROOMS_PER_BATCH",
    "ADMISSION_BATCH_ROOM_RANGE",
    "create_room",
    "get_room",
    "get_discussion_points",
    "list_room_turns",
    "get_dp_score",
    "post_message",
    "turn_kinds",
    "lens_name_of_launch",
    "build_participant_turn_envelope",
    "build_rank_all_envelope",
    "post_final_stance",
    "list_final_stances",
    "compute_agreement_pct",
    "record_buster_position",
    "get_buster_position",
    "build_extract_envelope",
    "record_turn_extracts",
    "list_turn_extracts",
    "build_moderator_scoring_envelope",
    "score_dp",
    "get_dp_label",
    "check_room_converged",
    "converge_room",
    "freeze_room",
    "get_freeze_reason",
    "register_room_deliverable",
    "render_room_markdown",
    "export_room",
    "build_admission_order",
    "admission_order_hash",
    "consolidated_ideas_for_admission",
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

#: The charter §6 turn kinds. A ``position`` states one; a ``question``
#: asks for an entailment the point has not supplied; a ``closure`` says
#: the point is settled. The vocabulary is closed because the
#: weak-entailment-first rule is stated over it: a participant's OPENING
#: turn on a point may be either of the first two and never the third.
TURN_KINDS: tuple[str, ...] = ("position", "question", "closure")

#: What a participant's own FIRST round on a discussion point may be
#: (charter §6(ii)/(iii); Sawyer Fig. 3.2, Stasser): closure turns and
#: preference statements are refused before round 2, so a point cannot be
#: closed by the first voice that reaches it.
ROUND_ONE_TURN_KINDS: tuple[str, ...] = ("position", "question")

#: What :func:`post_message` assumes when a caller names no kind — the
#: ordinary turn, and the one value that keeps every pre-framework caller
#: (and every pre-framework room) behaving exactly as before.
DEFAULT_TURN_KIND = "position"

#: The moderator's discrete verdict vocabulary (design §3.1 Phase 5(f)),
#: returned FIRST and independently of any number: (a) and (b) are the two
#: charter §6 criterion questions.
DP_LABELS: tuple[str, ...] = ("MEETS-A", "MEETS-B", "MEETS-BOTH", "FAILS", "UNDECIDED")

#: The two charter §6 criteria every final stance answers.
CRITERIA: tuple[str, ...] = ("a", "b")

#: A structured stance on one criterion. Ordinal-free and deliberately
#: three-valued: "unclear" is a real position and folding it into "no"
#: would manufacture agreement that nobody stated.
STANCE_VALUES: tuple[str, ...] = ("yes", "no", "unclear")

#: The ONLY dossier keys a discussion point may carry (design §3.1 Phase 5:
#: "the dossier's *labels* (not its rationale)"). Distances, the
#: KNOWN-MECHANIC flag, the candidate hits and every rationale field are
#: absent by construction rather than by discipline — see
#: :func:`_normalize_dossier_labels`.
DOSSIER_LABEL_KEYS: tuple[str, ...] = ("label_inventory", "label_corpus", "judged")

#: The id of the one procedural discussion point ``rank_all=True`` appends.
#: It is not an idea point: it carries no ``idea_id``, it is never scored
#: against the bar, and it is excluded from the convergence accounting.
RANK_ALL_DP_ID = "RANK-ALL"

#: What a neutral extract of one turn must carry (design §3.1 Phase 5(d)).
#: Every field is required: an extract missing its residual disagreement is
#: a summary, and a summary is what the extract pass exists to replace.
EXTRACT_FIELDS: tuple[str, ...] = ("claim", "anchors", "stance_a", "stance_b", "residual_disagreement")

#: Charter-size room: six ideas (= six discussion points) per room, the
#: number the design's own Phase 5 budget line is computed from ("36 turn
#: spawns (6 DPs × 2 rounds × 3)").
DEFAULT_IDEAS_PER_ROOM = 6

#: Charter-size batch, at the low end of the band below — a batch is a
#: sitting's worth of rooms, and the conservative end is the one a budget
#: can actually carry.
DEFAULT_ROOMS_PER_BATCH = 4

#: Charter §6's own batch band ("batches of 4-8 rooms"), soft-enforced by
#: :func:`build_admission_order` exactly the way :data:`PARTICIPANT_RANGE`
#: is by :func:`create_room` (``enforce_batch_band=False`` overrides with a
#: stated reason).
ADMISSION_BATCH_ROOM_RANGE: tuple[int, int] = (4, 8)


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


#: What every pass that reads a point's turn PROSE says when it is handed
#: the procedural rank-all point. The point's turns ARE the structured final
#: stances, and their bodies open with ``FINAL STANCE — <participant>``
#: (:func:`render_final_stance`) — so extracting or scoring them would put
#: participant identity straight back into the two passes built to keep it
#: out (the extract envelope drops ``author_launch`` deliberately; the
#: moderator's envelope drops it always, design §5.2 item 5).
_PROCEDURAL_PROSE_TAIL = (
    "is never extracted or scored — its turns are the structured final stances and their bodies "
    "name the seat that filed them, which is exactly what these passes withhold. Run the pass "
    "over the room's idea points instead"
)


def _refuse_procedural_point(
    dp: Mapping[str, Any], *, dp_id: str, room_id: str, caller: str, tail: str
) -> None:
    """Refuse the one procedural rank-all point, in the shape
    :func:`score_dp` refuses it — one message form for one rule, so a caller
    that meets it in the extract pass recognises it from the scoring pass."""
    if dp.get("procedural"):
        raise ValueError(
            f"{caller}: {dp_id!r} is room {room_id!r}'s procedural rank-all point and {tail}"
        )


def _idea_dps(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The room's IDEA discussion points — every point except the one
    procedural rank-all point ``rank_all=True`` appends. The convergence
    bar, the both-or-eliminated rule and the stance arithmetic are all
    stated over these; the procedural point is where the stances are filed,
    not a thing to be scored."""
    return [dp for dp in config["discussion_points"] if not dp.get("procedural")]


def _participants(config: Mapping[str, Any]) -> list[str]:
    return [str(p) for p in config.get("participants") or []]


def _normalize_dossier_labels(dp: Mapping[str, Any], *, dp_id: str) -> dict[str, Any] | None:
    """The dossier LABELS a discussion point carries into the room, and
    nothing else (design §3.1 Phase 5: "the record verbatim, the two charter
    §6 criterion questions, the dossier's *labels* (not its rationale)").

    A caller may hand either ``dossier`` (the whole dossier file the screen
    wrote) or ``dossier_labels`` (just the labels). From a whole dossier,
    exactly :data:`DOSSIER_LABEL_KEYS` are lifted out and every other key —
    the distances, the KNOWN-MECHANIC flag, the candidate hits with their
    retrieved text — is left behind; that is the safe direction, and it is
    why the wide form is accepted at all. From an explicit
    ``dossier_labels``, an unexpected key is REFUSED rather than dropped: a
    caller who named a field deserves to be told it has no home here, and a
    rationale smuggled in under a label-shaped name is exactly what the
    barrier exists to stop."""
    explicit = dp.get("dossier_labels")
    if explicit is not None:
        if not isinstance(explicit, Mapping):
            raise ValueError(f"create_room: discussion point {dp_id!r} dossier_labels must be a mapping")
        unknown = sorted(set(explicit) - set(DOSSIER_LABEL_KEYS))
        if unknown:
            raise ValueError(
                f"create_room: discussion point {dp_id!r} dossier_labels carries {unknown} — a room "
                f"receives the dossier's labels only ({list(DOSSIER_LABEL_KEYS)}); distances, flags and "
                "rationale never enter a participant envelope"
            )
        return {key: explicit.get(key) for key in DOSSIER_LABEL_KEYS if key in explicit}
    dossier = dp.get("dossier")
    if dossier is None:
        return None
    if not isinstance(dossier, Mapping):
        raise ValueError(f"create_room: discussion point {dp_id!r} dossier must be a mapping")
    return {key: dossier.get(key) for key in DOSSIER_LABEL_KEYS}


def _normalize_discussion_points(discussion_points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, dp in enumerate(discussion_points, start=1):
        dp_id = dp.get("dp_id") or f"DP{i}"
        if dp_id in seen_ids:
            raise ValueError(f"create_room: duplicate dp_id {dp_id!r}")
        if dp_id == RANK_ALL_DP_ID:
            raise ValueError(
                f"create_room: {RANK_ALL_DP_ID!r} is reserved for the procedural rank-all point "
                "(pass rank_all=True to append it); an idea point needs its own id"
            )
        seen_ids.add(dp_id)
        prompt = dp.get("prompt")
        if not prompt:
            raise ValueError(f"create_room: discussion point {dp_id!r} is missing a required 'prompt'")
        entry: dict[str, Any] = {"dp_id": dp_id, "prompt": prompt, "idea_id": dp.get("idea_id")}
        labels = _normalize_dossier_labels(dp, dp_id=dp_id)
        if labels is not None:
            entry["dossier_labels"] = labels
        normalized.append(entry)
    if not normalized:
        raise ValueError("create_room: at least one discussion point is required")
    return normalized


def _rank_all_dp(normalized: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The one procedural discussion point ``rank_all=True`` appends. Its
    prompt names every idea point in the room, in admission order, because
    the rank-all act is "rank EVERY idea in this room on both criteria" —
    a participant handed the instruction without the list would be ranking
    whatever it remembers."""
    ids = ", ".join(dp["dp_id"] for dp in normalized)
    return {
        "dp_id": RANK_ALL_DP_ID,
        "prompt": (
            f"RANK-ALL: rank every idea point in this room ({ids}) on criterion (a) and on criterion "
            "(b), then state your structured final stance on each point. This point is procedural: it "
            "is never scored against the convergence bar."
        ),
        "idea_id": None,
        "procedural": True,
    }


def lens_name_of_launch(store: Store, launch_id: str | None) -> str | None:
    """The lens name a launch was booked under (``launch.attrs.lens_name``,
    written by ``trialerror.lens.export.export_launch_bookable``), or
    ``None`` for a launch that declares none.

    This is the seam the NEITHER-ownership invariant needs and did not
    have. ``_check_neither_ownership`` compared ``idea.author_launch`` with
    the POSTING launch, and a lens re-spawned for its next turn carries a
    new launch id, so the comparison could never fire twice for the same
    lens — the design names that precisely, and names the fix: compare the
    NAME, resolved through the launch row's own attrs.

    ``None`` is not an error. A launch booked outside the lens export (the
    orchestrator's own, a moderator's, a test's) has no lens name, and the
    invariant simply has no name-level claim to make about it."""
    if not launch_id:
        return None
    row = store.platform.execute("SELECT attrs FROM launch WHERE launch_id = ?", (launch_id,)).fetchone()
    if row is None or not row["attrs"]:
        return None
    try:
        attrs = json.loads(row["attrs"])
    except (TypeError, ValueError):
        return None
    name = attrs.get("lens_name") if isinstance(attrs, Mapping) else None
    return str(name) if name else None


def _idea_owner(store: Store, idea_id: str | None) -> dict[str, Any]:
    """``{"idea_id", "author_launch", "lens_name"}`` for the idea a
    discussion point vets — every field ``None`` when there is no idea, or
    when the idea row no longer resolves (``knowledge.idea`` is an
    FK-free landing zone, design Section 4.1)."""
    if not idea_id:
        return {"idea_id": None, "author_launch": None, "lens_name": None}
    row = store_get(store, "idea", pk_column="idea_id", pk_value=idea_id)
    if row is None:
        return {"idea_id": idea_id, "author_launch": None, "lens_name": None}
    author_launch = row["author_launch"]
    return {
        "idea_id": idea_id,
        "author_launch": author_launch,
        "lens_name": lens_name_of_launch(store, author_launch),
    }


def _room_events(store: Store, *, event_type: str, room_id: str) -> list[dict[str, Any]]:
    """Every event of one type for one room, oldest-first, payload decoded.
    The same ``json_extract`` predicate :func:`get_freeze_reason` pushes
    into SQL (M-WA-7), applied to the event types the framework procedure
    keeps there (module TRIALERROR-DEV-NOTE item 6)."""
    rows = store.ops.execute(
        "SELECT payload, ts, launch_id FROM event WHERE type = ? "
        "AND json_extract(payload, '$.room_id') = ? ORDER BY ts ASC, rowid ASC",
        (event_type, room_id),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping):
            out.append({**payload, "_ts": row["ts"], "_launch_id": row["launch_id"]})
    return out


def _check_neither_ownership(store: Store, *, dp: Mapping[str, Any], launch_id: str) -> None:
    """The NEITHER-ownership invariant (module TRIALERROR-DEV-NOTE item 4):
    refuses a turn where the posting launch — or the posting LENS — is the
    one that authored the ``knowledge.idea`` row this discussion point
    exists to vet. A ``dp`` with no ``idea_id`` (or one that no longer
    resolves — ``knowledge.idea`` is a same-file FK-free landing zone,
    design Section 4.1) has nothing to enforce and is silently allowed.

    The LENS-name half is what makes this check fire at all for a framework
    round: every turn is its own booked spawn, so the author's launch id is
    never the poster's launch id twice, and the launch comparison alone
    would pass a lens vetting its own idea on its second turn. Both halves
    run; only the name half needs a booking that declares a lens name, so
    the launch half stays as the floor under it."""
    idea_id = dp.get("idea_id")
    if not idea_id:
        return
    owner = _idea_owner(store, idea_id)
    if owner["author_launch"] is None:
        return
    if owner["author_launch"] == launch_id:
        raise OwnershipConflictError(
            f"post_message: launch {launch_id!r} authored idea {idea_id!r} and cannot post a "
            f"vetting turn on discussion point {dp['dp_id']!r}, which reviews that idea "
            "(NEITHER-ownership invariant, the origin-project requirements notes Section 1.8)"
        )
    poster_lens = lens_name_of_launch(store, launch_id)
    if poster_lens is not None and owner["lens_name"] == poster_lens:
        raise OwnershipConflictError(
            f"post_message: lens {poster_lens!r} authored idea {idea_id!r} (on launch "
            f"{owner['author_launch']!r}) and cannot post a vetting turn on discussion point "
            f"{dp['dp_id']!r}, which reviews that idea — NEITHER ownership is checked by lens name, "
            "because a re-spawned lens posts under a new launch id every turn"
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
    blind_first_turn: bool = False,
    rank_all: bool = False,
    buster: str | None = None,
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
    succeeds once the room row itself has landed).

    The three framework flags are all add-only, all sealed into the same
    ``dps`` JSON, and all default off — a pre-framework room created without
    them behaves exactly as it did:

    ``blind_first_turn``
        Round 1 is simultaneous: :func:`build_participant_turn_envelope`
        withholds every prior turn until the point's round 1 is complete, so
        no participant's opening position is anchored on another's.
    ``rank_all``
        Appends the one procedural :data:`RANK_ALL_DP_ID` point. Each
        participant files one structured stance record on it
        (:func:`post_final_stance`), and :func:`converge_room` refuses until
        all of them have — "RANK-ALL before any verdict", charter §6(iv).
    ``buster``
        Names which participant holds the assumption-buster seat, so its
        recorded position can be re-injected verbatim into every one of its
        turn envelopes (charter §6(v)). Must be one of ``participants``.

    **NEITHER ownership, checked here by lens name (charter §6(vi)).** A
    participant whose lens authored any idea this room vets refuses the ROOM,
    not just the turn. Catching it at creation is the point: the alternative
    is a room that stands, gets spawned into, and only refuses the owning
    lens's turn once the budget for it is already booked."""
    _require_launch_exists_if_given(store, by_launch, field_name="by_launch")
    if enforce_participant_range and not (PARTICIPANT_RANGE[0] <= len(participants) <= PARTICIPANT_RANGE[1]):
        raise ValueError(
            f"create_room: participants must number {PARTICIPANT_RANGE[0]}-{PARTICIPANT_RANGE[1]} "
            f"(MN-033 room-size prior), got {len(participants)} — pass enforce_participant_range=False "
            "to override with a stated reason"
        )
    if rounds_per_dp < 1:
        raise ValueError(f"create_room: rounds_per_dp must be >= 1, got {rounds_per_dp}")
    participant_list = list(participants)
    if buster is not None and buster not in participant_list:
        raise ValueError(
            f"create_room: buster {buster!r} is not one of this room's participants {participant_list!r} "
            "— the buster seat is a seat AT the table, not a label beside it"
        )
    normalized_dps = _normalize_discussion_points(discussion_points)
    for dp in normalized_dps:
        if dp.get("idea_id"):
            _require_idea_exists(store, dp["idea_id"], field_name=f"discussion_points[{dp['dp_id']}].idea_id")
            owner = _idea_owner(store, dp["idea_id"])
            if owner["lens_name"] is not None and owner["lens_name"] in participant_list:
                raise OwnershipConflictError(
                    f"create_room: participant {owner['lens_name']!r} is the lens that authored idea "
                    f"{dp['idea_id']!r}, which discussion point {dp['dp_id']!r} exists to vet — NEITHER "
                    "ownership is checked by lens name at room creation (charter §6(vi)); seat a "
                    "different lens or move the idea to another room"
                )
    if rank_all:
        normalized_dps.append(_rank_all_dp(normalized_dps))

    dps_payload = {
        "discussion_points": normalized_dps,
        "participants": participant_list,
        "rounds_per_dp": rounds_per_dp,
        "convergence_bar_pct": CONVERGENCE_BAR_PCT,
        "blind_first_turn": bool(blind_first_turn),
        "rank_all_dp_id": RANK_ALL_DP_ID if rank_all else None,
        "buster": buster,
        "turn_kinds": list(TURN_KINDS),
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
            "participants": participant_list,
            "rounds_per_dp": rounds_per_dp,
            "blind_first_turn": bool(blind_first_turn),
            "rank_all": bool(rank_all),
            "buster": buster,
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
    kind: str = DEFAULT_TURN_KIND,
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
    NEITHER-ownership invariant (see :func:`_check_neither_ownership`);
    refuses (:class:`~trialerror.rooms.errors.TurnKindRefusedError`) a
    ``kind`` outside :data:`TURN_KINDS`, or a ``closure`` in the POSTER's
    own first round on this point (charter §6(ii)/(iii): weak entailment
    first). "Its own first round" is read per participant — whether this
    launch's lens has already spoken on this point — rather than off the
    room's turn count, because a room where one seat is slow is still a room
    where the fast seat has had its say.

    ``kind`` rides on the companion ``room_turn`` event rather than in a
    column (module TRIALERROR-DEV-NOTE item 6) and is read back by
    :func:`turn_kinds`. Its default, :data:`DEFAULT_TURN_KIND`, is what
    keeps every pre-framework caller behaving exactly as before.

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
    if kind not in TURN_KINDS:
        raise TurnKindRefusedError(
            f"post_message: kind must be one of {list(TURN_KINDS)}, got {kind!r}"
        )
    _check_neither_ownership(store, dp=dp, launch_id=launch_id)
    _require_launch_exists(store, launch_id, field_name="launch_id")
    if kind == "closure" and not dp.get("procedural"):
        spoken = _rounds_spoken(store, room_id=room_id, dp_id=dp_id, launch_id=launch_id)
        if spoken == 0:
            raise TurnKindRefusedError(
                f"post_message: a 'closure' turn is refused in round 1 on discussion point {dp_id!r} — "
                f"this author has not spoken on it yet, and round 1 admits {list(ROUND_ONE_TURN_KINDS)} "
                "only (weak entailment first, charter §6(ii)/(iii)). State a position or ask a question; "
                "close the point from round 2"
            )

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
        payload_extra={"dp_id": dp_id, "dp_ref": dp_ref, "seq": next_seq, "kind": kind},
    )
    return {
        "room_id": room_id, "seq": next_seq, "author_launch": launch_id, "dp_ref": dp_ref,
        "body": body, "kind": kind, "ts": ts,
    }


def turn_kinds(store: Store, *, room_id: str, dp_id: str | None = None) -> dict[int, str]:
    """``{seq: kind}`` for a room's turns, read back off the companion
    ``room_turn`` events (module TRIALERROR-DEV-NOTE item 6 — ``room_turn``
    has no ``kind`` column). A turn posted before kinds existed carries
    none and answers :data:`DEFAULT_TURN_KIND`, which is what it was."""
    out: dict[int, str] = {}
    for payload in _room_events(store, event_type="room_turn", room_id=room_id):
        seq = payload.get("seq")
        if seq is None:
            continue
        if dp_id is not None and payload.get("dp_id") != dp_id:
            continue
        out[int(seq)] = str(payload.get("kind") or DEFAULT_TURN_KIND)
    return out


def _turns_by_author(store: Store, *, room_id: str, dp_id: str) -> dict[str, list[dict[str, Any]]]:
    """This point's turns grouped by the LENS that posted them (falling back
    to the launch id for a launch that declares no lens name), oldest-first.
    The lens name is the stable identity across a round: every turn is its
    own spawn, so the launch id is not."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    kinds = turn_kinds(store, room_id=room_id, dp_id=dp_id)
    for turn in list_room_turns(store, room_id=room_id, dp_id=dp_id):
        author = lens_name_of_launch(store, turn["author_launch"]) or turn["author_launch"]
        grouped[author].append({**turn, "kind": kinds.get(turn["seq"], DEFAULT_TURN_KIND)})
    return dict(grouped)


def _rounds_spoken(store: Store, *, room_id: str, dp_id: str, launch_id: str) -> int:
    """How many turns this author has ALREADY posted on this point, counted
    by lens name where the booking declares one and by launch id where it
    does not. Zero means the author's next turn is its round 1."""
    author = lens_name_of_launch(store, launch_id) or launch_id
    return len(_turns_by_author(store, room_id=room_id, dp_id=dp_id).get(author, []))


def _writing_first_round(
    store: Store,
    *,
    room_id: str,
    dp_id: str,
    config: Mapping[str, Any],
    participant: str | None,
    n_turns: int,
) -> bool:
    """Is the turn ABOUT TO BE WRITTEN still part of round 1 on this point?

    Read per PARTICIPANT, never off the room's turn count — the same reading
    :func:`post_message` already enforces the closure rule with
    (:func:`_rounds_spoken`), and for the same reason. A total turn count
    says nothing about whether a given seat has spoken: a seat that posts
    twice on one point pushes the count past ``n_participants``, and a
    round-1 rule decided off that count then treats the OTHER seat's FIRST
    turn as a round-2 turn — which, under ``blind_first_turn``, means handing
    that seat the very turns it was supposed to write blind to. The barrier
    would fail open in the one direction it exists to prevent.

    With ``participant`` given the answer is about that seat alone: round 1
    is still its round until it has posted on this point. With no participant
    named the caller is asking about the POINT, and round 1 is still running
    while any declared seat has yet to post on it. A room that declares no
    participants has no seats to count, so the point's own emptiness is the
    only reading left — and it is the one the pre-framework behaviour had."""
    by_author = _turns_by_author(store, room_id=room_id, dp_id=dp_id)
    if participant is not None:
        return not by_author.get(participant)
    seats = _participants(config)
    if seats:
        return any(seat not in by_author for seat in seats)
    return n_turns == 0


def build_participant_turn_envelope(
    store: Store, *, room_id: str, dp_id: str, participant: str | None = None
) -> dict[str, Any]:
    """A plain-dict request envelope for a participant about to write the
    NEXT turn on ``dp_id`` — topic, the discussion point's own prompt, the
    prior turns on it (the append-only room doc so far, oldest-first), the
    round this turn belongs to, and which turn kinds that round admits.
    Exactly the ``trialerror.verify.hypothesis.
    build_hypothesis_judgment_envelope`` pattern (module docstring's
    LLM-judgment-boundary note), except the "judgment" here is generative
    (write the next turn) rather than a classification — this function never
    calls an LLM and never writes anything; the caller reads the envelope,
    produces a turn body externally, then calls :func:`post_message` with it.

    **``round_number`` is the DP's round, not its turn count** (corrected
    with the framework procedure). It was ``len(prior_turns) + 1``, which in
    a three-participant room reported "round 4 of 2" on the fourth turn of a
    two-round point and could not express "round 1" at all once more than one
    seat had spoken. The raw count is still reported, as ``turn_index``.

    **The round-1 RULES are read per participant, not off ``round_number``**
    (:func:`_writing_first_round`). ``round_number`` is a reported field —
    how far the POINT has got, as a reader of the room doc would say it — and
    a seat that posts twice advances it without the other seat having spoken.
    ``blinded`` and ``allowed_turn_kinds`` are therefore decided by whether
    THIS seat has posted on this point (and, with no seat named, by whether
    any declared seat still owes a turn), which is the same reading
    :func:`post_message` enforces the closure refusal with. Deciding them off
    the count let a fast seat's second turn lift the blind on the slow
    seat's first one.

    ``participant`` names which seat is about to write, when the caller knows
    it. Two things depend on it: the assumption-buster's own position
    envelope (re-injected VERBATIM — charter §6(v)), and nothing else. Every
    other field is the same for every seat, which is deliberate: a room where
    the envelope differs per seat in ways the seat cannot see is a room whose
    turns are not comparable.

    ``dossier_labels`` rides along when the point carries them — the labels
    alone, never the rationale or the distances (:data:`DOSSIER_LABEL_KEYS`).
    ``blinded`` says plainly when prior turns were withheld, so a participant
    cannot read an empty list as an empty room."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="build_participant_turn_envelope")
    prior_turns = list_room_turns(store, room_id=room_id, dp_id=dp_id)
    kinds = turn_kinds(store, room_id=room_id, dp_id=dp_id)
    n_participants = max(len(_participants(config)), 1)
    round_number = len(prior_turns) // n_participants + 1
    first_round = _writing_first_round(
        store, room_id=room_id, dp_id=dp_id, config=config, participant=participant,
        n_turns=len(prior_turns),
    )
    blind = bool(config.get("blind_first_turn")) and first_round
    shown = [] if blind else prior_turns
    allowed_kinds = list(ROUND_ONE_TURN_KINDS if first_round else TURN_KINDS)
    envelope: dict[str, Any] = {
        "kind": "room_participant_turn",
        "room_id": room_id,
        "topic": room["topic"],
        "dp_id": dp_id,
        "prompt": dp["prompt"],
        "prior_turns": [
            {
                "seq": t["seq"],
                "author_launch": t["author_launch"],
                "body": t["body"],
                "turn_kind": kinds.get(t["seq"], DEFAULT_TURN_KIND),
            }
            for t in shown
        ],
        "blinded": blind,
        "turn_index": len(prior_turns) + 1,
        "round_number": round_number,
        "writing_first_round": first_round,
        "rounds_per_dp": config["rounds_per_dp"],
        "allowed_turn_kinds": allowed_kinds,
        "instructions": (
            "Write this round's turn for the discussion point above, building on (or "
            "explicitly disagreeing with) prior turns rather than repeating them. Return "
            "plain text for the turn body — the room doc is append-only, so nothing written "
            "here can be edited later, only superseded by a later turn. Name the turn's kind "
            f"from {allowed_kinds}"
            + (
                " — round 1 states positions and asks questions; it does not close the point, and "
                "it carries no prior turns because every seat writes it at once."
                if first_round
                else "."
            )
        ),
    }
    if dp.get("dossier_labels") is not None:
        envelope["dossier_labels"] = dict(dp["dossier_labels"])
    if participant is not None:
        envelope["participant"] = participant
        if config.get("buster") and participant == config["buster"]:
            envelope["buster_position"] = _buster_position_envelope(
                store, room_id=room_id, config=config, participant=participant
            )
    return envelope


def _buster_position_envelope(
    store: Store, *, room_id: str, config: Mapping[str, Any], participant: str
) -> dict[str, Any]:
    """The assumption-buster's own position, carried BY RECORD rather than
    re-argued: its recorded output verbatim, plus every turn it has already
    posted in this room, verbatim, in order.

    The point of the verbatim rule (design §5.1's own pre-mortem row) is that
    a "consistent" position re-derived at each spawn is not a consistent
    position — it is a new position that happens to rhyme. Nothing here
    summarizes, truncates or re-orders: what O7 measures is the others'
    movement relative to a fixed text, so the text has to be fixed."""
    recorded = get_buster_position(store, room_id=room_id)
    # Collected across the whole room, not one point: the buster's position
    # is a position on the round's assumption, and it does not reset per DP.
    own: list[dict[str, Any]] = []
    kinds = turn_kinds(store, room_id=room_id)
    for turn in list_room_turns(store, room_id=room_id):
        author = lens_name_of_launch(store, turn["author_launch"]) or turn["author_launch"]
        if author == participant:
            own.append(
                {
                    "seq": turn["seq"],
                    "dp_ref": turn["dp_ref"],
                    "body": turn["body"],
                    "turn_kind": kinds.get(turn["seq"], DEFAULT_TURN_KIND),
                }
            )
    return {
        "participant": participant,
        "recorded_position": recorded,
        "stances_so_far": own,
        "instructions": (
            "This is your own recorded position and every turn you have already posted in this "
            "room, verbatim. Hold it: argue from it, extend it, or state plainly which part of it "
            "the discussion has defeated. Do not quietly restate it as something else."
        ),
    }


# ---------------------------------------------------------------------------
# rank-all, final stances, and the buster's carried position
# ---------------------------------------------------------------------------


def _require_rank_all(config: Mapping[str, Any], *, room_id: str, caller: str) -> str:
    dp_id = config.get("rank_all_dp_id")
    if not dp_id:
        raise ValueError(
            f"{caller}: room {room_id!r} has no rank-all discussion point — create it with "
            "create_room(..., rank_all=True); the stance record has nowhere to land otherwise"
        )
    return str(dp_id)


def build_rank_all_envelope(store: Store, *, room_id: str, participant: str | None = None) -> dict[str, Any]:
    """The envelope for the final rank-all act: every idea point in the room
    with its prompt and its dossier LABELS, both criterion questions, and the
    stance vocabulary the answer must use.

    This is the act charter §6(iv) puts before any verdict. It asks for two
    things that a prose turn cannot be mined for afterwards without a judge
    reading intent into it: a RANKING of every idea on each criterion, and a
    discrete STANCE per idea per criterion. ``agreement_pct`` is then
    arithmetic over the stances (:func:`compute_agreement_pct`) rather than a
    number somebody read out of paragraphs."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    rank_all_dp_id = _require_rank_all(config, room_id=room_id, caller="build_rank_all_envelope")
    idea_dps = _idea_dps(config)
    envelope: dict[str, Any] = {
        "kind": "room_rank_all",
        "room_id": room_id,
        "topic": room["topic"],
        "dp_id": rank_all_dp_id,
        "criteria": list(CRITERIA),
        "stance_values": list(STANCE_VALUES),
        "points": [
            {
                "dp_id": dp["dp_id"],
                "prompt": dp["prompt"],
                **({"dossier_labels": dict(dp["dossier_labels"])} if dp.get("dossier_labels") is not None else {}),
            }
            for dp in idea_dps
        ],
        "instructions": (
            "Rank EVERY point above on criterion (a) and, separately, on criterion (b) — best first, "
            "no ties, every point appearing exactly once in each ranking. Then state a final stance "
            f"per point per criterion from {list(STANCE_VALUES)}. Return "
            "{'ranking': {'a': [dp_id, ...], 'b': [dp_id, ...]}, 'stances': {dp_id: {'a': ..., "
            "'b': ...}}, 'note'?: <str>}. 'unclear' is a real answer; do not round it to 'no'."
        ),
    }
    if participant is not None:
        envelope["participant"] = participant
        if config.get("buster") and participant == config["buster"]:
            envelope["buster_position"] = _buster_position_envelope(
                store, room_id=room_id, config=config, participant=participant
            )
    return envelope


def _validate_stances(
    stances: Mapping[str, Any], *, idea_dp_ids: Sequence[str], participant: str
) -> dict[str, dict[str, str]]:
    missing: list[str] = []
    bad: list[str] = []
    out: dict[str, dict[str, str]] = {}
    for dp_id in idea_dp_ids:
        entry = stances.get(dp_id)
        if not isinstance(entry, Mapping):
            missing.append(dp_id)
            continue
        row: dict[str, str] = {}
        for criterion in CRITERIA:
            value = entry.get(criterion)
            if value is None:
                missing.append(f"{dp_id}.{criterion}")
            elif str(value) not in STANCE_VALUES:
                bad.append(f"{dp_id}.{criterion}={value!r}")
            else:
                row[criterion] = str(value)
        if len(row) == len(CRITERIA):
            out[dp_id] = row
    unknown = sorted(set(stances) - set(idea_dp_ids))
    if missing or bad or unknown:
        raise StanceIncompleteError(
            f"post_final_stance: {participant!r}'s stance record is not usable — "
            f"missing {missing or 'nothing'}; outside {list(STANCE_VALUES)}: {bad or 'nothing'}; "
            f"not an idea point in this room: {unknown or 'nothing'}. A stance record covers every "
            "idea point on both criteria or it is not one: agreement_pct is computed from it"
        )
    return out


def _validate_ranking(ranking: Mapping[str, Any], *, idea_dp_ids: Sequence[str], participant: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for criterion in CRITERIA:
        order = ranking.get(criterion)
        if not isinstance(order, Sequence) or isinstance(order, (str, bytes)):
            raise StanceIncompleteError(
                f"post_final_stance: {participant!r}'s ranking for criterion {criterion!r} is not a list"
            )
        ordered = [str(x) for x in order]
        if sorted(ordered) != sorted(idea_dp_ids):
            raise StanceIncompleteError(
                f"post_final_stance: {participant!r}'s ranking for criterion {criterion!r} is "
                f"{ordered} but this room's idea points are {list(idea_dp_ids)} — rank-all means every "
                "point exactly once, so a partial ranking is a different measurement"
            )
        out[criterion] = ordered
    return out


def render_final_stance(
    *, participant: str, ranking: Mapping[str, Sequence[str]], stances: Mapping[str, Mapping[str, str]], note: str | None
) -> str:
    """The human-readable body a stance record posts as its turn. The
    structured payload is the thing downstream arithmetic reads (it rides on
    the companion event); this text is what a person reading the room doc
    sees, and it is generated from the same values rather than written
    separately, so the two cannot disagree."""
    lines = [f"FINAL STANCE — {participant}", ""]
    for criterion in CRITERIA:
        lines.append(f"Ranking on ({criterion}): " + " > ".join(ranking[criterion]))
    lines.append("")
    for dp_id in sorted(stances):
        row = stances[dp_id]
        lines.append(f"- {dp_id}: (a) {row['a']} · (b) {row['b']}")
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def post_final_stance(
    store: Store,
    *,
    room_id: str,
    launch_id: str,
    participant: str,
    ranking: Mapping[str, Sequence[str]],
    stances: Mapping[str, Mapping[str, str]],
    note: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """File one participant's final stance record on the room's rank-all
    point: a complete ranking of every idea point on each criterion, and a
    discrete stance per point per criterion.

    Everything is validated BEFORE anything is written, which is what keeps
    the two halves honest: the turn (human-readable, in the append-only room
    doc) and the companion ``room_final_stance`` event (the structured
    payload the arithmetic reads). A record that does not cover every idea
    point on both criteria is refused
    (:class:`~trialerror.rooms.errors.StanceIncompleteError`) rather than
    partially stored — a stance set with holes would silently change what
    ``agreement_pct`` is a share OF.

    Re-filing is allowed and the latest record wins
    (:func:`list_final_stances`): the room doc is append-only, so a
    superseded stance stays readable as the position it was."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    rank_all_dp_id = _require_rank_all(config, room_id=room_id, caller="post_final_stance")
    participants = _participants(config)
    if participants and participant not in participants:
        raise ValueError(
            f"post_final_stance: {participant!r} is not a participant of room {room_id!r} "
            f"({participants!r})"
        )
    idea_dp_ids = [dp["dp_id"] for dp in _idea_dps(config)]
    checked_ranking = _validate_ranking(ranking, idea_dp_ids=idea_dp_ids, participant=participant)
    checked_stances = _validate_stances(stances, idea_dp_ids=idea_dp_ids, participant=participant)

    body = render_final_stance(
        participant=participant, ranking=checked_ranking, stances=checked_stances, note=note
    )
    turn = post_message(
        store, room_id=room_id, launch_id=launch_id, dp_id=rank_all_dp_id, body=body, kind="closure", ts=ts
    )
    _emit_room_event(
        store,
        event_type="room_final_stance",
        room_id=room_id,
        launch_id=launch_id,
        ts=turn["ts"],
        payload_extra={
            "dp_id": rank_all_dp_id,
            "seq": turn["seq"],
            "participant": participant,
            "ranking": checked_ranking,
            "stances": checked_stances,
            "note": note,
        },
    )
    return {**turn, "participant": participant, "ranking": checked_ranking, "stances": checked_stances, "note": note}


def list_final_stances(store: Store, *, room_id: str) -> dict[str, dict[str, Any]]:
    """``{participant: {ranking, stances, note, seq, ts}}`` — the LATEST
    record per participant, read off the ``room_final_stance`` event trail
    (module TRIALERROR-DEV-NOTE item 6). Oldest-first iteration with
    overwrite is what makes "latest wins" true without a second query."""
    out: dict[str, dict[str, Any]] = {}
    for payload in _room_events(store, event_type="room_final_stance", room_id=room_id):
        participant = payload.get("participant")
        if not participant:
            continue
        out[str(participant)] = {
            "participant": str(participant),
            "ranking": payload.get("ranking") or {},
            "stances": payload.get("stances") or {},
            "note": payload.get("note"),
            "seq": payload.get("seq"),
            "ts": payload.get("_ts"),
            "launch_id": payload.get("_launch_id"),
        }
    return out


def compute_agreement_pct(
    stances_by_participant: Mapping[str, Mapping[str, Any]], *, dp_id: str
) -> dict[str, Any]:
    """``agreement_pct`` for one discussion point, computed from the filed
    structured stances — design §3.1 Phase 5(f), verbatim: "the share of
    participants whose stance on each criterion matches the modal stance".

    Two decisions, both stated because both are load-bearing:

    - **Per criterion, then the MINIMUM.** A point gets one number against
      one bar, and the charter's rule is both-or-eliminated, so the number a
      point is judged on is its WEAKER criterion. Reporting the mean would
      let unanimity on (a) carry a split on (b) over the bar.
    - **Modal share, ties included.** With two participants disagreeing there
      are two modes; the share is then 50% either way, which is the honest
      reading of "half of them agree with the most common answer".

    Participants who filed no stance on this point are not counted in the
    denominator — :func:`post_final_stance` refuses an incomplete record, so
    the only way to be absent is to have filed nothing at all, and
    :func:`score_dp` refuses to score a point whose participants have not
    all filed."""
    per_criterion: dict[str, Any] = {}
    counted: list[str] = []
    for participant, record in sorted(stances_by_participant.items()):
        row = (record.get("stances") or {}).get(dp_id)
        if isinstance(row, Mapping) and all(row.get(c) in STANCE_VALUES for c in CRITERIA):
            counted.append(participant)
    for criterion in CRITERIA:
        values = [
            str((stances_by_participant[p].get("stances") or {})[dp_id][criterion]) for p in counted
        ]
        if not values:
            per_criterion[criterion] = {"modal": None, "share_pct": None, "counts": {}}
            continue
        counts = Counter(values)
        modal_n = max(counts.values())
        modal = sorted(v for v, n in counts.items() if n == modal_n)
        per_criterion[criterion] = {
            "modal": modal[0] if len(modal) == 1 else modal,
            "share_pct": round(100.0 * modal_n / len(values), 6),
            "counts": dict(sorted(counts.items())),
        }
    shares = [per_criterion[c]["share_pct"] for c in CRITERIA if per_criterion[c]["share_pct"] is not None]
    return {
        "dp_id": dp_id,
        "participants_counted": counted,
        "per_criterion": per_criterion,
        "agreement_pct": min(shares) if len(shares) == len(CRITERIA) else None,
    }


def record_buster_position(
    store: Store, *, room_id: str, participant: str, text: str, by_launch: str, ts: str | None = None
) -> dict[str, Any]:
    """Record the assumption-buster's own position ONCE, verbatim, for
    re-injection into every later envelope for that seat (charter §6(v)).

    ``text`` is the buster's own output as it was written — typically its
    NEGATE-card record. Nothing here edits it, and
    :func:`_buster_position_envelope` hands back exactly these bytes every
    time it is asked, which is the whole mechanism: O7 measures the other
    participants' movement relative to a FIXED position, and a position
    re-derived per spawn is not fixed.

    Refuses a room whose ``buster`` is somebody else (or nobody): a position
    recorded against a seat that is not the buster's would be re-injected to
    nobody, silently."""
    if not text or not text.strip():
        raise ValueError("record_buster_position: text is required (an empty position is not a position)")
    room = _require_room(store, room_id)
    config = _room_config(room)
    declared = config.get("buster")
    if declared != participant:
        raise ValueError(
            f"record_buster_position: room {room_id!r} declares its buster as {declared!r}, not "
            f"{participant!r} — pass create_room(..., buster=...) for the seat that holds the stake"
        )
    _require_launch_exists(store, by_launch, field_name="by_launch")
    event = _emit_room_event(
        store,
        event_type="room_buster_position",
        room_id=room_id,
        launch_id=by_launch,
        ts=ts,
        payload_extra={"participant": participant, "text": text},
    )
    return {"room_id": room_id, "participant": participant, "text": text, "ts": event["ts"]}


def get_buster_position(store: Store, *, room_id: str) -> dict[str, Any] | None:
    """The room's recorded buster position, verbatim, or ``None`` if none was
    recorded. The FIRST recording wins, deliberately: the position is
    supposed to be the one the buster arrived with, and letting a later
    write replace it would reintroduce exactly the re-argued position the
    verbatim rule exists to prevent. A second recording is still in the
    event trail, visible, and reported here as ``superseded_attempts``."""
    records = _room_events(store, event_type="room_buster_position", room_id=room_id)
    if not records:
        return None
    first = records[0]
    return {
        "participant": first.get("participant"),
        "text": first.get("text"),
        "ts": first.get("_ts"),
        "superseded_attempts": len(records) - 1,
    }


# ---------------------------------------------------------------------------
# the neutral extract pass
# ---------------------------------------------------------------------------


def build_extract_envelope(store: Store, *, room_id: str, dp_id: str) -> dict[str, Any]:
    """The envelope for the per-point neutral extract pass (design §3.1
    Phase 5(d)): each turn's prose in, a structured
    ``{claim, anchors, stance on (a), stance on (b), residual disagreement}``
    out, one entry per turn.

    **This is new code and not ``trialerror.summarize``.** That subsystem
    summarizes a document or a collection; its subjects are corpus objects
    and its output is prose. What the moderator needs is neither a summary
    nor prose: it is a fixed-field reduction of each turn that carries no
    authorship and no rhetoric, so that what reaches the scorer cannot
    address the scorer.

    The envelope carries ``seq`` and the body and DELIBERATELY NOT
    ``author_launch``: the extractor has no business knowing who wrote which
    turn either."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="build_extract_envelope")
    _refuse_procedural_point(
        dp, dp_id=dp_id, room_id=room_id, caller="build_extract_envelope", tail=_PROCEDURAL_PROSE_TAIL
    )
    turns = list_room_turns(store, room_id=room_id, dp_id=dp_id)
    kinds = turn_kinds(store, room_id=room_id, dp_id=dp_id)
    return {
        "kind": "room_turn_extract",
        "room_id": room_id,
        "topic": room["topic"],
        "dp_id": dp_id,
        "prompt": dp["prompt"],
        "criteria": list(CRITERIA),
        "stance_values": list(STANCE_VALUES),
        "extract_fields": list(EXTRACT_FIELDS),
        "turns": [
            {"seq": t["seq"], "body": t["body"], "turn_kind": kinds.get(t["seq"], DEFAULT_TURN_KIND)}
            for t in turns
        ],
        "instructions": (
            "For EACH turn above, return one extract: {'seq': <seq>, 'claim': <the single claim the "
            "turn makes, in its own terms>, 'anchors': [<ids or quotes the turn rests on>], "
            f"'stance_a': <one of {list(STANCE_VALUES)}>, 'stance_b': <one of {list(STANCE_VALUES)}>, "
            "'residual_disagreement': <what this turn leaves unresolved, or 'none'>}. Extract, do not "
            "summarize: no judgement of quality, no comparison between turns, nothing about who wrote "
            "what. Every field is required for every turn."
        ),
    }


def record_turn_extracts(
    store: Store,
    *,
    room_id: str,
    dp_id: str,
    extracts: Sequence[Mapping[str, Any]],
    by_launch: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Take the extract pass's structured answers back, validated against
    the turns actually on the point: one extract per turn, every
    :data:`EXTRACT_FIELDS` field present, no extract for a ``seq`` that is
    not a turn on this point, no turn left without one.

    Refuses (:class:`ValueError`) rather than storing a partial pass. The
    moderator envelope withholds raw prose exactly when every turn has an
    extract, so a half-recorded pass would silently put the prose back."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="record_turn_extracts")
    _refuse_procedural_point(
        dp, dp_id=dp_id, room_id=room_id, caller="record_turn_extracts", tail=_PROCEDURAL_PROSE_TAIL
    )
    _require_launch_exists(store, by_launch, field_name="by_launch")
    turn_seqs = [t["seq"] for t in list_room_turns(store, room_id=room_id, dp_id=dp_id)]
    if not turn_seqs:
        raise ValueError(
            f"record_turn_extracts: discussion point {dp_id!r} has no turns to extract"
        )
    by_seq: dict[int, dict[str, Any]] = {}
    problems: list[str] = []
    for entry in extracts:
        seq = entry.get("seq")
        if seq is None or int(seq) not in turn_seqs:
            problems.append(f"seq={seq!r} is not a turn on {dp_id!r}")
            continue
        missing = [f for f in EXTRACT_FIELDS if entry.get(f) in (None, "")]
        if missing:
            problems.append(f"seq={seq}: missing {missing}")
            continue
        by_seq[int(seq)] = {"seq": int(seq), **{f: entry[f] for f in EXTRACT_FIELDS}}
    uncovered = [s for s in turn_seqs if s not in by_seq]
    if uncovered:
        problems.append(f"turns with no extract: {uncovered}")
    if problems:
        raise ValueError(
            f"record_turn_extracts: refused for room {room_id!r} point {dp_id!r} — {problems}. "
            "A partial pass would put raw turn prose back in front of the moderator without saying so"
        )
    ordered = [by_seq[s] for s in turn_seqs]
    event = _emit_room_event(
        store,
        event_type="room_turn_extract",
        room_id=room_id,
        launch_id=by_launch,
        ts=ts,
        payload_extra={"dp_id": dp_id, "extracts": ordered, "n_turns": len(turn_seqs)},
    )
    return {"room_id": room_id, "dp_id": dp_id, "extracts": ordered, "ts": event["ts"]}


def list_turn_extracts(store: Store, *, room_id: str, dp_id: str) -> list[dict[str, Any]]:
    """The LATEST recorded extract pass for one point, in turn order, or
    ``[]`` if none was recorded. A re-run pass supersedes the previous one
    whole — extracts are a reduction of the turns as they stand, and mixing
    two passes would mix two readings of the same room."""
    passes = [p for p in _room_events(store, event_type="room_turn_extract", room_id=room_id) if p.get("dp_id") == dp_id]
    if not passes:
        return []
    return [dict(e) for e in passes[-1].get("extracts") or []]


# ---------------------------------------------------------------------------
# moderator scoring
# ---------------------------------------------------------------------------


def build_moderator_scoring_envelope(
    store: Store, *, room_id: str, dp_id: str, require_extracts: bool = False
) -> dict[str, Any]:
    """A plain-dict judgment-request envelope for the moderator scoring
    ``dp_id`` — topic, the discussion point's own prompt, what was said on
    it, and the fixed convergence bar. Same envelope-building role as
    :func:`~trialerror.verify.hypothesis.build_hypothesis_judgment_envelope`
    (module docstring's LLM-judgment-boundary note): this function never
    calls a judge itself — see :func:`score_dp`, which does, via an
    injected callable.

    **``author_launch`` is gone, always** (design §5.2 item 5: "``author_launch``
    is dropped from moderator envelopes"). It was there; it is a field whose
    only possible use at scoring time is to treat two identical arguments
    differently, so it is not passed.

    **What the moderator reads depends on whether the extract pass ran.**
    Once every turn on the point has a neutral extract
    (:func:`record_turn_extracts`), the envelope carries the EXTRACTS and
    withholds the raw prose — the design's barrier table says the moderator
    never sees raw turn prose at scoring, and the extract is what replaces
    it. With no extracts recorded, the prose is carried (that is every
    pre-framework room, unchanged) and the envelope says so in
    ``extracts_recorded: false`` rather than looking like a room nobody spoke
    in. ``require_extracts=True`` turns that gap into a refusal, which is
    what a framework round passes.

    The ASK has changed too: the moderator returns the discrete ``label``
    first and does not return ``agreement_pct`` at all where structured
    stances exist — see :func:`score_dp`, which computes it."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="build_moderator_scoring_envelope")
    _refuse_procedural_point(
        dp, dp_id=dp_id, room_id=room_id, caller="build_moderator_scoring_envelope",
        tail=_PROCEDURAL_PROSE_TAIL,
    )
    turns = list_room_turns(store, room_id=room_id, dp_id=dp_id)
    kinds = turn_kinds(store, room_id=room_id, dp_id=dp_id)
    extracts = list_turn_extracts(store, room_id=room_id, dp_id=dp_id)
    covered = {e["seq"] for e in extracts}
    fully_extracted = bool(turns) and all(t["seq"] in covered for t in turns)
    if require_extracts and not fully_extracted:
        raise ValueError(
            f"build_moderator_scoring_envelope: room {room_id!r} point {dp_id!r} has no complete "
            "neutral-extract pass, and require_extracts was asked for — run build_extract_envelope + "
            "record_turn_extracts first; scoring raw prose is what the extract pass exists to prevent"
        )
    stances = list_final_stances(store, room_id=room_id)
    computed = compute_agreement_pct(stances, dp_id=dp_id) if stances else None
    envelope: dict[str, Any] = {
        "kind": "room_moderator_score",
        "room_id": room_id,
        "topic": room["topic"],
        "dp_id": dp_id,
        "prompt": dp["prompt"],
        "criteria": list(CRITERIA),
        "labels": list(DP_LABELS),
        "extracts_recorded": fully_extracted,
        "extracts": extracts,
        "convergence_bar_pct": CONVERGENCE_BAR_PCT,
    }
    if not fully_extracted:
        envelope["turns"] = [
            {"seq": t["seq"], "body": t["body"], "turn_kind": kinds.get(t["seq"], DEFAULT_TURN_KIND)}
            for t in turns
        ]
    if computed is not None and computed["agreement_pct"] is not None:
        envelope["agreement_pct_computed"] = computed["agreement_pct"]
        envelope["instructions"] = (
            f"Judge this discussion point against criteria {list(CRITERIA)} and return the LABEL first: "
            f"{{'label': <one of {list(DP_LABELS)}>, 'note'?: <str>}}. Do NOT return agreement_pct — it "
            "is computed from the participants' structured final stances, not read out of what was said, "
            f"and it is already {computed['agreement_pct']}% for this point against the "
            f"{CONVERGENCE_BAR_PCT}% bar. Include a short note naming the specific residual disagreement."
        )
    else:
        envelope["instructions"] = (
            f"Judge this discussion point against criteria {list(CRITERIA)} and return the LABEL first: "
            f"{{'label': <one of {list(DP_LABELS)}>, 'agreement_pct': <0-100>, 'note'?: <str>}} — the "
            f"room converges on this point at agreement_pct >= {CONVERGENCE_BAR_PCT}; include a short "
            "note naming the specific disagreement when the score is below bar. No participant has filed "
            "a structured final stance on this point, so the share is yours to report rather than "
            "arithmetic over stances; a framework round files the stances first."
        )
    return envelope


def score_dp(
    store: Store,
    *,
    room_id: str,
    dp_id: str,
    judge: Callable[[Mapping[str, Any]], Any],
    by_launch: str,
    require_extracts: bool = False,
    ts: str | None = None,
) -> dict[str, Any]:
    """Build the moderator's judgment envelope, call ``judge(envelope)``
    (never an LLM call made by this module itself — see module docstring),
    and upsert the resulting score into ``room_score`` (keyed by the
    namespaced :func:`_dp_ref`, module TRIALERROR-DEV-NOTE item 3).

    **Two paths, decided by the data and not by a flag.**

    *With structured final stances on record for this point*, this is the
    framework path: ``agreement_pct`` is COMPUTED here
    (:func:`compute_agreement_pct`) and the judge's job is the discrete
    ``label`` (:data:`DP_LABELS`), which is required and is stated first in
    both the result and the event. A judge that also returns an
    ``agreement_pct`` is REFUSED — the number is the selection variable the
    whole framework is evaluated on (design §5.2 item 2), and a selector the
    judged content can address is a selector that will be addressed. Every
    participant must have filed, or the point is not scored
    (:class:`~trialerror.rooms.errors.StanceIncompleteError`): a share over
    whoever happened to answer is a different number with the same name.

    *With no stances on record*, the pre-framework path is unchanged: the
    judge returns ``{"agreement_pct": <0-100>, "note"?: <str>}`` or a bare
    number, and a ``label`` is carried if one is offered.

    **The room must be ``open``**, the way :func:`post_message` requires it.
    A converged or frozen room's label and share are the record of what it
    decided; re-scoring would rewrite that in place while ``room.state``
    stayed ``converged``, so the document would assert a decision nobody
    could see had changed.

    **NEITHER ownership is re-checked here** (charter §6(vi): "at room
    creation and again at scoring"), by lens name: the owning lens must not
    be a participant and must not have posted a turn on this point. Creation
    cannot see a turn posted later by a launch nobody declared at the time.

    ``by_launch`` (the moderator's own launch) is validated to exist BEFORE
    any write (module TRIALERROR-DEV-NOTE item 2's half-write concern: a bad
    launch id must never leave a scored ``room_score`` row with a
    silently-failed audit event).

    Returns the written ``room_score`` row plus ``label``/``dp_id``/``note``/
    ``converged`` (``agreement_pct >= CONVERGENCE_BAR_PCT``) and, on the
    framework path, the per-criterion ``agreement`` breakdown the number came
    from."""
    _require_launch_exists(store, by_launch, field_name="by_launch")
    room = _require_room(store, room_id)
    if room["state"] != "open":
        raise ValueError(
            f"score_dp: room {room_id!r} is not open (state={room['state']!r}); no further scores accepted. "
            "A converged or frozen room's label and share ARE the record of what it decided, and the room "
            "state would not move to say they had been rewritten -- re-open nothing, open a new room"
        )
    config = _room_config(room)
    dp = _find_dp(config, dp_id, room_id=room_id, caller="score_dp")
    _refuse_procedural_point(
        dp, dp_id=dp_id, room_id=room_id, caller="score_dp",
        tail="is never scored against the convergence bar — score the idea points instead",
    )
    _check_scoring_ownership(store, dp=dp, config=config, room_id=room_id)

    stances = list_final_stances(store, room_id=room_id)
    computed = compute_agreement_pct(stances, dp_id=dp_id) if stances else None
    from_stances = computed is not None and computed["agreement_pct"] is not None

    envelope = build_moderator_scoring_envelope(
        store, room_id=room_id, dp_id=dp_id, require_extracts=require_extracts
    )
    result = judge(envelope)
    label: str | None = None
    if isinstance(result, Mapping):
        supplied_pct = result.get("agreement_pct")
        note = result.get("note")
        label = result.get("label")
    else:
        supplied_pct = result
        note = None

    if from_stances:
        missing = sorted(set(_participants(config)) - set(computed["participants_counted"]))
        if missing:
            raise StanceIncompleteError(
                f"score_dp: participants {missing} have filed no final stance on point {dp_id!r} of room "
                f"{room_id!r} — agreement_pct is the share of the room's participants who agree, so it is "
                "computed once every seat has stated a stance, not over whoever answered"
            )
        if supplied_pct is not None:
            raise ValueError(
                f"score_dp: the judge returned agreement_pct={supplied_pct!r}, but point {dp_id!r} has "
                "structured final stances, so the number is computed from them here and is not the "
                "judge's to supply (design §5.2 item 2: the selector is computed, the label is judged). "
                f"Return {{'label': <one of {list(DP_LABELS)}>}} instead"
            )
        if label is None:
            raise ValueError(
                f"score_dp: the judge returned no label for point {dp_id!r}; on the stance path the label "
                f"is the judgment and is required (one of {list(DP_LABELS)})"
            )
        agreement_pct = float(computed["agreement_pct"])
    else:
        if supplied_pct is None and label is not None:
            # A label alone cannot produce the number on this path, and the
            # reason is worth saying: this point has no structured stances to
            # compute one from, so either the stances come first or the judge
            # reports the share.
            raise StanceIncompleteError(
                f"score_dp: the judge returned only a label for point {dp_id!r}, but no participant has filed "
                "a structured final stance on it, so there is nothing to compute agreement_pct from. File the "
                "rank-all stances first (post_final_stance), or have the judge report the share as well"
            )
        try:
            agreement_pct = float(supplied_pct)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"score_dp: judge returned a non-numeric agreement_pct: {supplied_pct!r}")
    if label is not None and str(label) not in DP_LABELS:
        raise ValueError(f"score_dp: label must be one of {list(DP_LABELS)}, got {label!r}")
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
    # Label FIRST, in the event payload and in the returned dict alike
    # (design §6 rooms row, verbatim). The order is not decoration: the
    # discrete judgment is the thing a judge produced, and the number beside
    # it is arithmetic over stances on the framework path.
    _emit_room_event(
        store,
        event_type="room_dp_scored",
        room_id=room_id,
        launch_id=by_launch,
        ts=ts,
        payload_extra={
            "label": str(label) if label is not None else None,
            "dp_id": dp_id,
            "dp_ref": dp_ref,
            "agreement_pct": agreement_pct,
            "agreement_from": "structured_stances" if from_stances else "judge",
            "note": note,
            "converged": converged,
        },
    )
    row = _get_room_score_row(store, room_id=room_id, dp_id=dp_id)
    assert row is not None
    out: dict[str, Any] = {
        "label": str(label) if label is not None else None,
        **row,
        "dp_id": dp_id,
        "note": note,
        "converged": converged,
        "agreement_from": "structured_stances" if from_stances else "judge",
    }
    if from_stances:
        out["agreement"] = computed
    return out


def _check_scoring_ownership(
    store: Store, *, dp: Mapping[str, Any], config: Mapping[str, Any], room_id: str
) -> None:
    """NEITHER ownership, re-read at scoring time by lens name (charter
    §6(vi)). Creation checks the roster it was handed; scoring checks what
    actually happened — a lens seated legitimately can still have posted a
    turn on a point whose idea it owns, if the booking that posted it
    declared a lens name nobody listed as a participant."""
    owner = _idea_owner(store, dp.get("idea_id"))
    if owner["lens_name"] is None:
        return
    if owner["lens_name"] in _participants(config):
        raise OwnershipConflictError(
            f"score_dp: room {room_id!r} seats {owner['lens_name']!r}, the lens that authored idea "
            f"{owner['idea_id']!r} which point {dp['dp_id']!r} vets — NEITHER ownership is checked again "
            "at scoring (charter §6(vi)); this point's score would not mean what it says"
        )
    for author in _turns_by_author(store, room_id=room_id, dp_id=dp["dp_id"]):
        if author == owner["lens_name"]:
            raise OwnershipConflictError(
                f"score_dp: lens {owner['lens_name']!r} authored idea {owner['idea_id']!r} and has posted "
                f"a turn on point {dp['dp_id']!r} of room {room_id!r} — the point cannot be scored on a "
                "record that includes its owner's own vetting turn"
            )


def get_dp_label(store: Store, *, room_id: str, dp_id: str) -> str | None:
    """The discrete ``label`` recorded by the point's most recent
    ``room_dp_scored`` event, or ``None`` if it was never scored (or was
    scored on the pre-framework path, where the judge returned a number and
    no label). ``room_score`` has no ``label`` column and the design asks
    for the label on the result and the event rather than in one — module
    TRIALERROR-DEV-NOTE item 6; same read-it-back-from-events shape
    :func:`get_freeze_reason` uses."""
    scored = [p for p in _room_events(store, event_type="room_dp_scored", room_id=room_id) if p.get("dp_id") == dp_id]
    if not scored:
        return None
    label = scored[-1].get("label")
    return str(label) if label else None


# ---------------------------------------------------------------------------
# convergence / freeze
# ---------------------------------------------------------------------------


def check_room_converged(store: Store, room_id: str) -> dict[str, Any]:
    """Read-only: for every IDEA discussion point in the room, its current
    score (``None`` if never scored) and whether it individually meets
    :data:`CONVERGENCE_BAR_PCT`. ``all_converged`` is ``True`` only when
    EVERY idea point is both scored and at/above bar — never a silent pass
    on an unscored DP.

    The procedural rank-all point is not one of them: it is where stances are
    filed, not a thing judged against the bar, so it is excluded from the
    accounting and reported separately as ``rank_all``. ``stances_filed`` is
    what charter §6(iv)'s "RANK-ALL before any verdict" turns into here — a
    precondition :func:`converge_room` enforces when the room has a rank-all
    point at all."""
    room = _require_room(store, room_id)
    config = _room_config(room)
    per_dp: list[dict[str, Any]] = []
    all_scored = True
    all_at_bar = True
    for dp in _idea_dps(config):
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
    out: dict[str, Any] = {
        "room_id": room_id,
        "convergence_bar_pct": CONVERGENCE_BAR_PCT,
        "all_scored": all_scored,
        "all_converged": all_scored and all_at_bar,
        "per_dp": per_dp,
    }
    rank_all_dp_id = config.get("rank_all_dp_id")
    if rank_all_dp_id:
        filed = sorted(list_final_stances(store, room_id=room_id))
        participants = _participants(config)
        out["rank_all"] = {
            "dp_id": rank_all_dp_id,
            "participants": participants,
            "stances_filed": filed,
            "all_filed": bool(participants) and set(participants) <= set(filed),
            "missing": sorted(set(participants) - set(filed)),
        }
    return out


def _transition_room_cas(
    store: Store,
    *,
    room_id: str,
    expected_state: str,
    to_state: str,
    by_launch: str,
    ts: str,
    event_type: str,
    payload_extra: Mapping[str, Any],
) -> None:
    """The ONE place ``room.state`` moves to a terminal state, and the
    close of WA-1's lost-write race for both :func:`converge_room` and
    :func:`freeze_room`.

    Before this, both verbs did a plain check-then-write: read the room,
    ``assert_legal_transition``, then a bare ``UPDATE room SET state=?``
    with no WHERE on the old state. Six concurrent freezes on one open
    room therefore ALL passed the check (they all read ``open``) and all
    six "succeeded", each emitting its own ``room_frozen`` event with its
    own reason -- so the room's audit trail claimed six escalations and
    the last writer's reason silently won.

    Now: one ``BEGIN IMMEDIATE`` write lock (the shape
    :func:`post_message` already uses), re-read the room UNDER that lock,
    re-check the edge against the state actually found, then a
    compare-and-swap ``UPDATE ... WHERE room_id = ? AND state = ?``. A
    ``rowcount`` of 0 means the row moved between the read and the write
    and is reported as :class:`~trialerror.rooms.errors.
    IllegalRoomTransitionError` naming the state actually found, never a
    silent no-op. The companion event is written inside the SAME
    transaction (:func:`trialerror.events.api.append_event_in_txn`), so a
    transition and its audit row land together or not at all -- the one
    behavioural change from the pre-fix code, which emitted the event
    after its own auto-committed UPDATE.

    ``by_launch`` is validated by the public caller BEFORE the transaction
    opens (``event.launch_id`` is a registered XID and the raw insert path
    does not re-check it) -- see ``trialerror.artifacts._txn``'s module
    docstring for the same contract."""
    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = conn.execute("SELECT state FROM room WHERE room_id = ?", (room_id,)).fetchone()
        if fresh is None:
            raise ValueError(f"no such room: {room_id!r}")
        assert_legal_transition(fresh["state"], to_state)
        cur = conn.execute(
            "UPDATE room SET state = ? WHERE room_id = ? AND state = ?",
            (to_state, room_id, expected_state),
        )
        if cur.rowcount == 0:
            found = conn.execute("SELECT state FROM room WHERE room_id = ?", (room_id,)).fetchone()
            found_state = found["state"] if found is not None else "<row disappeared>"
            raise IllegalRoomTransitionError(
                f"room {room_id!r}: cannot move to {to_state!r} — expected state "
                f"{expected_state!r} but found {found_state!r} (a concurrent writer "
                "moved this room first; nothing was written)"
            )
        append_event_in_txn(
            conn,
            event_type=event_type,
            payload={"room_id": room_id, **dict(payload_extra)},
            launch_id=by_launch,
            ts=ts,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def converge_room(store: Store, *, room_id: str, by_launch: str, ts: str | None = None) -> dict[str, Any]:
    """``open -> converged`` — refuses (:class:`~trialerror.rooms.errors.
    ConvergenceBarNotMetError`) unless :func:`check_room_converged` reports
    ``all_converged``; refuses (:class:`~trialerror.rooms.errors.
    IllegalRoomTransitionError`) if the room is already ``converged``/
    ``frozen`` (the state graph itself — ``trialerror.rooms.state_machine`` —
    has no outgoing edge from either terminal state, so this naturally
    also refuses converging an already-frozen room), INCLUDING when a
    concurrent caller wins the race between this call's own check and its
    write (:func:`_transition_room_cas`).

    A room with a rank-all point additionally refuses
    (:class:`~trialerror.rooms.errors.StanceIncompleteError`) until every
    participant has filed its final stance — charter §6(iv)'s "RANK-ALL
    before any verdict", enforced at the one place a verdict becomes final
    rather than trusted to ordering."""
    room = _require_room(store, room_id)
    _require_launch_exists(store, by_launch, field_name="by_launch")
    status = check_room_converged(store, room_id)
    rank_all = status.get("rank_all")
    if rank_all is not None and not rank_all["all_filed"]:
        raise StanceIncompleteError(
            f"converge_room: room {room_id!r} cannot converge before its rank-all point is complete — "
            f"participants {rank_all['missing']} have filed no final stance (charter §6(iv): rank-all "
            "before any verdict)"
        )
    if not status["all_converged"]:
        unmet = [d["dp_id"] for d in status["per_dp"] if not d["converged"]]
        raise ConvergenceBarNotMetError(
            f"converge_room: room {room_id!r} has discussion point(s) below the "
            f"{CONVERGENCE_BAR_PCT}% bar or not yet scored: {unmet}"
        )
    assert_legal_transition(room["state"], "converged")
    ts = ts or now()
    _transition_room_cas(
        store, room_id=room_id, expected_state=room["state"], to_state="converged",
        by_launch=by_launch, ts=ts, event_type="room_converged",
        payload_extra={"per_dp": status["per_dp"]},
    )
    return _require_room(store, room_id)


def freeze_room(store: Store, *, room_id: str, by_launch: str, reason: str, ts: str | None = None) -> dict[str, Any]:
    """``open -> frozen`` — origin-project's "freeze-and-escalate path". ``reason`` is
    required (a freeze with no stated reason defeats the point of
    escalating to a human) and is recorded on the companion ``room_frozen``
    event (module TRIALERROR-DEV-NOTE item 2: ``room_score`` has no per-room
    "reason" column, and ``room_turn`` is for discussion-point turns, not a
    room-level moderator act — the event trail is the faithful home for
    this). Refuses (:class:`~trialerror.rooms.errors.
    IllegalRoomTransitionError`) if the room is not currently ``open`` —
    including when the room stopped being ``open`` between this call's own
    check and its write (:func:`_transition_room_cas`): exactly one of N
    concurrent freezes lands, and exactly one ``room_frozen`` event is
    written."""
    if not reason:
        raise ValueError("freeze_room: reason is required (freeze-and-escalate needs something to escalate)")
    room = _require_room(store, room_id)
    _require_launch_exists(store, by_launch, field_name="by_launch")
    assert_legal_transition(room["state"], "frozen")
    ts = ts or now()
    _transition_room_cas(
        store, room_id=room_id, expected_state=room["state"], to_state="frozen",
        by_launch=by_launch, ts=ts, event_type="room_frozen", payload_extra={"reason": reason},
    )
    return _require_room(store, room_id)


def get_freeze_reason(store: Store, room_id: str) -> str | None:
    """The ``reason`` recorded by the room's most recent ``room_frozen``
    event, or ``None`` if the room was never frozen (or is not itself
    ``frozen`` right now, though this reads history regardless of current
    state).

    M-WA-7: the predicate is pushed into SQL (``json_extract`` on the
    payload's ``room_id``, which :func:`_emit_room_event` guarantees every
    room event carries) with a ``LIMIT 1`` — the previous version pulled
    EVERY ``room_frozen`` row in the program back into Python and decoded
    them one at a time until it found a match, which is O(all freezes ever)
    per dashboard panel build."""
    row = store.ops.execute(
        "SELECT payload FROM event WHERE type = 'room_frozen' "
        "AND json_extract(payload, '$.room_id') = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
        (room_id,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"]).get("reason")


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
    kinds = turn_kinds(store, room_id=room_id)
    turns_by_dp_ref: dict[str, list[dict[str, Any]]] = {}
    for t in turns:
        turns_by_dp_ref.setdefault(t["dp_ref"], []).append(t)

    lines: list[str] = [f"# {room['topic']}", "", f"room_id: `{room_id}`  |  state: **{room['state']}**", ""]
    for dp in config["discussion_points"]:
        dp_ref = _dp_ref(room_id, dp["dp_id"])
        lines.append(f"## {dp['dp_id']}: {dp['prompt']}")
        lines.append("")
        if dp.get("procedural"):
            # The rank-all point is never scored against the bar, so printing
            # "not yet scored" beside it would read as a missing score rather
            # than a point that does not take one.
            lines.append("_procedural point (rank-all + final stances); not scored against the bar_")
        else:
            score_row = _get_room_score_row(store, room_id=room_id, dp_id=dp["dp_id"])
            score_str = f"{score_row['agreement_pct']:.1f}%" if score_row is not None else "not yet scored"
            label = get_dp_label(store, room_id=room_id, dp_id=dp["dp_id"])
            label_str = f"**{label}** · " if label else ""
            lines.append(f"_{label_str}agreement: {score_str} (bar: {CONVERGENCE_BAR_PCT}%)_")
        lines.append("")
        dp_turns = turns_by_dp_ref.get(dp_ref, [])
        if not dp_turns:
            lines.append("_(no turns yet)_")
            lines.append("")
        for t in dp_turns:
            kind = kinds.get(t["seq"], DEFAULT_TURN_KIND)
            lines.append(f"**turn {t['seq']}** ({kind}) — `{t['author_launch']}`")
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


# ---------------------------------------------------------------------------
# admission order — which ideas are roomed, in which order, in which batch
# ---------------------------------------------------------------------------


def admission_order_hash(idea_ids: Sequence[str]) -> str:
    """The digest of one admission ORDER: ``sha256`` over the ids, newline-
    joined, in order. This is the value a round escrows at pre-registration
    and the ``aiif_round`` gate suite compares the order it actually ran
    against (``admission_order_hash_matches``).

    Over the order and nothing else — not the batching, not the seed, not the
    cell counts — because the order is the thing the anti-gaming rule is
    about: "admit the ideas the screen liked" is an ORDER, and a hash that
    also covered the room size would change when a budget did."""
    return hashlib.sha256("\n".join(str(i) for i in idea_ids).encode("utf-8")).hexdigest()


def _admission_cell(idea: Mapping[str, Any]) -> tuple[str, str]:
    """The (arm, card) cell one idea belongs to. A record with no arm or no
    card is its own cell, labelled as unknown rather than dropped or folded
    into a real one — every consolidated idea is roomed (design §5.2 item 8),
    including the ones whose round predates arms or cards."""
    arm = str(idea.get("arm") or idea.get("tier") or "unknown")
    card = str(idea.get("recipe_card") or idea.get("card") or "none")
    return (arm, card)


def _cell_sort_key(cell: tuple[str, str]) -> tuple[int, str, str]:
    arm, card = cell
    arm_index = ARMS.index(arm) if arm in ARMS else len(ARMS)
    return (arm_index, arm, card)


def build_admission_order(
    ideas: Sequence[Mapping[str, Any]],
    *,
    seed: str,
    ideas_per_room: int = DEFAULT_IDEAS_PER_ROOM,
    rooms_per_batch: int = DEFAULT_ROOMS_PER_BATCH,
    enforce_batch_band: bool = True,
) -> dict[str, Any]:
    """The room admission order (charter §6(i); design §3.1 Phase 5): EVERY
    consolidated idea is roomed, ordered by a seeded draw stratified on
    (arm, card), packed into rooms of ``ideas_per_room`` and rooms into
    batches of ``rooms_per_batch``, with the remainder carried in that same
    order.

    **Stratified means interleaved, not grouped.** The order is built by
    round-robin over the (arm, card) cells — cell order re-shuffled from the
    round's own seed on each pass, members shuffled once per cell from the
    same seed — so any prefix of the order, and therefore any batch, carries
    roughly the cell mix of the whole pool. Sorting by cell instead would
    make the first batch one arm's ideas, and then a batch would be a
    measurement of that arm rather than of the round.

    **Nothing from a dossier reaches this function.** Not a label, not a
    distance, not the collapse flag: the parameters are the idea's id, its
    arm and its card, and the seed. That is the design's own pre-mortem row
    on room admission ("dossier fields never gate or order admission") made
    structural — there is no argument through which a screen result could
    steer the order.

    The **remainder** is the trailing partial room, and it is carried rather
    than seated: an under-sized room is a different instrument (fewer ideas
    competing for the same attention), so the tail waits for the next sitting
    in exactly this order. Every idea appears exactly once across
    ``rooms`` and ``remainder``, which is the property a caller should assert.
    A pool SMALLER than one room therefore seats nobody, and the result says
    so in a ``note`` that names ``ideas_per_room`` — silence there reads as a
    failed draw, and a round-0 dry run of two ideas is not a failed draw.

    **The draw is a function of the SET, not of the caller's ordering.** Each
    cell's members are sorted before the seeded shuffle, so the same pool read
    back in a different order produces the same order and the same hash — and
    the hash is the thing the round escrows.

    ``rooms_per_batch`` is soft-enforced to :data:`ADMISSION_BATCH_ROOM_RANGE`
    — charter §6's own band — the same way :func:`create_room` soft-enforces
    the participant range; ``enforce_batch_band=False`` overrides it with a
    stated reason (a fixture round, a final short sitting)."""
    if ideas_per_room < 1:
        raise AdmissionOrderError(f"build_admission_order: ideas_per_room must be >= 1, got {ideas_per_room}")
    if rooms_per_batch < 1:
        raise AdmissionOrderError(f"build_admission_order: rooms_per_batch must be >= 1, got {rooms_per_batch}")
    low, high = ADMISSION_BATCH_ROOM_RANGE
    if enforce_batch_band and not (low <= rooms_per_batch <= high):
        raise AdmissionOrderError(
            f"build_admission_order: rooms_per_batch must be {low}-{high} (charter §6 batch band), got "
            f"{rooms_per_batch} — pass enforce_batch_band=False to override with a stated reason"
        )
    if not seed:
        raise AdmissionOrderError(
            "build_admission_order: seed is required — an unseeded order is an order nobody can reproduce, "
            "and the order is pre-registered"
        )

    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    seen: set[str] = set()
    for idea in ideas:
        idea_id = idea.get("idea_id") or idea.get("id")
        if not idea_id:
            raise AdmissionOrderError(
                f"build_admission_order: an idea carries no idea_id: {dict(idea)!r}"
            )
        idea_id = str(idea_id)
        if idea_id in seen:
            raise AdmissionOrderError(
                f"build_admission_order: idea {idea_id!r} appears twice in the pool — every consolidated "
                "idea is roomed exactly once"
            )
        seen.add(idea_id)
        cells[_admission_cell(idea)].append(idea_id)

    # Members shuffled per cell on the cell's own stream, so adding an idea to
    # one cell does not re-order another cell's draw -- and SORTED first, so
    # the draw is a function of the SET and the seed rather than of the order
    # the caller happened to hand the pool in. Without the sort, the same
    # round's own pool read back in a different order hashes differently, and
    # the escrowed hash is the thing the gate suite compares.
    pools: dict[tuple[str, str], list[str]] = {}
    for cell in sorted(cells, key=_cell_sort_key):
        members = sorted(cells[cell])
        derive_rng(seed, salt=f"room-admission::{cell[0]}::{cell[1]}").shuffle(members)
        pools[cell] = members

    pass_rng = derive_rng(seed, salt="room-admission::cells")
    order: list[str] = []
    while any(pools.values()):
        live = [cell for cell in sorted(pools, key=_cell_sort_key) if pools[cell]]
        pass_rng.shuffle(live)
        for cell in live:
            order.append(pools[cell].pop(0))

    rooms = [order[i : i + ideas_per_room] for i in range(0, len(order), ideas_per_room)]
    remainder: list[str] = []
    if rooms and len(rooms[-1]) < ideas_per_room:
        remainder = rooms.pop()
    batches = [rooms[i : i + rooms_per_batch] for i in range(0, len(rooms), rooms_per_batch)]
    result: dict[str, Any] = {
        "seed": seed,
        "order": order,
        "hash": admission_order_hash(order),
        "cells": {f"{arm}/{card}": len(members) for (arm, card), members in sorted(cells.items(), key=lambda kv: _cell_sort_key(kv[0]))},
        "rooms": rooms,
        "batches": batches,
        "remainder": remainder,
        "ideas_per_room": ideas_per_room,
        "rooms_per_batch": rooms_per_batch,
        "n_ideas": len(order),
    }
    if order and not rooms:
        # A pool smaller than one room seats nobody and used to say nothing:
        # every idea lands in the remainder and the caller reads an empty
        # `rooms` as "the draw failed". It did not — the sitting is smaller
        # than a room, which is what `ideas_per_room` is for.
        result["note"] = (
            f"all {len(order)} idea(s) are carried in the remainder: the pool is smaller than one room of "
            f"{ideas_per_room}, so no full room can be seated. Either wait for the pool to fill or run with "
            f"--ideas-per-room {len(order)} (a small dry-run sitting is a declared room size, not a short room)"
        )
    return result


def consolidated_ideas_for_admission(store: Store, *, round_id: str) -> list[dict[str, Any]]:
    """The pool :func:`build_admission_order` orders: every ``consolidated``
    idea of one round, with the two fields the stratification reads — the
    lens's ``arm`` (``idea.tier``, which IS the lens's arm under the
    roster-level assignment, design §3.3) and its ``recipe_card``.

    Only ``consolidated``: ``raw`` has not been through the screen, and
    ``merged``/``eliminated``/``promoted`` have already been dispositioned.
    Ordered by ``created_ts`` then id so the POOL is stable before the seeded
    draw touches it — a draw over an unstable pool is not reproducible no
    matter how good the seed is."""
    rows = store.knowledge.execute(
        "SELECT idea_id, tier, recipe_card, created_ts FROM idea "
        "WHERE round_id = ? AND status = 'consolidated' ORDER BY created_ts ASC, idea_id ASC",
        (round_id,),
    ).fetchall()
    return [
        {"idea_id": r["idea_id"], "arm": r["tier"], "recipe_card": r["recipe_card"], "created_ts": r["created_ts"]}
        for r in rows
    ]
