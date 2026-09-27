"""The egress policy: which documents may leave DEV for a rented host
(design section 9, charter rule 3).

A document leaves only when ALL of these hold:

1. ``[ingest.ocr] executor = "vastai"`` (the opt-in);
2. ``[vastai.egress]`` names it: its input sha256 is in ``allow_documents``
   (any tier, including a missing or ``unknown`` one), or its manifest's
   ``expect.license_tier`` is in ``allow_license_tiers``;
3. with ``require_approval = true`` (the default), an operator-minted approval
   seals exactly the current ``[vastai.egress]``: purpose
   ``vastai-ocr-egress``, this backend-config-root's fingerprint, an
   ``egress_digest`` equal to :func:`egress_digest`, a valid HMAC keyed by the
   vast.ai API key, not expired, not issued in the future, and a lifetime of at
   most ``approval_max_days``;
4. its size is at most ``[vastai.ocr] max_document_mb``.

:func:`decide_egress` answers with an :class:`EgressDecision`; every refusal
names its reason code and what would change the answer, and says that nothing
was sent. It is called on the manifest alone, after ``claim`` and before
``pull``: a refused document is never even downloaded to DEV.

This module is pure: :func:`sign_egress_approval` builds and signs a body but
checks no TTY and writes nothing. The TTY-only minting flow (challenge,
ledger row, file write) is ``trialerror vastai approve-ocr``, built on it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from trialerror.util.atomic import atomic_write_text
from trialerror.vastai.api import read_api_key
from trialerror.vastai.config import BYTES_PER_MB, VastConfig
from trialerror.vastai.errors import EgressRefused, VastConfigError, VastError
from trialerror.vastai.guard import FUTURE_SKEW, canonical_json, mac, parse_ts, program_fingerprint

__all__ = [
    "EGRESS_PURPOSE",
    "NOTHING_SENT",
    "APPROVE_COMMAND",
    "EgressDecision",
    "egress_digest",
    "egress_summary",
    "approval_body",
    "sign_egress_approval",
    "write_egress_approval",
    "read_egress_approval",
    "verify_egress_approval",
    "document_facts",
    "decide_egress",
    "approval_status",
]

EGRESS_PURPOSE = "vastai-ocr-egress"
NOTHING_SENT = "Nothing was sent to vast.ai."
APPROVE_COMMAND = "the OPERATOR runs `trialerror vastai approve-ocr` in an interactive terminal"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def egress_digest(cfg: VastConfig) -> str:
    """sha256 of the canonical current ``[vastai.egress]`` (normalised:
    defaults filled in, lists sorted and deduplicated), which is what an
    approval seals. Re-ordering a list or writing a default out explicitly
    does not change it; any change of meaning does."""
    return hashlib.sha256(canonical_json(cfg.egress.canonical())).hexdigest()


def egress_summary(cfg: VastConfig) -> dict[str, Any]:
    """What may leave, for the approval body, the mint prompt and the doctor.
    Document sha256s are counted, not listed."""
    e = cfg.egress
    return {
        "allow_license_tiers": list(e.allow_license_tiers),
        "allow_documents": len(e.allow_documents),
        "require_datacenter": e.require_datacenter,
        "require_verified": e.require_verified,
        "allow_geolocations": list(e.allow_geolocations),
        "remote_scratch": e.remote_scratch,
        "shm_required_tiers": list(e.shm_required_tiers),
    }


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value > 0


def approval_body(
    cfg: VastConfig,
    *,
    issued: datetime,
    days: float | None = None,
    max_job_usd: float | None = None,
    max_total_usd: float | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """The design 9.3 body, without ``mac``. Defaults: the configured
    ``approval_max_days``, ``[vastai] max_job_usd`` and ``max_approval_usd``.
    Raises :class:`VastConfigError` for a lifetime outside
    ``(0, approval_max_days]`` or an envelope that is not finite and > 0."""
    days = float(cfg.egress.approval_max_days if days is None else days)
    if not (0 < days <= cfg.egress.approval_max_days):
        raise VastConfigError(
            f"an egress approval lives at most [vastai.egress] approval_max_days = {cfg.egress.approval_max_days:g} "
            f"days; {days:g} was asked"
        )
    job = cfg.max_job_usd if max_job_usd is None else max_job_usd
    total = cfg.max_approval_usd if max_total_usd is None else max_total_usd
    if not (_positive(job) and _positive(total)):
        raise VastConfigError("an egress approval's max_job_usd and max_total_usd must be finite and > 0")
    if total > cfg.max_approval_usd:
        raise VastConfigError(
            f"max_total_usd ${total:.2f} exceeds [vastai] max_approval_usd ${cfg.max_approval_usd:.2f}"
        )
    issued = issued.astimezone(timezone.utc)
    return {
        "purpose": EGRESS_PURPOSE,
        "program": program_fingerprint(cfg.config_root),
        "egress_digest": egress_digest(cfg),
        "egress_summary": egress_summary(cfg),
        "issued": issued.isoformat(),
        "expires": (issued + timedelta(days=days)).isoformat(),
        "max_job_usd": float(job),
        "max_total_usd": float(total),
        "nonce": nonce or secrets.token_hex(16),
    }


def sign_egress_approval(
    cfg: VastConfig,
    *,
    now: datetime,
    days: float | None = None,
    max_job_usd: float | None = None,
    max_total_usd: float | None = None,
    nonce: str | None = None,
    key_reader: Callable[[Any], str] = read_api_key,
) -> dict[str, Any]:
    """Build and sign an egress approval body (pure: no TTY check, nothing
    written). The key is read, used for the HMAC and dropped."""
    body = approval_body(cfg, issued=now, days=days, max_job_usd=max_job_usd, max_total_usd=max_total_usd, nonce=nonce)
    key = key_reader(cfg.api_key_path)
    body["mac"] = mac(key, body)
    del key
    return body


def write_egress_approval(cfg: VastConfig, body: Mapping[str, Any]) -> Path:
    """Write a signed body to ``cfg.approval_path`` (atomically)."""
    if cfg.approval_path is None:
        raise VastConfigError("no approval path: set [vastai] api_key_path or [vastai] approval_path")
    cfg.approval_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cfg.approval_path, json.dumps(dict(body), indent=2))
    return cfg.approval_path


def _refused(code: str, message: str, *actions: str, **details: Any) -> EgressRefused:
    # A missing key refuses every later job of the run too (design 11.1).
    return EgressRefused(code, f"{message} {NOTHING_SENT}", next_actions=list(actions), details=details,
                         disable_executor=code == "key-missing")


def read_egress_approval(cfg: VastConfig) -> dict[str, Any] | None:
    """The approval body at ``cfg.approval_path``; ``None`` when there is no
    file. A file that is not a JSON object raises ``EgressRefused``
    (``approval-invalid``)."""
    path = cfg.approval_path
    if path is None or not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _refused(
            "approval-invalid", f"egress refused: the approval at {path} is unreadable ({type(exc).__name__}).",
            APPROVE_COMMAND,
        ) from None
    if not isinstance(body, dict):
        raise _refused("approval-invalid", f"egress refused: the approval at {path} is not a JSON object.",
                       APPROVE_COMMAND)
    return body


def verify_egress_approval(
    cfg: VastConfig,
    body: Mapping[str, Any] | None,
    *,
    now: datetime,
    key_reader: Callable[[Any], str] = read_api_key,
) -> dict[str, Any]:
    """Return the approval body, or raise :class:`EgressRefused` with the
    reason code of the first condition that fails (missing, invalid,
    unsealed, expired, future)."""
    where = f" at {cfg.approval_path}" if cfg.approval_path is not None else ""
    if body is None:
        raise _refused(
            "approval-missing",
            f"egress refused: no operator approval{where}; [vastai.egress] require_approval = true.",
            APPROVE_COMMAND,
        )
    body = dict(body)
    if body.get("purpose") != EGRESS_PURPOSE:
        raise _refused(
            "approval-invalid",
            f"egress refused: the approval{where} is not a vast.ai OCR egress approval "
            f"(purpose {body.get('purpose')!r}).",
            APPROVE_COMMAND,
        )
    try:
        key = key_reader(cfg.api_key_path)
    except Exception as exc:  # noqa: BLE001 - the message names the path only
        raise _refused("key-missing", f"egress refused: the approval cannot be verified ({exc}).",
                       "place the vast.ai key file at [vastai] api_key_path") from None
    expected = mac(key, body)
    del key
    if not hmac.compare_digest(str(body.get("mac", "")), expected):
        raise _refused(
            "approval-invalid",
            f"egress refused: the approval{where} has an invalid signature (hand-written or edited).",
            APPROVE_COMMAND,
        )
    if body.get("program") != program_fingerprint(cfg.config_root):
        raise _refused(
            "approval-invalid",
            f"egress refused: the approval{where} was issued for a different backend-config-root.",
            APPROVE_COMMAND,
        )
    if body.get("egress_digest") != egress_digest(cfg):
        raise _refused(
            "approval-unsealed",
            "egress refused: the egress policy changed since it was approved ([vastai.egress] no longer matches "
            "the approval's seal).",
            f"{APPROVE_COMMAND} to seal the current [vastai.egress]",
            "or restore [vastai.egress] to the approved policy",
        )
    try:
        issued, expires = parse_ts(body.get("issued")), parse_ts(body.get("expires"))
    except ValueError:
        raise _refused("approval-invalid", f"egress refused: the approval{where} carries unreadable times.",
                       APPROVE_COMMAND) from None
    max_days = cfg.egress.approval_max_days
    if expires - issued > timedelta(days=max_days) or expires <= issued:
        raise _refused(
            "approval-invalid",
            f"egress refused: the approval's lifetime ({issued.isoformat()} to {expires.isoformat()}) is not within "
            f"(0, {max_days:g}] days ([vastai.egress] approval_max_days).",
            APPROVE_COMMAND,
        )
    if now >= expires:
        raise _refused("approval-expired", f"egress refused: the approval expired at {body.get('expires')}.",
                       APPROVE_COMMAND)
    if now < issued - FUTURE_SKEW:
        raise _refused("approval-future", f"egress refused: the approval is issued in the future ({body.get('issued')}).",
                       "check this machine's clock", APPROVE_COMMAND)
    if not (_positive(body.get("max_job_usd")) and _positive(body.get("max_total_usd"))):
        raise _refused("approval-invalid", "egress refused: the approval carries no valid spend envelope.",
                       APPROVE_COMMAND)
    return body


def document_facts(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """``{job_id, doc_id, sha256, bytes, license_tier, input_name}`` of an
    OCR manifest. The input is the one ``expect.input_name`` names, else the
    first. Raises :class:`VastError` (settled as E) when the manifest has no
    well-formed input sha256 and size: the queue side wrote something
    malformed, which no policy decision can fix."""
    expect = manifest.get("expect") if isinstance(manifest.get("expect"), dict) else {}
    inputs = [i for i in (manifest.get("inputs") or []) if isinstance(i, dict)]
    name = expect.get("input_name")
    entry = next((i for i in inputs if name and i.get("name") == name), inputs[0] if inputs else None)
    sha = str((entry or {}).get("sha256") or "").lower()
    size = (entry or {}).get("bytes")
    if not _SHA256_RE.match(sha) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise VastError(
            f"offload manifest {manifest.get('job_id')!r} carries no well-formed input sha256 and byte size; "
            "the egress policy cannot identify the document"
        )
    tier = expect.get("license_tier")
    return {
        "job_id": manifest.get("job_id"),
        "doc_id": manifest.get("doc_id"),
        "sha256": sha,
        "bytes": int(size),
        "license_tier": str(tier) if tier else None,
        "input_name": (entry or {}).get("name"),
    }


@dataclass(frozen=True)
class EgressDecision:
    """``allowed`` with ``basis`` (``"sha256"`` or ``"tier"``), or refused
    with ``reason_code``. ``message`` always says whether anything was sent
    (it never was: this decision precedes the pull)."""

    allowed: bool
    reason_code: str | None
    message: str
    next_actions: tuple[str, ...] = ()
    basis: str | None = None
    job_id: str | None = None
    doc_id: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    license_tier: str | None = None
    approval_nonce: str | None = None
    approval_expires: str | None = None
    approval_max_job_usd: float | None = None
    approval_max_total_usd: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    disable_executor: bool = False

    def refusal(self) -> EgressRefused:
        """The exception to raise for a refused decision (R class)."""
        if self.allowed:
            raise ValueError("an allowed egress decision has no refusal")
        return EgressRefused(
            self.reason_code or "approval-invalid",
            self.message,
            next_actions=list(self.next_actions),
            details={**self.ledger_fields(), **self.details},
            disable_executor=self.disable_executor,
        )

    def raise_if_refused(self) -> "EgressDecision":
        if not self.allowed:
            raise self.refusal()
        return self

    def ledger_fields(self) -> dict[str, Any]:
        """The document's fields for a ledger ``refused`` / ``intent`` row."""
        return {
            "job_id": self.job_id,
            "doc_id": self.doc_id,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "license_tier": self.license_tier,
        }


def decide_egress(
    cfg: VastConfig,
    manifest: Mapping[str, Any],
    *,
    approval: Mapping[str, Any] | None,
    now: datetime,
    key_reader: Callable[[Any], str] = read_api_key,
) -> EgressDecision:
    """Design 9.2 on one OCR manifest (``approval`` = the parsed approval
    body, or ``None`` when there is no file; see :func:`read_egress_approval`).

    Raises :class:`VastConfigError` when ``executor`` is not ``"vastai"`` (the
    vast.ai backend was built without the opt-in: a programming error, not a
    refusal) and :class:`VastError` for a malformed manifest."""
    if not cfg.enabled:
        raise VastConfigError(
            "decide_egress called while [ingest.ocr] executor is not \"vastai\" -- nothing may leave DEV"
        )
    facts = document_facts(manifest)
    base = {k: facts[k] for k in ("job_id", "doc_id", "sha256", "bytes", "license_tier")}

    def refuse(code: str, message: str, *actions: str, **details: Any) -> EgressDecision:
        return EgressDecision(
            False, code, f"{message} {NOTHING_SENT}", tuple(actions), None, **base, details=details
        )

    approved: dict[str, Any] | None = None
    if cfg.egress.require_approval:
        try:
            approved = verify_egress_approval(cfg, approval, now=now, key_reader=key_reader)
        except EgressRefused as exc:
            return EgressDecision(False, exc.reason_code, exc.message, tuple(exc.next_actions), None, **base,
                                  disable_executor=exc.disable_executor)

    egress = cfg.egress
    if facts["sha256"] in egress.allow_documents:
        basis = "sha256"
    elif not facts["license_tier"]:
        return refuse(
            "tier-missing",
            f"egress refused: job {facts['job_id']} carries no expect.license_tier (queued before the tier stamp) and "
            "its sha256 is not in [vastai.egress] allow_documents.",
            f"name its sha256 {facts['sha256']} in [vastai.egress] allow_documents, then re-approve",
            "or re-queue it from a queue side that stamps the licence tier",
        )
    elif facts["license_tier"] in egress.allow_license_tiers:
        basis = "tier"
    else:
        return refuse(
            "tier-not-allowed",
            f"egress refused: licence tier {facts['license_tier']!r} is not in [vastai.egress] allow_license_tiers "
            f"({', '.join(egress.allow_license_tiers) or 'none'}) and the sha256 is not in allow_documents.",
            f"name its sha256 {facts['sha256']} in [vastai.egress] allow_documents, then re-approve",
            "or add the tier to [vastai.egress] allow_license_tiers (not possible for 'unknown'), then re-approve",
            "or leave it for a DEV-GPU run",
        )

    limit = cfg.ocr.max_document_bytes
    if facts["bytes"] > limit:
        return refuse(
            "document-too-large",
            f"egress refused: the document is {facts['bytes'] / BYTES_PER_MB:.1f} MB, over "
            f"[vastai.ocr] max_document_mb = {cfg.ocr.max_document_mb:g}.",
            "raise [vastai.ocr] max_document_mb, or leave it for a DEV-GPU run",
            limit_bytes=limit,
        )

    what = "sha256 named in allow_documents" if basis == "sha256" else f"tier {facts['license_tier']!r} allowed"
    return EgressDecision(
        True,
        None,
        f"egress allowed ({what}); nothing has been sent yet.",
        (),
        basis,
        **base,
        approval_nonce=(approved or {}).get("nonce"),
        approval_expires=(approved or {}).get("expires"),
        approval_max_job_usd=float(approved["max_job_usd"]) if approved else None,
        approval_max_total_usd=float(approved["max_total_usd"]) if approved else None,
    )


# ---------------------------------------------------------------------------
# lane L3 addition: the approval's state as data, for the doctor, `trialerror
# vastai plan` and the mint prompt. Nothing above changes.
# ---------------------------------------------------------------------------
def approval_status(
    cfg: VastConfig,
    *,
    now: datetime,
    key_reader: Callable[[Any], str] = read_api_key,
) -> dict[str, Any]:
    """The current egress approval, described rather than enforced: never
    raises for a refusal. ``valid`` is what :func:`verify_egress_approval`
    says; ``reason_code`` names the first condition that fails
    (``approval-missing`` when there is no file). The body's own fields are
    reported even when it is not valid, so an operator can see what an
    expired or unsealed approval had allowed. Never carries the ``mac``."""
    status: dict[str, Any] = {
        "required": cfg.egress.require_approval,
        "path": str(cfg.approval_path) if cfg.approval_path is not None else None,
        "present": False,
        "valid": False,
        "reason_code": None,
        "message": None,
        "nonce": None,
        "issued": None,
        "expires": None,
        "max_job_usd": None,
        "max_total_usd": None,
        "approved_tiers": [],
        "approved_documents": 0,
    }
    try:
        body = read_egress_approval(cfg)
    except EgressRefused as exc:
        status.update(present=True, reason_code=exc.reason_code, message=exc.message)
        return status
    if body is None:
        status.update(reason_code="approval-missing", message="no operator approval exists")
        return status
    summary = body.get("egress_summary") if isinstance(body.get("egress_summary"), dict) else {}
    tiers = summary.get("allow_license_tiers")
    status.update(
        present=True,
        nonce=body.get("nonce"),
        issued=body.get("issued"),
        expires=body.get("expires"),
        max_job_usd=body.get("max_job_usd"),
        max_total_usd=body.get("max_total_usd"),
        approved_tiers=[str(t) for t in tiers] if isinstance(tiers, list) else [],
        approved_documents=summary.get("allow_documents") if isinstance(summary.get("allow_documents"), int) else 0,
    )
    try:
        verify_egress_approval(cfg, body, now=now, key_reader=key_reader)
    except EgressRefused as exc:
        status.update(reason_code=exc.reason_code, message=exc.message)
        return status
    status.update(valid=True, message="valid")
    return status
