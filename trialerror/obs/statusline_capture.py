"""Claude Code statusLine hook: capture plan rate-limit quota into the store.

Claude Code >= 2.1.80 passes a ``rate_limits`` object in the statusLine
JSON on stdin (five_hour / seven_day windows with ``used_percentage`` and
``resets_at``; claude.ai subscriptions only — API-key sessions omit it).
This script is the capture side of the budget quota feed:

  * writes ``latest.json`` (atomic) + a throttled ``rate_limits.jsonl``
    history under the quota dir (``TRIALERROR_QUOTA_DIR`` or ``~/.trialerror/quota``)
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


def _write(snap: dict) -> None:
    d = quota_dir()
    os.makedirs(d, exist_ok=True)
    latest = os.path.join(d, "latest.json")
    tmp = latest + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, latest)
    history = os.path.join(d, "rate_limits.jsonl")
    if _should_append(history, snap, snap["epoch"]):
        with open(history, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")


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
            }
            _write(snap)
            print(_status_line(rate_limits, ctx_pct, model_name))
        else:
            # API-key session or pre-2.1.80 client: still be a useful status line.
            print(_status_line({}, ctx_pct, model_name))
        return 0
    except Exception:
        print("TRIALERROR | (no quota data)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
