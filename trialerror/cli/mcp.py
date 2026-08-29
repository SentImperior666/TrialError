"""``trialerror mcp`` — MCP server entry points. Design Section 12 (M14 row)'s own
worked example for the ``trialerror-ops`` server's command line: "Server entry per
the design (e.g. ``trialerror mcp ops``)." Design Section 12 (M8 row) / build
brief: "Server entry: ``trialerror mcp knowledge`` ... follow the design." Thin
CLI wrapper: this module only parses argv and resolves the program root; all
server logic lives in ``trialerror.mcp.ops``/``trialerror.mcp.knowledge``/
``trialerror.mcp.protocol``.

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by either M8 or M14.

TRIALERROR-DEV-NOTE (build-M8, shared-file note): this file is the ONE path both
M8 (``knowledge``) and M14 (``ops``) need a subcommand under, per the
design's own "``trialerror mcp <server>``" framing — a genuine concurrent-lane
overlap on a single file rather than a naming accident. The ``knowledge``
subparser/handler below is an additive edit (M14's ``ops`` wiring is
untouched); flagged for the integration session in case a later commit from
the other lane races this one.

``trialerror mcp ops``/``trialerror mcp knowledge`` both BLOCK for the server's
lifetime (each serves stdio until the client closes stdin, per the MCP
stdio shutdown sequence — see ``trialerror/mcp/protocol.py``). Each is meant to
be launched by an MCP-aware client (Claude Code's own MCP server
registration, ``.mcp.json`` or ``settings.json`` — wiring that registration
is an integration-session task, same class of item as M3/M6/M8's own
"live-CC ... integration item" notes), not invoked interactively for its
envelope output.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "mcp"
HELP = (
    "MCP servers: `trialerror mcp ops` starts the trialerror-ops stdio server (design Section 5.1, 12 tools); "
    "`trialerror mcp knowledge` starts the trialerror-knowledge stdio server (design Section 5.1, 11 tools)."
)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS on
    # every one of these three declarations so an unset value never
    # overwrites the global --program-root/--platform-root the top-level
    # parser resolved.
    parser.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)"
    )
    servers = parser.add_subparsers(dest="server", metavar="<server>")

    p_ops = servers.add_parser("ops", help="start the trialerror-ops stdio MCP server (blocks; serves until stdin closes)")
    p_ops.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)"
    )
    p_ops.add_argument(
        "--platform-root", default=argparse.SUPPRESS, help="override the platform root (default: TRIALERROR_PLATFORM_ROOT or ~/.trialerror)"
    )
    p_ops.set_defaults(handler=_run_ops)

    p_knowledge = servers.add_parser(
        "knowledge", help="start the trialerror-knowledge stdio MCP server (read-only; blocks; serves until stdin closes)"
    )
    p_knowledge.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)"
    )
    p_knowledge.add_argument(
        "--platform-root", default=argparse.SUPPRESS, help="override the platform root (default: TRIALERROR_PLATFORM_ROOT or ~/.trialerror)"
    )
    p_knowledge.set_defaults(handler=_run_knowledge)

    parser.set_defaults(handler=_run_no_server)
    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if args.program_root:
        return Path(args.program_root)
    return find_program_root()


def _run_no_server(args: argparse.Namespace) -> dict:
    return error_envelope(
        "mcp",
        "no_server",
        "specify a server: ops|knowledge",
        next_actions=[
            next_action(["trialerror", "mcp", "ops"], "start the trialerror-ops stdio server"),
            next_action(["trialerror", "mcp", "knowledge"], "start the trialerror-knowledge stdio server"),
        ],
    )


def _run_ops(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "mcp ops",
            "program_root_not_found",
            "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )

    from trialerror.mcp.ops import run_server  # deferred: keep `trialerror --help`/argv parsing free of the store import chain

    platform_root = Path(args.platform_root) if args.platform_root else None
    run_server(program_root=program_root, platform_root=platform_root)
    return ok_envelope("mcp ops", result={"program_root": str(program_root), "stopped": True})


def _run_knowledge(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "mcp knowledge",
            "program_root_not_found",
            "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )

    from trialerror.mcp.knowledge import run_server  # deferred: keep `trialerror --help`/argv parsing free of the store import chain

    platform_root = Path(args.platform_root) if args.platform_root else None
    run_server(program_root=program_root, platform_root=platform_root)
    return ok_envelope("mcp knowledge", result={"program_root": str(program_root), "stopped": True})
