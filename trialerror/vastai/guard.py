"""The program fingerprint, canonical JSON, the HMAC, the TTY check, and the
high-tier approval. Ported from the public TrialError copy's embedding
backend; the high-tier approval is unchanged.

``[vastai] tier = "high"`` alone is not enough. A lease on the high tier also
needs a high-tier approval file: a JSON body whose ``mac`` is
``HMAC-SHA256(<vast.ai API key>, canonical body)``. Minting one therefore
needs the key, which only exists in the operator-placed key file, AND an
interactive terminal: :func:`mint_high_tier_approval` itself refuses without a
TTY on both stdin and stdout and makes the operator type back a random
challenge. The check is inside the function, not only in the CLI, so
``python -c`` reaches the same refusal.

Where the file lives (round 4, decision d): beside the key file,
``<dir of api_key_path>/vastai-high-tier.approval``, because on DEV the
backend-config-root is read-only by contract; with no key path, under
``<program_root>/keys/``. One function decides where the writer writes
(:func:`approval_write_path`, the default of :func:`mint_high_tier_approval`,
which ``approve-high`` uses) and one decides what every reader reads
(:func:`approval_read_path`, the default of :func:`verify_high_tier_approval`):
the file beside the key or, when only the public copy's
``<program_root>/keys/vastai-high-tier.approval`` exists (an approval written
before the upgrade), that one. The embedding runner, the OCR pricing, ``plan``
and the doctor's ``vastai_high_tier`` all read through it. :func:`approval_path`
keeps the public name and meaning (that legacy location); with the key at its
default ``<program_root>/keys/vastai.key`` the two locations are the same file.

The egress approval (``trialerror.vastai.egress``) reuses the fingerprint,
the canonical JSON and the HMAC here.
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
    "FUTURE_SKEW",
    "HighTierRefused",
    "approval_path",
    "approval_write_path",
    "approval_read_path",
    "program_fingerprint",
    "canonical_json",
    "mac",
    "parse_ts",
    "is_interactive",
    "verify_high_tier_approval",
    "mint_high_tier_approval",
    "print_high_tier_banner",
]

APPROVAL_FILENAME = "vastai-high-tier.approval"
MAX_APPROVAL_HOURS = 24
#: How far in the future an approval's ``issued`` may be before it is refused
#: as issued in the future (clock skew between minting and checking).
FUTURE_SKEW = timedelta(minutes=5)


class HighTierRefused(RuntimeError):
    """``reason_code`` uses the vast.ai reason codes: ``approval-missing``,
    ``approval-invalid``, ``approval-expired``, ``approval-future``,
    ``key-missing``."""

    def __init__(self, message: str, *, reason_code: str = "approval-invalid") -> None:
        super().__init__(message)
        self.reason_code = reason_code


def approval_path(program_root: Path | str) -> Path:
    """The public copy's location, ``<program_root>/keys/vastai-high-tier.approval``
    (where an approval written before round 4 lies), kept with its meaning."""
    return Path(program_root) / "keys" / APPROVAL_FILENAME


def approval_write_path(program_root: Path | str, api_key_path: Path | str | None) -> Path:
    """Where the writer (``approve-high``) puts the high-tier approval: beside
    the key file (a relative key path is read against ``program_root``); with
    no key path, :func:`approval_path`."""
    if api_key_path is None:
        return approval_path(program_root)
    key = Path(api_key_path)
    return (key if key.is_absolute() else Path(program_root) / key).parent / APPROVAL_FILENAME


def approval_read_path(program_root: Path | str, api_key_path: Path | str | None) -> Path:
    """What every reader reads: :func:`approval_write_path`, falling back to
    :func:`approval_path` when only that one exists (an approval written
    before the upgrade)."""
    target = approval_write_path(program_root, api_key_path)
    legacy = approval_path(program_root)
    return legacy if not target.is_file() and legacy.is_file() else target


def program_fingerprint(program_root: Path | str) -> str:
    return hashlib.sha256(str(Path(program_root).resolve()).encode("utf-8")).hexdigest()


def canonical_json(body: dict[str, Any]) -> bytes:
    """The bytes an approval's ``mac`` signs: every key but ``mac``, sorted."""
    return json.dumps({k: v for k, v in body.items() if k != "mac"}, sort_keys=True).encode("utf-8")


def mac(key: str, body: dict[str, Any]) -> str:
    return hmac.new(key.encode("utf-8"), canonical_json(body), hashlib.sha256).hexdigest()


def parse_ts(value: Any) -> datetime:
    """An ISO timestamp as an aware UTC datetime (``Z`` accepted; a naive
    value is read as UTC). Raises ``ValueError`` on anything else."""
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# the public module's private names, kept so ported call sites and tests read the same
_canonical = canonical_json
_mac = mac
_parse_ts = parse_ts


def _is_interactive() -> bool:
    return is_interactive()


def verify_high_tier_approval(
    program_root: Path | str,
    api_key_path: Path | str | None,
    *,
    now: datetime | None = None,
    path: Path | str | None = None,
    key_reader: Callable[[Any], str] = read_api_key,
) -> dict[str, Any]:
    """Return the approval body, or raise :class:`HighTierRefused` naming
    exactly which condition failed. ``path`` overrides where the file is read
    (default :func:`approval_read_path`: beside the key file, else the legacy
    :func:`approval_path`)."""
    path = Path(path) if path is not None else approval_read_path(program_root, api_key_path)
    hint = (
        "The high tier needs an operator approval: the OPERATOR runs `trialerror vastai approve-high` "
        "in an interactive terminal. An agent must not create or edit this file."
    )
    if not path.is_file():
        raise HighTierRefused(f"tier 'high' refused: no approval at {path}. {hint}", reason_code="approval-missing")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HighTierRefused(f"tier 'high' refused: approval at {path} is unreadable ({exc}). {hint}") from None
    if not isinstance(body, dict) or body.get("tier") != "high":
        raise HighTierRefused(f"tier 'high' refused: approval at {path} is not a high-tier approval. {hint}")
    try:
        key = key_reader(api_key_path)
    except Exception as exc:  # noqa: BLE001 - message names the path only
        raise HighTierRefused(f"tier 'high' refused: cannot verify the approval ({exc})", reason_code="key-missing") from None
    expected = mac(key, body)
    del key
    if not hmac.compare_digest(str(body.get("mac", "")), expected):
        raise HighTierRefused(f"tier 'high' refused: approval at {path} has an invalid signature. {hint}")
    if body.get("program") != program_fingerprint(program_root):
        raise HighTierRefused(f"tier 'high' refused: approval at {path} was issued for a different program root")
    now = now or datetime.now(timezone.utc)
    try:
        issued, expires = parse_ts(body.get("issued")), parse_ts(body.get("expires"))
    except ValueError:
        raise HighTierRefused(f"tier 'high' refused: approval at {path} carries unreadable times") from None
    if expires - issued > timedelta(hours=MAX_APPROVAL_HOURS):
        raise HighTierRefused(f"tier 'high' refused: approval lifetime exceeds {MAX_APPROVAL_HOURS} h")
    if now >= expires:
        raise HighTierRefused(
            f"tier 'high' refused: approval expired at {body.get('expires')}. {hint}", reason_code="approval-expired"
        )
    if now < issued - FUTURE_SKEW:
        raise HighTierRefused("tier 'high' refused: approval is issued in the future", reason_code="approval-future")
    if float(body.get("max_job_usd") or 0) <= 0:
        raise HighTierRefused("tier 'high' refused: approval carries no max_job_usd")
    return body


def mint_high_tier_approval(
    program_root: Path | str,
    api_key_path: Path | str | None,
    *,
    hours: float,
    max_job_usd: float,
    input_fn: Callable[[str], str] = input,
    out: TextIO | None = None,
    now: datetime | None = None,
    path: Path | str | None = None,
) -> Path:
    """OPERATOR ONLY. Refuses without an interactive terminal; asks for a
    typed challenge; writes a signed, expiring approval to ``path`` (default
    :func:`approval_write_path`: beside the key file)."""
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
    body["mac"] = mac(key, body)
    del key
    target = Path(path) if path is not None else approval_write_path(program_root, api_key_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(body, indent=2))
    return target


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
        f"!!! Recorded in the vast.ai ledger; `trialerror doctor` will flag it.\n{bar}\n\n"
    )
    stream.flush()
