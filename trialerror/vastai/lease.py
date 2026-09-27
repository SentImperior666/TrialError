"""One rented instance: create -> use -> destroy. Two lanes share this module
(round 3, the superset of the public TrialError copy's embedding backend):

* the OCR lane's :class:`OcrInstanceLease` (design sections 3 and 10), whose
  run records live in the worker's state directory
  (:func:`state_runs_dir`, :func:`read_state_run_records`);
* the embedding lane's :class:`InstanceLease`, ported verbatim from the
  public copy, whose run records live under the program root
  (:func:`runs_dir`, :func:`read_run_records`), where the public reaper and
  doctor read them.

Both raise the ONE :class:`LeaseExpired` defined here
(``trialerror.vastai.remote`` re-exports it).

:class:`OcrInstanceLease` is a context manager. ``__exit__`` destroys the
instance on EVERY exit path -- success, exception, ``KeyboardInterrupt``,
:class:`LeaseExpired` -- and a watchdog destroys it at the hard deadline even
while the ``with`` body is still running. The deadline is also written into
the instance's vast.ai label, so the independent reaper can enforce it with no
local state, and into an in-instance dead man's switch (``onstart``).

What changed from the public copy:

* **The label is byte-compatible**: ``trialerror|<fp12>|VOCR-<random>|<deadline>``,
  four ``|``-separated fields, so the public reaper's ``parse_label`` reads it
  as a well-formed, in-deadline, foreign label and leaves it alone on a shared
  account. The run id is random: nothing on the vast.ai side links an
  instance to a queue job; only DEV's ledger does.
* **Run records** live in the worker's state directory on DEV
  (``worker_state_dir()/vastai/runs``, injectable), because the
  backend-config-root is read-only by contract. Each record carries the full
  program fingerprint, so the reaper of one DEV root never judges another's.
* **The watchdog runs on an injectable clock**: a daemon thread polls
  :meth:`OcrInstanceLease.poll_watchdog` (tests call it directly).
* **Offers are tried in order.** An offer taken between search and create
  (``no_such_ask``) rents nothing; the next ranked offer is tried, at most
  :data:`MAX_OFFER_ATTEMPTS`. Each attempt is its own lease id, with its own
  ledger ``intent`` row (written and fsynced BEFORE the create) and, when the
  offer was taken, an ``outcome`` row with ``result = "not_created"``.
* The lease records ``machine_id`` and ``host_id`` so a failover can exclude
  the machine.
* When the watchdog fired, ``__exit__`` turns whatever the body raised into
  :class:`LeaseExpired`: an expiry is class X (stop spending) and must not be
  mistaken for a host failure that would rent another host.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from trialerror.util.atomic import atomic_write_text
from trialerror.vastai.api import VastClient
from trialerror.vastai.errors import HostFailure, OfferUnavailable, VastApiError, VastPlanRefused
from trialerror.vastai.guard import program_fingerprint
from trialerror.vastai.ledger import Ledger, default_state_dir, utc_iso

__all__ = [
    "LABEL_PREFIX",
    "RUN_ID_PREFIX",
    "CLEAN_RECORD_STATES",
    "UNCONFIRMED_CREATE_STATES",
    "LIVE_RECORD_STATES",
    "MAX_OFFER_ATTEMPTS",
    "DESTROY_ATTEMPTS",
    "LeaseExpired",
    "new_run_id",
    "make_label",
    "parse_label",
    "state_runs_dir",
    "read_state_run_records",
    "write_run_record",
    "record_for_instance",
    "OcrInstanceLease",
    # the embedding lane's: the public copy's names and signatures
    "runs_dir",
    "read_run_records",
    "InstanceLease",
]

LABEL_PREFIX = "trialerror|"
RUN_ID_PREFIX = "VOCR"

#: Run-record states in which no instance of the run can be billing:
#: destroyed and confirmed, reaped, refused at create (``offer_taken``), or a
#: create failure a later live listing showed never produced an instance.
CLEAN_RECORD_STATES = ("destroyed", "reaped", "offer_taken", "absent")
#: States with no instance id yet: the create may or may not have landed
#: server-side. Only a successful live listing settles them.
UNCONFIRMED_CREATE_STATES = ("creating", "create_failed")
#: States of a lease that should still be running.
LIVE_RECORD_STATES = ("creating", "running")

MAX_OFFER_ATTEMPTS = 5
DESTROY_ATTEMPTS = 5


class LeaseExpired(KeyboardInterrupt):
    """The TTL watchdog destroyed the instance mid-job.

    A ``KeyboardInterrupt`` subclass ON PURPOSE: the offload worker answers an
    interrupt by RETURNING the claim unrun (no attempt burned -- a sizing miss
    is not a GPU fault) and ending the run loudly, and it keeps ``except
    Exception`` blocks from swallowing the expiry."""


def new_run_id() -> str:
    """``VOCR-<16 hex>``: random, never derived from a job or document."""
    return f"{RUN_ID_PREFIX}-{secrets.token_hex(8)}"


def make_label(program_fp: str, run_id: str, deadline_epoch: float) -> str:
    return f"{LABEL_PREFIX}{program_fp[:12]}|{run_id}|{int(deadline_epoch)}"


def parse_label(label: str | None) -> dict[str, Any] | None:
    """``None`` when not a TrialError label; ``{"malformed": True}`` when it
    carries our prefix but cannot be parsed. (The public copy's reaper
    destroys malformed labels; this lane's reaper only reports them.)"""
    if not label or not str(label).startswith(LABEL_PREFIX):
        return None
    parts = str(label).split("|")
    try:
        _, prog, run_id, deadline = parts
        return {"program": prog, "run_id": run_id, "deadline_epoch": int(deadline)}
    except ValueError:
        return {"malformed": True, "label": label}


def state_runs_dir(state_dir: Path | str | None = None) -> Path:
    """Where the OCR lane's run records live: ``<state_dir>/runs`` (the
    worker's state directory by default)."""
    return Path(state_dir if state_dir is not None else default_state_dir()) / "runs"


def read_state_run_records(
    state_dir: Path | str | None = None, *, program_fp: str | None = None
) -> list[dict[str, Any]]:
    """Every run record (unreadable files skipped), or only those of the DEV
    root whose full fingerprint is ``program_fp``."""
    d = state_runs_dir(state_dir)
    out: list[dict[str, Any]] = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            if program_fp is not None and rec.get("program_fp") != program_fp:
                continue
            out.append(rec)
    return out


def write_run_record(state_dir: Path | str | None, record: Mapping[str, Any]) -> Path:
    d = state_runs_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{record['run_id']}.json"
    atomic_write_text(path, json.dumps(dict(record), indent=2, default=str))
    return path


def record_for_instance(inst: Mapping[str, Any], records: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The run record naming ``inst`` by instance id, if any. The label is
    the primary tag, but whether vast.ai echoes it back is unverified; the id
    in our own record is ours too."""
    iid = inst.get("id")
    if iid is None:
        return None
    for rec in records:
        if rec.get("instance_id") is not None and str(rec.get("instance_id")) == str(iid):
            return dict(rec)
    return None


def _default_log(message: str) -> None:
    print(message, file=sys.stderr)


class OcrInstanceLease:
    """The OCR lane's lease. Rent one of ``offers`` (ranked ``OfferEstimate`` objects from
    :func:`trialerror.vastai.pricing.price_job`, tried in order), hold it
    for the ``with`` body, destroy it on exit.

    ``intent`` is the job's part of every ledger ``intent`` row (``job_id``,
    ``doc_id``, ``sha256``, ``bytes``, ``license_tier``, ``approval_nonce``,
    ``worker_run_id``, ``tier`` ...); the lease adds the lease id, the
    offer's fields, the worst case and the deadline. Without a ``ledger`` no
    rows are written (tests of the lease alone)."""

    def __init__(
        self,
        client: Any,
        *,
        config_root: Path | str,
        offers: Sequence[Any],
        image: str,
        disk_gb: int,
        state_dir: Path | str | None = None,
        ledger: Ledger | None = None,
        intent: Mapping[str, Any] | None = None,
        extra_record: Mapping[str, Any] | None = None,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        destroy_attempts: int = DESTROY_ATTEMPTS,
        max_offer_attempts: int = MAX_OFFER_ATTEMPTS,
        watchdog: bool = True,
        watchdog_interval_s: float = 1.0,
        run_id_factory: Callable[[], str] = new_run_id,
    ) -> None:
        if not offers:
            raise ValueError("OcrInstanceLease needs at least one priced offer")
        self.client = client
        self.config_root = Path(config_root)
        self.program_fp = program_fingerprint(self.config_root)
        self.offers = list(offers)
        self.image = image
        self.disk_gb = int(disk_gb)
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.ledger = ledger
        self.intent = dict(intent or {})
        self.extra_record = dict(extra_record or {})
        self.log = log or _default_log
        self.clock = clock
        self.sleep = sleep
        self.destroy_attempts = int(destroy_attempts)
        self.max_offer_attempts = int(max_offer_attempts)
        self.watchdog = bool(watchdog)
        self.watchdog_interval_s = float(watchdog_interval_s)
        self._run_id_factory = run_id_factory

        self.estimate: Any = self.offers[0]
        self.run_id: str | None = None
        self.label: str | None = None
        self.deadline_epoch: float = 0.0
        self.instance_id: int | None = None
        self.created_epoch: float | None = None
        self.destroy_confirmed_epoch: float | None = None
        self.expired = False
        self.destroyed = False
        self.destroy_error: str | None = None
        self.offers_taken: list[Any] = []
        self.on_expire: Callable[[], None] | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._record: dict[str, Any] = {}

    # -- what the rest of the backend reads ----------------------------------
    @property
    def lease_id(self) -> str | None:
        return self.run_id

    @property
    def offer(self) -> dict[str, Any]:
        return dict(self.estimate.offer)

    @property
    def ttl_s(self) -> float:
        return float(self.estimate.ttl_s)

    @property
    def dph(self) -> float:
        return float(self.estimate.dph)

    @property
    def machine_id(self) -> Any:
        return self.offer.get("machine_id")

    @property
    def host_id(self) -> Any:
        return self.offer.get("host_id")

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.deadline_epoch - self.clock())

    def onstart_script(self) -> str:
        # Dead man's switch [assumption, unverified live]: stop the container
        # shortly after the deadline even if every DEV process is gone.
        return f"nohup sh -c 'sleep {int(self.ttl_s) + 120}; kill -TERM 1' >/dev/null 2>&1 &"

    # -- run record (what the reaper and the doctor read) --------------------
    def _write(self, status: str, **extra: Any) -> None:
        self._record.update(
            status=status, instance_id=self.instance_id, updated_epoch=int(self.clock()), **extra
        )
        write_run_record(self.state_dir, self._record)

    def _begin_attempt(self, estimate: Any) -> None:
        self.estimate = estimate
        self.run_id = self._run_id_factory()
        self.deadline_epoch = self.clock() + float(estimate.ttl_s)
        self.label = make_label(self.program_fp, self.run_id, self.deadline_epoch)
        self.instance_id = None
        offer = dict(estimate.offer)
        self._record = {
            "run_id": self.run_id,
            "lease_id": self.run_id,
            "program_fp": self.program_fp,
            "program": self.program_fp[:12],
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "label": self.label,
            "deadline_epoch": int(self.deadline_epoch),
            "ttl_s": float(estimate.ttl_s),
            "offer_id": offer.get("id"),
            "machine_id": offer.get("machine_id"),
            "host_id": offer.get("host_id"),
            "gpu_name": offer.get("gpu_name"),
            "dph": float(estimate.dph),
            "worst_usd": float(estimate.worst_usd),
            **self.extra_record,
        }

    def _ledger(self, kind: str, **fields: Any) -> None:
        if self.ledger is not None:
            self.ledger.append(kind, **fields)

    @property
    def record(self) -> dict[str, Any]:
        """A copy of the current run record."""
        return dict(self._record)

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "OcrInstanceLease":
        self._create()
        self._start_watchdog()
        return self

    def _create(self) -> None:
        tried = 0
        for estimate in self.offers[: self.max_offer_attempts]:
            tried += 1
            self._begin_attempt(estimate)
            # Ledger 'intent' (fsync) and the run record BEFORE the create:
            # a crash after this point leaves a record the reaper can act on.
            self._ledger(
                "intent",
                **{
                    **self.intent,
                    **estimate.intent_fields(),
                    "lease_id": self.run_id,
                    "label": self.label,
                    "deadline": utc_iso(self.deadline_epoch),
                },
            )
            self._write("creating")
            try:
                self.instance_id = self.client.create_instance(
                    estimate.offer["id"], image=self.image, disk_gb=self.disk_gb, label=self.label,
                    onstart=self.onstart_script(),
                )
            except OfferUnavailable as exc:
                # vast.ai refused the create outright: nothing exists, nothing bills.
                self._write("offer_taken", error=f"{type(exc).__name__}: {exc}")
                self._ledger(
                    "outcome", lease_id=self.run_id, instance_id=None, end=utc_iso(self.clock()),
                    estimated_cost_usd=0.0, result="not_created", error="offer taken before create (no_such_ask)",
                )
                self.offers_taken.append(estimate.offer.get("id"))
                self.log(f"! vast.ai offer {estimate.offer.get('id')} was taken before create; nothing rented")
                continue
            except VastApiError as exc:
                # The create may have landed server-side before the error
                # reached us; the label carries the deadline and the reaper
                # settles it by listing.
                self._write("create_failed", error=f"{type(exc).__name__}: {exc}")
                self.log(f"! vast.ai create failed ({exc}); `trialerror vastai reap --ocr` sweeps any half-created instance")
                raise VastPlanRefused(
                    "create-failed",
                    f"vast.ai create on offer {estimate.offer.get('id')} failed ({exc}); the document was not sent.",
                    next_actions=["retry later", "run `trialerror vastai reap --ocr` if an instance appears on the account"],
                    details={"lease_id": self.run_id, "status": exc.status},
                ) from exc
            except BaseException as exc:
                self._write("create_failed", error=f"{type(exc).__name__}: {exc}")
                raise
            self.created_epoch = self.clock()
            self._write("running", created_epoch=int(self.created_epoch))
            self.log(f"= vast.ai lease {self.run_id}: instance {self.instance_id} created, deadline {utc_iso(self.deadline_epoch)}")
            return
        raise VastPlanRefused(
            "no-offer",
            f"every ranked vast.ai offer tried ({tried}) was taken between search and create; nothing was rented "
            "and the document was not sent.",
            next_actions=["retry later (market state, not the document)"],
            details={"offers_taken": list(self.offers_taken)},
        )

    def _start_watchdog(self) -> None:
        if not self.watchdog:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name=f"vastai-lease-{self.run_id}", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        while not self._stop.wait(self.watchdog_interval_s):
            if self.poll_watchdog():
                return

    def poll_watchdog(self) -> bool:
        """Destroy the instance if the clock has reached the deadline.
        Returns whether the lease has expired. Safe from any thread."""
        if self.expired:
            return True
        if self.instance_id is None or self.destroyed:
            return False
        if self.clock() >= self.deadline_epoch:
            self._expire()
            return True
        return False

    def _expire(self) -> None:
        with self._lock:
            if self.expired:
                return
            self.expired = True
        self.log(
            f"! vast.ai lease {self.run_id}: TTL of {self.ttl_s:.0f} s reached -- destroying instance "
            f"{self.instance_id} NOW (if this repeats, raise [vastai.ocr] startup_s or safety)"
        )
        try:
            if self.on_expire is not None:
                self.on_expire()
        except Exception as exc:  # noqa: BLE001 - the destroy below must still run
            self.log(f"! vast.ai lease {self.run_id}: on_expire hook failed: {exc}")
        finally:
            self.destroy()

    def check(self) -> None:
        """Raise :class:`LeaseExpired` once the deadline has passed."""
        if self.expired or self.clock() >= self.deadline_epoch:
            self.expired = True
            raise LeaseExpired(f"vast.ai lease {self.run_id} TTL reached")

    def wait_ready(self, poll_interval_s: float = 10.0, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Poll until the instance is ``running`` with an ssh endpoint.
        Raises :class:`HostFailure` if it enters ``exited``/``offline``/
        ``error`` or is not running within ``timeout_s``; the TTL check runs
        every round."""
        start = self.clock()
        last_error = ""
        while True:
            self.check()
            try:
                inst = self.client.show_instance(self.instance_id)
            except VastApiError as exc:
                inst, last_error = None, str(exc)
            if inst is not None:
                status = str(inst.get("actual_status") or "")
                if status == "running" and inst.get("ssh_host") and inst.get("ssh_port"):
                    return inst
                if status in ("exited", "offline", "error"):
                    raise HostFailure(
                        f"vast.ai instance {self.instance_id} entered state {status!r} before it was ready",
                        machine_id=self.machine_id, instance_id=self.instance_id,
                    )
            if timeout_s is not None and self.clock() - start >= timeout_s:
                raise HostFailure(
                    f"vast.ai instance {self.instance_id} not running after {timeout_s:.0f} s"
                    + (f" (last listing error: {last_error})" if last_error else ""),
                    machine_id=self.machine_id, instance_id=self.instance_id,
                )
            self.sleep(poll_interval_s)

    def destroy(self) -> bool:
        """Idempotent; retried; confirmed by reading the instance list."""
        with self._lock:
            if self.destroyed or self.instance_id is None:
                return self.destroyed
            last = None
            for attempt in range(self.destroy_attempts):
                try:
                    self.client.destroy_instance(self.instance_id)
                    if self.client.show_instance(self.instance_id) is None:
                        self.destroyed = True
                        self.destroy_confirmed_epoch = self.clock()
                        self.destroy_error = None
                        self._write(
                            "destroyed", expired=self.expired, destroy_confirmed_epoch=int(self.destroy_confirmed_epoch)
                        )
                        self.log(f"= vast.ai instance {self.instance_id} destroyed")
                        return True
                    last = "still listed after DELETE"
                except VastApiError as exc:
                    last = str(exc)
                if attempt + 1 < self.destroy_attempts:
                    self.sleep(min(2.0 ** attempt, 30.0))
            self.destroy_error = last
            self._write("destroy_failed", error=last)
            try:
                self._ledger("destroy_failed", instance_id=self.instance_id, lease_id=self.run_id, error=last,
                             label=self.label)
            except Exception as exc:  # noqa: BLE001 - the loud message below matters more
                self.log(f"! vast.ai ledger row for the failed destroy could not be written: {exc}")
            self.log(
                f"!!! vast.ai instance {self.instance_id} could NOT be confirmed destroyed ({last}). "
                "It may still be billing. Run `trialerror vastai reap` and check the vast.ai console NOW."
            )
            return False

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        self.destroy()
        if self.expired and exc is not None and not isinstance(exc, KeyboardInterrupt):
            raise LeaseExpired(
                f"vast.ai lease {self.run_id} TTL reached; instance destroyed mid-job ({type(exc).__name__}: {exc})"
            ) from exc
        return False

    # -- the outcome ---------------------------------------------------------
    def elapsed_s(self) -> float:
        """Seconds from create to destroy-confirmed (or to now)."""
        if self.created_epoch is None:
            return 0.0
        end = self.destroy_confirmed_epoch if self.destroy_confirmed_epoch is not None else self.clock()
        return max(0.0, end - self.created_epoch)

    def estimated_cost_usd(self, *, down_bytes: int = 0, up_bytes: int = 0) -> float:
        """The offer's effective price over create..destroy-confirmed, plus
        bandwidth at its $/GB. vast.ai's invoice is authoritative."""
        est = self.estimate
        return (
            self.dph * self.elapsed_s() / 3600.0
            + down_bytes / 1e9 * float(getattr(est, "inet_down_cost", 0.0) or 0.0)
            + up_bytes / 1e9 * float(getattr(est, "inet_up_cost", 0.0) or 0.0)
        )

    def outcome_fields(self, *, down_bytes: int = 0, up_bytes: int = 0) -> dict[str, Any]:
        """The lease's part of the ledger ``outcome`` row; the backend adds
        ``result`` and the OCR numbers."""
        end = self.destroy_confirmed_epoch if self.destroy_confirmed_epoch is not None else self.clock()
        return {
            "lease_id": self.run_id,
            "instance_id": self.instance_id,
            "end": utc_iso(end),
            "destroy_confirmed": utc_iso(self.destroy_confirmed_epoch) if self.destroy_confirmed_epoch else None,
            "destroyed": self.destroyed,
            "expired": self.expired,
            "machine_id": self.machine_id,
            "host_id": self.host_id,
            "gpu_name": self.offer.get("gpu_name"),
            "dph": round(self.dph, 6),
            "elapsed_s": round(self.elapsed_s(), 1),
            "estimated_cost_usd": round(self.estimated_cost_usd(down_bytes=down_bytes, up_bytes=up_bytes), 6),
        }


# ---------------------------------------------------------------------------
# The embedding lane's lease: the public TrialError copy's embedding backend,
# verbatim from here to the end of the module. Its run records live under the
# program root; the LeaseExpired it raises is the one above.
# ---------------------------------------------------------------------------
def runs_dir(program_root: Path | str) -> Path:
    return Path(program_root) / "offload" / "vastai" / "runs"


def read_run_records(program_root: Path | str) -> list[dict[str, Any]]:
    d = runs_dir(program_root)
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


class InstanceLease:
    def __init__(
        self,
        client: VastClient,
        *,
        program_root: Path,
        program_fp: str,
        run_id: str,
        offer: dict[str, Any],
        ttl_s: float,
        image: str,
        disk_gb: int,
        extra_record: dict[str, Any] | None = None,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        destroy_attempts: int = 5,
    ):
        self.client = client
        self.program_root = Path(program_root)
        self.run_id = run_id
        self.offer = offer
        self.ttl_s = float(ttl_s)
        self.image = image
        self.disk_gb = disk_gb
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self.clock = clock
        self.sleep = sleep
        self.destroy_attempts = destroy_attempts
        self.deadline_epoch = clock() + self.ttl_s
        self.label = make_label(program_fp, run_id, self.deadline_epoch)
        self.instance_id: int | None = None
        self.expired = False
        self.destroyed = False
        self.destroy_error: str | None = None
        self.on_expire: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._record: dict[str, Any] = {
            "run_id": run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "label": self.label,
            "deadline_epoch": int(self.deadline_epoch),
            "ttl_s": self.ttl_s,
            "offer_id": offer.get("id"),
            "gpu_name": offer.get("gpu_name"),
            "dph_total": offer.get("dph_total"),
            **(extra_record or {}),
        }

    # -- run record (what the reaper and doctor read) ---------------------
    def _write(self, status: str, **extra: Any) -> None:
        self._record.update(status=status, instance_id=self.instance_id, updated_epoch=int(self.clock()), **extra)
        d = runs_dir(self.program_root)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / f"{self.run_id}.json", json.dumps(self._record, indent=2))

    def onstart_script(self) -> str:
        # Dead man's switch [assumption, unverified live]: stop the container
        # shortly after the deadline even if every local process is gone.
        return f"nohup sh -c 'sleep {int(self.ttl_s) + 120}; kill -TERM 1' >/dev/null 2>&1 &"

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "InstanceLease":
        self._write("creating")
        try:
            self.instance_id = self.client.create_instance(
                self.offer["id"], image=self.image, disk_gb=self.disk_gb, label=self.label, onstart=self.onstart_script()
            )
        except OfferUnavailable as exc:
            # vast.ai refused the create outright: no instance exists and
            # nothing bills, so the record is terminal and clean.
            self._write("offer_taken", error=f"{type(exc).__name__}: {exc}")
            self.log(f"! vast.ai offer {self.offer.get('id')} was taken before create; nothing rented")
            raise
        except BaseException as exc:
            # The create may have succeeded server-side before the error
            # reached us; the label carries the deadline, so the reaper
            # will find it. Say so.
            self._write("create_failed", error=f"{type(exc).__name__}: {exc}")
            self.log(f"! vast.ai create failed ({exc}); run `trialerror vastai reap` to sweep any half-created instance")
            raise
        self._write("running")
        remaining = max(0.0, self.deadline_epoch - self.clock())
        self._timer = threading.Timer(remaining, self._expire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def _expire(self) -> None:
        self.expired = True
        self.log(f"! vast.ai lease {self.run_id}: TTL of {self.ttl_s:.0f} s reached -- destroying instance {self.instance_id} NOW")
        try:
            if self.on_expire is not None:
                self.on_expire()
        finally:
            self.destroy()

    def check(self) -> None:
        if self.expired or self.clock() >= self.deadline_epoch:
            self.expired = True
            raise LeaseExpired(f"vast.ai lease {self.run_id} TTL reached")

    def wait_ready(self, poll_interval_s: float = 10.0) -> dict[str, Any]:
        while True:
            self.check()
            inst = self.client.show_instance(self.instance_id)
            if inst is not None:
                status = str(inst.get("actual_status") or "")
                if status == "running" and inst.get("ssh_host") and inst.get("ssh_port"):
                    return inst
                if status in ("exited", "offline", "error"):
                    raise RuntimeError(f"vast.ai instance {self.instance_id} entered state {status!r} before it was ready")
            self.sleep(poll_interval_s)

    def destroy(self) -> bool:
        """Idempotent; retried; confirmed by reading the instance list."""
        with self._lock:
            if self.destroyed or self.instance_id is None:
                return self.destroyed
            last = None
            for attempt in range(self.destroy_attempts):
                try:
                    self.client.destroy_instance(self.instance_id)
                    if self.client.show_instance(self.instance_id) is None:
                        self.destroyed = True
                        self._write("destroyed", expired=self.expired)
                        self.log(f"= vast.ai instance {self.instance_id} destroyed")
                        return True
                    last = "still listed after DELETE"
                except VastApiError as exc:
                    last = str(exc)
                self.sleep(min(2.0 ** attempt, 30.0))
            self.destroy_error = last
            self._write("destroy_failed", error=last)
            self.log(
                f"!!! vast.ai instance {self.instance_id} could NOT be confirmed destroyed ({last}). "
                "It may still be billing. Run `trialerror vastai reap` and check the vast.ai console NOW."
            )
            return False

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._timer is not None:
            self._timer.cancel()
        self.destroy()
        return False
