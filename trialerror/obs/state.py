"""Durable, cross-process span-drop bookkeeping. Backs the
``obs_span_drop_counter`` doctor check (M12 integration contract: "doctor
checks in trialerror/obs/checks.py (exporter-reachable, span-drop counter)").

Why a file and not an in-memory counter: the thing counting drops
(``trialerror.obs.tracer``'s exporter wrapper) usually lives in a short-lived CLI
process (a `trialerror jobs start-worker --foreground` run, a single `trialerror
budget reconcile`, ...), while the thing reading the counter
(``trialerror doctor``) is a SEPARATE process invocation entirely -- an
in-process counter would always read back zero. One small JSON file per
program root, written with :func:`trialerror.util.atomic.atomic_write_text` (the
platform's one non-SQLite durable-write primitive -- see that module's
docstring), gives ``trialerror doctor`` something real to read regardless of
which process(es) did the dropping.

Deliberately NOT a `trialerror.events` row: recording a drop must never itself
be able to raise or block emission's own no-op guarantee (design Section
4.5: "tracing must never block operations") the way a SQLite write with its
own XID/redaction machinery could. Every call in this module is wrapped so
a bookkeeping failure is silently swallowed -- losing a drop COUNT is
acceptable; the drop itself was already silent by design.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from trialerror.util.atomic import atomic_write_text
from trialerror.util.timeutil import now

__all__ = ["record_span_drop", "read_span_drop_state", "reset_for_tests", "process_drop_count"]

_STATE_SUBDIR = "obs"
_STATE_FILENAME = "span_drop_state.json"

#: In-process fallback counter, always incremented regardless of whether a
#: ``program_root`` was available to persist to -- cheap, and useful for
#: same-process tests/smoke scripts that never touch the filesystem state.
_process_lock = threading.Lock()
_process_count = 0


def _state_path(program_root: Path | str) -> Path:
    return Path(program_root) / _STATE_SUBDIR / _STATE_FILENAME


def process_drop_count() -> int:
    """The in-process fallback counter (see module docstring). Test-only
    convenience: nothing in ``trialerror.obs`` reads this for real reporting --
    :func:`read_span_drop_state` (the persisted, cross-process view) is
    what ``trialerror.obs.checks`` uses."""
    with _process_lock:
        return _process_count


def record_span_drop(program_root: Path | str | None, *, count: int = 1, reason: str = "") -> None:
    """Record ``count`` dropped span(s), best-effort. Never raises."""
    global _process_count
    with _process_lock:
        _process_count += count

    if program_root is None:
        return
    try:
        path = _state_path(program_root)
        existing = _read(path)
        existing["count"] = int(existing.get("count", 0)) + count
        existing["last_ts"] = now()
        existing["last_reason"] = (reason or "")[:500]
        atomic_write_text(path, json.dumps(existing, ensure_ascii=False, indent=2))
    except Exception:  # noqa: BLE001 - deliberate: bookkeeping must never break emission
        pass


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"count": 0, "last_ts": None, "last_reason": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"count": 0, "last_ts": None, "last_reason": None}


def read_span_drop_state(program_root: Path | str) -> dict[str, Any]:
    """The persisted, cross-process view :func:`trialerror.obs.checks
    <trialerror.obs.checks.check_span_drop_counter>` reports on. Missing/corrupt
    state reads back as a clean zero -- a program that never dropped a span
    (or never ran with obs deps installed) is not a doctor failure."""
    return _read(_state_path(program_root))


def reset_for_tests(program_root: Path | str | None = None) -> None:
    """Test-only: zero the in-process counter and, if given, delete the
    persisted state file so each test starts from a clean slate."""
    global _process_count
    with _process_lock:
        _process_count = 0
    if program_root is not None:
        _state_path(program_root).unlink(missing_ok=True)
