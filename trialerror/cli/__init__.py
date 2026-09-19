"""``trialerror`` CLI entry point + command-group auto-discovery.

Design Section 5.2: "each CLI group lives in its own module
``trialerror/cli/<group>.py``, auto-discovered at load — no implementation lane
ever edits a shared ``cli/__init__.py``."

This file is that shared file. It is written once (M0) and never edited
again to ADD A GROUP: :func:`discover_groups` scans the ``trialerror.cli``
package directory at call time and imports every module that exposes a
``register(subparsers)`` callable. M1..M15 add groups purely by dropping a
new ``trialerror/cli/<group>.py`` file — this module's source never changes as a
result of a new group landing (see ``tests/test_cli_group_autodiscovery.py``
for a fixture proving exactly that; that invariant is still exactly true
after the TRIALERROR-DEV-NOTE below).

TRIALERROR-DEV-NOTE (C-0064 fix-tier2-cli, FX-12): this file gained the two
lines registering GLOBAL ``--program-root``/``--platform-root`` arguments
on the top-level parser below — a deliberate, one-time, reviewed exception
to "never edited again", authorized by IMPL_REVIEW_VERDICT.md's ranked fix
list (FX-12: 19 CLI groups placed these two flags 3 mutually-conflicting
ways — see ``docs/OPERATOR_GUIDE.md``'s former "placement is not
consistent" table). This is NOT a new group and NOT per-group business
logic; it is the one shared cross-cutting argument pair every group already
duplicated its own copy of. The "no lane edits it to register a group"
invariant is unchanged — a future module still adds a group purely by
dropping a file here, same as always. Every group's own
``--program-root``/``--platform-root`` declaration stays in place
(``default=argparse.SUPPRESS`` now, not ``default=None``) so a value given
AFTER the group/verb — every existing group's historical convention, and
every existing test's usage — still wins; the global flag above only
supplies the value when the group/verb-level one wasn't given. All three
placements (before the group, between the group and verb, after the verb)
now resolve to the same ``args.program_root``/``args.platform_root``.

TRIALERROR-DEV-NOTE (lane FB-1, item F12): this file also gained
:func:`_force_utf8_stdio`, called once at the top of :func:`main` — the
second deliberate, reviewed exception to "never edited again", on the same
grounds as FX-12 above: ``main`` is the single choke point every group's
envelope reaches ``emit()`` through, so a process-wide stdio decision has
exactly one place it can live without nineteen groups each keeping a copy.
It is NOT a new group and NOT per-group business logic, and the "no lane
edits this file to register a group" invariant is unchanged.

Group module contract::

    GROUP_NAME = "widget"          # the subcommand name: `trialerror widget ...`
    HELP = "..."                   # shown in `trialerror --help`

    def register(subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(GROUP_NAME, help=HELP)
        parser.add_argument(...)
        parser.set_defaults(handler=run)   # run(args) -> envelope dict
        return parser
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from types import ModuleType

from trialerror import __version__
from trialerror.util.envelope import PROTOCOL_VERSION, emit, error_envelope, next_action, ok_envelope

__all__ = ["discover_groups", "build_parser", "main", "_force_utf8_stdio"]


def discover_groups() -> list[ModuleType]:
    """Import every ``trialerror.cli.<name>`` module that exposes ``register``.

    Skips private modules (leading underscore) and nested packages. Sorted
    by ``GROUP_NAME`` (falling back to the module name) for stable
    ``--help`` output and deterministic tests.
    """
    import trialerror.cli as pkg

    groups: list[ModuleType] = []
    for _finder, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if ispkg or modname.startswith("_"):
            continue
        mod = importlib.import_module(f"trialerror.cli.{modname}")
        if callable(getattr(mod, "register", None)):
            groups.append(mod)
    groups.sort(key=lambda m: getattr(m, "GROUP_NAME", m.__name__))
    return groups


def build_parser(groups: list[ModuleType] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trialerror",
        description="TrialError: research-operations exoskeleton CLI. "
        "Every command emits an AgentEnvelope (JSON by default).",
    )
    parser.add_argument(
        "--version", action="store_true", help="print the package/protocol version and exit"
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="output format (default: json, the agent-facing AgentEnvelope)",
    )
    # FX-12: global, pre-subcommand form (`trialerror --program-root X <group> <verb>`).
    # Every group that resolves a program/platform root also still declares
    # its own copy of these two flags (now `default=argparse.SUPPRESS`) so a
    # value given after the group or verb keeps winning — see this module's
    # TRIALERROR-DEV-NOTE above for the full mechanics.
    parser.add_argument(
        "--program-root",
        default=None,
        help="override the program root for every group that resolves one (default: each "
        "group's own discovery, usually walking up from CWD for trialerror.toml). Also accepted "
        "after the group name or the verb, matching each group's pre-FX-12 convention.",
    )
    parser.add_argument(
        "--platform-root",
        default=None,
        help="override the platform root for every group that resolves one (default: the "
        "TRIALERROR_PLATFORM_ROOT env var, or ~/.trialerror). Also accepted after the group name or "
        "the verb, matching each group's pre-FX-12 convention.",
    )
    subparsers = parser.add_subparsers(dest="group", metavar="<group>")
    for mod in groups if groups is not None else discover_groups():
        mod.register(subparsers)
    return parser


def _version_envelope() -> dict:
    return ok_envelope(
        "version",
        result={"package": "trialerror", "version": __version__, "protocolVersion": PROTOCOL_VERSION},
        next_actions=[next_action(["trialerror", "doctor"], "run the health-check suite")],
    )


def _force_utf8_stdio() -> None:
    """Re-encode this process's stdout/stderr as UTF-8 before anything is
    written to them (lane FB-1 item F12).

    Every envelope this CLI emits is JSON with ``ensure_ascii=False``
    (:func:`trialerror.util.envelope.to_json_line`), so a title, an author
    name or a refusal message can carry any character the corpus does. The
    host console's encoding is not the harness's choice: a Windows console
    (or any process whose ``PYTHONIOENCODING`` names a legacy codepage)
    gives Python a cp1252/cp437 stdout, and ``print`` of a line that
    codepage cannot represent raises ``UnicodeEncodeError`` from inside
    ``print`` itself. The write is then discarded WHOLE -- a text stream
    encodes the string it was handed before touching the buffer, so the
    caller sees NO line at all, not a truncated one -- and the CLI exits 1
    with a traceback for a command that had in fact already succeeded.

    ``errors="replace"`` rather than ``"strict"``: a console that genuinely
    cannot render a glyph should show ``?`` and still hand the agent a
    parseable line. The guard is the shape
    ``trialerror/obs/statusline_capture.py`` already uses -- not every host
    stream is a ``TextIOWrapper`` (a captured ``StringIO`` under pytest has
    no ``.reconfigure``), and a stream that cannot be reconfigured must
    leave the CLI working exactly as before.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - a stream without .reconfigure is not an error
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(args_list)
    fmt = args.format

    if args.version:
        emit(_version_envelope(), fmt)
        return 0

    group = getattr(args, "group", None)
    if not group:
        parser.print_help()
        return 0

    handler = getattr(args, "handler", None)
    if handler is None:
        env = error_envelope(
            group, "no_handler", f"command group {group!r} registered no handler"
        )
        emit(env, fmt)
        return 2

    env = handler(args)
    emit(env, fmt)
    return 0 if env.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover - exercised via console_script instead
    raise SystemExit(main())
