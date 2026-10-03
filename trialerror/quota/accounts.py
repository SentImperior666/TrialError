"""The new-account guard (design ``L4_quota-policy.md`` Section 2.5, Section
0 item 3): "On 09-05 a new account, with no history, used up its five-hour
window." An account the meter has not seen for long gets stricter bands
until it has a history.

This module is the PURE decision function only -- it reads an already-loaded
``accounts.json`` document (design Section 2.1's flat
``{label: {first_seen_ts, last_seen_ts, n_rows, windows_seen}}`` shape) and
returns a classification. Nothing here does I/O (no ssh, no file read): the
design's real wiring point is a machine-wide meter script that lives in a
separate repository, outside this implementation's writable scope, so this
lane lands the guard's *logic*, fully tested, without editing that script
itself. ``trialerror quota status`` (:mod:`trialerror.cli.quota`) calls
:func:`classify_account` on the harness side so the same logic is exercised
there too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "NEW_ACCOUNT_AGE_DAYS",
    "NEW_ACCOUNT_MIN_WINDOWS",
    "NEW_YIELD_AT",
    "NEW_STOP_AT",
    "classify_account",
    "band",
]

#: Design Section 2.5 item 2: "less than 7 days old, or ... fewer than 3
#: values" -- either condition alone makes the account ``new``.
NEW_ACCOUNT_AGE_DAYS = 7
NEW_ACCOUNT_MIN_WINDOWS = 3

#: Design Section 2.5 item 2: "YIELD at 60, STOP at 75" -- stricter than the
#: established bands (the budget meter's GO<80<=YIELD<88<=STOP), never looser.
NEW_YIELD_AT = 60.0
NEW_STOP_AT = 75.0


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_account(
    accounts_doc: dict[str, Any] | None,
    label: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify ``label`` against ``accounts_doc`` (the parsed
    ``accounts.json``, design Section 2.1's flat shape).

    Returns a dict with:

    - ``standing``: ``"unlabelled"`` (design Section 2.5 item 3: an empty
      label turns the guard off and normal bands apply), ``"new"`` or
      ``"established"``;
    - ``yield_at``/``stop_at``: the bands to apply, or ``None`` for
      established/unlabelled accounts (meaning "use the normal bands" --
      this function never states what those are, so it cannot drift out of
      sync with wherever they are defined);
    - ``message``: the one line design Section 2.5 specifies verbatim for
      each standing;
    - ``age_days``/``n_windows``: the numbers the message and any caller's
      own reporting are based on (``None`` when unknown, e.g. no entry for
      this label has ever been recorded).

    An account with NO recorded entry at all (never captured under this
    label) is treated as ``new`` -- the design does not name this case
    explicitly, but "no history" is exactly what Section 0 item 3 opens
    with, and reading it any other way would let an account nobody has
    ever seen skip the guard entirely."""
    if now is None:
        now = datetime.now(timezone.utc)
    if label == "":
        return {
            "standing": "unlabelled",
            "yield_at": None,
            "stop_at": None,
            "age_days": None,
            "n_windows": None,
            "message": "account unlabelled: new-account guard off",
        }

    entry = accounts_doc.get(label) if isinstance(accounts_doc, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    first_seen = _parse_ts(entry.get("first_seen_ts"))
    windows_seen = entry.get("windows_seen")
    n_windows = len(windows_seen) if isinstance(windows_seen, list) else 0

    if first_seen is None:
        age_days: float | None = None
        is_new = True
    else:
        age_days = (now - first_seen).total_seconds() / 86400.0
        is_new = age_days < NEW_ACCOUNT_AGE_DAYS or n_windows < NEW_ACCOUNT_MIN_WINDOWS

    if not is_new:
        return {
            "standing": "established",
            "yield_at": None,
            "stop_at": None,
            "age_days": age_days,
            "n_windows": n_windows,
            "message": f"established account {label}",
        }

    if first_seen is not None:
        until = (first_seen + timedelta(days=NEW_ACCOUNT_AGE_DAYS)).strftime("%Y-%m-%d")
    else:
        until = "an unknown date (no history recorded yet)"
    return {
        "standing": "new",
        "yield_at": NEW_YIELD_AT,
        "stop_at": NEW_STOP_AT,
        "age_days": age_days,
        "n_windows": n_windows,
        "message": f"new account {label}: stricter bands until {until} or 3 full windows",
    }


def band(pct: float, classification: dict[str, Any]) -> str | None:
    """Review N9: a pure GO/YIELD/STOP verdict from a window reading
    (``pct``) and a :func:`classify_account` result, so the meter wiring
    becomes a one-line call instead of re-deriving the threshold comparison
    itself. Returns ``None`` for ``established``/``unlabelled`` accounts
    (``yield_at``/``stop_at`` are ``None`` there too) -- meaning "this
    function has no opinion; use the caller's own normal bands", exactly
    the same convention :func:`classify_account` already uses rather than
    hardcoding the budget meter's 80/88 here and risking the two drifting
    apart."""
    yield_at, stop_at = classification.get("yield_at"), classification.get("stop_at")
    if yield_at is None or stop_at is None:
        return None
    if pct >= stop_at:
        return "STOP"
    if pct >= yield_at:
        return "YIELD"
    return "GO"
