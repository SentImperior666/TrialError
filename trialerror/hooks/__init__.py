"""Claude Code hook implementations, as importable package modules.

These four modules used to live only as loose scripts under
``plugin/hooks/*.py``, invoked by ``plugin/hooks/hooks.json`` as
``python "${CLAUDE_PLUGIN_ROOT}/hooks/<name>.py"``. That wiring carried two
portability faults, both of which bit on Linux:

1. Bare ``python`` is not on ``PATH`` on a stock Debian/Ubuntu/Fedora box
   (only ``python3`` is), so every hook died with exit 127 before running.
   For :mod:`~trialerror.hooks.spawn_gate` in particular that is worse than
   it sounds: the gate is documented to fail CLOSED, but a hook whose
   interpreter does not exist never runs at all, so budget-at-spawn
   silently degraded to a no-op instead of refusing.
2. Even where ``python`` resolved, it was not necessarily the interpreter
   that has ``trialerror`` importable -- the scripts do a bare
   ``import trialerror.*`` with no ``sys.path`` bootstrap.

Both faults have the same root cause: naming an interpreter instead of
naming the program. The fix is to route hooks through the ``trialerror``
console script (see :mod:`trialerror.cli.hook`), which pip puts on ``PATH``
in the same environment as the package, on every platform. ``hooks.json``
now says ``trialerror hook spawn-gate``; there is no interpreter to guess.

The original ``plugin/hooks/*.py`` scripts remain as thin shims that import
and delegate here, so anything invoking them by path -- including this
repo's own subprocess tests -- keeps working unchanged.

Each module exposes the same two callables it always did:

``_evaluate(payload)``
    The pure decision logic, callable directly from a test with a crafted
    payload dict, no stdin or subprocess involved.
``main()``
    Reads the hook payload as JSON on stdin, writes any diagnostic to
    stderr (and, for SessionStart, its ``additionalContext`` JSON to
    stdout), and returns the process exit code Claude Code's hook protocol
    expects: 0 to proceed, 2 to block.
"""

from __future__ import annotations

__all__ = ["post_task", "session_start", "spawn_gate", "stop_check", "SUBAGENT_TOOL_NAMES"]

# Claude Code 2.1.x renamed the subagent-spawning tool from "Task" to
# "Agent" (found live on the sandbox host 2026-09-05, 03:34Z: a
# `claude -p ... --plugin-dir .../plugin` session on Claude Code 2.1.261
# spawned a subagent with NO spawn_gate refusal and NO post_task
# accounting -- the transcript showed `TOOL_USE: Agent {...}` while
# ``plugin/hooks/hooks.json``'s PreToolUse/PostToolUse matchers, and this
# package's own tool_name comparisons, still only recognized "Task").
# Both :mod:`trialerror.hooks.spawn_gate` and :mod:`trialerror.hooks.post_task`
# gate on this tuple (instead of a bare ``== "Task"``) so the booking law
# and its accounting keep firing under both the old and the new tool name.
SUBAGENT_TOOL_NAMES = ("Task", "Agent")
