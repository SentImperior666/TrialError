"""Doctor checks (design sections 4.1 item 4 and 4.2 item 6).

``vastai_high_tier``     warn if the high tier is configured, a high-tier
                         approval is present and unexpired, or a
                         ``vastai_high_tier_use`` event is < 7 days old.
``vastai_live_instances`` fail if any TrialError instance (local run record,
                         or -- when a key path is configured -- the live
                         vast.ai list) is past its deadline or failed to
                         destroy; warn if any is live at all.

Auto-discovered by ``trialerror.util.doctor.discover_and_register_checks``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.vastai.guard import approval_path, program_fingerprint
from trialerror.vastai.lease import parse_label, read_run_records, record_for_instance

__all__ = ["check_vastai_high_tier", "check_vastai_live_instances", "RECENT_HIGH_TIER_DAYS"]

_CATEGORY = "vastai"
RECENT_HIGH_TIER_DAYS = 7

#: Test seam: ``(api_key_path) -> client``; production builds a VastClient.
_client_factory: Callable[[Any], Any] | None = None


def _raw_config(ctx: DoctorContext) -> dict[str, Any]:
    from trialerror.util.config import CONFIG_FILENAME, load_config

    path = ctx.program_root / CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        return load_config(path).raw
    except Exception:  # noqa: BLE001 - a broken toml is another check's finding
        return {}


def _skip(name: str, why: str) -> CheckResult:
    return CheckResult(name=name, category=_CATEGORY, status="skip", message=why)


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@register_check("vastai_high_tier", category=_CATEGORY)
def check_vastai_high_tier(ctx: DoctorContext) -> CheckResult:
    if ctx.program_root is None:
        return _skip("vastai_high_tier", "program_root not configured")
    findings: list[str] = []
    details: dict[str, Any] = {}
    raw = _raw_config(ctx)
    if str((raw.get("vastai") or {}).get("tier") or "mid") == "high":
        findings.append("[vastai] tier = \"high\" is configured")
        details["configured_tier"] = "high"
    ap = approval_path(ctx.program_root)
    if ap.is_file():
        try:
            body = json.loads(ap.read_text(encoding="utf-8"))
            exp = _parse(body.get("expires"))
        except (OSError, ValueError, AttributeError):
            body, exp = {}, None
        if exp is None or exp > datetime.now(timezone.utc):
            findings.append(f"a high-tier approval is present at {ap} (expires {body.get('expires')})")
            details["approval_expires"] = body.get("expires")
    db = paths.ops_db_path(ctx.program_root)
    if db.exists():
        cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_HIGH_TIER_DAYS)
        conn = connect(db, read_only=True)
        try:
            rows = conn.execute(
                "SELECT ts FROM event WHERE type = 'vastai_high_tier_use' ORDER BY ts DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        recent = [r[0] for r in rows if (_parse(r[0]) or cutoff) > cutoff]
        if recent:
            findings.append(f"{len(recent)} high-tier run(s) in the last {RECENT_HIGH_TIER_DAYS} days (latest {recent[0]})")
            details["recent_high_tier_runs"] = recent
    if not findings:
        return CheckResult("vastai_high_tier", _CATEGORY, "pass", "high vast.ai tier not configured, approved or recently used")
    return CheckResult("vastai_high_tier", _CATEGORY, "warn", "HIGH vast.ai tier: " + "; ".join(findings), details)


@register_check("vastai_live_instances", category=_CATEGORY)
def check_vastai_live_instances(ctx: DoctorContext) -> CheckResult:
    if ctx.program_root is None:
        return _skip("vastai_live_instances", "program_root not configured")
    now_epoch = time.time()
    live: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []  # on the account, not TrialError's: reported, never touched
    blind = False
    for rec in read_run_records(ctx.program_root):
        status = rec.get("status")
        if status in ("destroyed", "reaped"):
            continue
        entry = {"source": "run_record", "run_id": rec.get("run_id"), "instance_id": rec.get("instance_id"), "status": status}
        if status == "destroy_failed" or now_epoch >= float(rec.get("deadline_epoch") or 0):
            bad.append(entry)
        else:
            live.append(entry)
    notes: list[str] = []
    raw = _raw_config(ctx)
    table = raw.get("vastai") or {}
    key_path = table.get("api_key_path")
    if key_path and table.get("doctor_api_check", True):
        from pathlib import Path

        kp = Path(key_path)
        kp = kp if kp.is_absolute() else ctx.program_root / kp
        if kp.is_file():
            try:
                if _client_factory is not None:
                    client = _client_factory(kp)
                else:
                    from trialerror.vastai.api import VastClient

                    client = VastClient(kp)
                fp12 = program_fingerprint(ctx.program_root)[:12]
                records = read_run_records(ctx.program_root)
                for inst in client.list_instances():
                    tag = parse_label(inst.get("label"))
                    if tag is None:
                        rec = record_for_instance(inst, records)
                        if rec is None:
                            others.append({"instance_id": inst.get("id"), "label": inst.get("label")})
                            continue
                        # unlabelled but named by our run record: overdue if the
                        # record thinks it is gone or its deadline has passed
                        entry = {"source": "vast.ai", "instance_id": inst.get("id"), "label": None,
                                 "run_id": rec.get("run_id"), "this_program": True}
                        gone = rec.get("status") in ("destroyed", "reaped")
                        if gone or now_epoch >= float(rec.get("deadline_epoch") or 0):
                            bad.append(entry)
                        else:
                            live.append(entry)
                        continue
                    entry = {"source": "vast.ai", "instance_id": inst.get("id"), "label": inst.get("label"),
                             "this_program": tag.get("program") == fp12}
                    if tag.get("malformed") or now_epoch >= tag["deadline_epoch"]:
                        bad.append(entry)
                    else:
                        live.append(entry)
            except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
                blind = True
                notes.append(f"live vast.ai list unavailable: {type(exc).__name__}: {exc}")
    details = {"live": live, "overdue_or_failed": bad, "other_instances": others, "notes": notes}
    if bad:
        return CheckResult(
            "vastai_live_instances", _CATEGORY, "fail",
            f"{len(bad)} TrialError vast.ai instance(s) past deadline or not confirmed destroyed -- "
            "run `trialerror vastai reap` and check the vast.ai console",
            details,
        )
    if live:
        return CheckResult(
            "vastai_live_instances", _CATEGORY, "warn",
            f"{len(live)} TrialError vast.ai instance(s) live now (billing)", details,
        )
    if blind:
        # A check that could not see the account must not report "none live".
        return CheckResult(
            "vastai_live_instances", _CATEGORY, "warn",
            "could not list vast.ai instances, so live GPUs cannot be ruled out -- " + "; ".join(notes), details,
        )
    if others:
        return CheckResult(
            "vastai_live_instances", _CATEGORY, "warn",
            f"{len(others)} vast.ai instance(s) on this account that are not TrialError's (billing; never reaped "
            "by TrialError) -- check the vast.ai console", details,
        )
    msg = "no live TrialError vast.ai instances"
    return CheckResult("vastai_live_instances", _CATEGORY, "pass", msg, details)
