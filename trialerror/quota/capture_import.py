"""``trialerror quota import`` (design ``L4_quota-policy.md`` Section 2.3 step
1 / Section 2.5): read a host's ``rate_limits.jsonl`` capture history
(:mod:`trialerror.obs.statusline_capture`'s output) and insert it into
``platform.db``'s ``quota_capture`` table.

Idempotent through the table's own ``UNIQUE (host, epoch, session_id)``
constraint (design Section 2.2): re-running against the same file, or a
freshly re-pulled copy of the same file (the sandbox's history arrives over
ssh ``cat`` to a temporary file -- design Section 2.5), inserts nothing a
second time. This module never writes to the source history file, and never
reads message text -- only the keys design Section 1 documents."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

__all__ = ["import_history"]


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _win(row: dict, key: str) -> tuple[float | None, float | None]:
    win = (row.get("rate_limits") or {}).get(key) if isinstance(row.get("rate_limits"), dict) else None
    if not isinstance(win, dict):
        return None, None
    return _num(win.get("used_percentage")), _num(win.get("resets_at"))


def import_history(conn: sqlite3.Connection, history_path: str, host: str) -> dict[str, Any]:
    """Import one host's ``rate_limits.jsonl`` into ``quota_capture``.

    A line that is not valid JSON, is not an object, or carries no usable
    ``epoch`` is counted as malformed and skipped -- never raises, so one
    torn line (a crash mid-append) never aborts the whole import. Returns a
    report dict: ``host``, ``rows_read``, ``rows_inserted``,
    ``rows_duplicate`` and ``rows_malformed``."""
    read = inserted = duplicate = malformed = 0
    try:
        with open(history_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return {"host": host, "rows_read": 0, "rows_inserted": 0, "rows_duplicate": 0, "rows_malformed": 0}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        read += 1
        try:
            row = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(row, dict):
            malformed += 1
            continue
        epoch = _num(row.get("epoch"))
        if epoch is None:
            malformed += 1
            continue
        five_pct, five_resets = _win(row, "five_hour")
        seven_pct, seven_resets = _win(row, "seven_day")
        cur = conn.execute(
            "INSERT OR IGNORE INTO quota_capture "
            "(host, account_label, epoch, captured_ts, session_id, session_cost_usd, "
            "five_pct, five_resets, seven_pct, seven_resets, cc_version, model) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                host,
                row.get("account_label") or "",
                epoch,
                row.get("captured_ts") or "",
                row.get("session_id"),
                _num(row.get("session_cost_usd")),
                five_pct,
                int(five_resets) if five_resets is not None else None,
                seven_pct,
                int(seven_resets) if seven_resets is not None else None,
                row.get("cc_version"),
                row.get("model"),
            ),
        )
        if cur.rowcount:
            inserted += 1
        else:
            duplicate += 1
    conn.commit()
    return {
        "host": host,
        "rows_read": read,
        "rows_inserted": inserted,
        "rows_duplicate": duplicate,
        "rows_malformed": malformed,
    }
