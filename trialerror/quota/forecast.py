"""``trialerror quota forecast`` (design ``L4_quota-policy.md`` Section 2.4):
turn a dollar figure or a token count into window points, using a fitted
``quota_rate`` row and (for tokens) the operator-filled ``[quota.prices]``
config table.

The harness ships no prices (design Section 1 / Section 5): every model's
price table entry is the custodian's, dated, from Anthropic's price page.
Without one, ``--tokens`` refuses by name rather than guessing.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "PriceMissingError",
    "TOKEN_TYPES",
    "YIELD_BAND",
    "OPERATOR_STOP_PCT",
    "price_for_model",
    "parse_tokens_spec",
    "tokens_to_usd",
    "forecast_from_usd",
    "remaining_points",
]

#: design Section 2.4: "input, output, cache_write_5m, cache_write_1h and cache_read".
TOKEN_TYPES = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")

#: design Section 2.4's own two named thresholds: "the lanes' YIELD band
#: (80) and the operator's 95 % rule" -- the budget meter's YIELD line and the
#: operator's own 95 % weekly rule, restated here so `quota forecast`'s output
#: cannot silently drift from either without a reviewer noticing the
#: duplication.
YIELD_BAND = 80.0
OPERATOR_STOP_PCT = 95.0

_TOKEN_SPEC_RE = re.compile(r"^([^:]+):([a-z0-9_]+)=(\d+)$")


class PriceMissingError(Exception):
    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"no_price_for_model: [quota.prices.\"{model}\"] is not configured in trialerror.toml")


def price_for_model(config: dict[str, Any] | None, model: str, token_type: str) -> float | None:
    """Dollars per MILLION tokens of ``token_type`` for ``model``, from
    ``[quota.prices."<model>"]``, or ``None`` when unset."""
    quota_table = (config or {}).get("quota") if isinstance(config, dict) else None
    prices = quota_table.get("prices") if isinstance(quota_table, dict) else None
    model_table = prices.get(model) if isinstance(prices, dict) else None
    if not isinstance(model_table, dict):
        return None
    value = model_table.get(token_type)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_tokens_spec(spec: str) -> list[tuple[str, str, int]]:
    """``"claude-sonnet-5:input=1000,claude-sonnet-5:output=200"`` ->
    ``[(model, type, n), ...]``. Raises ``ValueError`` naming the bad part on
    a malformed entry -- never a silent partial parse."""
    out: list[tuple[str, str, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = _TOKEN_SPEC_RE.match(part)
        if not m:
            raise ValueError(f"--tokens entry {part!r} must be MODEL:TYPE=N")
        model, token_type, n = m.group(1), m.group(2), int(m.group(3))
        if token_type not in TOKEN_TYPES:
            raise ValueError(f"--tokens entry {part!r}: TYPE must be one of {', '.join(TOKEN_TYPES)}")
        out.append((model, token_type, n))
    return out


def tokens_to_usd(entries: list[tuple[str, str, int]], config: dict[str, Any] | None) -> float:
    """Sum ``entries`` to a dollar figure via ``[quota.prices]``. Raises
    :class:`PriceMissingError`, naming the model, on the first one with no
    price configured for the type it asked for."""
    total = 0.0
    for model, token_type, n in entries:
        price_per_million = price_for_model(config, model, token_type)
        if price_per_million is None:
            raise PriceMissingError(model)
        total += (n / 1_000_000.0) * price_per_million
    return total


def forecast_from_usd(usd: float, rate_row: dict[str, Any]) -> dict[str, Any]:
    """``usd`` * a fitted ``quota_rate`` row's ``points_per_usd`` (+ its
    interval, when the fit had one).

    Review B1: ``rate_method``/``rate_n_windows`` are always echoed (not
    only on refusal) so a caller can tell a real estimate from one this
    module would otherwise present as indistinguishable from it -- the
    CLI (:mod:`trialerror.cli.quota`) refuses outright before ever calling
    this when ``rate_row["method"] == "no_estimate"``; this function stays
    a plain multiply so a caller with its own opinion on that still gets a
    number, never a silent 0.0 mistaken for "this wave costs nothing"."""
    rate = rate_row.get("points_per_usd") or 0.0
    ci_low, ci_high = rate_row.get("ci_low"), rate_row.get("ci_high")
    return {
        "usd": usd,
        "window": rate_row.get("window"),
        "points": usd * rate,
        "points_ci_low": usd * ci_low if ci_low is not None else None,
        "points_ci_high": usd * ci_high if ci_high is not None else None,
        "rate_fitted_ts": rate_row.get("fitted_ts"),
        "rate_method": rate_row.get("method"),
        "rate_n_windows": rate_row.get("n_windows"),
    }


def remaining_points(current_pct: float | None) -> dict[str, float | None]:
    """How many points remain, from ``current_pct``, before the lanes' own
    YIELD band and the operator's 95 % rule (design Section 2.4's last
    sentence). Negative means the band is already crossed."""
    if current_pct is None:
        return {"to_yield_80": None, "to_operator_95": None}
    return {
        "to_yield_80": YIELD_BAND - current_pct,
        "to_operator_95": OPERATOR_STOP_PCT - current_pct,
    }
