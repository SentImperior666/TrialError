"""``trialerror vastai ...``: rent a vast.ai GPU. Two lanes share the group
(round 3, the superset of the public TrialError copy's embedding backend).

The embedding lane's verbs, as the public copy has them (design
``docs/VASTAI_EMBED_DESIGN.md``; ``--program-root``):

    plan         read-only: one offer search, print selection/TTL/worst-case $
                 (``plan [--max-jobs N]``, without ``--input``)
    run          create -> embed the pending markers -> destroy (always)
    reap         destroy TrialError instances past deadline or orphaned (the
                 public policy; a live OCR lease is never destroyed)
    approve-high OPERATOR ONLY, interactive terminal: sign a short-lived
                 high-tier approval

The OCR lane's verbs (design ``docs/VASTAI_OCR_DESIGN.md`` sections 9-13). They
run on DEV, against DEV's backend-config-root (``--backend-config-root``, as
``offload worker`` takes it; ``--program-root`` is the same flag); the queue
side has no use for them.

    approve-ocr  OPERATOR ONLY, interactive terminal: seal the current
                 [vastai.egress] in a signed, expiring approval with a spend
                 envelope (design 9.3)
    approve-high OPERATOR ONLY, interactive terminal: the high GPU tier for
                 at most 24 h (ported unchanged)
    plan --input the dry run for one PDF: range plan, RAM floor, ONE read-only
                 offer search, ranked offers, TTL, worst case, which caps
                 pass. Rents nothing
    ledger       the run ledger (design 9.5), torn lines reported
    reap --ocr   the OCR lane's reaper pass for this root (design 11.2);
                 --dry-run destroys nothing
    lock-deps    write the hashed requirements lock the instance installs
                 marker's wheels from

There is intentionally no command that keeps, reuses or extends an instance,
and no OCR command rents: the OCR lane rents only inside ``trialerror offload
worker`` with ``[ingest.ocr] executor = "vastai"``. ``run`` is the embedding
lane's renting verb, under that lane's own guards.

Every verb answers with the house envelope; every refusal carries
``nextActions``. Auto-discovered by ``trialerror.cli.discover_groups``.
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from trialerror.util.config import CONFIG_FILENAME, find_program_root, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "vastai"
HELP = (
    "Rent a vast.ai GPU for pending embed jobs (plan/run/reap); create -> run -> destroy, never kept alive. "
    "And for this worker's marker OCR (DEV side): approve-ocr, approve-high, plan --input, ledger, reap --ocr, "
    "lock-deps; no OCR command rents (the offload worker does, and only when approved)."
)

# -- test seams ---------------------------------------------------------------
#: ``(api_key_path) -> client``; production builds a ``VastClient``.
_client_factory: Callable[[Any], Any] | None = None
#: The challenge prompt's input and output.
_input: Callable[[str], str] = input
_out: TextIO | None = None
#: ``() -> aware UTC datetime``.
_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731
#: ``(url) -> bytes`` for ``lock-deps``; production uses ``envlock.urllib_get``.
_lock_http: Callable[[str], bytes] | None = None

#: How many ranked offers ``plan`` shows without ``--json``.
PLAN_OFFERS_SHOWN = 5


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="vastai_cmd", metavar="<command>", required=True)

    def _root(p: argparse.ArgumentParser) -> None:
        # Same dest and default=SUPPRESS as `offload worker` (FX-12): the
        # global --program-root still reaches it when this flag is unset.
        # --program-root: the public copy's name of the same flag (same dest).
        p.add_argument(
            "--backend-config-root",
            "--program-root",
            dest="program_root",
            default=argparse.SUPPRESS,
            metavar="ROOT",
            help="DEV's backend-config-root: the root whose trialerror.toml carries [ingest.ocr] and [vastai] "
            "(default: discovered from CWD via trialerror.toml); --program-root is the same flag",
        )
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (tests)")

    p = sub.add_parser(
        "approve-ocr",
        help="OPERATOR ONLY (interactive terminal): seal [vastai.egress] in a signed approval with a spend envelope",
    )
    _root(p)
    p.add_argument("--days", type=float, default=None,
                   help="lifetime in days (default and ceiling: [vastai.egress] approval_max_days, at most 7)")
    p.add_argument("--max-total-usd", type=float, default=None,
                   help="the envelope: dollars all leases under this approval may cost (default and ceiling: "
                   "[vastai] max_approval_usd)")
    p.add_argument("--max-job-usd", type=float, default=None,
                   help="dollars one job may cost under this approval (default: [vastai] max_job_usd; the "
                   "stricter of the two applies)")
    p.set_defaults(handler=_cmd_approve_ocr)

    p = sub.add_parser("approve-high", help="OPERATOR ONLY (interactive terminal): approve the high tier for <= 24 h")
    _root(p)
    p.add_argument("--hours", type=float, required=True)
    p.add_argument("--max-job-usd", type=float, required=True)
    p.set_defaults(handler=_cmd_approve_high)

    p = sub.add_parser(
        "plan",
        help="read-only: search offers and show what a run would rent and cost; with --input PDF, the OCR dry run "
        "for one PDF: ranges, RAM floor, one offer search, worst case, caps",
    )
    _root(p)
    p.add_argument("--max-jobs", type=int, default=None, help="the embed plan: at most this many pending embed jobs")
    p.add_argument("--input", default=None, metavar="PDF",
                   help="the OCR plan for this PDF (read locally; never sent); without it, the embed plan")
    p.add_argument("--json", action="store_true", help="the OCR plan in full: every offer, every rejection, the query")
    p.add_argument("--offer-keys", type=int, nargs="?", const=3, default=None, metavar="N",
                   help="debug the offer shape: the KEY NAMES of the first N raw offers (default 3) and the values "
                        "of a short allow-list of non-sensitive fields. Never the whole object, never account data")
    p.set_defaults(handler=_cmd_plan)

    p = sub.add_parser(
        "ssh-probe",
        help="the cheapest live ssh answer: rent the cheapest admissible offer, attempt ssh with a bounded retry, "
        "run nvidia-smi, destroy. A DRY RUN unless --rent is given; no document, no bootstrap, nothing uploaded",
    )
    _root(p)
    p.add_argument("--rent", action="store_true",
                   help="actually rent (otherwise this prints what it WOULD rent and costs nothing)")
    p.add_argument("--max-usd", type=float, default=None, metavar="USD",
                   help="REQUIRED with --rent: the probe's own cost ceiling, on top of [vastai] max_job_usd and "
                        "max_run_usd (the stricter of the three applies)")
    p.add_argument("--max-minutes", type=float, default=12.0, metavar="M",
                   help="the lease's TTL in minutes (default 12; [vastai] ttl_cap_s still caps it)")
    p.add_argument("--auth-grace-s", type=float, default=None, metavar="S",
                   help=f"how long a Permission denied is retried before it is believed (default: the backend's "
                        f"own grace)")
    p.add_argument("--min-cpu-ram-gb", type=float, default=0.0, metavar="G",
                   help="the RAM floor to search with (default 0: a probe carries no document; give the canary's "
                        "floor to probe a host the canary could also use)")
    p.add_argument("--log-file", default=None, metavar="PATH",
                   help="append each line, UTC-stamped, as it happens (watch it with Get-Content -Wait)")
    p.add_argument("--json", action="store_true", help="the whole result, including every kept ssh -v line")
    p.set_defaults(handler=_cmd_ssh_probe)

    p = sub.add_parser("run", help="rent one GPU, embed pending markers, destroy it (the embedding lane)")
    _root(p)
    p.add_argument("--max-jobs", type=int, default=None)
    p.set_defaults(handler=_cmd_run)

    p = sub.add_parser("ledger", help="print the vast.ai run ledger (design 9.5); torn lines are reported")
    _root(p)
    p.add_argument("--since", default=None, metavar="ISO", help="rows at or after this UTC time")
    p.add_argument("--job-id", default=None, metavar="ID", help="rows of this job, and of its leases")
    p.add_argument("--json", action="store_true", help="whole rows instead of one line each")
    p.set_defaults(handler=_cmd_ledger)

    p = sub.add_parser(
        "reap",
        help="destroy TrialError-tagged instances past their deadline or orphaned; with --ocr, the OCR lane's "
        "narrowed pass for this root (design 11.2)",
    )
    _root(p)
    p.add_argument("--dry-run", action="store_true", help="list and judge; destroy and record nothing")
    p.add_argument("--ocr", action="store_true",
                   help="the OCR lane's reaper: only this root's OCR (VOCR-) instances, recorded in the ledger")
    p.set_defaults(handler=_cmd_reap)

    p = sub.add_parser("lock-deps", help="write the hashed requirements lock the rented instance installs from")
    _root(p)
    p.add_argument("--pins", default=None, metavar="P",
                   help="a pip-freeze pins file (default: the packaged marker 1.10.2 pins)")
    p.add_argument("--out", default=None, metavar="O",
                   help="where to write the lock (default: <backend-config-root>/<[vastai.ocr] requirements_lock>)")
    p.set_defaults(handler=_cmd_lock_deps)
    return parser


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _argv(verb: str, root: Path | None, *extra: str) -> list[str]:
    argv = ["trialerror", "vastai", verb]
    if root is not None:
        argv += ["--backend-config-root", str(root)]
    return argv + list(extra)


def _refusal(
    command: str,
    code: str,
    message: str,
    *,
    rerun: list[str],
    advice: Any = (),
    details: dict[str, Any] | None = None,
) -> dict:
    """An error envelope whose ``nextActions`` always say what would change
    the answer: each piece of advice, attached to the command to re-run."""
    actions = [next_action(rerun, str(a)) for a in (advice or ()) if str(a).strip()]
    if not actions:
        actions = [next_action(rerun, "re-run once the cause above is fixed")]
    return error_envelope(command, code, message, details=details, next_actions=actions)


def _exc_refusal(command: str, exc: BaseException, *, rerun: list[str], default_code: str) -> dict:
    code = getattr(exc, "reason_code", None) or default_code
    message = getattr(exc, "message", None) or str(exc)
    details = getattr(exc, "details", None) or None
    return _refusal(command, code, str(message), rerun=rerun, advice=getattr(exc, "next_actions", ()),
                    details=dict(details) if isinstance(details, dict) else None)


def _resolve_root(args: argparse.Namespace) -> Path | None:
    if getattr(args, "program_root", None):
        return Path(args.program_root)
    return find_program_root()


def _load(args: argparse.Namespace, command: str, verb: str) -> tuple[Path | None, Any, dict | None]:
    """``(root, VastConfig, None)`` or ``(root, None, refusal envelope)``."""
    from trialerror.vastai.config import load_vast_config
    from trialerror.vastai.errors import VastConfigError

    root = _resolve_root(args)
    if root is None:
        return None, None, _refusal(
            command, "no_backend_config_root",
            "no --backend-config-root given and no trialerror.toml found walking up from CWD",
            rerun=_argv(verb, None, "--backend-config-root", "<DEV backend-config-root>"),
            advice=["name DEV's backend-config-root, the root whose trialerror.toml carries [ingest.ocr]"],
        )
    cfg_path = root / CONFIG_FILENAME
    try:
        raw = load_config(cfg_path).raw
    except Exception as exc:  # noqa: BLE001 - surfaced as an envelope, never a traceback
        return root, None, _refusal(
            command, "bad_config", f"could not read {cfg_path}: {exc}", rerun=_argv(verb, root),
            advice=[f"fix {CONFIG_FILENAME} under the backend-config-root"],
        )
    try:
        cfg = load_vast_config(raw, config_root=root)
    except VastConfigError as exc:
        return root, None, _exc_refusal(command, exc, rerun=_argv(verb, root), default_code="vastai_config_refused")
    return root, cfg, None


def _client(cfg: Any) -> Any:
    if _client_factory is not None:
        return _client_factory(cfg.api_key_path)
    from trialerror.vastai.api import VastClient

    return VastClient(cfg.api_key_path)


def _is_interactive() -> bool:
    # Resolved at call time so the guard's own seam is the one that answers.
    from trialerror.vastai import guard

    return guard._is_interactive()


def _money(value: Any) -> str:
    return "none" if value is None else f"${float(value):.2f}"


# ---------------------------------------------------------------------------
# approve-ocr
# ---------------------------------------------------------------------------
def _approve_prompt(cfg: Any, body: dict[str, Any], challenge: str) -> str:
    from trialerror.vastai.pricing import job_cap_usd

    s = body["egress_summary"]
    e = cfg.egress
    geos = ", ".join(e.allow_geolocations) if e.allow_geolocations else "any country"
    lines = [
        f"You are approving vast.ai OCR egress for the backend-config-root {cfg.config_root.resolve()}.",
        "",
        "What may leave this machine for a rented GPU host:",
        f"  licence tiers   : {', '.join(s['allow_license_tiers']) or 'none'}",
        f"  named documents : {s['allow_documents']} (by sha256, whatever their tier)",
        f"  executor now    : [ingest.ocr] executor = \"{cfg.executor}\""
        + ("" if cfg.enabled else " (nothing leaves until it is \"vastai\")"),
        "Host requirements:",
        f"  datacenter only : {e.require_datacenter}    verified only : {e.require_verified}    countries : {geos}",
        f"  scratch         : {e.remote_scratch} (RAM-backed /dev/shm required for: "
        f"{', '.join(e.shm_required_tiers) or 'no tier'})",
        "Spend envelope:",
        f"  all leases under this approval : at most {_money(body['max_total_usd'])}",
        f"  one job                        : at most {_money(job_cap_usd(cfg, body['max_job_usd']))} "
        f"(the stricter of [vastai] max_job_usd and this approval's)",
        f"Valid from {body['issued']} until {body['expires']}.",
    ]
    if "commercial_restricted" in s["allow_license_tiers"]:
        lines.append("NOTE: commercial_restricted documents may leave under this approval.")
    lines += ["", f"Type {challenge} to confirm: "]
    return "\n".join(lines)


def _cmd_approve_ocr(args: argparse.Namespace) -> dict:
    from trialerror.vastai.egress import approval_body, sign_egress_approval, write_egress_approval
    from trialerror.vastai.errors import VastConfigError, VastError
    from trialerror.vastai.ledger import Ledger

    command = "vastai.approve-ocr"
    root = _resolve_root(args)
    rerun = _argv("approve-ocr", root)
    if not _is_interactive():
        return _refusal(
            command, "not_interactive",
            "approve-ocr refuses to run without an interactive terminal (stdin and stdout must be a TTY). "
            "Sealing what may leave this machine is an operator act; agents and scripts cannot grant it. "
            "Nothing was written.",
            rerun=rerun, advice=["the OPERATOR runs this command in an interactive terminal"],
        )
    root, cfg, err = _load(args, command, "approve-ocr")
    if err:
        return err
    if cfg.api_key_path is None:
        return _refusal(command, "key-missing",
                        "no [vastai] api_key_path: the approval is signed with the vast.ai key. Nothing was written.",
                        rerun=rerun, advice=["set [vastai] api_key_path and place the key file there"])
    if not cfg.egress.require_approval:
        return _refusal(
            command, "approval-not-required",
            "[vastai.egress] require_approval = false: the worker does not consult an approval, and one minted now "
            "would be unsealed the moment the key is set back to true. Nothing was written.",
            rerun=rerun, advice=["set [vastai.egress] require_approval = true, then re-run"],
        )
    if not cfg.egress.allow_license_tiers and not cfg.egress.allow_documents:
        return _refusal(
            command, "nothing-to-approve",
            "[vastai.egress] names no licence tier and no document, so an approval would let nothing leave. "
            "Nothing was written.",
            rerun=rerun,
            advice=["add the documents' sha256 to [vastai.egress] allow_documents (the first-run setting), "
                    "or a tier to allow_license_tiers"],
        )
    if not cfg.api_key_path.is_file():
        return _refusal(command, "key-missing",
                        f"the vast.ai key file is absent at {cfg.api_key_path}. Nothing was written.",
                        rerun=rerun, advice=["place the key file at [vastai] api_key_path"])
    now = _now()
    try:
        preview = approval_body(cfg, issued=now, days=args.days, max_job_usd=args.max_job_usd,
                                max_total_usd=args.max_total_usd)
    except VastConfigError as exc:
        return _refusal(command, "approval_refused", f"{exc} Nothing was written.", rerun=rerun,
                        advice=list(exc.next_actions) or [
                            f"--days at most {cfg.egress.approval_max_days:g}, --max-total-usd at most "
                            f"{cfg.max_approval_usd:.2f}, every amount > 0"])
    if preview["max_job_usd"] > preview["max_total_usd"]:
        return _refusal(
            command, "approval_refused",
            f"--max-job-usd {_money(preview['max_job_usd'])} is above the envelope {_money(preview['max_total_usd'])}; "
            "one job cannot cost more than all of them. Nothing was written.",
            rerun=rerun, advice=["lower --max-job-usd or raise --max-total-usd"],
        )
    challenge = secrets.token_hex(3)
    out = _out or sys.stdout
    out.write(_approve_prompt(cfg, preview, challenge))
    out.flush()
    if _input("").strip() != challenge:
        return _refusal(command, "challenge_mismatch", "challenge not matched -- no approval written.",
                        rerun=rerun, advice=["re-run and type the challenge exactly"])
    try:
        body = sign_egress_approval(cfg, now=now, days=args.days, max_job_usd=args.max_job_usd,
                                    max_total_usd=args.max_total_usd, nonce=preview["nonce"])
        # The ledger row first: an approval that exists always has its row.
        row = Ledger().append(
            "approval_minted", nonce=body["nonce"], purpose=body["purpose"], summary=body["egress_summary"],
            egress_digest=body["egress_digest"], issued=body["issued"], expires=body["expires"],
            max_job_usd=body["max_job_usd"], max_total_usd=body["max_total_usd"],
        )
        path = write_egress_approval(cfg, body)
    except VastError as exc:
        return _exc_refusal(command, exc, rerun=rerun, default_code="approval_refused")
    return ok_envelope(
        command,
        result={
            "approval": str(path),
            "nonce": body["nonce"],
            "issued": body["issued"],
            "expires": body["expires"],
            "max_job_usd": body["max_job_usd"],
            "max_total_usd": body["max_total_usd"],
            "egress_summary": body["egress_summary"],
            "ledger_row_ts": row["ts"],
        },
        next_actions=[
            next_action(_argv("plan", root, "--input", "<pdf>"), "dry-run one document before any real run"),
            next_action(["trialerror", "doctor", "--only", "vastai_ocr_egress", "--program-root", str(root)],
                        "see what may leave now"),
        ],
    )


# ---------------------------------------------------------------------------
# approve-high (ported)
# ---------------------------------------------------------------------------
def _cmd_approve_high(args: argparse.Namespace) -> dict:
    import json

    from trialerror.vastai.errors import VastError
    from trialerror.vastai.guard import HighTierRefused, mint_high_tier_approval
    from trialerror.vastai.ledger import Ledger

    command = "vastai.approve-high"
    root, cfg, err = _load(args, command, "approve-high")
    if err:
        return err
    rerun = _argv("approve-high", root, "--hours", str(args.hours), "--max-job-usd", str(args.max_job_usd))
    try:
        path = mint_high_tier_approval(
            root, cfg.api_key_path, hours=args.hours, max_job_usd=args.max_job_usd,
            input_fn=_input, out=_out, now=_now(),  # where guard.approval_write_path says (round 4, d)
        )
    except HighTierRefused as exc:
        return _refusal(command, exc.reason_code, str(exc), rerun=rerun,
                        advice=["the OPERATOR runs this command in an interactive terminal"])
    except VastError as exc:
        return _exc_refusal(command, exc, rerun=rerun, default_code="key-missing")
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        Ledger().append("approval_minted", nonce=body.get("nonce"), purpose="vastai-high-tier",
                        tier="high", issued=body.get("issued"), expires=body.get("expires"),
                        max_job_usd=body.get("max_job_usd"))
    except (OSError, ValueError, VastError) as exc:
        return ok_envelope(command, result={"approval": str(path)},
                           warnings=[{"code": "ledger_row_missing", "message": f"approval written, ledger row not: {exc}"}])
    return ok_envelope(command, result={"approval": str(path), "expires": body.get("expires")})


# ---------------------------------------------------------------------------
# plan (the dry run)
# ---------------------------------------------------------------------------
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _cap(passed: bool | None, detail: str) -> dict[str, Any]:
    return {"pass": passed, "detail": detail}


def _cmd_plan(args: argparse.Namespace) -> dict:
    from trialerror.vastai.config import runtime_files
    from trialerror.vastai.egress import approval_status
    from trialerror.vastai.errors import VastApiError, VastKeyMissing
    from trialerror.vastai.guard import HighTierRefused, verify_high_tier_approval
    from trialerror.vastai.ledger import Ledger, spend_view
    from trialerror.vastai.pricing import (
        approval_cap_usd,
        estimate_offer,
        job_cap_usd,
        offer_debug_rows,
        offer_query,
        offer_refusals,
        plan_document,
        rank_estimates,
    )
    from trialerror.vastai.sshkeys import check_ssh_key

    if args.input is None:
        return _cmd_plan_embed(args)
    command = "vastai.plan"
    if getattr(args, "max_jobs", None) is not None:
        return _refusal(
            command, "bad_arguments",
            "--max-jobs belongs to the embed plan (plan without --input); the OCR plan is one PDF (--input)",
            rerun=_argv("plan", _resolve_root(args), "--input", str(args.input)),
            advice=["drop --max-jobs for the OCR plan, or --input for the embed plan"],
        )
    root, cfg, err = _load(args, command, "plan")
    if err:
        return err
    pdf = Path(args.input)
    rerun = _argv("plan", root, "--input", str(pdf))
    if not pdf.is_file():
        return _refusal(command, "bad_input", f"--input {pdf} is not a file", rerun=rerun,
                        advice=["name a PDF on this machine"])
    if cfg.api_key_path is None:
        return _refusal(command, "key-missing", "no [vastai] api_key_path: the offer search needs the vast.ai key",
                        rerun=rerun, advice=["set [vastai] api_key_path and place the key file there"])
    try:
        plan = plan_document(pdf, cfg)
    except Exception as exc:  # noqa: BLE001 - a PDF the planner cannot read is a named refusal
        return _refusal(command, "bad_input", f"could not plan {pdf}: {type(exc).__name__}: {exc}", rerun=rerun,
                        advice=["check that --input is a readable PDF"])
    now = _now()
    size = pdf.stat().st_size
    sha = _sha256_file(pdf)
    notes: list[str] = []
    if not cfg.enabled:
        notes.append(f'[ingest.ocr] executor is "{cfg.executor}": the worker sends nothing until it is "vastai"')

    # -- the egress view (the document's tier is unknown to a dry run) -----
    approval = approval_status(cfg, now=now)
    e = cfg.egress
    by_sha = sha in e.allow_documents
    egress: dict[str, Any] = {
        "sha256": sha,
        "bytes": size,
        "named_in_allow_documents": by_sha,
        "allow_license_tiers": list(e.allow_license_tiers),
        "approval": {k: approval[k] for k in ("required", "valid", "reason_code", "expires", "max_job_usd",
                                              "max_total_usd", "nonce")},
    }
    if by_sha:
        egress["may_leave"] = "yes: its sha256 is in [vastai.egress] allow_documents"
    elif e.allow_license_tiers:
        egress["may_leave"] = f"only if its source's licence tier is one of {', '.join(e.allow_license_tiers)}"
    else:
        egress["may_leave"] = "no: [vastai.egress] names neither its sha256 nor any tier (tier-not-allowed)"
    approval_ok = approval["valid"] or not e.require_approval
    nonce = approval["nonce"] if approval["valid"] else None

    # -- spend so far and the caps that do not need an offer ---------------
    ledger_read = Ledger().read()
    spend = spend_view(ledger_read.rows, approval_nonce=nonce, worker_run_id=None)
    high_cap = None
    high_refusal = None
    if cfg.tier == "high":
        try:
            body = verify_high_tier_approval(root, cfg.api_key_path, now=now)  # guard.approval_read_path (round 4, d)
            high_cap = float(body.get("max_job_usd") or 0) or None
        except HighTierRefused as exc:
            high_refusal = exc.reason_code
    approval_job = float(approval["max_job_usd"]) if approval["valid"] else None
    job_cap = job_cap_usd(cfg, approval_job, high_cap)
    run_left = cfg.max_run_usd  # a fresh worker run
    cap_total = approval_cap_usd(cfg, float(approval["max_total_usd"]) if approval["valid"] else None)
    approval_left = None if cap_total is None else cap_total - spend.approval_usd

    # -- ONE read-only offer search -----------------------------------------
    client = _client(cfg)
    query = offer_query(cfg, min_cpu_ram_gb=plan.min_cpu_ram_gb)
    try:
        offers = client.search_offers(query)
    except VastKeyMissing as exc:
        return _exc_refusal(command, exc, rerun=rerun, default_code="key-missing")
    except VastApiError as exc:
        return _refusal(command, "api-error", f"the vast.ai offer search failed: {exc}. Nothing was rented.",
                        rerun=rerun, advice=["retry later (market or platform state, not the document)"])
    # -- the free ssh-key pre-flight (the C4 refusal of 2026-09-19, for $0) --
    ssh_key = check_ssh_key(client, cfg.ssh_identity_path)
    if ssh_key.refuses:
        notes.append(ssh_key.message())
    elif ssh_key.state == "unknown":
        notes.append(f"the ssh identity could not be checked against the account: {ssh_key.detail}")
    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for offer in offers:
        reasons = offer_refusals(offer, cfg, min_cpu_ram_gb=plan.min_cpu_ram_gb)
        if reasons:
            rejected.append({"offer_id": offer.get("id"), "reasons": list(reasons)})
        else:
            admitted.append(offer)
    ranked = rank_estimates(estimate_offer(o, plan, cfg, document_bytes=size) for o in admitted)
    credit = None
    if ranked:
        try:
            credit = client.account_credit()
        except VastApiError as exc:
            notes.append(f"the account's credit could not be read ({exc}); the credit cap is unknown")
        if credit is None and not any("credit" in n for n in notes):
            notes.append("the account's credit is not exposed by the API; the credit cap is unknown")

    def fails(est: Any) -> list[str]:
        out = []
        if not est.fits_ttl_cap:
            out.append("cap-ttl")
        if est.worst_usd > job_cap:
            out.append("cap-job")
        if est.worst_usd > run_left:
            out.append("cap-run")
        if approval_left is not None and est.worst_usd > approval_left:
            out.append("cap-approval")
        if credit is not None and est.worst_usd > credit:
            out.append("credit-low")
        return out

    shown = ranked if args.json else ranked[:PLAN_OFFERS_SHOWN]
    offer_rows = [{**est.as_dict(), "fails": fails(est)} for est in shown]
    best = ranked[0] if ranked else None
    passing = [est for est in ranked if not fails(est)]
    caps = {
        "egress": _cap(True if by_sha else (None if e.allow_license_tiers else False), egress["may_leave"]),
        "document_size": _cap(size <= cfg.ocr.max_document_bytes,
                              f"{size} bytes; [vastai.ocr] max_document_mb = {cfg.ocr.max_document_mb:g}"),
        "offer": _cap(bool(ranked), f"{len(ranked)} of {len(offers)} offer(s) pass the host requirements"),
        "ttl": _cap(None if best is None else best.fits_ttl_cap,
                    "no offer" if best is None else f"best offer needs {best.ttl_uncapped_s:.0f} s; "
                    f"[vastai] ttl_cap_s = {cfg.ttl_cap_s:.0f}"),
        "job": _cap(None if best is None else best.worst_usd <= job_cap,
                    "no offer" if best is None else f"worst case {_money(best.worst_usd)} vs the job cap {_money(job_cap)}"),
        "run": _cap(None if best is None else best.worst_usd <= run_left,
                    "no offer" if best is None else f"worst case {_money(best.worst_usd)} vs [vastai] max_run_usd "
                    f"{_money(cfg.max_run_usd)} (a fresh run)"),
        "approval": _cap(
            (None if best is None else (approval_left is None or best.worst_usd <= approval_left)) if approval_ok
            else False,
            (f"no valid approval ({approval['reason_code']})" if not approval_ok else
             "require_approval = false: no envelope" if approval_left is None else
             "no offer" if best is None else
             f"worst case {_money(best.worst_usd)} vs {_money(approval_left)} left of the envelope"),
        ),
        "credit": _cap(None if (best is None or credit is None) else best.worst_usd <= credit,
                       "unknown" if credit is None else f"account credit {_money(credit)}"),
    }
    if cfg.tier == "high":
        caps["high_tier_approval"] = _cap(high_refusal is None, high_refusal or "valid")
    caps["ssh_key"] = _cap(not ssh_key.refuses, ssh_key.state)
    order = [("ssh_key", "key-missing"),
             ("approval", approval["reason_code"] or "approval-missing"), ("egress", "tier-not-allowed"),
             ("document_size", "document-too-large"),
             ("high_tier_approval", high_refusal or "approval-missing"), ("offer", "no-offer"), ("ttl", "cap-ttl"),
             ("job", "cap-job"), ("run", "cap-run"), ("credit", "credit-low")]
    refusal = next((code for name, code in order if name in caps and caps[name]["pass"] is False), None)
    if refusal is None and ranked and not passing:
        refusal = fails(best)[0]
    runtime = runtime_files(cfg)
    missing = [name for name, entry in runtime.items() if not entry["exists"]]
    if missing:
        notes.append("missing on this machine: " + ", ".join(missing)
                     + " (the worker refuses to start; `trialerror vastai lock-deps` writes the lock)")
    result = {
        "input": {"path": str(pdf), "bytes": size, "sha256": sha},
        "plan": plan.as_dict(),
        "ram_floor_gb": round(plan.min_cpu_ram_gb, 2),
        "searched": len(offers),
        "admitted": len(ranked),
        "offers": offer_rows,
        "best": None if best is None else {**best.as_dict(), "fails": fails(best)},
        "ttl_s": None if best is None else round(best.ttl_s, 1),
        "worst_usd": None if best is None else round(best.worst_usd, 4),
        "caps": caps,
        "spend": {"approval_usd": round(spend.approval_usd, 4), "approval_left_usd": approval_left,
                  "job_cap_usd": job_cap, "run_cap_usd": cfg.max_run_usd, "credit_usd": credit},
        "egress": egress,
        "verdict": "would-rent" if refusal is None else "would-refuse",
        "reason_code": refusal,
        "runtime_files": runtime,
        "ssh_key": ssh_key.as_dict(),
        "rented": False,
        "notes": notes,
    }
    if getattr(args, "offer_keys", None) is not None:
        result["offer_keys"] = offer_debug_rows(offers, int(args.offer_keys))
    if args.json:
        result["query"] = query
        result["rejected"] = rejected
    else:
        result["rejected"] = len(rejected)
    return ok_envelope(
        command, result=result,
        next_actions=[next_action(_argv("ledger", root), "the spend so far, per lease"),
                      next_action(_argv("approve-ocr", root), "the OPERATOR seals the egress policy and envelope")],
    )


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
_LINE_KEYS = ("job_id", "lease_id", "instance_id", "sha256", "bytes", "reason_code", "result", "worst_usd",
              "estimated_cost_usd", "nonce", "reason")


def _row_line(row: dict[str, Any]) -> str:
    parts = [str(row.get("ts")), str(row.get("kind"))]
    for key in _LINE_KEYS:
        value = row.get(key)
        if value is None:
            continue
        if key == "sha256":
            value = str(value)[:12]
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _cmd_ledger(args: argparse.Namespace) -> dict:
    from trialerror.vastai.guard import parse_ts
    from trialerror.vastai.ledger import Ledger, lease_spend

    command = "vastai.ledger"
    root = _resolve_root(args)
    rerun = _argv("ledger", root)
    since = None
    if args.since:
        try:
            since = parse_ts(args.since)
        except ValueError:
            return _refusal(command, "bad_arguments", f"--since {args.since!r} is not an ISO time", rerun=rerun,
                            advice=["pass --since like 2026-09-19T00:00:00Z"])
    read = Ledger().read()
    rows = list(read.rows)
    if since is not None:
        def _after(row: dict[str, Any]) -> bool:
            try:
                return parse_ts(row.get("ts")) >= since
            except ValueError:
                return True  # an unreadable time is shown, never hidden
        rows = [r for r in rows if _after(r)]
    if args.job_id:
        leases = {str(r["lease_id"]) for r in read.rows if r.get("job_id") == args.job_id and r.get("lease_id")}
        rows = [r for r in rows if r.get("job_id") == args.job_id or str(r.get("lease_id")) in leases]
    spend = lease_spend(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("kind"))] = counts.get(str(r.get("kind")), 0) + 1
    result = {
        "path": str(read.path),
        "count": len(rows),
        "kinds": counts,
        "spend_usd": round(sum(s.usd for s in spend.values()), 4),
        "unsettled_leases": sorted(s.lease_id for s in spend.values() if not s.settled),
        "rows": rows if args.json else [_row_line(r) for r in rows],
        "torn": [{"line_no": t.line_no, "error": t.error, "text": t.text} for t in read.torn],
    }
    warnings = None
    if read.torn:
        warnings = [{"code": "ledger_torn",
                     "message": f"{len(read.torn)} torn line(s) in the ledger: reported, not dropped (lines "
                                + ", ".join(str(t.line_no) for t in read.torn) + ")"}]
    return ok_envelope(command, result=result, warnings=warnings,
                       next_actions=[next_action(["trialerror", "doctor", "--only", "vastai_ocr_ledger"],
                                                 "the ledger's own check")])


# ---------------------------------------------------------------------------
# ssh-probe (round 6): the cheapest discriminating live experiment
# ---------------------------------------------------------------------------
#: The probe's shell factory, replaced by the tests with a fake (no ssh), and
#: the sleep between its attempts (the tests make it instant).
_shell_factory: Callable[..., Any] | None = None


def _probe_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _cmd_ssh_probe(args: argparse.Namespace) -> dict:
    """Rent the cheapest admissible offer, ask ssh one question, destroy.

    The canary answers the same question 313 s and a whole bootstrap later
    (C4, 2026-09-19); this answers it for a few cents and ships nothing. A DRY
    RUN unless ``--rent``: without it nothing is created and the envelope says
    what it would have rented."""
    from trialerror.vastai import shell as sh
    from trialerror.vastai.errors import HostFailure, VastApiError, VastKeyMissing, VastPlanRefused
    from trialerror.vastai.ledger import Ledger
    from trialerror.vastai.lease import OcrInstanceLease
    from trialerror.vastai.pricing import OfferEstimate, job_cap_usd, offer_query, offer_refusals
    from trialerror.vastai.sshkeys import check_ssh_key
    from trialerror.vastai.sshprobe import NOTHING_SHA256, run_ssh_probe
    from trialerror.vastai.tiers import effective_dph

    command = "vastai.ssh-probe"
    root, cfg, err = _load(args, command, "ssh-probe")
    if err:
        return err
    rerun = _argv("ssh-probe", root)
    rent = bool(args.rent)
    if rent and (args.max_usd is None or float(args.max_usd) <= 0):
        return _refusal(command, "bad_arguments", "--rent needs --max-usd: a probe may not spend without a ceiling",
                        rerun=rerun + ["--rent", "--max-usd", "0.05"],
                        advice=["give --max-usd, e.g. --max-usd 0.05"])
    if float(args.max_minutes) <= 0:
        return _refusal(command, "bad_arguments", f"--max-minutes {args.max_minutes} must be above zero", rerun=rerun,
                        advice=["give --max-minutes, e.g. --max-minutes 12"])
    if cfg.api_key_path is None:
        return _refusal(command, "key-missing", "no [vastai] api_key_path: the offer search needs the vast.ai key",
                        rerun=rerun, advice=["set [vastai] api_key_path and place the key file there"])
    if cfg.ssh_identity_path is None:
        return _refusal(command, "key-missing",
                        "no [vastai] ssh_identity_path: there is no identity to probe with", rerun=rerun,
                        advice=["set [vastai] ssh_identity_path to the private half of the dedicated key pair"])
    if rent and cfg.tier == "high":
        # The high tier's guard governs every rental on it, probe or not.
        from trialerror.vastai.guard import HighTierRefused, verify_high_tier_approval
        try:
            verify_high_tier_approval(root, cfg.api_key_path, now=_now())
        except HighTierRefused as exc:
            return _exc_refusal(command, exc, rerun=rerun, default_code="high-tier-refused")

    lines: list[str] = []
    log_path = Path(args.log_file) if args.log_file else None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch()
        except OSError as exc:
            return _refusal(command, "bad_arguments", f"--log-file {log_path} cannot be written: {exc}", rerun=rerun,
                            advice=["name a writable path for --log-file"])
    from trialerror.cli.offload import worker_log_sink

    say = worker_log_sink(lines, log_path)

    client = _client(cfg)
    # The free pre-flight first: if the account does not hold the key, the
    # probe would only buy the answer round 5 already gets for nothing.
    ssh_key = check_ssh_key(client, cfg.ssh_identity_path)
    if ssh_key.refuses:
        return _refusal(command, "key-missing", f"{ssh_key.message()} Nothing was rented.", rerun=rerun,
                        advice=["register the dedicated key pair's public half with the vast.ai account"],
                        details={"ssh_key": ssh_key.as_dict()})
    say(f"= ssh-probe: the account's key list says {ssh_key.state}")

    try:
        offers = client.search_offers(offer_query(cfg, min_cpu_ram_gb=float(args.min_cpu_ram_gb)))
    except VastKeyMissing as exc:
        return _exc_refusal(command, exc, rerun=rerun, default_code="key-missing")
    except VastApiError as exc:
        return _refusal(command, "api-error", f"the vast.ai offer search failed: {exc}. Nothing was rented.",
                        rerun=rerun, advice=["retry later (market or platform state)"])
    admitted = [o for o in offers if not offer_refusals(o, cfg, min_cpu_ram_gb=float(args.min_cpu_ram_gb))]
    if not admitted:
        return _refusal(
            command, "no-offer",
            f"none of the {len(offers)} offer(s) the search returned passes this root's host requirements; "
            "nothing was rented",
            rerun=rerun, advice=["widen [vastai] tier or relax [vastai.egress] for the probe"],
            details={"searched": len(offers)},
        )
    admitted.sort(key=lambda o: effective_dph(dict(o), cfg.disk_gb))
    offer = dict(admitted[0])
    dph = effective_dph(offer, cfg.disk_gb)
    ttl_s = min(float(args.max_minutes) * 60.0, float(cfg.ttl_cap_s))
    worst = dph * ttl_s / 3600.0
    caps = {
        "probe": None if args.max_usd is None else float(args.max_usd),
        "job": job_cap_usd(cfg, None, None),
        "run": float(cfg.max_run_usd),
    }
    crossed = [name for name, cap in caps.items() if cap is not None and worst > cap]
    estimate = OfferEstimate(
        offer=offer, factor=1.0, dph=dph, compute_s=0.0, transfer_s=0.0, ttl_uncapped_s=ttl_s, ttl_s=ttl_s,
        down_gb=0.0, up_gb=0.0, bandwidth_usd=0.0, worst_usd=worst, pages_per_usd=0.0,
    )
    plan = {
        "offer": estimate.intent_fields(),
        "ttl_s": round(ttl_s, 1),
        "worst_usd": round(worst, 4),
        "caps_usd": caps,
        "caps_crossed": crossed,
        "uploads": "nothing: the probe runs `true` and `nvidia-smi -L` only",
        "ssh_key": ssh_key.as_dict(),
    }
    if crossed:
        return _refusal(
            command, f"cap-{crossed[0]}",
            f"the probe's worst case {_money(worst)} crosses the {crossed[0]} cap "
            f"{_money(caps[crossed[0]])}; nothing was rented",
            rerun=rerun, advice=[f"raise the {crossed[0]} cap, or lower --max-minutes"], details=plan,
        )
    if not rent:
        return ok_envelope(command, result={**plan, "rented": False, "verdict": "would-rent", "log": lines},
                           next_actions=[next_action(rerun + ["--rent", "--max-usd", f"{worst * 2:.2f}"],
                                                     "rent it and ask ssh (a few cents)")])

    # -- the one rental ----------------------------------------------------
    ledger = Ledger()
    lease = OcrInstanceLease(
        client, config_root=root, offers=[estimate], image=cfg.image, disk_gb=cfg.disk_gb, ledger=ledger,
        # The ledger's document fields, honestly: a probe sends no bytes, so
        # its sha256 is the digest OF NO BYTES and its size is 0. The rows stay
        # in the same two kinds, so the probe's spend counts against the run
        # and approval envelopes like any other lease's.
        intent={"probe": "ssh", "shipped": False, "sha256": NOTHING_SHA256, "bytes": 0,
                "note": "ssh probe: no document, no bootstrap"},
        log=say,
    )
    probe = None
    result_code = "no-instance"
    try:
        with lease:
            instance = lease.wait_ready(cfg.poll_interval_s, timeout_s=min(ttl_s, float(cfg.ocr.startup_s)))
            known_hosts = sh.known_hosts_path(lease.state_dir, str(lease.lease_id))
            factory = _shell_factory or sh.SshShell
            shell = factory(
                host=str(instance.get("ssh_host")), port=int(instance.get("ssh_port")),
                identity_path=cfg.ssh_identity_path, known_hosts=known_hosts, verbose=True,
                **({} if args.auth_grace_s is None else {"auth_grace_s": float(args.auth_grace_s)}),
            )
            say(f"= ssh-probe: instance {lease.instance_id} is running; ssh {instance.get('ssh_host')}:"
                f"{instance.get('ssh_port')}")
            probe = run_ssh_probe(
                shell, check=lease.check, timeout_s=max(60.0, lease.remaining_s - 60.0), log=say,
                sleep=lambda seconds: _probe_sleep(seconds),
                **({} if args.auth_grace_s is None else {"auth_grace_s": float(args.auth_grace_s)}),
            )
            result_code = probe.verdict
    except VastPlanRefused as exc:
        result_code = exc.reason_code
        say(f"! ssh-probe: {exc}")
    except HostFailure as exc:
        # An instance that enters `exited` or never reaches `running` inside the
        # startup window: the lease has destroyed it on the way out and the
        # `finally` writes the outcome row either way, so the operator gets the
        # envelope that says so rather than a traceback. `LeaseExpired` is NOT
        # caught here on purpose (a KeyboardInterrupt subclass: the TTL
        # watchdog ends the run loudly, as everywhere else in this lane).
        result_code = "host-failure"
        say(f"! ssh-probe: {exc}")
    except (VastApiError, VastKeyMissing) as exc:
        result_code = "api-error"
        say(f"! ssh-probe: {exc}")
    finally:
        if lease.instance_id is not None:
            try:
                # `result` is the ledger's own vocabulary; the probe's verdict
                # is its own field beside it.
                ledger.append("outcome", **lease.outcome_fields(),
                              result="expired" if lease.expired else "returned", ended_by="probe",
                              probe="ssh", probe_verdict=result_code, shipped=False,
                              ssh_authenticated=bool(probe and probe.authenticated),
                              ssh_denials=(probe.denials if probe else None))
            except Exception as exc:  # noqa: BLE001 - the destroy above matters more than its row
                say(f"! ssh-probe: the outcome row could not be written: {exc}")
    cost = round(lease.estimated_cost_usd(), 6)
    result = {
        **plan,
        "rented": True,
        "lease_id": lease.lease_id,
        "instance_id": lease.instance_id,
        "destroyed": lease.destroyed,
        "destroy_confirmed": lease.destroy_confirmed_epoch is not None,
        "elapsed_s": round(lease.elapsed_s(), 1),
        "estimated_cost_usd": cost,
        "verdict": result_code,
        "probe": probe.as_dict() if probe else None,
        "reading": probe.reading() if probe else f"no ssh answer: {result_code}",
        "log": lines,
    }
    if not args.json and probe is not None:
        result["probe"] = {k: v for k, v in probe.as_dict().items() if k != "attempts"}
    warnings = None
    if not lease.destroyed:
        warnings = [{"code": "vastai_instance_not_destroyed",
                     "message": f"instance {lease.instance_id} was NOT confirmed destroyed: it may still be billing. "
                                "Run `trialerror vastai reap --ocr` and check the console now."}]
    return ok_envelope(command, result=result, warnings=warnings,
                       next_actions=[next_action(_argv("ledger", root, "--json"), "the ledger rows this wrote")])


# ---------------------------------------------------------------------------
# reap
# ---------------------------------------------------------------------------
def _cmd_reap(args: argparse.Namespace) -> dict:
    if not getattr(args, "ocr", False):
        return _cmd_reap_embed(args)
    return _cmd_reap_ocr(args)


def _cmd_reap_ocr(args: argparse.Namespace) -> dict:
    from trialerror.vastai.errors import VastApiError, VastKeyMissing
    from trialerror.vastai.reaper import reap_ocr

    command = "vastai.reap"
    root, cfg, err = _load(args, command, "reap")
    if err:
        return err
    rerun = _argv("reap", root, "--ocr", *(["--dry-run"] if args.dry_run else []))
    if cfg.api_key_path is None:
        return _refusal(command, "key-missing", "no [vastai] api_key_path: the reaper lists the account with the key",
                        rerun=rerun, advice=["set [vastai] api_key_path and place the key file there"])
    log: list[str] = []
    try:
        entries = reap_ocr(_client(cfg), config_root=root, dry_run=args.dry_run, log=log.append)
    except VastKeyMissing as exc:
        return _exc_refusal(command, exc, rerun=rerun, default_code="key-missing")
    except VastApiError as exc:
        return _refusal(command, "api-error",
                        f"the vast.ai account could not be listed ({exc}); nothing was destroyed or recorded",
                        rerun=rerun, advice=["retry later; the label deadlines and the dead man's switch still bound "
                                             "billing meanwhile"])
    result = {"dry_run": bool(args.dry_run), "count": len(entries), "entries": entries, "log": log}
    failed = [e for e in entries if e.get("action") == "destroy_failed"]
    if failed:
        return _refusal(
            command, "destroy_failed",
            f"{len(failed)} instance(s) could NOT be destroyed -- they may still bill; check the vast.ai console",
            rerun=_argv("reap", root, "--ocr"), details=result,
            advice=["destroy them in the vast.ai console, then re-run the reaper to confirm"],
        )
    return ok_envelope(command, result=result, next_actions=[
        next_action(["trialerror", "doctor", "--only", "vastai_live_instances", "--program-root", str(root)],
                    "confirm nothing of this root is live")])


# ---------------------------------------------------------------------------
# the embedding lane's verbs: plan (without --input), run, reap (without --ocr)
# -- the public TrialError copy's embedding backend's, with refusals by name
# that carry nextActions
# ---------------------------------------------------------------------------
def _pargv(verb: str, root: Path | None, *extra: str) -> list[str]:
    argv = ["trialerror", "vastai", verb]
    if root is not None:
        argv += ["--program-root", str(root)]
    return argv + list(extra)


def _load_raw(args: argparse.Namespace, command: str, verb: str) -> tuple[Path | None, dict, dict | None]:
    """The public copy's loader: ``(root, raw toml, None)`` or a refusal."""
    root = _resolve_root(args)
    if root is None:
        return None, {}, _refusal(
            command, "no_program_root", "no --program-root and no trialerror.toml above CWD",
            rerun=_pargv(verb, None, "--program-root", "<program root>"),
            advice=["name the program root, the root whose trialerror.toml carries [ingest.embed] and [vastai]"],
        )
    cfg_path = root / CONFIG_FILENAME
    try:
        raw = load_config(cfg_path).raw if cfg_path.is_file() else {}
    except Exception as exc:  # noqa: BLE001 - surfaced as an envelope, never a traceback
        return root, {}, _refusal(
            command, "bad_config", f"could not read {cfg_path}: {exc}", rerun=_pargv(verb, root),
            advice=[f"fix {CONFIG_FILENAME} under the program root"],
        )
    return root, raw, None


def _embed_client(raw: dict, root: Path) -> Any:
    """``None`` in production (the runner builds its ``VastClient`` from the
    embedding lane's config, as the public copy does); the test seam's
    client when one is set."""
    if _client_factory is None:
        return None
    from trialerror.vastai.tiers import load_vast_config

    return _client_factory(load_vast_config(raw, root).api_key_path)


def _embed_refusal(command: str, verb: str, root: Path | None, exc: BaseException, *extra: str) -> dict:
    return _refusal(
        command, "refused", str(exc), rerun=_pargv(verb, root, *extra),
        advice=list(getattr(exc, "next_actions", ()) or ()) or ["re-run once the cause above is fixed"],
    )


def _cmd_plan_embed(args: argparse.Namespace) -> dict:
    from trialerror.vastai.api import VastApiError
    from trialerror.vastai.guard import HighTierRefused
    from trialerror.vastai.runner import VastRunRefused, prepare_run
    from trialerror.vastai.tiers import PlanRefused, VastConfigError

    command = "vastai.plan"
    root, raw, err = _load_raw(args, command, "plan")
    if err:
        return err
    try:
        prep = prepare_run(root, raw, client=_embed_client(raw, root), max_jobs=args.max_jobs)
    except (VastRunRefused, PlanRefused, HighTierRefused, VastConfigError, VastApiError) as exc:
        return _embed_refusal(command, "plan", root, exc)
    return ok_envelope(
        command,
        result={"plan": prep["plan"].as_dict(), "jobs": [j for j, _ in prep["jobs"]], "notes": prep["cfg"].notes,
                "note": "prices are live offers; throughput/TTL are estimates (docs/VASTAI_EMBED_DESIGN.md 3)"},
        next_actions=[next_action(["trialerror", "vastai", "run"], "rent, embed, destroy")],
    )


def _cmd_run(args: argparse.Namespace) -> dict:
    from trialerror.stores.store import open_store
    from trialerror.vastai import runner
    from trialerror.vastai.api import VastApiError
    from trialerror.vastai.guard import HighTierRefused
    from trialerror.vastai.runner import VastRunRefused
    from trialerror.vastai.tiers import PlanRefused, VastConfigError

    command = "vastai.run"
    root, raw, err = _load_raw(args, command, "run")
    if err:
        return err
    try:
        client = _embed_client(raw, root)
    except VastConfigError as exc:
        return _embed_refusal(command, "run", root, exc)
    store = open_store(root, platform_root=getattr(args, "platform_root", None))
    try:
        summary = runner.run_vastai(root, raw, store=store, client=client, max_jobs=args.max_jobs)
    except (VastRunRefused, PlanRefused, HighTierRefused, VastConfigError, VastApiError) as exc:
        return _embed_refusal(command, "run", root, exc)
    except Exception as exc:  # noqa: BLE001 - the lease has already destroyed the instance on the way out
        return error_envelope(
            command,
            "vastai_run_failed",
            f"{type(exc).__name__}: {exc} -- the lease destroyed its instance on exit (see the run record); "
            "confirm with `trialerror vastai reap --dry-run`",
            next_actions=[next_action(_pargv("reap", root, "--dry-run"),
                                      "confirm no TrialError instance is left on the account")],
        )
    finally:
        store.close()
    return ok_envelope(
        command,
        result=summary,
        next_actions=[next_action(["trialerror", "offload", "kick"], "let the parked embed jobs pick up the published vectors")],
    )


def _cmd_reap_embed(args: argparse.Namespace) -> dict:
    from trialerror.vastai.api import VastApiError, VastClient
    from trialerror.vastai.reaper import reap
    from trialerror.vastai.tiers import VastConfigError, load_vast_config

    command = "vastai.reap"
    root, raw, err = _load_raw(args, command, "reap")
    if err:
        return err
    try:
        cfg = load_vast_config(raw, root)
        client = _client_factory(cfg.api_key_path) if _client_factory is not None else VastClient(cfg.api_key_path)
        result = reap(client, root, dry_run=args.dry_run)
    except (VastApiError, VastConfigError) as exc:
        return _refusal(
            command, "vastai_error", str(exc), rerun=_pargv("reap", root, *(["--dry-run"] if args.dry_run else [])),
            advice=list(getattr(exc, "next_actions", ()) or ())
            or ["fix [vastai] (api_key_path, the key file) or retry once the account can be listed"],
        )
    return ok_envelope(command, result={"reaped": result, "count": len(result), "dry_run": args.dry_run})


# ---------------------------------------------------------------------------
# lock-deps
# ---------------------------------------------------------------------------
def _cmd_lock_deps(args: argparse.Namespace) -> dict:
    from trialerror.vastai import envlock

    command = "vastai.lock-deps"
    root = _resolve_root(args)
    extra = [x for pair in (("--pins", args.pins), ("--out", args.out)) if pair[1] for x in pair]
    rerun = _argv("lock-deps", root, *extra)
    if args.out:
        out = Path(args.out)
    else:
        root, cfg, err = _load(args, command, "lock-deps")
        if err:
            return err
        out = cfg.ocr.requirements_lock
    pins_path = Path(args.pins) if args.pins else envlock.PACKAGED_PINS
    if not pins_path.is_file():
        return _refusal(command, "pins_missing", f"no pins file at {pins_path}", rerun=rerun,
                        advice=["pass --pins <the pip freeze of DEV's marker environment>"])
    try:
        pins = envlock.parse_pins(pins_path.read_text(encoding="utf-8"), origin=pins_path.name)
        result = envlock.build_lock(pins, http=_lock_http or envlock.urllib_get, origin=pins_path.name, now=_now())
        written = envlock.write_lock(out, result)
    except envlock.EnvLockError as exc:
        return _exc_refusal(command, exc, rerun=rerun, default_code="lock_refused")
    except OSError as exc:
        return _refusal(command, "lock_refused", f"could not write the lock: {exc}", rerun=rerun,
                        advice=["pass --out <a writable file>"])
    return ok_envelope(
        command,
        result={"lock": str(written), "pins": str(pins_path), "locked": len(result.locked),
                "dropped": list(result.dropped), "hashes": result.hashes},
        next_actions=[next_action(["trialerror", "vastai", "plan", "--input", "<pdf>"]
                                  + (["--backend-config-root", str(root)] if root else []),
                                  "dry-run one document")],
    )
