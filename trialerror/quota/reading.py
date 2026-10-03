"""The plan-quota reading: the figure the lanes and the admission verb band on, computed from capture rows.

Pure functions, no I/O. The caller reads the rows (``latest.json`` and the tail of ``rate_limits.jsonl``, one set per
host) and passes them in; host names come from the caller and never appear here.

**Why not "the latest row".** Several sessions write the same capture. A quiet session re-renders its own OLD numbers
about an hour later and overwrites a fresher session's higher figure: the file then looks fresh and reads low (44 %
where the truth was 69 %). The reading here is the **highest figure seen in the current window**, and a figure counts as
*fresh* only when the session that wrote it has just made a real call.

**A reset early in a window.** When the operator presses the free reset, the weekly figure falls to almost nothing but
keeps its old reset time. A plain "highest figure" rule would keep reading the pre-reset figure for days. A fall in one
session's own figure marks such a reset, and older rows stop counting.

**A bound for quiet periods.** With no fresh figure, the answer can still be a safe upper bound: the highest figure
seen, grown by the fastest rise ever observed. A bound is labelled as a bound (:attr:`WindowReading.basis`), and only a
real reading may ever justify stopping work.

Constants: :data:`DEFAULT_BOUNDS` (measured; the method and date are on the constants). Config may raise them, never
lower them (:func:`bounds_from_config`).
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "Row",
    "Bounds",
    "BoundsSet",
    "WindowReading",
    "Reading",
    "DEFAULT_BOUNDS",
    "CLOCK_SKEW_S",
    "SAME_ACCOUNT_S",
    "parse_row",
    "account_rows",
    "reading",
    "bounds_from_config",
]

#: A stamp more than this many seconds in the future is unusable (clock skew between hosts).
CLOCK_SKEW_S = 120
#: Two reset times this close belong to one account (a plan's windows reset at one instant for every session).
SAME_ACCOUNT_S = 120


@dataclass(frozen=True)
class Row:
    host: str
    epoch: float
    captured_ts: str
    session_id: str | None
    account_label: str
    session_cost_usd: float | None
    five_pct: float | None
    five_resets: int | None
    seven_pct: float | None
    seven_resets: int | None


@dataclass(frozen=True)
class Bounds:
    """Per window kind. Percentages are points of the window; times are minutes or seconds as named.

    * ``fresh_s`` -- a fresh sighting this recent is a reading (``upper`` = the window maximum, no growth term).
    * ``max_age_s`` -- beyond this age since the last fresh sighting there is no bound (unknown).
    * ``margin`` and ``slope`` -- a bound is ``base + margin + slope x minutes`` since the last fresh sighting.
    * ``start`` and ``after_slope`` -- after a reset the bound is ``start + after_slope x minutes`` since the old
      window was last seen.
    * ``reset_drop`` -- a fall in one session's own figure of MORE than this many points marks a reset."""

    fresh_s: float
    max_age_s: float
    margin: float
    slope: float
    start: float
    after_slope: float
    reset_drop: float

    def growth(self, minutes: float) -> float:
        return self.margin + self.slope * minutes

    def after_reset(self, minutes: float) -> float:
        return self.start + self.after_slope * max(minutes, 0.0)


@dataclass(frozen=True)
class BoundsSet:
    five: Bounds
    seven: Bounds


#: Measured 2026-09-28 from both hosts' capture histories (1,083 and 1,279 rows, 2026-09-05 to 2026-09-28), by a
#: script kept in the lane report. Method: (1) from every fresh sighting, the rise of every later same-window row over
#: the window maximum up to that sighting, over spans of 10-120 minutes (five-hour) and 30-720 minutes (seven-day);
#: the smallest line ``margin + slope x span`` above every pair; (2) the highest figure seen ``m`` minutes after the
#: old window was last seen, over every window change and the one early reset; the smallest line ``start + slope x m``;
#: (3) every slope multiplied by 1.5, because more lanes may run in parallel than ever did. Found: five-hour margin 5
#: needs slope 0.95 (x 1.5 = 1.43); after a reset, start 5 needs 0.47 (x 1.5 = 0.70); seven-day margin 5 needs 0.14
#: (x 1.5 = 0.21); after a reset, start 5 needs 0.083 (x 1.5 = 0.125). A one-point fall inside one window occurs on
#: rounding (8 times in 2,362 rows); the two real resets fell by 95 and 96 points, so ``reset_drop`` is 3.
#:
#: ``fresh_s`` (D2, 2026-09-28): the review found arrivals up to 7 points above a fresh reading within its old 10-minute
#: window -- a reading had no growth term. Shortened to 120 s (five-hour) and 600 s (seven-day); past that age the
#: basis is ``bound`` and the growth term applies from the start.
DEFAULT_BOUNDS = BoundsSet(
    five=Bounds(fresh_s=120.0, max_age_s=3600.0, margin=5.0, slope=1.45, start=5.0, after_slope=0.70, reset_drop=3.0),
    seven=Bounds(fresh_s=600.0, max_age_s=43200.0, margin=5.0, slope=0.21, start=5.0, after_slope=0.13, reset_drop=3.0),
)

_RAISE_ONLY = ("margin", "slope", "start", "after_slope", "reset_drop")  # a larger value is the safer one
_LOWER_ONLY = ("fresh_s", "max_age_s")  # a smaller value is the safer one


def bounds_from_config(cfg: Mapping[str, Any] | None) -> BoundsSet:
    """``[quota.bound.five_hour]`` and ``[quota.bound.seven_day]`` (or ``five``/``seven``) tables applied to the
    defaults. Config may make a bound WIDER (raise ``margin``, ``slope``, ``start``, ``after_slope``, ``reset_drop``;
    lower ``fresh_s``, ``max_age_s``), never narrower: an unsafe value is ignored. Non-numbers are ignored."""
    cfg = cfg if isinstance(cfg, Mapping) else {}
    out = []
    for name, aliases, default in (("five", ("five_hour", "five"), DEFAULT_BOUNDS.five),
                                   ("seven", ("seven_day", "seven"), DEFAULT_BOUNDS.seven)):
        table: Mapping[str, Any] = {}
        for alias in aliases:
            if isinstance(cfg.get(alias), Mapping):
                table = cfg[alias]
                break
        values = {}
        for key in _RAISE_ONLY + _LOWER_ONLY:
            base = getattr(default, key)
            v = table.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                values[key] = base
            elif key in _RAISE_ONLY:
                values[key] = max(base, float(v))
            else:
                values[key] = min(base, float(v)) if v > 0 else base
        out.append(Bounds(**values))
    return BoundsSet(five=out[0], seven=out[1])


@dataclass(frozen=True)
class WindowReading:
    """One window kind's answer.

    ``basis`` is ``reading`` (a fresh real sighting; ``upper`` is the window maximum), ``bound`` (no fresh sighting;
    ``upper`` is a safe upper bound), ``bound_after_reset`` (the window has ended and no figure of the new one exists;
    ``upper`` counts from the last moment the old window was seen) or ``unknown``. ``value`` is the highest figure seen
    in the window (``None`` when there is none, and after a reset)."""

    basis: str
    value: float | None = None
    upper: float | None = None
    resets: int | None = None
    t_fresh: float | None = None
    age_min: float | None = None
    rows_dropped_early_reset: int = 0
    rows_dropped_implausible: int = 0
    src_host: str | None = None


@dataclass(frozen=True)
class Reading:
    five: WindowReading
    seven: WindowReading
    src_host: str | None = None
    acct: str = ""


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _pct(v: Any) -> float | None:
    n = _num(v)
    return None if n is None else round(n, 1)  # float noise: 28.999999999999996 -> 29.0


def _epoch(v: Any) -> float | None:
    """A time as epoch seconds: a number as is, an ISO string parsed; ``None`` when it is neither."""
    n = _num(v)
    if n is not None:
        return n
    if isinstance(v, str) and v.strip():
        try:
            return _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def parse_row(obj: Any, host: str, now: float | None = None, clock_offset: float = 0.0) -> Row | None:
    """A history row or ``latest.json`` as a :class:`Row`; ``None`` when it has no usable time or shape.

    ``clock_offset`` (D1) is that host's clock reading minus the caller's, read in the same call as the rows
    themselves; it is subtracted from the row's epoch BEFORE anything else, so every host's rows land on the caller's
    own clock and a host that merely runs fast or slow is never mistaken for a future or stale writer. With ``now``,
    a stamp (after correction) more than :data:`CLOCK_SKEW_S` in the future is dropped -- that rule now catches only a
    writer whose own stamp is off from ITS host's clock, not the host's clock itself. ``captured_ts`` keeps the raw,
    uncorrected string for display. Never raises."""
    try:
        if not isinstance(obj, dict) or not obj:
            return None
        epoch = _epoch(obj.get("epoch"))
        if epoch is None:
            epoch = _epoch(obj.get("captured_ts"))
        if epoch is None:
            return None
        epoch -= clock_offset
        if now is not None and epoch > now + CLOCK_SKEW_S:
            return None
        rl = obj.get("rate_limits")
        rl = rl if isinstance(rl, dict) else {}
        fh = rl.get("five_hour") if isinstance(rl.get("five_hour"), dict) else {}
        sd = rl.get("seven_day") if isinstance(rl.get("seven_day"), dict) else {}
        five_r, seven_r = _epoch(fh.get("resets_at")), _epoch(sd.get("resets_at"))
        label = obj.get("account_label")
        sid = obj.get("session_id")
        return Row(
            host=str(host),
            epoch=epoch,
            captured_ts=obj.get("captured_ts") if isinstance(obj.get("captured_ts"), str) else "",
            session_id=sid if isinstance(sid, str) and sid else None,
            account_label=label if isinstance(label, str) else "",
            session_cost_usd=_num(obj.get("session_cost_usd")),
            five_pct=_pct(fh.get("used_percentage")),
            five_resets=None if five_r is None else int(round(five_r)),
            seven_pct=_pct(sd.get("used_percentage")),
            seven_resets=None if seven_r is None else int(round(seven_r)),
        )
    except Exception:  # noqa: BLE001 - a malformed row must never hide the other rows
        return None


def _dedupe(rows: Iterable[Row]) -> list[Row]:
    """``latest.json`` repeats the newest history row: keep one of each (host, epoch, session)."""
    seen: set[tuple] = set()
    out = []
    for r in sorted(rows, key=lambda r: r.epoch):
        key = (r.host, r.epoch, r.session_id, r.five_pct, r.seven_pct)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


def _near(x: int | None, ys: Iterable[int | None]) -> bool:
    return x is not None and any(y is not None and abs(x - y) <= SAME_ACCOUNT_S for y in ys)


def _joins(r: Row, anchors: list[Row], live_five: set[int], ref: float) -> bool:
    """Whether an unlabelled row belongs to the labelled account. Its weekly reset time must agree with a labelled
    anchor's (a row with no weekly figure: its five-hour time must). It is excluded when its five-hour window is live
    and differs from every live five-hour window of the labelled account: two live five-hour windows at once cannot be
    one account, though two accounts can share the weekly time (weekly resets fall on the hour). A five-hour window that
    has ended cannot contradict one."""
    if r.seven_resets is not None:
        if not _near(r.seven_resets, (a.seven_resets for a in anchors)):
            return False
    elif not _near(r.five_resets, (a.five_resets for a in anchors)):
        return False
    if r.five_resets is not None and r.five_resets > ref and live_five and not _near(r.five_resets, live_five):
        return False
    return True


def account_rows(rows: Iterable[Row], caller_label: str, caller_host: str = "", now: float | None = None) -> list[Row]:
    """The caller's account's rows.

    With a caller label: rows whose ``account_label`` equals it, plus unlabelled rows that join it (see ``_joins``: the
    weekly reset time agrees with a labelled row's, and no live five-hour window contradicts). Rows labelled otherwise
    are another account's. Without a caller label: today's
    test, reset times that agree within 120 s; when the hosts' latest reset times disagree, the caller's host only
    (``caller_host`` has no default: with none named, disagreeing hosts keep no rows, and the answer is unknown).
    With ``now``, only a LIVE pair decides (both reset times still ahead of ``now``, and for the five-hour pair both
    hosts' latest rows under ten minutes old): a host whose last capture belongs to an ended window says nothing about
    the account."""
    rows = _dedupe(rows)
    if caller_label:
        kept = [r for r in rows if r.account_label == caller_label]
        loose = [r for r in rows if not r.account_label]
        if kept:
            ref = now if now is not None else max((r.epoch for r in rows), default=0.0)
            live_five = {a.five_resets for a in kept if a.five_resets is not None and a.five_resets > ref}
            anchors = kept[-200:]
            kept += [r for r in loose if _joins(r, anchors, live_five, ref)]
        else:
            kept = loose
        return sorted(kept, key=lambda r: r.epoch)
    hosts = {r.host for r in rows}
    if len(hosts) < 2:
        return rows
    for attr in ("seven_resets", "five_resets"):  # the weekly time is fixed for a week: it decides first
        latest: dict[str, Row] = {}
        for r in rows:  # time order: the last row of each host that carries the time
            if getattr(r, attr) is not None:
                latest[r.host] = r
        if len(latest) != 2:
            continue
        a, b = latest.values()
        if now is not None:
            live = getattr(a, attr) > now and getattr(b, attr) > now
            if attr == "five_resets":
                live = live and now - a.epoch <= 600 and now - b.epoch <= 600
            if not live:
                continue
        if abs(getattr(a, attr) - getattr(b, attr)) > SAME_ACCOUNT_S:
            return [r for r in rows if r.host == caller_host]
        return rows
    return rows


# ---------------------------------------------------------------------------
# the reading
# ---------------------------------------------------------------------------


@dataclass
class _P:
    """One row seen from one window kind."""

    row: Row
    pct: float
    resets: int
    t: float
    fresh: bool = False


def _points(rows: list[Row], kind: str) -> list[_P]:
    """The rows that carry ``kind``, in time order, each flagged fresh or not.

    A row is fresh when it shows a real call by the session that wrote it: that session's running cost rose since its
    previous row; or the row has no cost field (or its previous row had none) and the figure changed since that
    session's previous row; or it is the session's first row -- unless it carries a cost of zero (a new session's
    first render can show stale numbers before any call: 44 % where the truth was 69 %)."""
    pts: list[_P] = []
    prev: dict[tuple, tuple[Row, float | None]] = {}
    for r in rows:  # time order
        pct = r.five_pct if kind == "five" else r.seven_pct
        resets = r.five_resets if kind == "five" else r.seven_resets
        key = (r.host, r.session_id)
        before = prev.get(key)
        prev[key] = (r, pct)
        if pct is None or resets is None:
            continue
        if before is None:
            fresh = r.session_cost_usd is None or r.session_cost_usd > 0
        elif r.session_cost_usd is not None and before[0].session_cost_usd is not None:
            fresh = r.session_cost_usd > before[0].session_cost_usd + 1e-9
        else:
            fresh = before[1] != pct
        pts.append(_P(r, pct, resets, r.epoch, fresh))
    return pts


def _sightings(rows: list[_P]) -> list[_P]:
    """The fresh rows of ``rows`` (time order) that can be a real sighting of the window: a real call cannot show a
    figure below an earlier TRUE figure of the same window, so a fresh-flagged row more than one point (rounding)
    under the highest figure of ``rows`` up to its own time is a lagging or stale row that merely changed (a new
    session's first render, a session whose figures trail the account's). It must not restart the clock."""
    out: list[_P] = []
    high = float("-inf")
    i = 0
    while i < len(rows):
        j = i
        while j < len(rows) and rows[j].t == rows[i].t:
            j += 1
        high = max(high, max(p.pct for p in rows[i:j]))
        out += [p for p in rows[i:j] if p.fresh and p.pct >= high - 1.0]
        i = j
    return out


def _window(pts: list[_P], now: float, b: Bounds, window_now: float) -> WindowReading:
    live = [p for p in pts if p.resets > window_now]
    if live:
        return _live_window(live, now, b)
    past = [p for p in pts if p.resets <= window_now]
    if not past:
        return WindowReading("unknown")
    r_prev = max(p.resets for p in past)
    # The last moment the old window was seen by a real call, never later than its own end: a stale re-render of the
    # old numbers after the reset says nothing about the new window.
    seen = [min(p.t, float(r_prev)) for p in _sightings([p for p in past if p.resets == r_prev])]
    if not seen:
        return WindowReading("unknown")
    t_old = max(seen)
    age = max(now - t_old, 0.0)
    if age > b.max_age_s:
        return WindowReading("unknown", t_fresh=t_old, age_min=round(age / 60, 1))
    upper = min(100.0, b.after_reset(age / 60))
    return WindowReading("bound_after_reset", None, round(upper, 1), None, t_old, round(age / 60, 1))


#: D3 (N5): an evidence row must be captured at least this long after the earlier row it falls below, on the common
#: clock (D1), for that fall to mark a reset -- the same gap the account guard uses to call two windows "the same".
_RESET_EVIDENCE_GAP_S = 120.0


def _detect_reset(rows: list[_P], b: Bounds) -> tuple[float | None, set[tuple]]:
    """(the reset time ``B``, the sessions that have themselves shown it) for ``rows`` (one window, time order).

    Two rules, both feeding one ``B`` (the latest qualifying earlier row's time) and one ``shown`` set:

    * **Own-session (unchanged, the original rule):** a session's figure below its own immediately PRECEDING row by
      more than ``reset_drop`` marks a reset at that preceding row's time -- whatever the gap between them, and
      whether or not either row is ``fresh``. D3 leaves this exactly as it was.
    * **D3's widening:** any EVIDENCE row (``fresh``, and not its session's first row -- a new session's first render
      can trail the account's figures and must never be read as reset evidence) that is lower by more than
      ``reset_drop`` than an EARLIER row of this window, of any session, captured at least
      :data:`_RESET_EVIDENCE_GAP_S` before it. A reset seen only by a session that started afterwards is now caught,
      not only one a session sees in its own fall.

      **Guard against a lagging session (not in the design text; found while testing D3):** a session whose own
      figures are RISING (each row above its own previous one) is catching up to the account's true figure, not
      reporting a reset -- the exact shape ``_sightings`` already excludes from the value, e.g. a lagging session's
      0, 5, 8, 10 under a live window of 23. Comparing such a row against a DIFFERENT session's higher earlier row
      would misread it as reset evidence and then drop that other session's real figure as "implausible", reading
      LOW -- the one outcome this whole lane exists to prevent. So an evidence row only counts for this widened rule
      when it is flat or falling against its OWN session's immediately preceding row; missing a reset (kept as a
      high reading, until an own-session fall or a flat newer-session row is seen) is the safe direction, a false
      reset is not. The own-session rule below is untouched by this guard: a session's own fall is definitive on its
      own terms.

      **S1 (the review, part D fix round): the rise check alone still admits a lagging session's row that is FLAT
      against its own previous one, or wobbles down by rounding (a one-point fall occurs 8 times in 2,362 real
      rows) -- 0, 5, 8, 10, 9 reads a reset at the busy session's 23, though nothing reset. Added: an evidence row
      is also skipped when its own session's figures in this window, from its first row up to and including this
      one, SPAN more than ``reset_drop`` (max minus min). A session catching up has already climbed past that span
      by the time it goes flat or wobbles; a session that starts after a real reset has not -- its whole span, from
      a low first figure to a slightly higher later one, stays inside `reset_drop`. Checked against the real
      combined history (both hosts, every window prefix): with the span guard, the result is identical to the
      own-session rule alone -- the replay's dropped-row count moved from 665 to 664, a cosmetic change with no
      verdict change (part D fix check).

    Sessions whose row triggered either rule are "shown": they keep their later rise without the post-reset
    plausibility check (``_live_window`` below)."""
    reset_at: float | None = None
    shown: set[tuple] = set()
    last: dict[tuple, _P] = {}
    own_prev: dict[int, _P] = {}
    for p in rows:  # own-session: unchanged
        key = (p.row.host, p.row.session_id)
        q = last.get(key)
        if q is not None:
            own_prev[id(p)] = q
            if p.pct < q.pct - b.reset_drop:
                reset_at = q.t if reset_at is None else max(reset_at, q.t)
                shown.add(key)
        last[key] = p
    seen_before: set[tuple] = set()
    span: dict[tuple, tuple[float, float]] = {}  # session -> (min, max) pct seen so far in this window (S1)
    for i, p in enumerate(rows):  # D3: any evidence row against any earlier row of the window, >= 120 s before it
        key = (p.row.host, p.row.session_id)
        is_first = key not in seen_before
        seen_before.add(key)
        lo, hi = span.get(key, (p.pct, p.pct))
        lo, hi = min(lo, p.pct), max(hi, p.pct)
        span[key] = (lo, hi)
        prev = own_prev.get(id(p))
        if (not p.fresh or is_first or (prev is not None and p.pct > prev.pct)  # a rise: a lagging session, not evidence
                or hi - lo > b.reset_drop):  # S1: it has already climbed further than a reset_drop this window
            continue
        best_q_t = None
        for q in rows[:i]:
            if q.t > p.t - _RESET_EVIDENCE_GAP_S:
                continue
            if q.pct > p.pct + b.reset_drop and (best_q_t is None or q.t > best_q_t):
                best_q_t = q.t
        if best_q_t is not None:
            shown.add(key)
            reset_at = best_q_t if reset_at is None else max(reset_at, best_q_t)
    return reset_at, shown


def _live_window(live: list[_P], now: float, b: Bounds) -> WindowReading:
    big_r = max(p.resets for p in live)
    rows = [p for p in live if p.resets == big_r]
    reset_at, shown = _detect_reset(rows, b)
    dropped_early = dropped_stale = 0
    if reset_at is not None:
        kept = []
        for p in rows:
            if p.t <= reset_at:
                dropped_early += 1
            elif not p.fresh:
                dropped_stale += 1
            elif (p.row.host, p.row.session_id) in shown or p.pct <= b.after_reset((p.t - reset_at) / 60):
                # A session that has itself shown the reset (its own fall, or a kept post-reset row) keeps its later
                # fresh rows: its rise is real. The plausibility line is for a session that has NOT: its first row
                # after the reset may be a pre-reset re-render whose figure is not a post-reset one.
                shown.add((p.row.host, p.row.session_id))
                kept.append(p)
            else:
                dropped_stale += 1
        rows = kept
    if not rows:
        return WindowReading("unknown", resets=big_r, rows_dropped_early_reset=dropped_early,
                             rows_dropped_implausible=dropped_stale)
    value = max(p.pct for p in rows)
    fresh = _sightings(rows)
    common = dict(resets=big_r, rows_dropped_early_reset=dropped_early, rows_dropped_implausible=dropped_stale)
    if not fresh:
        return WindowReading("unknown", value, None, **common)
    f = max(fresh, key=lambda p: p.t)
    age = max(now - f.t, 0.0)
    if age <= b.fresh_s:
        return WindowReading("reading", value, value, t_fresh=f.t, age_min=round(age / 60, 1),
                             src_host=f.row.host, **common)
    if age <= b.max_age_s:
        # Grow from the window maximum up to the sighting, never below the sighting's own figure: a session's stale
        # first render can be the latest "fresh" row while another session's higher figure is already in the window.
        base = max(p.pct for p in rows if p.t <= f.t)
        upper = min(100.0, max(value, base + b.growth(age / 60)))
        return WindowReading("bound", value, round(upper, 1), t_fresh=f.t, age_min=round(age / 60, 1),
                             src_host=f.row.host, **common)
    return WindowReading("unknown", value, None, t_fresh=f.t, age_min=round(age / 60, 1), **common)


def reading(rows: Iterable[Row], now: float, *, bounds: BoundsSet = DEFAULT_BOUNDS, acct: str = "",
           window_now: float | None = None) -> Reading:
    """The five-hour and seven-day answers from ``rows`` (already filtered to one account) at time ``now``.

    **S2 (the review, part D fix round):** ``resets_at`` values are the API's own, true-time stamps. D1 moves every
    OTHER host's row epochs onto the caller's clock, but the caller's own clock could itself run ahead of true
    time -- and comparing a true-time ``resets_at`` against an inflated ``now`` then judges a still-live window as
    already ended, reading low from a ``bound_after_reset`` that starts near zero. ``window_now`` is the earliest
    clock the caller has read (its own, and every host's, e.g. ``min(now, sandbox_now)``): a SAFE (never-late) lower
    bound on true time, used only to decide whether a window is still live or has ended. ``now`` itself is
    unchanged for every age and growth calculation -- a row's own corrected epoch and ``now`` are both already on
    the caller's clock (D1), so their DIFFERENCE (an age) stays correct even when that clock's absolute reading is
    off by a constant amount. Defaults to ``now`` (today's behaviour) when not given."""
    usable = [r for r in _dedupe(rows) if r.epoch <= now + CLOCK_SKEW_S]
    wn = now if window_now is None else min(now, window_now)
    five = _window(_points(usable, "five"), now, bounds.five, wn)
    seven = _window(_points(usable, "seven"), now, bounds.seven, wn)
    src = five.src_host or seven.src_host
    return Reading(five=five, seven=seven, src_host=src, acct=acct)
