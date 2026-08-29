"""On-demand doctor invocation + its sidecar state file.

Design brief: "doctor panel (last doctor run results -- run doctor on
demand via a serve endpoint)." Deliberately NOT part of the passive
panel-refresh loop the SSE watcher drives -- ``trialerror doctor``'s checks can
be non-trivial work (a full XID-dangling scan across every table, a
vendored/-tree license scan) and are explicitly an OPERATOR-INITIATED
action here, not something that fires on every file-save the way
mechspace's rebuild used to have to guard against for embeddings
(``MECHSPACE_NO_EMBED``). This module's ``run_doctor_and_persist`` is only
ever called from a dedicated serve endpoint (``GET /dashboard/api/doctor/
run``) or the CLI, never from the watcher thread.

The result is persisted to one JSON sidecar file under the program root
(``<program_root>/.trialerror_dashboard/doctor_state.json``) -- a NEW file this
dashboard layer owns, exactly the same "sidecar next to the thing it
reports on, not one of the program's real stores" convention
``serve_mechspace.py``'s own ``rebuild_state.json`` documents (see that
file's README_LIVE.md, ``data/rebuild_state.json`` section, read as this
build's reference architecture) -- so a human/agent inspecting the program
root after a live dashboard session finds it in the obvious place, and a
restarted dashboard server picks the last result back up without re-running
doctor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks
from trialerror.util.timeutil import now

__all__ = ["doctor_state_path", "read_doctor_state", "run_doctor_and_persist"]

_STATE_DIRNAME = ".trialerror_dashboard"
_STATE_FILENAME = "doctor_state.json"


def doctor_state_path(program_root: Path | str) -> Path:
    return Path(program_root) / _STATE_DIRNAME / _STATE_FILENAME


def read_doctor_state(program_root: Path | str | None) -> dict[str, Any] | None:
    if program_root is None:
        return None
    path = doctor_state_path(program_root)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def run_doctor_and_persist(
    *,
    repo_root: Path | str,
    program_root: Path | str | None,
    platform_root: Path | str | None,
) -> dict[str, Any]:
    """Runs every registered doctor check (same framework ``trialerror doctor``
    itself uses -- ``trialerror.util.doctor.discover_and_register_checks`` +
    ``run_checks``) and persists a summary to the sidecar file. Returns the
    persisted dict. Doctor checks are themselves read-only (each opens its
    own ``connect(path, read_only=True)``, per every ``checks.py`` module
    read for this build) -- this function's only WRITE is the sidecar file
    itself, and only when ``program_root`` is given (no program chosen yet
    -> nothing to persist against, result is still returned for the
    caller's immediate use)."""
    discover_and_register_checks()
    ctx = DoctorContext(
        repo_root=Path(repo_root),
        program_root=Path(program_root) if program_root else None,
        platform_root=Path(platform_root) if platform_root else None,
    )
    results = run_checks(ctx)
    failed = [r for r in results if r.status == "fail"]
    warned = [r for r in results if r.status == "warn"]
    passed = [r for r in results if r.status == "pass"]
    skipped = [r for r in results if r.status == "skip"]

    state = {
        "schema": "trialerror-dashboard-doctor-state@v1",
        "ran_ts": now(),
        "summary": {
            "total": len(results),
            "passed": len(passed),
            "failed": len(failed),
            "warned": len(warned),
            "skipped": len(skipped),
        },
        "checks": [r.to_dict() for r in results],
    }

    if program_root is not None:
        path = doctor_state_path(program_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write("\n")
        tmp.replace(path)  # atomic-ish swap, same convention serve_mechspace.write_json uses

    return state
