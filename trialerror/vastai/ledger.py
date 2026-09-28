"""The run ledger: the operator's record of every refusal, lease and shipment
(design section 9.5).

``worker_state_dir()/vastai/ledger.jsonl`` on DEV: append-only, one JSON
object per line, each line flushed and fsynced before :meth:`Ledger.append`
returns, under an inter-process lock (the worker, ``trialerror vastai reap``
and ``approve-ocr`` may append at the same time). A torn line -- a crash
mid-write -- is reported by :meth:`Ledger.read` in ``torn``, never silently
dropped, and the next append starts on a fresh line so it cannot glue itself
to the torn one.

Every row has ``kind`` and ``ts`` (UTC ISO). :data:`REQUIRED_FIELDS` names
what each kind must carry: the charter's fields (document sha256 and byte
size, instance id, host id, datacentre and verified flags, start, end,
estimated cost) plus what the spend view needs (lease id, worst case).

Spend: a lease costs its ``outcome`` row's ``estimated_cost_usd`` once
settled, and its ``intent`` row's ``worst_usd`` until then -- an unsettled
lease (in flight, or its process died) counts at its worst case.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from trialerror.offload.lock import worker_state_dir
from trialerror.vastai.errors import REASON_CODES, VastError, redact_secrets

__all__ = [
    "LEDGER_FILENAME",
    "LEDGER_KINDS",
    "REQUIRED_FIELDS",
    "OUTCOME_RESULTS",
    "FORBIDDEN_FIELDS",
    "LedgerError",
    "TornLine",
    "LedgerRead",
    "LeaseSpend",
    "SpendView",
    "Ledger",
    "default_state_dir",
    "ledger_path",
    "utc_iso",
    "lease_spend",
    "spent_under_approval",
    "spent_in_run",
    "spent_on_job",
    "unsettled_leases",
    "spend_view",
]

LEDGER_FILENAME = "ledger.jsonl"
LEDGER_KINDS = ("refused", "intent", "shipped", "outcome", "approval_minted", "reaped", "destroy_failed")

#: Fields each kind must carry (present, and not ``None`` unless noted in
#: :meth:`Ledger.append`).
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "refused": ("sha256", "bytes", "reason_code"),
    "intent": ("lease_id", "sha256", "bytes", "host_id", "datacenter", "verified", "worst_usd"),
    "shipped": ("lease_id", "instance_id", "start"),
    "outcome": ("lease_id", "instance_id", "end", "estimated_cost_usd", "result"),
    "approval_minted": ("nonce",),
    "reaped": ("instance_id", "reason"),
    "destroy_failed": ("instance_id",),
}

#: ``outcome.result``. ``not_created``: the create was refused (offer taken)
#: or failed before any instance existed as far as DEV knows.
OUTCOME_RESULTS = ("published", "failed", "returned", "expired", "not_created")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOCK_POLL_S = 0.02
#: Field names no row may carry: an approval's signature, key material.
FORBIDDEN_FIELDS = ("mac", "api_key")


class LedgerError(VastError):
    """A row was refused (unknown kind, a required field missing or
    malformed), or the ledger lock could not be taken."""


def default_state_dir() -> Path:
    """``worker_state_dir()/vastai``: the ledger and the run records live
    here, on DEV, never in the (read-only) backend-config-root."""
    return worker_state_dir() / "vastai"


def ledger_path(state_dir: Path | str | None = None) -> Path:
    return Path(state_dir if state_dir is not None else default_state_dir()) / LEDGER_FILENAME


def utc_iso(value: datetime | float | int | None = None) -> str:
    """UTC ISO-8601 with a ``Z``; ``value`` is a datetime or epoch seconds
    (default: now)."""
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromtimestamp(float(value), timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _file_lock(lock_path: Path, timeout_s: float) -> Iterator[None]:
    """An exclusive OS lock on ``lock_path``, waited for up to ``timeout_s``
    (``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere; both are
    released when the handle closes or the process dies)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LedgerError(
                        f"the vast.ai ledger lock {lock_path} is busy after {timeout_s:.0f} s",
                        next_actions=["retry; another worker, reap or approve-ocr run is appending"],
                    ) from None
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - closing the handle releases it anyway
                pass
    finally:
        os.close(fd)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate(kind: str, row: dict[str, Any]) -> None:
    if kind not in LEDGER_KINDS:
        raise LedgerError(f"ledger kind {kind!r} is not one of {', '.join(LEDGER_KINDS)}")
    secret = [f for f in FORBIDDEN_FIELDS if f in row]
    if secret:
        raise LedgerError(f"ledger {kind!r} row carries {', '.join(secret)}: an approval's mac or key material never "
                          "goes into the ledger")
    missing = [f for f in REQUIRED_FIELDS[kind] if f not in row]
    if missing:
        raise LedgerError(f"ledger {kind!r} row is missing {', '.join(missing)}")
    if kind in ("refused", "intent"):
        if not (isinstance(row["sha256"], str) and _SHA256_RE.match(row["sha256"])):
            raise LedgerError(f"ledger {kind!r} row: sha256 must be 64 lower-case hex characters")
        if not (isinstance(row["bytes"], int) and not isinstance(row["bytes"], bool) and row["bytes"] >= 0):
            raise LedgerError(f"ledger {kind!r} row: bytes must be a whole number >= 0")
    if kind == "refused" and row["reason_code"] not in REASON_CODES:
        raise LedgerError(f"ledger 'refused' row: reason_code {row['reason_code']!r} is not a vast.ai reason code")
    if kind == "intent":
        if row["host_id"] is None or row["lease_id"] in (None, ""):
            raise LedgerError("ledger 'intent' row: host_id and lease_id must be set")
        for flag in ("datacenter", "verified"):
            if not isinstance(row[flag], bool):
                raise LedgerError(f"ledger 'intent' row: {flag} must be true or false")
        if not (_is_number(row["worst_usd"]) and row["worst_usd"] >= 0):
            raise LedgerError("ledger 'intent' row: worst_usd must be a finite number >= 0")
    if kind == "shipped" and (row["instance_id"] is None or not row["start"]):
        raise LedgerError("ledger 'shipped' row: instance_id and start must be set")
    if kind == "outcome":
        if row["result"] not in OUTCOME_RESULTS:
            raise LedgerError(f"ledger 'outcome' row: result must be one of {', '.join(OUTCOME_RESULTS)}")
        if row["instance_id"] is None and row["result"] != "not_created":
            raise LedgerError("ledger 'outcome' row: instance_id may be null only when result = 'not_created'")
        if not row["end"]:
            raise LedgerError("ledger 'outcome' row: end must be set")
        if not (_is_number(row["estimated_cost_usd"]) and row["estimated_cost_usd"] >= 0):
            raise LedgerError("ledger 'outcome' row: estimated_cost_usd must be a finite number >= 0")
    if kind in ("reaped", "destroy_failed") and row["instance_id"] is None:
        raise LedgerError(f"ledger {kind!r} row: instance_id must be set")


@dataclass(frozen=True)
class TornLine:
    line_no: int
    text: str
    error: str


@dataclass(frozen=True)
class LedgerRead:
    path: Path
    rows: list[dict[str, Any]] = field(default_factory=list)
    torn: list[TornLine] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.torn


class Ledger:
    """``Ledger(state_dir)`` -- ``state_dir`` is the vast.ai state directory
    (default :func:`default_state_dir`). ``now`` is injectable for tests."""

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        lock_timeout_s: float = 30.0,
        secrets: Callable[[], Iterable[str | None]] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir if state_dir is not None else default_state_dir())
        self.path = ledger_path(self.state_dir)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock_timeout_s = float(lock_timeout_s)
        #: What :meth:`append` redacts besides the credential shapes: called
        #: at each append (the backend passes its key reader); a failure to
        #: read means the shapes only.
        self._secrets = secrets

    def _redacted(self, value: Any, secrets: tuple[str, ...]) -> Any:
        if isinstance(value, str):
            return redact_secrets(value, *secrets)
        if isinstance(value, dict):
            return {k: self._redacted(v, secrets) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redacted(v, secrets) for v in value]
        return value

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Validate, then append one row: flushed and fsynced before this
        returns. ``ts`` is added unless given. Returns the row written.

        Every string in the row is redacted first (the key, ``Bearer
        <anything>``, ``api_key=<anything>``): a second line of defence
        behind the client's own redaction. It redacts rather than refuses,
        so that a refusal row is still written."""
        row: dict[str, Any] = {"kind": kind, "ts": fields.pop("ts", None) or utc_iso(self._now())}
        row.update(fields)
        try:
            secrets = tuple(str(s) for s in (self._secrets() if self._secrets is not None else ()) if s)
        except Exception:  # noqa: BLE001 - an unreadable key: the shapes are still redacted
            secrets = ()
        row = {k: self._redacted(v, secrets) for k, v in row.items()}
        _validate(kind, row)
        try:
            line = json.dumps(row, ensure_ascii=False, allow_nan=False, default=str) + "\n"
        except ValueError as exc:
            raise LedgerError(f"ledger {kind!r} row is not serialisable: {exc}") from None
        data = line.encode("utf-8")
        with _file_lock(self.lock_path, self._lock_timeout_s):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file() and self.path.stat().st_size > 0:
                with open(self.path, "rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    if existing.read(1) != b"\n":
                        data = b"\n" + data  # a torn last line stays reported, and separate
            with open(self.path, "ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        return row

    def read(self) -> LedgerRead:
        if not self.path.is_file():
            return LedgerRead(self.path)
        data = self.path.read_bytes()
        rows: list[dict[str, Any]] = []
        torn: list[TornLine] = []
        lines = data.split(b"\n")
        for index, raw in enumerate(lines, start=1):
            if not raw.strip():
                continue
            last_unterminated = index == len(lines)  # no newline after it: a write that did not finish
            text = raw.decode("utf-8", "replace")
            try:
                row = json.loads(text)
            except ValueError as exc:
                torn.append(TornLine(index, text[:200], f"not JSON ({exc.msg})"))
                continue
            if not isinstance(row, dict) or row.get("kind") not in LEDGER_KINDS:
                torn.append(TornLine(index, text[:200], "not a ledger row (no known kind)"))
                continue
            if last_unterminated:
                torn.append(TornLine(index, text[:200], "last line has no newline (write interrupted)"))
                continue
            rows.append(row)
        return LedgerRead(self.path, rows, torn)


# ---------------------------------------------------------------------------
# spend
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LeaseSpend:
    lease_id: str
    usd: float
    settled: bool
    approval_nonce: str | None
    worker_run_id: str | None
    tier: str | None
    ts: str | None


def lease_spend(rows: Iterable[dict[str, Any]]) -> dict[str, LeaseSpend]:
    """Per lease id: its settled estimate (``outcome``) or its worst case
    (``intent`` without an outcome). Leases with no intent are not counted.

    An ``outcome`` whose destroy was NOT confirmed (``destroyed: false``)
    does not settle the lease: the instance may bill until its deadline, so
    it stays unsettled at ``max(worst_usd, estimate)`` until a ``reaped`` row
    names it (by lease/run id or instance id) or a later ``outcome`` of the
    same lease confirms the destroy."""
    intents: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    reaped_runs: set[str] = set()
    reaped_instances: set[str] = set()
    for r in rows:
        if r.get("kind") == "reaped":
            if r.get("run_id") is not None:
                reaped_runs.add(str(r["run_id"]))
            if r.get("instance_id") is not None:
                reaped_instances.add(str(r["instance_id"]))
            continue
        lease_id = r.get("lease_id")
        if lease_id is None:
            continue
        if r.get("kind") == "intent":
            intents[str(lease_id)] = r
        elif r.get("kind") == "outcome":
            outcomes[str(lease_id)] = r
    out: dict[str, LeaseSpend] = {}
    for lease_id, intent in intents.items():
        outcome = outcomes.get(lease_id)
        worst = float(intent.get("worst_usd") or 0.0)
        if outcome is not None and _is_number(outcome.get("estimated_cost_usd")):
            estimate = float(outcome["estimated_cost_usd"])
            unconfirmed = outcome.get("destroyed") is False and not (
                lease_id in reaped_runs
                or (outcome.get("instance_id") is not None and str(outcome["instance_id"]) in reaped_instances)
            )
            if unconfirmed:
                usd, settled = max(worst, estimate), False
            else:
                usd, settled = estimate, True
        else:
            usd, settled = worst, False
        out[lease_id] = LeaseSpend(
            lease_id=lease_id,
            usd=usd,
            settled=settled,
            approval_nonce=intent.get("approval_nonce"),
            worker_run_id=intent.get("worker_run_id"),
            tier=intent.get("tier"),
            ts=intent.get("ts"),
        )
    return out


def spent_under_approval(rows: Iterable[dict[str, Any]], nonce: str | None) -> float:
    if not nonce:
        return 0.0
    return sum(s.usd for s in lease_spend(rows).values() if s.approval_nonce == nonce)


def spent_in_run(rows: Iterable[dict[str, Any]], worker_run_id: str | None) -> float:
    if not worker_run_id:
        return 0.0
    return sum(s.usd for s in lease_spend(rows).values() if s.worker_run_id == worker_run_id)


def spent_on_job(rows: Iterable[dict[str, Any]], job_id: str | None, worker_run_id: str | None) -> float:
    """What one job has booked in this worker run: every lease of the job
    (its failovers included), unsettled ones at their worst case. Summed
    against ``[vastai] max_job_usd``, so the cap bounds the JOB, not each
    lease."""
    if not job_id or not worker_run_id:
        return 0.0
    rows = list(rows)
    jobs = {str(r.get("lease_id")): r.get("job_id") for r in rows if r.get("kind") == "intent"}
    return sum(
        s.usd for s in lease_spend(rows).values()
        if s.worker_run_id == worker_run_id and jobs.get(s.lease_id) == job_id
    )


def unsettled_leases(rows: Iterable[dict[str, Any]]) -> list[str]:
    return sorted(s.lease_id for s in lease_spend(rows).values() if not s.settled)


@dataclass(frozen=True)
class SpendView:
    """What has been spent (settled estimates + unsettled worst cases) in
    this worker run and under this approval."""

    run_usd: float = 0.0
    approval_usd: float = 0.0
    unsettled: tuple[str, ...] = ()
    #: This job's leases in this worker run (:func:`spent_on_job`); 0 when
    #: the view was built without a job.
    job_usd: float = 0.0


def spend_view(
    rows: Iterable[dict[str, Any]],
    *,
    approval_nonce: str | None,
    worker_run_id: str | None,
    job_id: str | None = None,
) -> SpendView:
    rows = list(rows)
    return SpendView(
        run_usd=spent_in_run(rows, worker_run_id),
        approval_usd=spent_under_approval(rows, approval_nonce),
        unsettled=tuple(unsettled_leases(rows)),
        job_usd=spent_on_job(rows, job_id, worker_run_id),
    )
