"""``trialerror quota notify`` (design ``L4_quota-policy.md`` Section 2.6,
amended by ``L9_exchange-rate-on-observed-spend.md`` Section 3 Part E,
2026-09-29): turn ``limit_hit`` flag files (written by
:mod:`trialerror.hooks.stop_failure`, never here -- "no network call inside
a hook") into ``quota_notice`` rows, and run the credit-risk alert before
usage credits are touched.

Meant to run from cron every 5 minutes, INSIDE the sandbox container (L9
Part E2), spending no model tokens and making no network call itself either
-- ``push`` is injected so this module never imports a transport; the CLI
wires it to ``[packet] notify_cmd`` when one is configured, else the outbox
(``[packet] outbox = true``)."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, Callable

from trialerror.packet.store import PacketError, parse_ts

if TYPE_CHECKING:
    from pathlib import Path

    from trialerror.quota.reading import Reading, WindowReading

__all__ = [
    "LIMIT_HIT_FLAG_DIRNAME",
    "FLAG_MAX_AGE_S",
    "process_limit_hit_flags",
    "CREDIT_RISK_WINDOW_WORDS",
    "credit_risk_packet_payload",
    "check_credit_risk",
]

LIMIT_HIT_FLAG_DIRNAME = "quota_limit_hit"

#: L9 §3 Part E, E1: a flag counts toward the trigger only under this age (by
#: its own ``ts``). Older flags, and flags with no readable ``ts``, are
#: recorded and cleared as backlog -- never an alert.
FLAG_MAX_AGE_S = 6 * 3600.0

#: E4: at most 3 outbox attempts a month for the combined alert, the second
#: at least an hour after the first, the third at least a day after the
#: second.
_ALERT_MAX_ATTEMPTS = 3
_ALERT_RETRY_AFTER_S = {1: 3600.0, 2: 86400.0}

#: L9 §3 Part E: the two window kinds a real (non-bound) reading can name,
#: in the plain words the push body and packet item use.
CREDIT_RISK_WINDOW_WORDS = {"five_hour": "five-hour", "seven_day": "weekly"}

_ATTEMPT_PERIOD_PREFIX = "credit_risk_alert"


def _parse_epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return parse_ts(ts).timestamp()
    except PacketError:
        return None


def process_limit_hit_flags(
    conn: sqlite3.Connection,
    flags_dir: "Path",
    account_label: str,
    now_ts: str,
) -> dict[str, Any]:
    """Record every ``*.flag`` file once in ``quota_notice`` (kind
    ``limit_hit``, keyed on the flag's own filename in ``period``) and clear
    it -- E5: this never pushes on its own any more. L4's per-flag push is
    folded into L9's combined credit-risk alert (:func:`check_credit_risk`),
    which this function's return value feeds: ``fresh_flags`` lists every
    flag still under :data:`FLAG_MAX_AGE_S` old by its own ``ts``, for that
    function's own E1 trigger decision -- judged in the SAME run, before the
    flags are cleared here (E5).

    A flag older than :data:`FLAG_MAX_AGE_S`, or with no readable ``ts``, is
    recorded and cleared without ever counting toward a trigger (E1's
    backlog rule -- this also drains whatever built up before this alert
    existed). ``backlog_cleared`` counts those, for the run to report.

    A flag file that cannot be parsed is removed unconditionally -- there is
    nothing a retry could fix about it."""
    processed = fresh = backlog = 0
    fresh_flags: list[dict[str, Any]] = []
    if not flags_dir.is_dir():
        return {"processed": 0, "fresh": 0, "backlog_cleared": 0, "fresh_flags": []}
    now_epoch = _parse_epoch(now_ts)
    for path in sorted(flags_dir.glob("*.flag")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if not isinstance(data, dict):
            path.unlink(missing_ok=True)
            continue
        flag_key = path.stem  # stable across runs until the flag is deleted
        processed += 1
        flag_epoch = _parse_epoch(data.get("ts")) if isinstance(data.get("ts"), str) else None
        age_s = (now_epoch - flag_epoch) if (now_epoch is not None and flag_epoch is not None) else None
        is_fresh = age_s is not None and 0.0 <= age_s < FLAG_MAX_AGE_S
        if is_fresh:
            fresh += 1
            fresh_flags.append({"key": flag_key, "data": data})
        else:
            backlog += 1
        detail = json.dumps(data, ensure_ascii=False)
        existing = conn.execute(
            "SELECT id FROM quota_notice WHERE account_label = ? AND kind = 'limit_hit' AND period = ? LIMIT 1",
            (account_label, flag_key),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO quota_notice (account_label, kind, period, created_ts, sent_ts, detail) "
                "VALUES (?, 'limit_hit', ?, ?, ?, ?)",
                (account_label, flag_key, now_ts, now_ts, detail),
            )
        path.unlink(missing_ok=True)
    conn.commit()
    return {"processed": processed, "fresh": fresh, "backlog_cleared": backlog, "fresh_flags": fresh_flags}


def _resets_words(resets: float | int | None) -> str:
    """N7 (fix check): " at HH:MMZ" from a window's own ``resets`` epoch, as
    E2 words it ("...until the window resets at <time>"). Empty for the flag
    trigger (no window at all) or a window with no known reset time --
    E2's exact wording is then kept as before ("...the window resets")."""
    import datetime as _dt

    if resets is None:
        return ""
    try:
        when = _dt.datetime.fromtimestamp(float(resets), _dt.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return f" at {when.strftime('%H:%M')}Z"


def credit_risk_packet_payload(triggers: list[str], when_date: str) -> dict[str, Any]:
    """L9 §3 Part E, E3 item 3 -- the ``raw`` dict for
    :func:`trialerror.packet.store.add_item`, verbatim from the design's
    what/why/options/if_undecided. ``triggers`` is every trigger newly
    firing in this run (one combined item per push, fix check S6/E9:
    "several triggers in one run give one push and one item"); a flag alone
    gets the plain "usage limit" wording, a window alone names its window,
    and a window with a flag together get the window's wording plus "; a
    session stopped on it"."""
    windows = [t for t in triggers if t in CREDIT_RISK_WINDOW_WORDS]
    flagged = "flag" in triggers
    if windows:
        window_words = " and ".join(CREDIT_RISK_WINDOW_WORDS[t] for t in windows)
        what = f"The plan's {window_words} limit was reached on {when_date}"
        if flagged:
            what += "; a session stopped on it"
    else:
        what = f"The plan's usage limit was reached on {when_date}"
    return {
        "what": what,
        "why": "Usage credits are an emergency reserve; you asked to discuss any planned use first",
        "options": [
            {"key": "fine", "label": "it was an emergency, fine", "consequence": "no change"},
            {
                "key": "find_out",
                "label": "find out what ran",
                "consequence": "the custodian lists the work active at that time from the units table",
            },
        ],
        "recommended": "fine",
        "if_undecided": "nothing changes; the next month's first event alerts again",
        "needed_by": "next-session",
        "priority": "blocking",
    }


def _effective_upper(win: "WindowReading | None") -> float | None:
    """The highest figure a live window could really be at: the real
    ``value`` when it is known exactly (``basis == "reading"``, no bound
    applies), else the bound's own ``upper`` estimate. ``None`` for a window
    that is not live at all (``bound_after_reset``, ``unknown``, or no
    window)."""
    if win is None or win.basis not in ("reading", "bound"):
        return None
    return win.value if win.basis == "reading" else win.upper


def _window_triggers(reading: "Reading | None") -> tuple[list[str], dict[str, Any]]:
    triggers: list[str] = []
    windows_by_trigger: dict[str, Any] = {}
    if reading is not None:
        for key in ("five_hour", "seven_day"):
            win = reading.five if key == "five_hour" else reading.seven
            windows_by_trigger[key] = win
            if win is not None and win.basis in ("reading", "bound") and win.value is not None and win.value >= 100.0:
                triggers.append(key)
    return triggers, windows_by_trigger


def _flag_rate_limited(windows_by_trigger: dict[str, Any]) -> bool:
    """E1's short rate limit: a fresh flag raises no alert when BOTH windows
    are live and even their upper estimates are under 100% -- the plan's
    limit cannot have been reached then."""
    five_upper = _effective_upper(windows_by_trigger.get("five_hour"))
    seven_upper = _effective_upper(windows_by_trigger.get("seven_day"))
    if five_upper is None or seven_upper is None:
        return False
    return five_upper < 100.0 and seven_upper < 100.0


def _combined_words(triggers: list[str], windows_by_trigger: dict[str, Any]) -> tuple[str, str]:
    title = "Plan limit reached: work may be using the emergency reserve"
    windows = [t for t in triggers if t in CREDIT_RISK_WINDOW_WORDS]
    flagged = "flag" in triggers
    if windows:
        window_words = " and ".join(CREDIT_RISK_WINDOW_WORDS[t] for t in windows)
        resets_words = ""
        for t in windows:
            win = windows_by_trigger.get(t)
            rw = _resets_words(getattr(win, "resets", None)) if win is not None else ""
            if rw:
                resets_words = rw
                break
        lead = f"The {window_words} limit is at 100%"
        if flagged:
            lead += "; a session stopped on it"
        body = (
            f"{lead}. Further work now may spend usage credits, which are kept for emergencies only. "
            f"The meters say STOP; nothing new should start until the window resets{resets_words}."
        )
    else:
        body = (
            "A session stopped on a usage limit. Further work now may spend usage credits, which are kept for "
            "emergencies only. The meters say STOP; nothing new should start until the limit resets."
        )
    return title, body


def _trigger_entry(name: str, month: str) -> dict[str, str]:
    return {"name": name, "month": month}


def _entry_name(entry: Any) -> str:
    return entry["name"] if isinstance(entry, dict) else entry


def _entry_month(entry: Any, default_month: str) -> str:
    if isinstance(entry, dict):
        return entry.get("month") or default_month
    return default_month  # a legacy plain-string entry, from before N-b


def _entry_names(entries: list[Any]) -> list[str]:
    return [_entry_name(e) for e in entries]


def _dedup_entries(entries: list[Any], default_month: str) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for entry in entries:
        name, month = _entry_name(entry), _entry_month(entry, default_month)
        key = (name, month)
        if key not in seen:
            seen.add(key)
            out.append(_trigger_entry(name, month))
    return out


def _prev_month_key(period_month: str) -> str | None:
    try:
        year_s, month_s = period_month.split("-", 1)
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        return None
    return f"{year}-{month - 1:02d}" if month > 1 else f"{year - 1}-12"


def _load_attempt_state_row(conn: sqlite3.Connection, account_label: str, period_month: str) -> tuple[int | None, dict[str, Any]]:
    period = f"{_ATTEMPT_PERIOD_PREFIX}:{period_month}"
    row = conn.execute(
        "SELECT id, detail FROM quota_notice WHERE account_label = ? AND kind = 'limit_hit' AND period = ? LIMIT 1",
        (account_label, period),
    ).fetchone()
    if row is None:
        return None, {"attempts": 0, "last_attempt_ts": None, "outbox_id": None, "triggers": []}
    try:
        detail = json.loads(row["detail"] or "{}")
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    detail.setdefault("attempts", 0)
    detail.setdefault("last_attempt_ts", None)
    detail.setdefault("outbox_id", None)
    detail.setdefault("triggers", [])
    return row["id"], detail


def _load_attempt_state(conn: sqlite3.Connection, account_label: str, period_month: str) -> tuple[int | None, dict[str, Any]]:
    row_id, state = _load_attempt_state_row(conn, account_label, period_month)
    if not state.get("outbox_id") and not state.get("triggers"):
        # N-b (fix check, 2026-09-29): a queued alert with no receipt yet at
        # the month boundary must not be stranded -- the new month's own
        # attempt row starts empty, so without this the prior month's
        # pending outbox id (and the triggers waiting on it) would simply
        # never be looked at again. Each carried trigger already remembers
        # its own origin month (_trigger_entry), so reconciling it later
        # still updates the RIGHT quota_notice row, not this month's.
        prev_month = _prev_month_key(period_month)
        if prev_month:
            prev_row_id, prev_state = _load_attempt_state_row(conn, account_label, prev_month)
            if prev_row_id is not None and (prev_state.get("outbox_id") or prev_state.get("triggers")):
                prev_state["triggers"] = [
                    _trigger_entry(_entry_name(e), _entry_month(e, prev_month)) for e in prev_state["triggers"]
                ]
                state = prev_state
                # the prior month's row is spent -- its content now lives
                # under the current month's row once this run saves.
                conn.execute("UPDATE quota_notice SET detail = ? WHERE id = ?", (json.dumps({}), prev_row_id))
    return row_id, state


def _save_attempt_state(conn: sqlite3.Connection, row_id: int | None, account_label: str, period_month: str, now_ts: str, state: dict[str, Any]) -> None:
    # this row is bookkeeping, not a notice a caller should ever see in an
    # "open notices" list -- it always carries a sent_ts, whether or not an
    # alert is currently pending delivery.
    period = f"{_ATTEMPT_PERIOD_PREFIX}:{period_month}"
    detail = json.dumps(state, ensure_ascii=False)
    if row_id is None:
        conn.execute(
            "INSERT INTO quota_notice (account_label, kind, period, created_ts, sent_ts, detail) VALUES (?, 'limit_hit', ?, ?, ?, ?)",
            (account_label, period, now_ts, now_ts, detail),
        )
    else:
        conn.execute("UPDATE quota_notice SET sent_ts = ?, detail = ? WHERE id = ?", (now_ts, detail, row_id))


def _merge_trigger_detail(conn: sqlite3.Connection, account_label: str, period: str, updates: dict[str, Any]) -> None:
    """N-c (review fix check, 2026-09-29): E3 item 1 says a trigger's own
    ``quota_notice`` row records the outbox id and (E4) a failed receipt's
    code -- both were kept only in the attempt-state row before this, never
    in the trigger's own row ``check_credit_risk``'s own docstring already
    described "every trigger gets its own quota_notice row" for."""
    row = conn.execute(
        "SELECT id, detail FROM quota_notice WHERE account_label = ? AND kind = 'limit_hit' AND period = ? LIMIT 1",
        (account_label, period),
    ).fetchone()
    if row is None:
        return
    try:
        detail = json.loads(row["detail"] or "{}")
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    detail.update(updates)
    conn.execute("UPDATE quota_notice SET detail = ? WHERE id = ?", (json.dumps(detail, ensure_ascii=False), row["id"]))


def _may_attempt(state: dict[str, Any], now_epoch: float | None) -> bool:
    attempts = int(state.get("attempts") or 0)
    if attempts >= _ALERT_MAX_ATTEMPTS:
        return False
    if attempts == 0:
        return True
    last_epoch = _parse_epoch(state.get("last_attempt_ts"))
    if last_epoch is None or now_epoch is None:
        return True
    required = _ALERT_RETRY_AFTER_S.get(attempts, 0.0)
    return (now_epoch - last_epoch) >= required


def check_credit_risk(
    conn: sqlite3.Connection,
    account_label: str,
    reading: "Reading | None",
    fresh_flag_seen: bool,
    now_ts: str,
    period_month: str,
    push: Callable[[str, str], str | None],
    *,
    reconcile_result: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """L9 §3 Part E (amended 2026-09-29): the one-time-per-trigger alert
    before usage credits are touched, run in the sandbox every 5 minutes.

    E1: a window trigger fires on a real sighting (``value``, basis
    ``reading`` or ``bound``) at or over 100% -- the bound's own ``upper``
    estimate never fires it, nor does a reset window. The flag trigger fires
    only on a FRESH flag (``fresh_flag_seen``, from
    :func:`process_limit_hit_flags`'s own age filter), unless both windows
    are live with even their upper estimates under 100% (the short rate
    limit).

    E3/E4/E5: every trigger gets its own ``quota_notice`` row, at most once
    per calendar month (a later sighting of an already-alerted trigger adds
    a repeat row once per window, keyed on its own ``resets``, never once
    per tick -- N5). All triggers newly firing in ONE run share ONE push and
    ONE packet item (E9: "several triggers in one run give one push and one
    item"), written once regardless of whether the push itself succeeds.

    ``push(title, body)`` returns an outbox id (queued, not yet delivered)
    or ``None`` (delivered immediately, e.g. ``notify_cmd``); it raises on
    an immediate failure. ``reconcile_result`` -- ``{"delivered": [...],
    "failed": [...]}`` outbox ids, from
    :func:`trialerror.packet.outbox.reconcile_receipts`, read by the caller
    at the start of the run -- lets a run notice a PRIOR attempt's outcome
    before deciding whether a new one is due (E4's 1-hour/1-day retry
    schedule, at most 3 attempts a month in all)."""
    window_triggers, windows_by_trigger = _window_triggers(reading)
    flag_candidate = bool(fresh_flag_seen) and not _flag_rate_limited(windows_by_trigger)
    triggers = list(window_triggers) + (["flag"] if flag_candidate else [])

    # E4: reconcile a PRIOR attempt's outcome first, so a trigger whose
    # receipt just came back delivered is no longer "pending" by the time
    # the per-trigger loop below reads its row.
    row_id, state = _load_attempt_state(conn, account_label, period_month)
    now_epoch = _parse_epoch(now_ts)
    outbox_id = state.get("outbox_id")
    if outbox_id and reconcile_result:
        receipt = (reconcile_result.get("receipts") or {}).get(outbox_id) or {}
        if outbox_id in reconcile_result.get("delivered", []):
            # N-c: sent_ts is the receipt's own ts, not this run's clock.
            delivered_ts = receipt.get("ts") or now_ts
            for entry in state.get("triggers", []):
                period = f"credit_risk:{_entry_name(entry)}:{_entry_month(entry, period_month)}"
                conn.execute(
                    "UPDATE quota_notice SET sent_ts = ? WHERE account_label = ? AND kind = 'limit_hit' AND period = ?",
                    (delivered_ts, account_label, period),
                )
            state["outbox_id"] = None
            state["triggers"] = []
            # S-d (review fix check, 2026-09-29): the cap of 3 applies to
            # the retries of ONE alert (E4: "one alert's attempts always
            # fit" in the outbox's 2-a-day allowance) -- a delivered alert
            # must not leave a NEW, later event waiting behind the 1h/1day
            # delay a PRIOR alert's retries used.
            state["attempts"] = 0
            state["last_attempt_ts"] = None
        elif outbox_id in reconcile_result.get("failed", []):
            for entry in state.get("triggers", []):
                period = f"credit_risk:{_entry_name(entry)}:{_entry_month(entry, period_month)}"
                _merge_trigger_detail(conn, account_label, period, {"failure_code": receipt.get("exit_code")})
            state["outbox_id"] = None
            # triggers stay pending -- E4: "a failed alert is queued again
            # on a later run, whether or not its condition still holds"

    recorded: list[str] = []
    pending: list[dict[str, str]] = []
    new_this_run: list[str] = []
    for trigger in triggers:
        period = f"credit_risk:{trigger}:{period_month}"
        recorded.append(trigger)
        row = conn.execute(
            "SELECT id, sent_ts FROM quota_notice WHERE account_label = ? AND kind = 'limit_hit' AND period = ? LIMIT 1",
            (account_label, period),
        ).fetchone()
        if row is not None and row["sent_ts"] is not None:
            # N5: a repeat sighting is recorded once per WINDOW -- keyed on
            # its own resets time -- not once per 5-minute tick. The flag
            # trigger names no window, so a lingering fresh flag adds no
            # further rows here at all once its own row has fired.
            win = windows_by_trigger.get(trigger)
            resets = getattr(win, "resets", None) if win is not None else None
            if resets is None:
                continue
            repeat_period = f"{period}:reset={resets}"
            repeat_seen = (
                conn.execute(
                    "SELECT 1 FROM quota_notice WHERE account_label = ? AND kind = 'limit_hit' AND period = ? LIMIT 1",
                    (account_label, repeat_period),
                ).fetchone()
                is not None
            )
            if repeat_seen:
                continue
            conn.execute(
                "INSERT INTO quota_notice (account_label, kind, period, created_ts, sent_ts, detail) "
                "VALUES (?, 'limit_hit', ?, ?, NULL, ?)",
                (account_label, repeat_period, now_ts, json.dumps({"credit_risk": True, "trigger": trigger, "repeat": True}, ensure_ascii=False)),
            )
            continue
        pending.append(_trigger_entry(trigger, period_month))
        if row is None:
            new_this_run.append(trigger)
            detail_obj: dict[str, Any] = {"credit_risk": True, "trigger": trigger}
            conn.execute(
                "INSERT INTO quota_notice (account_label, kind, period, created_ts, sent_ts, detail) "
                "VALUES (?, 'limit_hit', ?, ?, NULL, ?)",
                (account_label, period, now_ts, json.dumps(detail_obj, ensure_ascii=False)),
            )

    when_date = now_ts[:10] if len(now_ts) >= 10 else now_ts
    packets: list[dict[str, Any]] = []
    if new_this_run:
        # E3 item 3: one packet item per push, written once at first firing
        # -- combining every trigger that is newly firing in THIS run,
        # whether or not the push below actually gets through.
        packets.append(credit_risk_packet_payload(sorted(new_this_run), when_date))

    outstanding = _dedup_entries((state.get("triggers") or []) + pending, period_month)
    state["triggers"] = outstanding

    fired: list[str] = []
    failed: list[str] = []
    if outstanding and not state.get("outbox_id") and _may_attempt(state, now_epoch):
        title, body = _combined_words(_entry_names(outstanding), windows_by_trigger)
        try:
            sent_outbox_id = push(title, body)
        except Exception:  # noqa: BLE001 - a failed push must never crash `quota notify`
            failed.extend(_entry_names(outstanding))
            state["attempts"] = int(state.get("attempts") or 0) + 1
            state["last_attempt_ts"] = now_ts
        else:
            state["attempts"] = int(state.get("attempts") or 0) + 1
            state["last_attempt_ts"] = now_ts
            if sent_outbox_id:
                state["outbox_id"] = sent_outbox_id
                for entry in outstanding:
                    period = f"credit_risk:{_entry_name(entry)}:{_entry_month(entry, period_month)}"
                    _merge_trigger_detail(conn, account_label, period, {"outbox_id": sent_outbox_id})
            else:
                for entry in outstanding:
                    conn.execute(
                        "UPDATE quota_notice SET sent_ts = ? WHERE account_label = ? AND kind = 'limit_hit' AND period = ?",
                        (now_ts, account_label, f"credit_risk:{_entry_name(entry)}:{_entry_month(entry, period_month)}"),
                    )
                fired.extend(_entry_names(outstanding))
                state["triggers"] = []
                # S-d: delivered at once (notify_cmd) -- same reset as the
                # outbox's own delivered branch above, same reason.
                state["attempts"] = 0
                state["last_attempt_ts"] = None

    # only persist the attempt-state row once it holds something real -- an
    # always-NULL-sent_ts row for a run that fired nothing would otherwise
    # pollute `quota status`'s open-notices list forever.
    if row_id is not None or state.get("attempts") or state.get("triggers"):
        _save_attempt_state(conn, row_id, account_label, period_month, now_ts, state)
    conn.commit()
    return {
        "recorded": recorded,
        "new_this_run": new_this_run,
        "fired": fired,
        "failed": failed,
        "pending": _entry_names(state.get("triggers") or []),
        "packet_payloads": packets,
    }
