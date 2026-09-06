"""The offload queue: on-disk layout, manifests, and the verb semantics.

Design section 4, verbatim layout::

    <program_root>/offload/
      pending/<job_id>.json          manifest (offload_attempts inside)
      pending/<job_id>/              inputs: the raw file (ocr) or
                                     chunks.jsonl (embed: ALL chunks of the doc)
      claimed/<worker_id>/<job_id>.json + <job_id>.heartbeat + <job_id>/
      done/<job_id>/                 result.json + payload files + manifest.json
                                     (published by atomic rename from
                                      done/.partial/<job_id>/)
      failed/<job_id>/               error.json (terminal after max_attempts)

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
from typing import Any, Iterable

from trialerror.util.atomic import atomic_write_bytes, atomic_write_text
from trialerror.util.timeutil import now, parse

__all__ = [
    "OFFLOAD_DIRNAME",
    "MANIFEST_SCHEMA",
    "RESULT_SCHEMA",
    "ERROR_SCHEMA",
    "MANIFEST_FILENAME",
    "QUEUED_DIRNAME",
    "REJECTED_DIRNAME",
    "MAX_PAYLOAD_BYTES",
    "RESULT_FILENAME",
    "ERROR_FILENAME",
    "HEARTBEAT_SUFFIX",
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
    "discard_published",
    "list_pending",
    "list_claims",
    "list_done",
    "list_failed",
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
MANIFEST_FILENAME = "manifest.json"
RESULT_FILENAME = "result.json"
ERROR_FILENAME = "error.json"
HEARTBEAT_SUFFIX = ".heartbeat"

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
    whitespace, no shell metacharacters), and the two traversal names
    ``.``/``..`` the character class itself would otherwise allow."""
    if not isinstance(job_id, str) or not job_id:
        raise OffloadProtocolError("offload: empty job id")
    if job_id in (".", ".."):
        raise OffloadProtocolError(f"offload: refused job id {job_id!r} (path traversal)")
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
) -> dict[str, Any]:
    """Write one job's inputs and manifest into ``pending/``.

    Inputs land in ``pending/<job_id>/`` FIRST and the manifest -- the
    thing every other verb keys off -- last, so a crash mid-write leaves an
    orphan input directory (harmless, overwritten by the next attempt)
    rather than a manifest pointing at inputs that do not exist yet.
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
            if c.is_file():
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
    )


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
            for suffix in (".json", HEARTBEAT_SUFFIX):
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
        for manifest_path in sorted(worker.glob("*.json")):
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
    d = failed_dir(Path(root))
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


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
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "pending": len(pend),
        "claimed": len(claims),
        "done": len(list_done(root)),
        "failed": len(list_failed(root)),
        "pending_job_ids": pend,
        "claims": claims,
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
        reclaimed.append({**claim, "reclaimed_ts": now()})
    return reclaimed


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
    _rmtree(partial_dir(root) / job_id)


def server_heartbeat(root: Path | str, job_id: str, *, worker_id: str) -> str:
    root = Path(root)
    worker_dir, _ = _claim_paths(root, job_id, worker_id)
    stamp = now()
    atomic_write_text(worker_dir / f"{job_id}{HEARTBEAT_SUFFIX}", stamp + "\n")
    return stamp
