"""``trialerror.eval`` exceptions. Mirrors ``trialerror.verify.errors``'s split: a
caller that only cares "did the gate-suite operation fail" catches
:class:`EvalError`; a caller that needs to branch on *why* catches the
specific subclass.
"""

from __future__ import annotations

__all__ = ["EvalError", "UnknownGateSuiteError", "GateSuiteRunnerError"]


class EvalError(Exception):
    """Base class for every error :mod:`trialerror.eval` raises."""


class UnknownGateSuiteError(EvalError):
    """``trialerror.eval.gate_suites.get_suite`` was asked for a ``suite_id``
    that was never registered via :func:`~trialerror.eval.gate_suites.register_suite`."""


class GateSuiteRunnerError(EvalError):
    """The pytest-subprocess runner (:func:`~trialerror.eval.gate_suites.run_gate_suite`)
    failed to execute at all (couldn't launch, timed out, or exited with a
    pytest code outside ``{0, 1}`` -- "the suite ran to completion, some
    checks may have failed" -- meaning infrastructure broke, not that a
    metric function failed its threshold; a per-check failure is reported
    in the run result, never raised)."""
