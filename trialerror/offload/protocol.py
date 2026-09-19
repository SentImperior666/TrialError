"""The offload queue: on-disk layout, manifests, and the verb semantics.

Design section 4, verbatim layout::

    <program_root>/offload/
      pending/<job_id>.json          manifest (offload_attempts inside)
      pending/<job_id>/              inputs: the raw file (ocr) or
                                     chunks.jsonl (embed: ALL chunks of the doc)
      claimed/<worker_id>/<job_id>.json + <job_id>.heartbeat + <job_id>/
      claimed/<worker_id>/<job_id>.progress.json    what the worker is doing
      claimed/<worker_id>/CONTROL.json              pause/resume/stop request
      done/<job_id>/                 result.json + payload files + manifest.json
                                     (published by atomic rename from
                                      done/.partial/<job_id>/)
      failed/<job_id>/               error.json (terminal after max_attempts)

The two new names are worker CONTROL and worker OBSERVABILITY (ruling
C-0097, ``docs/reviews/WORKER_CONTROL_DESIGN.md`` D1/D3). They ride the
heartbeat verb rather than adding one: the sandbox side writes
``CONTROL.json``, the ``heartbeat`` verb prints the current control word on
stdout after writing the stamp, and the optional payload a worker sends on
that verb's stdin is stored as ``<job_id>.progress.json`` beside the stamp.
The SEMANTICS live in :mod:`trialerror.offload.control`; this module owns
the LAYOUT (the names, the paths, and the one verb that writes them) -- the
same split every other file in this queue follows.

**SANDBOX owns the truth; DEV is a stateless worker. Nothing on SANDBOX
ever calls DEV.** Every mutation here is a ``rename`` (atomic on both
POSIX and Windows within one filesystem) so a torn state is never
observable and a lost race is an ordinary, expected outcome rather than an
error: two workers that both try to claim one job produce one winner and
one ``FileNotFoundError``, exactly the way
``trialerror.jobs.ledger.claim_specific``'s conditional UPDATE produces one
winner and one ``None``.

**Crash windows are closed by ordering, not by locking.** Each verb does
its steps in the order that leaves any interrupted state recoverable by a
LATER verb rather than stranded:

- ``claim``   manifest first (that rename IS the ownership transfer), then
  the inputs directory. Interrupted between the two: the manifest sits in
  ``claimed/`` with the inputs still in ``pending/`` -- :func:`reclaim_stale`
  puts the manifest back after the expiry and tolerates the inputs already
  being there.
- ``return``  inputs first, then the manifest. Interrupted: the inputs are
  back in ``pending/`` while the manifest is still claimed -- again the
  reclaim path converges.
- ``publish`` the manifest is moved INTO the staging directory first, the
  claim is cleaned second, and the single ``rename(done/.partial/<id>,
  done/<id>)`` is last, so ``done/<id>/`` never appears without its
  manifest. Interrupted before that final rename, the staging directory is
  an orphan carrying its own manifest -- :func:`adopt_orphaned_partials`
  (run by ``trialerror offload kick``) finishes the publish.

The verbs below are the SERVER side (they run where the queue lives). The
DEV worker never touches these functions: it speaks the same seven verbs
over SSH to ``deploy/sandbox/offload-shell.sh``, which implements exactly
these semantics in POSIX shell because it must run on the queue host
outside the container, with no Python and no harness install. That
duplication is deliberate and bounded: the shell wrapper IS the security
of the restricted key, and ``tests/test_offload_shell.py`` proves the two
halves share one refusal table.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from trialerror.util.atomic import atomic_write_bytes, atomic_write_text
from trialerror.util.timeutil import now, parse

__all__ = [
    "OFFLOAD_DIRNAME",
    "MANIFEST_SCHEMA",
    "RESULT_SCHEMA",
    "ERROR_SCHEMA",
    "MANIFEST_FILENAME",
    "RETRY_NOTE_FILENAME",
    "RETRY_NOTE_SCHEMA",
    "QUEUED_DIRNAME",
    "REJECTED_DIRNAME",
    "RETRIED_DIRNAME",
    "PARKED_LARGEFORMAT_DIRNAME",
    "MAX_PAYLOAD_BYTES",
    "RESULT_FILENAME",
    "ERROR_FILENAME",
    "HEARTBEAT_SUFFIX",
    "PROGRESS_SUFFIX",
    "CONTROL_FILENAME",
    "CONTROL_SCHEMA",
    "RANGE_CACHE_DIRNAME",
    "IDLE_JOB_PREFIX",
    "idle_job_id",
    "control_path",
    "progress_path",
    "PARTIAL_DIRNAME",
    "DEFAULT_CLAIM_EXPIRY_S",
    "DEFAULT_MAX_OFFLOAD_ATTEMPTS",
    "BACKLOG_WARN_S",
    "JOB_ID_RE",
    "OffloadError",
    "OffloadProtocolError",
    "OffloadVerificationError",
    "validate_job_id",
    "offload_root",
    "ensure_layout",
    "pending_dir",
    "claimed_dir",
    "done_dir",
    "failed_dir",
    "partial_dir",
    "queued_dir",
    "rejected_dir",
    "retried_dir",
    "parked_largeformat_dir",
    "parked_largeformat_entry",
    "read_json",
    "write_json",
    "sha256_bytes",
    "queue_marker",
    "find_manifest",
    "queued_manifest",
    "forget_queued",
    "trusted_manifest",
    "published_result",
    "published_error",
    "verify_published",
    "fail_marker",
    "requeue_marker",
    "retry_marker",
    "retry_source_manifest",
    "queue_half_retried",
    "discard_published",
    "list_pending",
    "list_claims",
    "list_done",
    "list_failed",
    "list_retried",
    "counts",
    "oldest_pending_age_s",
    "reclaim_stale",
    "adopt_orphaned_partials",
    "rejected_partials",
    "pack_dir",
    "unpack_into",
    "check_payload_size",
    "server_list",
    "server_claim",
    "server_pull",
    "server_push",
    "server_publish",
    "server_return",
    "server_heartbeat",
    "server_control_word",
]

OFFLOAD_DIRNAME = "offload"
PARTIAL_DIRNAME = ".partial"

#: The sandbox's OWN record of every job it queued, one manifest per job.
#: SEC-1: nothing the DEV key can say reaches this directory -- none of the
#: seven verbs writes here and ``offload-shell.sh`` does not even create it
#: -- so it is the only manifest copy in the tree that a holder of the
#: restricted key cannot author. Adoption of an interrupted publish is
#: gated on it (:func:`adopt_orphaned_partials`) and the sandbox verifies
#: published results against it (:func:`trusted_manifest`).
QUEUED_DIRNAME = "queued"

#: Where a staging directory that failed the adoption gate is parked, so
#: the refusal is inspectable instead of silently re-examined every five
#: minutes by the next ``trialerror offload kick``.
REJECTED_DIRNAME = "rejected"

#: Lane FB-8a: where ``failed/<job_id>/`` goes when an operator retries the
#: job (``trialerror jobs retry``). It lives UNDER ``failed/`` on purpose --
#: the evidence of a terminal attempt belongs beside the terminal attempts --
#: and it is never deleted, only added to. :func:`list_failed` skips it, so
#: ``offload status`` and the ``offload_failed`` doctor check count a retried
#: job as retried rather than as a failure that is still outstanding.
#:
#: Reserved as a job id for the same reason :data:`RANGE_CACHE_DIRNAME` is: a
#: job called ``_retried`` would make ``failed/_retried`` its own terminal
#: directory, and :func:`fail_marker`'s ``_rmtree(dest)`` would then delete
#: every other job's kept evidence on that job's first DEV failure.
RETRIED_DIRNAME = "_retried"

#: A queue directory this module READS and never writes: a deployment may
#: park an oversized job here (a scan too large for one GPU pass) outside the
#: seven-verb contract. ``jobs retry`` names a twin found here in its
#: envelope's warnings and leaves it exactly where it is -- what to do with
#: an entry nothing in this harness created is the operator's call, not a
#: side effect of retrying the ledger row.
PARKED_LARGEFORMAT_DIRNAME = "parked_largeformat"
MANIFEST_FILENAME = "manifest.json"
RESULT_FILENAME = "result.json"
ERROR_FILENAME = "error.json"

#: What ``jobs retry`` writes INTO the evidence directory it keeps, beside
#: the untouched ``manifest.json``/``error.json`` pair: who retried this
#: marker, when, why, and the sha256 of the ``error.json`` sitting next to
#: it. Same role (and same reason) as ``rejected.json`` under ``rejected/``.
RETRY_NOTE_FILENAME = "retried.json"
RETRY_NOTE_SCHEMA = "trialerror.offload.retried/1"
HEARTBEAT_SUFFIX = ".heartbeat"

#: C-0097 D3: what a worker says it is doing, written by the ``heartbeat``
#: verb from an optional stdin payload and read by the dashboard, the
#: ``worker-status`` verb and two doctor checks. One file per job, plus one
#: under :func:`idle_job_id` for a worker holding no claim.
#:
#: The suffix ENDS in ``.json`` on purpose (the design names the file), which
#: is why every reader of ``claimed/<worker>/*.json`` has to skip it --
#: :func:`list_claims` and :func:`reclaim_stale` do, through
#: :func:`_is_manifest`. A job id that itself ended in ``.progress`` would be
#: invisible to those two readers; nothing mints such an id
#: (:func:`queue_marker` ids are ``JOB-<stage>-<doc>``), and the alternative
#: -- a suffix that is not ``.json`` -- would make the file unreadable by
#: every JSON tool an operator reaches for on the host.
PROGRESS_SUFFIX = ".progress.json"

#: C-0097 D1: one pause/resume/stop request per worker, written ONLY by the
#: sandbox side (``trialerror offload worker-control`` or the dashboard's
#: write action) and only ever READ by the worker, through the control word
#: the ``heartbeat`` verb prints. The restricted key cannot write it: no verb
#: of the seven-verb contract touches this name.
CONTROL_FILENAME = "CONTROL.json"
CONTROL_SCHEMA = "trialerror.offload.control/1"

#: Lane e1e Part B: the directory under a worker's ``work_root`` that holds
#: chunked OCR jobs' finished page ranges, one subdirectory per job id
#: (``trialerror.offload.worker.OCR_RANGE_CACHE_DIRNAME`` is this name).
#: Reserved as a job id for the same reason :data:`CONTROL_FILENAME` is
#: (FIX V-3): a job by this name would make ``<work_root>/_ranges`` its own
#: job directory, which ``_process_one`` wipes at every claim -- taking every
#: other job's resume cache with it -- and which ``_retry_publishes`` skips by
#: name, stranding its published result forever. Nothing mints such an id;
#: one refusal is cheaper than trusting that.
#:
#: Unlike ``CONTROL``/``*.progress`` this one is refused on the SANDBOX side
#: only: it is a fact about a worker's own work root, not about the claim
#: directory both halves share, and the queue-host wrapper (and its port in
#: :mod:`trialerror.offload.shell`) is deliberately left unchanged -- every
#: id reaching it was minted here by :func:`queue_marker`, which now cannot
#: mint this one.
RANGE_CACHE_DIRNAME = "_ranges"

#: The synthetic job id a worker heartbeats under while it holds no claim, so
#: the dashboard can tell "idle worker" from "no worker" (D3). ``heartbeat``
#: accepts it WITHOUT a claim -- the one exception to ``claimed_or_die``, and
#: the reason it is a fixed prefix plus the worker's own id rather than
#: anything a client chooses.
IDLE_JOB_PREFIX = "WORKER-"

MANIFEST_SCHEMA = "trialerror.offload.manifest/1"
RESULT_SCHEMA = "trialerror.offload.result/1"
ERROR_SCHEMA = "trialerror.offload.error/1"

#: design section 4: "``trialerror offload reclaim`` returns claims with
#: heartbeats older than 60 min to ``pending/``".
DEFAULT_CLAIM_EXPIRY_S = 60 * 60

#: How many DEV-side attempts a single offloaded stage gets before its
#: marker becomes terminal (``failed/``). Distinct from the ledger's own
#: ``max_attempts``: a GPU that fails deterministically must stop consuming
#: DEV time, and the ledger row then abandons through its own three logic
#: failures once the marker is terminal.
DEFAULT_MAX_OFFLOAD_ATTEMPTS = 3

#: design section 4 doctor row: ``offload_backlog`` warns past 24 h.
BACKLOG_WARN_S = 24 * 60 * 60

#: SEC-4: the hard ceiling on a single ``push``/``pull`` payload, in bytes.
#: Neither side streams to disk incrementally, and the queue lives on the
#: same filesystem as the record, so an unbounded archive from the remote
#: key is a disk-exhaustion lever. 2 GB is far above any real page image or
#: chunk batch and far below "fills the volume". The identical default sits
#: in ``offload-shell.sh`` as ``TE_OFFLOAD_MAX_PUSH_BYTES`` /
#: ``TE_OFFLOAD_MAX_PULL_BYTES``; both sides FAIL the verb rather than
#: truncate, because a truncated archive is a corrupted result.
MAX_PAYLOAD_BYTES = 2_000_000_000

#: design section 4: "``job_id`` is validated against ``^[A-Za-z0-9._-]+$``
#: before use as a path component". That class still admits ``.`` and
#: ``..`` -- :func:`validate_job_id` refuses those separately, and
#: ``offload-shell.sh`` carries the identical pair of rules.
#:
#: SEC-6: anchored with ``\Z``, not ``$``. Python's ``$`` also matches
#: immediately BEFORE a trailing newline, so a job id ending in a
#: newline used to pass -- and "abc" plus a newline is a different
#: path component from "abc" on POSIX. The shell wrapper's ``case`` glob
#: has no such escape hatch, so ``$`` here was the looser of the two halves of a rule
#: that is supposed to be identical on both sides.
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")


class OffloadError(Exception):
    """Base class for every offload-queue error."""


class OffloadProtocolError(OffloadError):
    """A malformed request: a bad job id, a verb applied to a job that is
    not in the state that verb needs, a claim that is not ours."""


class OffloadVerificationError(OffloadError):
    """A published result did not match what the sandbox asked for (payload
    sha, config hash, or the manifest's ``expect`` block). Design section 4
    step 1: "Mismatch -> logic failure (attempt burned, visible), result
    moved to ``failed/``"."""


# ---------------------------------------------------------------------------
# ids and paths
# ---------------------------------------------------------------------------
def validate_job_id(job_id: str) -> str:
    """Return ``job_id`` if it is safe to use as a single path component,
    else raise :class:`OffloadProtocolError`. Refuses the empty string,
    anything outside ``[A-Za-z0-9._-]`` (so no separators, no NUL, no
    whitespace, no shell metacharacters), the two traversal names
    ``.``/``..`` the character class itself would otherwise allow, and the
    names the claim directory and the worker's work root RESERVE (FIX V-11,
    FIX V-3).

    The reserved names are ``CONTROL``, anything ending in ``.progress``,
    :data:`RANGE_CACHE_DIRNAME` and :data:`RETRIED_DIRNAME`.
    ``claimed/<worker>/`` holds ``CONTROL.json``
    and ``<job>.progress.json`` beside the manifests, so a job by either of
    those names would have :func:`find_manifest` return a status file as its
    manifest; ``<work_root>/_ranges/`` holds every chunked OCR job's resume
    cache, so a job by THAT name would have its own claim-time wipe delete
    every other job's unpublished GPU hours; ``failed/_retried/`` holds the
    kept evidence of every retried terminal attempt, so a job by THAT name
    would have :func:`fail_marker`'s ``_rmtree(dest)`` destroy the whole
    archive the moment it first failed on the GPU (probe b, lane FB-8a).
    Nothing mints such an id --
    :func:`queue_marker` ids are ``JOB-<stage>-<doc>`` -- and one refusal is
    cheaper than trusting that."""
    if not isinstance(job_id, str) or not job_id:
        raise OffloadProtocolError("offload: empty job id")
    if job_id in (".", ".."):
        raise OffloadProtocolError(f"offload: refused job id {job_id!r} (path traversal)")
    if job_id == CONTROL_FILENAME.removesuffix(".json") or job_id.endswith(
        PROGRESS_SUFFIX.removesuffix(".json")
    ):
        raise OffloadProtocolError(
            f"offload: refused job id {job_id!r} (reserved: the claim directory keeps "
            f"{CONTROL_FILENAME} and <job>{PROGRESS_SUFFIX} under these names)"
        )
    if job_id == RANGE_CACHE_DIRNAME:
        raise OffloadProtocolError(
            f"offload: refused job id {job_id!r} (reserved: a worker's work root keeps every "
            "chunked OCR job's resume cache under this name)"
        )
    if job_id == RETRIED_DIRNAME:
        raise OffloadProtocolError(
            f"offload: refused job id {job_id!r} (reserved: failed/{RETRIED_DIRNAME}/ keeps the "
            "evidence of every retried terminal attempt under this name)"
        )
    if not JOB_ID_RE.match(job_id):
        raise OffloadProtocolError(
            f"offload: refused job id {job_id!r} (must match {JOB_ID_RE.pattern})"
        )
    return job_id


def offload_root(program_root: Path | str) -> Path:
    return Path(program_root) / OFFLOAD_DIRNAME


def pending_dir(root: Path) -> Path:
    return Path(root) / "pending"


def claimed_dir(root: Path) -> Path:
    return Path(root) / "claimed"


def done_dir(root: Path) -> Path:
    return Path(root) / "done"


def failed_dir(root: Path) -> Path:
    return Path(root) / "failed"


def partial_dir(root: Path) -> Path:
    return done_dir(root) / PARTIAL_DIRNAME


def queued_dir(root: Path) -> Path:
    """The sandbox's own record of what it queued (SEC-1). Deliberately NOT
    one of the directories ``offload-shell.sh`` creates or touches."""
    return Path(root) / QUEUED_DIRNAME


def rejected_dir(root: Path) -> Path:
    return Path(root) / REJECTED_DIRNAME


def retried_dir(root: Path) -> Path:
    """``failed/_retried`` -- the kept evidence of every retried terminal
    attempt (:data:`RETRIED_DIRNAME`). Created on demand by
    :func:`retry_marker`, never by :func:`ensure_layout`: a program that has
    never retried anything should not grow the directory as a side effect of
    somebody asking a question."""
    return failed_dir(Path(root)) / RETRIED_DIRNAME


def parked_largeformat_dir(root: Path) -> Path:
    """``parked_largeformat/`` -- read-only from this module
    (:data:`PARKED_LARGEFORMAT_DIRNAME`)."""
    return Path(root) / PARKED_LARGEFORMAT_DIRNAME


def parked_largeformat_entry(root: Path | str, job_id: str) -> Path | None:
    """The parked entry for ``job_id``, or ``None``.

    Either shape counts -- a directory named for the job, or a manifest file
    named for it -- because nothing in this harness writes the directory and
    a reader that guessed one shape would silently report "no twin" for the
    other. Nothing here creates, moves or deletes anything."""
    d = parked_largeformat_dir(Path(root))
    if not d.is_dir():
        return None
    validate_job_id(job_id)
    for candidate in (d / job_id, d / f"{job_id}.json"):
        if candidate.exists():
            return candidate
    return None


def idle_job_id(worker_id: str) -> str:
    """The synthetic id a worker with no claim heartbeats under (D3)."""
    return f"{IDLE_JOB_PREFIX}{worker_id}"


def control_path(root: Path | str, worker_id: str) -> Path:
    """``claimed/<worker_id>/CONTROL.json`` -- one pending request per
    worker, whatever it is currently running."""
    return claimed_dir(Path(root)) / worker_id / CONTROL_FILENAME


def progress_path(root: Path | str, worker_id: str, job_id: str) -> Path:
    """``claimed/<worker_id>/<job_id>.progress.json``. ``job_id`` is a real
    job id while one is claimed and :func:`idle_job_id` otherwise."""
    return claimed_dir(Path(root)) / worker_id / f"{job_id}{PROGRESS_SUFFIX}"


def _is_manifest(path: Path) -> bool:
    """Whether a ``claimed/<worker>/*.json`` hit is a job MANIFEST.

    ``CONTROL.json`` and ``<job>.progress.json`` share the claim directory
    and the ``.json`` extension with the manifests (see
    :data:`PROGRESS_SUFFIX`), and neither is a claim: a reader that counted
    them would report a worker's own status file as a held job."""
    name = path.name
    return name != CONTROL_FILENAME and not name.endswith(PROGRESS_SUFFIX)


def ensure_layout(root: Path | str) -> Path:
    root = Path(root)
    for d in (
        pending_dir(root),
        claimed_dir(root),
        done_dir(root),
        failed_dir(root),
        partial_dir(root),
        queued_dir(root),
    ):
        d.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# small io helpers
# ---------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return (parse(now()) - parse(ts)).total_seconds()
    except Exception:  # noqa: BLE001 - an unparseable stamp is "unknown age", never a crash
        return None


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _move_dir(src: Path, dst: Path) -> None:
    """Rename ``src`` onto ``dst``, tolerating the post-crash duplicate.

    Both existing means an earlier verb was interrupted between its two
    renames (see this module's docstring): the directory move is a single
    ``rename``, so it cannot have been half-applied -- ``dst`` is already
    the whole thing and ``src`` is the stale copy."""
    if not src.exists():
        return
    if dst.exists():
        _rmtree(src)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------
def queue_marker(
    root: Path | str,
    *,
    job_id: str,
    stage: str,
    doc_id: str | None,
    expect: dict[str, Any],
    config_hash: str,
    inputs: Iterable[tuple[str, bytes]],
    offload_attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_OFFLOAD_ATTEMPTS,
    retried: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write one job's inputs and manifest into ``pending/``.

    Inputs land in ``pending/<job_id>/`` FIRST and the manifest -- the
    thing every other verb keys off -- last, so a crash mid-write leaves an
    orphan input directory (harmless, overwritten by the next attempt)
    rather than a manifest pointing at inputs that do not exist yet.

    ``retried`` (lane FB-8a) is a job's retry HISTORY, carried through by
    :func:`requeue_marker`. This function builds a manifest from scratch
    rather than copying one, so anything not named in its signature is
    dropped -- which is how the first DEV failure AFTER a retry used to
    erase the record of that retry, leaving an evidence directory under
    ``failed/_retried/`` that the live manifest no longer pointed back at.
    The key is emitted only when there IS a history, so every manifest this
    codebase already writes is byte-identical to before.
    """
    root = ensure_layout(root)
    validate_job_id(job_id)
    in_dir = pending_dir(root) / job_id
    _rmtree(in_dir)
    in_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    for name, data in inputs:
        if "/" in name or "\\" in name or name in (".", "..") or not name:
            raise OffloadProtocolError(f"offload: refused input payload name {name!r}")
        atomic_write_bytes(in_dir / name, data)
        entries.append({"name": name, "sha256": sha256_bytes(data), "bytes": len(data)})

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "job_id": job_id,
        "stage": stage,
        "doc_id": doc_id,
        "created_ts": now(),
        "config_hash": config_hash,
        "offload_attempts": int(offload_attempts),
        "max_attempts": int(max_attempts),
        "inputs": entries,
        "expect": dict(expect),
    }
    if retried:
        manifest["retried"] = [dict(stamp) for stamp in retried]
    # SEC-1: the sandbox's own copy FIRST. `queued/` is unreachable through
    # the seven verbs, so from here on there is exactly one manifest in the
    # tree whose provenance is not in question -- and every later decision
    # that could fold a DEV-supplied payload into the record is checked
    # against it rather than against a manifest sitting in a directory the
    # remote key can write.
    write_json(queued_dir(root) / f"{job_id}.json", manifest)
    write_json(pending_dir(root) / f"{job_id}.json", manifest)
    return manifest


def find_manifest(root: Path | str, job_id: str) -> tuple[str, Path] | None:
    """Where this job's manifest currently lives, as ``(state, path)`` with
    ``state`` in ``pending|claimed|done|failed`` -- design section 4 step 2's
    "the manifest exists in ``pending/ u claimed/*/ u done/ u failed/``"
    membership test, in one place so every caller agrees on it."""
    root = Path(root)
    validate_job_id(job_id)
    p = pending_dir(root) / f"{job_id}.json"
    if p.is_file():
        return "pending", p
    claimed = claimed_dir(root)
    if claimed.is_dir():
        for worker in sorted(claimed.iterdir()):
            if not worker.is_dir():
                continue
            c = worker / f"{job_id}.json"
            # FIX V-11's belt and braces: `validate_job_id` above already
            # refuses the two reserved names, and `_is_manifest` is the same
            # rule `list_claims` and `reclaim_stale` apply -- so every reader of
            # this directory agrees about what a manifest is.
            if c.is_file() and _is_manifest(c):
                return "claimed", c
    d = done_dir(root) / job_id / MANIFEST_FILENAME
    if d.is_file():
        return "done", d
    f = failed_dir(root) / job_id / MANIFEST_FILENAME
    if f.is_file():
        return "failed", f
    return None


def queued_manifest(root: Path | str, job_id: str) -> dict[str, Any] | None:
    """The sandbox's OWN copy of this job's manifest, or ``None`` if this
    sandbox never queued it (SEC-1).

    This is the trust anchor of the whole verification chain: every other
    manifest copy in the tree has, at some point, sat in a directory the
    restricted DEV key can write into."""
    path = queued_dir(Path(root)) / f"{validate_job_id(job_id)}.json"
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):  # pragma: no cover - a corrupt record is "no record"
        return None


def forget_queued(root: Path | str, job_id: str) -> None:
    """Drop the sandbox's record once the job is settled for good (its
    result was swept, or its marker went terminal)."""
    path = queued_dir(Path(root)) / f"{validate_job_id(job_id)}.json"
    if path.is_file():
        path.unlink()


def trusted_manifest(root: Path | str, job_id: str, fallback: Path | str) -> dict[str, Any]:
    """The manifest to verify a published result against: the sandbox's own
    record when it has one, the on-disk copy otherwise.

    The fallback exists for markers written before this record did (and for
    hand-built fixtures); it is never the preferred source, because the
    file it names has been reachable by the remote key."""
    record = queued_manifest(root, job_id)
    if record is not None:
        return record
    return read_json(Path(fallback))


def published_result(root: Path | str, job_id: str) -> dict[str, Any] | None:
    path = done_dir(Path(root)) / validate_job_id(job_id) / RESULT_FILENAME
    return read_json(path) if path.is_file() else None


def published_error(root: Path | str, job_id: str) -> dict[str, Any] | None:
    path = done_dir(Path(root)) / validate_job_id(job_id) / ERROR_FILENAME
    return read_json(path) if path.is_file() else None


# ---------------------------------------------------------------------------
# verification of a published result
# ---------------------------------------------------------------------------
def verify_published(root: Path | str, job_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Design section 4 step 1: "verify every payload sha and the ``expect``
    block ... vector count = chunk count, chunk ids match". Returns the
    parsed ``result.json`` on success; raises
    :class:`OffloadVerificationError` naming the first mismatch otherwise.

    Every check here exists because the alternative is a silent corruption
    of the record: a payload whose bytes changed in transit, a result
    produced by a DIFFERENT model than the ``emb`` rows will be keyed
    under, or a vector list that does not line up with the chunk list it
    claims to be for.

    SEC-2: there is deliberately no ``config_hash`` comparison here any
    more. The one that used to sit in this function compared
    ``result["config_hash"]`` against ``manifest["config_hash"]`` -- but
    DEV populated the former by copying the latter, so the check could only
    ever pass, and a manifest written against a configuration the program
    has since changed sailed through it. The real question ("was this work
    done for the configuration this stage has NOW?") can only be answered
    where the live config is, so it is asked in
    :func:`trialerror.offload.stage._resolve_or_park` instead, against
    ``config_hash(cfg)`` rather than against a value the worker echoed.
    """
    root = Path(root)
    job_dir = done_dir(root) / validate_job_id(job_id)
    result = published_result(root, job_id)
    if result is None:
        raise OffloadVerificationError(f"offload {job_id}: no {RESULT_FILENAME} in {job_dir}")

    if result.get("schema") != RESULT_SCHEMA:
        raise OffloadVerificationError(
            f"offload {job_id}: result schema {result.get('schema')!r} != {RESULT_SCHEMA!r}"
        )
    if result.get("job_id") != job_id:
        raise OffloadVerificationError(
            f"offload {job_id}: result claims job_id {result.get('job_id')!r}"
        )
    if result.get("stage") != manifest.get("stage"):
        raise OffloadVerificationError(
            f"offload {job_id}: result stage {result.get('stage')!r} != manifest "
            f"stage {manifest.get('stage')!r}"
        )
    outputs = result.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise OffloadVerificationError(f"offload {job_id}: result declares no outputs")
    for entry in outputs:
        name = entry.get("name")
        if not isinstance(name, str) or "/" in name or "\\" in name or name in (".", ".."):
            raise OffloadVerificationError(f"offload {job_id}: refused output name {name!r}")
        path = job_dir / name
        if not path.is_file():
            raise OffloadVerificationError(f"offload {job_id}: declared output {name!r} is missing")
        actual = sha256_bytes(path.read_bytes())
        if actual != entry.get("sha256"):
            raise OffloadVerificationError(
                f"offload {job_id}: payload {name!r} sha256 mismatch "
                f"(declared {str(entry.get('sha256'))[:12]}..., actual {actual[:12]}...)"
            )

    expect = manifest.get("expect") or {}
    stage = manifest.get("stage")
    if stage == "ocr":
        backend = result.get("backend")
        if not backend or backend == "fake":
            raise OffloadVerificationError(
                f"offload {job_id}: DEV worker reported OCR backend {backend!r} -- a fake "
                "backend result is never folded into the record (D13)"
            )
        wanted = expect.get("backend")
        if wanted and backend != wanted:
            raise OffloadVerificationError(
                f"offload {job_id}: OCR backend {backend!r} != expected {wanted!r}"
            )
        wanted_v = expect.get("version")
        if wanted_v and result.get("version") != wanted_v:
            raise OffloadVerificationError(
                f"offload {job_id}: OCR backend version {result.get('version')!r} != expected {wanted_v!r}"
            )
    elif stage == "embed":
        if result.get("model_key") != expect.get("model_key"):
            raise OffloadVerificationError(
                f"offload {job_id}: model_key {result.get('model_key')!r} != expected "
                f"{expect.get('model_key')!r} -- emb rows are keyed by it"
            )
        if int(result.get("dims") or 0) != int(expect.get("dims") or 0):
            raise OffloadVerificationError(
                f"offload {job_id}: dims {result.get('dims')!r} != expected {expect.get('dims')!r}"
            )
        got_ids = result.get("chunk_ids")
        want_ids = expect.get("chunk_ids")
        if got_ids != want_ids:
            raise OffloadVerificationError(
                f"offload {job_id}: chunk id list does not match the manifest's "
                f"({len(got_ids or [])} returned vs {len(want_ids or [])} sent)"
            )
    else:  # pragma: no cover - guarded at queue time
        raise OffloadVerificationError(f"offload {job_id}: unknown stage {stage!r}")

    return result


# ---------------------------------------------------------------------------
# terminal / retry transitions on the sandbox side
# ---------------------------------------------------------------------------
def fail_marker(root: Path | str, job_id: str, *, manifest: dict[str, Any], error: str) -> Path:
    """Move a job's marker to ``failed/<job_id>/`` (terminal) with an
    ``error.json`` beside its manifest, and clear every other trace of it
    from the queue. The ledger row is settled by the CALLER raising a logic
    failure -- this function only owns the file queue."""
    root = ensure_layout(root)
    validate_job_id(job_id)
    dest = failed_dir(root) / job_id
    _rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    write_json(dest / MANIFEST_FILENAME, manifest)
    write_json(
        dest / ERROR_FILENAME,
        {
            "schema": ERROR_SCHEMA,
            "job_id": job_id,
            "stage": manifest.get("stage"),
            "offload_attempts": manifest.get("offload_attempts"),
            "error": error,
            "ts": now(),
        },
    )
    _purge_elsewhere(root, job_id, keep="failed")
    forget_queued(root, job_id)
    return dest


def requeue_marker(root: Path | str, job_id: str, *, manifest: dict[str, Any], inputs: Iterable[tuple[str, bytes]]) -> dict[str, Any]:
    """Put a job back in ``pending/`` after a DEV-side failure, carrying its
    incremented ``offload_attempts`` forward. Inputs are re-written from
    the caller's own source of truth (the raw file / the chunk rows) rather
    than salvaged from the failed attempt's directory -- the store is the
    durable truth, the queue is a courier."""
    root = ensure_layout(root)
    validate_job_id(job_id)
    _purge_elsewhere(root, job_id, keep="pending")
    return queue_marker(
        root,
        job_id=job_id,
        stage=manifest["stage"],
        doc_id=manifest.get("doc_id"),
        expect=manifest.get("expect") or {},
        config_hash=manifest.get("config_hash", ""),
        inputs=inputs,
        offload_attempts=int(manifest.get("offload_attempts", 0)),
        max_attempts=int(manifest.get("max_attempts", DEFAULT_MAX_OFFLOAD_ATTEMPTS)),
        # Lane FB-8a: a re-queue must not erase the job's retry history --
        # see :func:`queue_marker`'s note on why it would otherwise.
        retried=manifest.get("retried"),
    )


def _evidence_stamp(ts: str) -> str:
    """``2026-09-19T05:20:00.123Z`` -> ``20260919T052000Z``.

    The evidence directory's name is a PATH COMPONENT on both machines, and
    a raw ISO stamp carries ``:``, which is a drive separator and an
    alternate-data-stream separator on Windows (the same reason
    :func:`unpack_into` refuses a member name containing one). Milliseconds
    are dropped because the suffix only has to disambiguate, and
    :func:`retry_marker` disambiguates a same-second collision itself."""
    return re.sub(r"[^0-9A-Za-z]", "", ts.split(".")[0]) + "Z"


def retry_source_manifest(
    root: Path | str, job_id: str
) -> tuple[dict[str, Any], bytes | None, str] | None:
    """The manifest a retry starts from, as ``(manifest, error_bytes,
    source)`` where ``source`` is ``"failed"`` or ``"retried"``.

    ``failed/<job_id>/`` is the ordinary case. The ``_retried/`` fallback is
    the RESUME case: :func:`retry_marker` moves the evidence before it writes
    the queue entry (see its ordering note), so a crash in between leaves a
    job whose terminal directory is already archived and whose queue entry
    does not exist yet -- and the only surviving copy of what to re-queue is
    the archived one. Newest entry wins, by the stamp its name ends in."""
    root = Path(root)
    validate_job_id(job_id)
    direct = failed_dir(root) / job_id
    if (direct / MANIFEST_FILENAME).is_file():
        err = direct / ERROR_FILENAME
        return read_json(direct / MANIFEST_FILENAME), (err.read_bytes() if err.is_file() else None), "failed"
    archive = retried_dir(root)
    if archive.is_dir():
        candidates = sorted(
            (c for c in archive.iterdir() if c.is_dir() and c.name.rsplit(".", 1)[0] == job_id),
            key=lambda c: c.name,
        )
        for candidate in reversed(candidates):
            if (candidate / MANIFEST_FILENAME).is_file():
                err = candidate / ERROR_FILENAME
                return (
                    read_json(candidate / MANIFEST_FILENAME),
                    (err.read_bytes() if err.is_file() else None),
                    "retried",
                )
    return None


def queue_half_retried(root: Path | str, job_id: str) -> dict[str, Any] | None:
    """The queue entry this job was already retried back into, or ``None``.

    "Already retried" is three facts at once, and all three are needed: the
    sandbox's OWN record (``queued/``, the one directory the offload key
    cannot write -- SEC-1) carries a non-empty ``retried`` list, the work
    queue (``pending/<job_id>.json``) carries the same manifest, and
    ``failed/<job_id>/`` is gone. Any two of the three is an INTERRUPTED
    retry, which :func:`retry_marker` finishes rather than reports."""
    root = Path(root)
    record = queued_manifest(root, job_id)
    if record is None or not record.get("retried"):
        return None
    if not (pending_dir(root) / f"{job_id}.json").is_file():
        return None
    if (failed_dir(root) / job_id).exists():
        return None
    return record


def retry_marker(
    root: Path | str,
    job_id: str,
    *,
    reason: str,
    inputs: Iterable[tuple[str, bytes]],
    ts: str | None = None,
    by_launch: str | None = None,
) -> dict[str, Any]:
    """The inverse of :func:`fail_marker`: put a terminal marker back in the
    queue, and KEEP the terminal attempt as evidence.

    ``fail_marker`` writes ``failed/<job_id>/{manifest.json, error.json}``,
    purges the job from everywhere else and forgets the sandbox's own
    ``queued/`` record. This undoes exactly those three things, in the one
    order that leaves every interruption recoverable, and adds the two that
    make the undo auditable:

    1. **inputs** into ``pending/<job_id>/``. Rebuilt by the caller from the
       record (the raw file, the chunk rows) rather than salvaged -- the
       same rule :func:`requeue_marker` states, and there is nothing to
       salvage anyway, because ``fail_marker`` purged them. An interruption
       here leaves an input directory with no manifest: invisible to every
       reader (they all key on ``*.json``) and overwritten by the next call.
    2. **the evidence move**: ``failed/<job_id>/`` -> ``failed/_retried/<job
       _id>.<stamp>/``, one ``rename``, with :data:`RETRY_NOTE_FILENAME`
       written beside the untouched pair. Interrupted after this, the job has
       no manifest anywhere and :func:`retry_source_manifest` reads the
       archived copy on the next call.
    3. **the sandbox's own record** (``queued/<job_id>.json``), carrying
       ``offload_attempts: 0`` and the appended ``retried`` stamp.
    4. **the work queue** (``pending/<job_id>.json``) -- LAST, because this
       is the write that makes the job claimable by the GPU worker, and
       nothing should be claimable before its inputs, its evidence and its
       provenance are all durably in place.

    Idempotent and detectable: a job whose queue entry is already back
    (:func:`queue_half_retried`) is returned with ``moved: False`` and
    nothing is touched, which is what lets ``trialerror jobs retry`` finish
    a retry that crashed between this half and the jobs-store half.

    No lock. This queue closes its crash windows by ORDERING, not by
    locking (see the module docstring) -- every mutation here is a rename or
    an atomic write, and the recovery path above is what a second call
    converges through.
    """
    root = ensure_layout(root)
    validate_job_id(job_id)
    if not reason or not reason.strip():
        raise OffloadProtocolError(f"offload retry {job_id}: a reason is required")

    already = queue_half_retried(root, job_id)
    if already is not None:
        return {
            "moved": False,
            "already_retried": True,
            "from": None,
            "to": None,
            "attempts_reset": int(already.get("offload_attempts", 0)),
            "manifest": already,
        }

    found = retry_source_manifest(root, job_id)
    if found is None:
        raise OffloadProtocolError(
            f"offload retry {job_id}: no terminal marker to retry -- nothing in "
            f"{failed_dir(root) / job_id} and nothing archived under {retried_dir(root)}"
        )
    manifest, error_bytes, source = found
    ts = ts or now()
    previous_attempts = int(manifest.get("offload_attempts", 0) or 0)
    stamp = {
        "ts": ts,
        "reason": reason,
        "previous_attempts": previous_attempts,
        # The sha of the error.json FILE, not of the message inside it: the
        # file is what the evidence directory keeps, so this is the value an
        # operator can recompute against the thing on disk.
        "previous_error_sha256": sha256_bytes(error_bytes) if error_bytes is not None else None,
    }
    if by_launch:
        stamp["by_launch"] = by_launch
    history = list(manifest.get("retried") or [])
    history.append(stamp)
    next_manifest = {**manifest, "offload_attempts": 0, "retried": history}

    # -- step 1: inputs ----------------------------------------------------
    in_dir = pending_dir(root) / job_id
    _rmtree(in_dir)
    in_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for name, data in inputs:
        if "/" in name or "\\" in name or name in (".", "..") or not name:
            raise OffloadProtocolError(f"offload retry {job_id}: refused input payload name {name!r}")
        atomic_write_bytes(in_dir / name, data)
        entries.append({"name": name, "sha256": sha256_bytes(data), "bytes": len(data)})
    next_manifest["inputs"] = entries

    # -- step 2: the evidence move ----------------------------------------
    moved_to: Path | None = None
    src = failed_dir(root) / job_id
    if source == "failed" and src.is_dir():
        archive = retried_dir(root)
        archive.mkdir(parents=True, exist_ok=True)
        base = f"{job_id}.{_evidence_stamp(ts)}"
        dest = archive / base
        suffix = 2
        while dest.exists():  # two retries inside one second still keep both
            dest = archive / f"{base}-{suffix}"
            suffix += 1
        os.replace(src, dest)
        write_json(
            dest / RETRY_NOTE_FILENAME,
            {
                "schema": RETRY_NOTE_SCHEMA,
                "job_id": job_id,
                "stage": manifest.get("stage"),
                "doc_id": manifest.get("doc_id"),
                **stamp,
            },
        )
        moved_to = dest

    # -- steps 3 and 4: the sandbox's record, then the work queue ---------
    write_json(queued_dir(root) / f"{job_id}.json", next_manifest)
    write_json(pending_dir(root) / f"{job_id}.json", next_manifest)
    return {
        "moved": True,
        "already_retried": False,
        "from": str(src) if moved_to is not None else None,
        "to": str(moved_to) if moved_to is not None else None,
        "attempts_reset": previous_attempts,
        "manifest": next_manifest,
    }


def discard_published(root: Path | str, job_id: str) -> None:
    """Delete ``done/<job_id>/`` -- ``trialerror offload kick``'s sweeper
    once the ledger row is ``complete`` (design section 4 step 1). The
    sandbox's own record of the job goes with it: the work is in the record
    now, and a queued-record with no job behind it would keep the adoption
    gate open for a job id nobody is waiting on."""
    root = Path(root)
    _rmtree(done_dir(root) / validate_job_id(job_id))
    forget_queued(root, job_id)


def _purge_elsewhere(root: Path, job_id: str, *, keep: str) -> None:
    if keep != "pending":
        _rmtree(pending_dir(root) / job_id)
        p = pending_dir(root) / f"{job_id}.json"
        if p.is_file():
            p.unlink()
    if keep != "done":
        _rmtree(done_dir(root) / job_id)
    _rmtree(partial_dir(root) / job_id)
    if keep != "failed":
        _rmtree(failed_dir(root) / job_id)
    claimed = claimed_dir(root)
    if claimed.is_dir():
        for worker in claimed.iterdir():
            if not worker.is_dir():
                continue
            for suffix in (".json", HEARTBEAT_SUFFIX, PROGRESS_SUFFIX):
                f = worker / f"{job_id}{suffix}"
                if f.is_file():
                    f.unlink()
            _rmtree(worker / job_id)


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------
def list_pending(root: Path | str) -> list[str]:
    d = pending_dir(Path(root))
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.is_file())


def list_claims(root: Path | str) -> list[dict[str, Any]]:
    """Every live claim as ``{"job_id", "worker_id", "heartbeat_ts",
    "age_s"}`` -- ``age_s`` is ``None`` when no heartbeat has been written
    yet (a claim taken seconds ago) and falls back to the manifest's own
    mtime when the heartbeat file is unreadable."""
    root = Path(root)
    out: list[dict[str, Any]] = []
    claimed = claimed_dir(root)
    if not claimed.is_dir():
        return out
    for worker in sorted(claimed.iterdir()):
        if not worker.is_dir():
            continue
        for manifest_path in sorted(p for p in worker.glob("*.json") if _is_manifest(p)):
            job_id = manifest_path.stem
            hb = worker / f"{job_id}{HEARTBEAT_SUFFIX}"
            ts = None
            if hb.is_file():
                try:
                    ts = hb.read_text(encoding="utf-8").strip() or None
                except OSError:
                    ts = None
            age = _age_s(ts)
            if age is None:
                try:
                    import datetime as _dt

                    mtime = (hb if hb.is_file() else manifest_path).stat().st_mtime
                    age = max(0.0, _dt.datetime.now().timestamp() - mtime)
                except OSError:  # pragma: no cover - stat failure is unreachable in practice
                    age = None
            out.append(
                {"job_id": job_id, "worker_id": worker.name, "heartbeat_ts": ts, "age_s": age}
            )
    return out


def list_done(root: Path | str) -> list[str]:
    d = done_dir(Path(root))
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and p.name != PARTIAL_DIRNAME)


def list_failed(root: Path | str) -> list[str]:
    """Job ids whose marker is terminal AND still outstanding.

    ``failed/_retried/`` is skipped (:data:`RETRIED_DIRNAME`) the same way
    ``done/.partial/`` is skipped by :func:`list_done`: it is not a job, it
    is this directory's own archive, and a reader that counted it would
    report one permanent extra failure per program plus a doctor finding
    that can never be cleared."""
    d = failed_dir(Path(root))
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and p.name != RETRIED_DIRNAME)


def list_retried(root: Path | str) -> list[dict[str, Any]]:
    """Every kept evidence directory under ``failed/_retried/``, as
    ``{"job_id", "entry", "path", "retried_ts", "reason"}``.

    Counted SEPARATELY from :func:`list_failed` (design of lane FB-8a): a
    retried job is one a human has already dealt with, so it must stop
    reading as an outstanding failure -- while still being visible, because
    the attempt it records really did happen and really did burn GPU time.

    The archived ``manifest.json``/``error.json`` are left EXACTLY as
    :func:`fail_marker` wrote them -- that pair is the evidence, and an
    evidence directory this verb edited would be worth less than one it did
    not. Why it was retried is written BESIDE them, as
    :data:`RETRY_NOTE_FILENAME`, the same way :func:`_quarantine_partial`
    writes its ``rejected.json``.

    ``job_id`` comes from the note when it is readable and from the
    directory name otherwise; the stamp suffix
    (:func:`_evidence_stamp`) contains no ``.``, so the fallback split is
    unambiguous for every id :func:`validate_job_id` admits."""
    d = retried_dir(Path(root))
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(d.iterdir()):
        if not child.is_dir():
            continue
        row: dict[str, Any] = {
            "job_id": child.name.rsplit(".", 1)[0],
            "entry": child.name,
            "path": str(child),
            "retried_ts": None,
            "reason": None,
        }
        note = child / RETRY_NOTE_FILENAME
        if note.is_file():
            try:
                parsed = read_json(note)
            except (OSError, json.JSONDecodeError):  # pragma: no cover - a corrupt note is still evidence
                parsed = {}
            row["job_id"] = str(parsed.get("job_id") or row["job_id"])
            row["retried_ts"] = parsed.get("ts")
            row["reason"] = parsed.get("reason")
        out.append(row)
    return out


def oldest_pending_age_s(root: Path | str) -> float | None:
    """Age of the oldest pending manifest, by its ``created_ts``. Drives
    the ``offload_backlog`` doctor check's 24 h warn and the HOME line."""
    root = Path(root)
    oldest: float | None = None
    for job_id in list_pending(root):
        try:
            manifest = read_json(pending_dir(root) / f"{job_id}.json")
        except (OSError, json.JSONDecodeError):
            continue
        age = _age_s(manifest.get("created_ts"))
        if age is None:
            continue
        oldest = age if oldest is None else max(oldest, age)
    return oldest


def counts(root: Path | str) -> dict[str, Any]:
    root = Path(root)
    pend = list_pending(root)
    claims = list_claims(root)
    retried = list_retried(root)
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "pending": len(pend),
        "claimed": len(claims),
        "done": len(list_done(root)),
        "failed": len(list_failed(root)),
        # Lane FB-8a: its OWN count, never folded into `failed`. A retried
        # marker is one a human has already acted on; leaving it in the
        # failure count would mean the number an operator watches can only
        # ever go up.
        "retried": len(retried),
        "pending_job_ids": pend,
        "claims": claims,
        "retried_entries": retried,
        "oldest_pending_age_s": oldest_pending_age_s(root),
    }


# ---------------------------------------------------------------------------
# reclaim / orphan adoption (the sandbox's own janitors)
# ---------------------------------------------------------------------------
def reclaim_stale(root: Path | str, *, expiry_s: float = DEFAULT_CLAIM_EXPIRY_S) -> list[dict[str, Any]]:
    """Design section 4: "returns claims with heartbeats older than 60 min
    to ``pending/``". A DEV laptop that is suspended, unplugged, or simply
    closed has no way to run a cleanup hook -- this is the one mechanism
    that gets its claim back, so it must never depend on the worker's
    cooperation."""
    # V2: read-only when there is no queue. `trialerror offload reclaim`
    # runs from an unattended loop in EVERY program, including the many
    # that offload nothing -- creating a five-directory tree in each of
    # them as a side effect of asking a question is not what the verb
    # promises.
    root = Path(root)
    if not root.is_dir():
        return []
    ensure_layout(root)
    reclaimed: list[dict[str, Any]] = []
    for claim in list_claims(root):
        age = claim.get("age_s")
        if age is None or age < expiry_s:
            continue
        job_id = claim["job_id"]
        worker = claimed_dir(root) / claim["worker_id"]
        # inputs first, manifest last -- the same ordering `return` uses.
        _move_dir(worker / job_id, pending_dir(root) / job_id)
        src = worker / f"{job_id}.json"
        dst = pending_dir(root) / f"{job_id}.json"
        if src.is_file():
            if dst.exists():
                src.unlink()
            else:
                os.replace(src, dst)
        hb = worker / f"{job_id}{HEARTBEAT_SUFFIX}"
        if hb.is_file():
            hb.unlink()
        _unlink_progress(worker, job_id)
        reclaimed.append({**claim, "reclaimed_ts": now()})
    return reclaimed


def _unlink_progress(worker_dir: Path, job_id: str) -> None:
    """Drop a job's progress file wherever its claim ends -- publish,
    return, or reclaim.

    The file says what a worker is doing with a job it HOLDS; left behind
    after the claim is gone it would keep a finished job on the JOBS card as
    a live worker row until it aged into ``lost``, which is a reading about
    the worker rather than about the job. The per-worker ``CONTROL.json``
    deliberately survives: a pause is a standing instruction to the worker,
    not to the job it happened to be running."""
    path = worker_dir / f"{job_id}{PROGRESS_SUFFIX}"
    if path.is_file():
        try:
            path.unlink()
        except OSError:  # pragma: no cover - a vanished file is the desired state
            pass


def _quarantine_partial(root: Path, staging: Path, job_id: str, reason: str) -> None:
    """Move a staging directory that failed the adoption gate out of
    ``done/.partial/`` and into ``rejected/``, with the reason beside it.

    Refusing in place would mean re-examining (and re-refusing) the same
    directory every five minutes forever, and would leave the payload
    sitting where the next genuine publish stages through. Deleting it
    would destroy the only evidence of an attempt worth looking at."""
    dest = rejected_dir(root) / job_id
    _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(staging, dest)
    except OSError:  # pragma: no cover - a cross-device queue is not a supported layout
        _rmtree(staging)
        return
    write_json(dest / "rejected.json", {"job_id": job_id, "reason": reason, "ts": now()})


def adopt_orphaned_partials(root: Path | str) -> list[str]:
    """Finish a publish that was interrupted after its manifest moved into
    the staging directory but before the final rename (see this module's
    docstring). A staging directory WITHOUT a manifest is a push still in
    flight, or the leftovers of one -- never touched here.

    **SEC-1: adoption is the one place a DEV-authored file could otherwise
    become the contract it is checked against.** Everything under
    ``done/.partial/<job_id>/`` arrives by ``push``, i.e. it is whatever
    the holder of the restricted key put in a tar. The pre-fix rule
    ("adopt any staging directory containing a ``manifest.json``") let that
    key hand the sandbox a manifest of its own choosing -- config hash,
    ``expect`` block, payload shas and all -- which
    :func:`verify_published` would then dutifully check the key's own
    result against. Two conditions now gate the rename, and both are facts
    only the SANDBOX can establish:

    1. :func:`queued_manifest` must know the job. ``queued/`` is
       unreachable through the seven verbs, so a staging directory for a
       job this sandbox never queued cannot be adopted at all.
    2. The manifest must be GONE from ``pending/`` and ``claimed/``. A
       genuine interrupted publish is interrupted precisely between "the
       manifest moved into staging" and "the staging directory became
       visible", so for it :func:`find_manifest` finds nothing. A key that
       pushed a ``manifest.json`` of its own and simply never called
       ``publish`` still has the real manifest sitting in its claim
       directory -- which is exactly how that forgery is told apart from a
       crash.

    The manifest that then lands in ``done/<job_id>/`` is the sandbox's own
    copy, not the one the archive carried: a directory that passes both
    gates has still had DEV bytes in it.

    Note that the literal form the review suggested -- refuse when
    ``find_manifest(...) is None`` -- inverts this: ``None`` is the state a
    genuine interrupted publish leaves behind, so that gate would refuse
    every real adoption and admit none of the forgeries. The invariant it
    asked for ("an adopted publish must correspond to a job the sandbox
    itself queued and claimed") is what rule 1 enforces.
    """
    root = Path(root)
    adopted: list[str] = []
    partial = partial_dir(root)
    if not partial.is_dir():  # V2: nothing to adopt, and nothing to create
        return adopted
    for staging in sorted(partial.iterdir()):
        if not staging.is_dir() or not (staging / MANIFEST_FILENAME).is_file():
            continue
        job_id = staging.name
        try:
            validate_job_id(job_id)
        except OffloadProtocolError:  # pragma: no cover - only a hand-made directory gets here
            continue
        record = queued_manifest(root, job_id)
        if record is None:
            _quarantine_partial(
                root,
                staging,
                job_id,
                "no queued record: this sandbox never queued a job with this id, so the "
                "staging directory (and the manifest in it) can only have come from the "
                "offload key",
            )
            continue
        located = find_manifest(root, job_id)
        if located is not None and located[0] in ("pending", "claimed"):
            _quarantine_partial(
                root,
                staging,
                job_id,
                f"job is still {located[0]}: a publish interrupted after its manifest moved "
                "into the staging directory leaves no manifest behind, so this one was "
                "pushed rather than moved",
            )
            continue
        dest = done_dir(root) / job_id
        if dest.exists():
            _rmtree(staging)
            continue
        # The archive's own manifest.json is DEV-written; overwrite it with
        # the sandbox's record before this directory becomes visible.
        write_json(staging / MANIFEST_FILENAME, record)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, dest)
        _purge_elsewhere(root, job_id, keep="done")
        adopted.append(job_id)
    return adopted


def rejected_partials(root: Path | str) -> list[dict[str, Any]]:
    """What :func:`adopt_orphaned_partials` refused, for ``trialerror
    offload kick`` to report. Never swept automatically: a refusal here
    means something wrote into the queue that should not have."""
    d = rejected_dir(Path(root))
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(d.iterdir()):
        if not child.is_dir():
            continue
        info: dict[str, Any] = {"job_id": child.name, "path": str(child)}
        note = child / "rejected.json"
        if note.is_file():
            try:
                info.update(read_json(note))
            except (OSError, ValueError):  # pragma: no cover
                pass
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# tar helpers (the pull/push wire format)
# ---------------------------------------------------------------------------
def pack_dir(path: Path | str) -> bytes:
    """Uncompressed tar of ``path``'s regular files, one flat level, member
    names relative to ``path``. Uncompressed on purpose: the payloads are
    already-compressed PDFs or float text that gzip barely helps, and a
    plain tar is what the shell wrapper produces with the same layout."""
    path = Path(path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file():
                    tar.add(child, arcname=child.name)
    return buf.getvalue()


def check_payload_size(data: bytes, verb: str, *, max_bytes: int | None = None) -> bytes:
    """SEC-4: refuse an over-large ``push``/``pull`` payload rather than
    truncate it. A truncated tar is a corrupt result that would then be
    checked against a manifest it can no longer satisfy -- failing the verb
    is both safer and far easier to diagnose."""
    max_bytes = MAX_PAYLOAD_BYTES if max_bytes is None else max_bytes
    if max_bytes and len(data) > max_bytes:
        raise OffloadProtocolError(
            f"offload {verb}: payload is {len(data)} bytes, over the {max_bytes}-byte cap "
            "(TE_OFFLOAD_MAX_PUSH_BYTES / TE_OFFLOAD_MAX_PULL_BYTES on the wrapper side)"
        )
    return data


def unpack_into(data: bytes, dest: Path | str, *, max_bytes: int | None = None) -> list[str]:
    """Extract a flat tar into ``dest``, refusing anything that is not a
    plain regular file with a plain single-component name.

    This is the same gate ``offload-shell.sh`` applies on ``push``.
    Absolute paths, ``..`` components, symlinks, hardlinks and device nodes
    are the whole attack surface of "let a remote key hand us an archive",
    so they are rejected BY NAME here rather than trusted to tar's own
    defaults, and rejected BEFORE anything is written.

    SEC-5: ``os.path.isabs`` is not enough on Windows. ``C:pages.json`` is
    a drive-RELATIVE name -- ``isabs`` says False, and joining it onto
    ``dest`` yields a path resolved against the *current directory of drive
    C:*, i.e. outside ``dest`` entirely. Any member carrying a drive letter
    (or a bare ``:``, which is also the NTFS alternate-data-stream
    separator) is refused.

    SEC-4: the archive's total uncompressed size is bounded too, so a small
    tar that expands to a full disk is refused rather than written.
    """
    dest = Path(dest)
    max_bytes = MAX_PAYLOAD_BYTES if max_bytes is None else max_bytes
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    check_payload_size(data, "push", max_bytes=max_bytes)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        members = tar.getmembers()
        total = 0
        for member in members:
            name = member.name
            if not member.isfile():
                raise OffloadProtocolError(
                    f"offload push: refused non-regular archive member {name!r}"
                )
            if (
                name in ("", ".", "..")
                or "/" in name
                or "\\" in name
                or ":" in name
                or os.path.splitdrive(name)[0]
                or os.path.isabs(name)
            ):
                raise OffloadProtocolError(f"offload push: refused archive member name {name!r}")
            total += int(member.size or 0)
            if max_bytes and total > max_bytes:
                raise OffloadProtocolError(
                    f"offload push: archive expands to more than {max_bytes} bytes"
                )
        for member in members:
            src = tar.extractfile(member)
            if src is None:  # pragma: no cover - isfile() already guarantees a stream
                raise OffloadProtocolError(f"offload push: unreadable archive member {member.name!r}")
            atomic_write_bytes(dest / member.name, src.read())
            written.append(member.name)
    return written


# ---------------------------------------------------------------------------
# the seven verbs, server side
# ---------------------------------------------------------------------------
def server_list(root: Path | str) -> list[str]:
    return list_pending(root)


def server_claim(root: Path | str, job_id: str, *, worker_id: str) -> dict[str, Any]:
    """Atomic claim. The manifest rename IS the ownership transfer: a
    second worker racing for the same job finds no source file and gets an
    :class:`OffloadProtocolError`, exactly as
    ``ledger.claim_specific`` returns ``None`` to the loser of its own
    conditional UPDATE."""
    root = ensure_layout(root)
    validate_job_id(job_id)
    _validate_worker_id(worker_id)
    src = pending_dir(root) / f"{job_id}.json"
    worker_dir = claimed_dir(root) / worker_id
    worker_dir.mkdir(parents=True, exist_ok=True)
    dst = worker_dir / f"{job_id}.json"
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise OffloadProtocolError(f"offload claim {job_id}: not pending ({exc.__class__.__name__})") from exc
    _move_dir(pending_dir(root) / job_id, worker_dir / job_id)
    atomic_write_text(worker_dir / f"{job_id}{HEARTBEAT_SUFFIX}", now() + "\n")
    return read_json(dst)


def _validate_worker_id(worker_id: str) -> str:
    if not worker_id or not JOB_ID_RE.match(worker_id) or worker_id in (".", ".."):
        raise OffloadProtocolError(f"offload: refused worker id {worker_id!r}")
    return worker_id


def _claim_paths(root: Path, job_id: str, worker_id: str) -> tuple[Path, Path]:
    validate_job_id(job_id)
    _validate_worker_id(worker_id)
    worker_dir = claimed_dir(root) / worker_id
    manifest = worker_dir / f"{job_id}.json"
    if not manifest.is_file():
        raise OffloadProtocolError(
            f"offload: job {job_id!r} is not claimed by worker {worker_id!r}"
        )
    return worker_dir, manifest


def server_pull(root: Path | str, job_id: str, *, worker_id: str) -> bytes:
    root = Path(root)
    worker_dir, _ = _claim_paths(root, job_id, worker_id)
    in_dir = worker_dir / job_id
    if not in_dir.is_dir():
        raise OffloadProtocolError(f"offload pull {job_id}: no input directory in the claim")
    return check_payload_size(pack_dir(in_dir), f"pull {job_id}")


def server_push(root: Path | str, job_id: str, data: bytes, *, worker_id: str) -> list[str]:
    root = ensure_layout(root)
    _claim_paths(root, job_id, worker_id)
    staging = partial_dir(root) / job_id
    _rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        return unpack_into(data, staging)
    except Exception:
        _rmtree(staging)
        raise


def server_publish(root: Path | str, job_id: str, *, worker_id: str) -> Path:
    """Make a pushed result visible in one atomic step.

    Manifest into the staging directory, claim cleaned, then the single
    rename -- so ``done/<job_id>/`` never exists without its manifest, and
    a partially-pushed result is never visible at all (it is still under
    ``done/.partial/``, which every reader skips)."""
    root = ensure_layout(root)
    worker_dir, manifest_path = _claim_paths(root, job_id, worker_id)
    staging = partial_dir(root) / job_id
    if not staging.is_dir():
        raise OffloadProtocolError(f"offload publish {job_id}: nothing pushed yet")
    dest = done_dir(root) / job_id
    if dest.exists():
        raise OffloadProtocolError(f"offload publish {job_id}: already published")
    os.replace(manifest_path, staging / MANIFEST_FILENAME)
    _rmtree(worker_dir / job_id)
    hb = worker_dir / f"{job_id}{HEARTBEAT_SUFFIX}"
    if hb.is_file():
        hb.unlink()
    _unlink_progress(worker_dir, job_id)
    os.replace(staging, dest)
    return dest


def server_return(root: Path | str, job_id: str, *, worker_id: str) -> None:
    """Hand a claim back, unrun. Inputs first, manifest last (see the
    module docstring's ordering note)."""
    root = ensure_layout(root)
    worker_dir, manifest_path = _claim_paths(root, job_id, worker_id)
    _move_dir(worker_dir / job_id, pending_dir(root) / job_id)
    dst = pending_dir(root) / f"{job_id}.json"
    if dst.exists():
        manifest_path.unlink()
    else:
        os.replace(manifest_path, dst)
    hb = worker_dir / f"{job_id}{HEARTBEAT_SUFFIX}"
    if hb.is_file():
        hb.unlink()
    _unlink_progress(worker_dir, job_id)
    _rmtree(partial_dir(root) / job_id)


def _PAYLOAD_WHITESPACE() -> bytes:
    """The bytes a payload may be padded with, from the half that owns the
    rule.

    Imported lazily, like every other cross-module reference in this file:
    this module is the LAYOUT layer and ``trialerror.offload.shell`` holds the
    wrapper's rules, so the constant lives there and is never copied here
    (FIX V-8 exists because two spellings of "whitespace" disagreed)."""
    from trialerror.offload.shell import PAYLOAD_WHITESPACE

    return PAYLOAD_WHITESPACE


def server_heartbeat(
    root: Path | str, job_id: str, *, worker_id: str, progress: bytes | None = None
) -> str:
    """Stamp the claim, and -- C-0097 D3 -- store the optional progress
    payload beside it.

    ``progress`` is the RAW BYTES the worker sent on the verb's stdin, not a
    dict: the wrapper on the queue host has no JSON parser and writes the
    bytes it was given, so the in-process mirror must store exactly what the
    shell would store or the two sides disagree about a file the dashboard
    reads. The payload has already passed
    :func:`trialerror.offload.shell.progress_payload_refusal` by the time it
    gets here (both callers apply it first, the same way both apply
    :func:`trialerror.offload.shell.parse_command`).

    An empty or whitespace-only ``progress`` is "no payload", exactly as the
    wrapper reads it, and writes no file (FIX V-8).

    ``job_id`` may be :func:`idle_job_id` -- a worker that holds no claim
    still has to be able to say "I am here and idle", and there is no
    manifest to check ownership against. That is the one hole in
    ``claimed_or_die``, and it is a hole into a directory the key already
    owns: the worst a holder of the key can do with it is write its own
    status file under its own worker id."""
    root = Path(root)
    if job_id == idle_job_id(worker_id):
        _validate_worker_id(worker_id)
        validate_job_id(job_id)
        worker_dir = claimed_dir(root) / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
    else:
        worker_dir, _ = _claim_paths(root, job_id, worker_id)
    stamp = now()
    atomic_write_text(worker_dir / f"{job_id}{HEARTBEAT_SUFFIX}", stamp + "\n")
    # FIX V-8: only a payload that IS one. The wrapper writes nothing for an
    # empty or whitespace-only stdin (the refusal layer treats both as "no
    # payload"), and this mirror used to write the bytes anyway -- leaving an
    # empty ``<job>.progress.json`` whose row read as state `unknown` with a
    # JSONDecodeError. Nothing sends that shape, and now nothing can store it.
    if progress is not None and progress.strip(_PAYLOAD_WHITESPACE()) != b"":
        atomic_write_bytes(worker_dir / f"{job_id}{PROGRESS_SUFFIX}", progress)
    return stamp


def server_control_word(root: Path | str, worker_id: str, *, ttl_s: float | None = None) -> str:
    """The word the ``heartbeat`` verb prints: ``none``, ``pause``,
    ``resume`` or ``stop``.

    Thin on purpose -- the reading rules (the TTL, what a stale request
    means) live in :mod:`trialerror.offload.control`, imported lazily so this
    module stays the layout layer and nothing here depends on the semantics
    layer."""
    from trialerror.offload.control import control_word

    return control_word(root, worker_id, ttl_s=ttl_s)
