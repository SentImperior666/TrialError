"""The fail-closed faithfulness guard.
``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 4.3, Section 5 step 3.

The design's own words for why this exists: "a wrong translation of a gate
verdict or a booking id is worse than no translation". So the gate's
disposition is CLOSED -- a translation it fails is stored (countable,
inspectable) but never served; the dashboard keeps showing the original
and says the translation was withheld.

Two tiers, in this order:

1. **Deterministic fidelity tier -- always runs, no LLM, no network.**
   :func:`trialerror.feed_translate.style.check_style`'s ``fidelity``-severity
   rules (§4.5 rules 8-9): every id, number and date from the original
   survives verbatim; nothing numeric is invented; no hedge family is
   promoted to a fact. Any one of these fails the gate. This tier is the
   floor, and it is what makes "fail-closed" a property of the system
   rather than a property of whether someone remembered to pass judges.
2. **Judged faithfulness tier -- optional, deeper.** Ragas-style
   statement decomposition, retargeted per §4.3.1 so the ORIGINAL POST
   BODY is the sole anchor every claim in the translation must trace back
   to. Produces a supported-claims ratio, thresholded by
   :func:`trialerror.eval.gate_suites.faithfulness_threshold` -- the same
   function §4.3.3 names -- and recorded as a ``knowledge.verdict`` row
   (``procedure="custom"``, label = the score to 4dp), the same verdict
   shape :func:`trialerror.verify.faithfulness.run_faithfulness` writes.

TRIALERROR-DEV-NOTE (why tier 2 composes
:mod:`trialerror.verify.faithfulness` instead of calling
:func:`~trialerror.verify.faithfulness.run_faithfulness` whole). Two hard
blockers, both structural, neither a matter of taste:

- ``run_faithfulness`` verifies claims through
  :func:`trialerror.verify.citecheck.run_citecheck` unchanged, and that
  function resolves each pair's ``anchor_id`` against a real
  ``knowledge.quote_anchor`` row, which is ``NOT NULL REFERENCES
  document(doc_id)`` and must additionally resolve byte-exact through
  ``trialerror.retrieve.engine.resolve_quote``. A Feed post is not a
  corpus document and has neither. Manufacturing a synthetic
  ``source``/``document``/``quote_anchor`` triple per translated post
  would inject junk rows into the knowledge corpus that every corpus
  count, coverage panel and ``anchors_dangling`` scan would then have to
  learn to ignore -- a much larger and more damaging change than this
  feature is entitled to make.
- ``run_citecheck``'s mechanical tier auto-passes on a shared 6-word
  shingle or shared numbers. For a TRANSLATION that heuristic is actively
  inverted: a good plain-English rewrite deliberately shares almost no
  6-word shingle with its jargon-dense original, so the better the
  translation, the more likely it mechanically "fails" and escalates.
  The mechanical tier that IS right for translation is the token-identity
  one -- ids, numbers, dates -- which is exactly tier 1 above.

So this module imports and uses the pieces of that pipeline whose contract
does transfer -- :func:`~trialerror.verify.faithfulness.build_decomposition_envelope`
(step 1, unchanged), :data:`~trialerror.verify.faithfulness.CLEAN_PASS_STATUSES`
and :data:`~trialerror.verify.citecheck.CITECHECK_LABELS` (the judge's
vocabulary), :func:`~trialerror.verify.verdicts.record_verdict` (the durable
verdict row) -- and supplies the one piece that cannot transfer: an
anchor that is the post body itself rather than a corpus row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from trialerror.eval.gate_suites import faithfulness_threshold
from trialerror.feed_translate.errors import ClaimJudgmentMissingError
from trialerror.feed_translate.style import DEFAULT_STYLE_MODE, StyleReport, check_style, split_sentences
from trialerror.stores.store import Store
from trialerror.verify.citecheck import CITECHECK_LABELS
from trialerror.verify.faithfulness import CLEAN_PASS_STATUSES, build_decomposition_envelope
from trialerror.verify.verdicts import record_verdict

__all__ = [
    "DEFAULT_FAITHFULNESS_MIN_SCORE",
    "GateResult",
    "build_claim_envelope",
    "judge_from_claim_table",
    "run_translation_gate",
]

#: §4.3.3's "configured floor", defaulted to the same 0.8
#: :func:`trialerror.eval.gate_suites.faithfulness_threshold` already uses
#: everywhere else in this codebase -- one number, not a second opinion.
#: Overridable per program via ``[feed.translator] faithfulness_min_score``.
DEFAULT_FAITHFULNESS_MIN_SCORE = 0.8

_CLAIM_INSTRUCTION = (
    "The ANCHOR below is a Feed post exactly as it was written. The CLAIM below is one "
    "atomic statement taken from a plain-English translation of that post. Does the anchor "
    "support the claim -- is the claim something the anchor actually says? A claim that adds "
    "a fact, cause, number or certainty the anchor does not state is NOT supported, even if it "
    f"sounds plausible. Respond with exactly one of: {', '.join(CITECHECK_LABELS)}."
)


@dataclass
class GateResult:
    """The verdict, its reasons, and the row fields
    :func:`trialerror.feed_translate.api.store_translation` writes.

    ``passed`` is the only thing the dashboard needs; everything else is
    why. ``score`` is ``None`` when the judged tier did not run (no judge
    supplied) or found no claims -- distinct from a score of ``0.0``.
    """

    passed: bool
    style: StyleReport
    score: float | None = None
    threshold: float = DEFAULT_FAITHFULNESS_MIN_SCORE
    verdict_id: str | None = None
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def gate_status(self) -> str:
        return "pass" if self.passed else "fail"

    def as_row(self) -> dict[str, Any]:
        """The four ``feed_post_translation`` columns this verdict owns, in
        the shape :func:`trialerror.feed_translate.api.store_translation`
        expects as its ``gate=`` argument."""
        return {
            "gate_status": self.gate_status,
            "gate_reasons": json.dumps(self.as_reasons_dict(), ensure_ascii=False),
            "faithfulness_score": self.score,
            "faithfulness_verdict_id": self.verdict_id,
        }

    def as_reasons_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "score": self.score,
            "threshold": self.threshold,
            "judged": self.score is not None,
            "style": self.style.as_dict(),
        }


def build_claim_envelope(
    *, post_id: str, claim_id: str, claim: str, original_body: str
) -> dict[str, Any]:
    """The judgment-request envelope for tier 2's per-claim verification.

    Deliberately the same key names
    :func:`trialerror.verify.citecheck.build_citecheck_judgment_envelope`
    uses (``pair_id``/``sentence``/``anchor_quote``/``labels``/
    ``instruction``), so a judge callable written for citecheck works here
    unmodified -- what differs is only where ``anchor_quote`` comes from:
    the post body itself (§4.3.1's "single synthetic anchor whose content
    is ``feed_post.body``"), not a ``quote_anchor`` row.
    """
    return {
        "kind": "feed_translate_claim",
        "pair_id": claim_id,
        "post_id": post_id,
        "sentence": claim,
        "anchor_id": None,
        "anchor_quote": original_body,
        "labels": list(CITECHECK_LABELS),
        "instruction": _CLAIM_INSTRUCTION,
    }


def judge_from_claim_table(table: Mapping[str, Any]) -> Callable[[Mapping[str, Any]], Any]:
    """Reconstitute a judge callable from a plain ``{pair_id: judgment}``
    table -- the same shape :func:`trialerror.cli.verify`'s own
    ``_judge_from_table`` builds for ``verify faithfulness``'s
    ``--decomposition-file``/``--judgments-file``. Works for BOTH of
    :func:`run_translation_gate`'s judge parameters: :func:`build_
    decomposition_envelope` and :func:`build_claim_envelope` both key their
    envelope on ``"pair_id"``, the one field this closure reads.

    FT-1 (fix pass): this is what makes the judged tier reachable from the
    ``feed_translate`` JOB path, not just from a live agent session calling
    :func:`run_translation_gate` directly. The earlier claim that "judge
    callables cannot be serialized into a job payload" was true of the
    CALLABLE -- it was never true of the DATA a callable like this one
    closes over. ``table`` is a plain JSON-safe dict: it travels through
    the job payload/checkpoint exactly the way ``judgments`` (the
    translation text itself) already does, and this function turns it back
    into a callable only at the point of use, inside the worker process
    that actually runs the gate.
    """

    def judge(envelope: Mapping[str, Any]) -> Any:
        key = envelope["pair_id"]
        if key not in table:
            raise ClaimJudgmentMissingError(f"no claim judgment supplied for pair_id={key!r}")
        return table[key]

    return judge


def _normalize_claims(decomposition: Any, *, fallback_sentence: str) -> list[str]:
    """Same latitude :func:`trialerror.verify.faithfulness.run_faithfulness`
    gives its own decompose judge: a bare list of claim strings or a
    ``{"claims": [...]}`` dict; an empty reply falls back to the sentence
    itself as one atomic claim (never zero)."""
    claims = decomposition.get("claims", []) if isinstance(decomposition, Mapping) else list(decomposition or [])
    cleaned = [str(c).strip() for c in claims if str(c).strip()]
    return cleaned or [fallback_sentence]


def run_translation_gate(
    store: Store | None,
    *,
    post_id: str,
    original_body: str,
    translation_body: str,
    style_mode: str = DEFAULT_STYLE_MODE,
    strict_style: bool = False,
    require_faithfulness_score: bool = False,
    min_score: float = DEFAULT_FAITHFULNESS_MIN_SCORE,
    decompose_judge: Callable[[Mapping[str, Any]], Any] | None = None,
    verify_judge: Callable[[Mapping[str, Any]], Any] | None = None,
    issued_by_launch: str | None = None,
    procedure_version: str = "1",
) -> GateResult:
    """Gate one candidate translation. Never raises for a bad translation
    -- a failure is a :class:`GateResult` with ``passed=False``, which the
    caller stores.

    ``store`` may be ``None`` for a pure style/fidelity check with no
    verdict row (what the CLI's dry-run path and most unit tests want).
    A verdict row is written only when a store, both judges, and an
    ``issued_by_launch`` are all present -- ``knowledge.verdict``'s
    ``issued_by_launch`` is ``NOT NULL`` and XID-checked, so there is no
    honest way to record one for an orchestrator-identity translation that
    has no launch. Such a translation still gets a score; it just has no
    verdict row to point at.

    Dispositions, in order:

    - any ``fidelity``-severity style violation -> FAIL (tier 1, always).
    - ``strict_style=True`` and any ``register``-severity violation ->
      FAIL (opt-in; ``[feed.translator] strict_style``).
    - a judged score below ``min_score`` -> FAIL (tier 2, §4.3.3).
    - ``require_faithfulness_score=True`` and no score at all -> FAIL, via
      :func:`trialerror.eval.gate_suites.faithfulness_threshold`'s own
      fail-closed-on-``None`` semantics. Off by default: tier 1 is the
      always-on floor, and withholding every translation on a program that
      has no judge wired would leave the operator with an empty right-hand
      column and no way to tell that apart from a broken translator.
    """
    style = check_style(original_body, translation_body, style_mode=style_mode)
    reasons: list[str] = [v.detail for v in style.fidelity_violations]
    passed = not style.fidelity_violations

    if strict_style and style.register_violations:
        passed = False
        reasons.extend(f"[strict_style] {v.detail}" for v in style.register_violations)

    score: float | None = None
    verdict_id: str | None = None
    breakdown: list[dict[str, Any]] = []

    if decompose_judge is not None and verify_judge is not None:
        score, breakdown = _run_judged_tier(
            post_id=post_id,
            original_body=original_body,
            translation_body=translation_body,
            decompose_judge=decompose_judge,
            verify_judge=verify_judge,
        )
        if score is not None and store is not None and issued_by_launch is not None:
            verdict_row = record_verdict(
                store,
                subject_kind="artifact",
                subject_id=f"feed_translate::{post_id}",
                procedure="custom",
                procedure_version=procedure_version,
                label=f"{score:.4f}",
                evidence=[
                    {"stance": b["status"], "note": b["claim"]} for b in breakdown
                ],
                issued_by_launch=issued_by_launch,
            )
            verdict_id = verdict_row["verdict_id"]

    if score is not None or require_faithfulness_score:
        metric = faithfulness_threshold({"faithfulness": {"score": score}}, min_score=min_score)
        if not metric.passed:
            passed = False
            reasons.append(metric.message)

    return GateResult(
        passed=passed,
        style=style,
        score=score,
        threshold=min_score,
        verdict_id=verdict_id,
        breakdown=breakdown,
        reasons=reasons,
    )


def _run_judged_tier(
    *,
    post_id: str,
    original_body: str,
    translation_body: str,
    decompose_judge: Callable[[Mapping[str, Any]], Any],
    verify_judge: Callable[[Mapping[str, Any]], Any],
) -> tuple[float | None, list[dict[str, Any]]]:
    """Decompose the TRANSLATION into atomic claims, verify each against
    the ORIGINAL as the sole anchor, return ``(score, breakdown)``.
    ``score`` is ``None`` when the translation decomposes to zero claims
    (an undefined ratio, never a spurious ``0.0``/``1.0`` -- the same
    distinction :func:`trialerror.verify.faithfulness.run_faithfulness`
    draws)."""
    claim_pairs: list[dict[str, str]] = []
    for i, sentence in enumerate(split_sentences(translation_body)):
        envelope = build_decomposition_envelope(
            {"pair_id": f"{post_id}::S-{i + 1}", "sentence": sentence, "anchor_id": None}
        )
        for j, claim in enumerate(_normalize_claims(decompose_judge(envelope), fallback_sentence=sentence)):
            claim_pairs.append({"pair_id": f"{post_id}::S-{i + 1}::CLM-{j + 1}", "claim": claim})

    if not claim_pairs:
        return None, []

    breakdown: list[dict[str, Any]] = []
    for pair in claim_pairs:
        envelope = build_claim_envelope(
            post_id=post_id, claim_id=pair["pair_id"], claim=pair["claim"], original_body=original_body
        )
        reply = verify_judge(envelope)
        label = reply["label"] if isinstance(reply, Mapping) else reply
        note = reply.get("note") if isinstance(reply, Mapping) else None
        # Read onto citecheck's own two-status pass vocabulary so
        # CLEAN_PASS_STATUSES stays the single definition of "supported"
        # across both pipelines.
        status = "llm_pass" if label == "supported" else "llm_fail"
        breakdown.append(
            {
                "pair_id": pair["pair_id"],
                "claim": pair["claim"],
                "label": label,
                "note": note,
                "status": status,
                "supported": status in CLEAN_PASS_STATUSES,
            }
        )

    supported = sum(1 for b in breakdown if b["supported"])
    return supported / len(breakdown), breakdown
