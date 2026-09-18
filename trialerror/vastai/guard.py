"""High-tier guard: operator approval, banner (design section 4.1).

``[vastai] tier = "high"`` alone is not enough. A run on the high tier also
needs ``<program_root>/keys/vastai-high-tier.approval``: a JSON body whose
``mac`` is ``HMAC-SHA256(<vast.ai API key>, canonical body)``. Minting one
therefore needs the key, which only exists in the operator-placed key file,
AND an interactive terminal: :func:`mint_high_tier_approval` itself refuses
without a TTY on both stdin and stdout and makes the operator type back a
random challenge. The check is inside the function, not only in the CLI, so
``python -c`` reaches the same refusal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from trialerror.util.atomic import atomic_write_text
from trialerror.vastai.api import read_api_key

__all__ = [
    "APPROVAL_FILENAME",
    "MAX_APPROVAL_HOURS",
    "HighTierRefused",
    "approval_path",
    "program_fingerprint",
    "verify_high_tier_approval",
    "mint_high_tier_approval",
    "print_high_tier_banner",
]

APPROVAL_FILENAME = "vastai-high-tier.approval"
MAX_APPROVAL_HOURS = 24


class HighTierRefused(RuntimeError):
    pass


def approval_path(program_root: Path | str) -> Path:
    return Path(program_root) / "keys" / APPROVAL_FILENAME


def program_fingerprint(program_root: Path | str) -> str:
    return hashlib.sha256(str(Path(program_root).resolve()).encode("utf-8")).hexdigest()


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps({k: v for k, v in body.items() if k != "mac"}, sort_keys=True).encode("utf-8")


def _mac(key: str, body: dict[str, Any]) -> str:
    return hmac.new(key.encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()


def _parse_ts(value: Any) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def verify_high_tier_approval(
    program_root: Path | str,
    api_key_path: Path | str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the approval body, or raise :class:`HighTierRefused` naming
    exactly which condition failed."""
    path = approval_path(program_root)
    hint = (
        "The high tier needs an operator approval: the OPERATOR runs `trialerror vastai approve-high` "
        "in an interactive terminal. An agent must not create or edit this file."
    )
    if not path.is_file():
        raise HighTierRefused(f"tier 'high' refused: no approval at {path}. {hint}")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HighTierRefused(f"tier 'high' refused: approval at {path} is unreadable ({exc}). {hint}") from None
    if not isinstance(body, dict) or body.get("tier") != "high":
        raise HighTierRefused(f"tier 'high' refused: approval at {path} is not a high-tier approval. {hint}")
    try:
        key = read_api_key(api_key_path)
    except Exception as exc:  # noqa: BLE001 - message names the path only
        raise HighTierRefused(f"tier 'high' refused: cannot verify the approval ({exc})") from None
    expected = _mac(key, body)
    del key
    if not hmac.compare_digest(str(body.get("mac", "")), expected):
        raise HighTierRefused(f"tier 'high' refused: approval at {path} has an invalid signature. {hint}")
    if body.get("program") != program_fingerprint(program_root):
        raise HighTierRefused(f"tier 'high' refused: approval at {path} was issued for a different program root")
    now = now or datetime.now(timezone.utc)
    issued, expires = _parse_ts(body.get("issued")), _parse_ts(body.get("expires"))
    if expires - issued > timedelta(hours=MAX_APPROVAL_HOURS):
        raise HighTierRefused(f"tier 'high' refused: approval lifetime exceeds {MAX_APPROVAL_HOURS} h")
    if now >= expires:
        raise HighTierRefused(f"tier 'high' refused: approval expired at {body.get('expires')}. {hint}")
    if now < issued - timedelta(minutes=5):
        raise HighTierRefused("tier 'high' refused: approval is issued in the future")
    if float(body.get("max_job_usd") or 0) <= 0:
        raise HighTierRefused("tier 'high' refused: approval carries no max_job_usd")
    return body


def _is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def mint_high_tier_approval(
    program_root: Path | str,
    api_key_path: Path | str | None,
    *,
    hours: float,
    max_job_usd: float,
    input_fn: Callable[[str], str] = input,
    out: TextIO | None = None,
    now: datetime | None = None,
) -> Path:
    """OPERATOR ONLY. Refuses without an interactive terminal; asks for a
    typed challenge; writes a signed, expiring approval under ``keys/``."""
    out = out or sys.stdout
    if not _is_interactive():
        raise HighTierRefused(
            "approve-high refuses to run without an interactive terminal (stdin and stdout must be a TTY). "
            "High-tier approval is an operator action; agents and scripts cannot grant it."
        )
    if not (0 < hours <= MAX_APPROVAL_HOURS):
        raise HighTierRefused(f"--hours must be in (0, {MAX_APPROVAL_HOURS}]")
    if max_job_usd <= 0:
        raise HighTierRefused("--max-job-usd must be > 0")
    challenge = secrets.token_hex(3)
    out.write(
        f"You are approving the HIGH vast.ai tier for {Path(program_root).resolve()}\n"
        f"for {hours:g} h, at most ${max_job_usd:.2f} per job.\n"
        f"Type {challenge} to confirm: "
    )
    out.flush()
    if input_fn("").strip() != challenge:
        raise HighTierRefused("challenge not matched -- no approval written")
    now = now or datetime.now(timezone.utc)
    body: dict[str, Any] = {
        "tier": "high",
        "program": program_fingerprint(program_root),
        "issued": now.isoformat(),
        "expires": (now + timedelta(hours=hours)).isoformat(),
        "max_job_usd": float(max_job_usd),
        "nonce": secrets.token_hex(16),
    }
    key = read_api_key(api_key_path)
    body["mac"] = _mac(key, body)
    del key
    path = approval_path(program_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(body, indent=2))
    return path


def print_high_tier_banner(plan: dict[str, Any], approval: dict[str, Any], *, stream: TextIO | None = None) -> None:
    stream = stream or sys.stderr
    bar = "!" * 78
    stream.write(
        f"\n{bar}\n!!! HIGH-TIER vast.ai GPU -- OPERATOR-APPROVED SPEND\n"
        f"!!!   gpu        : {plan.get('gpu_name')}  (offer {plan.get('offer_id')})\n"
        f"!!!   price      : ${plan.get('dph_total')}/h\n"
        f"!!!   TTL        : {plan.get('ttl_s')} s (instance destroyed at the latest then)\n"
        f"!!!   worst case : ${plan.get('worst_case_usd')}  (cap ${plan.get('max_job_usd')})\n"
        f"!!!   approval   : expires {approval.get('expires')}\n"
        f"!!! Recorded as a 'vastai_high_tier_use' event; `trialerror doctor` will flag it.\n{bar}\n\n"
    )
    stream.flush()
