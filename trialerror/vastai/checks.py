"""Doctor checks, category ``vastai`` (design section 13). Ported from the
public TrialError copy's embedding backend, narrowed to this DEV root, and
(round 3) the union of both lanes' sources: the two ported checks read the
embedding lane's run records (``<root>/offload/vastai/runs``) and the OCR
lane's (the worker's state directory), and the ops-DB
``vastai_high_tier_use`` events beside the OCR ledger's ``intent`` rows.

``vastai_live_instances``  fail if an instance of THIS root (a run record of
                           either lane, or -- when a key path is configured
                           and ``doctor_api_check`` is on -- the live vast.ai
                           list) is past its deadline or failed to destroy,
                           and (round 4, the public result) if the account
                           carries another root's TrialError-labelled
                           instance past its deadline or a malformed
                           TrialError label (``trialerror vastai reap``);
                           warn if one is live, if the account could not be
                           listed, or if the account carries another root's
                           instance within its deadline or one that is not
                           TrialError's (reported, never touched).
``vastai_high_tier``       warn if the high tier is configured, a high-tier
                           approval is present and unexpired, a
                           ``vastai_high_tier_use`` event (a high-tier
                           embedding run) is less than 7 days old, or the
                           ledger shows a high-tier OCR lease in the last 7
                           days.
``vastai_ocr_egress``      what may leave now (executor, tiers, document
                           count, approval, envelope left, host
                           requirements); warn if the policy names
                           commercial_restricted, the table no longer matches
                           its seal, or the approval file is not valid.
``vastai_ocr_ledger``      fail on a shipped lease with no outcome whose
                           instance is not confirmed destroyed; warn on a torn
                           line, a lease in flight, or 7-day spend above half
                           the approval's envelope.

The last two read local files only (the toml, the approval, the key to check
the approval's signature, the ledger, the run records): no network call.

The doctor runs these on DEV with ``--program-root <backend-config-root>``.
Where vast.ai is not configured -- no ``[vastai]`` table and ``[ingest.ocr]
executor`` not ``"vastai"``, which includes the queue side's own doctor --
both pass at once, with no API call and no state read.

Auto-discovered by ``trialerror.util.doctor.discover_and_register_checks``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.vastai.errors import VastConfigError
from trialerror.vastai.guard import program_fingerprint
from trialerror.vastai.lease import (
    CLEAN_RECORD_STATES,
    UNCONFIRMED_CREATE_STATES,
    parse_label,
    read_run_records,
    read_state_run_records,
    record_for_instance,
)
from trialerror.vastai.ledger import Ledger, default_state_dir

__all__ = [
    "check_vastai_high_tier",
    "check_vastai_live_instances",
    "check_vastai_ocr_egress",
    "check_vastai_ocr_ledger",
    "RECENT_HIGH_TIER_DAYS",
    "LEDGER_SPEND_WINDOW_DAYS",
    "LEDGER_SPEND_WARN_SHARE",
]

_CATEGORY = "vastai"
RECENT_HIGH_TIER_DAYS = 7

#: Test seam: ``(api_key_path) -> client``; production builds a VastClient.
_client_factory: Callable[[Any], Any] | None = None
#: Test seam: ``() -> vast.ai state dir``; production uses the worker's.
_state_dir_factory: Callable[[], Path] | None = None


def _state_dir() -> Path:
    return _state_dir_factory() if _state_dir_factory is not None else default_state_dir()


class _Settings:
    """What the checks read from the root's toml: the raw ``[vastai]``
    table (enough to find the key even when the table is refused), the
    parsed config when it loads, and the refusal when it does not."""

    def __init__(self, root: Path, raw: dict[str, Any]):
        from trialerror.vastai.config import load_vast_config

        self.root = root
        self.table: dict[str, Any] = raw.get("vastai") if isinstance(raw.get("vastai"), dict) else {}
        ingest_ocr = (raw.get("ingest") or {}).get("ocr") if isinstance(raw.get("ingest"), dict) else None
        executor = ingest_ocr.get("executor") if isinstance(ingest_ocr, dict) else None
        self.configured = "vastai" in raw or executor == "vastai"
        self.cfg = None
        self.error: str | None = None
        if self.configured:
            try:
                self.cfg = load_vast_config(raw, config_root=root)
            except VastConfigError as exc:
                self.error = str(exc)

    def key_path(self) -> Path | None:
        if self.cfg is not None:
            return self.cfg.api_key_path
        value = self.table.get("api_key_path")
        if not isinstance(value, str) or not value.strip():
            return None
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    def doctor_api_check(self) -> bool:
        if self.cfg is not None:
            return self.cfg.doctor_api_check
        return self.table.get("doctor_api_check", True) is not False

    def tier(self) -> str:
        return self.cfg.tier if self.cfg is not None else str(self.table.get("tier") or "mid")

    def high_tier_approval_path(self) -> Path:
        """What the doctor reads, through the readers' one resolution (round 4,
        decision d): beside the key file, or the legacy ``<root>/keys/`` one."""
        from trialerror.vastai.guard import approval_read_path

        return approval_read_path(self.root, self.key_path())


def _settings(ctx: DoctorContext) -> _Settings | None:
    """``None`` when there is no readable toml at the root."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    path = ctx.program_root / CONFIG_FILENAME
    if not path.is_file():
        return None
    try:
        raw = load_config(path).raw
    except Exception:  # noqa: BLE001 - a broken toml is offload_backend_root_resolved's finding
        return None
    return _Settings(ctx.program_root, raw)


def _not_configured(name: str, why: str) -> CheckResult:
    return CheckResult(name, _CATEGORY, "pass", f"vast.ai is not configured here ({why})")


def _parse(ts: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _recent_high_tier_events(program_root: Path, cutoff: datetime) -> list[str]:
    """The timestamps of the ops DB's ``vastai_high_tier_use`` events (the
    embedding lane's high-tier runs) newer than ``cutoff``, newest first, as
    the public copy reads them. No ops DB, or one without the table: none."""
    db = paths.ops_db_path(program_root)
    if not db.exists():
        return []
    try:
        conn = connect(db, read_only=True)
        try:
            rows = conn.execute(
                "SELECT ts FROM event WHERE type = 'vastai_high_tier_use' ORDER BY ts DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if (_parse(r[0]) or cutoff) > cutoff]


def _root_run_records(program_root: Path, state_dir: Path, fp: str) -> list[dict[str, Any]]:
    """This root's run records of both lanes: the embedding lane's under the
    program root (this root's by where they are), then the OCR lane's in the
    worker's state directory (this root's by their full fingerprint)."""
    embed = [r for r in read_run_records(program_root) if isinstance(r, dict)]
    return embed + read_state_run_records(state_dir, program_fp=fp)


@register_check("vastai_high_tier", category=_CATEGORY)
def check_vastai_high_tier(ctx: DoctorContext) -> CheckResult:
    name = "vastai_high_tier"
    if ctx.program_root is None:
        return CheckResult(name, _CATEGORY, "skip", "program_root not configured")
    settings = _settings(ctx)
    if settings is None or not settings.configured:
        return _not_configured(name, "no [vastai] table, and [ingest.ocr] executor is not \"vastai\"")
    findings: list[str] = []
    details: dict[str, Any] = {}
    if settings.tier() == "high":
        findings.append("[vastai] tier = \"high\" is configured")
        details["configured_tier"] = "high"
    ap = settings.high_tier_approval_path()
    if ap is not None and ap.is_file():
        try:
            body = json.loads(ap.read_text(encoding="utf-8"))
            exp = _parse(body.get("expires"))
        except (OSError, ValueError, AttributeError):
            body, exp = {}, None
        if exp is None or exp > datetime.now(timezone.utc):
            findings.append(f"a high-tier approval is present at {ap} (expires {body.get('expires')})")
            details["approval_expires"] = body.get("expires")
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_HIGH_TIER_DAYS)
    runs = _recent_high_tier_events(ctx.program_root, cutoff)
    if runs:
        findings.append(f"{len(runs)} high-tier run(s) in the last {RECENT_HIGH_TIER_DAYS} days (latest {runs[0]})")
        details["recent_high_tier_runs"] = runs
    recent = [
        r.get("ts")
        for r in Ledger(_state_dir()).read().rows
        if r.get("kind") == "intent" and r.get("tier") == "high" and (_parse(r.get("ts")) or cutoff) > cutoff
    ]
    if recent:
        findings.append(f"{len(recent)} high-tier lease(s) in the last {RECENT_HIGH_TIER_DAYS} days (latest {recent[-1]})")
        details["recent_high_tier_leases"] = recent
    if not findings:
        return CheckResult(name, _CATEGORY, "pass", "high vast.ai tier not configured, approved or recently used")
    return CheckResult(name, _CATEGORY, "warn", "HIGH vast.ai tier: " + "; ".join(findings), details)


@register_check("vastai_live_instances", category=_CATEGORY)
def check_vastai_live_instances(ctx: DoctorContext) -> CheckResult:
    name = "vastai_live_instances"
    if ctx.program_root is None:
        return CheckResult(name, _CATEGORY, "skip", "program_root not configured")
    settings = _settings(ctx)
    if settings is None or not settings.configured:
        return _not_configured(name, "no [vastai] table, and [ingest.ocr] executor is not \"vastai\"")
    now_epoch = time.time()
    fp = program_fingerprint(ctx.program_root)
    fp12 = fp[:12]
    records = _root_run_records(ctx.program_root, _state_dir(), fp)
    live: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []  # another TrialError root's, within its deadline: warns, never touched
    foreign_bad: list[dict[str, Any]] = []  # another root's past its deadline, or a malformed label: fails
    others: list[dict[str, Any]] = []  # not TrialError's at all: reported, never touched
    notes: list[str] = []
    if settings.error:
        notes.append(f"[vastai] settings are refused: {settings.error}")
    blind = False
    listed_labels: set[str] | None = None
    unconfirmed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for rec in records:
        status = rec.get("status")
        if status in CLEAN_RECORD_STATES:
            continue
        entry = {"source": "run_record", "run_id": rec.get("run_id"), "instance_id": rec.get("instance_id"),
                 "status": status}
        overdue = status == "destroy_failed" or now_epoch >= float(rec.get("deadline_epoch") or 0)
        if status in UNCONFIRMED_CREATE_STATES and rec.get("instance_id") is None:
            unconfirmed.append((rec, {**entry, "overdue": overdue}))
        elif overdue:
            bad.append(entry)
        else:
            live.append(entry)
    key_path = settings.key_path()
    if key_path is not None and settings.doctor_api_check():
        if key_path.is_file():
            try:
                if _client_factory is not None:
                    client = _client_factory(key_path)
                else:
                    from trialerror.vastai.api import VastClient

                    client = VastClient(key_path)
                listed = client.list_instances()
                listed_labels = {str(i.get("label")) for i in listed if i.get("label")}
                for inst in listed:
                    tag = parse_label(inst.get("label"))
                    rec = record_for_instance(inst, records)
                    entry = {"source": "vast.ai", "instance_id": inst.get("id"), "label": inst.get("label")}
                    if rec is not None:
                        entry["run_id"] = rec.get("run_id")
                        gone = rec.get("status") in CLEAN_RECORD_STATES
                        deadline = float(rec.get("deadline_epoch") or 0)
                        if tag is not None and not tag.get("malformed"):
                            deadline = float(tag["deadline_epoch"])
                        (bad if gone or now_epoch >= deadline else live).append(entry)
                    elif tag is None:
                        others.append(entry)
                    elif tag.get("malformed") or tag.get("program") != fp12:
                        entry["past_deadline"] = bool(not tag.get("malformed") and now_epoch >= tag["deadline_epoch"])
                        entry["malformed"] = bool(tag.get("malformed"))
                        # round 4 (e), the public result: past its deadline or malformed fails (never touched)
                        (foreign_bad if entry["past_deadline"] or entry["malformed"] else foreign).append(entry)
                    elif now_epoch >= tag["deadline_epoch"]:
                        bad.append(entry)
                    else:
                        live.append(entry)
            except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
                blind = True
                notes.append(f"live vast.ai list unavailable: {type(exc).__name__}: {exc}")
        else:
            notes.append(f"the key file at {key_path} is absent, so the account was not listed")
            blind = True
    elif key_path is None:
        notes.append("no [vastai] api_key_path, so the account was not listed")
    else:
        notes.append("[vastai] doctor_api_check = false, so the account was not listed")
    resolved: list[dict[str, Any]] = []
    for rec, entry in unconfirmed:
        if listed_labels is not None and str(rec.get("label")) not in listed_labels:
            resolved.append(entry)  # the account has no instance with this lease's label
        elif entry.pop("overdue"):
            bad.append(entry)
        else:
            live.append(entry)
    details = {"live": live, "overdue_or_failed": bad, "other_roots": foreign,
               "other_roots_overdue_or_malformed": foreign_bad, "not_trialerror": others,
               "create_failures_not_on_account": resolved, "notes": notes}
    if bad or foreign_bad:
        failing: list[str] = []
        if bad:
            failing.append(
                f"{len(bad)} vast.ai instance(s) of this root past deadline or not confirmed destroyed -- "
                "run `trialerror vastai reap` (an OCR lease: `trialerror vastai reap --ocr`)"
            )
        if foreign_bad:
            failing.append(
                f"{len(foreign_bad)} TrialError vast.ai instance(s) of another root past deadline or with a "
                "malformed TrialError label -- run `trialerror vastai reap`"
            )
        return CheckResult(name, _CATEGORY, "fail", "; ".join(failing) + " and check the vast.ai console", details)
    if live:
        return CheckResult(name, _CATEGORY, "warn", f"{len(live)} vast.ai instance(s) of this root live now (billing)",
                           details)
    if blind:
        # A check that could not see the account must not report "none live".
        return CheckResult(
            name, _CATEGORY, "warn",
            "could not list vast.ai instances, so live GPUs cannot be ruled out -- " + "; ".join(notes), details,
        )
    if foreign or others:
        return CheckResult(
            name, _CATEGORY, "warn",
            f"{len(foreign)} instance(s) of another TrialError root within their deadline and "
            f"{len(others)} instance(s) that are not TrialError's on this account (billing; never reaped from this "
            "root) -- check the vast.ai console",
            details,
        )
    if settings.error:
        return CheckResult(name, _CATEGORY, "warn", "no live vast.ai instances of this root; " + notes[0], details)
    return CheckResult(name, _CATEGORY, "pass", "no live vast.ai instances of this root", details)


# ---------------------------------------------------------------------------
# lane L3: what may leave, and the ledger (design 13). Neither makes a network
# call; both pass at once where vast.ai is not configured.
# ---------------------------------------------------------------------------
#: The ledger check's spend window, and the share of the envelope that warns.
LEDGER_SPEND_WINDOW_DAYS = 7
LEDGER_SPEND_WARN_SHARE = 0.5


def _money(value: float | None) -> str:
    return "none" if value is None else f"${value:.2f}"


@register_check("vastai_ocr_egress", category=_CATEGORY)
def check_vastai_ocr_egress(ctx: DoctorContext) -> CheckResult:
    """What may leave this machine now: the executor, the licence tiers and
    the number of documents the policy names, the approval (valid or why
    not, expiry, envelope left) and the host requirements. ``warn`` when the
    policy or its approval names ``commercial_restricted``, when the table no
    longer matches its seal, or when the approval file is not a valid
    approval of this root (hand-written, edited, another root's)."""
    from trialerror.vastai.egress import approval_status
    from trialerror.vastai.ledger import spent_under_approval
    from trialerror.vastai.pricing import approval_cap_usd

    name = "vastai_ocr_egress"
    if ctx.program_root is None:
        return CheckResult(name, _CATEGORY, "skip", "program_root not configured")
    settings = _settings(ctx)
    if settings is None or not settings.configured:
        return _not_configured(name, "no [vastai] table, and [ingest.ocr] executor is not \"vastai\"")
    if settings.cfg is None:
        return CheckResult(name, _CATEGORY, "warn",
                           "[vastai] settings are refused, so the worker refuses every vast.ai job and nothing "
                           f"leaves: {settings.error}", {"error": settings.error})
    cfg = settings.cfg
    e = cfg.egress
    details: dict[str, Any] = {
        "executor": cfg.executor,
        "allow_license_tiers": list(e.allow_license_tiers),
        "allow_documents": len(e.allow_documents),
        "host_requirements": {
            "require_datacenter": e.require_datacenter,
            "require_verified": e.require_verified,
            "allow_geolocations": list(e.allow_geolocations),
            "remote_scratch": e.remote_scratch,
            "shm_required_tiers": list(e.shm_required_tiers),
        },
        "require_approval": e.require_approval,
        "when_refused": e.when_refused,
    }
    if not cfg.enabled:
        return CheckResult(name, _CATEGORY, "pass",
                           f'[ingest.ocr] executor = "{cfg.executor}": no document leaves this machine', details)
    status = approval_status(cfg, now=datetime.now(timezone.utc))
    details["approval"] = {k: v for k, v in status.items() if k != "message"}
    if status["valid"]:
        cap = approval_cap_usd(cfg, float(status["max_total_usd"]))
        spent = spent_under_approval(Ledger(_state_dir()).read().rows, status["nonce"])
        details["envelope"] = {"cap_usd": cap, "spent_usd": round(spent, 4),
                               "left_usd": round(max(cap - spent, 0.0), 4)}
    warns: list[str] = []
    if "commercial_restricted" in e.allow_license_tiers or "commercial_restricted" in status["approved_tiers"]:
        warns.append("the egress policy names commercial_restricted: documents of that tier may leave for a "
                     "rented host")
    if status["reason_code"] == "approval-unsealed":
        warns.append("[vastai.egress] no longer matches its approval's seal: every job is refused until the "
                     "operator re-approves (`trialerror vastai approve-ocr`) or the table is restored")
    elif status["reason_code"] == "approval-invalid" and status["present"]:
        warns.append(f"the approval file is not a valid approval of this root ({status['message']})")
    what = f"tiers {', '.join(e.allow_license_tiers) or 'none'}, {len(e.allow_documents)} named document(s)"
    host = (f"datacenter={e.require_datacenter}, verified={e.require_verified}, countries "
            f"{', '.join(e.allow_geolocations) or 'any'}, scratch {e.remote_scratch}")
    if not e.require_approval:
        state = "no approval needed ([vastai.egress] require_approval = false): the config switch alone decides"
    elif status["valid"]:
        envelope = details["envelope"]
        state = (f"approval valid until {status['expires']}, {_money(envelope['left_usd'])} of "
                 f"{_money(envelope['cap_usd'])} left in its envelope")
    else:
        state = f"no valid approval ({status['reason_code']}): nothing leaves until the operator runs approve-ocr"
    if not e.allow_license_tiers and not e.allow_documents:
        message = f"nothing may leave: [vastai.egress] names no tier and no document; {state}"
    else:
        message = f"may leave: {what}; {state}; hosts: {host}"
    if warns:
        return CheckResult(name, _CATEGORY, "warn", "; ".join(warns) + f" -- {message}", details)
    return CheckResult(name, _CATEGORY, "pass", message, details)


@register_check("vastai_ocr_ledger", category=_CATEGORY)
def check_vastai_ocr_ledger(ctx: DoctorContext) -> CheckResult:
    """The run ledger. ``fail`` on a ``shipped`` row with no ``outcome``
    whose instance is not confirmed destroyed (no ``reaped`` row, and no run
    record that says destroyed) once its lease is past its deadline, has no
    run record, or failed to destroy; such a lease still inside its deadline
    is in flight (``warn``). ``warn`` on a torn line, or when the spend of the
    last 7 days passes half the approval's envelope."""
    from trialerror.vastai.egress import read_egress_approval
    from trialerror.vastai.errors import EgressRefused
    from trialerror.vastai.ledger import lease_spend
    from trialerror.vastai.pricing import approval_cap_usd

    name = "vastai_ocr_ledger"
    if ctx.program_root is None:
        return CheckResult(name, _CATEGORY, "skip", "program_root not configured")
    settings = _settings(ctx)
    if settings is None or not settings.configured:
        return _not_configured(name, "no [vastai] table, and [ingest.ocr] executor is not \"vastai\"")
    read = Ledger(_state_dir()).read()
    rows = read.rows
    now = datetime.now(timezone.utc)
    records = {str(r.get("run_id")): r for r in read_state_run_records(_state_dir())}
    outcomes = {str(r.get("lease_id")) for r in rows if r.get("kind") == "outcome"}
    reaped = {str(r.get("instance_id")) for r in rows if r.get("kind") == "reaped"}
    failed_destroy = {str(r.get("instance_id")) for r in rows if r.get("kind") == "destroy_failed"}
    open_bad: list[dict[str, Any]] = []
    in_flight: list[dict[str, Any]] = []
    for r in rows:
        if r.get("kind") != "shipped" or str(r.get("lease_id")) in outcomes:
            continue
        iid = str(r.get("instance_id"))
        rec = records.get(str(r.get("lease_id")))
        if iid in reaped or (rec is not None and rec.get("status") in CLEAN_RECORD_STATES):
            continue  # confirmed destroyed, by the reaper or by the lease
        entry = {"lease_id": r.get("lease_id"), "instance_id": r.get("instance_id"), "start": r.get("start"),
                 "run_record": None if rec is None else rec.get("status")}
        deadline = float((rec or {}).get("deadline_epoch") or 0)
        if iid not in failed_destroy and rec is not None and now.timestamp() < deadline:
            in_flight.append(entry)
        else:
            open_bad.append(entry)
    cutoff = now - timedelta(days=LEDGER_SPEND_WINDOW_DAYS)
    spend_7d = sum(s.usd for s in lease_spend(rows).values() if (_parse(s.ts) or cutoff) > cutoff)
    envelope = None
    if settings.cfg is not None:
        try:
            body = read_egress_approval(settings.cfg)
        except EgressRefused:
            body = None
        total = body.get("max_total_usd") if isinstance(body, dict) else None
        valid_total = isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0
        envelope = approval_cap_usd(settings.cfg, float(total)) if valid_total else settings.cfg.max_approval_usd
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("kind"))] = counts.get(str(r.get("kind")), 0) + 1
    details = {
        "path": str(read.path),
        "rows": counts,
        "torn": [{"line_no": t.line_no, "error": t.error} for t in read.torn],
        "shipped_not_settled": open_bad,
        "in_flight": in_flight,
        "spend_last_7_days_usd": round(spend_7d, 4),
        "envelope_usd": envelope,
    }
    if open_bad:
        return CheckResult(
            name, _CATEGORY, "fail",
            f"{len(open_bad)} shipped lease(s) with no outcome whose instance is not confirmed destroyed "
            f"({', '.join(str(b['instance_id']) for b in open_bad)}) -- it may still bill and hold the document: "
            "run `trialerror vastai reap --ocr` and check the vast.ai console",
            details,
        )
    warns: list[str] = []
    if read.torn:
        warns.append(f"{len(read.torn)} torn ledger line(s) (lines "
                     f"{', '.join(str(t.line_no) for t in read.torn)}): reported, not dropped")
    if in_flight:
        warns.append(f"{len(in_flight)} lease(s) in flight (shipped, inside their deadline)")
    if envelope is not None and spend_7d > LEDGER_SPEND_WARN_SHARE * envelope:
        warns.append(f"{_money(spend_7d)} spent in the last {LEDGER_SPEND_WINDOW_DAYS} days, more than half of "
                     f"the approval's envelope {_money(envelope)}")
    if warns:
        return CheckResult(name, _CATEGORY, "warn", "; ".join(warns), details)
    if not rows:
        return CheckResult(name, _CATEGORY, "pass", "the vast.ai ledger is empty: nothing has been shipped", details)
    return CheckResult(
        name, _CATEGORY, "pass",
        f"{len(rows)} ledger row(s), every shipped lease settled; {_money(spend_7d)} in the last "
        f"{LEDGER_SPEND_WINDOW_DAYS} days", details,
    )
