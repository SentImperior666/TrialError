"""``trialerror dashboard`` -- the v1 LIVE DASHBOARD's CLI surface (design
Section 11). Thin argv/envelope shell over ``trialerror.dashboard.serve`` /
``trialerror.dashboard.export`` -- all logic lives there (design Section 5.2
registration rule: "each CLI group lives in its own module
``trialerror/cli/<group>.py`` ... no implementation lane ever edits a shared
``cli/__init__.py``"; this file is that drop-in).

Two subcommands:

- ``trialerror dashboard serve`` -- by default spawns a DETACHED server process
  and returns immediately (same shape as ``trialerror jobs start-worker``'s own
  detached-by-default convention -- see ``trialerror.jobs.worker.spawn_worker``,
  read as this command's reference); ``--foreground`` runs inline in THIS
  process instead (blocking until Ctrl+C) -- this is what the detached
  child itself invokes, and what a test subprocess drives directly.
- ``trialerror dashboard export`` -- writes one self-contained snapshot HTML
  file and returns.

TRIALERROR-DEV-NOTE (detached-by-default, a deliberate deviation from the
mechspace reference): ``serve_mechspace.py`` (this build's named
architecture reference) is a standalone script always run in the
foreground -- ``python serve_mechspace.py``, Ctrl+C to stop, no detached
mode at all. Every OTHER ``trialerror`` CLI command returns control to the
shell immediately (design's own ``AgentEnvelope`` framing assumes a
one-shot command, not a blocking one), and this codebase already has an
established convention for a long-running local server needing to fit that
shape: ``trialerror jobs start-worker`` spawns a DETACHED child by default and
only blocks under an explicit ``--foreground`` (``trialerror.jobs.worker.
spawn_worker``). ``trialerror dashboard serve`` follows THAT precedent rather
than mechspace's always-foreground one, since an agent driving this CLI
programmatically needs its shell back, and ``--foreground`` is exactly the
one-line escape hatch (used by this build's own subprocess smoke test,
``tests/test_dashboard_serve.py``) when blocking is what's wanted.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from trialerror.dashboard import export as dashboard_export
from trialerror.dashboard import serve as dashboard_serve
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "dashboard"
HELP = "Local read-only web view over a program's stores: serve (live, SSE) or export (static snapshot)."


def _common(p: argparse.ArgumentParser) -> None:
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root/
    # --platform-root the top-level parser resolved.
    p.add_argument(
        "--program-root", default=argparse.SUPPRESS,
        help="program scaffold root (default: discovered from CWD via trialerror.toml; "
        "omit entirely for a program-agnostic, platform-only view)",
    )
    p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="dashboard_cmd", metavar="<command>", required=True)

    p_serve = sub.add_parser("serve", help="start the live server (detached by default; --foreground to run inline)")
    _common(p_serve)
    p_serve.add_argument("--port", type=int, default=8850)
    p_serve.add_argument("--host", default="127.0.0.1", help="loopback only by default -- this is a local dev tool")
    p_serve.add_argument("--poll-interval", type=float, default=dashboard_serve.DEFAULT_POLL_INTERVAL_S)
    p_serve.add_argument("--debounce", type=float, default=dashboard_serve.DEFAULT_DEBOUNCE_S)
    p_serve.add_argument("--no-watch", action="store_true", help="serve only, disable the watcher/SSE-change thread")
    p_serve.add_argument("--repo-root", default=None, help="repo root the doctor panel's license_audit check scans (default: CWD)")
    p_serve.add_argument("--log-dir", default=None, help="detached-mode log file directory (default: <program_root>/dashboard_logs, or CWD if no program root)")
    p_serve.add_argument(
        "--foreground", action="store_true",
        help="run inline in THIS process instead of spawning a detached one (this is what the detached child itself invokes)",
    )
    p_serve.set_defaults(handler=_cmd_serve)

    p_export = sub.add_parser("export", help="write a self-contained static snapshot HTML file")
    _common(p_export)
    p_export.add_argument("--out", required=True, help="output .html path")
    p_export.add_argument("--repo-root", default=None, help="repo root the doctor panel's license_audit check scans (default: CWD)")
    p_export.add_argument("--run-doctor", action="store_true", help="run doctor's full check suite fresh before exporting (default: report the last run, if any)")
    p_export.set_defaults(handler=_cmd_export)

    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    given = getattr(args, "program_root", None)
    if given:
        return Path(given)
    return find_program_root()


def _resolve_platform_root(args: argparse.Namespace) -> Path | None:
    given = getattr(args, "platform_root", None)
    return Path(given) if given else None


def _cmd_serve(args: argparse.Namespace) -> dict[str, Any]:
    program_root = _resolve_program_root(args)
    platform_root = _resolve_platform_root(args)

    if args.foreground:
        argv = [
            "--port", str(args.port), "--host", args.host,
            "--poll-interval", str(args.poll_interval), "--debounce", str(args.debounce),
        ]
        if program_root is not None:
            argv += ["--program-root", str(program_root)]
        if platform_root is not None:
            argv += ["--platform-root", str(platform_root)]
        if args.repo_root:
            argv += ["--repo-root", args.repo_root]
        if args.no_watch:
            argv.append("--no-watch")
        rc = dashboard_serve.main(argv)  # blocks until Ctrl+C / process signal
        if rc != 0:
            return error_envelope("dashboard serve", "serve_exited_nonzero", f"server exited with code {rc}")
        return ok_envelope("dashboard serve", result={"foreground": True, "exit_code": rc})

    log_dir = Path(args.log_dir) if args.log_dir else (program_root / "dashboard_logs" if program_root else Path.cwd())
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"dashboard-{args.port}.log"

    child_argv = [
        sys.executable, "-m", "trialerror.cli", "dashboard", "serve", "--foreground",
        "--port", str(args.port), "--host", args.host,
        "--poll-interval", str(args.poll_interval), "--debounce", str(args.debounce),
    ]
    if program_root is not None:
        child_argv += ["--program-root", str(program_root)]
    if platform_root is not None:
        child_argv += ["--platform-root", str(platform_root)]
    if args.repo_root:
        child_argv += ["--repo-root", args.repo_root]
    if args.no_watch:
        child_argv.append("--no-watch")

    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:  # pragma: no cover - this design is Windows-first (Section 13); POSIX fallback only
        popen_kwargs["start_new_session"] = True

    log_fh = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            child_argv,
            cwd=str(program_root) if program_root else None,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            **popen_kwargs,
        )
    finally:
        log_fh.close()

    url = f"http://{args.host}:{args.port}/"
    return ok_envelope(
        "dashboard serve",
        result={"pid": proc.pid, "url": url, "log_path": str(log_path), "argv": child_argv},
        next_actions=[next_action(["trialerror", "dashboard", "export", "--out", "snapshot.html"], "capture a static snapshot instead")],
    )


def _cmd_export(args: argparse.Namespace) -> dict[str, Any]:
    program_root = _resolve_program_root(args)
    platform_root = _resolve_platform_root(args)
    try:
        out_path = dashboard_export.export_snapshot(
            out_path=args.out,
            program_root=program_root,
            platform_root=platform_root,
            repo_root=args.repo_root,
            run_doctor=args.run_doctor,
        )
    except Exception as exc:  # noqa: BLE001 - report honestly, never crash the CLI
        return error_envelope("dashboard export", "export_failed", str(exc))
    return ok_envelope("dashboard export", result={"out_path": str(out_path)})
