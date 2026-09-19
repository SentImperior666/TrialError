"""The offload half of ``trialerror jobs retry`` (lane FB-8a).

One function, :func:`retry_offload_entry`, and the whole of its reason for
existing is the ORDER it imposes:

    queue first, store second.

A retry has two halves that cannot be one transaction -- a directory tree
and a SQLite row -- so one of them is going to be able to land without the
other, and the only question is which failure an operator would rather
find. Queue-then-store leaves, at worst, a marker sitting in ``pending/``
for a ledger row that is still ``failed``: the GPU worker may claim it and
publish a result, the stage will find that result the next time the row is
claimable, and a second ``jobs retry`` completes the store half and says
so. Store-then-queue would leave the opposite -- a ``pending`` ledger row
whose marker is still terminal -- and that one is not recoverable by
repeating the command, because the ledger row no longer looks like anything
that needs retrying. It just re-fails against ``failed/<job_id>/``, three
times, and abandons.

So the queue move is first, it is idempotent, and it is DETECTABLE
(:func:`trialerror.offload.protocol.queue_half_retried`).

This module is the SEMANTICS layer for that move, the same split every
other file in this subsystem follows:
:mod:`trialerror.offload.protocol` owns the layout (what is written where),
:mod:`trialerror.offload.stage` owns the payloads (what an ``ocr``/``embed``
job's inputs are), and this module decides what a retry of one job means --
which warnings the operator is owed, and what is deliberately left alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trialerror.offload import protocol

__all__ = ["OFFLOAD_STATE_NONE", "offload_entry_state", "retry_offload_entry"]

#: What :func:`offload_entry_state` reports for a job this queue has never
#: heard of -- the ordinary case for every non-offloaded job in the ledger.
OFFLOAD_STATE_NONE = "none"


def _nameable(job_id: str) -> bool:
    """Whether the offload queue could ever have a directory for this job.

    The ledger puts no constraint on ``job.job_id``; the offload queue does
    (:func:`protocol.validate_job_id`, because the id becomes a path
    component on two machines). An id the queue cannot name therefore has NO
    offload entry, by construction -- and saying that is very different from
    refusing to retry the ledger row.

    PROBE (a), second finding: this used to be handled only inside
    :func:`offload_entry_state`, while the ``parked_largeformat`` lookup
    validated the id on its own path. So the moment a program grew a
    ``parked_largeformat/`` directory, every ledger job whose id the offload
    rule dislikes became unretryable -- refused by a queue it has nothing to
    do with. One guard now, in front of every queue read."""
    try:
        protocol.validate_job_id(job_id)
    except protocol.OffloadProtocolError:
        return False
    return True


def offload_entry_state(root: Path | str, job_id: str) -> str:
    """Where this job's marker currently is, as one word.

    ``retried`` (the queue half of a retry is already complete),
    ``failed``/``pending``/``claimed``/``done`` (:func:`protocol.find_manifest`'s
    own vocabulary), ``archived`` (only the kept evidence is left -- an
    interrupted retry), or :data:`OFFLOAD_STATE_NONE`."""
    root = Path(root)
    if not root.is_dir() or not _nameable(job_id):
        return OFFLOAD_STATE_NONE
    if protocol.queue_half_retried(root, job_id) is not None:
        return "retried"
    located = protocol.find_manifest(root, job_id)
    if located is not None:
        return located[0]
    if protocol.retry_source_manifest(root, job_id) is not None:
        return "archived"
    return OFFLOAD_STATE_NONE


def retry_offload_entry(
    store,
    root: Path | str,
    job_id: str,
    *,
    reason: str,
    ts: str | None = None,
    by_launch: str | None = None,
) -> dict[str, Any]:
    """Return this job's offload marker to the queue, or explain why not.

    Returns ``{"moved", "from", "to", "attempts_reset", "state",
    "already_retried", "warnings"}``. ``moved`` is ``False`` -- with no
    warning and no error -- for a job that simply has no offload entry,
    which is what most ledger jobs are.

    Raises :class:`~trialerror.offload.stage.MissingStageInputError` when the
    payload the worker needs is gone from the sandbox, and
    :class:`~trialerror.offload.protocol.OffloadProtocolError` for a marker
    that is not in a state a retry can act on. Both are refusals the CALLER
    turns into an error envelope BEFORE it touches the jobs store -- a retry
    that cannot put the work back must not settle the row as though it had.
    """
    root = Path(root)
    warnings: list[dict[str, str]] = []
    state = offload_entry_state(root, job_id)

    # Named, never touched: what to do with an entry nothing in this harness
    # wrote is the operator's decision (brief item 2).
    parked = (
        protocol.parked_largeformat_entry(root, job_id)
        if root.is_dir() and _nameable(job_id)
        else None
    )
    if parked is not None:
        warnings.append(
            {
                "code": "parked_largeformat_twin",
                "message": (
                    f"a {protocol.PARKED_LARGEFORMAT_DIRNAME}/ entry for this job id is still "
                    f"there ({parked}) and was NOT touched -- this retry puts the queue marker "
                    "back; whether the parked copy should also be released is your call"
                ),
            }
        )

    if state in (OFFLOAD_STATE_NONE, "pending", "done"):
        if state == "pending":
            warnings.append(
                {
                    "code": "offload_already_queued",
                    "message": (
                        "this job's offload marker is already in pending/ -- the ledger row is "
                        "being retried, the queue entry was left exactly as it is"
                    ),
                }
            )
        if state == "done":
            warnings.append(
                {
                    "code": "offload_result_published",
                    "message": (
                        "the GPU worker has already published a result for this job "
                        f"({protocol.done_dir(root) / job_id}) -- the retried ledger row will "
                        "verify and consume it rather than queue fresh work"
                    ),
                }
            )
        return {
            "moved": False,
            "from": None,
            "to": None,
            "attempts_reset": None,
            "state": state,
            "already_retried": False,
            "warnings": warnings,
        }

    if state == "claimed":
        # Not a refusal: the LEDGER row is the thing being retried, and the
        # GPU worker holding the marker is a different machine's business.
        # But an operator who is not told will read `moved: false` as a bug.
        raise protocol.OffloadProtocolError(
            f"offload retry {job_id}: a GPU worker currently holds this marker "
            f"({protocol.claimed_dir(root)}) -- let it finish or run `trialerror offload reclaim`, "
            "then retry"
        )

    if state == "retried":
        result = protocol.retry_marker(root, job_id, reason=reason, inputs=[], ts=ts, by_launch=by_launch)
        warnings.append(
            {
                "code": "offload_half_already_done",
                "message": (
                    "this job's offload marker was already back in the queue with a retried "
                    "stamp -- a previous `jobs retry` was interrupted between its two halves, "
                    "and this call completed the jobs-store half"
                ),
            }
        )
        return {
            "moved": False,
            "from": None,
            "to": None,
            "attempts_reset": result["attempts_reset"],
            "state": state,
            "already_retried": True,
            "warnings": warnings,
        }

    # state in ("failed", "archived"): the real inverse.
    from trialerror.offload.stage import rebuild_inputs

    found = protocol.retry_source_manifest(root, job_id)
    assert found is not None  # offload_entry_state just said so, same process
    manifest = found[0]
    inputs = rebuild_inputs(store, manifest)
    result = protocol.retry_marker(
        root, job_id, reason=reason, inputs=inputs, ts=ts, by_launch=by_launch
    )
    if state == "archived":
        warnings.append(
            {
                "code": "offload_evidence_already_archived",
                "message": (
                    "this job's terminal directory had already been archived under "
                    f"failed/{protocol.RETRIED_DIRNAME}/ -- a previous retry was interrupted "
                    "after the move and before the queue entry; the queue entry was rebuilt "
                    "from the archived manifest and no second evidence directory was made"
                ),
            }
        )
    return {
        "moved": True,
        "from": result["from"],
        "to": result["to"],
        "attempts_reset": result["attempts_reset"],
        "state": state,
        "already_retried": False,
        "warnings": warnings,
    }
