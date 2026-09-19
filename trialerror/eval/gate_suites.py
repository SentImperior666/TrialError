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

**Three registered suites.** ``citation-grounded`` and ``review-verdict``
are this module's originals; ``aiif_round`` is the ideation framework's own
gate class, fourteen checks over one round's artifacts (see that section's
own header for the subject shape it reads, and for the one posture every
check there shares: absent data fails closed). It is a pure-function suite
like the others -- it reads an assembled ``subject`` dict and never opens a
store.

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

from trialerror.budget.policy import meets_minimum
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
    "AIIF_ROUND_SUITE_ID",
    "AIIF_MODEL_FLOORS",
    "IDEA_DISPOSITIONS",
    "BUNDLE_LABEL_KEYS",
    "SIGNIFICANCE_TERMS",
    "MIN_LENS_CELLS_PER_CARD",
    "prereg_present",
    "models_table_present",
    "novelty_bundle_complete",
    "self_assessment_absent",
    "plants_caught",
    "distribution_card_present",
    "control_arm_present",
    "arm_mode_declared",
    "card_cells_ge_2",
    "admission_order_hash_matches",
    "lens_log_reconciled",
    "per_arm_n_disclosed",
    "no_significance_language",
    "consolidation_completeness_over_ideas",
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
# The framework round suite (AIIF). Design §6's own `eval/gate_suites.py`
# row names thirteen checks plus `consolidation_completeness` reused; this
# section is those fourteen and the suite that composes them.
#
# One posture, stated once and applied fourteen times: EVERY check FAILS
# CLOSED on absent data. A round artifact that carries no prereg section, no
# models table, no distribution card is not a round that is exempt from
# those bars -- it is a round that has not met them, and the same reasoning
# `faithfulness_threshold` already states for itself ("a gate class that
# includes this check is asserting faithfulness WAS measured") applies to
# every bar a pre-registered round commits to.
#
# The SUBJECT is assembled by the caller from the round's own artifacts and
# stores (the suite never reads a store -- module docstring). Its sections,
# each named by the check that reads it:
#
#   prereg            {prereg_id, status, params{...}}      prereg_present,
#                                                           admission_order_hash_matches
#   models_table      {purpose: class}                      models_table_present
#   ideas             [{idea_id, status, dossier{...}}]      novelty_bundle_complete,
#                                                           consolidation_completeness
#   reference_snapshot {R1..R5 each with sha256/snapshot_id} novelty_bundle_complete
#   judge_envelopes   [{subject_id, record{statement,...}}]  self_assessment_absent
#   plants            score_plants()'s own result            plants_caught
#   distribution      run_mechanical_screen()'s own card     distribution_card_present
#   roster            [{lens_name, seat, recipe_cards[]}]    control_arm_present,
#                                                           card_cells_ge_2
#   assignment        {arm_mode, ...}                        arm_mode_declared
#   admission_order   {hash, ...}                            admission_order_hash_matches
#   lens_log          {offenders[]} | [rows]                 lens_log_reconciled
#   outcomes          [{cell, arm?, n, ...}]                 per_arm_n_disclosed
#   report_text       the round's own prose                  no_significance_language
# ---------------------------------------------------------------------------

#: The model floors a framework round requires, purpose by purpose -- the
#: same eight the program scaffold ships (`trialerror/cli/program.py`'s
#: commented `[models]` example). `screen` and `consolidation` are `mid` and
#: not `small`: no small-class model makes a research judgment, and the
#: screen decides which ideas a judge ever sees.
AIIF_MODEL_FLOORS: dict[str, str] = {
    "keystone": "top",
    "ideation": "top",
    "moderation": "top",
    "room_participant": "top",
    "novelty_judge": "top",
    "gates": "top",
    "consolidation": "mid",
    "screen": "mid",
}

#: A prereg that has been VOIDED is not a prereg a round may gate on (its
#: escrow failed its own tamper check); `committed` and `revealed` both are.
PREREG_USABLE_STATUSES: frozenset[str] = frozenset({"committed", "revealed"})

#: What an idea's disposition may be for the consolidation law to count it
#: as dispositioned -- the `idea.status` vocabulary minus `raw`, which IS
#: the undispositioned state.
IDEA_DISPOSITIONS: frozenset[str] = frozenset({"CONSOLIDATED", "MERGED", "ELIMINATED", "PROMOTED"})

#: The two dossier label keys every consolidated idea's novelty BUNDLE must
#: carry. Two, not three: the literature vocabulary is ONE judgment made
#: against the corpus and the external index together, so there is no third
#: label to require (the screen's own refutation of that finding).
#:
#: Named for the bundle rather than for the dossier, because
#: `trialerror.rooms.api` exports its own dossier-label whitelist for a
#: different contract -- the keys a room ENVELOPE may carry, which include
#: `judged`. Two exported names with one spelling and two contents invite
#: importing the wrong one.
BUNDLE_LABEL_KEYS: tuple[str, ...] = ("label_inventory", "label_corpus")

#: The reference sets a round's novelty bundle is measured against, each of
#: which must be snapshot-identified on the round so a verdict can say what
#: it was compared to AS OF when it was compared.
REFERENCE_SET_KEYS: tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5")

#: The seat that makes the matched-budget comparison possible. It is a
#: MEASUREMENT seat: it never counts toward the arm mix or the far floor
#: (charter AMENDMENT-4 item 1 / design AMENDMENT-5 item 1), which is why
#: `control_arm_present` checks for it by seat and says so in its message
#: rather than looking for it among the arms.
CONTROL_SEAT = "control"
STANDARD_SEAT = "standard"
BUSTER_SEAT = "assumption_buster"

#: `card_cells_ge_2`'s bar, and the reason for it: a card held by one lens
#: cannot be told apart from that lens's vantage, slice and seed, so no
#: decision rule may act on its cell (design §4.3's own "decision rules act
#: only on >=2-lens cells").
MIN_LENS_CELLS_PER_CARD = 2

#: The card that comes with the assumption-buster's seat, held by one lens
#: by design -- excluded from the cell bar for exactly that reason.
BUSTER_ONLY_CARD = "NEGATE"

#: Significance vocabulary a directional, single-digit-n round may not use
#: (design §4.3: "All directional, per-cell n disclosed, no significance
#: language"). Matched case-insensitively on word boundaries.
SIGNIFICANCE_TERMS: tuple[str, ...] = (
    "statistically significant",
    "statistical significance",
    "significantly",
    "significant",
    "significance",
    "p-value",
    "p value",
    "confidence interval",
    "null hypothesis",
)

_SIGNIFICANCE_RE = re.compile(
    r"(?<![\w-])(?:" + "|".join(t.replace(" ", r"\s+").replace("-", r"[-\s]") for t in SIGNIFICANCE_TERMS) + r")(?![\w-])",
    re.IGNORECASE,
)

#: `p < .05`, `p<0.05`, `p = 0.03` -- the form that carries significance
#: language without using the word.
_P_VALUE_RE = re.compile(r"(?<![\w])p\s*[<>=]\s*\.?\d", re.IGNORECASE)


def _result(name: str, passed: bool, message: str, score: float | None = None) -> MetricResult:
    return MetricResult(name=name, passed=passed, score=score, message=message)


def prereg_present(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["prereg"]`` must name a ``prereg_id`` whose status is one of
    :data:`PREREG_USABLE_STATUSES`. A framework round commits its procedure
    and parameters BLIND before any spawn; a gate with no prereg id is a
    round whose procedure can still be described after the fact, which is
    the one thing pre-registration exists to prevent."""
    prereg = subject.get("prereg") or {}
    prereg_id = prereg.get("prereg_id")
    status = prereg.get("status")
    if not prereg_id:
        return _result("prereg_present", False, "subject['prereg'] names no prereg_id (nothing was escrowed before the round ran)")
    if status not in PREREG_USABLE_STATUSES:
        return _result(
            "prereg_present", False,
            f"prereg {prereg_id} has status {status!r}; a round may gate only on {sorted(PREREG_USABLE_STATUSES)} "
            "(a voided escrow failed its own tamper check)",
        )
    return _result("prereg_present", True, f"prereg {prereg_id} is {status}")


def models_table_present(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["models_table"]`` must name every purpose in
    :data:`AIIF_MODEL_FLOORS` at or above its floor, resolved through
    ``trialerror.budget.policy.meets_minimum`` so the gate and the booking
    path cannot disagree about what "at or above" means.

    An absent purpose is NOT a pass: ``meets_minimum(class, None)`` returns
    True precisely because an unconfigured purpose has no floor, which is
    the state this check exists to refuse — a round that booked its lenses
    against no policy at all."""
    table = subject.get("models_table") or {}
    if not table:
        return _result(
            "models_table_present", False,
            "subject['models_table'] is empty -- no [models] purpose table, so no booking in this round had a "
            "floor to meet (meets_minimum(class, None) passes everything)",
        )
    missing = sorted(p for p in AIIF_MODEL_FLOORS if p not in table)
    below = sorted(
        f"{p}={table[p]!r}<{floor}" for p, floor in AIIF_MODEL_FLOORS.items()
        if p in table and not meets_minimum(str(table[p]), floor)
    )
    if missing or below:
        return _result(
            "models_table_present", False,
            f"[models] is incomplete or below the framework floors -- missing: {missing or 'nothing'}; "
            f"below floor: {below or 'nothing'}",
        )
    return _result("models_table_present", True, f"all {len(AIIF_MODEL_FLOORS)} purposes carry their floor or better")


def novelty_bundle_complete(subject: Mapping[str, Any]) -> MetricResult:
    """Every consolidated idea carries a dossier with both label keys, and
    the round carries a snapshot id for every reference set
    (:data:`REFERENCE_SET_KEYS`).

    The labels may be the mechanical pair (``no-close-neighbour`` with
    ``judged: false``) — that is a complete bundle for an unjudged idea, and
    requiring a judged label for all of them would require judging all of
    them, which the design explicitly does not. What is refused is a
    consolidated idea with NO bundle, and a bundle that cannot say which
    snapshot of the reference sets it was measured against."""
    ideas = [i for i in subject.get("ideas", []) if str(i.get("status", "")).lower() == "consolidated"]
    snapshot = subject.get("reference_snapshot") or {}
    if not ideas:
        return _result("novelty_bundle_complete", False, "subject['ideas'] holds no consolidated idea -- nothing reached a room")
    missing_snapshot = [
        key for key in REFERENCE_SET_KEYS
        if not (snapshot.get(key) or {}).get("sha256") and not (snapshot.get(key) or {}).get("snapshot_id")
    ]
    without: list[str] = []
    for idea in ideas:
        dossier = idea.get("dossier") or {}
        if not dossier or any(key not in dossier for key in BUNDLE_LABEL_KEYS):
            without.append(str(idea.get("idea_id") or "<unnamed idea>"))
    if without or missing_snapshot:
        return _result(
            "novelty_bundle_complete", False,
            f"{len(without)} consolidated idea(s) carry no novelty dossier with both label keys "
            f"({without[:10]}); reference sets with no snapshot id: {missing_snapshot or 'none'}",
            score=round((len(ideas) - len(without)) / len(ideas), 6),
        )
    return _result(
        "novelty_bundle_complete", True,
        f"all {len(ideas)} consolidated idea(s) carry a dossier against snapshot-identified R1-R5",
        score=1.0,
    )


def self_assessment_absent(subject: Mapping[str, Any]) -> MetricResult:
    """No judge envelope in ``subject["judge_envelopes"]`` carries a sentence
    in which a record grades its own novelty — re-run here with
    ``trialerror.lens.novelty.strip_self_assessment``, the same stripper the
    screen applies, so the gate measures the rule rather than a restatement
    of it.

    The honest caveat travels with the check: the stripper is hygiene and is
    trivially paraphrased ("no register row states this procedure" matches
    nothing). A pass means the overt form is absent, not that nothing in the
    envelope is addressed to the judge."""
    envelopes = subject.get("judge_envelopes") or []
    if not envelopes:
        return _result(
            "self_assessment_absent", False,
            "subject['judge_envelopes'] is empty -- the judged half's envelopes are what this check reads, and "
            "a round that ran a judged screen has them",
        )
    from trialerror.lens.novelty import strip_self_assessment

    offenders: list[dict[str, Any]] = []
    for envelope in envelopes:
        record = envelope.get("record") or {}
        text = " ".join(str(record.get(f) or "") for f in ("requirements", "statement", "probe"))
        stripped = strip_self_assessment(text)
        if stripped["removed"]:
            offenders.append({"subject_id": envelope.get("subject_id"), "removed": stripped["removed"][:3]})
    if offenders:
        return _result(
            "self_assessment_absent", False,
            f"{len(offenders)} judge envelope(s) still carry self-assessment sentences: {offenders[:5]} "
            "(the stripper runs on judge envelopes only; the record and the feed post keep their full text)",
        )
    return _result("self_assessment_absent", True, f"none of {len(envelopes)} judge envelope(s) carries an overt self-assessment sentence")


def plants_caught(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["plants"]`` is ``score_plants``'s own result. Every
    INVENTORY plant must have been caught and the batch must have been
    auditable at all: a judge handed an existing row verbatim and calling it
    new cannot be trusted on the rows it was not handed, and a batch with no
    inventory plants has not been audited however clean it looks.

    A missed PARAPHRASE plant is reported in the message and does not fail —
    paraphrase detection is the harder task and the design does not stake
    the batch on it."""
    plants = subject.get("plants") or {}
    if not plants:
        return _result("plants_caught", False, "subject['plants'] is empty -- no seeded-plant battery rode in the judged batch")
    if plants.get("unauditable") or not (plants.get("n_inventory_plants") or plants.get("failures") is not None):
        return _result("plants_caught", False, "the judged batch carried no inventory plants -- unauditable, which fails on that ground alone")
    # `failures` is the set the batch actually turns on -- the misses on the
    # kinds the round declared (lane FB-5 item 3, `--batch-fail-on`, default
    # `inventory`). A score written before that key existed carries only
    # `inventory_failures`, which under the default rule IS the same list, so
    # the fallback reads an older subject correctly rather than passing it
    # vacuously.
    failures = list(
        plants["failures"] if plants.get("failures") is not None else (plants.get("inventory_failures") or [])
    )
    reported_misses = [m for m in plants.get("missed") or [] if m.get("plant_id") not in failures]
    catch_rate = plants.get("catch_rate")
    fail_on = list(plants.get("batch_fail_on") or ["inventory"])
    n_failable = plants.get("n_inventory_plants")
    if plants.get("by_kind"):
        n_failable = sum(block["n"] for kind, block in plants["by_kind"].items() if kind in fail_on)
    if failures:
        return _result(
            "plants_caught", False,
            f"{len(failures)} {'/'.join(fail_on)} plant(s) missed or unlabelled: {failures} -- the batch is "
            f"reopened with caveat and nothing it judged is consolidated (catch rate {catch_rate})",
            score=catch_rate,
        )
    return _result(
        "plants_caught", True,
        f"every one of {n_failable} {'/'.join(fail_on)} plant(s) was caught "
        f"(catch rate {catch_rate}; {len(reported_misses)} plant(s) of other kinds missed, reported not failed)",
        score=catch_rate,
    )


def distribution_card_present(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["distribution"]`` must carry the round's audit card: a
    declared-operation entropy per axis, the pairwise-similarity
    distribution, and the template mass.

    A value of ``None`` is acceptable and is NOT a hole — an entropy over
    one record, or a pairwise median over a single-idea batch, genuinely has
    no value, and the card says so. What is refused is the KEY being absent:
    a card that never computed the measure cannot be read as a card that
    computed it and found nothing."""
    distribution = subject.get("distribution") or {}
    if not distribution:
        return _result("distribution_card_present", False, "subject['distribution'] is empty -- the round published no audit card")
    holes: list[str] = []
    declared = distribution.get("declared_operations") or {}
    for axis in ("opportunity", "method"):
        axis_card = declared.get(axis)
        if not isinstance(axis_card, Mapping) or "entropy" not in axis_card:
            holes.append(f"declared_operations.{axis}.entropy")
    pairwise = distribution.get("pairwise_similarity")
    if not isinstance(pairwise, Mapping) or "median" not in pairwise:
        holes.append("pairwise_similarity.median")
    template = distribution.get("template_mass")
    if not isinstance(template, Mapping) or "template_share" not in template:
        holes.append("template_mass.template_share")
    if holes:
        return _result("distribution_card_present", False, f"the audit card is missing {holes}")
    return _result(
        "distribution_card_present", True,
        "the audit card carries entropy per axis, the pairwise-similarity distribution and the template mass",
    )


def control_arm_present(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["roster"]`` must seat exactly one ``control`` lens, and that
    lens must hold no recipe card.

    **The CONTROL seat never counts toward the arm mix or the far floor**
    (charter AMENDMENT-4 item 1, design AMENDMENT-5 item 1). This check
    therefore looks for it by SEAT and says so in its own message: a reader
    who went looking for the control among the arms would be looking in the
    place the amendment removed it from. A control lens holding a card is
    not a control at all, and the round it sits in reports a comparison it
    did not run."""
    roster = subject.get("roster") or []
    if not roster:
        return _result("control_arm_present", False, "subject['roster'] is empty -- no seats to check")
    controls = [r for r in roster if str(r.get("seat")) == CONTROL_SEAT]
    if not controls:
        return _result(
            "control_arm_present", False,
            "the roster seats no control lens -- the matched-budget comparison (AIIF vs CONTROL on O1) has no "
            "control arm, and the adoption rule rests on it. The control seat sits on the modal arm and is "
            "excluded from the arm mix and the far floor, so it is counted here by seat, not among the arms",
        )
    if len(controls) > 1:
        return _result(
            "control_arm_present", False,
            f"{len(controls)} control seats are rostered; the comparison is against ONE matched-budget control",
        )
    carded = [r.get("lens_name") for r in controls if r.get("recipe_cards")]
    if carded:
        return _result(
            "control_arm_present", False,
            f"the control lens {carded} holds recipe cards -- a control with a card is not a control",
        )
    return _result(
        "control_arm_present", True,
        f"one control lens ({controls[0].get('lens_name')}) is seated with no card, excluded from the arm mix "
        "and from the far floor",
    )


def arm_mode_declared(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["assignment"]["arm_mode"]`` must be declared, and the
    prereg's own params must declare the same value.

    Either assignment mode may be run — the interim per-slice mix is a
    documented, declarable option — but it has to be declared in the
    escrow, because the mode decides what per-arm n MEANS (lenses per arm
    under ``per_lens``, slices per arm under ``per_slice``), and a round that
    reports O4 without saying which is reporting two different numbers under
    one name."""
    assignment = subject.get("assignment") or {}
    mode = assignment.get("arm_mode")
    declared = ((subject.get("prereg") or {}).get("params") or {}).get("arm_mode")
    if not mode:
        return _result("arm_mode_declared", False, "subject['assignment'] declares no arm_mode -- per-arm n has no defined unit")
    if not declared:
        return _result(
            "arm_mode_declared", False,
            f"the round ran arm_mode={mode!r} but its prereg params declare none -- the mode decides what "
            "per-arm n means and is escrowed, not chosen afterwards",
        )
    if str(declared) != str(mode):
        return _result(
            "arm_mode_declared", False,
            f"the round ran arm_mode={mode!r} but pre-registered {declared!r}",
        )
    return _result("arm_mode_declared", True, f"arm_mode={mode!r}, matching the pre-registered params")


def card_cells_ge_2(subject: Mapping[str, Any]) -> MetricResult:
    """Every recipe card in play is held by at least
    :data:`MIN_LENS_CELLS_PER_CARD` STANDARD lenses.

    A card held by one lens cannot be told apart from that lens's vantage,
    slice and seed, so O3 has nothing to say about it and no reweighting rule
    may act on it. Standard seats only: the buster's :data:`BUSTER_ONLY_CARD`
    comes with its seat and is held by one lens by design, and a control
    holds none — counting either would fail a correctly assembled round."""
    roster = subject.get("roster") or []
    if not roster:
        return _result("card_cells_ge_2", False, "subject['roster'] is empty -- no card cells to count")
    holders: dict[str, int] = {}
    for row in roster:
        if str(row.get("seat")) != STANDARD_SEAT:
            continue
        for card in row.get("recipe_cards") or []:
            if str(card) == BUSTER_ONLY_CARD:
                continue
            holders[str(card)] = holders.get(str(card), 0) + 1
    if not holders:
        return _result(
            "card_cells_ge_2", False,
            "no standard lens holds a recipe card -- a framework round's standard seats write under cards, and "
            "O3 (card x survival) has nothing to compare without them",
        )
    thin = sorted(f"{card}={n}" for card, n in holders.items() if n < MIN_LENS_CELLS_PER_CARD)
    if thin:
        return _result(
            "card_cells_ge_2", False,
            f"card cells below {MIN_LENS_CELLS_PER_CARD} standard lenses: {thin} -- a one-lens cell cannot be "
            "told apart from that lens, and no decision rule may act on it",
            score=round(min(holders.values()) / MIN_LENS_CELLS_PER_CARD, 6),
        )
    return _result(
        "card_cells_ge_2", True,
        f"every card in play is held by >= {MIN_LENS_CELLS_PER_CARD} standard lenses ({holders})",
    )


def admission_order_hash_matches(subject: Mapping[str, Any]) -> MetricResult:
    """The admission order the round RAN must hash to the order it ESCROWED.

    This is the one check standing behind "dossier labels never order
    admission": the order comes out of a seeded draw, is escrowed, and is
    then compared — so re-ordering the rooms after seeing the screen's
    results changes a hash that was written down first.

    **Two escrow places, because the order cannot exist at Phase 0.** The
    pool it is drawn over is the round's ``consolidated`` ideas, and nothing
    is consolidated until the judged screen has run (Phase 3b). A round that
    put the hash in its Phase-0 prereg params either invented it or
    back-filled the prereg, and back-filling is the one thing the procedure
    forbids. So the Phase-0 escrow carries the RULE and the SEED
    (``room_admission``, ``admission_seed``) and a SECOND named escrow,
    committed after Phase 3b and before the first room opens, carries the
    order itself. This check reads either — ``prereg.params
    .admission_order_hash`` for a round that had its order at Phase 0 (a
    re-run over a closed pool), or ``admission_escrow`` for the ordinary
    case — and SAYS WHICH ONE it read, because "matched its escrow" means
    different things about when the order was fixed.

    An ``admission_escrow`` carrying a status outside
    :data:`PREREG_USABLE_STATUSES` is not an escrow a round may gate on,
    the same way a voided prereg is not."""
    ran = (subject.get("admission_order") or {}).get("hash")
    params = (subject.get("prereg") or {}).get("params") or {}
    escrow = subject.get("admission_escrow") or {}
    if not ran:
        return _result("admission_order_hash_matches", False, "subject['admission_order'] carries no hash -- the order the round ran is unrecorded")
    if params.get("admission_order_hash"):
        escrowed = params["admission_order_hash"]
        source = "the prereg params (admission_order_hash)"
    elif escrow.get("admission_order_hash") or escrow.get("hash"):
        escrowed = escrow.get("admission_order_hash") or escrow.get("hash")
        status = str(escrow.get("status") or "committed")
        if status not in PREREG_USABLE_STATUSES:
            return _result(
                "admission_order_hash_matches", False,
                f"subject['admission_escrow'] is {status!r} -- an escrow outside "
                f"{sorted(PREREG_USABLE_STATUSES)} is not one a round may gate on, the same way a voided prereg "
                "is not",
            )
        source = (
            f"the post-screen admission escrow ({escrow.get('prereg_id') or 'unnamed'}, {status})"
        )
    else:
        return _result(
            "admission_order_hash_matches", False,
            "no escrow carries an admission_order_hash -- neither the prereg params nor an "
            "'admission_escrow' section. The order is drawn over the round's consolidated pool, so it "
            "is escrowed after the judged screen and before the first room opens; an order nothing was escrowed "
            "against can be re-derived once the screen's results are in",
        )
    if str(ran) != str(escrowed):
        return _result(
            "admission_order_hash_matches", False,
            f"the admission order run ({str(ran)[:12]}...) is not the order escrowed in {source} "
            f"({str(escrowed)[:12]}...) -- the rooms were ordered by something other than the escrowed seeded draw",
        )
    return _result(
        "admission_order_hash_matches", True,
        f"the admission order matches the hash escrowed in {source} ({str(ran)[:12]}...)",
    )


def lens_log_reconciled(subject: Mapping[str, Any]) -> MetricResult:
    """Every booked lens posted: ``subject["lens_log"]`` carries no offender.

    Accepts ``trialerror lens log``'s own shape -- ``rows`` (one per lens,
    each carrying ``posted``), ``n_lenses`` and ``offenders`` -- or a bare
    list of those rows. A lens that was assigned a slice and never posted is
    a dropped launch — budget spent, arm unrepresented — not a quiet skip,
    and a round that gates with one unreconciled is reporting an arm mix it
    did not run.

    **A subject carrying neither ``rows`` nor ``offenders`` FAILS** with
    "log shape unrecognised". The old envelope (``assignments`` + ``count``)
    is exactly that shape: the check counted its rows, found no offender and
    returned a vacuous PASS for a round where one lens of three had posted.
    A check that cannot see the state it exists to check reports that it
    cannot, never that all is well.

    **A log that reconciles NO lens FAILS too** (fix pass B-2), in the
    mapping branch as well as the list branch. ``lens log`` returns
    ``rows: []``/``n_lenses: 0`` for a round id nothing was assigned under —
    a mistyped ``--round-id``, or a gate run before ``lens assign`` — and
    "every booked lens posted" said of no lens at all is the same vacuous
    PASS in a different shape."""
    log = subject.get("lens_log")
    if log is None:
        return _result("lens_log_reconciled", False, "subject['lens_log'] is absent -- nothing reconciled the round's booked lenses against its posts")
    if isinstance(log, Mapping):
        rows = log.get("rows")
        declared = log.get("offenders")
        if rows is None and declared is None:
            # The shape `lens log` used to return -- `assignments` and a
            # count -- carries no per-lens posting state at all. Counting its
            # rows and finding no offender is not a reconciliation; it is the
            # check answering a question the subject never contained, and it
            # PASSED a round in which one lens of three had posted.
            return _result(
                "lens_log_reconciled", False,
                f"log shape unrecognised: subject['lens_log'] carries neither 'rows' nor 'offenders' "
                f"(keys: {sorted(log)[:8]}) -- run `trialerror lens log --round-id <round>` and pass its "
                "result, which reports posted per lens",
            )
        offenders = [
            str(o.get("lens_name") or o.get("roster_id") or o) if isinstance(o, Mapping) else str(o)
            for o in declared or []
        ]
        for row in rows or []:
            if isinstance(row, Mapping) and not row.get("posted"):
                name = str(row.get("lens_name") or row.get("roster_id") or row)
                if name not in offenders:
                    offenders.append(name)
        total = log.get("n_lenses")
        if total is None:
            total = log.get("count")
        if total is None and rows is not None:
            total = len(rows)
        # Fix pass B-2: a mapping whose population is empty (or never
        # reported) reconciles nothing. The list branch has always failed
        # `[]`; the mapping branch passed the same emptiness with "count not
        # reported", which is what `lens log` returns for a round id no
        # assignment row names.
        empty_population = not offenders and not total
    else:
        rows = list(log)
        if not rows:
            return _result("lens_log_reconciled", False, "subject['lens_log'] is empty -- a round has booked lenses to reconcile")
        offenders = [str(r.get("lens_name") or r.get("roster_id") or r) for r in rows if not r.get("posted")]
        total = len(rows)
        empty_population = False
    if empty_population:
        return _result(
            "lens_log_reconciled", False,
            f"the log reconciles no lens at all (rows: {len(rows or [])}, n_lenses: {total!r}) -- this is what "
            "`trialerror lens log` returns for a round id no assignment row names (a mistyped --round-id, or a "
            "gate run before `lens assign`). 'Every booked lens posted' said of no lens is not a reconciliation",
        )
    if offenders:
        return _result(
            "lens_log_reconciled", False,
            f"{len(offenders)} booked lens(es) never posted: {offenders[:10]} -- a dropped launch, not a skip",
        )
    return _result("lens_log_reconciled", True, f"every booked lens posted ({total if total is not None else 'count not reported'})")


def per_arm_n_disclosed(subject: Mapping[str, Any]) -> MetricResult:
    """Every outcome cell in ``subject["outcomes"]`` discloses its own ``n``,
    and the CONTROL cell is reported as a control rather than as an arm.

    Per-cell n is single digits for rounds of six to fourteen lenses, so
    every comparison is directional and a cell without its n reads as a
    result rather than as a direction. The control's own n is disclosed too —
    it is the comparison the adoption rule rests on — but never inside the
    arm cells, because the amendment keeps it out of the arm mix.

    **The control cell is REQUIRED, by seat.** A round that never reported
    it passed this check before, which contradicted the docstring above it:
    the adoption rule is "AIIF arm ≥ CONTROL on O1", so a reported analysis
    with no control cell has not reported the comparison it rests on. A row
    that names the control in its cell TEXT while carrying no ``seat`` key is
    read as an undisclosed control, not as an arm cell — that is the shape
    that let the cell pass as one."""
    outcomes = subject.get("outcomes") or []
    if not outcomes:
        return _result("per_arm_n_disclosed", False, "subject['outcomes'] is empty -- the pre-registered analysis reported no cells")
    undisclosed = [
        str(row.get("cell") or row.get("arm") or row.get("card") or "<unnamed cell>")
        for row in outcomes
        if not isinstance(row.get("n"), (int, float))
    ]
    mixed = [
        str(row.get("cell") or "<unnamed cell>")
        for row in outcomes
        if str(row.get("seat") or "").lower() == CONTROL_SEAT and row.get("arm") in ("near", "moderate", "far")
    ]
    unkeyed = [
        str(row.get("cell") or "<unnamed cell>")
        for row in outcomes
        if not str(row.get("seat") or "").strip() and CONTROL_SEAT in str(row.get("cell") or "").lower()
    ]
    if undisclosed or mixed or unkeyed:
        return _result(
            "per_arm_n_disclosed", False,
            f"{len(undisclosed)} outcome cell(s) disclose no n ({undisclosed[:10]}); control cells reported "
            f"inside the arm mix: {mixed or 'none'}; cell(s) naming the control with no seat key, which is an "
            f"undisclosed control rather than an arm: {unkeyed or 'none'}",
        )
    control_cells = [r for r in outcomes if str(r.get("seat") or "").lower() == CONTROL_SEAT]
    if not control_cells:
        return _result(
            "per_arm_n_disclosed", False,
            f"all {len(outcomes)} outcome cell(s) disclose their n, but no cell carries seat="
            f"{CONTROL_SEAT!r} -- the adoption rule compares the arms against the matched-budget control, so a "
            "reported analysis with no control cell has not reported the comparison it rests on. The control is "
            "named by seat, because the amendment keeps it out of the arm mix",
        )
    return _result(
        "per_arm_n_disclosed", True,
        f"all {len(outcomes)} outcome cell(s) disclose their n; the control is reported as a control "
        f"({len(control_cells)} cell(s)) and not as an arm",
    )


def no_significance_language(subject: Mapping[str, Any]) -> MetricResult:
    """``subject["report_text"]`` carries no significance vocabulary
    (:data:`SIGNIFICANCE_TERMS`) and no ``p < .05``-shaped claim.

    The round's own limits paragraph says why: n per arm or card is single
    digits, so every comparison is directional. "Significant" in a report
    over six cells is a claim the design does not make, whether or not a
    test was run."""
    text = subject.get("report_text")
    if text is None:
        return _result("no_significance_language", False, "subject['report_text'] is absent -- there is no prose to check")
    found = sorted({m.group(0).lower() for m in _SIGNIFICANCE_RE.finditer(str(text))})
    p_values = sorted({m.group(0) for m in _P_VALUE_RE.finditer(str(text))})
    if found or p_values:
        return _result(
            "no_significance_language", False,
            f"the report uses significance language {found + p_values} -- every comparison in a round of this "
            "size is directional, with its per-cell n disclosed",
        )
    return _result("no_significance_language", True, "the report states its comparisons directionally, with no significance language")


def consolidation_completeness_over_ideas(subject: Mapping[str, Any]) -> MetricResult:
    """The C-0066 consolidation law applied to a round's own records: every
    raw idea has a disposition. :func:`consolidation_completeness` verbatim,
    reading the round's ``ideas`` section as its findings list — the law's
    implementation is not restated here, only pointed at the other subject
    shape, so the two cannot drift.

    ``raw`` is the one status that is NOT a disposition: it is the state of
    an idea nothing has decided about yet."""
    findings = [
        {"finding": i.get("idea_id") or "<unnamed idea>", "disposition": str(i.get("status") or "").upper()}
        for i in subject.get("ideas", [])
    ]
    return consolidation_completeness({"findings": findings}, valid_dispositions=IDEA_DISPOSITIONS)


#: The framework round gate class: design §6's thirteen named checks plus
#: `consolidation_completeness` reused over the round's own idea rows.
AIIF_ROUND_SUITE_ID = "aiif_round"
register_suite(
    GateSuite(
        suite_id=AIIF_ROUND_SUITE_ID,
        checks={
            "prereg_present": prereg_present,
            "models_table_present": models_table_present,
            "novelty_bundle_complete": novelty_bundle_complete,
            "self_assessment_absent": self_assessment_absent,
            "plants_caught": plants_caught,
            "distribution_card_present": distribution_card_present,
            "control_arm_present": control_arm_present,
            "arm_mode_declared": arm_mode_declared,
            "card_cells_ge_2": card_cells_ge_2,
            "admission_order_hash_matches": admission_order_hash_matches,
            "lens_log_reconciled": lens_log_reconciled,
            "per_arm_n_disclosed": per_arm_n_disclosed,
            "no_significance_language": no_significance_language,
            "consolidation_completeness": consolidation_completeness_over_ideas,
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
