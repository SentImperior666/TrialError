"""Worker control and worker observability: the semantics of the control
word and the progress file (ruling C-0097,
``docs/reviews/WORKER_CONTROL_DESIGN.md`` decisions D1-D4, D7).

The law this implements, restated from the design: **anything the interface
can start, the interface must be able to pause and stop, and the dashboard
must show the job's status.** For the GPU worker that is not a process
signal -- the sandbox cannot reach the worker's machine at all, and there is
no preemptive kill anywhere in this harness (D8). It is a *request left in
the queue* and a *cooperative checkpoint* in the worker:

    sandbox writes  claimed/<worker>/CONTROL.json   (this module, one writer)
    worker reads    the word the `heartbeat` verb prints on stdout
    worker obeys    at its next checkpoint, finishing the unit it is on
    worker reports  claimed/<worker>/<job>.progress.json (this module reads)

Three properties are worth naming because each one is a decision rather
than an implementation detail:

- **One writer.** :func:`request_control` is the ONLY function that writes a
  control request. ``trialerror offload worker-control`` calls it and so
  does the dashboard's ``worker-control`` write action, so the refusals
  (unknown worker, a request already pending, no launch) cannot differ
  between the two surfaces -- the design's D5, read as code.
- **Every control act carries a launch** (the L-E4 posture). A pause with no
  ``by_launch`` is refused by name, before anything is written. The XID check
  against ``platform.launch`` belongs to the CALLERS (they hold a ``Store``;
  this module holds a directory), and both apply it before calling here.
- **A stale request is not a request.** A ``CONTROL.json`` older than
  :data:`DEFAULT_CONTROL_TTL_S` is reported (``stale``) and the word read
  over it is ``none``: a worker that was switched off for a week must not
  come back and immediately stop itself because of something an operator
  asked for last Tuesday.
- **A request has a LIFECYCLE, and the sandbox is the half that closes it**
  (FIX V-2/V-3). ``written`` -> ``read`` (a beat reports ``control_seen``) ->
  ``spent`` (read, and nothing left to stand for) -> ``reaped``. The first
  version of this module wrote requests and never ended them, and the two
  things that breaks are the two halves of the law: ``resume`` was refused for
  the whole TTL as "still pending" (so a pause could not be undone), and a
  ``stop`` outlived the worker it stopped (so every restart inside the hour
  stopped itself -- the exact manoeuvre the ruling was written for). The rules
  live in :func:`control_was_read`, :func:`control_is_spent` and
  :func:`reap_spent_controls`; only the sandbox side can apply them, because
  no verb of the seven-verb contract can delete this file.

The progress file is the other half. It exists so the JOBS card can tell
three states apart that all look identical from the queue directory alone:
a worker running a job, a worker sitting idle with nothing claimed, and no
worker at all. The first two write a file; the third is the absence of one.
``lost`` is a READING over that file's age, never a state a worker writes
about itself -- see :func:`lost_after_s`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from trialerror.offload import protocol
from trialerror.offload.shell import (
    CONTROL_WORDS,
    MAX_PROGRESS_BYTES,
    PROGRESS_KEYS,
    PROGRESS_SETTINGS_KEYS,
    progress_payload_refusal,
)
from trialerror.util.timeutil import now, parse

__all__ = [
    "CONTROL_REQUESTS",
    "CONTROL_NONE",
    "CONTROL_WORDS",
    "WORKER_STATES",
    "WORKER_GONE_STATES",
    "MAX_PROGRESS_BYTES",
    "PROGRESS_KEYS",
    "PROGRESS_SETTINGS_KEYS",
    "DEFAULT_CONTROL_TTL_S",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "LOST_WINDOW_PAD_S",
    "MAX_PROGRESS_STRING_CHARS",
    "CONTROL_EVENT_TYPE",
    "ControlError",
    "lost_after_s",
    "request_control",
    "request_control_for_launch",
    "read_control",
    "control_word",
    "clear_control",
    "clear_control_for_launch",
    "control_was_read",
    "control_is_spent",
    "reap_spent_controls",
    "list_controls",
    "validate_progress",
    "encode_progress",
    "read_progress",
    "worker_rows",
    "worker_row",
]

#: D1: the three things an operator may ask a worker for. There is no
#: ``kill``: D8 forbids preemptive termination anywhere in the harness, and a
#: worker that will not stop cooperatively is a DEFECT the doctor reports
#: (``worker_heartbeat_stale``), not a process to shoot.
CONTROL_REQUESTS = ("pause", "resume", "stop")
CONTROL_NONE = "none"

#: D3's ``state`` vocabulary, shared with the sandbox jobs subsystem's own
#: pause/resume (D8's last clause) so one word means one thing on the card.
#: ``idle`` is the worker-level state that goes with
#: :func:`trialerror.offload.protocol.idle_job_id`.
#:
#: ``exited`` is one word past D3's four, added by FIX V-9: a run's LAST beat
#: used to report ``idle``, so a worker whose process had ended read as "alive
#: with nothing to do" for the whole lost window (eleven minutes at the default
#: interval). The worker cannot delete its own progress file -- no verb of the
#: seven-verb contract can -- so the honest alternative is a terminal state the
#: card and the status line can label.
WORKER_STATES = ("claiming", "running", "paused", "stopping", "idle", "exited")

#: The states that mean "this worker is not working under anything any more".
#: Used by :func:`control_is_spent` to decide when a standing instruction has
#: nothing left to stand for.
WORKER_GONE_STATES = ("idle", "exited")

#: D1: "A stale ``CONTROL.json`` (older than ``control_ttl_s``, default 1 h)
#: is ignored and reported."
DEFAULT_CONTROL_TTL_S = 60.0 * 60.0

#: The worker's own beat interval, named here rather than in
#: ``trialerror.offload.worker`` so the dashboard can compute the lost window
#: without importing the worker (which drags in ~32 modules -- the same
#: hazard ``trialerror.cli.offload`` documents about its parser).
DEFAULT_HEARTBEAT_INTERVAL_S = 300.0

#: D4: ``lost`` past ``2 x heartbeat_interval_s + 60``. Two missed beats plus
#: a minute: one missed beat is an ordinary dropped SSH connection (the
#: heartbeat thread swallows those on purpose), two in a row with no third is
#: a machine that went away.
LOST_WINDOW_PAD_S = 60.0

#: D3: "strings <= 256 chars".
MAX_PROGRESS_STRING_CHARS = 256

#: D5: one event per control act, the same shape lane e used for
#: ``term_candidate_withdrawn`` -- underscore-keyed, like every other event
#: type in the log.
CONTROL_EVENT_TYPE = "offload_worker_control"


class ControlError(protocol.OffloadProtocolError):
    """A control request this module refuses to write, or a progress payload
    it refuses to send. A subclass of the queue's own protocol error so
    every existing caller's ``except OffloadError`` still covers it."""


def lost_after_s(heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S) -> float:
    """The age past which a worker's progress file means "gone", not "busy"."""
    return 2.0 * float(heartbeat_interval_s) + LOST_WINDOW_PAD_S


# ---------------------------------------------------------------------------
# the control word: one writer, one reader
# ---------------------------------------------------------------------------
def request_control(
    root: Path | str,
    *,
    worker_id: str,
    request: str,
    by_launch: str,
    job_id: str | None = None,
    ts: str | None = None,
    require_worker: bool = True,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
) -> dict[str, Any]:
    """Write ``claimed/<worker_id>/CONTROL.json``. The ONE writer (D5).

    Refuses, by name and before writing anything:

    * a ``request`` outside :data:`CONTROL_REQUESTS`;
    * a missing ``by_launch`` -- ruling L-E4's wording, the same sentence
      ``trialerror.lexicon.api`` refuses with, because "no launch, no
      control" is the same law in both places;
    * an unknown worker: no progress file inside the lost window, i.e.
      nothing is listening. ``require_worker=False`` is for the operator who
      wants the request waiting BEFORE the worker starts (the launcher case),
      and the CLI exposes it as ``--even-if-absent``;
    * **the SAME request already pending and unread**: asking twice for a word
      the worker has not acknowledged yet is the one case where silence is
      worth a refusal -- the operator's first instruction is still in flight,
      and overwriting it would only reset its timestamp. "Unread" is
      :func:`control_was_read`, measured against what the worker REPORTS
      (``control_seen`` on a beat later than the request), not assumed.

      Every OTHER follow-up goes through, and that is FIX V-2: a ``resume``
      after a ``pause`` is an operator changing their mind, which is the whole
      point of having a resume, and it used to be refused for the full TTL
      because nothing ever cleared the file -- making the design's own
      acceptance step ("resume, watch progress continue") unperformable. A
      ``stop`` was allowed through from the start, which is how this stayed
      invisible.

    Before any of that, a request the worker has demonstrably ACTED on is
    reaped (FIX V-3, :func:`reap_spent_controls`): an hour-old ``stop`` that a
    worker already obeyed must not stop the next run the operator starts.

    Returns the record as written, plus ``path``.
    """
    root = Path(root)
    protocol._validate_worker_id(worker_id)
    if request not in CONTROL_REQUESTS:
        raise ControlError(
            f"offload worker-control: request must be one of {'/'.join(CONTROL_REQUESTS)}, "
            f"got {request!r}"
        )
    if not by_launch or not str(by_launch).strip():
        raise ControlError(
            "offload worker-control requires by_launch (ruling L-E4: an existing "
            "platform.launch row) -- no launch, no control"
        )
    if job_id is not None:
        protocol.validate_job_id(job_id)

    if require_worker:
        row = worker_row(root, worker_id, heartbeat_interval_s=heartbeat_interval_s)
        if row is None or row["lost"]:
            raise ControlError(
                f"offload worker-control: no worker {worker_id!r} has reported within the last "
                f"{lost_after_s(heartbeat_interval_s):.0f}s -- start it first, or pass "
                "--even-if-absent to leave the request waiting"
            )

    # FIX V-3: a spent request is not a pending request. Reaping here is what
    # makes "ask again" work after a worker has acted and exited; the janitor
    # (``offload kick``) and ``worker-status`` reap too, so a RESTART inside the
    # TTL starts clean without the operator doing anything.
    reap_spent_controls(root, worker_id=worker_id, heartbeat_interval_s=heartbeat_interval_s)

    pending = read_control(root, worker_id)
    if (
        pending is not None
        and not pending["stale"]
        and request != "stop"
        and pending["request"] == request
        and not control_was_read(
            worker_row(root, worker_id, heartbeat_interval_s=heartbeat_interval_s), pending
        )
    ):
        raise ControlError(
            f"offload worker-control: an unread {pending['request']!r} request for worker "
            f"{worker_id!r} is still pending (written {pending['ts']}) -- no beat has reported "
            f"reading it yet, so asking again would only move its timestamp; a different word is "
            "accepted at any time, and --clear drops it"
        )

    record = {
        "schema": protocol.CONTROL_SCHEMA,
        "request": request,
        "by_launch": str(by_launch),
        "ts": ts or now(),
        "job_id": job_id,
    }
    path = protocol.control_path(root, worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    protocol.write_json(path, record)
    return {**record, "worker_id": worker_id, "path": str(path)}


def request_control_for_launch(
    store: Any,
    root: Path | str,
    *,
    worker_id: str,
    request: str,
    by_launch: str,
    job_id: str | None = None,
    require_worker: bool = True,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
) -> dict[str, Any]:
    """:func:`request_control` with the launch actually checked and the act
    recorded. **The one function both control surfaces call** (D5).

    Order matters and is the same order every other launch-gated write in this
    harness uses: refuse a missing launch, then refuse a launch with no
    ``platform.launch`` row (``XidTargetMissingError``, never a fallback
    identity), then apply the queue-side refusals, then write, then append the
    event. Nothing is written before every refusal has had its chance --
    a half-applied control act would be an instruction nobody authorised.

    ``store`` is typed loosely on purpose: this module is the queue's
    semantics layer and has no business importing the store package at
    module scope (the same discipline ``trialerror.offload.protocol``
    follows). Both imports below are local."""
    from trialerror.events.api import append_event
    from trialerror.stores.writer import require_xid_targets

    if not by_launch or not str(by_launch).strip():
        raise ControlError(
            "offload worker-control requires by_launch (ruling L-E4: an existing "
            "platform.launch row) -- no launch, no control"
        )
    require_xid_targets(store, "event", {"launch_id": by_launch})
    record = request_control(
        root,
        worker_id=worker_id,
        request=request,
        by_launch=by_launch,
        job_id=job_id,
        require_worker=require_worker,
        heartbeat_interval_s=heartbeat_interval_s,
    )
    append_event(
        store,
        event_type=CONTROL_EVENT_TYPE,
        payload={
            "worker_id": worker_id,
            "request": record["request"],
            "job_id": record.get("job_id"),
            "by_launch": by_launch,
            "queue_root": str(Path(root)),
        },
        launch_id=by_launch,
        ts=record["ts"],
    )
    return record


def clear_control_for_launch(
    store: Any,
    root: Path | str,
    *,
    worker_id: str,
    by_launch: str,
) -> dict[str, Any]:
    """Drop a worker's pending request, attributably (FIX V-3).

    A clear is an ACT, not a request: it changes what the worker will be told,
    so it carries a launch exactly like the three words do (L-E4) and lands in
    the event log under the same type with ``request: "clear"``. It is the
    operator's way out of the one case the automatic reaping cannot reach -- a
    request no worker ever read, or one they changed their mind about before any
    sandbox-side verb ran -- and the alternative was waiting out the TTL."""
    from trialerror.events.api import append_event
    from trialerror.stores.writer import require_xid_targets

    if not by_launch or not str(by_launch).strip():
        raise ControlError(
            "offload worker-control requires by_launch (ruling L-E4: an existing "
            "platform.launch row) -- no launch, no control"
        )
    protocol._validate_worker_id(worker_id)
    require_xid_targets(store, "event", {"launch_id": by_launch})
    previous = read_control(root, worker_id)
    cleared = clear_control(root, worker_id)
    ts = now()
    append_event(
        store,
        event_type=CONTROL_EVENT_TYPE,
        payload={
            "worker_id": worker_id,
            "request": "clear",
            "cleared": cleared,
            "previous_request": None if previous is None else previous.get("request"),
            "by_launch": str(by_launch),
            "queue_root": str(Path(root)),
        },
        launch_id=by_launch,
        ts=ts,
    )
    return {
        "worker_id": worker_id,
        "request": "clear",
        "cleared": cleared,
        "previous_request": None if previous is None else previous.get("request"),
        "by_launch": str(by_launch),
        "ts": ts,
    }


def read_control(
    root: Path | str, worker_id: str, *, ttl_s: float | None = None
) -> dict[str, Any] | None:
    """The worker's pending request, or ``None`` when there is no file.

    The returned record carries ``age_s`` and ``stale`` -- a stale request is
    REPORTED, never silently deleted: the doctor's
    ``worker_heartbeat_stale`` check fails on a stop request the worker has
    been ignoring, and deleting the evidence here would be the one thing that
    hides that defect."""
    ttl = DEFAULT_CONTROL_TTL_S if ttl_s is None else float(ttl_s)
    path = protocol.control_path(root, worker_id)
    if not path.is_file():
        return None
    try:
        record = protocol.read_json(path)
    except (OSError, ValueError) as exc:
        # An unreadable request is a refused request: acting on a half-written
        # file would be worse than ignoring it, and the reason travels out.
        return {
            "worker_id": worker_id,
            "request": None,
            "by_launch": None,
            "ts": None,
            "job_id": None,
            "age_s": None,
            "stale": True,
            "unreadable": f"{type(exc).__name__}: {exc}",
            "path": str(path),
        }
    age = _age_s(record.get("ts"))
    request = record.get("request")
    if request not in CONTROL_REQUESTS:
        request = None
    return {
        "worker_id": worker_id,
        "request": request,
        "by_launch": record.get("by_launch"),
        "ts": record.get("ts"),
        "job_id": record.get("job_id"),
        "age_s": age,
        "stale": request is None or age is None or age > ttl,
        "ttl_s": ttl,
        "path": str(path),
    }


def control_word(root: Path | str, worker_id: str, *, ttl_s: float | None = None) -> str:
    """What the ``heartbeat`` verb prints: one of :data:`CONTROL_WORDS`.

    A stale or unreadable request reads as ``none``; the staleness itself is
    visible to the sandbox through :func:`read_control`, which is where it
    belongs -- the worker's job is to obey live instructions, not to
    adjudicate old ones."""
    record = read_control(root, worker_id, ttl_s=ttl_s)
    if record is None or record["stale"] or record["request"] is None:
        return CONTROL_NONE
    return str(record["request"])


def clear_control(root: Path | str, worker_id: str) -> bool:
    """Remove a worker's pending request. Returns whether there was one.

    Called by the WORKER (through the sandbox-side reclaim path, never over
    the restricted key) once it has acted: a ``resume`` that stayed on disk
    would be re-read as a fresh instruction on every later beat."""
    path = protocol.control_path(root, worker_id)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:  # pragma: no cover - a vanished file is the desired state
        return False
    return True


def control_was_read(row: Mapping[str, Any] | None, record: Mapping[str, Any] | None) -> bool:
    """Whether the worker has DEMONSTRABLY read this control request (FIX V-2).

    The old "a request is pending, therefore unread" rule asserted something
    the code could not know, and was false in the one case that mattered: a
    pause the worker had already picked up still blocked every later request
    for the full TTL, so ``resume`` was unreachable.

    What the worker actually reports is ``control_seen`` -- the last word it
    applied -- on every beat. So "read" is two facts, both from the queue:

    * the newest progress row names this request in ``control_seen``, and
    * that row was beaten AFTER the request was written
      (``heartbeat_age_s <= age_s``; both are ages from the same read, so the
      younger one happened later).

    The second half is what keeps a SECOND pause from being mistaken for the
    first one's acknowledgement -- the words are equal, so only the clock can
    tell them apart."""
    if row is None or record is None or not record.get("request"):
        return False
    if row.get("control_seen") != record["request"]:
        return False
    beat_ts, request_ts = row.get("heartbeat_ts"), record.get("ts")
    if beat_ts and request_ts:
        try:
            return parse(str(beat_ts)) >= parse(str(request_ts))
        except Exception:  # noqa: BLE001 - an unparseable stamp falls through
            pass
    beat_age = row.get("heartbeat_age_s")
    request_age = record.get("age_s")
    if beat_age is None or request_age is None:
        return False
    # No stamp to compare (the claim ended between two reads, so the age came
    # off an mtime): allow a second of slack, because the two ages were measured
    # against two different readings of the clock.
    return float(beat_age) <= float(request_age) + 1.0


def control_is_spent(row: Mapping[str, Any] | None, record: Mapping[str, Any] | None) -> bool:
    """Whether a control request has been acted on and has nothing left to do
    (FIX V-3).

    Spent is strictly narrower than read, and the difference is what keeps the
    doctor's witness alive:

    * ``resume`` is spent the moment it is read -- there is nothing standing
      about "carry on".
    * ``pause`` and ``stop`` are STANDING instructions while the worker is
      still working under them, so they are spent only once it has gone
      (``idle``/``exited``, or silent past the lost window). A worker still
      reporting ``running`` with an acknowledged hour-old stop is exactly the
      defect ``worker_heartbeat_stale`` fails on, and deleting the evidence
      would be the one thing that hides it.

    The consequence that matters operationally: a stop no longer outlives the
    run it stopped. Before this, every restart inside the one-hour TTL stopped
    itself immediately -- which is the very manoeuvre (stop the worker, restart
    it with a smaller batch size) the ruling was written for."""
    if not control_was_read(row, record):
        return False
    if record is not None and record.get("request") == "resume":
        return True
    if row is None:
        return False
    return bool(row.get("lost")) or row.get("state") in WORKER_GONE_STATES


def reap_spent_controls(
    root: Path | str,
    *,
    worker_id: str | None = None,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
) -> list[dict[str, Any]]:
    """Delete every control request the worker has acted on and finished with.
    Returns the records that were removed (FIX V-3).

    Runs SANDBOX-SIDE only, which is the whole reason it is a function rather
    than something the worker does on its way out: the restricted key has no
    verb that can delete this file, and giving it one would change the
    seven-verb contract (D8). So the three places that already run in the
    container call it -- ``worker-control`` (before it judges a pending
    request), ``worker-status`` (what the status line polls) and ``kick`` (the
    janitor the jobs loop runs every cycle) -- and ``worker-control --clear``
    is the operator's explicit override for anything those cannot reach."""
    root = Path(root)
    reaped: list[dict[str, Any]] = []
    records = (
        [r for r in list_controls(root) if r["worker_id"] == worker_id]
        if worker_id is not None
        else list_controls(root)
    )
    for record in records:
        row = worker_row(root, record["worker_id"], heartbeat_interval_s=heartbeat_interval_s)
        if not control_is_spent(row, record):
            continue
        if clear_control(root, record["worker_id"]):
            reaped.append({**record, "cleared": True})
    return reaped


def list_controls(root: Path | str) -> list[dict[str, Any]]:
    """Every pending control request in the queue, for the doctor's
    ``offload_control_orphaned`` check and ``worker-status``."""
    root = Path(root)
    claimed = protocol.claimed_dir(root)
    out: list[dict[str, Any]] = []
    if not claimed.is_dir():
        return out
    for worker in sorted(claimed.iterdir()):
        if not worker.is_dir():
            continue
        record = read_control(root, worker.name)
        if record is not None:
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# the progress payload
# ---------------------------------------------------------------------------
def validate_progress(payload: Mapping[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """D3's validation, in full, on the side that can actually parse JSON.

    The wrapper checks size, object-shape and the key allowlist (see
    :func:`trialerror.offload.shell.progress_payload_refusal`, which both
    halves share). This adds the two rules a shell cannot apply: strings are
    at most :data:`MAX_PROGRESS_STRING_CHARS` characters and numbers are
    finite. It is applied by the WORKER, before sending -- so a payload that
    would be refused on arrival never costs a heartbeat -- and by the
    in-process transport, so the test suite exercises the same gate.

    Over-long strings are TRUNCATED rather than refused for exactly one key,
    ``last_error``: an error message is the most useful field on the card and
    the least predictable in length, and dropping the whole heartbeat because
    a backend raised a long exception would lose the status at the one moment
    it matters. Every other over-long string is a protocol violation by the
    worker's own code and refuses.

    ``strict=False`` is the READ side (FIX V-5), and the difference is only in
    what it does with input it cannot use rather than in what it accepts: an
    unknown key is DROPPED instead of refusing the file (the wrapper's own key
    check only sees identifier-shaped keys, so a stored payload can carry one
    neither half names, and the row builder has always ignored those), and every
    over-long string is truncated instead of only ``last_error``. Everything
    else still raises -- a nested object where a scalar belongs, a non-finite
    number, a state outside the vocabulary -- and :func:`read_progress` turns
    that raise into the ``unreadable`` row the card can already draw. Before
    this, those three went through untouched: ``Infinity``/``NaN`` reached the
    dashboard's serialiser and took the whole console bundle out of JSON, and a
    2,000-character ``last_error`` reached the card."""
    if not isinstance(payload, Mapping):
        raise ControlError("offload heartbeat: progress payload must be a JSON object")
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in PROGRESS_KEYS:
            if not strict:
                continue
            raise ControlError(f"offload heartbeat: progress payload carries an unknown key {key!r}")
        if key == "settings":
            out[key] = _validate_settings(value, strict=strict)
            continue
        out[key] = _validate_scalar(key, value, truncate=(not strict or key == "last_error"))
    state = out.get("state")
    if state is not None and state not in WORKER_STATES:
        raise ControlError(
            f"offload heartbeat: state must be one of {'/'.join(WORKER_STATES)}, got {state!r}"
        )
    return out


def _validate_settings(value: Any, *, strict: bool = True) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ControlError("offload heartbeat: progress 'settings' must be a JSON object")
    out: dict[str, Any] = {}
    for key, inner in value.items():
        if key not in PROGRESS_SETTINGS_KEYS:
            if not strict:
                continue
            raise ControlError(
                f"offload heartbeat: progress settings carries an unknown key {key!r}"
            )
        out[key] = _validate_scalar(f"settings.{key}", inner, truncate=not strict)
    return out


def _validate_scalar(key: str, value: Any, *, truncate: bool) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_PROGRESS_STRING_CHARS:
            if truncate:
                return value[: MAX_PROGRESS_STRING_CHARS - 1] + "…"
            raise ControlError(
                f"offload heartbeat: progress {key!r} is {len(value)} characters, over the "
                f"{MAX_PROGRESS_STRING_CHARS}-character limit"
            )
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ControlError(f"offload heartbeat: progress {key!r} is not a finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_validate_scalar(key, v, truncate=truncate) for v in value]
    raise ControlError(
        f"offload heartbeat: progress {key!r} must be a string, number or null, got "
        f"{type(value).__name__}"
    )


def encode_progress(payload: Mapping[str, Any]) -> bytes:
    """Validate and serialise a progress payload to the bytes the
    ``heartbeat`` verb carries on stdin.

    Compact separators, on purpose: the cap is on BYTES, and a payload that
    failed only because it was pretty-printed would be the silliest possible
    way to lose a status update. Refuses anything the wrapper would refuse,
    so the worker never spends a round trip learning it."""
    validated = validate_progress(payload)
    raw = json.dumps(validated, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    refusal = progress_payload_refusal(raw)
    if refusal is not None:
        raise ControlError(f"offload heartbeat: {refusal}")
    return raw


def read_progress(root: Path | str, worker_id: str, job_id: str) -> dict[str, Any] | None:
    """One progress file, or ``None``. An unparseable file comes back as
    ``{"unreadable": ...}`` rather than as nothing: the wrapper writes the
    bytes it was handed (it has no parser), so "the worker sent something we
    cannot read" is a real state and the card has to be able to say so."""
    path = protocol.progress_path(root, worker_id, job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"unreadable": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"unreadable": "progress file is not a JSON object"}
    try:
        # FIX V-5: the reader validates. This file is written by the restricted
        # key through a wrapper with no JSON parser, so "it is an object, it is
        # under the cap and its keys are known" is everything the writing side
        # could check -- and a value it could not check (a nested object where
        # the card expects a word, `Infinity` where it expects a number) used to
        # travel all the way into the dashboard's serialiser and take the whole
        # console bundle out of JSON.
        return validate_progress(data, strict=False)
    except ControlError as exc:
        return {"unreadable": f"{type(exc).__name__}: {exc}"}


def worker_rows(
    root: Path | str,
    *,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ttl_s: float | None = None,
) -> list[dict[str, Any]]:
    """One row per worker the queue has heard from, newest report first
    within each worker (D4).

    A worker is "heard from" when ``claimed/<worker>/`` holds at least one
    progress file. The row reports the NEWEST one -- a worker runs one job at
    a time, so an older file is the previous job's leftover (publish and
    return delete them; a crash between the two can leave one behind) and
    reporting it would age the worker's whole row to that leftover.

    One exception, and it is the interesting one (FIX V-9): a file whose job is
    still CLAIMED outranks a newer one whose job is not. Every run ends with a
    beat under the idle id, so that beat is always the newest file -- and on the
    "stopped, but the claim could not be handed back" path it used to hide the
    very claim somebody has to deal with."""
    root = Path(root)
    claimed = protocol.claimed_dir(root)
    rows: list[dict[str, Any]] = []
    if not claimed.is_dir():
        return rows
    for worker in sorted(claimed.iterdir()):
        if not worker.is_dir():
            continue
        row = worker_row(root, worker.name, heartbeat_interval_s=heartbeat_interval_s, ttl_s=ttl_s)
        if row is not None:
            rows.append(row)
    return rows


def worker_row(
    root: Path | str,
    worker_id: str,
    *,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ttl_s: float | None = None,
) -> dict[str, Any] | None:
    """The dashboard's and ``worker-status``'s reading of one worker, or
    ``None`` when that worker has never reported.

    ``worker_id`` is the claim DIRECTORY's name and ``reported_worker_id`` is
    what the payload called itself; ``worker_id_mismatch`` says when the two
    disagree, which the doctor warns about rather than silently preferring one
    (FIX V-7).

    ``heartbeat_age_s`` is computed AT READ TIME from the job's own
    ``.heartbeat`` stamp (the real beat), falling back to the progress file's
    mtime when the stamp is gone -- the stamp is what the verb writes, so it
    is the honest source, and the mtime is what is left when a claim ended
    between two reads. ``lost`` is that age past :func:`lost_after_s`."""
    root = Path(root)
    worker_dir = protocol.claimed_dir(root) / worker_id
    if not worker_dir.is_dir():
        return None
    newest: tuple[float, str] | None = None
    newest_claimed: tuple[float, str] | None = None
    for path in sorted(worker_dir.glob(f"*{protocol.PROGRESS_SUFFIX}")):
        job_id = path.name[: -len(protocol.PROGRESS_SUFFIX)]
        try:
            mtime = path.stat().st_mtime
        except OSError:  # pragma: no cover - stat failure is unreachable in practice
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, job_id)
        # FIX V-9: a progress file whose MANIFEST is still in this claim
        # directory outranks a newer one whose job has ended. Every run's last
        # beat goes out under the idle id, so that beat is always the newest
        # file -- and on the "stopped, but the claim could not be handed back"
        # path it used to REPLACE the held job's row, hiding the claim somebody
        # still has to reclaim and delaying worker_heartbeat_stale's warn by the
        # whole lost window.
        if (worker_dir / f"{job_id}.json").is_file() and (
            newest_claimed is None or mtime > newest_claimed[0]
        ):
            newest_claimed = (mtime, job_id)
    picked = newest_claimed or newest
    if picked is None:
        return None
    _mtime, job_id = picked
    payload = read_progress(root, worker_id, job_id) or {}
    stamp, age = _heartbeat_age_s(worker_dir, job_id)
    control = read_control(root, worker_id, ttl_s=ttl_s)
    pending = None
    if control is not None and not control["stale"]:
        pending = control["request"]
    settings = payload.get("settings")
    reported = payload.get("worker_id")
    reported = reported if isinstance(reported, str) and reported else None
    row = {
        # FIX V-7: the DIRECTORY name, always. It used to be
        # `payload["worker_id"] or worker_id` -- the payload's own claim about
        # who it is -- which the wrapper never checks, so a worker that reported
        # a different id silenced its own `worker_heartbeat_stale` warning (the
        # check matches row ids against `list_claims`' directory names) and
        # turned its live control request into an `offload_control_orphaned`
        # warn. A defect-detector the watched thing can switch off is not one.
        "worker_id": worker_id,
        "reported_worker_id": reported,
        "worker_id_mismatch": bool(reported is not None and reported != worker_id),
        "state": payload.get("state"),
        "kind": payload.get("kind"),
        "job_id": payload.get("job_id") if payload.get("job_id") else _real_job_id(job_id, worker_id),
        "units_done": payload.get("units_done"),
        "units_total": payload.get("units_total"),
        "unit": payload.get("unit"),
        "started_ts": payload.get("started_ts"),
        "pace_s_per_unit": payload.get("pace_s_per_unit"),
        "eta_s": payload.get("eta_s"),
        "settings": settings if isinstance(settings, dict) else {},
        "control_seen": payload.get("control_seen"),
        "last_error": payload.get("last_error"),
        "heartbeat_ts": stamp,
        "heartbeat_age_s": age,
        "lost": age is None or age > lost_after_s(heartbeat_interval_s),
        "lost_after_s": lost_after_s(heartbeat_interval_s),
        "pending_control": pending,
        "control": control,
        "progress_job_id": job_id,
    }
    if "unreadable" in payload:
        row["unreadable"] = payload["unreadable"]
        row["state"] = row["state"] or "unknown"
    return row


def _real_job_id(progress_job_id: str, worker_id: str) -> str | None:
    """``None`` for the idle slot's synthetic id -- the card must not print
    ``WORKER-<id>`` in a column that means "the job this worker is running"."""
    return None if progress_job_id == protocol.idle_job_id(worker_id) else progress_job_id


def _heartbeat_age_s(worker_dir: Path, job_id: str) -> tuple[str | None, float | None]:
    """``(stamp, age_s)`` for this worker's newest beat.

    The stamp travels as well as the age because an AGE cannot answer "did this
    beat happen after that request was written" -- two ages are measured against
    two different readings of the clock. The stamp is absolute (FIX V-2's
    :func:`control_was_read` compares it against the request's ``ts``)."""
    stamp_path = worker_dir / f"{job_id}{protocol.HEARTBEAT_SUFFIX}"
    if stamp_path.is_file():
        try:
            stamp = stamp_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            stamp = None
        age = _age_s(stamp)
        if age is not None:
            return stamp, age
    import datetime as _dt

    for path in (stamp_path, worker_dir / f"{job_id}{protocol.PROGRESS_SUFFIX}"):
        if path.is_file():
            try:
                return None, max(0.0, _dt.datetime.now().timestamp() - path.stat().st_mtime)
            except OSError:  # pragma: no cover - stat failure is unreachable in practice
                continue
    return None, None


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return (parse(now()) - parse(ts)).total_seconds()
    except Exception:  # noqa: BLE001 - an unparseable stamp is "unknown age", never a crash
        return None
