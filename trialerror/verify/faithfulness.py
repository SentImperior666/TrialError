"""Faithfulness scoring -- the Ragas statement-decomposition + NLI-verdict
pattern (``docs/mining/S6-eval-obs__ragas.md``'s #1 steal-patterns item),
ported OFFLINE per design Section 11 ("hypothesis pipeline hardening ...
Ragas faithfulness port"). Design Section 8.2's own deferral note names
this exactly: "the Ragas ~160-line faithfulness-NLI port as an additional
per-claim quote-grounding metric; v0's mechanical citecheck covers the
floor" -- this module is that v1 piece.

**The port, mapped onto machinery this codebase already has (not a fresh
NLI implementation):** Ragas's algorithm is (1) an LLM call decomposes a
response into atomic statements; (2) a second LLM call NLI-checks each
statement against the joined retrieved context; (3) score = fraction
"supported". Step (2) is, structurally, exactly what
:mod:`trialerror.verify.citecheck` already does -- one sentence, one cited
anchor, a judge returning ``supported``/``unsupported``/``uncertain``. So
this module does NOT reimplement an NLI check: it decomposes each cited
SENTENCE into atomic CLAIMS (step 1, a new judge envelope --
:func:`build_decomposition_envelope`), gives each claim the SAME anchor its
parent sentence cited, and re-verifies each claim through
:func:`trialerror.verify.citecheck.run_citecheck` UNCHANGED (step 2 --
"via the existing resolve_quote/citecheck machinery", this build's brief,
verbatim) -- mechanical text-match first, LLM escalation only for what the
mechanical pass can't settle, same as any other citecheck pair. This is
STRICTER than Ragas's own "joined context" NLI check: our per-claim grounding
is against the ONE anchor the claim's parent sentence actually cited, not
the whole retrieved context pool, matching this codebase's "quote-ground
every claim" curriculum policy more literally than Ragas's own reference
implementation does.

**LLM-judgment boundary (same convention as citecheck/hypothesis, restated
here):** this module never calls an LLM. :func:`run_faithfulness` accepts
two judge callables -- ``decompose_judge(envelope) -> claims`` (step 1) and
``verify_judge(envelope) -> label`` (step 2, the exact
:func:`~trialerror.verify.citecheck.build_citecheck_judgment_envelope` shape,
passed straight through to :func:`~trialerror.verify.citecheck.run_citecheck` as
its own ``judge``) -- a real subagent fills at runtime, a deterministic
fake fills in tests.

**Score.** ``supported-claims ratio`` = ``(mechanical_pass + llm_pass) /
total_claims`` over the citecheck run against the DECOMPOSED claims (not the
original sentences) -- Ragas's own "score = fraction of statements whose
verdict is 'supported'", read onto our two-status-name vocabulary
(:data:`trialerror.verify.citecheck.CITECHECK_LABELS` collapses to PASS/FAIL the
same way citecheck's own summary does). A verdict row
(``procedure="custom"``, since "faithfulness" is not one of ``verdict.
procedure``'s five fixed values -- see :mod:`trialerror.verify.verdicts`) records
the score, one aggregate row per :func:`run_faithfulness` call, label = the
score formatted to 4 decimal places (a plain numeric string, not a
PASS/FAIL bucket -- thresholding is the gate-suite's job, see
:func:`trialerror.eval.gate_suites.faithfulness_threshold`, not this module's).

TRIALERROR-DEV-NOTE (escalation-stage semantics): :func:`run_citecheck_with_faithfulness`
wires this in as an OPTIONAL citecheck stage WITHOUT editing
``trialerror/verify/citecheck.py`` at all (one-directional dependency: this
module imports FROM ``citecheck.py``, never the reverse -- editing the
already-landed, already-tested ``run_citecheck`` would risk a circular
import the moment it needed to call back into faithfulness scoring, and
every existing citecheck test stays untouched). "Escalation" here means:
run the normal two-tier citecheck pass first, then apply the deeper
atomic-claim faithfulness check only to pairs that did NOT cleanly resolve
as supported at the sentence level (``mechanical_pass``/``llm_pass``) --
paying for a third, more expensive tier only where the first two tiers
left real doubt, mirroring citecheck's OWN "mechanical first, LLM escalation
only for what mechanical can't settle" economy one level up.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from trialerror.stores.store import Store
from trialerror.verify.citecheck import extract_citation_pairs, run_citecheck
from trialerror.verify.errors import VerifyError
from trialerror.verify.verdicts import record_verdict

__all__ = [
    "CLEAN_PASS_STATUSES",
    "build_decomposition_envelope",
    "run_faithfulness",
    "run_citecheck_with_faithfulness",
]

#: citecheck pair statuses that already resolved as "supported" at the
#: sentence level -- these are what :func:`run_citecheck_with_faithfulness`
#: does NOT re-escalate to atomic-claim faithfulness by default (no
#: additional doubt to resolve).
CLEAN_PASS_STATUSES: frozenset[str] = frozenset({"mechanical_pass", "llm_pass"})

_DECOMPOSE_INSTRUCTION = (
    "Decompose this sentence into its atomic, independently-verifiable factual claims "
    "(Ragas-style statement decomposition). Each claim must be a short, standalone "
    "statement that follows ONLY from what this sentence actually says -- do not add "
    "facts the sentence doesn't state. A sentence with a single fact may decompose to "
    "just one claim (itself, lightly reworded as a standalone statement)."
)


def build_decomposition_envelope(pair: Mapping[str, Any]) -> dict[str, Any]:
    """The judgment-request envelope for faithfulness's step 1 (Ragas's
    ``StatementGeneratorPrompt``) -- one cited sentence in, a claims list
    expected out. ``pair`` is any citecheck-pair-shaped mapping carrying at
    least ``pair_id``/``sentence``/``anchor_id``
    (:func:`~trialerror.verify.citecheck.extract_citation_pairs`'s own row
    shape, or one of :func:`~trialerror.verify.citecheck.run_citecheck`'s
    returned ``pairs`` rows -- both work unmodified)."""
    return {
        "kind": "faithfulness_decompose",
        "pair_id": pair["pair_id"],
        "sentence": pair["sentence"],
        "anchor_id": pair.get("anchor_id"),
        "instruction": _DECOMPOSE_INSTRUCTION,
    }


def _normalize_claims(decomposition: Any, *, fallback_sentence: str) -> list[str]:
    """A judge's decomposition reply may be a bare list of claim strings or
    a ``{"claims": [...]}`` dict (same "bare value or dict-with-note" latitude
    every other judge return in this codebase gets -- e.g.
    ``run_hypothesis_verification``'s ``verdict_out["label"] if isinstance(...,
    Mapping) else verdict_out``). An empty/undecomposable reply falls back
    to the ORIGINAL sentence as its own single claim -- a sentence that
    can't be broken down further is still one atomic claim, never zero."""
    claims = decomposition.get("claims", []) if isinstance(decomposition, Mapping) else list(decomposition)
    claims = [str(c).strip() for c in claims if str(c).strip()]
    return claims or [fallback_sentence]


def run_faithfulness(
    store: Store,
    *,
    subject_id: str,
    text: str | None = None,
    pairs: Sequence[Mapping[str, Any]] | None = None,
    decompose_judge: Callable[[Mapping[str, Any]], Any],
    verify_judge: Callable[[Mapping[str, Any]], Any],
    issued_by_launch: str,
    procedure_version: str = "1",
    sample_rate: int = 5,
) -> dict[str, Any]:
    """The full faithfulness pipeline: decompose every cited sentence into
    atomic claims, then verify each claim against ITS parent's cited
    anchor via :func:`~trialerror.verify.citecheck.run_citecheck` unchanged
    (module docstring). Exactly one of ``text`` (extracted via
    :func:`~trialerror.verify.citecheck.extract_citation_pairs`) or ``pairs``
    (pre-extracted citecheck-pair rows -- e.g. a subset of another
    ``run_citecheck`` call's own ``pairs``) must be given, same convention
    as ``run_citecheck`` itself.

    Returns ``{"subject_id", "total_claims", "supported_claims", "score"
    (``None`` when there were zero source pairs to decompose -- an
    undefined ratio, never a spurious ``0.0``/``1.0``), "breakdown"
    (per-claim ``{pair_id, parent_pair_id, claim, anchor_id, status}``),
    "claim_pairs", "citecheck_result" (the full nested citecheck run over
    the decomposed claims, or ``None`` when ``score`` is ``None``),
    "verdict"}``.
    """
    if (text is None) == (pairs is None):
        raise VerifyError("run_faithfulness: exactly one of 'text' or 'pairs' must be given")
    resolved_pairs = list(pairs) if pairs is not None else extract_citation_pairs(text)  # type: ignore[arg-type]

    claim_pairs: list[dict[str, Any]] = []
    for pair in resolved_pairs:
        envelope = build_decomposition_envelope(pair)
        claims = _normalize_claims(decompose_judge(envelope), fallback_sentence=pair["sentence"])
        for i, claim_text in enumerate(claims):
            claim_pairs.append(
                {
                    "pair_id": f"{pair['pair_id']}::CLM-{i + 1}",
                    "sentence": claim_text,
                    "anchor_id": pair.get("anchor_id"),
                    "marker": pair.get("marker"),
                    "parent_pair_id": pair["pair_id"],
                }
            )

    if not claim_pairs:
        verdict_row = record_verdict(
            store, subject_kind="artifact", subject_id=subject_id, procedure="custom",
            procedure_version=procedure_version, label="no_claims", evidence=[],
            issued_by_launch=issued_by_launch,
        )
        return {
            "subject_id": subject_id, "total_claims": 0, "supported_claims": 0, "score": None,
            "breakdown": [], "claim_pairs": [], "citecheck_result": None, "verdict": verdict_row,
        }

    citecheck_result = run_citecheck(
        store, subject_id=f"faithfulness::{subject_id}", pairs=claim_pairs,
        procedure_version=procedure_version, issued_by_launch=issued_by_launch,
        judge=verify_judge, sample_rate=sample_rate,
    )

    parent_of = {cp["pair_id"]: cp["parent_pair_id"] for cp in claim_pairs}
    total = len(citecheck_result["pairs"])
    supported = sum(1 for p in citecheck_result["pairs"] if p["status"] in CLEAN_PASS_STATUSES)
    score = supported / total if total else None
    breakdown = [
        {
            "pair_id": p["pair_id"],
            "parent_pair_id": parent_of.get(p["pair_id"]),
            "claim": p["sentence"],
            "anchor_id": p.get("anchor_id"),
            "status": p["status"],
            "supported": p["status"] in CLEAN_PASS_STATUSES,
        }
        for p in citecheck_result["pairs"]
    ]

    verdict_row = record_verdict(
        store, subject_kind="artifact", subject_id=subject_id, procedure="custom",
        procedure_version=procedure_version, label=f"{score:.4f}",
        evidence=[
            {"anchor_id": b["anchor_id"], "stance": b["status"], "note": b["claim"]} for b in breakdown
        ],
        issued_by_launch=issued_by_launch,
    )

    return {
        "subject_id": subject_id,
        "total_claims": total,
        "supported_claims": supported,
        "score": score,
        "breakdown": breakdown,
        "claim_pairs": claim_pairs,
        "citecheck_result": citecheck_result,
        "verdict": verdict_row,
    }


def run_citecheck_with_faithfulness(
    store: Store,
    *,
    subject_id: str,
    text: str | None = None,
    pairs: Sequence[Mapping[str, Any]] | None = None,
    decompose_judge: Callable[[Mapping[str, Any]], Any],
    verify_judge: Callable[[Mapping[str, Any]], Any],
    issued_by_launch: str,
    citecheck_judge: Callable[[Mapping[str, Any]], Any] | None = None,
    procedure_version: str = "1",
    sample_rate: int = 5,
    escalate_statuses: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run :func:`~trialerror.verify.citecheck.run_citecheck` first (unmodified,
    exactly as any other caller would), then escalate to the deeper
    atomic-claim faithfulness check ONLY for pairs that didn't cleanly pass
    at the sentence level (module docstring's TRIALERROR-DEV-NOTE) --
    ``escalate_statuses`` narrows that default set further when given
    (e.g. only ``["llm_fail"]`` to skip re-checking pairs the sampler never
    even selected for the first-tier LLM escalation).

    Returns the citecheck result dict with one extra key,
    ``"faithfulness"`` -- the :func:`run_faithfulness` result over the
    escalated pairs, or ``None`` when nothing needed escalation (every pair
    was a clean ``mechanical_pass``/``llm_pass``)."""
    citecheck_result = run_citecheck(
        store, subject_id=subject_id, text=text, pairs=pairs, procedure_version=procedure_version,
        issued_by_launch=issued_by_launch, judge=citecheck_judge, sample_rate=sample_rate,
    )
    wanted = frozenset(escalate_statuses) if escalate_statuses is not None else None
    escalate_pairs = [
        p for p in citecheck_result["pairs"]
        if p["status"] not in CLEAN_PASS_STATUSES and (wanted is None or p["status"] in wanted)
    ]
    if not escalate_pairs:
        return {**citecheck_result, "faithfulness": None}

    faithfulness_result = run_faithfulness(
        store, subject_id=f"escalation::{subject_id}", pairs=escalate_pairs,
        decompose_judge=decompose_judge, verify_judge=verify_judge, issued_by_launch=issued_by_launch,
        procedure_version=procedure_version, sample_rate=sample_rate,
    )
    return {**citecheck_result, "faithfulness": faithfulness_result}
