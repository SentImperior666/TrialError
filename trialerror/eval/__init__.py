"""``trialerror.eval`` -- DeepEval-pattern gate acceptance suites, ported OFFLINE
as ordinary pytest test cases (design Section 11: "hypothesis pipeline
hardening ... DeepEval DAG judges for gates as pytest suites"). New package,
this build (v2-hypharden lane): ``trialerror/verify/`` + ``trialerror/eval/`` are this
lane's owned surface.

- :mod:`trialerror.eval.errors` -- this package's exception hierarchy.
- :mod:`trialerror.eval.gate_suites` -- :class:`~trialerror.eval.gate_suites.GateSuite`
  registry, the four named metric functions (citation-coverage,
  faithfulness-threshold, reproduction-status, fence-compliance), the
  worked ``review-verdict`` gate class (the C-0066 consolidation-
  completeness law as an executable check), and the pytest-subprocess
  runner (:func:`~trialerror.eval.gate_suites.run_gate_suite`/
  :func:`~trialerror.eval.gate_suites.run_gate_suite_for_gate`).
- :mod:`trialerror.eval._gate_suite_runner` -- the static pytest module the
  runner spawns as a subprocess; not part of this package's public API
  (never imported directly by a caller -- see its own module docstring).
"""

from __future__ import annotations

from trialerror.eval.errors import EvalError, GateSuiteRunnerError, UnknownGateSuiteError

__all__ = ["EvalError", "UnknownGateSuiteError", "GateSuiteRunnerError"]
