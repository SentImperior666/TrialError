"""One instance, one job batch: create -> run -> destroy (design section 5).

:class:`InstanceLease` is a context manager. ``__exit__`` destroys the
instance on EVERY exit path -- success, exception, ``KeyboardInterrupt``,
:class:`~trialerror.vastai.remote.LeaseExpired` -- and a watchdog timer
destroys it at the hard deadline even while the ``with`` body is still
running. The deadline is also written into the instance's vast.ai label, so
the independent reaper (``reaper.py``) can enforce it with no local state,
and into an in-instance dead man's switch (``onstart``).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from trialerror.util.atomic import atomic_write_text
from trialerror.vastai.api import VastApiError, VastClient
from trialerror.vastai.remote import LeaseExpired

__all__ = ["LABEL_PREFIX", "make_label", "parse_label", "runs_dir", "read_run_records", "InstanceLease"]

LABEL_PREFIX = "trialerror|"


def make_label(program_fp: str, run_id: str, deadline_epoch: float) -> str:
    return f"{LABEL_PREFIX}{program_fp[:12]}|{run_id}|{int(deadline_epoch)}"


def parse_label(label: str | None) -> dict[str, Any] | None:
    """``None`` when not a TrialError label; ``{"malformed": True}`` when it
    carries our prefix but cannot be parsed (the reaper destroys those)."""
    if not label or not str(label).startswith(LABEL_PREFIX):
        return None
    parts = str(label).split("|")
    try:
        _, prog, run_id, deadline = parts
        return {"program": prog, "run_id": run_id, "deadline_epoch": int(deadline)}
    except ValueError:
        return {"malformed": True, "label": label}


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
