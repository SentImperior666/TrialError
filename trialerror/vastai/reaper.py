"""``trialerror vastai reap``: the independent backstop (design section 4.2 item 4).

Needs nothing from the process that rented the instance: every TrialError
instance's vast.ai label carries its own hard deadline. Destroys a tagged
instance when ANY of these holds:

* ``past_deadline``  -- now >= the deadline in its label (any program);
* ``malformed_label`` -- our prefix, unparseable label;
* for THIS program's instances (label fingerprint matches):
  ``no_run_record`` / ``run_finished`` (record says it should be gone) /
  ``owner_dead`` (record's PID on this host is not alive).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable

from trialerror.util.atomic import atomic_write_text
from trialerror.vastai.api import VastApiError, VastClient
from trialerror.vastai.guard import program_fingerprint
from trialerror.vastai.lease import parse_label, read_run_records, runs_dir

__all__ = ["pid_alive", "classify", "reap"]

_LIVE_RECORD_STATES = ("creating", "running")


def pid_alive(pid: int) -> bool:
    """True if ``pid`` exists on this host. Never signals the process
    (``os.kill(pid, 0)`` would TERMINATE it on Windows)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def classify(
    inst: dict[str, Any],
    *,
    program_fp: str,
    records: dict[str, dict[str, Any]],
    now_epoch: float,
    host: str,
    alive: Callable[[int], bool] = pid_alive,
) -> str | None:
    """The reason to destroy ``inst``, or ``None`` to leave it."""
    tag = parse_label(inst.get("label"))
    if tag is None:
        return None  # not ours -- never touch
    if tag.get("malformed"):
        return "malformed_label"
    if now_epoch >= tag["deadline_epoch"]:
        return "past_deadline"
    if tag["program"] != program_fp[:12]:
        return None  # another program's live, in-deadline lease
    rec = records.get(tag["run_id"])
    if rec is None:
        return "no_run_record"
    if rec.get("status") not in _LIVE_RECORD_STATES:
        return "run_finished"
    if rec.get("host") == host and not alive(int(rec.get("pid") or 0)):
        return "owner_dead"
    return None


def reap(
    client: VastClient,
    program_root: Path | str,
    *,
    dry_run: bool = False,
    clock: Callable[[], float] = time.time,
    alive: Callable[[int], bool] = pid_alive,
) -> list[dict[str, Any]]:
    program_root = Path(program_root)
    records = {r.get("run_id"): r for r in read_run_records(program_root)}
    fp = program_fingerprint(program_root)
    host = socket.gethostname()
    now_epoch = clock()
    out: list[dict[str, Any]] = []
    for inst in client.list_instances():
        reason = classify(inst, program_fp=fp, records=records, now_epoch=now_epoch, host=host, alive=alive)
        if reason is None:
            continue
        entry = {"instance_id": inst.get("id"), "label": inst.get("label"), "reason": reason, "destroyed": False}
        if not dry_run:
            try:
                client.destroy_instance(int(inst["id"]))
                entry["destroyed"] = True
            except VastApiError as exc:
                entry["error"] = str(exc)
            tag = parse_label(inst.get("label")) or {}
            rec = records.get(tag.get("run_id"))
            if rec is not None and entry["destroyed"]:
                rec.update(status="reaped", reaped_reason=reason, updated_epoch=int(now_epoch))
                atomic_write_text(runs_dir(program_root) / f"{rec['run_id']}.json", json.dumps(rec, indent=2))
        out.append(entry)
    return out
