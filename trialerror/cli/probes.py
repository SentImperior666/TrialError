"""``trialerror probes`` -- the CLI surface over :mod:`trialerror.probes.
registry` (design Section 3.1). Like ``trialerror units`` (design Section
2.3, F1's own machine-wide rationale), a probe result is a fact about this
HOST's Claude Code install, not about any one program, so this group also
opens only the platform connection.

``--live`` is the one flag this module never invokes on its own: design
Section 3.3 -- "The command prints 'this spends one small model run' before
starting. The custodian runs it once per Claude Code version, when the
meter says GO." Automated callers must never run ``probes run --live`` at
all; only a human operator does. The code path exists (design requires it),
and is exercised in tests only via a mocked subprocess -- see
``tests/test_probes_cli.py``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from trialerror.probes.registry import ProbeContext, ProbeRunRow, discover_and_register_probes, run_probes
from trialerror.stores import paths as store_paths
from trialerror.stores.connection import connect
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform as platform_schema
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root, resolve_program_id
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.util.timeutil import now, parse

GROUP_NAME = "probes"
HELP = "Conformance probes, canaries and answer stamps (design F5)."

#: design Section 3.3 verbatim -- printed before a --live run ever shells out.
LIVE_WARNING = "this spends one small model run"

#: A second, independent safety interlock on top of "the CLI flag exists but
#: an automated caller must never pass it": run_live_capture() REFUSES to
#: actually invoke `claude -p` unless this exact env var is "1" -- so a test
#: whose mock/monkeypatch of run_live_capture somehow fails to take
#: (observed once during earlier development: a case where the real
#: function ran instead of the test double, since fixed by excluding this
#: module from probe discovery -- see trialerror.probes.registry's own
#: _EXCLUDED_SUBPACKAGES) still cannot shell out to a real, token-spending
#: Claude Code session. The operator is the only one who ever sets this.
LIVE_CAPTURE_ALLOW_ENV = "TRIALERROR_ALLOW_LIVE_PROBE_CAPTURE"


def _add_platform_root_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--platform-root", default=argparse.SUPPRESS,
        help="override the platform root (default: TRIALERROR_PLATFORM_ROOT or ~/.trialerror)",
    )


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_run = actions.add_parser("run", help="run registered probes and record their results")
    _add_platform_root_arg(p_run)
    p_run.add_argument(
        "--program-root", default=None,
        help="the program root, so canaries/embed_coverage can open its knowledge store and the "
        "recorded rows are scoped to this program (B-1/B-2; default: discover trialerror.toml "
        "upward from CWD -- conformance probes need no program root at all)",
    )
    p_run.add_argument("--kind", default=None, choices=["conformance", "canary", "drill"])
    p_run.add_argument("--name", dest="names", action="append", default=None)
    p_run.add_argument("--host", default="default", help="a label for this host (e.g. dev, sandbox)")
    p_run.add_argument(
        "--live", action="store_true",
        help="run one small headless Claude Code turn to refresh hook_events.jsonl for the current "
        "version -- SPENDS MODEL TOKENS; a human operator's to run, never an automated caller's",
    )
    p_run.add_argument("--live-model", default=None, help="the model --live uses (default: the program's cheapest allowed model, if configured)")
    p_run.set_defaults(handler=_run_run)

    p_status = actions.add_parser("status", help="the latest run of each probe, with its age")
    _add_platform_root_arg(p_status)
    p_status.add_argument("--host", default=None)
    p_status.set_defaults(handler=_run_status)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "probes", "no_action", "specify an action: run|status",
        next_actions=[next_action(["trialerror", "probes", "--help"], "list probes actions")],
    )


def _memory_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_platform_root(args: argparse.Namespace) -> Path:
    raw = getattr(args, "platform_root", None)
    return Path(raw) if raw else store_paths.platform_root()


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "program_root", None)
    if raw:
        return Path(raw)
    return find_program_root()


def _open_platform_only(args: argparse.Namespace) -> Store:
    """Same rationale as ``trialerror.cli.units._open_platform_only``: only
    ``platform.db`` is real; ops/knowledge/jobs are throwaway in-memory
    connections, because ``probe_run`` (like ``unit``) lives in platform.db."""
    platform_root = _resolve_platform_root(args)
    # check_same_thread=False: trialerror.probes.registry.run_probes runs each
    # probe body on its own (sequential, joined-before-the-next-starts)
    # thread to enforce its timeout -- see connect()'s own docstring.
    platform_conn = connect(store_paths.platform_db_path(root=platform_root), check_same_thread=False)
    apply_migrations(platform_conn, platform_schema.MIGRATIONS)
    return Store(
        platform=platform_conn, ops=_memory_conn(), knowledge=_memory_conn(), jobs=_memory_conn(),
        program_root=Path("."), platform_root=platform_root,
    )


def _open_stores_for_run(args: argparse.Namespace) -> tuple[Store, Store | None]:
    """``(platform_store, program_store)`` for ``probes run`` (B-1 fix
    round). When a program root is available (``--program-root``, or
    discovered from CWD), ONE full :class:`Store` is opened and reused for
    BOTH roles -- the same shape :mod:`trialerror.hooks.session_start` uses
    -- so ``platform.db`` is never opened twice for one run. Otherwise falls
    back to :func:`_open_platform_only`, and ``program_store`` is ``None``:
    canaries/``embed_coverage`` then correctly report "skip: no program
    store given" instead of silently doing nothing, which is what happened
    before this fix (B-1's third bug: this CLI opened only the platform
    store, unconditionally)."""
    program_root = _resolve_program_root(args)
    if program_root is not None:
        platform_root = _resolve_platform_root(args)
        try:
            full = open_store(program_root, platform_root=platform_root, check_same_thread=False)
            return full, full
        except Exception:  # noqa: BLE001 - fall back to platform-only rather than refuse the whole run
            pass
    return _open_platform_only(args), None


def detect_cc_version(timeout_s: float = 5.0) -> str | None:
    """``claude --version``'s own output, or ``None`` when the binary is
    absent/unreachable -- a local subprocess call, not a model turn (design
    Section 3.3: "Everything is read from local files, at no model cost,
    unless --live is given"; a version check is neither a transcript read
    nor a model call)."""
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _row_to_dict(row: ProbeRunRow) -> dict:
    return {
        "name": row.name, "kind": row.kind, "host": row.host, "program_id": row.program_id,
        "cc_version": row.cc_version, "build": row.build, "started_ts": row.started_ts,
        "finished_ts": row.finished_ts, "status": row.status, "detail": row.detail,
    }


def live_capture_command(model: str) -> list[str]:
    """design Section 3.3's exact ``--live`` command: one small headless run
    that spawns a subagent and lets it complete, so ``hook_events.jsonl``
    gets a real SubagentStart/SubagentStop pair for the CURRENT version."""
    prompt = "Use the Agent tool once to spawn a subagent that replies ok, then reply ok."
    return ["claude", "-p", "--model", model, "--output-format", "json", prompt]


def run_live_capture(*, model: str, cwd: Path, timeout_s: float = 180.0) -> dict:
    """Runs design Section 3.3's ``--live`` command and returns
    ``{"session_id": ..., "ok": bool}`` or ``{"error": ...}``. NEVER called
    by this test suite except with a mocked ``subprocess.run`` -- see this
    module's docstring.

    Refuses immediately (returns ``{"error": ...}``, never calls
    ``subprocess.run``) unless :data:`LIVE_CAPTURE_ALLOW_ENV` is exactly
    ``"1"`` -- see that constant's own docstring for why this guard exists
    on top of the CLI flag."""
    import os

    print(LIVE_WARNING)
    if os.environ.get(LIVE_CAPTURE_ALLOW_ENV) != "1":
        return {
            "error": f"refused: {LIVE_CAPTURE_ALLOW_ENV}=1 is required to actually run a --live capture "
            "(an automated caller must never set it; a human operator's to run)"
        }
    cmd = live_capture_command(model)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=str(cwd))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    try:
        parsed = json.loads(proc.stdout)
    except ValueError:
        parsed = {}
    session_id = parsed.get("session_id") if isinstance(parsed, dict) else None
    return {"ok": proc.returncode == 0, "session_id": session_id, "returncode": proc.returncode}


def _run_run(args: argparse.Namespace) -> dict:
    platform_store, program_store = _open_stores_for_run(args)
    try:
        discover_and_register_probes()

        live_result = None
        if args.live:
            model = args.live_model or "claude-haiku-4-5-20251001"
            live_result = run_live_capture(model=model, cwd=Path.cwd())
            if live_result.get("error"):
                return error_envelope("probes.run", "live_capture_failed", live_result["error"])

        cc_version = detect_cc_version()
        program_root = _resolve_program_root(args)
        program_id = resolve_program_id(program_root) if program_root is not None else None
        ctx = ProbeContext(
            host=args.host, platform_store=platform_store, store=program_store,
            program_root=program_root, program_id=program_id,
            cc_version=cc_version, live=bool(args.live),
        )
        rows = run_probes(ctx, kind=args.kind, names=args.names)
        result = {"probes": [_row_to_dict(r) for r in rows]}
        if live_result is not None:
            result["live_capture"] = live_result

        failed = [r for r in rows if r.status == "fail"]
        if failed:
            return error_envelope(
                "probes.run", "probe_failed", f"{len(failed)} probe(s) failed: {', '.join(r.name for r in failed)}",
                details=result,
            )
        return ok_envelope("probes.run", result=result)
    finally:
        if program_store is not None and program_store is not platform_store:
            program_store.close()
        platform_store.close()


def _humanize_age(seconds: float) -> str:
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _run_status(args: argparse.Namespace) -> dict:
    store = _open_platform_only(args)
    try:
        clauses, params = [], []
        if args.host:
            clauses.append("host = ?")
            params.append(args.host)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        names = [
            r["name"]
            for r in store.platform.execute(f"SELECT DISTINCT name FROM probe_run {where}", params).fetchall()
        ]
        current = now()
        latest = []
        for name in sorted(names):
            row = store.platform.execute(
                f"SELECT * FROM probe_run {where}{' AND' if where else ' WHERE'} name = ? "
                "ORDER BY started_ts DESC LIMIT 1",
                (*params, name),
            ).fetchone()
            if row is None:
                continue
            try:
                age_s = (parse(current) - parse(row["started_ts"])).total_seconds()
                age = _humanize_age(age_s)
            except ValueError:
                age = "unknown"
            latest.append(
                {
                    "name": row["name"], "kind": row["kind"], "status": row["status"],
                    "started_ts": row["started_ts"], "age": age, "build": row["build"],
                }
            )
        return ok_envelope("probes.status", result={"probes": latest})
    finally:
        store.close()
