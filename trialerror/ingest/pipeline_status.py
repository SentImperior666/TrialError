"""Where one document actually IS in the pipeline -- the stage chain, one
derived state, and the next thing to do about it.

**The problem (feedback disposition D-FB-8).** ``trialerror ingest status``
reported a document's row and three counts. That answers "what has been
written" and not "what is happening", so the ordinary question -- *this
document has been sitting at zero chunks for an hour, why?* -- had to be
answered by hand out of ``trialerror jobs list``, matching job ids against
a document id by eye, remembering that an environmentally-deferred job
looks ``pending`` and that a paused one looks like neither, and then
guessing whether the GPU worker on the other machine had published
anything.

**The shape of the answer.** ``job`` has no ``doc_id`` column, and this
lane adds no migration, so the link is the one the pipeline already
maintains: the payload's ``doc_id``, with the ``JOB-ingest-<doc>``
id convention as a fallback, and which of the two found each row is
REPORTED rather than assumed (:func:`trialerror.jobs.ledger.list_jobs_for_doc`).
On top of the chain, one derived word -- ``queued`` / ``running`` /
``parked`` / ``failed-will-retry`` / ``failed`` / ``complete`` -- and the
action that word implies.

**Why the derived word cannot be read off ``job.state``.** Four of the six
ledger states mean something different depending on the columns beside
them:

* ``pending`` with ``failure_class = 'environmental'`` and a ``next_attempt_ts``
  STILL IN THE FUTURE is not queued, it is PARKED -- typically waiting for
  a result the GPU machine has not published yet (``trialerror.offload``).
  Once that time passes the same row is queued again, because nothing
  re-claims a job on its own: the comparison is the one the ledger's claim
  predicate makes, not the presence of the column.
* ``pending`` with nothing beside it is genuinely queued: it needs a
  worker, not patience.
* ``failed`` with attempts left is a scheduled retry, not a failure to act
  on; ``failed`` with none left is ``abandoned``'s twin and needs a human --
  and a dead stage is reported ahead of a retryable sibling, because a
  document that needs a human needs one whatever its healthier stages are
  about to do.
* ``paused`` is an operator's own hold, and the only thing that clears it
  is ``trialerror jobs resume``.

And one state cannot be read off the ledger at all: a document that the
normalize stage REFUSED on extraction quality
(``[ingest.quality] refuse_below``) has ``document.status = 'failed'`` with
its job settled ``complete`` -- the job did exactly what it was asked to.
The document's own status therefore outranks every job row here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from trialerror.jobs import ledger
from trialerror.util.timeutil import now as now_ts_utc

__all__ = [
    "PIPELINE_STATES",
    "NOT_SEARCHABLE_UNTIL_RUN",
    "not_yet_searchable_next_actions",
    "job_stage",
    "derive_pipeline_state",
    "document_pipeline",
]

#: Every value ``pipeline_state`` can take. ``failed`` is the terminal one
#: (a quality refusal, a retraction, or a job out of attempts); the other
#: five are all "still moving, or waiting for something nameable".
PIPELINE_STATES: tuple[str, ...] = (
    "queued",
    "running",
    "parked",
    "failed-will-retry",
    "failed",
    "complete",
)

#: ``document.status`` for a document whose pipeline ended badly -- the only
#: value the schema's CHECK allows for that, shared by
#: ``trialerror.ingest.retract`` and the quality refusal.
_FAILED_DOCUMENT_STATUS = "failed"

#: ``document.status`` at the end of a complete pipeline.
_FINAL_DOCUMENT_STATUS = "indexed"


#: Lane FB-1 item F4. The sentence both acquisition surfaces say, in one
#: place so they cannot say it two ways: a registered document is not a
#: searchable one.
NOT_SEARCHABLE_UNTIL_RUN = (
    "run the enqueued pipeline job (the document is not searchable until it completes)"
)


def not_yet_searchable_next_actions(job: Mapping[str, Any] | None, *, next_action=None) -> list:
    """The two next actions a just-registered document earns.

    The first names the job and says what it is for -- "acquired" reads as
    done, and the document is in fact unnormalized, unchunked, unembedded
    and unindexed until a worker runs this. The second is the small-batch
    answer: one document, one job, run it here and watch it, rather than
    arranging for a detached worker to exist. (``--foreground`` with
    ``--job-id`` claims exactly that job -- `jobs start-worker`'s own
    default is detached.)

    ``next_action`` is injected by the caller so this module, which is
    otherwise pure derivation over ledger rows, keeps no dependency on the
    envelope layer.
    """
    if not job or not job.get("job_id"):
        return []
    if next_action is None:
        from trialerror.util.envelope import next_action as next_action_
        next_action = next_action_
    job_id = str(job["job_id"])
    return [
        next_action(
            ["trialerror", "jobs", "start-worker", "--job-id", job_id],
            NOT_SEARCHABLE_UNTIL_RUN,
        ),
        next_action(
            ["trialerror", "jobs", "start-worker", "--foreground", "--job-id", job_id],
            "or run it inline in this shell and watch it (one document, one job)",
        ),
    ]


def job_stage(kind: str, payload: Mapping[str, Any] | str | None) -> str:
    """The logical PIPELINE STAGE a job row belongs to.

    The inverse of :func:`trialerror.ingest.pipeline.stage_job_kind_and_payload`:
    every stage rides its own name as ``job.kind``, except a genuine custom
    stage (``djvu``) which rides ``kind='custom'`` with ``payload['handler']``
    naming it. Written as an inverse of that one function rather than as a
    second list of stage names, so a stage added there needs nothing here.
    """
    if kind != "custom":
        return kind
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = None
    if isinstance(data, Mapping):
        handler = data.get("handler")
        if handler:
            return str(handler)
    return "custom"


def _id(doc_id: str | None) -> str:
    """A document id for a message, or the placeholder a caller that did
    not pass one gets -- never a silent empty string in the middle of a
    command an operator is meant to copy."""
    return doc_id or "<doc-id>"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_due(ts: Any, now: str) -> bool:
    """Has a scheduled ``next_attempt_ts`` passed?

    The SAME comparison the ledger's own claim predicate makes -- a plain
    string comparison against an ISO-8601 UTC millisecond stamp
    (``trialerror.jobs.ledger._ELIGIBLE_PREDICATE``: ``next_attempt_ts IS
    NULL OR next_attempt_ts <= :now``). Written as that comparison rather
    than as a parse-and-subtract so this module cannot come to a different
    answer than the worker about whether a job is claimable, and so a
    malformed stamp degrades to "not due" instead of raising inside
    ``ingest status``.

    A ``NULL`` stamp is claimable in the ledger, and callers here treat it
    separately (there is nothing to report a time for), so it is not "due"
    by this predicate.
    """
    return isinstance(ts, str) and bool(ts) and ts <= now


def derive_pipeline_state(
    doc_status: str | None,
    stages: Sequence[Mapping[str, Any]],
    *,
    doc_id: str | None = None,
    refused: bool = False,
    retracted: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """One word for where this document is, why, and what to do next.

    Pure: takes the document's own status and its stage rows (as
    :func:`trialerror.jobs.ledger.list_jobs_for_doc` returns them, each
    carrying at least ``state``), returns
    ``{pipeline_state, reason, next_action, next_argv, stalled_stage,
    stalled_job_id}``. ``next_action`` is the sentence for a human;
    ``next_argv`` is the same thing as an argv list when there is one exact
    command to run (the envelope's ``next_actions`` entry is built from
    that, never by parsing the prose).

    **Precedence, in order, and every branch is a real ledger shape:**

    1. the DOCUMENT says it failed -- a quality refusal or a retraction
       settles its jobs ``complete``, so no job row can report this;
    2. a stage is ``claimed``/``running`` -- work is happening now;
    3. a stage is ``paused`` (an operator's hold), or ``pending`` with an
       environmental deferral whose time has NOT yet come -- PARKED, waiting
       on something outside this process;
    4. a stage is ``abandoned`` (or ``failed`` out of attempts) -- terminal;
    5. a stage is ``failed`` with attempts left -- a retry is scheduled;
    6. a stage is ``pending`` -- queued, waiting for a worker (including a
       defer whose time has passed: nothing re-claims a job on its own);
    7. nothing is outstanding: ``complete`` if the document reached
       ``indexed``, otherwise ``queued`` with the reason saying no job
       exists for the next stage (including the "no jobs at all" corpus a
       hand-built or migrated program can have).

    **Two orderings that are advice, not ledger semantics** (fix pass V-8).
    A terminal stage outranks a retryable one: a document whose embed stage
    is dead and whose chunk stage will retry needs a human either way, and
    reading ``failed-will-retry`` off the healthier row hides that. And an
    environmental defer is only ``parked`` while its ``next_attempt_ts`` is
    still in the future -- once it has passed, nothing re-claims the job on
    its own, so telling the operator "nothing here" would be telling them
    not to do the one thing that would move it.

    ``now`` (ISO-8601 UTC, :func:`trialerror.util.timeutil.now` by default)
    is injectable so this stays a pure function of its arguments.
    """
    now = now or now_ts_utc()
    if refused:
        return {
            "pipeline_state": "failed",
            "reason": "the normalize stage refused this document on extraction quality ([ingest.quality] refuse_below)",
            "next_action": f"read the numbers (`trialerror ingest quality --doc-id {_id(doc_id)}`), then fix the "
            "extraction route and re-ingest, or relax [ingest.quality] refuse_below if this text is acceptable "
            "after all",
            "next_argv": ["trialerror", "ingest", "quality", "--doc-id", _id(doc_id)],
            "stalled_stage": "normalize",
            "stalled_job_id": None,
        }
    if retracted:
        return {
            "pipeline_state": "failed",
            "reason": "this document was retracted (`trialerror ingest retract`); its derived rows were removed "
            "deliberately",
            "next_action": "nothing -- re-ingest the raw file with `trialerror ingest add` if it belongs in the "
            "corpus again",
            "next_argv": None,
            "stalled_stage": None,
            "stalled_job_id": None,
        }
    if doc_status == _FAILED_DOCUMENT_STATUS:
        return {
            "pipeline_state": "failed",
            "reason": "document.status is 'failed' with no refusal or retraction record to explain it",
            "next_action": "read this document's job history (`trialerror jobs logs --job-id ...`)",
            "next_argv": None,
            "stalled_stage": None,
            "stalled_job_id": None,
        }

    def _first(predicate):
        for row in stages:
            if predicate(row):
                return row
        return None

    running = _first(lambda r: r.get("state") in ("claimed", "running"))
    if running is not None:
        stage = job_stage(running.get("kind"), running.get("payload"))
        worker = running.get("claimed_by") or "a worker"
        return {
            "pipeline_state": "running",
            "reason": f"the {stage} stage is running ({worker})",
            "next_action": f"wait -- `trialerror jobs logs --job-id {running['job_id']}` shows its progress",
            "next_argv": ["trialerror", "jobs", "logs", "--job-id", running["job_id"]],
            "stalled_stage": stage,
            "stalled_job_id": running["job_id"],
        }

    paused = _first(lambda r: r.get("state") == "paused")
    if paused is not None:
        stage = job_stage(paused.get("kind"), paused.get("payload"))
        return {
            "pipeline_state": "parked",
            "reason": f"the {stage} stage is paused (an operator's hold, not a failure)",
            "next_action": f"`trialerror jobs resume --job-id {paused['job_id']}`",
            "next_argv": ["trialerror", "jobs", "resume", "--job-id", paused["job_id"]],
            "stalled_stage": stage,
            "stalled_job_id": paused["job_id"],
        }

    deferred = _first(
        lambda r: r.get("state") == "pending"
        and r.get("failure_class") == "environmental"
        and r.get("next_attempt_ts")
        and not _is_due(r.get("next_attempt_ts"), now)
    )
    if deferred is not None:
        stage = job_stage(deferred.get("kind"), deferred.get("payload"))
        return {
            "pipeline_state": "parked",
            "reason": f"the {stage} stage deferred on an environmental failure and is waiting until "
            f"{deferred['next_attempt_ts']} (attempts are not consumed by these)"
            + (f": {deferred['last_error']}" if deferred.get("last_error") else ""),
            "next_action": "nothing here -- an environmental defer clears when the condition does (for an "
            "offloaded stage, when the GPU worker publishes its result)",
            "next_argv": None,
            "stalled_stage": stage,
            "stalled_job_id": deferred["job_id"],
        }

    dead = _first(
        lambda r: r.get("state") == "abandoned"
        or (r.get("state") == "failed" and _int(r.get("attempts")) >= _int(r.get("max_attempts")))
    )
    if dead is not None:
        stage = job_stage(dead.get("kind"), dead.get("payload"))
        return {
            "pipeline_state": "failed",
            "reason": f"the {stage} stage is out of attempts ({_int(dead.get('attempts'))} of "
            f"{_int(dead.get('max_attempts'))})"
            + (f": {dead['last_error']}" if dead.get("last_error") else ""),
            "next_action": f"read the cause (`trialerror jobs logs --job-id {dead['job_id']}`), fix it, then "
            "re-enqueue the stage (`trialerror ingest rechunk`/`re-embed`) or re-ingest the document",
            "next_argv": ["trialerror", "jobs", "logs", "--job-id", dead["job_id"]],
            "stalled_stage": stage,
            "stalled_job_id": dead["job_id"],
        }

    retrying = _first(
        lambda r: r.get("state") == "failed" and _int(r.get("attempts")) < _int(r.get("max_attempts"))
    )
    if retrying is not None:
        stage = job_stage(retrying.get("kind"), retrying.get("payload"))
        when = retrying.get("next_attempt_ts")
        if not when:
            # A logic failure with budget left and no backoff recorded is
            # claimable right now by the ledger's own predicate -- "scheduled
            # to retry at None" was the old rendering of exactly this row.
            schedule = "and is claimable again now (no backoff recorded)"
            action = f"`trialerror jobs start-worker`, or read the cause first: `trialerror jobs logs --job-id {retrying['job_id']}`"
        elif _is_due(when, now):
            schedule = f"and its retry time ({when}) has passed -- it is claimable now"
            action = f"`trialerror jobs start-worker`, or read the cause first: `trialerror jobs logs --job-id {retrying['job_id']}`"
        else:
            schedule = f"and is scheduled to retry at {when}"
            action = (
                f"`trialerror jobs start-worker` once the retry is due, or read the cause first: "
                f"`trialerror jobs logs --job-id {retrying['job_id']}`"
            )
        return {
            "pipeline_state": "failed-will-retry",
            "reason": f"the {stage} stage failed {_int(retrying.get('attempts'))} of "
            f"{_int(retrying.get('max_attempts'))} attempts {schedule}"
            + (f": {retrying['last_error']}" if retrying.get("last_error") else ""),
            "next_action": action,
            "next_argv": ["trialerror", "jobs", "logs", "--job-id", retrying["job_id"]],
            "stalled_stage": stage,
            "stalled_job_id": retrying["job_id"],
        }

    pending = _first(lambda r: r.get("state") == "pending")
    if pending is not None:
        stage = job_stage(pending.get("kind"), pending.get("payload"))
        elapsed_defer = pending.get("failure_class") == "environmental" and _is_due(
            pending.get("next_attempt_ts"), now
        )
        return {
            "pipeline_state": "queued",
            "reason": (
                f"the {stage} stage deferred on an environmental failure and its wait ended at "
                f"{pending['next_attempt_ts']} -- it is claimable now and nothing has claimed it"
                if elapsed_defer
                else f"the {stage} stage is enqueued and waiting for a worker"
            )
            + (f": {pending['last_error']}" if elapsed_defer and pending.get("last_error") else ""),
            "next_action": "`trialerror jobs start-worker` (nothing claims a job on its own)",
            "next_argv": ["trialerror", "jobs", "start-worker"],
            "stalled_stage": stage,
            "stalled_job_id": pending["job_id"],
        }

    if doc_status == _FINAL_DOCUMENT_STATUS:
        return {
            "pipeline_state": "complete",
            "reason": "every stage settled and the document reached 'indexed'",
            "next_action": None,
            "next_argv": None,
            "stalled_stage": None,
            "stalled_job_id": None,
        }

    # Nothing outstanding, and the document never reached the end of the
    # chain: either its last stage settled without enqueueing the next one
    # (a hand-run stage, a program migrated in) or it has no jobs at all.
    # "queued" is the honest word -- something is waiting to be run -- and
    # the reason says the part that would otherwise mislead: there is no
    # job row to wait for.
    return {
        "pipeline_state": "queued",
        "reason": (
            f"no job is outstanding and document.status is {doc_status!r} -- nothing is enqueued for the "
            "next stage"
            if stages
            else f"this document has no job rows at all and document.status is {doc_status!r}"
        ),
        "next_action": f"enqueue the next stage yourself: `trialerror ingest rechunk --doc-id {_id(doc_id)}` "
        f"(chunk) or `trialerror ingest re-embed --doc-id {_id(doc_id)}` (embed), or re-ingest the raw file",
        "next_argv": None,
        "stalled_stage": None,
        "stalled_job_id": None,
    }


def _offload_annotation(program_root: Path | None, job_ids: Sequence[str]) -> dict[str, Any] | None:
    """Where each job's offload manifest currently sits, or ``None`` when
    this program has no offload queue at all.

    **Degrades quietly, by design.** A single-machine program has no
    ``offload/`` directory and must not see a word about one; a program
    that HAS one but whose manifest tree is unreadable gets the key with
    the error named rather than a traceback out of ``ingest status``.
    """
    if program_root is None:
        return None
    from trialerror.offload import protocol

    root = protocol.offload_root(program_root)
    if not root.is_dir():
        return None

    out: dict[str, Any] = {"queue_dir": protocol.OFFLOAD_DIRNAME, "jobs": {}}
    for job_id in job_ids:
        try:
            found = protocol.find_manifest(root, job_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable queue is an annotation, not a failure
            out["jobs"][job_id] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        if found is None:
            continue
        state, path = found
        try:
            where = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - find_manifest only returns paths under root
            where = path.name
        out["jobs"][job_id] = {"queue_state": state, "manifest": f"{protocol.OFFLOAD_DIRNAME}/{where}"}
    return out


def document_pipeline(store, doc_id: str, *, document: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The stage chain, the derived state and the next action for one
    document -- the ``pipeline`` key of ``trialerror ingest status``.

    Read-only. ``document`` is the already-loaded row when the caller has
    one (``ingest status`` does), so this costs one jobs-db query plus, on
    a program that offloads, one stat per job.
    """
    from trialerror.ingest.quality import quality_refusal_record
    from trialerror.ingest.retract import retraction_record

    if document is None:
        row = store.knowledge.execute(
            "SELECT doc_id, status FROM document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        document = dict(row) if row is not None else {}

    jobs = ledger.list_jobs_for_doc(store, doc_id)
    refusal = quality_refusal_record(store.knowledge, doc_id)
    retraction = retraction_record(store.knowledge, doc_id)

    stages = [
        {
            "job_id": j["job_id"],
            "stage": job_stage(j.get("kind"), j.get("payload")),
            "kind": j.get("kind"),
            "state": j.get("state"),
            "attempts": j.get("attempts"),
            "max_attempts": j.get("max_attempts"),
            "failure_class": j.get("failure_class"),
            "last_error": j.get("last_error"),
            "next_attempt_ts": j.get("next_attempt_ts"),
            "claimed_by": j.get("claimed_by"),
            "created_ts": j.get("created_ts"),
            "settled_ts": j.get("settled_ts"),
            "match_kind": j.get("match_kind"),
        }
        for j in jobs
    ]

    # Fix pass V-3: the refusal RECORD is history and is never retired --
    # it is the evidence of what was refused and stays readable in the
    # envelope forever. What it is not is a current state: a document whose
    # text was measured again (relax `[ingest.quality] refuse_below`, re-run
    # the normalize stage) is written back to 'normalized' and drains
    # normally, and reporting it `failed` on the strength of an old record
    # would make this envelope contradict its own `document.status` and its
    # own job rows. The pair -- record AND status still 'failed' -- is the
    # same condition `trialerror.ingest.pipeline.requeue_stage` guards on.
    refused = refusal is not None and document.get("status") == _FAILED_DOCUMENT_STATUS
    derived = derive_pipeline_state(
        document.get("status"),
        [dict(s, payload=j.get("payload")) for s, j in zip(stages, jobs)],
        doc_id=doc_id,
        refused=refused,
        retracted=retraction is not None,
    )

    match_kinds = sorted({s["match_kind"] for s in stages if s.get("match_kind")})
    offload = _offload_annotation(getattr(store, "program_root", None), [s["job_id"] for s in stages])
    if offload is not None:
        for stage in stages:
            annotation = offload["jobs"].get(stage["job_id"])
            if annotation is not None:
                stage["offload"] = annotation

    return {
        "stages": stages,
        "match_kind": "+".join(match_kinds) if match_kinds else "none",
        "offload": offload,
        "quality_refusal": refusal,
        **derived,
    }
