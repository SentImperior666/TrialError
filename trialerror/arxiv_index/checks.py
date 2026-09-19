"""``arxiv_index_ready`` doctor check. Build brief item 5: "absent /
building / ready + row count + dims sanity." Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` purely because this
file exists at ``trialerror/arxiv_index/checks.py`` -- zero edits to
``trialerror/util/doctor.py`` or any other shared file (same convention every
other subsystem's own ``checks.py`` uses).

No network, no live OpenAI call -- this check only opens the standalone
index db (read-only inspection of its own build-state/meta tables), same
"config/state-inspection only" posture ``trialerror.litapi.checks.
check_litapi_providers_ready`` documents for its own always-runs check.
"""

from __future__ import annotations

from trialerror.arxiv_index.store import default_db_path, get_build_state, row_count
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["EXPECTED_DIMS", "check_arxiv_index_ready"]

#: text-embedding-3-large's real, published dimensionality
#: (docs/reviews/ALL_ARXIV_SEARCH.md Sec 2, confirmed from the dataset's
#: own schema.org block) -- the dims-sanity check's reference value.
EXPECTED_DIMS = 3072


def _resolve_db_path(ctx: DoctorContext) -> str | None:
    if ctx.program_root is None:
        return None
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = ctx.program_root / CONFIG_FILENAME
    raw: dict = {}
    if cfg_path.is_file():
        try:
            raw = load_config(cfg_path).raw
        except Exception:
            raw = {}
    configured = ((raw.get("litapi", {}) or {}).get("arxiv_index", {}) or {}).get("db_path")
    if configured:
        from pathlib import Path

        p = Path(configured)
        return str(p if p.is_absolute() else ctx.program_root / p)
    return str(default_db_path(ctx.program_root))


@register_check("arxiv_index_ready", category="arxiv_index")
def check_arxiv_index_ready(ctx: DoctorContext) -> CheckResult:
    db_path = _resolve_db_path(ctx)
    if db_path is None:
        return CheckResult(
            name="arxiv_index_ready", category="arxiv_index", status="skip",
            message="no program_root configured -- cannot resolve [litapi.arxiv_index].db_path",
        )

    from pathlib import Path

    if not Path(db_path).is_file():
        return CheckResult(
            name="arxiv_index_ready", category="arxiv_index", status="skip",
            message=f"no arxiv semantic index built yet (no db at {db_path}) -- run "
            "`trialerror lit arxiv-index build --zip <path>` first",
            details={"state": "absent", "db_path": db_path},
        )

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        state = get_build_state(conn)
        count = row_count(conn)
    finally:
        conn.close()

    if not state:
        return CheckResult(
            name="arxiv_index_ready", category="arxiv_index", status="skip",
            message=f"db file exists at {db_path} but has no build-state (schema never initialized)",
            details={"state": "absent", "db_path": db_path},
        )

    status_str = state.get("status", "unknown")
    dims = int(state["dims"]) if state.get("dims") else None
    dims_ok = dims == EXPECTED_DIMS

    details = {
        "state": status_str,
        "db_path": db_path,
        "backend": state.get("backend"),
        "row_count": count,
        "dims": dims,
        "dims_expected": EXPECTED_DIMS,
        "dims_ok": dims_ok,
        "zip_path": state.get("zip_path"),
        "last_updated_ts": state.get("last_updated_ts"),
    }

    if status_str == "building":
        return CheckResult(
            name="arxiv_index_ready", category="arxiv_index", status="warn",
            message=f"build in progress ({count} rows ingested so far) -- resume with "
            "`trialerror lit arxiv-index build --zip <same path>` if it was interrupted",
            details=details,
        )
    if status_str != "complete":
        return CheckResult(
            name="arxiv_index_ready", category="arxiv_index", status="warn",
            message=f"unexpected build-state status {status_str!r}", details=details,
        )
    if not dims_ok:
        return CheckResult(
            name="arxiv_index_ready", category="arxiv_index", status="fail",
            message=f"index built at dims={dims}, expected {EXPECTED_DIMS} (text-embedding-3-large) -- "
            "queries embedded with the real OpenAI model will not compare correctly against this index",
            details=details,
        )
    return CheckResult(
        name="arxiv_index_ready", category="arxiv_index", status="pass",
        message=f"ready -- {count} rows, dims={dims}, backend={state.get('backend')}",
        details=details,
    )
