"""``trialerror resolve`` — design Section 2: "any id can be looked up in
words." ``trialerror resolve ID [ID...] [--json]`` prints one short
paragraph per id (through the standard :mod:`trialerror.util.envelope`
shape every group uses: each id's paragraph is the ``result`` entry a
plain ``--format text`` run shows; ``--json`` on this group swaps the
paragraph for the full structured :class:`~trialerror.resolve.Description`,
for a caller that wants the fields rather than the sentence).

Never exposed to seats (design Section 6 trap 1): this group opens the
same read-only, unscoped :class:`~trialerror.dashboard.store_ro.RoStore`
the dashboard uses, and is not registered on any MCP server.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from trialerror.dashboard.store_ro import open_store_ro
from trialerror.resolve import describe
from trialerror.resolve.base import describe_line
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, ok_envelope

GROUP_NAME = "resolve"
HELP = "Resolve one or more ids to what they are, in plain words (never exposed to seats)."

_PROGRAM_ROOT_HELP = "override the program root (default: discover trialerror.toml upward from CWD)"


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    parser.add_argument("--program-root", default=argparse.SUPPRESS, help=_PROGRAM_ROOT_HELP)
    parser.add_argument("ids", nargs="+", metavar="ID")
    parser.add_argument("--json", action="store_true", help="the full structured description, not the paragraph")
    parser.set_defaults(handler=_run_resolve)
    return parser


def _run_resolve(args: argparse.Namespace) -> dict:
    root = Path(args.program_root) if getattr(args, "program_root", None) else find_program_root()
    if root is None:
        return error_envelope(
            "resolve", "program_root_not_found", "no trialerror.toml found upward from CWD; pass --program-root"
        )
    ro = open_store_ro(root)
    try:
        results = []
        for id_ in args.ids:
            desc = describe(id_, ro)
            results.append(dataclasses.asdict(desc) if args.json else {"id": id_, "paragraph": describe_line(desc)})
    finally:
        ro.close()
    return ok_envelope("resolve", result=results)
