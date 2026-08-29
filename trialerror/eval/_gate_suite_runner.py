"""The real pytest module :func:`trialerror.eval.gate_suites.run_gate_suite`
spawns (``[sys.executable, "-m", "pytest", str(_RUNNER_PATH), ...]``, a
genuine subprocess -- see that function's own docstring for why a
subprocess, not an in-process ``pytest.main()`` call, was the deliberate
choice here). One test function, parameterized at COLLECTION time over
whichever gate suite's check names the environment names -- this file's own
source text never changes between runs; only the environment variables a
fresh subprocess reads do, so there is no code-injection surface (a gate
suite's ``subject`` payload travels as a JSON file path, never interpolated
into Python source) and no cross-run staleness (a subprocess is a fresh
Python interpreter every time -- no ``sys.modules`` caching concern the
way a repeated in-process ``pytest.main()`` call would have to guard
against).

Never collected by the project's own ``tests/`` suite: this file's name
matches neither ``test_*.py`` nor ``*_test.py`` (pytest's default
discovery globs), and ``pyproject.toml``'s ``testpaths = ["tests"]``
doesn't reach ``trialerror/eval/`` at all -- it is only ever collected by
:func:`~trialerror.eval.gate_suites.run_gate_suite` naming this exact file path
explicitly on the command line.
"""

from __future__ import annotations

import json
import os

import pytest

from trialerror.eval.gate_suites import get_suite

_SUITE = get_suite(os.environ["TRIALERROR_GATE_SUITE_ID"])
with open(os.environ["TRIALERROR_GATE_SUITE_SUBJECT_PATH"], "r", encoding="utf-8") as _f:
    _SUBJECT = json.load(_f)
_RESULTS_PATH = os.environ.get("TRIALERROR_GATE_SUITE_RESULTS_PATH")
_RESULTS: list[dict] = []


def pytest_generate_tests(metafunc):
    if "check_name" in metafunc.fixturenames:
        metafunc.parametrize("check_name", sorted(_SUITE.checks))


def test_gate_suite_check(check_name):
    """One test case per registered metric function on the suite named by
    ``TRIALERROR_GATE_SUITE_ID`` -- a metric's own ``MetricResult.passed``
    becomes this test's pass/fail via a plain ``assert``, which is the
    whole DeepEval-pattern trick (``docs/mining/S6-eval-obs__deepeval.md``:
    "assert_test() inside a normal pytest function ... normal pytest exit
    codes work for CI gating exactly as they would for any other pytest
    suite"). Writes the accumulated results list back out to
    ``TRIALERROR_GATE_SUITE_RESULTS_PATH`` after EVERY case (not just once at
    session end via a ``pytest_sessionfinish`` hook -- session-scoped hooks
    defined in a plain test module rather than a ``conftest.py``/registered
    plugin are not reliably invoked by pytest's plugin manager, confirmed
    by a genuine empty-results run during this build; writing incrementally
    from inside the test function itself, which unquestionably always
    runs, sidesteps that hook-registration question entirely) -- pytest's
    own subprocess exit code is only PASS/FAIL, never the structured
    per-check breakdown a gate row needs to carry forward (the
    ``reproduction_ref`` pattern -- see
    :func:`~trialerror.eval.gate_suites.run_gate_suite_for_gate`)."""
    metric_fn = _SUITE.checks[check_name]
    result = metric_fn(_SUBJECT)
    _RESULTS.append(result.to_dict())
    if _RESULTS_PATH:
        with open(_RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(_RESULTS, f, ensure_ascii=False)
    assert result.passed, f"{check_name}: {result.message}"
