"""Citation-check pipeline. Design Section 8.1 (``trialerror verify citecheck
<artifact|file|claim-set>``), two-tier, hyperresearch ``citecheck.py``
pattern -- deterministic and resumable:

1. **Mechanical pass (zero LLM):** bind every citation marker to its
   sentence; auto-pass when the sentence's numbers or a 6-word shingle
   appear in the cited chunk/anchor; ``resolve_quote`` confirms the anchor
   still resolves byte-exact (quote_sha match).
2. **LLM escalation:** unresolved pairs only; deterministic sampling (100%
   of number-bearing sentences, every k-th of the rest); an external judge
   (a booked, tool-locked read-only verifier subagent at runtime, a
   deterministic fake in tests) returns per-pair labels.

Output: ``verdict`` rows (``procedure=citecheck``) + a summary dict a
caller can register as a typed artifact (``trialerror.artifacts`` is out of this
module's lane -- see ``trialerror/verify/__init__.py``'s scope note); failures
list the exact sentence, marker, and anchor for surgical patching.

**Citation-marker convention (a v0 scope decision -- design Section 8.1
does not pin an exact syntax):** a citation binds to the sentence
immediately preceding it via ``[[cite:ANC-<ulid>]]`` — e.g. ``"Dice pools
resolve uncertain outcomes. [[cite:ANC-01JXYZ...]]"``. :func:`extract_citation_pairs`
is the one place this convention is implemented; a future artifact template
that embeds citations differently only needs a different extractor feeding
the same :func:`run_citecheck` (which accepts pre-extracted ``pairs``
directly, bypassing the extractor entirely).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from trialerror.retrieve import engine as retrieve_engine
from trialerror.stores import get as store_get
from trialerror.stores.store import Store
from trialerror.verify.errors import CitecheckError
from trialerror.verify.verdicts import record_verdict

__all__ = [
    "CITATION_MARKER_RE",
    "CITECHECK_LABELS",
    "extract_citation_pairs",
    "build_citecheck_judgment_envelope",
    "run_citecheck",
]

#: ``[[cite:ANC-<id>]]`` -- see module docstring's "Citation-marker
#: convention" note.
CITATION_MARKER_RE = re.compile(r"\[\[cite:(?P<anchor_id>ANC-[0-9A-Za-z]+)\]\]")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"\d[\d,.]*")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_SHINGLE_N = 6

#: This pipeline's own escalation-judge vocabulary (distinct from
#: :mod:`trialerror.verify.labels`'s 11-point contracrow scale, which is
#: :mod:`trialerror.verify.hypothesis`'s vocabulary for a different question --
#: "does this literature agree/contradict a hypothesis" vs. citecheck's
#: narrower "does this anchor actually support this sentence").
CITECHECK_LABELS: tuple[str, ...] = ("supported", "unsupported", "uncertain")


def extract_citation_pairs(text: str) -> list[dict[str, Any]]:
    """Scan ``text`` for ``[[cite:ANC-...]]`` markers, binding each to the
    sentence immediately preceding it. Returns one dict per marker:
    ``{pair_id, sentence, marker, anchor_id, char_start, char_end}`` in
    document order (the order this function returns pairs in is what makes
    :func:`run_citecheck`'s escalation sampling deterministic)."""
    pairs: list[dict[str, Any]] = []
    cursor = 0
    for match in CITATION_MARKER_RE.finditer(text):
        segment = text[cursor : match.start()].strip()
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(segment) if s.strip()]
        sentence = sentences[-1] if sentences else segment
        pairs.append(
            {
                "pair_id": f"CPR-{len(pairs) + 1}",
                "sentence": sentence,
                "marker": match.group(0),
                "anchor_id": match.group("anchor_id"),
                "char_start": match.start(),
                "char_end": match.end(),
            }
        )
        cursor = match.end()
    return pairs


def _numbers(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text))


def _shingles(text: str, n: int = _SHINGLE_N) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _WORD_RE.findall(text)]
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _mechanical_text_match(sentence: str, anchor_quote: str) -> bool:
    """Design Section 8.1: "auto-pass when the sentence's numbers or a
    6-word shingle appear in the cited chunk/anchor". A sentence with no
    numbers and fewer than 6 words never mechanically passes on either
    signal -- it always escalates, which is the conservative (never a
    false mechanical pass) direction."""
    sentence_numbers = _numbers(sentence)
    if sentence_numbers and sentence_numbers & _numbers(anchor_quote):
        return True
    sentence_shingles = _shingles(sentence)
    if sentence_shingles and sentence_shingles & _shingles(anchor_quote):
        return True
    return False


def _is_number_bearing(sentence: str) -> bool:
    return bool(_NUMBER_RE.search(sentence))


def _select_escalation_sample(candidates: Sequence[dict[str, Any]], sample_rate: int) -> list[dict[str, Any]]:
    """Design Section 8.1: "deterministic sampling (100% of number-bearing
    sentences, every k-th of the rest)". Deterministic because it depends
    only on ``candidates``' order (document order -- see
    :func:`extract_citation_pairs`), never on randomness or wall-clock
    time -- re-running citecheck over the same text selects the identical
    sample every time (design Section 8.1: "deterministic and resumable")."""
    if sample_rate < 1:
        raise CitecheckError(f"sample_rate must be >= 1, got {sample_rate}")
    selected: list[dict[str, Any]] = []
    rest_idx = 0
    for pair in candidates:
        if _is_number_bearing(pair["sentence"]):
            selected.append(pair)
            continue
        if rest_idx % sample_rate == 0:
            selected.append(pair)
        rest_idx += 1
    return selected


def build_citecheck_judgment_envelope(pair: Mapping[str, Any]) -> dict[str, Any]:
    """The judgment-request envelope an external judge (real subagent or
    test fake) fills for one escalated pair -- this module never calls an
    LLM itself (see ``trialerror/verify/__init__.py``'s LLM-judgment-boundary
    note)."""
    return {
        "kind": "citecheck",
        "pair_id": pair["pair_id"],
        "sentence": pair["sentence"],
        "marker": pair["marker"],
        "anchor_id": pair["anchor_id"],
        "anchor_quote": pair.get("anchor_quote"),
        "labels": list(CITECHECK_LABELS),
        "instruction": (
            "Does the cited anchor text support the sentence's claim? "
            f"Respond with exactly one of: {', '.join(CITECHECK_LABELS)}."
        ),
    }


def _resolve_anchor_liveness(store: Store, anchor: Mapping[str, Any]) -> bool:
    """"resolve_quote confirms the anchor still resolves byte-exact
    (quote_sha match)" (design Section 8.1) -- calls M8's own
    :func:`trialerror.retrieve.engine.resolve_quote` (the integration contract:
    "trialerror.retrieve.engine.search/resolve_quote are your literature-evidence
    calls") rather than re-deriving quote-hash comparison logic here.

    Requires ``match_type == "exact"`` specifically -- design's own words
    are "byte-exact (quote_sha match)", not merely "found somewhere":
    ``resolve_quote`` falls back to a substring scan when the exact
    ``quote_sha256`` lookup misses (design Section 5.1: "a caller supplying
    a partial quote"), and accepting that fallback here would let a
    corrupted anchor (``quote_text`` edited without ``quote_sha256`` kept
    in sync -- exactly what a stale re-normalization looks like) trivially
    "resolve" against its OWN now-inconsistent row via a substring match on
    itself."""
    quote_text = anchor.get("quote_text") or ""
    resolved = retrieve_engine.resolve_quote(store, quote_text, doc_id=anchor.get("doc_id"))
    if not resolved["found"] or resolved["match_type"] != "exact":
        return False
    return any(m["anchor_id"] == anchor["anchor_id"] for m in resolved["matches"])


def run_citecheck(
    store: Store,
    *,
    subject_id: str,
    text: str | None = None,
    pairs: Sequence[Mapping[str, Any]] | None = None,
    procedure_version: str = "1",
    issued_by_launch: str,
    judge: Callable[[Mapping[str, Any]], Any] | None = None,
    sample_rate: int = 5,
) -> dict[str, Any]:
    """Run the full two-tier citecheck pipeline over either ``text``
    (extracted via :func:`extract_citation_pairs`) or pre-extracted
    ``pairs`` (exactly one of the two must be given). ``subject_id``
    identifies what is being checked (an artifact id, a file path, a
    claim-set id -- design's ``<artifact|file|claim-set>`` -- purely a
    label this function attaches to its verdict rows, never dereferenced).

    ``judge``, if given, is called as ``judge(envelope) -> label_or_dict``
    for every SAMPLED escalation candidate (see
    :func:`build_citecheck_judgment_envelope`); its return may be a bare
    label string (one of :data:`CITECHECK_LABELS`) or a
    ``{"label": ..., "note": ...}`` dict. A pair that fails the mechanical
    heuristic ends up in one of three unresolved states, none of which get
    a verdict row written (there is no label to record yet): pairs the
    deterministic sampler did NOT select stay ``"escalation_not_sampled"``;
    pairs it DID select but ``judge=None`` stay ``"escalation_selected"``
    (envelope built, ready for a caller to re-run with a judge later);
    pairs it selected AND a judge was given become ``"llm_pass"``/
    ``"llm_fail"``.

    Returns ``{"pairs": [...], "failures": [...], "summary": {...},
    "summary_verdict": <row>, "verdict_rows": [<row>, ...]}``. ``failures``
    lists, per design Section 8.1, "the exact sentence, marker, and anchor
    for surgical patching" for every pair that did not pass (mechanically,
    by LLM judgment, or because its anchor does not resolve at all).
    """
    if (text is None) == (pairs is None):
        raise CitecheckError("run_citecheck: exactly one of 'text' or 'pairs' must be given")
    resolved_pairs = list(pairs) if pairs is not None else extract_citation_pairs(text)  # type: ignore[arg-type]

    results: list[dict[str, Any]] = []
    escalation_candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for pair in resolved_pairs:
        pair = dict(pair)
        anchor = store_get(store, "quote_anchor", pk_column="anchor_id", pk_value=pair["anchor_id"])
        if anchor is None:
            pair["status"] = "anchor_not_found"
            results.append(pair)
            failures.append(pair)
            continue

        pair["anchor_quote"] = anchor.get("quote_text")
        pair["chunk_id"] = anchor.get("chunk_id")

        if not _resolve_anchor_liveness(store, anchor):
            pair["status"] = "anchor_stale"
            results.append(pair)
            failures.append(pair)
            continue

        if _mechanical_text_match(pair["sentence"], pair["anchor_quote"] or ""):
            pair["status"] = "mechanical_pass"
            results.append(pair)
            continue

        pair["status"] = "escalation_candidate"
        results.append(pair)
        escalation_candidates.append(pair)

    sampled_ids = {p["pair_id"] for p in _select_escalation_sample(escalation_candidates, sample_rate)}
    for pair in results:
        if pair["status"] != "escalation_candidate":
            continue
        if pair["pair_id"] not in sampled_ids:
            # Design Section 8.1's sampling economy: this pair failed the
            # mechanical heuristic but was NOT selected for LLM escalation
            # (every k-th of the "rest" bucket) -- no envelope is built for
            # it at all, distinct from "selected but not yet judged" below.
            pair["status"] = "escalation_not_sampled"
            continue
        envelope = build_citecheck_judgment_envelope(pair)
        pair["judgment_envelope"] = envelope
        if judge is None:
            # Selected by the deterministic sampler, envelope built and
            # ready -- just no judge supplied THIS run. A caller re-runs
            # with a judge (or a human addresses it directly) later.
            pair["status"] = "escalation_selected"
            continue
        verdict = judge(envelope)
        label = verdict["label"] if isinstance(verdict, Mapping) else verdict
        note = verdict.get("note") if isinstance(verdict, Mapping) else None
        pair["judge_label"] = label
        pair["judge_note"] = note
        if label == "supported":
            pair["status"] = "llm_pass"
        else:
            pair["status"] = "llm_fail"
            failures.append(pair)

    _UNRESOLVED_STATUSES = ("escalation_candidate", "escalation_not_sampled", "escalation_selected")
    verdict_rows: list[dict[str, Any]] = []
    for pair in results:
        if pair["status"] in _UNRESOLVED_STATUSES:
            continue
        label = "PASS" if pair["status"] in ("mechanical_pass", "llm_pass") else "FAIL"
        evidence = [
            {
                "anchor_id": pair.get("anchor_id"),
                "chunk_id": pair.get("chunk_id"),
                "stance": pair["status"],
                "note": pair.get("judge_note") or pair["sentence"],
            }
        ]
        row = record_verdict(
            store,
            subject_kind="citation",
            subject_id=pair.get("anchor_id") or pair["pair_id"],
            procedure="citecheck",
            procedure_version=procedure_version,
            label=label,
            evidence=evidence,
            issued_by_launch=issued_by_launch,
        )
        verdict_rows.append(row)

    summary = {
        "subject_id": subject_id,
        "total_pairs": len(results),
        "mechanical_pass": sum(1 for r in results if r["status"] == "mechanical_pass"),
        "llm_pass": sum(1 for r in results if r["status"] == "llm_pass"),
        "llm_fail": sum(1 for r in results if r["status"] == "llm_fail"),
        "escalation_not_sampled": sum(1 for r in results if r["status"] == "escalation_not_sampled"),
        "escalation_selected": sum(1 for r in results if r["status"] == "escalation_selected"),
        "anchor_not_found": sum(1 for r in results if r["status"] == "anchor_not_found"),
        "anchor_stale": sum(1 for r in results if r["status"] == "anchor_stale"),
        "overall": "PASS" if not failures else "FAIL",
    }
    summary_verdict = record_verdict(
        store,
        subject_kind="artifact",
        subject_id=subject_id,
        procedure="citecheck",
        procedure_version=procedure_version,
        label=summary["overall"],
        evidence=[{"note": f"citecheck summary: {summary}"}],
        issued_by_launch=issued_by_launch,
    )

    return {
        "pairs": results,
        "failures": failures,
        "summary": summary,
        "summary_verdict": summary_verdict,
        "verdict_rows": verdict_rows,
    }
