"""Hypothesis-vs-literature pipeline. Design Section 8.2 (``trialerror verify
hypothesis HYP-x``), "Declared v0 (R4), owned by M9. This pipeline ships in
v0 in minimal form -- steps 1-5 with stratified retrieval behind a flag":

1. (Optional, default for keystones) :func:`~trialerror.verify.prereg.commit_prereg`
   the procedure + parameters blind.
2. Retrieve: :func:`stratified_retrieve` -- hybrid search stratified into
   near/moderate/far slices (design: "the same machinery as Section 9.6").
3. Classify: each evidence chunk scored against the hypothesis with the
   vendored **contracrow** prompt (:mod:`trialerror.verify.labels`) -- an
   external judge fills a "judgment request" envelope
   (:func:`build_hypothesis_judgment_envelope`); this module never calls
   an LLM itself (see ``trialerror/verify/__init__.py``'s LLM-judgment-boundary
   note).
4. Aggregate: :func:`aggregate_labels` -- label distribution -> hypothesis
   status proposal, alongside :func:`~trialerror.verify.independence.
   independence_stats` (design Section 8.2's "independence clustering /
   syndication discount", v1 scope -- design Section 11: "hypothesis
   pipeline hardening ... independence clustering" -- landed by this build;
   see :mod:`trialerror.verify.independence`'s own module docstring for the
   union-find algorithm and the "why not a new DB column" TRIALERROR-DEV-NOTE).
5. Verdict: one ``verdict`` row (``procedure=contracrow``), evidence-
   anchored, ``prereg_compliant`` stamped when a prereg is attached.

**Stratification, fix-tier3 (C-0064 NB-1): now over embedding distance, per
design Section 8.2's own words -- "near/moderate/far slices over embedding
distance -- the same machinery as Section 9.6".** At the M9 build this
function stratified by RANK instead (M13 was the same build wave, so
``trialerror.lens`` did not exist yet -- see the superseded rationale this
replaces, preserved in git history). M13 has since landed
(:mod:`trialerror.lens.vectors`, :mod:`trialerror.lens.stratify`), so
:func:`stratified_retrieve` now does what the design actually specifies:

1. Retrieve candidates via one ``trialerror.retrieve.engine.search`` call, same
   as before (``mode`` still governs FTS/vector/hybrid *retrieval*).
2. Score each candidate CHUNK's embedding distance to the hypothesis QUERY
   embedding, and tercile-cut on that score -- reusing M13's
   :func:`~trialerror.lens.stratify.score_candidates` /
   :func:`~trialerror.lens.stratify.stratify` VERBATIM (the actual "same
   machinery as Section 9.6", not a reimplementation of it).
3. Apply the 40/40/20 + far-floor quotas to the resulting near/moderate/far
   pools, unchanged from before.

TRIALERROR-DEV-NOTE (faithful-reading choice: per-chunk distance, not M13's
doc-pooled vectors): :mod:`trialerror.lens.vectors` mean-pools chunk vectors up
to DOCUMENT level because M13's own use case scores whole candidate
documents against a home DOCUMENT set -- there is no chunk-level "home" to
compare against in that pipeline, only document-to-document. Hypothesis
verification has no document-level "home" either: its reference point is
the hypothesis QUERY TEXT, and its evidence unit is the individual CHUNK
(every "near"/"moderate"/"far" row this function returns already IS one
chunk, not one document). M8 already exposes exactly the right-shaped
substrate for that -- ``trialerror.retrieve.vecsearch.fetch_vectors`` is
chunk-keyed and is the SAME function ``engine.search``'s own vector tier
uses to rank -- so this function embeds the query once (same
``_resolve_embed_backend`` resolution ``engine.search`` uses internally,
so the SAME ``model_key`` the corpus was embedded under is always used) and
feeds ``{candidate_chunk_id: chunk_vector}`` as ``candidates`` and
``{"__hypothesis_query__": query_vector}`` as the single-vector ``home``
set into :func:`~trialerror.lens.stratify.score_candidates` --
:mod:`trialerror.lens.stratify`'s own docstring names exactly this degenerate
single-vector-home case as "the common case this module is exercised
with". Pooling to document level here would blur exactly the distinction
stratification exists to preserve: two chunks from the same source
document can legitimately sit in different arms if one paragraph agrees
and another contradicts.

Fallback (documented, not a silent degrade): when NONE of the retrieved
candidates have a vector under the corpus's active ``model_key`` (a fresh
corpus with no embeddings run yet), this function falls back to the
original RANK-tercile split -- near = best-ranked third, far =
worst-ranked third of the same search call -- and the returned dict's
``stratify_method`` key records which path ran (``"distance"`` |
``"rank_fallback"`` | ``"empty"`` for the zero-candidate case). A candidate
retrieved but missing a vector under PARTIAL coverage is simply excluded
from the distance-scored pool (the "missing is absent" convention
``fetch_vectors`` itself documents), never silently rank-substituted for.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from trialerror.lens.stratify import score_candidates, stratify
from trialerror.retrieve import engine as retrieve_engine
from trialerror.retrieve.vecsearch import fetch_vectors
from trialerror.stores import get as store_get
from trialerror.stores import update as store_update
from trialerror.stores.store import Store
from trialerror.verify.errors import VerifyError
from trialerror.verify.independence import independence_stats
from trialerror.verify.labels import CONTRACROW_LABELS, CONTRACROW_QA_PROMPT, label_polarity
from trialerror.verify.prereg import check_prereg_compliance, commit_prereg
from trialerror.verify.verdicts import record_verdict

__all__ = [
    "DEFAULT_WEIGHTS",
    "DEFAULT_FAR_FLOOR",
    "stratified_retrieve",
    "build_hypothesis_judgment_envelope",
    "aggregate_labels",
    "run_hypothesis_verification",
]

#: (near, moderate, far) -- AMENDMENT-3's default tercile split (design
#: Section 9.6, ``lens_assignment.weights`` DDL comment: "default [40,40,20]").
DEFAULT_WEIGHTS: tuple[int, int, int] = (40, 40, 20)

#: AMENDMENT-3's default far-arm floor (``lens_assignment.far_floor`` DDL
#: comment: "default 2").
DEFAULT_FAR_FLOOR = 2

_STATUS_VALUES = frozenset({"open", "supported", "contradicted", "mixed"})


def _rank_tercile_pools(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """The ORIGINAL (pre-fix-tier3) stratification: near/moderate/far by
    RANK tercile of one already-fused search call. Kept verbatim as the
    documented fallback for when no candidate has a vector to score by
    distance (see module docstring)."""
    n = len(rows)
    third = max(1, n // 3)
    near_pool = list(rows[:third])
    moderate_pool = list(rows[third : 2 * third]) if n > third else []
    far_pool = list(rows[2 * third :]) if n > 2 * third else list(rows[third:]) or list(rows[:third])
    return {"near": near_pool, "moderate": moderate_pool, "far": far_pool}


def _distance_tercile_pools(
    store: Store, *, query: str, rows: Sequence[Mapping[str, Any]]
) -> dict[str, list[Mapping[str, Any]]] | None:
    """Near/moderate/far pools by embedding distance from each candidate
    CHUNK to the hypothesis QUERY embedding -- design Section 8.2 ("over
    embedding distance -- the same machinery as Section 9.6"), reusing
    :func:`trialerror.lens.stratify.score_candidates`/:func:`~trialerror.lens.
    stratify.stratify` verbatim; see module docstring's TRIALERROR-DEV-NOTE for
    why this scores chunk vectors (not M13's doc-pooled ones) against a
    single-vector "home" set (the embedded query).

    Returns ``None`` (never an empty-dict pools shape) when NONE of
    ``rows`` has a vector under the corpus's active ``model_key`` --  the
    signal the caller falls back to :func:`_rank_tercile_pools` on. A
    candidate present in ``rows`` but missing a vector under partial
    coverage is simply excluded from the scored pool (never rank-
    substituted -- ``fetch_vectors``'s own "missing is absent" contract).
    """
    chunk_ids = [r["chunk_id"] for r in rows]
    model_key, backend = retrieve_engine._resolve_embed_backend(store)
    candidate_vectors = fetch_vectors(store, model_key, chunk_ids)
    if not candidate_vectors:
        return None

    query_vector = backend.embed_batch([query], kind="query")[0]
    scores = score_candidates(candidate_vectors, {"__hypothesis_query__": query_vector})
    stratified = stratify(scores)

    row_by_id = {r["chunk_id"]: r for r in rows}
    pools: dict[str, list[Mapping[str, Any]]] = {"near": [], "moderate": [], "far": []}
    for candidate in stratified:
        row = row_by_id.get(candidate.candidate_id)
        if row is not None:
            pools[candidate.arm].append(row)
    return pools


def stratified_retrieve(
    store: Store,
    *,
    query: str,
    k_total: int = 6,
    weights: Sequence[int] = DEFAULT_WEIGHTS,
    far_floor: int = DEFAULT_FAR_FLOOR,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """One ``trialerror.retrieve.engine.search`` call (``mode`` governs THIS
    retrieval stage only), split into near/moderate/far arms by EMBEDDING
    DISTANCE from each candidate chunk to the hypothesis query embedding
    (:func:`_distance_tercile_pools` -- reuses M13's lens/stratify
    machinery, per design Section 8.2; falls back to the original RANK
    tercile, :func:`_rank_tercile_pools`, when no candidate has a vector --
    see module docstring), then sampled down to ``k_total`` evidence chunks
    distributed proportionally to ``weights`` (far arm floored at
    ``far_floor`` when the far pool can supply it).

    Returns ``{"near": [...], "moderate": [...], "far": [...], "all":
    near+moderate+far, "query_id": ..., "stratify_method": "distance" |
    "rank_fallback" | "empty"}``, each arm a list of
    ``SearchResponse.results[]`` rows (design Section 7 shape, full
    citation blocks included). Deterministic given a fixed corpus and
    query -- both the underlying search call and M13's ``stratify`` break
    ties on id, so the SAME arms come back for the same inputs every time.
    """
    if len(weights) != 3:
        raise VerifyError(f"stratified_retrieve: weights must be a 3-tuple (near, moderate, far), got {weights!r}")
    total_weight = sum(weights) or 1
    fetch_k = max(k_total * 3, far_floor * 3, 12)
    result = retrieve_engine.search(store, query=query, k=fetch_k, mode=mode)
    rows = result["results"]
    n = len(rows)
    if n == 0:
        return {"near": [], "moderate": [], "far": [], "all": [], "query_id": result["query_id"], "stratify_method": "empty"}

    pools = _distance_tercile_pools(store, query=query, rows=rows)
    stratify_method = "distance"
    if pools is None:
        pools = _rank_tercile_pools(rows)
        stratify_method = "rank_fallback"
    near_pool, moderate_pool, far_pool = pools["near"], pools["moderate"], pools["far"]

    raw_counts = {"near": k_total * weights[0] / total_weight, "moderate": k_total * weights[1] / total_weight, "far": k_total * weights[2] / total_weight}
    counts = {arm: max(1, round(c)) for arm, c in raw_counts.items()}
    if far_pool and far_floor:
        counts["far"] = max(counts["far"], min(far_floor, len(far_pool)))

    arms = {
        "near": near_pool[: min(counts["near"], len(near_pool))],
        "moderate": moderate_pool[: min(counts["moderate"], len(moderate_pool))],
        "far": far_pool[: min(counts["far"], len(far_pool))],
    }
    return {
        **arms,
        "all": arms["near"] + arms["moderate"] + arms["far"],
        "query_id": result["query_id"],
        "stratify_method": stratify_method,
    }


def build_hypothesis_judgment_envelope(
    hypothesis_text: str,
    evidence_row: Mapping[str, Any],
    *,
    answer_length: str = "about 50 words",
) -> dict[str, Any]:
    """One judgment-request envelope for one evidence chunk (a
    ``SearchResponse.results[]`` row) -- ``prompt`` is the vendored
    contracrow "qa" template (:data:`trialerror.verify.labels.CONTRACROW_QA_PROMPT`)
    filled in with this chunk as context and the hypothesis as the claim
    under test; ``labels`` is the fixed 11-word vocabulary the judge's
    response must match exactly."""
    citation = evidence_row["citation"]
    context = f"Excerpt from {citation['title']}\n\n----\n\n{evidence_row['text']}"
    prompt = CONTRACROW_QA_PROMPT.format(context=context, question=hypothesis_text, answer_length=answer_length)
    return {
        "kind": "contracrow",
        "chunk_id": evidence_row["chunk_id"],
        "anchor_id": citation["anchor"]["anchor_id"],
        "hypothesis": hypothesis_text,
        "quote": citation["quote"],
        "labels": list(CONTRACROW_LABELS),
        "prompt": prompt,
    }


def aggregate_labels(
    labeled_evidence: Sequence[Mapping[str, Any]], *, independence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Design Section 8.2 step 4: "label distribution ... hypothesis status
    proposal" + "independence clustering (syndication/near-duplicate
    sources counted once)". Each item in ``labeled_evidence`` needs only a
    ``"label"`` key (one of :data:`trialerror.verify.labels.CONTRACROW_LABELS`).

    Status rule (mirrors ``hypothesis.status``'s DDL enum minus
    ``retired``, which is a lifecycle state this function never proposes):
    any contradiction-side label AND any agreement-side label present ->
    ``"mixed"``; only agreement-side -> ``"supported"``; only
    contradiction-side -> ``"contradicted"``; neither (empty, or entirely
    ``"lack of evidence"``) -> ``"open"``.

    ``independence``, when given, is a precomputed
    :func:`trialerror.verify.independence.independence_stats` result -- folded
    into the return dict unchanged (as ``"independence"``, defaulting to
    ``None`` when the caller doesn't supply one, e.g. every pre-existing
    call site in this codebase and every landed test) so a caller can read
    the EFFECTIVE independent-source count alongside the raw label
    distribution instead of only the raw chunk-labeled count. This
    function itself never computes clustering (no ``Store`` handle here to
    query source rows with) -- see :func:`run_hypothesis_verification` for
    the one call site that does.
    """
    distribution: dict[str, int] = {}
    contra = agree = 0
    for item in labeled_evidence:
        label = item["label"]
        distribution[label] = distribution.get(label, 0) + 1
        polarity = label_polarity(label)
        if polarity < 0:
            contra += 1
        elif polarity > 0:
            agree += 1

    if contra and agree:
        status = "mixed"
    elif agree:
        status = "supported"
    elif contra:
        status = "contradicted"
    else:
        status = "open"

    return {
        "distribution": distribution,
        "status_proposal": status,
        "n_contradicting": contra,
        "n_agreeing": agree,
        "independence": dict(independence) if independence is not None else None,
    }


def run_hypothesis_verification(
    store: Store,
    *,
    hyp_id: str | None = None,
    hypothesis_text: str | None = None,
    query: str | None = None,
    judge: Callable[[Mapping[str, Any]], Any],
    issued_by_launch: str,
    k_total: int = 6,
    weights: Sequence[int] = DEFAULT_WEIGHTS,
    far_floor: int = DEFAULT_FAR_FLOOR,
    mode: str = "hybrid",
    procedure_version: str = "1",
    prereg: bool = False,
    prereg_title: str | None = None,
    executed_procedure: str | None = None,
    executed_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The full pipeline (design Section 8.2 steps 1-5). Exactly one of
    ``hyp_id`` (an existing ``hypothesis`` row -- its ``text`` and
    ``prereg_id`` are read from the store) or ``hypothesis_text`` (an
    inline hypothesis, no ``hypothesis`` row touched or required) must be
    given. ``query`` defaults to the hypothesis text itself.

    ``prereg=True`` commits a blind pre-registration FIRST (design step 1,
    "optional but default for keystones") when the hypothesis does not
    already carry a ``prereg_id`` -- ``executed_procedure``/
    ``executed_params`` (defaulting to a canonical description of this very
    call's own procedure/params) are what get hashed both at commit time
    and again at verdict time for the ``prereg_compliant`` stamp (design:
    "verdict tooling stamps prereg_compliant by recomputing the sha of the
    procedure it actually executed").

    Returns a dict with the proposed ``status``, the label ``distribution``,
    the per-evidence ``evidence`` list actually recorded, the retrieval
    ``arms`` (chunk ids only), the ``independence``
    (:func:`~trialerror.verify.independence.independence_stats`) clustering of
    that same evidence set, and the written ``verdict`` row. When
    ``hyp_id`` was given, ``hypothesis.status`` is updated to the proposed
    status in the same call.
    """
    if (hyp_id is None) == (hypothesis_text is None):
        raise VerifyError("run_hypothesis_verification: exactly one of hyp_id or hypothesis_text must be given")

    prereg_id: str | None = None
    if hyp_id is not None:
        hyp_row = store_get(store, "hypothesis", pk_column="hyp_id", pk_value=hyp_id)
        if hyp_row is None:
            raise VerifyError(f"no such hypothesis: {hyp_id!r}")
        hypothesis_text = hyp_row["text"]
        prereg_id = hyp_row.get("prereg_id")

    query = query or hypothesis_text
    default_params = {"query": query, "k_total": k_total, "weights": list(weights), "far_floor": far_floor, "mode": mode}
    default_procedure = f"trialerror.verify.hypothesis.run_hypothesis_verification:contracrow:v{procedure_version}"
    procedure_used = executed_procedure or default_procedure
    params_used = dict(executed_params) if executed_params is not None else default_params

    if prereg and prereg_id is None:
        committed = commit_prereg(
            store,
            title=prereg_title or f"hypothesis verification: {hypothesis_text[:80]}",
            procedure=procedure_used,
            params=params_used,
        )
        prereg_id = committed["prereg_id"]
        if hyp_id is not None:
            store_update(store, "hypothesis", pk_column="hyp_id", pk_value=hyp_id, changes={"prereg_id": prereg_id})

    arms = stratified_retrieve(store, query=query, k_total=k_total, weights=weights, far_floor=far_floor, mode=mode)

    labeled_evidence: list[dict[str, Any]] = []
    for row in arms["all"]:
        envelope = build_hypothesis_judgment_envelope(hypothesis_text, row)
        verdict_out = judge(envelope)
        label = verdict_out["label"] if isinstance(verdict_out, Mapping) else verdict_out
        note = verdict_out.get("note") if isinstance(verdict_out, Mapping) else None
        if label not in CONTRACROW_LABELS:
            raise VerifyError(f"judge returned a label outside the contracrow vocabulary: {label!r}")
        labeled_evidence.append(
            {"anchor_id": row["citation"]["anchor"]["anchor_id"], "chunk_id": row["chunk_id"], "stance": label, "note": note}
        )

    # Independence clustering (design Section 8.2 step 4, v1 -- this build):
    # EFFECTIVE independent-source count over the evidence actually
    # labeled, not the raw chunk count ("a claim 'supported by 12 chunks'
    # from one book is 1 source"). Computed over the SAME rows the
    # aggregation itself scores, so a caller can line up label counts and
    # independence counts against the identical evidence set.
    independence = independence_stats(store, evidence=labeled_evidence)

    aggregation = aggregate_labels([{"label": e["stance"]} for e in labeled_evidence], independence=independence)
    status = aggregation["status_proposal"]

    prereg_compliant: bool | None = None
    if prereg_id is not None:
        prereg_compliant = check_prereg_compliance(
            store, prereg_id=prereg_id, executed_procedure=procedure_used, executed_params=params_used
        )

    verdict_row = record_verdict(
        store,
        subject_kind="hypothesis",
        subject_id=hyp_id or f"HYP-inline::{hypothesis_text[:60]}",
        procedure="contracrow",
        procedure_version=procedure_version,
        label=status,
        evidence=labeled_evidence,
        prereg_id=prereg_id,
        prereg_compliant=prereg_compliant,
        issued_by_launch=issued_by_launch,
    )

    if hyp_id is not None:
        store_update(store, "hypothesis", pk_column="hyp_id", pk_value=hyp_id, changes={"status": status})

    return {
        "hyp_id": hyp_id,
        "hypothesis_text": hypothesis_text,
        "status": status,
        "distribution": aggregation["distribution"],
        "evidence": labeled_evidence,
        "arms": {arm: [r["chunk_id"] for r in arms[arm]] for arm in ("near", "moderate", "far")},
        "prereg_id": prereg_id,
        "prereg_compliant": prereg_compliant,
        "independence": independence,
        "verdict": verdict_row,
    }
