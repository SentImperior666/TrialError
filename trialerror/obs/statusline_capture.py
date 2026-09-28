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
REPLACE_ATTEMPTS = 3
REPLACE_SLEEP_S = 0.03
STALE_TMP_S = 3600
SESSIONS_KEEP_DAYS = 30
SESSIONS_REWRITE_S = 60
SESSIONS_MAX = 500
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


def _should_append(history_path: str, snap: dict, now_epoch: float) -> bool:
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


def _update_sessions(payload: dict, now_epoch: float) -> None:
    """Record this session's cost figures in ``sessions.json`` (atomic replace). Only numeric
    fields of the payload's ``cost`` object are kept; sessions unseen for 30 days are dropped and
    the file is capped at 500 sessions. Two sessions ticking at once can lose one another's
    latest update (last writer wins); the next tick restores it."""
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
    }
    pcts = _pcts(payload.get("rate_limits") or {}) if isinstance(payload.get("rate_limits"), dict) else {}
    if pcts:
        entry["window_pct"] = pcts
    # Nothing but the clock moved and the last write is under a minute old: skip the rewrite (the file
    # is up to ~200 KB at 500 sessions and every render would otherwise read and rewrite all of it).
    if prev and all(prev.get(k) == entry.get(k) for k in ("model", "cc_version", "cost", "window_pct")):
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


def _write(snap: dict) -> None:
    d = quota_dir()
    os.makedirs(d, exist_ok=True)
    latest = os.path.join(d, "latest.json")
    _atomic_write(latest, lambda f: json.dump(snap, f, ensure_ascii=False))
    _sweep_stale_tmp(d, snap["epoch"])
    history = os.path.join(d, "rate_limits.jsonl")
    if _should_append(history, snap, snap["epoch"]):
        with open(history, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")


def _record_session(payload: dict, now_epoch: float) -> None:
    """Never let the per-session file break the status line."""
    try:
        _update_sessions(payload, now_epoch)
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
            snap = {
                "epoch": now_epoch,
                "captured_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch)),
                "rate_limits": rate_limits,
                "model": model_name,
                "session_id": payload.get("session_id"),
                "cc_version": payload.get("version"),
                "account_hint": os.environ.get("CLAUDE_CONFIG_DIR", ""),
                "payload_cost_api_ms": _api_ms(payload),
            }
            snap["numbers_changed_ts"] = _numbers_changed_ts(
                _read_json(os.path.join(quota_dir(), "latest.json")), rate_limits, snap["captured_ts"]
            )
            _write(snap)
            print(_status_line(rate_limits, ctx_pct, model_name))
            _record_session(payload, now_epoch)
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
