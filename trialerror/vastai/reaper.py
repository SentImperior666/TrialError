"""``trialerror vastai reap``: the independent backstop. Two policies, each
for its own lane (round 3, the superset of the public TrialError copy's
embedding backend):

* :func:`reap` / :func:`classify` -- the embedding lane's, with the public
  copy's signature and policy (``trialerror vastai reap``): it destroys a
  TrialError-tagged instance past the deadline in its label (any program), one
  with a malformed label, and -- for this program's instances -- one with no
  run record, a finished run or a dead owner. The one addition (C-7): it also
  reads the OCR lane's run records (the worker's state directory) and never
  destroys an instance whose OCR lease is live.
* :func:`reap_ocr` / :func:`classify_ocr` -- the OCR lane's narrowed reaper
  (design section 11.2), used by the OCR worker and by
  ``trialerror vastai reap --ocr``, described below.

The OCR lane's reaper runs at worker start (before anything is claimed), at worker exit, and on a
schedule. It destroys an instance only when it belongs to THIS backend-config-
root -- its label's fingerprint is this root's, or one of this root's run
records names its instance id -- whose run id is the OCR lane's (``VOCR-``)
or one of this root's OCR run records names, and one of these holds:

* ``past_deadline``  -- now >= the deadline in its label (or record);
* ``no_run_record``  -- labelled as ours, but no run record of ours knows it;
* ``run_finished``   -- its record says it should already be gone;
* ``owner_dead``     -- its record's owner process on this host is not alive.

Everything else carrying a ``trialerror|`` label is REPORTED and left alone:
a foreign label past its deadline (``foreign_past_deadline``), a malformed one
(``foreign_malformed_label``) or a live one (``foreign_live``) may belong to
another TrialError programme on a shared account (C3), whose own reaper owns
it. Only the first two are logged. An instance of this root carrying another
lane's run id (the embedding lane's ``VAST-``) is reported too
(``other_lane_past_deadline``, logged, or ``other_lane_live``), never destroyed
here (V1-F07): the embedding lane's reaper judges it. Instances without our label and not named
by our records are not touched and not reported.

A create that failed without an instance id, whose deadline has passed and
whose label no listed instance carries, never produced a billing instance:
its record is settled as ``absent`` and the ledger gets a ``not_created``
outcome, so its worst case stops counting against the spend envelopes.
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
from trialerror.vastai.lease import (
    LIVE_RECORD_STATES,
    RUN_ID_PREFIX,
    UNCONFIRMED_CREATE_STATES,
    parse_label,
    read_run_records,
    read_state_run_records,
    record_for_instance,
    runs_dir,
    write_run_record,
)
from trialerror.vastai.ledger import Ledger, default_state_dir, utc_iso

__all__ = [
    "DESTROY_REASONS",
    "REPORT_REASONS",
    "pid_alive",
    "classify",
    "reap",
    "classify_ocr",
    "reap_ocr",
]

#: What :func:`reap_ocr` destroys for, and what it only reports.
DESTROY_REASONS = ("past_deadline", "no_run_record", "run_finished", "owner_dead")
REPORT_REASONS = (
    "foreign_past_deadline",
    "foreign_malformed_label",
    "foreign_live",
    "other_lane_past_deadline",
    "other_lane_live",
)

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


def _judge_record(rec: dict[str, Any], *, now_epoch: float, host: str, alive: Callable[[int], bool]) -> str | None:
    if now_epoch >= float(rec.get("deadline_epoch") or 0):
        return "past_deadline"
    if rec.get("status") not in LIVE_RECORD_STATES:
        return "run_finished"
    if rec.get("host") == host and not alive(int(rec.get("pid") or 0)):
        return "owner_dead"
    return None


def classify_ocr(
    inst: dict[str, Any],
    *,
    program_fp: str,
    records: dict[str, dict[str, Any]],
    now_epoch: float,
    host: str,
    alive: Callable[[int], bool] = pid_alive,
) -> tuple[str, str] | None:
    """The OCR lane's verdict: ``("destroy", reason)``, ``("report", reason)``
    or ``None`` (leave it, say nothing). ``records`` = THIS root's OCR run
    records by run id."""
    by_id = record_for_instance(inst, records.values())
    tag = parse_label(inst.get("label"))
    if tag is None or tag.get("malformed"):
        if by_id is not None:
            reason = _judge_record(by_id, now_epoch=now_epoch, host=host, alive=alive)
            return ("destroy", reason) if reason else None
        if tag is None:
            return None  # not a TrialError instance, and not ours: never touched
        return ("report", "foreign_malformed_label")
    if tag["program"] != program_fp[:12] and by_id is None:
        if now_epoch >= tag["deadline_epoch"]:
            return ("report", "foreign_past_deadline")
        return ("report", "foreign_live")  # another root's live, in-deadline lease
    rec = records.get(tag["run_id"]) or by_id
    if rec is None and not str(tag.get("run_id") or "").startswith(f"{RUN_ID_PREFIX}-"):
        # V1-F07: this root's label, another lane's run id (the embedding
        # lane's VAST-...), and no OCR record of ours: never destroyed here.
        return ("report", "other_lane_past_deadline" if now_epoch >= tag["deadline_epoch"] else "other_lane_live")
    if now_epoch >= tag["deadline_epoch"]:
        return ("destroy", "past_deadline")
    if rec is None:
        return ("destroy", "no_run_record")
    reason = _judge_record(rec, now_epoch=now_epoch, host=host, alive=alive)
    return ("destroy", reason) if reason else None


def reap_ocr(
    client: Any,
    *,
    config_root: Path | str,
    state_dir: Path | str | None = None,
    ledger: Ledger | None = None,
    dry_run: bool = False,
    clock: Callable[[], float] = time.time,
    alive: Callable[[int], bool] = pid_alive,
    host: str | None = None,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """The OCR lane's reap pass for the DEV root ``config_root``. Returns one entry per
    instance acted on or reported (``instance_id``, ``label``, ``run_id``,
    ``reason``, ``action`` in ``destroyed`` / ``would_destroy`` /
    ``destroy_failed`` / ``reported``, ``destroyed``), plus one per settled
    create failure (``action = "settled_absent"``). ``dry_run`` lists and
    judges but destroys, writes and appends nothing. Raises
    :class:`~trialerror.vastai.errors.VastApiError` when the account cannot
    be listed: a blind reaper must not pretend it found nothing."""
    config_root = Path(config_root)
    state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
    fp = program_fingerprint(config_root)
    host = host or socket.gethostname()
    log = log or (lambda m: print(m, file=sys.stderr))
    ledger = ledger if ledger is not None else Ledger(state_dir)
    records = {str(r.get("run_id")): r for r in read_state_run_records(state_dir, program_fp=fp)}
    now_epoch = clock()
    listed = client.list_instances()
    out: list[dict[str, Any]] = []
    for inst in listed:
        verdict = classify_ocr(inst, program_fp=fp, records=records, now_epoch=now_epoch, host=host, alive=alive)
        if verdict is None:
            continue
        action, reason = verdict
        tag = parse_label(inst.get("label")) or {}
        rec = records.get(str(tag.get("run_id"))) or record_for_instance(inst, records.values())
        entry: dict[str, Any] = {
            "instance_id": inst.get("id"),
            "label": inst.get("label"),
            "run_id": (rec or {}).get("run_id") or tag.get("run_id"),
            "reason": reason,
            "action": "reported" if action == "report" else ("would_destroy" if dry_run else "destroyed"),
            "destroyed": False,
        }
        if action == "report":
            if reason not in ("foreign_live", "other_lane_live"):
                log(f"! vast.ai instance {inst.get('id')} ({reason}) is not this root's -- reported, left alone")
        elif not dry_run:
            try:
                client.destroy_instance(int(inst["id"]))
                entry["destroyed"] = True
            except VastApiError as exc:
                entry["action"] = "destroy_failed"
                entry["error"] = str(exc)
        out.append(entry)

    if dry_run:
        return out

    destroyed = [e for e in out if e["destroyed"]]
    if destroyed:
        # Confirm by listing, as the lease does: DELETE accepted is not "gone".
        try:
            still = {str(i.get("id")) for i in client.list_instances()}
        except VastApiError as exc:
            still = None
            log(f"! vast.ai listing after reaping failed ({exc}); destroys are unconfirmed")
        for entry in destroyed:
            if still is None or str(entry["instance_id"]) in still:
                entry["destroyed"] = False
                entry["action"] = "destroy_failed"
                entry["error"] = "still listed after DELETE" if still is not None else "unconfirmed (listing failed)"
    for entry in out:
        if entry["action"] == "destroyed":
            ledger.append("reaped", instance_id=entry["instance_id"], reason=entry["reason"], run_id=entry["run_id"],
                          label=entry["label"])
            rec = records.get(str(entry["run_id"]))
            if rec is not None:
                rec.update(status="reaped", reaped_reason=entry["reason"], updated_epoch=int(now_epoch))
                write_run_record(state_dir, rec)
            log(f"= vast.ai instance {entry['instance_id']} reaped ({entry['reason']})")
        elif entry["action"] == "destroy_failed":
            ledger.append("destroy_failed", instance_id=entry["instance_id"], run_id=entry["run_id"],
                          error=entry.get("error"), label=entry["label"])
            log(f"!!! vast.ai instance {entry['instance_id']} could NOT be reaped ({entry.get('error')}); check the console")

    labels = {str(i.get("label")) for i in listed if i.get("label")}
    for rec in records.values():
        if (
            rec.get("status") in UNCONFIRMED_CREATE_STATES
            and rec.get("instance_id") is None
            and now_epoch >= float(rec.get("deadline_epoch") or 0)
            and str(rec.get("label")) not in labels
        ):
            rec.update(status="absent", absent_confirmed_epoch=int(now_epoch), updated_epoch=int(now_epoch))
            write_run_record(state_dir, rec)
            ledger.append(
                "outcome", lease_id=rec.get("lease_id") or rec.get("run_id"), instance_id=None,
                end=utc_iso(now_epoch), estimated_cost_usd=0.0, result="not_created",
                error="create failed; no instance with this lease's label on the account after its deadline",
            )
            out.append({"instance_id": None, "label": rec.get("label"), "run_id": rec.get("run_id"),
                        "reason": "create_failed_not_listed", "action": "settled_absent", "destroyed": False})
    return out


# ---------------------------------------------------------------------------
# The embedding lane's policy: the public TrialError copy's embedding backend,
# verbatim from here on, but for reap's one addition (the OCR lane's records).
# ---------------------------------------------------------------------------
def _live_ocr_lease(
    inst: dict[str, Any],
    ocr_records: dict[str, dict[str, Any]],
    *,
    now_epoch: float,
    host: str,
    alive: Callable[[int], bool],
) -> dict[str, Any] | None:
    """The OCR lane's run record showing ``inst`` as a live lease (in its
    deadline, running, its owner alive or on another host), or ``None``."""
    tag = parse_label(inst.get("label")) or {}
    rec = ocr_records.get(str(tag["run_id"])) if tag.get("run_id") else None
    rec = rec or record_for_instance(inst, ocr_records.values())
    if rec is None:
        return None
    return rec if _judge_record(rec, now_epoch=now_epoch, host=host, alive=alive) is None else None


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
        rec = record_for_instance(inst, records.values())
        if rec is None:
            return None  # not ours -- never touch
        # Unlabelled, but our own run record names it: judge it by the record.
        tag = {"deadline_epoch": float(rec.get("deadline_epoch") or 0), "program": program_fp[:12], "run_id": rec.get("run_id")}
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
    state_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """The public copy's reap pass for ``program_root``, with one addition
    (C-7): the OCR lane's run records (``state_dir``, the worker's state
    directory by default) are read too, and an instance one of them shows as
    a live OCR lease is never destroyed, whatever its label says."""
    program_root = Path(program_root)
    records = {r.get("run_id"): r for r in read_run_records(program_root)}
    fp = program_fingerprint(program_root)
    host = socket.gethostname()
    now_epoch = clock()
    ocr_records = {str(r.get("run_id")): r for r in read_state_run_records(state_dir)}
    out: list[dict[str, Any]] = []
    listed = client.list_instances()
    for inst in listed:
        reason = classify(inst, program_fp=fp, records=records, now_epoch=now_epoch, host=host, alive=alive)
        if reason is None:
            continue
        if _live_ocr_lease(inst, ocr_records, now_epoch=now_epoch, host=host, alive=alive) is not None:
            continue  # C-7: a live OCR lease is the OCR lane's to end
        entry = {"instance_id": inst.get("id"), "label": inst.get("label"), "reason": reason, "destroyed": False}
        if not dry_run:
            try:
                client.destroy_instance(int(inst["id"]))
                entry["destroyed"] = True
            except VastApiError as exc:
                entry["error"] = str(exc)
            tag = parse_label(inst.get("label")) or {}
            rec = records.get(tag.get("run_id")) or record_for_instance(inst, records.values())
            if rec is not None and entry["destroyed"]:
                rec.update(status="reaped", reaped_reason=reason, updated_epoch=int(now_epoch))
                atomic_write_text(runs_dir(program_root) / f"{rec['run_id']}.json", json.dumps(rec, indent=2))
        out.append(entry)
    if not dry_run:
        # A create that failed without an instance id, whose deadline has
        # passed and whose label is on no listed instance, never produced a
        # billing instance: settle its record so doctor stops alarming on it.
        labels = {str(i.get("label")) for i in listed if i.get("label")}
        for rec in records.values():
            if (
                rec.get("status") in UNCONFIRMED_CREATE_STATES
                and rec.get("instance_id") is None
                and now_epoch >= float(rec.get("deadline_epoch") or 0)
                and str(rec.get("label")) not in labels
            ):
                rec.update(status="absent", absent_confirmed_epoch=int(now_epoch), updated_epoch=int(now_epoch))
                atomic_write_text(runs_dir(program_root) / f"{rec['run_id']}.json", json.dumps(rec, indent=2))
    return out
