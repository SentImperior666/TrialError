"""Claude Code statusLine hook: capture plan rate-limit quota into the store.

Claude Code >= 2.1.80 passes a ``rate_limits`` object in the statusLine
JSON on stdin (five_hour / seven_day windows with ``used_percentage`` and
``resets_at``; claude.ai subscriptions only — API-key sessions omit it).
This script is the capture side of the budget quota feed:

  * writes ``latest.json`` (atomic) + a throttled ``rate_limits.jsonl``
    history under the quota dir (``TRIALERROR_QUOTA_DIR`` or ``~/.trialerror/quota``)
  * writes ``sessions.json``: per-session cost figures (what each session has
    spent so far, from the payload's ``cost`` object), so a later reader can
    see WHICH session moved the shared window
  * stamps ``numbers_changed_ts`` (when a percentage or reset time last
    actually moved) and ``payload_cost_api_ms`` into ``latest.json``
  * prints a one-line status string so it doubles as an actual status line

Wire-up (per Claude Code account, in ``~/.claude/settings.json``)::

    "statusLine": {"type": "command",
                   "command": "python <abs path to this file>"}

Deliberately stdlib-only and runnable as a bare file: statusLine fires on
every UI tick, so it must start fast, import nothing from trialerror, and NEVER
crash the status line — every failure degrades to a plain text line and
exit 0. The read side lives in :mod:`trialerror.budget.quota`.
"""

from __future__ import annotations

import json
import os
import sys
import time

MAX_STDIN_BYTES = 1_000_000
HISTORY_THROTTLE_S = 300
HISTORY_MIN_DELTA_PCT = 1.0
HISTORY_MIN_DELTA_COST_USD = 0.50
REPLACE_ATTEMPTS = 3
REPLACE_SLEEP_S = 0.03
STALE_TMP_S = 3600
SESSIONS_KEEP_DAYS = 30
SESSIONS_REWRITE_S = 60
SESSIONS_MAX = 500
ACCOUNTS_WINDOWS_KEEP = 20
_COST_FIELDS = (
    "total_cost_usd",
    "total_duration_ms",
    "total_api_duration_ms",
    "total_lines_added",
    "total_lines_removed",
)

_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}


def quota_dir() -> str:
    d = os.environ.get("TRIALERROR_QUOTA_DIR")
    if not d:
        d = os.path.join(os.path.expanduser("~"), ".trialerror", "quota")
    return d


def _fmt_reset(resets_at: object) -> str:
    if not isinstance(resets_at, str) or "T" not in resets_at:
        return ""
    try:
        clock = resets_at.split("T", 1)[1][:5]
        day = resets_at.split("T", 1)[0][5:]  # MM-DD
        return f" r{day} {clock}Z"
    except Exception:
        return ""


def _status_line(rate_limits: dict, ctx_pct: object, model_name: str) -> str:
    parts: list[str] = []
    known = [k for k in ("five_hour", "seven_day") if k in rate_limits]
    extra = sorted(k for k in rate_limits if k not in _WINDOW_LABELS)
    for key in known + extra:
        win = rate_limits.get(key)
        if not isinstance(win, dict):
            continue
        pct = win.get("used_percentage")
        if not isinstance(pct, (int, float)):
            continue
        label = _WINDOW_LABELS.get(key, key.replace("_", "-"))
        parts.append(f"{label} {pct:.0f}%{_fmt_reset(win.get('resets_at'))}")
    if isinstance(ctx_pct, (int, float)):
        parts.append(f"ctx {ctx_pct:.0f}%")
    if model_name:
        parts.append(model_name)
    return "TRIALERROR | " + " | ".join(parts) if parts else "TRIALERROR | (no quota data)"


def _tail_last_line(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _pcts(rate_limits: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, win in rate_limits.items():
        if isinstance(win, dict) and isinstance(win.get("used_percentage"), (int, float)):
            out[key] = float(win["used_percentage"])
    return out


def _should_append(history_path: str, snap: dict, now_epoch: float, *, force: bool = False) -> bool:
    if force:
        return True
    last = _tail_last_line(history_path)
    if last is None:
        return True
    try:
        prev = json.loads(last)
        if now_epoch - float(prev.get("epoch", 0)) >= HISTORY_THROTTLE_S:
            return True
        prev_pcts = _pcts(prev.get("rate_limits", {}))
        for key, pct in _pcts(snap.get("rate_limits", {})).items():
            if key not in prev_pcts or abs(pct - prev_pcts[key]) >= HISTORY_MIN_DELTA_PCT:
                return True
        return False
    except Exception:
        return True


def _atomic_write(path: str, write) -> None:
    """Write ``path`` through a temp file and ``os.replace``. On Windows a reader that has the target open
    (another session's tick, the meter) makes the replace fail with PermissionError for a moment, so it is retried
    a few times; whatever happens, the temp file never outlives the call."""
    tmp = path + f".tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            write(f)
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_SLEEP_S)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _sweep_stale_tmp(directory: str, now_epoch: float) -> None:
    """Remove ``*.tmp*`` files an earlier crash left in the quota directory (older than an hour, so a write in
    progress in another process is never touched)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if ".tmp" not in name:
            continue
        path = os.path.join(directory, name)
        try:
            if now_epoch - os.path.getmtime(path) > STALE_TMP_S:
                os.unlink(path)
        except OSError:
            pass


def _numbers(rate_limits: object) -> dict:
    """Every window's (used_percentage, resets_at): what "the numbers" are."""
    out: dict = {}
    if isinstance(rate_limits, dict):
        for key, win in rate_limits.items():
            if isinstance(win, dict):
                out[key] = (win.get("used_percentage"), win.get("resets_at"))
    return out


def _numbers_changed_ts(previous: object, rate_limits: dict, captured_ts: str) -> str:
    """When the numbers last changed. ``captured_ts`` is when this command ran, not when an API
    response last carried new numbers, so a status-line refresh on a timer would make old numbers
    look fresh. Unchanged numbers keep the previous stamp; any change (or no previous) is now."""
    if isinstance(previous, dict) and _numbers(previous.get("rate_limits")) == _numbers(rate_limits):
        kept = previous.get("numbers_changed_ts") or previous.get("captured_ts")
        if isinstance(kept, str) and kept:
            return kept
    return captured_ts


def _read_json(path: str) -> object:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _api_ms(payload: dict) -> object:
    cost = payload.get("cost")
    value = cost.get("total_api_duration_ms") if isinstance(cost, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _update_sessions(payload: dict, now_epoch: float, *, history_cost_usd: float | None = None) -> None:
    """Record this session's cost figures in ``sessions.json`` (atomic replace). Only numeric
    fields of the payload's ``cost`` object are kept; sessions unseen for 30 days are dropped and
    the file is capped at 500 sessions. Two sessions ticking at once can lose one another's
    latest update (last writer wins); the next tick restores it.

    ``history_cost_usd``: the ``session_cost_usd`` just written to a ``rate_limits.jsonl`` row for
    THIS session, or ``None`` when this tick did not append one. Carried over unchanged when
    ``None`` (this session's last-recorded HISTORY cost does not move just because a tick happened),
    so :func:`_should_force_history` can compare "since this session's last row" (design L4 Section
    2.1) against the moment a row was actually written, never against every tick's cost (which
    would make the 0.50 rise reset on ticks that changed nothing about this session's own history)."""
    sid = payload.get("session_id")
    cost = payload.get("cost")
    if not isinstance(sid, str) or not sid or not isinstance(cost, dict):
        return
    figures = {
        k: cost[k]
        for k in _COST_FIELDS
        if isinstance(cost.get(k), (int, float)) and not isinstance(cost.get(k), bool)
    }
    if not figures:
        return
    d = quota_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "sessions.json")
    doc = _read_json(path)
    sessions = doc.get("sessions") if isinstance(doc, dict) else None
    if not isinstance(sessions, dict):
        sessions = {}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch))
    prev = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
    model = payload.get("model") or {}
    entry = {
        "first_seen_ts": prev.get("first_seen_ts") or stamp,
        "last_seen_ts": stamp,
        "last_seen_epoch": now_epoch,
        "model": str(model.get("display_name") or "") if isinstance(model, dict) else "",
        "cc_version": payload.get("version"),
        "cost": figures,
        "last_history_cost_usd": history_cost_usd if history_cost_usd is not None else prev.get("last_history_cost_usd"),
    }
    pcts = _pcts(payload.get("rate_limits") or {}) if isinstance(payload.get("rate_limits"), dict) else {}
    if pcts:
        entry["window_pct"] = pcts
    # Nothing but the clock moved and the last write is under a minute old: skip the rewrite (the file
    # is up to ~200 KB at 500 sessions and every render would otherwise read and rewrite all of it).
    # N1: `last_history_cost_usd` must be compared too -- a tick that just appended a history row
    # changed it (even when nothing else here did), and skipping that rewrite lost the update. At
    # upgrade this made a running session with a pre-L4 entry append a row on EVERY tick for up to
    # 60s (15 unchanged ticks -> 15 extra rows; the throttle alone would add 0).
    if prev and all(prev.get(k) == entry.get(k) for k in ("model", "cc_version", "cost", "window_pct", "last_history_cost_usd")):
        try:
            if 0 <= now_epoch - float(prev.get("last_seen_epoch")) < SESSIONS_REWRITE_S:
                return
        except (TypeError, ValueError):
            pass
    sessions[sid] = entry
    horizon = now_epoch - SESSIONS_KEEP_DAYS * 86400
    kept = {
        k: v for k, v in sessions.items() if isinstance(v, dict) and float(v.get("last_seen_epoch") or 0) >= horizon
    }
    if len(kept) > SESSIONS_MAX:
        newest = sorted(kept, key=lambda k: float(kept[k].get("last_seen_epoch") or 0), reverse=True)[:SESSIONS_MAX]
        kept = {k: kept[k] for k in newest}
    _atomic_write(path, lambda f: json.dump({"version": 1, "sessions": kept}, f, ensure_ascii=False))


def _prev_resets(previous: object, rate_limits: dict) -> dict:
    """``prev_five_resets`` / ``prev_seven_resets`` for ``latest.json``: when this payload carries no figure for a
    window, the last ``resets_at`` seen for it (the previous capture's own, or the one it had itself carried over).
    A payload that carries the window keeps nothing: its ``resets_at`` is in ``rate_limits``. Lets a reader tell
    which window a figure-less capture followed (the seven-day figure alone, stamped just after a five-hour reset)."""
    out: dict = {}
    prev_rl = previous.get("rate_limits") if isinstance(previous, dict) else None
    for key, name in (("five_hour", "prev_five_resets"), ("seven_day", "prev_seven_resets")):
        win = rate_limits.get(key)
        if isinstance(win, dict) and isinstance(win.get("used_percentage"), (int, float)):
            continue
        old = prev_rl.get(key) if isinstance(prev_rl, dict) else None
        seen = old.get("resets_at") if isinstance(old, dict) else None
        if not isinstance(seen, (int, float)) or isinstance(seen, bool):
            seen = previous.get(name) if isinstance(previous, dict) else None
        if isinstance(seen, (int, float)) and not isinstance(seen, bool):
            out[name] = seen
    return out


def _write(snap: dict, *, force_history: bool = False, latest_extra: dict | None = None) -> bool:
    """Write ``latest.json`` (the snapshot plus ``latest_extra``) and, when due, append the snapshot alone to
    ``rate_limits.jsonl``: the history rows are unchanged.
    Returns whether the history line was appended -- callers use this to
    know whether THIS TICK became "this session's last row" (see
    :func:`_update_sessions`'s ``history_cost_usd`` parameter)."""
    d = quota_dir()
    os.makedirs(d, exist_ok=True)
    latest = os.path.join(d, "latest.json")
    _atomic_write(latest, lambda f: json.dump({**snap, **(latest_extra or {})}, f, ensure_ascii=False))
    _sweep_stale_tmp(d, snap["epoch"])
    history = os.path.join(d, "rate_limits.jsonl")
    if _should_append(history, snap, snap["epoch"], force=force_history):
        with open(history, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        return True
    return False


def _cost_usd(cost: object) -> float | None:
    value = cost.get("total_cost_usd") if isinstance(cost, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _prior_session_cost_usd(sessions_doc: object, sid: object) -> float | None:
    """The ``session_cost_usd`` this session carried on its LAST recorded
    HISTORY row (``sessions.json``'s ``last_history_cost_usd``, set only
    when :func:`_write` actually appended one for this session -- never on
    a tick that merely ticked) -- so the new append rule (design L4 Section
    2.1) can tell "rose by >= 0.50 since this session's last row" without
    re-reading the whole history file per session. ``None`` both when the
    session has no row yet and when its recorded cost is unusable, which is
    exactly the "no row yet" case the rule treats the same way (force an
    append)."""
    if not isinstance(sessions_doc, dict) or not isinstance(sid, str) or not sid:
        return None
    sessions = sessions_doc.get("sessions")
    entry = sessions.get(sid) if isinstance(sessions, dict) else None
    if not isinstance(entry, dict):
        return None
    value = entry.get("last_history_cost_usd")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _should_force_history(sessions_doc: object, sid: object, session_cost_usd: float | None) -> bool:
    if session_cost_usd is None:
        return False
    prior = _prior_session_cost_usd(sessions_doc, sid)
    if prior is None:
        return True  # this session has no row yet
    return (session_cost_usd - prior) >= HISTORY_MIN_DELTA_COST_USD - 1e-9


def _update_accounts(label: str, now_epoch: float, five_resets: object, *, appended: bool) -> None:
    """Upsert ``accounts.json`` (design L4 Section 2.1): a flat
    ``{label: {first_seen_ts, last_seen_ts, n_rows, windows_seen}}`` map,
    written atomically like ``latest.json``. ``windows_seen`` holds the
    distinct five-hour ``resets_at`` values seen for this label, most
    recent last, capped at :data:`ACCOUNTS_WINDOWS_KEEP`. Never raises:
    callers wrap this the same way as :func:`_record_session`.

    N2: ``n_rows`` only counts a tick that actually appended a
    ``rate_limits.jsonl`` row (``appended=True``) -- it used to count every
    status-line tick regardless, which is a different (larger, and
    throttle-dependent) number than its name promises. ``first_seen_ts``/
    ``last_seen_ts``/``windows_seen`` still track every tick: the account
    genuinely WAS observed then, whether or not that tick wrote history."""
    d = quota_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "accounts.json")
    doc = _read_json(path)
    accounts = doc if isinstance(doc, dict) else {}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch))
    entry = accounts.get(label) if isinstance(accounts.get(label), dict) else {}
    windows_seen = list(entry.get("windows_seen")) if isinstance(entry.get("windows_seen"), list) else []
    if isinstance(five_resets, (int, float)) and not isinstance(five_resets, bool):
        if not windows_seen or windows_seen[-1] != five_resets:
            windows_seen.append(five_resets)
    windows_seen = windows_seen[-ACCOUNTS_WINDOWS_KEEP:]
    accounts[label] = {
        "first_seen_ts": entry.get("first_seen_ts") or stamp,
        "last_seen_ts": stamp,
        "n_rows": int(entry.get("n_rows") or 0) + (1 if appended else 0),
        "windows_seen": windows_seen,
    }
    _atomic_write(path, lambda f: json.dump(accounts, f, ensure_ascii=False))


def _record_account(label: str, now_epoch: float, five_resets: object, *, appended: bool) -> None:
    try:
        _update_accounts(label, now_epoch, five_resets, appended=appended)
    except Exception:
        pass


def _record_session(payload: dict, now_epoch: float, *, history_cost_usd: float | None = None) -> None:
    """Never let the per-session file break the status line."""
    try:
        _update_sessions(payload, now_epoch, history_cost_usd=history_cost_usd)
    except Exception:
        pass


def main() -> int:
    try:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES) if hasattr(sys.stdin, "buffer") else sys.stdin.read(MAX_STDIN_BYTES)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("statusLine payload is not an object")
        rate_limits = payload.get("rate_limits")
        model = payload.get("model") or {}
        ctx = payload.get("context_window") or {}
        model_name = str(model.get("display_name") or "")
        ctx_pct = ctx.get("used_percentage")
        if isinstance(rate_limits, dict) and rate_limits:
            now_epoch = time.time()
            session_cost_usd = _cost_usd(payload.get("cost"))
            sid = payload.get("session_id")
            sessions_doc = _read_json(os.path.join(quota_dir(), "sessions.json"))
            snap = {
                "epoch": now_epoch,
                "captured_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch)),
                "rate_limits": rate_limits,
                "model": model_name,
                "session_id": sid,
                "cc_version": payload.get("version"),
                "account_hint": os.environ.get("CLAUDE_CONFIG_DIR", ""),
                "account_label": os.environ.get("TRIALERROR_ACCOUNT", ""),
                "payload_cost_api_ms": _api_ms(payload),
                "session_cost_usd": session_cost_usd,
            }
            previous_latest = _read_json(os.path.join(quota_dir(), "latest.json"))
            snap["numbers_changed_ts"] = _numbers_changed_ts(previous_latest, rate_limits, snap["captured_ts"])
            force_history = _should_force_history(sessions_doc, sid, session_cost_usd)
            appended = _write(
                snap, force_history=force_history, latest_extra=_prev_resets(previous_latest, rate_limits)
            )
            print(_status_line(rate_limits, ctx_pct, model_name))
            _record_session(payload, now_epoch, history_cost_usd=session_cost_usd if appended else None)
            five = rate_limits.get("five_hour")
            five_resets = five.get("resets_at") if isinstance(five, dict) else None
            _record_account(snap["account_label"], now_epoch, five_resets, appended=appended)
        else:
            _record_session(payload, time.time())
            # API-key session or pre-2.1.80 client: still be a useful status line.
            print(_status_line({}, ctx_pct, model_name))
        return 0
    except Exception:
        print("TRIALERROR | (no quota data)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
