"""DeepEval-pattern gate acceptance suites, ported OFFLINE as ordinary
pytest test cases (design Section 11: "hypothesis pipeline hardening ...
DeepEval DAG judges for gates as pytest suites"). Per this build's brief,
this ports the PATTERN documented in ``docs/mining/S6-eval-obs__deepeval.md``
-- specifically its #1 steal-pattern, "the pytest-plugin gating pattern
itself (trivial to imitate, no need to even depend on DeepEval)" -- not the
``deepeval`` library itself: no new dependency, no LLM judge calls inside
this module, no DAG-node graph-builder machinery (that report's #2 item);
``deepeval``'s own verdict actually recommends depending on the library
directly ("Depend on deepeval directly for assert_test + GEval + the dag
submodule"), which this build's OFFLINE / no-new-heavy-dependency
constraint overrides -- see this module's own TRIALERROR-DEV-NOTE below and the
build report for the full disclosed deviation.

**The core idea.** A "gate class" is a NAMED, REGISTERED :class:`GateSuite`
-- a small dict of ``check_name -> MetricFn``, each ``MetricFn`` a plain
Python callable ``subject -> MetricResult`` (no LLM, no I/O; a metric
function reads a pre-assembled ``subject`` dict the CALLER built from the
store -- the artifact/gate/verdict data under review -- exactly the same
"this module never calls a judge itself" boundary
:mod:`trialerror.verify.citecheck`/:mod:`trialerror.verify.hypothesis` hold, restated
here as "this module never scores anything with a judge, only with pure
functions over already-judged data"). :func:`run_gate_suite` turns a
suite's checks into REAL pytest test cases and runs them as a genuine
subprocess (module docstring of :mod:`trialerror.eval._gate_suite_runner`) --
so a gate suite's pass/fail is an ordinary pytest exit code, the DeepEval
mining note's own "CI-standard pass/fail semantics" bar, with zero new test
runner invented.

**Why a subprocess, not ``pytest.main()`` in-process:** a fresh Python
process per run sidesteps every ``sys.modules``-caching subtlety a
repeated in-process ``pytest.main()`` call over the SAME test file would
hit (parametrize decorators evaluated once at first import; pytest's
assertion-rewrite import hook warning "module already imported" on a
second collection of an already-cached module) -- this codebase's own
``trialerror.verify.reproduce`` reproduction runner already establishes the
"spawn ``[sys.executable, ...]`` explicitly, ``capture_output=True``,
``timeout=``, decode stderr with ``errors='replace'``" convention this
module's :func:`run_gate_suite` reuses verbatim, rather than inventing a
second subprocess convention.

**Results land on the gate row via the reproduction_ref pattern.**
:func:`run_gate_suite_for_gate` writes the suite's structured per-check
result onto ``gate.reproduction_ref`` (JSON) and ``gate.reproduction_status``
(``"match"``/``"mismatch"``) -- the EXACT two columns
``trialerror.verify.reproduce.reproduce_verdict`` already writes for a script
reproduction, via the same ``trialerror.stores.update(store, "gate", ...,
changes={...})`` call M10's own TRIALERROR-DEV-NOTE names as "the CONTRACT M9
inherits: whatever writes that column for real ... this module's
enforcement applies unchanged". A failing gate suite therefore BLOCKS
``apply_union`` through ``trialerror.artifacts.gates``'s existing
``reproduction_status == 'mismatch'`` entry-condition check -- zero new
enforcement code, zero edits to ``trialerror/artifacts/gates.py`` (out of this
build's lane; see this build's own scope note) -- reusing infrastructure
that already exists is the entire integration.

TRIALERROR-DEV-NOTE (CLI surface deviates from the literal brief): the brief
names the CLI verb ``trialerror gate eval <gate_id>``. This build's lane owns
``trialerror/verify/`` and ``trialerror/eval/`` (new) only -- ``trialerror/cli/gate.py``
is the CLI surface for ``trialerror/artifacts/gates.py``, a different
subsystem/lane this build does not touch. The shipped CLI verb is
``trialerror eval gate --gate-id <gate_id> --suite <suite_id> ...`` instead, in
a NEW ``trialerror/cli/eval.py`` (auto-discovered, zero shared-file edits,
same "adding a CLI group never touches trialerror/cli/__init__.py" convention
``trialerror/cli/verify.py`` already documents) -- the underlying mechanism
(:func:`run_gate_suite_for_gate`, the ``reproduction_ref`` write) is
unchanged from the brief; only which CLI file names the verb differs, to
stay inside this build's own pathspec-limited commit boundary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from trialerror.eval.errors import GateSuiteRunnerError, UnknownGateSuiteError
from trialerror.stores import get as store_get
from trialerror.stores import update as store_update
from trialerror.stores.store import Store
from trialerror.verify.verdicts import record_verdict

__all__ = [
    "MetricResult",
    "MetricFn",
    "GateSuite",
    "register_suite",
    "get_suite",
    "list_suites",
    "citation_coverage",
    "faithfulness_threshold",
    "reproduction_status_check",
    "fence_compliance",
    "consolidation_completeness",
    "DEFAULT_DISPOSITIONS",
    "CITATION_GROUNDED_SUITE_ID",
    "REVIEW_VERDICT_SUITE_ID",
    "run_gate_suite",
    "run_gate_suite_for_gate",
]

_RUNNER_PATH = Path(__file__).resolve().parent / "_gate_suite_runner.py"


@dataclass(frozen=True)
class MetricResult:
    """One metric function's verdict on one subject -- DeepEval's
    ``assert_test``-consumed metric-result shape, trimmed to what a gate
    suite needs: a name (for the per-check breakdown), a bool (what the
    generated pytest ``assert`` actually checks), an optional numeric score
    (for threshold-style metrics; ``None`` for a metric with no natural
    scalar, e.g. reproduction status), and a human-readable message (the
    assertion failure text -- design's own "surgical patching" bar: a
    failure names exactly what's wrong, never just a bare boolean)."""

    name: str
    passed: bool
    score: float | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "score": self.score, "message": self.message}


MetricFn = Callable[[Mapping[str, Any]], MetricResult]


@dataclass(frozen=True)
class GateSuite:
    """A named, registered gate acceptance suite: one :data:`MetricFn` per
    named check. ``checks`` order is not significant -- pytest cases are
    generated sorted by check name (module docstring of
    :mod:`trialerror.eval._gate_suite_runner`) for deterministic, reproducible
    output across runs."""

    suite_id: str
    checks: dict[str, MetricFn] = field(default_factory=dict)


_REGISTRY: dict[str, GateSuite] = {}


def register_suite(suite: GateSuite) -> GateSuite:
    """Register ``suite`` under its own ``suite_id``, overwriting any prior
    registration of the same id (module-reimport-safe: this module's own
    built-in suites at the bottom of this file call this exactly once each
    at import time, and a caller extending the registry with a custom suite
    is free to do the same)."""
    _REGISTRY[suite.suite_id] = suite
    return suite


def get_suite(suite_id: str) -> GateSuite:
    if suite_id not in _REGISTRY:
        raise UnknownGateSuiteError(f"no registered gate suite: {suite_id!r} (known: {sorted(_REGISTRY)})")
    return _REGISTRY[suite_id]


def list_suites() -> dict[str, list[str]]:
    """``suite_id -> sorted [check_name, ...]`` for every registered suite
    -- what ``trialerror eval list-suites`` prints."""
    return {suite_id: sorted(suite.checks) for suite_id, suite in _REGISTRY.items()}


# ---------------------------------------------------------------------------
# Metric functions -- the four named in this build's brief, plus the
# review-verdict worked example's own check.
# ---------------------------------------------------------------------------


def citation_coverage(subject: Mapping[str, Any], *, min_ratio: float = 1.0) -> MetricResult:
    """``subject["citecheck_summary"]`` -- the exact ``summary`` dict
    :func:`trialerror.verify.citecheck.run_citecheck` already returns
    (``mechanical_pass``/``llm_pass``/``total_pairs``, ...). Coverage =
    ``(mechanical_pass + llm_pass) / total_pairs`` -- the same ratio
    citecheck's own ``summary["overall"]`` is derived from, exposed here as
    a THRESHOLD a gate suite can require (``min_ratio`` need not be
    ``1.0``; a gate class may tolerate a small fraction of unresolved
    citations, unlike citecheck's own strict "any failure -> FAIL")."""
    summary = subject.get("citecheck_summary") or {}
    total = summary.get("total_pairs", 0)
    supported = summary.get("mechanical_pass", 0) + summary.get("llm_pass", 0)
    ratio = (supported / total) if total else 0.0
    passed = total > 0 and ratio >= min_ratio
    message = (
        f"{supported}/{total} citation pairs supported ({ratio:.0%}) >= threshold {min_ratio:.0%}"
        if total
        else "no citation pairs found in subject['citecheck_summary']"
    )
    if total and not passed:
        message = f"{supported}/{total} citation pairs supported ({ratio:.0%}) below threshold {min_ratio:.0%}"
    return MetricResult(name="citation_coverage", passed=passed, score=ratio if total else None, message=message)


def faithfulness_threshold(subject: Mapping[str, Any], *, min_score: float = 0.8) -> MetricResult:
    """``subject["faithfulness"]`` -- the ``{"score": ...}`` shape
    :func:`trialerror.verify.faithfulness.run_faithfulness` returns. A subject
    with no faithfulness data at all (``score is None``, e.g. the pipeline
    was never run, or found zero claims to check) fails closed -- a gate
    class that includes this check is asserting faithfulness WAS measured,
    not merely that it wasn't measured badly."""
    faithfulness = subject.get("faithfulness") or {}
    score = faithfulness.get("score")
    passed = score is not None and score >= min_score
    if score is None:
        message = "no faithfulness score in subject['faithfulness'] (faithfulness pipeline not run, or zero claims)"
    elif passed:
        message = f"faithfulness score {score:.4f} >= threshold {min_score}"
    else:
        message = f"faithfulness score {score:.4f} below threshold {min_score}"
    return MetricResult(name="faithfulness_threshold", passed=passed, score=score, message=message)


def reproduction_status_check(subject: Mapping[str, Any], *, disallowed: tuple[str, ...] = ("mismatch",)) -> MetricResult:
    """``subject["gate"]["reproduction_status"]`` must not be one of
    ``disallowed`` -- mirrors ``trialerror.artifacts.gates``'s own
    ``union_applied`` entry condition ("reproduction_status is not
    'mismatch'") as an independently-runnable pytest assertion, so a gate
    class can check this BEFORE ever attempting ``apply-union``, not only
    discover it there."""
    status = (subject.get("gate") or {}).get("reproduction_status")
    passed = status not in disallowed
    message = f"gate.reproduction_status = {status!r}" + ("" if passed else f" (disallowed: {list(disallowed)})")
    return MetricResult(name="reproduction_status", passed=passed, score=None, message=message)


_FENCE_EXCERPT_WORD_LIMIT = 20


def fence_compliance(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["fenced_chunks"]`` -- a list of ``{chunk_id,
    license_tier, fenced, excerpt_word_count}`` rows (the shape a caller
    assembles from ``search``/``get_chunk`` results actually served to this
    artifact). Every ``commercial_restricted`` chunk must carry
    ``fenced: true`` AND an excerpt at or under design's own R9/D-COC-1
    bar ("a <=20-word verbatim excerpt") -- an executable regression check
    for the license-fencing invariant :mod:`trialerror.retrieve.engine` already
    enforces at serve time, re-checked here at the ARTIFACT level (did the
    artifact actually respect what it was served, not just was it served
    correctly)."""
    offenders = []
    for chunk in subject.get("fenced_chunks", []):
        if chunk.get("license_tier") != "commercial_restricted":
            continue
        if not chunk.get("fenced"):
            offenders.append({"chunk_id": chunk.get("chunk_id"), "reason": "not fenced"})
        elif (chunk.get("excerpt_word_count") or 0) > _FENCE_EXCERPT_WORD_LIMIT:
            offenders.append(
                {"chunk_id": chunk.get("chunk_id"), "reason": f"excerpt {chunk.get('excerpt_word_count')} words > {_FENCE_EXCERPT_WORD_LIMIT}"}
            )
    passed = not offenders
    message = (
        "every commercial_restricted chunk is fenced and within the 20-word excerpt cap"
        if passed
        else f"{len(offenders)} restricted-license fence violation(s): {offenders}"
    )
    return MetricResult(name="fence_compliance", passed=passed, score=None, message=message)


#: The disposition vocabulary a finding's leading token must match --
#: exactly the four dispositions used throughout
#: ``docs/reviews/IMPL_REVIEW_VERDICT.md`` (its own "28 FIXED - 8 ACCEPTED -
#: 5 DEFERRED-v1" tally), generalized to drop the "-v1" version suffix so a
#: bare "DEFERRED" also matches.
DEFAULT_DISPOSITIONS: frozenset[str] = frozenset({"FIXED", "ACCEPTED", "DEFERRED", "REJECTED"})

_DISPOSITION_HEAD_RE = re.compile(r"^([A-Za-z]+)")


def _disposition_kind(disposition: str | None) -> str | None:
    """The leading alpha token of a disposition string, uppercased --
    ``"FIXED (tier3 09e68d2): ..."`` -> ``"FIXED"``, ``"DEFERRED-v1"`` ->
    ``"DEFERRED"``, ``"ACCEPTED (reasoned)"`` -> ``"ACCEPTED"`` (the three
    real forms ``IMPL_REVIEW_VERDICT.md`` actually uses). ``None`` for a
    blank/whitespace-only or non-alpha-leading string."""
    if not disposition:
        return None
    match = _DISPOSITION_HEAD_RE.match(disposition.strip())
    return match.group(1).upper() if match else None


def consolidation_completeness(
    subject: Mapping[str, Any], *, valid_dispositions: frozenset[str] = DEFAULT_DISPOSITIONS
) -> MetricResult:
    """The C-0066 consolidation-completeness law, made executable: "a
    consolidation is complete only when every finding has an explicit
    disposition row" (``docs/reviews/IMPL_REVIEW_VERDICT.md``'s own closing
    Regression-audit-note lesson, recorded there in prose AFTER the fact --
    this check enforces it BEFORE a review-verdict artifact can gate,
    rather than relying on a future consolidation remembering the lesson).

    ``subject["findings"]`` is a list of ``{finding, disposition}`` rows
    (or any mapping with a ``"finding"``/``"id"`` label key and a
    ``"disposition"`` text key) -- every row's disposition must be
    non-blank AND its leading token (:func:`_disposition_kind`) must be one
    of ``valid_dispositions``. A subject with ZERO findings fails closed
    (nothing to consolidate is not the same as a complete consolidation --
    an artifact of this gate class asserting "review complete" with an
    empty findings list is almost certainly a wiring bug, not a genuine
    zero-finding review)."""
    findings = subject.get("findings", [])
    offenders = [
        f.get("finding") or f.get("id") or "<unnamed finding>"
        for f in findings
        if _disposition_kind(f.get("disposition")) not in valid_dispositions
    ]
    passed = bool(findings) and not offenders
    if not findings:
        message = "subject['findings'] is empty -- nothing to consolidate"
    elif passed:
        message = f"all {len(findings)} finding(s) carry a disposition in {sorted(valid_dispositions)}"
    else:
        message = f"{len(offenders)} finding(s) missing/invalid disposition: {offenders}"
    return MetricResult(name="consolidation_completeness", passed=passed, score=None, message=message)


# ---------------------------------------------------------------------------
# Built-in suites.
# ---------------------------------------------------------------------------

#: A generic "citation-grounded artifact" gate class, composing all four
#: metric functions this build's brief names.
CITATION_GROUNDED_SUITE_ID = "citation-grounded"
register_suite(
    GateSuite(
        suite_id=CITATION_GROUNDED_SUITE_ID,
        checks={
            "citation_coverage": citation_coverage,
            "faithfulness_threshold": faithfulness_threshold,
            "reproduction_status": reproduction_status_check,
            "fence_compliance": fence_compliance,
        },
    )
)

#: The worked example: the review-verdict gate class (design brief:
#: "checks: every finding has a disposition row -- the C-0066
#: consolidation-completeness law as an EXECUTABLE check").
REVIEW_VERDICT_SUITE_ID = "review-verdict"
register_suite(
    GateSuite(
        suite_id=REVIEW_VERDICT_SUITE_ID,
        checks={
            "consolidation_completeness": consolidation_completeness,
            "reproduction_status": reproduction_status_check,
        },
    )
)


# ---------------------------------------------------------------------------
# The pytest-subprocess runner.
# ---------------------------------------------------------------------------


def run_gate_suite(suite_id: str, subject: Mapping[str, Any], *, cwd: str | Path | None = None, timeout: float = 60.0) -> dict[str, Any]:
    """Run every registered check of ``suite_id`` against ``subject`` as a
    real pytest subprocess (module docstring). Validates ``suite_id``
    up front, in THIS process, with a typed :class:`~trialerror.eval.errors.
    UnknownGateSuiteError` -- never spawning a subprocess for a suite id
    that can't possibly resolve (same "fail fast, typed, before the
    expensive step" posture ``trialerror.verify.verdicts.record_verdict``
    documents for its own subject_kind/procedure checks).

    Returns ``{"suite_id", "returncode", "overall": "PASS"|"FAIL",
    "checks": [MetricResult.to_dict(), ...], "stdout_tail"}``. Raises
    :class:`~trialerror.eval.errors.GateSuiteRunnerError` if the subprocess
    itself couldn't run (failed to launch, timed out, or exited with a
    pytest code outside ``{0, 1}`` -- 0/1 are pytest's own "ran to
    completion, all-passed/some-failed" codes; anything else means
    collection blew up or the run was interrupted, an infrastructure
    failure distinct from a metric function failing its own assertion)."""
    get_suite(suite_id)  # typed refusal before ever touching the filesystem/subprocess

    with tempfile.TemporaryDirectory(prefix="trialerror-gate-suite-") as tmp_dir:
        subject_path = Path(tmp_dir) / "subject.json"
        results_path = Path(tmp_dir) / "results.json"
        subject_path.write_text(json.dumps(dict(subject), ensure_ascii=False), encoding="utf-8")

        env = dict(os.environ)
        env["TRIALERROR_GATE_SUITE_ID"] = suite_id
        env["TRIALERROR_GATE_SUITE_SUBJECT_PATH"] = str(subject_path)
        env["TRIALERROR_GATE_SUITE_RESULTS_PATH"] = str(results_path)
        argv = [sys.executable, "-m", "pytest", str(_RUNNER_PATH), "-p", "no:cacheprovider", "-q"]

        try:
            proc = subprocess.run(
                argv, cwd=str(cwd) if cwd else None, capture_output=True, timeout=timeout, env=env, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateSuiteRunnerError(f"gate suite {suite_id!r} runner failed to execute: {type(exc).__name__}: {exc}") from exc

        if proc.returncode not in (0, 1):
            stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-2000:]
            raise GateSuiteRunnerError(
                f"gate suite {suite_id!r} runner exited {proc.returncode} (not a clean pytest pass/fail); stderr: {stderr_tail}"
            )

        checks = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []
        return {
            "suite_id": suite_id,
            "returncode": proc.returncode,
            "overall": "PASS" if proc.returncode == 0 else "FAIL",
            "checks": checks,
            "stdout_tail": (proc.stdout or b"").decode("utf-8", errors="replace")[-2000:],
        }


def run_gate_suite_for_gate(
    store: Store,
    *,
    gate_id: str,
    suite_id: str,
    subject: Mapping[str, Any],
    issued_by_launch: str,
    timeout: float = 60.0,
    procedure_version: str = "1",
) -> dict[str, Any]:
    """:func:`run_gate_suite`, then write the result onto ``gate_id`` via
    the ``reproduction_ref`` pattern (module docstring) AND record a
    ``knowledge.verdict`` row (``procedure="gate"`` -- the one enum value
    the schema names for gate-related verdicts and no landed code had yet
    claimed; ``subject_kind="artifact"``, ``subject_id=gate.artifact_id``
    -- "over the artifact under review", this build's brief, verbatim).

    Raises :class:`~trialerror.eval.errors.GateSuiteRunnerError` (via
    :func:`run_gate_suite`) if the gate id doesn't resolve or the suite
    itself failed to run at all."""
    gate = store_get(store, "gate", pk_column="gate_id", pk_value=gate_id)
    if gate is None:
        raise GateSuiteRunnerError(f"no such gate: {gate_id!r}")

    run_result = run_gate_suite(suite_id, subject, timeout=timeout)
    reproduction_status = "match" if run_result["overall"] == "PASS" else "mismatch"
    reproduction_ref = json.dumps(
        {"kind": "gate_suite", "suite_id": suite_id, "returncode": run_result["returncode"], "checks": run_result["checks"]},
        ensure_ascii=False,
    )
    store_update(
        store, "gate", pk_column="gate_id", pk_value=gate_id,
        changes={"reproduction_status": reproduction_status, "reproduction_ref": reproduction_ref},
    )

    verdict_row = record_verdict(
        store, subject_kind="artifact", subject_id=gate["artifact_id"], procedure="gate",
        procedure_version=procedure_version, label=run_result["overall"],
        evidence=[{"note": f"{c['name']}: {c['message']}", "stance": "PASS" if c["passed"] else "FAIL"} for c in run_result["checks"]],
        reproduction_ref=reproduction_ref, issued_by_launch=issued_by_launch,
    )

    return {**run_result, "gate_id": gate_id, "reproduction_status": reproduction_status, "verdict": verdict_row}
