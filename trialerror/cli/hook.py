"""``trialerror hook`` -- the Claude Code hook entry points, as console-script
subcommands rather than loose ``python path/to/script.py`` invocations.

Why this group exists (portability, and specifically Linux): ``hooks.json``
used to name an *interpreter* (``python "${CLAUDE_PLUGIN_ROOT}/hooks/
spawn_gate.py"``). Bare ``python`` does not exist on a stock Linux install
-- only ``python3`` does -- so all four hooks failed with exit 127, which
for the spawn gate meant budget-at-spawn silently became a no-op instead of
refusing (design Section 1 commitment 1: "Enforcement over convention").
And even where ``python`` resolved it was not necessarily the interpreter
with ``trialerror`` importable. Naming the *program* instead of an
interpreter fixes both at once: pip guarantees the ``trialerror`` console
script is on ``PATH`` in the same environment as the package, on Windows
and POSIX alike. See :mod:`trialerror.hooks` for the full account.

Exit-code protocol -- the reason this group does NOT emit an AgentEnvelope
=========================================================================
Every other CLI group returns an envelope dict that
:func:`trialerror.cli.main` serializes to stdout and maps to exit 0/1. Hooks
cannot use that machinery, for two independent reasons:

- Claude Code's hook protocol reads the *exit code* as the verdict: 0 lets
  the tool call proceed, 2 BLOCKS it and surfaces stderr to the agent as
  the refusal reason. The envelope path can only ever produce 0 or 1, so a
  refusal would be indistinguishable from success.
- stdout is part of the protocol, not a place for diagnostics. SessionStart
  parses stdout as ``{"hookSpecificOutput": {...}}`` and folds it into the
  session's context; an envelope printed there would corrupt it.

So each handler below runs the hook and raises ``SystemExit(code)``
directly. :func:`trialerror.cli.main` calls ``handler(args)`` without
catching exceptions, so the ``SystemExit`` propagates out through the
console script and becomes the process's real exit code, with nothing
extra written to stdout. That is deliberate, and it is why these handlers
break the "return an envelope" contract every other group follows.

Registration rule (design Section 5.2): this module is auto-discovered by
:func:`trialerror.cli.discover_groups`; adding it does not touch
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse

from trialerror.hooks import post_task, session_start, spawn_gate, stop_check

GROUP_NAME = "hook"
HELP = (
    "Claude Code hook entry points (session-start, spawn-gate, post-task, stop-check). "
    "Reads the hook payload as JSON on stdin and communicates via exit code, not an envelope -- "
    "wired by plugin/hooks/hooks.json, not normally run by hand."
)

#: subcommand name -> the module implementing it. The CLI spelling is
#: kebab-case (matching every other group's verbs); the module name stays
#: snake_case (matching the historical ``plugin/hooks/<name>.py`` files and
#: the ``payload.hook`` marker values ``record_hook_alive_once`` writes).
_HOOKS = {
    "session-start": session_start,
    "spawn-gate": spawn_gate,
    "post-task": post_task,
    "stop-check": stop_check,
}


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    for name, module in _HOOKS.items():
        sub = actions.add_parser(
            name,
            help=f"run the {name} hook (payload on stdin; exit code is the verdict)",
        )
        sub.set_defaults(handler=_make_handler(module))

    parser.set_defaults(handler=_run_no_action)
    return parser


def _make_handler(module):
    def _handler(_args: argparse.Namespace) -> dict:
        # Raises rather than returns: see this module's docstring on why the
        # envelope path cannot carry a hook verdict.
        raise SystemExit(module.main())

    return _handler


def _run_no_action(_args: argparse.Namespace) -> dict:
    from trialerror.util.envelope import error_envelope, next_action

    return error_envelope(
        "hook",
        "no_action",
        "specify a hook: " + ", ".join(_HOOKS),
        next_actions=[next_action(["trialerror", "hook", "--help"], "list hook entry points")],
    )
