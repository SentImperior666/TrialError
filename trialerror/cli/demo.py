"""``trialerror demo`` -- scaffold and populate a sample program, so the dashboard
has something to show.

The gap this closes: ``trialerror program init`` gives you a correct but empty
program, and an empty program makes the dashboard look broken rather than
new. Every panel renders its zero state, so a first-time operator cannot tell
which surfaces are unimplemented, which are waiting on data, and which are
the actual product. Answering "what does this thing do?" required running a
real research program for a week.

``trialerror demo seed --dir ./demo-program`` does it in a few seconds.

Registration rule (design Section 5.2): auto-discovered by
:func:`trialerror.cli.discover_groups`; adding it does not touch
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.demo import SeedRefused, seed_demo_program
from trialerror.demo.seed import default_platform_root
from trialerror.stores.errors import StoreError
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "demo"
HELP = (
    "Scaffold a sample program populated with a small research narrative -- "
    "corpus, budget, jobs, gates, rooms, feed, and course criteria -- so "
    "`trialerror dashboard serve` has something to render."
)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_seed = actions.add_parser(
        "seed",
        help="create a demo program and fill it with sample data (safe to throw away)",
    )
    p_seed.add_argument(
        "--dir", dest="dir_", default=None,
        help="where to create the demo program (default: ./demo-program under CWD)",
    )
    p_seed.add_argument(
        "--program-id", default="demo",
        help="the program id written to trialerror.toml (default: demo)",
    )
    p_seed.add_argument(
        "--platform-root", default=None,
        help="override the platform root. Defaults to <program>/.platform, NOT the real "
        "~/.trialerror -- accounts and budget pools live in the platform store, and a demo "
        "has no business writing them into your actual ledger. Pass this only if you want "
        "the demo to share a real platform root.",
    )
    p_seed.add_argument(
        "--force", action="store_true",
        help="reseed a program that already has data (destructive: deletes its stores/ "
        "directory, and its .platform/ when the demo owns that too)",
    )
    p_seed.set_defaults(handler=_run_seed)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "demo", "no_action", "specify an action: seed",
        next_actions=[next_action(["trialerror", "demo", "--help"], "list demo actions")],
    )


def _run_seed(args: argparse.Namespace) -> dict:
    program_root = Path(args.dir_).resolve() if args.dir_ else (Path.cwd() / "demo-program").resolve()
    # Defaulted here, not left to open_store, so `program init` below creates
    # the demo's platform.db in the same place the seed will write to.
    platform_root = (
        Path(args.platform_root).resolve() if args.platform_root
        else default_platform_root(program_root)
    )

    if program_root.is_file():
        return error_envelope(
            "demo seed", "dir_is_a_file",
            f"{program_root} already exists and is a file, not a directory",
        )

    # Scaffold first if needed. `program init` owns the trialerror.toml
    # template and the directory layout; re-deriving either here would mean
    # two definitions of what a program is.
    if not (program_root / "trialerror.toml").exists():
        from trialerror.cli.program import _run_init

        init_args = argparse.Namespace(
            name=args.program_id,
            dir_=str(program_root),
            platform_root=str(platform_root),
        )
        init_env = _run_init(init_args)
        if not init_env.get("ok"):
            return init_env

    try:
        result = seed_demo_program(
            program_root,
            platform_root=platform_root,
            program_id=args.program_id,
            force=args.force,
        )
    except SeedRefused as exc:
        return error_envelope(
            "demo seed", "already_seeded", str(exc),
            next_actions=[
                next_action(
                    ["trialerror", "demo", "seed", "--dir", str(program_root), "--force"],
                    "recreate this program's stores and seed it again (destructive)",
                )
            ],
        )
    except StoreError as exc:
        return error_envelope("demo seed", "seed_failed", str(exc),
                              details={"program_root": str(program_root)})

    # Every suggested command carries --platform-root as well as
    # --program-root. The demo's account and budget pools live in its OWN
    # platform store (see trialerror.demo.seed.DEMO_PLATFORM_DIRNAME); omit
    # the flag and the reader gets the real ~/.trialerror instead, which
    # silently renders THEIR accounts and an empty pool list on the demo's
    # dashboard -- the panel looks broken and nothing says why.
    roots = [
        "--program-root", str(result.program_root),
        "--platform-root", str(platform_root),
    ]

    return ok_envelope(
        "demo seed",
        result={
            "program_root": str(result.program_root),
            "platform_root": str(platform_root),
            "program_id": result.program_id,
            "account_id": result.account_id,
            "open_session_id": result.open_session_id,
            "closed_session_id": result.closed_session_id,
            "seeded": result.counts,
            "notes": result.notes,
        },
        next_actions=[
            next_action(
                ["trialerror", "dashboard", "serve", *roots],
                "open the dashboard on the demo program -- this is the point of the command",
            ),
            next_action(
                ["trialerror", "query", "search", "interleaved practice retention", *roots],
                "search the seeded corpus",
            ),
            next_action(
                ["trialerror", "doctor", *roots],
                "run the check sweep against a program that actually has data",
            ),
        ],
    )
