"""Stage 3 of the dashboard V2 build -- OPERATOR WRITE ACTIONS. The write
half of ``trialerror.dashboard.data``'s read-only panel layer: one dispatch
table, ``WRITABLE_ACTIONS``, naming every write action the dashboard UI is
allowed to perform, each a thin wrapper calling straight through to the SAME
module-level business-logic function the equivalent ``trialerror <group>`` CLI
verb already calls (``trialerror.artifacts.gates``, ``trialerror.ingest.extract``,
``trialerror.ingest.requests``, ``trialerror.rooms.api``, ``trialerror.events.api``) --
never raw SQL, never a second implementation of a rule those modules already
enforce (design constraint #1 of this build's brief).

One later action, ``feed-translate`` (lane-b-translator), wraps
``trialerror.jobs.ledger.enqueue`` instead of a subsystem write function, with
the exact payload ``trialerror.cli.feed.run_translate`` builds -- because
translation is a JOB, never an inline call, so the "same function the CLI
verb calls" for that action IS the enqueue. Every rule about what gets
translated and how it is gated still lives in one place, the handler
(``trialerror.feed_translate.handlers``), never here.

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
own fallback).

**Opening a NEW thread is now offered too** (``thread-create``, lane C step
C7). It was not, and the reason stood in this docstring: ``create_thread``
required a real ``launch_id`` because ``thread.created_by_launch`` was NOT
NULL, which an orchestrator identity has none of -- a schema constraint the
lane that wrote that line had no licence to relax. **ops v8**
(``ops_v8_thread_created_by_nullable_and_author``) relaxes it and gives
``thread`` the same derived ``created_by`` a post carries; ruling L-C1 is the
licence. Authorship is still server-derived and never caller-settable, so the
guarantee that used to be enforced by "you cannot do this at all" is now
enforced the same way ``feed-post``'s always was.

**Four lexicon decisions join the table too** (``term-sense-accept``,
``term-sense-reject``, ``term-relation-decide``, ``term-mark-reviewed`` --
lane e step E4, design ``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` section 5).
Each wraps a ``trialerror.lexicon.api`` function exactly like every action
above wraps its own subsystem's callable, and each requires a real
``by_launch`` per ruling L-E4 -- identical to ``gate-send-back``'s L-C2
posture: an id with no ``platform.launch`` row refuses with
``XidTargetMissingError``, never a fallback. **Every one of the four does its
``import trialerror.lexicon`` LAZILY, inside its own handler function, never
at this module's top level** -- ``trialerror.lexicon``'s package
``__init__.py`` asserts a minimum SQLite version for the ``term_fts`` trigram
tokenizer and raises (an :class:`ImportError` subclass) on a build too old to
host it (module docstring, ``trialerror/lexicon/__init__.py``). A top-level
import here would mean every OTHER write action in this file -- a room turn,
a gate verify, nothing to do with the lexicon -- stops working the moment
THIS FILE is imported, on a machine that will never call a ``term-*`` action
at all. :func:`_is_lexicon_refusal` mirrors the same lazy-import discipline
for the catch side (see its own docstring), so :data:`_EXPECTED_ERRORS` --
built once at THIS module's import time -- never has to name
``trialerror.lexicon.errors.LexiconError`` directly.

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
from typing import Any, Callable, Mapping

from trialerror.artifacts import gates as gates_api
from trialerror.artifacts.errors import ArtifactsError
from trialerror.events import api as events_api
from trialerror.ingest import extract as extract_api
from trialerror.ingest import requests as ingest_requests
from trialerror.ingest.errors import IngestError
from trialerror.jobs import ledger as jobs_ledger
from trialerror.offload import protocol as offload_protocol
from trialerror.offload.protocol import OffloadError
from trialerror.rooms import api as rooms_api
from trialerror.rooms.errors import RoomsError
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.verify.errors import VerifyError

__all__ = ["WRITABLE_ACTIONS", "REQUIRED_FIELDS", "dispatch"]

#: Every exception a business-logic call below can legitimately raise as a
#: clean refusal (module docstring: "the refusing module's own str(exc)").
#: ``ExtractError``/every ``trialerror.ingest`` subclass is already covered by
#: ``IngestError``; ``ValidationError``/``XidTargetMissingError`` are already
#: covered by ``StoreError`` (see ``trialerror/stores/errors.py``).
#: ``VerifyError`` covers ``trialerror.verify``'s own refusal family
#: (``PreregNotFoundError``/``PreregVoidedError``/``PreregTamperedError``,
#: ...) -- listed here ahead of the actions that raise it (spec §4's
#: ``prereg-reveal``, C7) so a verify refusal can never reach the HTTP
#: layer as a 500: a tampered escrow is a finding, not a server fault.
#: ``OffloadError`` covers ``trialerror.offload.control.ControlError`` -- the
#: three C-0097 refusals (unknown worker, a request already pending, no launch)
#: are findings about the queue, not server faults, so a ``worker-control``
#: refusal must reach the operator as a 200 ``{"ok": false}`` like every other
#: refusal here. Named at module level rather than lazily (unlike
#: ``LexiconError``, see :func:`_is_lexicon_refusal`) because
#: ``trialerror.offload.protocol`` asserts nothing at import time.
_EXPECTED_ERRORS: tuple[type[Exception], ...] = (
    ArtifactsError, IngestError, OffloadError, RoomsError, StoreError, VerifyError, ValueError,
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


# ---------------------------------------------------------------------------
# Lane C (C7): the four actions the Determinations queue drew disabled.
# Spec section 4; rulings L-C2 (identity), L-C3 (reveal), L-C6 (no REJECT).
# ---------------------------------------------------------------------------


def _do_prereg_reveal(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """Un-blind one pre-registration (ruling L-C3).

    The most consequential button on this dashboard: a reveal ends the blind
    and cannot be undone. Three guards, and none of them is the browser's to
    relax.

    **``dest_dir`` is never read from the body.** The module takes an optional
    destination and this function does not pass one, so a reveal always lands
    under ``program_root / "prereg" / "revealed"``. An HTTP caller naming a
    write path is a path-traversal primitive, not a feature -- the same reason
    EXPORT TRANSCRIPT stays disabled (12.12).

    **The audit row belongs to the module, not to this layer.**
    ``reveal_prereg`` emits ``prereg_revealed`` itself (carrying the two
    COMMITTED HASHES, never the revealed content), so a CLI reveal and a
    browser reveal leave the identical record. All this adds is ``session_id``
    -- the dashboard's own session, so the log says which sitting broke the
    blind.

    **A tampered escrow is a FINDING, not a server fault.**
    ``PreregTamperedError`` voids the row as a side effect and surfaces here as
    a clean ``{"ok": false}`` with the module's own message; ``VerifyError`` is
    in :data:`_EXPECTED_ERRORS` precisely so it can never reach the HTTP layer
    as a 500.

    The two-click confirm is the CLIENT's guard and is deliberately not
    duplicated here. A confirm token on the wire would be one more thing to
    forge; this endpoint's real protection is the write token, and the
    irreversibility being stated before the first click."""
    from trialerror.verify.prereg import reveal_prereg

    return reveal_prereg(store, prereg_id=body["prereg_id"], session_id=_clean(body.get("session_id")))


def _do_memory_resolve(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """Resolve one memory-sync conflict group: KEEP LEFT / KEEP RIGHT /
    KEEP BOTH.

    ``keep`` is validated by ``resolve_conflict`` itself -- a ``ValueError``
    naming the three legal values -- not here: this layer would only be a
    second copy of a rule that already exists, and two copies drift. The same
    call refuses a group that is already resolved, so a double-click cannot
    quietly re-answer it differently."""
    from trialerror.memory.merge import resolve_conflict

    return resolve_conflict(store, group_id=body["group_id"], keep=body["keep"])


def _do_gate_send_back(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """Object to a critic's edit and send it back -- the non-destructive
    counterpart to ``verify-edit``.

    All four fields are required, ``note`` included: a send-back with no stated
    objection is the freeze-without-reason case, and whoever has to redo the
    work needs to know what was wrong with it.

    ``by_launch`` is still free text on the wire (interim rule L-C2), and
    ``send_back_edit`` refuses an id with no ``platform.launch`` row --
    ``XidTargetMissingError``, surfaced verbatim. It never falls back to some
    other identity: an unattributable objection is worse than a refused one.
    A platform-level operator identity is a separate lane.

    REJECT (``gated -> failed``) is deliberately NOT offered (ruling L-C6): a
    destructive verb driven by a free-text identity is not auditable, so it
    stays a CLI verdict path until L-C2 lands properly."""
    return gates_api.send_back_edit(
        store,
        gate_id=body["gate_id"],
        edit_id=body["edit_id"],
        by_launch=body["by_launch"],
        note=body["note"],
    )


def _do_thread_create(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """Open a new feed thread AND post the first message into it.

    The module docstring above used to explain why this action could not
    exist: ``thread.created_by_launch`` was NOT NULL and an orchestrator
    identity has no launch. **ops v8** made the column nullable and gave
    ``thread`` the same derived ``created_by`` a post already carries, so the
    operator can now OPEN a thread exactly as they could always post into one.
    Authorship stays server-derived and is never caller-settable
    (``launch_id=None``, like :func:`_do_feed_post`).

    A first post is REQUIRED. An empty thread is a room with nobody in it, and
    both the feed rail and anything reading ``list_threads`` would show it as
    something to act on.

    **Two auto-commits, not one transaction -- and that is the safer choice
    here.** ``create_thread`` commits, then ``post_feed`` commits. A crash
    between them leaves a VISIBLE EMPTY THREAD: recoverable, obvious in the
    rail, and strictly better than the alternative failure (a post with no
    thread) that ordering the other way round would produce. Making it atomic
    would need transaction-scoped variants of two functions several other
    callers already use, to buy protection against the milder of the two
    outcomes."""
    thread = events_api.create_thread(
        store,
        title=body["title"],
        launch_id=None,
        session_id=_clean(body.get("session_id")),
    )
    post = events_api.post_feed(
        store,
        thread_id=thread["thread_id"],
        body=body["body"],
        launch_id=None,
        session_id=_clean(body.get("session_id")),
    )
    return {
        "thread_id": thread["thread_id"],
        "post_id": post["post_id"],
        "author": thread["created_by"],
        "ts": thread["created_ts"],
    }


# ---------------------------------------------------------------------------
# Lane e (E4): the four term-store decisions the Lexicon detail pane draws.
# Design: docs/reviews/LANE_E_TERM_STORE_DESIGN.md section 5; ruling L-E4.
# ---------------------------------------------------------------------------


def _do_term_sense_accept(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """``proposed -> current`` -- the evidenced route, and the only one
    (``trialerror.lexicon.api.accept_sense``'s own module docstring). Refuses
    (``SenseWithoutEvidenceError``) if every evidence row under the sense has
    since been retracted, and (``SenseNotDecidableError``) if the sense is not
    ``proposed`` -- a decided sense is superseded or retired in the open,
    never silently re-decided."""
    from trialerror.lexicon import api as lexicon_api

    return lexicon_api.accept_sense(store, body["sense_id"], by_launch=body["by_launch"])


def _do_term_sense_reject(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """``proposed -> rejected``. The evidence rows are KEPT -- the program
    looked at this reading and said no, and the material it said no to stays
    part of that record."""
    from trialerror.lexicon import api as lexicon_api

    return lexicon_api.reject_sense(
        store, body["sense_id"], by_launch=body["by_launch"], reason=_clean(body.get("reason")),
    )


def _do_term_relation_decide(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """Resolve one pending term/sense judgment -- the artboard's three
    actions (MERGE = ``same_as``/``variant_of``, SCOPE = ``scoped``, KEEP ONE
    = ``not_conflict`` with ``into``) plus REJECT.

    ``disambiguators`` (a ``scoped`` decision only) travels as a JSON OBJECT
    in the request body, ``{sense_id: disambiguator_text, ...}`` -- one entry
    per member sense a conflict names -- not a string, so it is checked
    against :data:`_NON_STRING_FIELDS` rather than the string default every
    other field here gets. ``decide_relation`` itself is what validates
    every other rule (which decision applies to which relation shape,
    whether every member got a disambiguator, whether ``into``/``canonical``
    names a real member) -- this wrapper adds no rule of its own, matching
    the module docstring's "never a second implementation"."""
    from trialerror.lexicon import api as lexicon_api

    disambiguators = body.get("disambiguators")
    if disambiguators is not None and not isinstance(disambiguators, Mapping):
        raise ValueError(
            f"term-relation-decide: disambiguators must be an object, got {type(disambiguators).__name__}"
        )
    return lexicon_api.decide_relation(
        store,
        body["rel_id"],
        decision=body["decision"],
        by_launch=body["by_launch"],
        disambiguators=disambiguators,
        into=_clean(body.get("into")),
        canonical=_clean(body.get("canonical")),
        reason=_clean(body.get("reason")),
    )


def _do_term_mark_reviewed(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """"I looked at this and it is still right" -- resets the sense's
    engram-F5 decay window (MINING §5.7) and changes nothing else. Staleness
    surfaces a row for a human; reviewing it is cheap, and neither ever
    decides anything on its own."""
    from trialerror.lexicon import api as lexicon_api

    return lexicon_api.mark_reviewed(store, body["sense_id"], by_launch=body["by_launch"])


def _do_feed_translate(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """ENQUEUE a plain-English translation job for one post or one thread.
    Never translates inline: the operator's click books work on the M2
    ledger and returns immediately, and the feed panel renders that post's
    right-hand column as "translation pending" until a worker lands a row
    (``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 4.1's option C;
    its option B -- "book a launch per VIEW" -- is exactly what this
    avoids).

    This is the one action here that does not wrap a business-logic
    function some CLI verb already calls: it calls the same
    ``trialerror.jobs.ledger.enqueue`` with the same payload
    ``trialerror.cli.feed.run_translate`` builds. That payload shape is the
    handler's documented contract
    (``trialerror.feed_translate.handlers.run_feed_translate``), and the
    HANDLER -- never this function -- owns every rule about what gets
    translated and how it is gated.

    ``created_by_launch`` is deliberately never taken from the request
    body: a dashboard operator has no launch identity (the same reason
    :func:`_do_feed_post` always passes ``launch_id=None``), so a
    translation requested here is stored under the orchestrator's
    no-launch identity, exactly like an orchestrator feed post. A
    budget-spending backend refuses such a job outright rather than
    running unbooked -- see that handler's "Budget law" note.
    """
    post_id = _clean(body.get("post_id"))
    thread_id = _clean(body.get("thread_id"))
    if bool(post_id) == bool(thread_id):
        raise ValueError("feed-translate: give exactly one of post_id / thread_id")
    style_mode = _clean(body.get("style_mode")) or "flavored"
    if style_mode not in ("flavored", "strict"):
        raise ValueError(f"feed-translate: style_mode must be 'flavored' or 'strict', got {style_mode!r}")

    payload: dict[str, Any] = {"handler": "feed_translate", "style_mode": style_mode, "created_by_launch": None}
    if post_id:
        payload["post_ids"] = [post_id]
    else:
        payload["thread_id"] = thread_id
    job = jobs_ledger.enqueue(store, kind="custom", payload=payload)
    return {"job_id": job["job_id"], "state": job["state"], "kind": job["kind"], "target": post_id or thread_id}


def _do_worker_control(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """Ask a GPU worker to pause, resume or stop (ruling C-0097 D5).

    Calls ``trialerror.offload.control.request_control_for_launch`` -- the SAME
    function ``trialerror offload worker-control`` calls -- so the three
    refusals (unknown worker, a request already pending, no launch) are
    identical on both surfaces because they are literally the same code, not
    because two lists happen to agree today. The act appends one
    ``offload_worker_control`` event, exactly the shape lane e used for
    ``term_candidate_withdrawn``.

    **No process control anywhere.** This write leaves a small JSON file in the
    queue; the worker reads it off its next heartbeat reply and complies at its
    next cooperative checkpoint. The dashboard cannot reach the worker's
    machine and nothing in this harness kills anything (D8) -- which is why the
    result says ``requested``, never "paused".

    The queue lives beside the program, so its root comes from
    ``store.program_root`` and never from the body: a caller-supplied queue
    path would be an HTTP request choosing which directory to write into."""
    from trialerror.offload import control as offload_control

    request = _clean(body.get("request"))
    if request not in ("pause", "resume", "stop"):
        raise ValueError(f"worker-control: request must be pause, resume or stop, got {request!r}")
    record = offload_control.request_control_for_launch(
        store,
        offload_protocol.offload_root(store.program_root),
        worker_id=str(_clean(body.get("worker_id"))),
        request=str(request),
        by_launch=str(_clean(body.get("by_launch"))),
        job_id=_clean(body.get("job_id")),
    )
    return {
        "worker_id": record["worker_id"],
        "requested": record["request"],
        "by_launch": record["by_launch"],
        "job_id": record.get("job_id"),
        "ts": record["ts"],
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
    "feed-translate": _do_feed_translate,
    # lane C (C7): the four the Determinations queue used to draw disabled.
    "prereg-reveal": _do_prereg_reveal,
    "memory-resolve": _do_memory_resolve,
    "gate-send-back": _do_gate_send_back,
    "thread-create": _do_thread_create,
    # lane e (E4): the term-store decisions the Lexicon detail pane draws.
    "term-sense-accept": _do_term_sense_accept,
    "term-sense-reject": _do_term_sense_reject,
    "term-relation-decide": _do_term_relation_decide,
    "term-mark-reviewed": _do_term_mark_reviewed,
    # C-0097 (D5): the JOBS card's worker rows get a verb. Named with the
    # hyphen every other action in this table uses -- the design writes it
    # `worker_control`, which is the EVENT type's spelling (events are
    # underscore-keyed); the action namespace has been hyphenated since the
    # first one.
    "worker-control": _do_worker_control,
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
    # feed-translate takes exactly ONE of post_id/thread_id, which this
    # flat "every listed field is required" table cannot express --
    # validated inside the handler instead, where a ValueError becomes the
    # same clean {"ok": false, "message": ...} refusal (_EXPECTED_ERRORS).
    "feed-translate": (),
    # lane C (C7).
    "prereg-reveal": ("prereg_id",),
    "memory-resolve": ("group_id", "keep"),
    # note is required on purpose: a send-back with no stated objection is
    # the freeze-without-reason case (spec section 4).
    "gate-send-back": ("gate_id", "edit_id", "by_launch", "note"),
    # body is required: an empty thread is a room with nobody in it.
    "thread-create": ("title", "body"),
    # lane e (E4). "reason" is optional on both accept and reject (unlike
    # gate-send-back's note): a rejection is legible from the relation/sense
    # it closes even with nothing further said, and MissingDisambiguatorError
    # (a real 200 refusal) already names exactly what a bad 'scoped' decision
    # is missing -- a second flat-table rule here would just repeat it.
    "term-sense-accept": ("sense_id", "by_launch"),
    "term-sense-reject": ("sense_id", "by_launch"),
    "term-relation-decide": ("rel_id", "decision", "by_launch"),
    "term-mark-reviewed": ("sense_id", "by_launch"),
    # C-0097 (D5). `by_launch` is required for the same reason every lane e
    # action requires it (L-E4): a control act with no launch is the machine
    # deciding on its own. `request` is validated by VALUE in the handler --
    # this flat table can say "present", not "one of three".
    "worker-control": ("worker_id", "request", "by_launch"),
}


#: Every body field this module reads that is NOT a plain string, and the
#: Python type(s) a JSON body may legitimately carry it as. Anything absent
#: from this table is a STRING field -- which is what makes
#: :func:`_validate_fields` a closed check rather than a best-effort one.
#:
#: M-WA-1/M-WA-2 (sweep §5, promoted to batch W3): before this table, a JSON
#: object or number in a string field sailed past the old
#: ``_missing_fields`` (it is neither ``None`` nor a blank string), reached
#: SQLite as a ``dict``, and died there with ``sqlite3.ProgrammingError`` --
#: an exception no layer caught, so the socket closed with NO response at
#: all (HTTP 000) even though this module's docstring promises a 500.
#: Worse, a JSON value sqlite CAN bind (a number in
#: ``verified_note``/``reason``) was silently persisted into a text column.
#: Both are the same missing type check.
#:
#: ``agreement_pct`` also accepts ``str``: the dashboard's own score form
#: reads it off an ``<input type="number">``, whose ``.value`` is a string,
#: and the CLI's ``--agreement-pct`` is argv. Whether that string is a
#: NUMBER is :func:`_do_room_score`'s own check, which already raises a
#: ``ValueError`` naming the field and echoing the value -- a better
#: message than anything this table could produce, and the reason this
#: check is about JSON SHAPE only.
_NON_STRING_FIELDS: dict[str, tuple[type, ...]] = {
    "agreement_pct": (int, float, str),
    # lane e (E4): {sense_id: disambiguator_text, ...} on a 'scoped' decision
    # -- the one field in this table whose JSON type is an OBJECT rather than
    # a wider scalar set, because it is genuinely structured data, not a
    # single value read off one form control.
    "disambiguators": (dict,),
}


def _validate_fields(action: str, body: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(missing, type_errors)`` for one action's body.

    ``missing`` keeps the original semantics (a required field that is
    absent, ``None``, or whitespace-only). ``type_errors`` is the new half:
    EVERY field present in the body is checked against
    :data:`_NON_STRING_FIELDS` (string unless listed there), so a client
    bug is refused by name before any store is opened -- never handed to
    the database to fail on.

    A boolean is rejected for a numeric field deliberately: ``True`` is an
    ``int`` in Python, but it is never what a caller meant by
    ``agreement_pct``."""
    missing: list[str] = []
    for field in REQUIRED_FIELDS.get(action, ()):
        value = body.get(field)
        if value is None:
            missing.append(field)
        elif isinstance(value, str) and not value.strip():
            missing.append(field)

    type_errors: list[str] = []
    for field, value in body.items():
        if value is None:
            continue
        expected = _NON_STRING_FIELDS.get(field)
        if expected is None:
            if not isinstance(value, str):
                type_errors.append(f"{field} must be a string, got {type(value).__name__}")
        elif isinstance(value, bool) or not isinstance(value, expected):
            names = "/".join(t.__name__ for t in expected)
            type_errors.append(f"{field} must be a {names}, got {type(value).__name__}")
    return missing, type_errors


def _is_lexicon_refusal(exc: BaseException) -> bool:
    """Whether ``exc`` is a :class:`trialerror.lexicon.errors.LexiconError` --
    checked with a LAZY import, on purpose, rather than by adding
    ``LexiconError`` to :data:`_EXPECTED_ERRORS` directly.

    ``trialerror.lexicon``'s package ``__init__.py`` asserts a minimum SQLite
    version for its ``term_fts`` trigram index and raises on a build too old
    to host it -- deliberately ALSO an :class:`ImportError`
    (``trialerror/lexicon/errors.py::UnsupportedSqliteError``'s own
    docstring). ``_EXPECTED_ERRORS`` is built once, at this module's IMPORT
    time; naming ``LexiconError`` there would mean importing
    ``trialerror.lexicon.errors`` right then -- which imports the PACKAGE
    first, running that assertion whether or not this program ever calls a
    ``term-*`` action. A machine that cannot host the lexicon would then
    fail to import this file at all, breaking every OTHER write action
    (room turns, gate verifies) that has nothing to do with it. Calling this
    only from inside :func:`dispatch`'s except clause -- after a handler has
    already run -- means the import is paid only by a caller who actually
    invoked a lexicon action, the same lazy-import discipline the four
    ``_do_term_*`` handlers themselves already follow.

    An ``ImportError`` here (the package genuinely is not importable) means
    "this is not a lexicon refusal" -- ``False``, not a raise -- so an
    environment where the package cannot even be checked still gets the
    correct answer for every non-lexicon action, and a genuine bug in one
    of THOSE still reaches the 500 path unchanged."""
    try:
        from trialerror.lexicon.errors import LexiconError
    except ImportError:
        return False
    return isinstance(exc, LexiconError)


def dispatch(
    action: str,
    *,
    program_root: Path | str | None,
    platform_root: Path | str | None,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Validate + execute one write action. Never raises for an EXPECTED
    refusal (unknown action, no program selected, a missing required field,
    a field of the wrong JSON type, any :data:`_EXPECTED_ERRORS` the
    business-logic call itself raises, or a lexicon refusal -- see
    :func:`_is_lexicon_refusal` for why that one is checked separately
    rather than folded into :data:`_EXPECTED_ERRORS`) -- each of those is
    reported as ``{"ok": False, "message": ...}``. Any OTHER exception
    propagates (module docstring: a genuine bug must look like one, never a
    disguised refusal); the HTTP layer turns it into a 500 JSON envelope with
    the traceback on stderr."""
    handler = WRITABLE_ACTIONS.get(action)
    if handler is None:
        return {"ok": False, "status": "unknown_action", "message": f"no such write action: {action!r}"}
    if program_root is None:
        return {
            "ok": False, "status": "no_program_root",
            "message": "no program is selected on this dashboard (no --program-root) -- writes need a real program",
        }
    missing, type_errors = _validate_fields(action, body)
    if missing:
        return {
            "ok": False, "status": "missing_fields",
            "message": f"missing required field(s) for {action!r}: {', '.join(missing)}",
        }
    if type_errors:
        return {
            "ok": False, "status": "bad_request",
            "message": f"bad field type(s) for {action!r}: {'; '.join(sorted(type_errors))}",
        }

    store = open_store(Path(program_root), platform_root=Path(platform_root) if platform_root else None)
    try:
        result = handler(store, body)
    except _EXPECTED_ERRORS as exc:
        return {"ok": False, "status": type(exc).__name__, "message": str(exc)}
    except Exception as exc:
        if _is_lexicon_refusal(exc):
            return {"ok": False, "status": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        store.close()
    return {"ok": True, "result": result}
