"""Not a test module (pytest only collects ``test_*.py``) -- test-only job
handlers, registered via the explicit ``--handler-module`` CLI flag /
``extra_handler_modules`` worker-spawn param rather than
``trialerror.jobs.registry``'s auto-discovery (test scaffolding stays out of
``trialerror/`` on purpose). Both in-process tests (importing this module
directly) and the real-subprocess kill-mid-job test (passing
``tests._job_handlers`` as an ``--handler-module``, with ``PYTHONPATH`` set
so the fresh child interpreter can import it) share these handlers, so a
resumability bug can't hide behind two different test-only implementations.
"""

from __future__ import annotations

import time

from trialerror.jobs.registry import register_handler
from trialerror.jobs.worker import EnvironmentalFailure


@register_handler("test_step_counter")
def step_counter(ctx) -> None:
    """Deliberately slow, checkpointed multi-step handler used by the
    kill-mid-job resume acceptance test: sleeps ``step_delay_s`` before
    each step and durably records progress via ``set_checkpoint`` after
    every step, resuming from ``checkpoint['completed_steps']`` rather than
    always starting at 0 -- the resumability contract under test."""
    payload = ctx.payload
    total_steps = int(payload["total_steps"])
    step_delay_s = float(payload.get("step_delay_s", 0.1))
    start = int(ctx.checkpoint.get("completed_steps", 0))
    for step in range(start, total_steps):
        time.sleep(step_delay_s)
        ctx.set_checkpoint({"completed_steps": step + 1})


@register_handler("test_always_fails")
def always_fails(ctx) -> None:
    """Unconditionally raises -- a logic failure every time it runs, for
    exercising the attempts/backoff/abandon path deterministically."""
    raise RuntimeError(ctx.payload.get("message", "deliberate test failure"))


@register_handler("test_environmental_failure")
def environmental_failure(ctx) -> None:
    """Unconditionally raises :class:`EnvironmentalFailure` -- for
    exercising the "does not consume an attempt" path deterministically."""
    raise EnvironmentalFailure(
        ctx.payload.get("reason", "deliberate test environmental failure"),
        retry_delay_s=ctx.payload.get("retry_delay_s"),
    )


@register_handler("test_heartbeat_loop")
def heartbeat_loop(ctx) -> None:
    """Calls ``ctx.heartbeat()`` in a loop -- used to prove that an
    operator-issued ``trialerror jobs pause`` mid-run surfaces as
    ``JobPausedError`` at the handler's very next heartbeat (propagated by
    ``run_one`` as ``status: "paused"``, no exception escaping to the
    caller)."""
    iterations = int(ctx.payload.get("iterations", 20))
    step_delay_s = float(ctx.payload.get("step_delay_s", 0.05))
    for _ in range(iterations):
        time.sleep(step_delay_s)
        ctx.heartbeat()


@register_handler("test_pauses_itself")
def pauses_itself(ctx) -> None:
    """Deterministic exercise of ``run_one``'s ``JobPausedError`` ->
    ``status: "paused"`` translation, with no real operator/subprocess
    needed: the handler pauses its OWN job mid-run (via the ledger
    directly, simulating what ``trialerror jobs pause`` would do from another
    process) and then calls ``ctx.heartbeat()`` again, which now observes
    the paused state and raises."""
    from trialerror.jobs import ledger

    ctx.set_checkpoint({"phase": "before_pause"})
    ledger.pause(ctx.store, ctx.job_id)
    ctx.heartbeat()  # raises JobPausedError -- must propagate out of the handler uncaught
    ctx.set_checkpoint({"phase": "unreachable"})
