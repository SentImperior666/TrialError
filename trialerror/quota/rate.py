"""The exchange rate between work and quota (design ``L4_quota-policy.md``
Section 2.3): ``trialerror quota rate fit``.

A ratio estimator over completed plan windows, read entirely from
``quota_capture`` (already imported by :mod:`trialerror.quota.capture_import`).
No model tokens are spent computing this -- everything it reads was already
free (design Section 0 item 1)."""

from __future__ import annotations

import bisect
import random
import statistics
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "fit_rate",
    "mixed_account_windows",
    "mixed_account_windows_seven_day",
    "WINDOW_SECONDS",
    "SEVEN_DAY_WINDOW_SECONDS",
    "MIXED_ACCOUNT_TOLERANCE_S",
]

#: A plan's five-hour window, in seconds -- used to derive a window's START
#: from its (known) `resets_at` END, since `quota_capture` records only the
#: end.
WINDOW_SECONDS = 5 * 3600

#: A plan's seven-day window, in seconds -- same derivation, for the
#: seven-day ratio path (review S3: give it the same window-span exclusions
#: as five-hour, instead of skipping them).
SEVEN_DAY_WINDOW_SECONDS = 7 * 86400

#: Design Section 2.3 step 4: "a capture exists within 15 minutes of its
#: reset time" -- also the tolerance used to decide whether two hosts'
#: captures near the same instant name the same account (Section 2.3 step 4
#: last bullet / Section 3 item 2's "mixed-account guard").
COMPLETE_WITHIN_S = 900
MIXED_ACCOUNT_TOLERANCE_S = 900

#: Design Section 2.3 step 4: "points < 5, too coarse for whole percentages".
MIN_POINTS = 5.0

#: Design Section 2.3 step 5: "500 bootstrap resamples ... with a fixed seed".
BOOTSTRAP_N = 500
BOOTSTRAP_SEED = 42
CI_LOW_PCT, CI_HIGH_PCT = 5, 95  # a 90% interval


@dataclass
class _Row:
    host: str
    epoch: float
    captured_ts: str
    session_id: str | None
    session_cost_usd: float | None
    five_pct: float | None
    five_resets: int | None
    seven_pct: float | None
    seven_resets: int | None


@dataclass
class _WindowResult:
    reset: int
    points: float
    dollars: float
    kept: bool
    reason: str | None
    delta_seven_pct: float | None = None
    epochs: list[float] = field(default_factory=list)


def _rows(conn: sqlite3.Connection, account_label: str) -> list[_Row]:
    cur = conn.execute(
        "SELECT host, epoch, captured_ts, session_id, session_cost_usd, five_pct, five_resets, "
        "seven_pct, seven_resets FROM quota_capture WHERE account_label = ? ORDER BY epoch ASC",
        (account_label,),
    )
    return [_Row(*row) for row in cur.fetchall()]


def _mixed_windows(rows: list[_Row], *, window_seconds: int, tolerance_s: int, reset_attr: str) -> set[int]:
    """``resets_at`` values (of whichever window kind ``reset_attr`` names)
    that must be excluded because a capture on one host falls INSIDE another
    host's window span while the two disagree on the reset time by more than
    ``tolerance_s`` -- design Section 2.3 step 4's last bullet: "the two
    hosts' captures disagree on `five_resets` for the same time, meaning
    they are on different accounts."

    Fix round 2 (review S2): the original form only compared captures taken
    within ``tolerance_s`` of EACH OTHER, so a host with sparse captures (an
    idle machine's statusLine only ticks on UI events) could sit inside
    another host's window the whole time without ever landing near one of
    its captures -- the common case, not an edge, and it silently kept the
    other account's window as an extra one of "this" account's. Comparing
    against the WINDOW SPAN instead (``reset_a - window_seconds <= t <=
    reset_a``) needs no coincidence of timing.

    O(hosts² · log captures) via a per-host, epoch-sorted capture list and
    :func:`bisect.bisect_left`/``bisect_right`` for the window-span slice
    (review N8), rather than the prior pairwise O(captures²)."""
    relevant = [r for r in rows if getattr(r, reset_attr) is not None]
    by_host: dict[str, list[_Row]] = {}
    for r in relevant:
        by_host.setdefault(r.host, []).append(r)
    hosts = sorted(by_host)
    for h in hosts:
        by_host[h].sort(key=lambda r: r.epoch)
    epochs_by_host = {h: [r.epoch for r in by_host[h]] for h in hosts}

    flagged: set[int] = set()
    for host_a in hosts:
        windows_a = sorted({getattr(r, reset_attr) for r in by_host[host_a]})
        for host_b in hosts:
            if host_b == host_a:
                continue
            epochs_b = epochs_by_host[host_b]
            rows_b = by_host[host_b]
            for reset_a in windows_a:
                window_start = reset_a - window_seconds
                lo = bisect.bisect_left(epochs_b, window_start)
                hi = bisect.bisect_right(epochs_b, reset_a)
                for r_b in rows_b[lo:hi]:
                    reset_b = getattr(r_b, reset_attr)
                    if reset_b is not None and abs(reset_a - reset_b) > tolerance_s:
                        flagged.add(reset_a)
                        flagged.add(reset_b)
    return flagged


def mixed_account_windows(conn: sqlite3.Connection, account_label: str, *, tolerance_s: int = MIXED_ACCOUNT_TOLERANCE_S) -> set[int]:
    """The five-hour form of :func:`_mixed_windows`, reading straight from
    ``quota_capture`` -- used by ``quota import``'s own report and by
    :func:`fit_rate`'s five-hour path."""
    return _mixed_windows(_rows(conn, account_label), window_seconds=WINDOW_SECONDS, tolerance_s=tolerance_s, reset_attr="five_resets")


def mixed_account_windows_seven_day(conn: sqlite3.Connection, account_label: str, *, tolerance_s: int = MIXED_ACCOUNT_TOLERANCE_S) -> set[int]:
    """The seven-day form of :func:`_mixed_windows` (review S3: the
    seven-day ratio path gets the same mixed-account exclusion as
    five-hour, not none at all)."""
    return _mixed_windows(_rows(conn, account_label), window_seconds=SEVEN_DAY_WINDOW_SECONDS, tolerance_s=tolerance_s, reset_attr="seven_resets")


def _headless_active_default(*_args: Any, **_kwargs: Any) -> bool:
    return False


def _window_dollars(group: list[_Row], all_rows: list[_Row], window_start: float) -> tuple[float, bool]:
    """(dollars, partial_start) for one five-hour window's rows ``group``,
    per design Section 2.3 step 3."""
    by_session: dict[str, list[_Row]] = {}
    for r in group:
        if r.session_id:
            by_session.setdefault(r.session_id, []).append(r)
    total = 0.0
    partial_start = False
    for sid, session_rows in by_session.items():
        session_rows.sort(key=lambda r: r.epoch)
        in_window_costs = [r.session_cost_usd for r in session_rows if r.session_cost_usd is not None]
        if not in_window_costs:
            continue
        last_in_window = in_window_costs[-1]
        before = [
            r.session_cost_usd
            for r in all_rows
            if r.session_id == sid and r.epoch < window_start and r.session_cost_usd is not None
        ]
        if before:
            prior = before[-1]  # all_rows is epoch-ascending
            total += last_in_window - prior
        else:
            first_cost = in_window_costs[0]
            # design Section 2.3 step 3, verbatim: "less than 0.50" -- N6:
            # this was `<= 0.50`, one cent too generous at the boundary.
            if first_cost < 0.50:
                total += last_in_window - 0.0
            else:
                partial_start = True
    return total, partial_start


def _fit_five_hour(
    rows: list[_Row],
    *,
    mixed: set[int],
    headless_active: Callable[[int, float, float], bool],
) -> list[_WindowResult]:
    groups: dict[int, list[_Row]] = {}
    for r in rows:
        if r.five_resets is not None:
            groups.setdefault(r.five_resets, []).append(r)
    resets_sorted = sorted(groups)
    results: list[_WindowResult] = []
    for reset in resets_sorted:
        group = groups[reset]
        epochs = [r.epoch for r in group]
        later_started = any(other > reset for other in resets_sorted)
        complete = later_started or any(e >= reset - COMPLETE_WITHIN_S for e in epochs)
        if not complete:
            continue  # not yet decidable either way -- simply not a window to judge
        points = max((r.five_pct for r in group if r.five_pct is not None), default=None)
        window_start = reset - WINDOW_SECONDS
        dollars, partial_start = _window_dollars(group, rows, window_start)
        # review S3, scenario 1: a seven-day reset can land INSIDE a
        # five-hour window (seven_pct drops from ~100 back near 0 mid-group),
        # which made `last - first` swing hugely negative. group is already
        # epoch-ascending (built from `rows`, itself ORDER BY epoch), so a
        # single `seven_resets` value across every seven_pct-bearing row
        # means the window never crossed a seven-day reset; more than one
        # means it did, and the delta is unusable for the fallback
        # multiplier -- skip it (None) rather than compute a nonsense swing.
        seven_vals = [r.seven_pct for r in group if r.seven_pct is not None]
        seven_resets_seen = {r.seven_resets for r in group if r.seven_pct is not None and r.seven_resets is not None}
        if len(seven_vals) >= 2 and len(seven_resets_seen) <= 1:
            delta_seven = seven_vals[-1] - seven_vals[0]
        else:
            delta_seven = None

        reason = None
        if points is None or points < MIN_POINTS:
            reason = "too_few_points"
        elif partial_start:
            reason = "partial_start"
        elif dollars <= 0:
            reason = "non_positive_dollars"
        elif reset in mixed:
            reason = "mixed_account"
        elif headless_active(reset, window_start, reset):
            reason = "headless_active"
        results.append(
            _WindowResult(
                reset=reset,
                points=points or 0.0,
                dollars=dollars,
                kept=reason is None,
                reason=reason,
                delta_seven_pct=delta_seven,
                epochs=epochs,
            )
        )
    return results


def _fit_seven_day(
    rows: list[_Row],
    *,
    mixed: set[int],
    headless_active: Callable[[int, float, float], bool],
) -> list[_WindowResult]:
    """Seven-day windows, judged by the SAME rules as
    :func:`_fit_five_hour` (review S3: "give it the same exclusions" --
    the original ratio path skipped mixed-account and headless entirely and
    stored no exclusion reasons)."""
    groups: dict[int, list[_Row]] = {}
    for r in rows:
        if r.seven_resets is not None:
            groups.setdefault(r.seven_resets, []).append(r)
    resets_sorted = sorted(groups)
    results: list[_WindowResult] = []
    for reset in resets_sorted:
        group = groups[reset]
        epochs = [r.epoch for r in group]
        later_started = any(other > reset for other in resets_sorted)
        complete = later_started or any(e >= reset - COMPLETE_WITHIN_S for e in epochs)
        if not complete:
            continue
        points = max((r.seven_pct for r in group if r.seven_pct is not None), default=None)
        window_start = reset - SEVEN_DAY_WINDOW_SECONDS
        dollars, partial_start = _window_dollars(group, rows, window_start)

        reason = None
        if points is None or points < MIN_POINTS:
            reason = "too_few_points"
        elif partial_start:
            reason = "partial_start"
        elif dollars <= 0:
            reason = "non_positive_dollars"
        elif reset in mixed:
            reason = "mixed_account"
        elif headless_active(reset, window_start, reset):
            reason = "headless_active"
        results.append(
            _WindowResult(reset=reset, points=points or 0.0, dollars=dollars, kept=reason is None, reason=reason, epochs=epochs)
        )
    return results


def _bootstrap_ci(pairs: list[tuple[float, float]], *, n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> tuple[float | None, float | None]:
    if len(pairs) < 2:
        return None, None
    rng = random.Random(seed)
    ratios = []
    for _ in range(n):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        dollars = sum(d for _, d in sample)
        if dollars <= 0:
            continue
        ratios.append(sum(p for p, _ in sample) / dollars)
    if not ratios:
        return None, None
    ratios.sort()

    def pct(p: float) -> float:
        idx = min(len(ratios) - 1, max(0, round(p / 100 * (len(ratios) - 1))))
        return ratios[idx]

    return pct(CI_LOW_PCT), pct(CI_HIGH_PCT)


def fit_rate(
    conn: sqlite3.Connection,
    account_label: str,
    *,
    now_ts: str,
    headless_active: Callable[[int, float, float], bool] | None = None,
    prior_points_per_usd: float | None = None,
) -> dict[str, Any]:
    """Fit both window kinds for ``account_label`` and store one
    ``quota_rate`` row per kind (design Section 2.3 steps 2-8, Section 2.3's
    seven-day fallback). Returns ``{"five_hour": {...}, "seven_day": {...}}``,
    each the row as inserted plus ``windows`` (every window considered, kept
    or not, with its reason).

    ``headless_active(five_resets, window_start_epoch, window_end_epoch) ->
    bool`` is design Section 2.3 step 4's "a headless unit ... active in the
    window on either host" exclusion -- injected rather than queried
    in-line here, so this module stays ignorant of :mod:`trialerror.units`'
    own schema; the CLI wires the real check (see
    :mod:`trialerror.cli.quota`)."""
    headless_active = headless_active or _headless_active_default
    rows = _rows(conn, account_label)
    mixed = mixed_account_windows(conn, account_label)

    five_results = _fit_five_hour(rows, mixed=mixed, headless_active=headless_active)
    five_kept = [r for r in five_results if r.kept]
    five_excluded = len(five_results) - len(five_kept)
    five_ratio = (sum(r.points for r in five_kept) / sum(r.dollars for r in five_kept)) if five_kept else None
    five_ci_low, five_ci_high = _bootstrap_ci([(r.points, r.dollars) for r in five_kept])
    span_ts = [r.captured_ts for r in rows] or [now_ts]

    detail: dict[str, Any] = {
        "n_windows": len(five_kept),
        "excluded_windows": five_excluded,
        "exclusion_reasons": _reason_counts(five_results),
    }
    if prior_points_per_usd:
        detail["prior_points_per_usd"] = prior_points_per_usd
        detail["ratio_to_prior"] = (five_ratio / prior_points_per_usd) if five_ratio is not None else None
    else:
        detail["prior_check"] = "no price table configured; sanity check skipped"

    five_out = {
        "account_label": account_label,
        "window": "five_hour",
        # B1: zero usable windows must never be presented as a real rate of
        # 0.000 points/dollar -- `quota forecast --usd 50` would otherwise
        # answer "0 points" for a wave that in fact costs something, with
        # nothing else in the row saying the estimate does not exist. The
        # NOT NULL column still gets a placeholder 0.0; `method` is the flag
        # every reader (rate show, forecast) must check first.
        "points_per_usd": five_ratio if five_ratio is not None else 0.0,
        "ci_low": five_ci_low,
        "ci_high": five_ci_high,
        "n_windows": len(five_kept),
        "excluded_windows": five_excluded,
        "fit_from_ts": min(span_ts),
        "fit_to_ts": max(span_ts),
        "fitted_ts": now_ts,
        "method": "ratio" if five_ratio is not None else "no_estimate",
        "detail": detail,
    }

    # Design Section 2.3 step 6: fewer than 2 complete seven-day windows ->
    # fall back to the five-hour data. review S3: the ratio path (when
    # attempted) now gets the same mixed-account/headless exclusions as
    # five-hour, with reasons counted, rather than none at all.
    seven_mixed = _mixed_windows(rows, window_seconds=SEVEN_DAY_WINDOW_SECONDS, tolerance_s=MIXED_ACCOUNT_TOLERANCE_S, reset_attr="seven_resets")
    seven_results = _fit_seven_day(rows, mixed=seven_mixed, headless_active=headless_active)
    seven_kept = [r for r in seven_results if r.kept]

    if len(seven_results) >= 2 and five_ratio is not None:
        seven_dollars_sum = sum(r.dollars for r in seven_kept)
        seven_ratio = (sum(r.points for r in seven_kept) / seven_dollars_sum) if seven_kept and seven_dollars_sum > 0 else None
        seven_ci_low, seven_ci_high = _bootstrap_ci([(r.points, r.dollars) for r in seven_kept])
        attempted_method = "ratio"
        seven_n, seven_excluded = len(seven_kept), len(seven_results) - len(seven_kept)
        seven_detail: dict[str, Any] = {
            "n_windows": seven_n,
            "excluded_windows": seven_excluded,
            "exclusion_reasons": _reason_counts(seven_results),
        }
    else:
        # review S3, scenario 2: `and r.points > 0` alone made a delta of
        # exactly 0.0 falsy and dropped it, biasing the median multiplier up
        # when the true 7d/5h ratio is small. `is not None` keeps it.
        ratios7to5 = [r.delta_seven_pct / r.points for r in five_kept if r.delta_seven_pct is not None and r.points > 0]
        multiplier = statistics.median(ratios7to5) if ratios7to5 else None
        seven_ratio = (five_ratio * multiplier) if (five_ratio is not None and multiplier is not None) else None
        seven_ci_low = seven_ci_high = None
        attempted_method = "five_hour_fallback"
        seven_n, seven_excluded = len(five_kept), five_excluded
        seven_detail = {"fallback_from": "five_hour", "n_five_hour_deltas_used": len(ratios7to5)}

    # B1: a fit with no usable data is never presented as a real rate.
    seven_method = attempted_method if seven_ratio is not None else "no_estimate"

    seven_out = {
        "account_label": account_label,
        "window": "seven_day",
        "points_per_usd": seven_ratio if seven_ratio is not None else 0.0,
        "ci_low": seven_ci_low,
        "ci_high": seven_ci_high,
        "n_windows": seven_n,
        "excluded_windows": seven_excluded,
        "fit_from_ts": min(span_ts),
        "fit_to_ts": max(span_ts),
        "fitted_ts": now_ts,
        "method": seven_method,
        "detail": seven_detail,
    }

    for out in (five_out, seven_out):
        conn.execute(
            "INSERT INTO quota_rate (account_label, window, points_per_usd, ci_low, ci_high, n_windows, "
            "excluded_windows, fit_from_ts, fit_to_ts, fitted_ts, method, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                out["account_label"],
                out["window"],
                out["points_per_usd"],
                out["ci_low"],
                out["ci_high"],
                out["n_windows"],
                out["excluded_windows"],
                out["fit_from_ts"],
                out["fit_to_ts"],
                out["fitted_ts"],
                out["method"],
                _json(out["detail"]),
            ),
        )
    conn.commit()

    five_out["windows"] = [_window_dict(r) for r in five_results]
    return {"five_hour": five_out, "seven_day": seven_out}


def _reason_counts(results: list[_WindowResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        if r.reason:
            counts[r.reason] = counts.get(r.reason, 0) + 1
    return counts


def _window_dict(r: _WindowResult) -> dict[str, Any]:
    return {"reset": r.reset, "points": r.points, "dollars": r.dollars, "kept": r.kept, "reason": r.reason}


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
