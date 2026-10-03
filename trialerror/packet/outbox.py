"""The container-to-host outbox (design §5, part D). Today the sender lives
on the host and its secret never enters the container, so the custodian
announces every packet by hand from DEV. With ``[packet] outbox = true`` and
no ``notify_cmd`` configured, ``push``/``remind`` (``trialerror/packet/
build.py``) queue a notification file here instead of refusing -- a small
host job (``deploy/sandbox/containment/te-outbox.sh``) sends it and writes
back a receipt. The secret stays on the host; the container never sees it,
and never controls what actually gets sent (D2: "the host job trusts
nothing in it, and enforces its own limits").

Queued is not sent (design §6 trap 3): only a receipt with ``delivered:
true`` appends a ``sent.jsonl`` row, and the 24-hour/one-push-per-packet
limits count every PENDING entry too, so a second push cannot queue a
duplicate while the first is still waiting on the host.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trialerror.packet.store import (
    PacketError,
    PacketSettings,
    append_jsonl,
    locked,
    parse_ts,
    read_jsonl,
    utc_iso,
)

__all__ = [
    "queue_notification",
    "pending_entries",
    "missing_receipt_entries",
    "reconcile_receipts",
    "notification_status",
    "MISSING_RECEIPT_AFTER_MIN",
]

#: design §5 D1: "a missing receipt is reported" past this age.
MISSING_RECEIPT_AFTER_MIN = 15
_FAILURE_WORDS = {1: "the push failed", 2: "no channel is configured on the host"}


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


#: N-7: the host's own NAME_RE (te-outbox.sh) is ASCII-only
#: (``[0-9TZ_A-Za-z-]``); a component built from Unicode alphanumerics could
#: mint a filename the host never sweeps, which later reads as "the host job
#: may not be running" instead of the truth. Restrict to exactly that set.
_SAFE_STAMP_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_stamp_component(text: str) -> str:
    return _SAFE_STAMP_RE.sub("_", text) or "x"


def queue_notification(
    settings: PacketSettings,
    *,
    kind: str,
    packet_id: str,
    title: str,
    body: str,
    trigger: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one outbox entry and return it. ``kind`` is ``"push"`` or
    ``"reminder"``. Filename ``<utc ts>_<packet id>_<kind>.json`` (design §5
    D1), written atomically (tmp, then rename) -- its ``id`` field is the
    filename stem, so the host script never has to mint or coordinate a
    second id of its own. ``trigger`` (design §4/§5, review finding B-2) is
    carried through to the ``sent.jsonl`` row a delivered receipt writes, so
    a packet delivered through the outbox still starts the weekly-limit
    cooldown -- without it, only ``trigger == "weekly_limit"`` rows count
    for that, and every outbox-delivered packet left it blank."""
    if kind not in ("push", "reminder", "alert"):
        raise PacketError("bad_input", f"outbox kind must be 'push', 'reminder' or 'alert', got {kind!r}")
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    entry_id = f"{stamp}_{_safe_stamp_component(packet_id)}_{kind}"
    entry = {
        "id": entry_id, "kind": kind, "packet_id": packet_id, "title": title, "body": body,
        "trigger": trigger, "created_ts": utc_iso(now),
    }
    _write_atomic_json(settings.outbox_dir / f"{entry_id}.json", entry)
    return entry


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def pending_entries(settings: PacketSettings) -> list[dict[str, Any]]:
    """Every queued entry with no receipt yet -- design §5 D1: "the limits
    count queued entries. A pending entry ... counts as a push for the
    24-hour limit and for 'one push per packet'." """
    outbox_dir = settings.outbox_dir
    if not outbox_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(outbox_dir.glob("*.json")):
        entry = _read_json_object(path)
        if entry is None or not isinstance(entry.get("id"), str):
            continue
        if (settings.outbox_receipts / path.name).is_file():
            continue
        out.append(entry)
    return out


def missing_receipt_entries(settings: PacketSettings, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Pending entries older than :data:`MISSING_RECEIPT_AFTER_MIN`."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=MISSING_RECEIPT_AFTER_MIN)
    out = []
    for entry in pending_entries(settings):
        try:
            created = parse_ts(entry["created_ts"])
        except (KeyError, PacketError):
            continue
        if created < cutoff:
            out.append(entry)
    return out


def reconcile_receipts(settings: PacketSettings, *, now: datetime | None = None) -> dict[str, Any]:
    """Read every receipt under ``outbox/receipts/`` (design §5 D1, called
    "at the start of every packet verb"). A delivered receipt appends its
    ``sent.jsonl`` row, once per id (an ``outbox_id`` field on the row is
    the de-dup key, so a receipt read twice across two runs never appends
    twice), carrying the queue entry's own ``trigger`` (B-2) so a packet
    delivered through the outbox still starts the weekly-limit cooldown; a
    failed one is left for :func:`notification_status` to report in words.
    Tolerant of anything malformed -- a receipt the container could have
    forged (design §5 D2 item 4) must never crash a packet verb, only fool
    the container's own counters. N-5: the ``sent.jsonl`` append happens
    under the packet lock, so two concurrent packet verbs cannot both see
    the same receipt as unreconciled and double-append it."""
    now = now or datetime.now(timezone.utc)
    receipts_dir = settings.outbox_receipts
    # N-c (review fix check, 2026-09-29): "receipts" gives a caller (L9's
    # check_credit_risk) each id's own receipt ts/exit_code, so a delivered
    # trigger's sent_ts can come from the receipt's own time, and a failed
    # one can record its real code -- never the run's own clock or a bare
    # boolean. Additive: delivered/failed stay plain id lists for every
    # existing caller.
    result: dict[str, Any] = {"delivered": [], "failed": [], "receipts": {}}
    if not receipts_dir.is_dir():
        return result
    with locked(settings):
        already = {row.get("outbox_id") for row in read_jsonl(settings.sent) if row.get("outbox_id")}
        for receipt_path in sorted(receipts_dir.glob("*.json")):
            receipt = _read_json_object(receipt_path)
            if receipt is None:
                continue
            entry_id = receipt.get("id") or receipt_path.stem
            if not isinstance(entry_id, str) or not entry_id:
                continue
            entry = _read_json_object(settings.outbox_dir / f"{entry_id}.json") or {}
            # L9 E6: an "alert" entry never writes a sent.jsonl row, delivered or not --
            # it is not a packet push, and counting it as one would wrongly start the
            # packet's own 24-hour/one-push-per-packet cooldown. It still shows up in
            # `result` below (callers -- L9's check_credit_risk -- need to know delivery
            # outcome for its own retry bookkeeping) and in notification_status()
            # (which reads receipts directly, not sent.jsonl).
            if bool(receipt.get("delivered")):
                if entry.get("kind") != "alert":
                    if entry_id in already:
                        continue
                    append_jsonl(
                        settings.sent,
                        {
                            "packet_id": entry.get("packet_id"),
                            "trigger": entry.get("trigger"),
                            "pushed_ts": str(receipt.get("ts") or utc_iso(now)),
                            "reminder": entry.get("kind") == "reminder",
                            "force": False,
                            "body_sha256": None,
                            "outbox_id": entry_id,
                        },
                    )
                    already.add(entry_id)
                result["delivered"].append(entry_id)
            else:
                result["failed"].append(entry_id)
            result["receipts"][entry_id] = {"ts": receipt.get("ts"), "exit_code": receipt.get("exit_code")}
    return result


def notification_status(settings: PacketSettings, *, now: datetime | None = None) -> str | None:
    """One line for ``packet list`` (design §5 D1), or ``None`` when there
    is nothing to say: a pending entry stuck past
    :data:`MISSING_RECEIPT_AFTER_MIN` wins over a failed one (it is the
    fresher problem), else the warning fires only when the MOST RECENT entry
    across all receipts -- by filename stamp, i.e. by when it was queued,
    not by when its receipt happened to be read -- itself failed (S-3: a
    later delivery must silence an earlier failure; receipts are never
    pruned, so without this an old failure warned forever)."""
    if not settings.outbox:
        return None
    stuck = missing_receipt_entries(settings, now=now)
    if stuck:
        oldest = min(stuck, key=lambda e: e.get("created_ts") or "")
        return (
            f"the notification queued at {oldest.get('created_ts')} has not been sent yet; "
            "the host job may not be running"
        )
    receipts_dir = settings.outbox_receipts
    if not receipts_dir.is_dir():
        return None
    receipts: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(receipts_dir.glob("*.json")):  # filename stamp order: last is newest
        receipt = _read_json_object(path)
        if receipt is not None:
            receipts.append((path.stem, receipt))
    if not receipts:
        return None
    _entry_id, latest = receipts[-1]
    if bool(latest.get("delivered")):
        return None
    code = latest.get("exit_code")
    reason = _FAILURE_WORDS.get(code, f"exit code {code}" if code is not None else "unknown reason")
    return f"the last notification was not sent: {reason}"
