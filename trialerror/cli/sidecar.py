"""``trialerror sidecar`` -- start, inspect and stop the long-lived helper
processes this program needs running (lane F-1b item 4).

Auto-discovered by :func:`trialerror.cli.discover_groups`, so adding this group
edits no shared file (the group contract in ``trialerror/cli/__init__.py``).

The verbs::

    trialerror sidecar start <name> [--cmd-from-config]
    trialerror sidecar status [<name>]
    trialerror sidecar stop <name> [--grace-s 10]

``start`` is idempotent, and idempotent under concurrency: it holds an
exclusive lock on ``run/sidecars/<name>.lock`` across the read-then-spawn
window, so a poll loop and an operator starting the same sidecar at the same
moment produce one process, not two. A start that cannot take the lock
refuses with ``start_in_progress`` and spawns nothing.

**A sidecar's command comes from ``[sidecars.<name>]`` and from nowhere
else.** There is no ``--cmd``, no ``--exec``, no shell string: the verbs take a
NAME, and ``trialerror.toml`` carries the argv, the working directory and the
environment (``LD_LIBRARY_PATH`` included -- a vendored runtime usually needs
it). ``--cmd-from-config`` is accepted on ``start`` because the brief names it
and because saying it out loud is worth a flag, but it is the only mode there
is; passing it changes nothing. That is the same reading ``webfetch sidecar``
takes of its policy directory, for the same reason: an agent should be able to
start the process an operator configured, and should not be able to start
something else.

Supervision is a poll. ``status`` is what restarts a ``restart = "always"``
sidecar it finds dead, which means the thing doing the supervising is whatever
already runs on a loop in this container and calls ``status`` -- no daemon
here claims to be watching after it has exited. ``--no-restart`` asks the same
questions and changes nothing.

The reference configuration for an embedding sidecar (generic paths -- fill in
your own; ``docs/OPERATOR_GUIDE.md`` carries the same block with the query
backend it serves)::

    [sidecars.embed]
    command = [
      "/opt/llama.cpp/llama-server",
      "-m", "/models/an-embedding-model.gguf",
      "--embedding", "--pooling", "last", "--embd-normalize", "-1",
      "-c", "2049", "-b", "2049", "-ub", "2049", "-t", "8",
      "--host", "127.0.0.1", "--port", "8871",
    ]
    env = { LD_LIBRARY_PATH = "/opt/llama.cpp" }
    health_url = "http://127.0.0.1:8871/health"
    restart = "always"
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from trialerror.sidecar.supervisor import (
    DEFAULT_STOP_GRACE_S,
    SIDECARS_TABLE,
    SidecarConfigError,
    SidecarStartBusy,
    sidecar_names,
    sidecar_status,
    start_sidecar,
    stop_sidecar,
)
from trialerror.util.config import CONFIG_FILENAME, ConfigError, find_program_root, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "sidecar"
HELP = "supervise this program's long-lived helper processes (e.g. the embedding server)."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="sidecar_cmd", metavar="<command>", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        # default=SUPPRESS so an unset value here never overwrites the global
        # --program-root/--platform-root the top-level parser resolved
        # (trialerror/cli/__init__.py's FX-12 note).
        p.add_argument(
            "--program-root",
            default=argparse.SUPPRESS,
            help="program scaffold root (default: discovered from CWD via trialerror.toml)",
        )
        p.add_argument(
            "--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)"
        )

    p_start = sub.add_parser("start", help=f"start the process [{SIDECARS_TABLE}.<name>] configures")
    p_start.add_argument("name", nargs="?", default=None, help="the configured sidecar's name (e.g. embed)")
    p_start.add_argument("--name", dest="name_flag", default=None, help="the same name, as a flag")
    p_start.add_argument(
        "--cmd-from-config",
        action="store_true",
        help=f"explicit (and the only mode): the argv comes from [{SIDECARS_TABLE}.<name>] command",
    )
    _common(p_start)
    p_start.set_defaults(handler=_cmd_start)

    p_status = sub.add_parser("status", help="what each configured sidecar is doing now")
    p_status.add_argument("name", nargs="?", default=None, help="one sidecar (default: all configured)")
    p_status.add_argument("--name", dest="name_flag", default=None, help="the same name, as a flag")
    p_status.add_argument(
        "--no-restart",
        action="store_true",
        help='report only: do not restart a dead sidecar even when restart = "always"',
    )
    _common(p_status)
    p_status.set_defaults(handler=_cmd_status)

    p_stop = sub.add_parser("stop", help="ask the recorded process to exit (SIGTERM, then SIGKILL)")
    p_stop.add_argument("name", nargs="?", default=None, help="the configured sidecar's name")
    p_stop.add_argument("--name", dest="name_flag", default=None, help="the same name, as a flag")
    p_stop.add_argument(
        "--grace-s",
        type=float,
        default=DEFAULT_STOP_GRACE_S,
        help=f"seconds to wait after SIGTERM before SIGKILL (default {DEFAULT_STOP_GRACE_S:g})",
    )
    _common(p_stop)
    p_stop.set_defaults(handler=_cmd_stop)

    return parser


# ---------------------------------------------------------------------------
# resolution helpers
# ---------------------------------------------------------------------------


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if getattr(args, "program_root", None):
        return Path(args.program_root)
    return find_program_root()


def _name(args: argparse.Namespace) -> str | None:
    """``sidecar start embed`` and ``sidecar start --name embed`` are the same
    command: the brief spells the flag, the acceptance run spells the
    positional, and an operator should not have to know which."""
    return getattr(args, "name_flag", None) or getattr(args, "name", None)


def _context(args: argparse.Namespace, action: str) -> tuple[Path | None, dict[str, Any], dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, {}, error_envelope(
            action,
            "no_program_root",
            "no --program-root given and no trialerror.toml found walking up from CWD",
        )
    cfg_path = program_root / CONFIG_FILENAME
    config: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            config = load_config(cfg_path).raw
        except ConfigError as exc:
            return None, {}, error_envelope(action, "bad_config", str(exc))
    return program_root, config, None


def _no_name_error(action: str, config: dict[str, Any]) -> dict:
    configured = sidecar_names(config)
    return error_envelope(
        action,
        "no_sidecar_named",
        f"name which sidecar (configured: {', '.join(configured) or 'none'})",
        next_actions=[next_action(["trialerror", "sidecar", "status"], "list what is configured")],
    )


def _status_next_actions(name: str) -> list:
    return [
        next_action(["trialerror", "sidecar", "status", name], "check it again"),
        next_action(["trialerror", "doctor", "--only", "sidecar_alive"], "the same answer as a doctor line"),
    ]


# ---------------------------------------------------------------------------
# the verbs
# ---------------------------------------------------------------------------


def _cmd_start(args: argparse.Namespace) -> dict:
    action = "sidecar.start"
    program_root, config, err = _context(args, action)
    if err is not None:
        return err
    name = _name(args)
    if not name:
        return _no_name_error(action, config)
    try:
        result = start_sidecar(program_root, name, config=config)
    except SidecarConfigError as exc:
        return error_envelope(action, "not_configured", str(exc))
    except SidecarStartBusy as exc:
        # Another start of the same sidecar holds the lock: nothing was
        # spawned, and saying so is the whole point of the lock.
        return error_envelope(
            action, "start_in_progress", str(exc),
            next_actions=[next_action(["trialerror", "sidecar", "status", name], "see what it did")],
        )
    except Exception as exc:  # noqa: BLE001 - a failed spawn is an envelope, not a traceback
        return error_envelope(action, "spawn_failed", f"{type(exc).__name__}: {exc}")
    warnings = []
    health = result.get("health") or {}
    if health.get("configured") and health.get("ok") is False:
        # Normal on a start: a model takes seconds to load, and the process is
        # up. Said out loud rather than reported as a failure.
        warnings.append(
            {
                "code": "health_not_yet_ok",
                "message": (
                    f"started, but {health.get('url')} has not answered 200 yet "
                    f"({health.get('error') or health.get('status')}) -- a model may still be loading; "
                    f"`trialerror sidecar status {name}` again in a moment"
                ),
            }
        )
    return ok_envelope(action, result=result, next_actions=_status_next_actions(name), warnings=warnings or None)


def _cmd_status(args: argparse.Namespace) -> dict:
    action = "sidecar.status"
    program_root, config, err = _context(args, action)
    if err is not None:
        return err
    name = _name(args)
    names = [name] if name else sidecar_names(config)
    if not names:
        return ok_envelope(
            action,
            result={"configured": [], "sidecars": {}},
            meta={"note": f"no [{SIDECARS_TABLE}] table in trialerror.toml"},
        )
    rows: dict[str, Any] = {}
    for one in names:
        try:
            rows[one] = sidecar_status(
                program_root, one, config=config, restart_if_dead=not args.no_restart
            )
        except SidecarConfigError as exc:
            rows[one] = {"name": one, "running": False, "state": "misconfigured", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one broken row must not break the report
            rows[one] = {
                "name": one, "running": False, "state": "unreadable",
                "error": f"{type(exc).__name__}: {exc}",
            }
    running = sorted(k for k, v in rows.items() if v.get("running"))
    return ok_envelope(
        action,
        result={
            "configured": sidecar_names(config),
            "running": running,
            "restarted": sorted(k for k, v in rows.items() if v.get("restarted")),
            "sidecars": rows,
        },
    )


def _cmd_stop(args: argparse.Namespace) -> dict:
    action = "sidecar.stop"
    program_root, config, err = _context(args, action)
    if err is not None:
        return err
    name = _name(args)
    if not name:
        return _no_name_error(action, config)
    try:
        result = stop_sidecar(program_root, name, config=config, grace_s=args.grace_s)
    except Exception as exc:  # noqa: BLE001 - report, never raise out of a stop
        return error_envelope(action, "stop_failed", f"{type(exc).__name__}: {exc}")
    return ok_envelope(
        action,
        result=result,
        next_actions=[next_action(["trialerror", "sidecar", "start", name], "start it again")],
    )
