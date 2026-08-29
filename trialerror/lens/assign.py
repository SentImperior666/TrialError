"""Orchestration: home/candidate vectors -> stratify -> per-lens seeded
quota draw -> ``lens_assignment`` rows. Design Section 9.6/12 (M13 row):
"corpus-slice assignment ... seeded + logged in ``lens_assignment`` rows ...
assignment table logged BEFORE any spawn" (build brief).

Split in two, deliberately:

- :func:`build_assignment_plan` is a PURE function (no ``Store``, no ids,
  no timestamps) over already-fetched vectors — this is what "stratify on
  fixture corpus reproduces byte-identical arms from same seed" is tested
  against directly: two calls with identical arguments produce
  ``json.dumps``-identical output, full stop, with no ULID/``now()``
  nondeterminism anywhere in the comparison.
- :func:`run_assignment` is the DB-touching wrapper: fetches doc-pooled
  vectors (:mod:`trialerror.lens.vectors`), calls the pure planner, then WRITES
  one ``lens_assignment`` row per (lens, drawn candidate) pair — freshly
  generated ``assign_id``/``created_ts`` per row, same as every other
  module's write path (a second run with the same seed reproduces the same
  LOGICAL plan, never byte-identical database rows — ids/timestamps are
  never claimed to be reproducible, only the assignment decisions are).

No duplicate slices across a round: each lens is processed in the given
order and drawn candidates are removed from the shared arm pools before the
next lens draws — a candidate can be assigned to at most one lens per round
by construction, never by a post-hoc check.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from trialerror.lens.errors import InsufficientCandidatesError
from trialerror.lens.quota import compute_quota_counts, derive_rng, draw_quota
from trialerror.lens.stratify import ARMS, score_candidates, stratify
from trialerror.lens.vectors import fetch_doc_vectors
from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["build_assignment_plan", "run_assignment", "list_assignments"]


def _apply_inter_cluster_mandate(
    pools: dict[str, list[str]],
    *,
    inter_cluster_mandate: bool,
    cluster_of: Mapping[str, str] | None,
    home_cluster: str | None,
) -> dict[str, list[str]]:
    if not inter_cluster_mandate:
        return pools
    if not cluster_of or home_cluster is None:
        raise ValueError(
            "inter_cluster_mandate=True requires both cluster_of and home_cluster "
            "(cannot honor a mandate to cross cluster boundaries with no cluster labels)"
        )
    filtered = dict(pools)
    filtered["far"] = [cid for cid in pools["far"] if cluster_of.get(cid) != home_cluster]
    return filtered


def build_assignment_plan(
    *,
    candidates: Mapping[str, Sequence[float]],
    home: Mapping[str, Sequence[float]],
    lenses: Sequence[Mapping[str, Any]],
    slices_per_lens: int,
    seed: str,
    weights: Sequence[int] = (40, 40, 20),
    far_floor: int = 2,
    inter_cluster_mandate: bool = False,
    cluster_of: Mapping[str, str] | None = None,
    home_cluster: str | None = None,
) -> dict[str, Any]:
    """Pure planner. ``lenses`` is processed in the given order (each item
    at least ``{"roster_id": ...}``); returns a JSON-serializable dict with
    the full stratified candidate list plus, per lens, its quota and the
    slices drawn for it. Raises
    :class:`~trialerror.lens.errors.InsufficientCandidatesError` (via
    :func:`~trialerror.lens.quota.draw_quota`) the moment any lens's draw cannot
    be satisfied from what remains in its arm's pool — no partial plan is
    returned in that case."""
    scores = score_candidates(candidates, home)
    stratified = stratify(scores, cluster_of=cluster_of)

    pools: dict[str, list[str]] = {arm: [] for arm in ARMS}
    for sc in stratified:
        pools[sc.arm].append(sc.candidate_id)
    pools = _apply_inter_cluster_mandate(
        pools, inter_cluster_mandate=inter_cluster_mandate, cluster_of=cluster_of, home_cluster=home_cluster
    )

    by_id = {sc.candidate_id: sc for sc in stratified}
    quota = compute_quota_counts(slices_per_lens, weights=weights, far_floor=far_floor)

    lens_plans: list[dict[str, Any]] = []
    for lens in lenses:
        roster_id = lens["roster_id"]
        rng = derive_rng(seed, salt=roster_id)
        try:
            drawn = draw_quota(pools, quota, rng)
        except InsufficientCandidatesError as exc:
            raise InsufficientCandidatesError(
                f"build_assignment_plan: lens {roster_id!r} — {exc}"
            ) from exc
        slices: list[dict[str, Any]] = []
        for arm in ARMS:
            for cid in drawn.get(arm, ()):
                sc = by_id[cid]
                slices.append(sc.to_dict())
                pools[arm].remove(cid)
        # Deterministic slice order within a lens: (arm rank, candidate_id)
        # — independent of `random.Random.sample`'s own internal output
        # order, which is not part of this module's determinism contract.
        slices.sort(key=lambda s: (ARMS.index(s["arm"]), s["candidate_id"]))
        lens_plans.append({"roster_id": roster_id, "quota": dict(quota), "slices": slices})

    return {
        "seed": seed,
        "weights": list(weights),
        "far_floor": far_floor,
        "inter_cluster_mandate": inter_cluster_mandate,
        "home_cluster": home_cluster,
        "slices_per_lens": slices_per_lens,
        "candidates": [sc.to_dict() for sc in stratified],
        "lenses": lens_plans,
    }


def plan_to_json(plan: Mapping[str, Any]) -> str:
    """Canonical, byte-stable JSON rendering of a plan — sorted keys, fixed
    separators, so two independently-built plans from the same inputs
    compare equal as strings, not just as Python objects (the literal
    "byte-identical" acceptance wording)."""
    return json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def run_assignment(
    store: Store,
    *,
    round_id: str,
    model_key: str,
    home_doc_ids: Sequence[str],
    candidate_doc_ids: Sequence[str],
    lenses: Sequence[Mapping[str, Any]],
    slices_per_lens: int,
    seed: str,
    weights: Sequence[int] = (40, 40, 20),
    far_floor: int = 2,
    inter_cluster_mandate: bool = False,
    cluster_of: Mapping[str, str] | None = None,
    home_cluster: str | None = None,
    launch_id: str | None = None,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Fetch doc-pooled vectors, build the plan, then write one
    ``lens_assignment`` row per (lens, drawn candidate). Returns
    ``{"plan": <pure plan dict>, "rows": [<written lens_assignment rows>]}``.

    ``home_doc_ids``/``candidate_doc_ids`` that resolve to no vector
    (:mod:`trialerror.lens.vectors`'s "missing is absent" contract) are simply
    excluded rather than raising HERE — :func:`~trialerror.lens.stratify.score_candidates`
    raises :class:`~trialerror.lens.errors.MissingEmbeddingError` if that leaves
    either side empty, which is the actual failure condition worth naming.
    """
    home = fetch_doc_vectors(store, model_key=model_key, doc_ids=home_doc_ids)
    candidates = fetch_doc_vectors(store, model_key=model_key, doc_ids=candidate_doc_ids)

    plan = build_assignment_plan(
        candidates=candidates,
        home=home,
        lenses=lenses,
        slices_per_lens=slices_per_lens,
        seed=seed,
        weights=weights,
        far_floor=far_floor,
        inter_cluster_mandate=inter_cluster_mandate,
        cluster_of=cluster_of,
        home_cluster=home_cluster,
    )

    ts = now_ts or now()
    weights_json = json.dumps(list(weights))
    rows: list[dict[str, Any]] = []
    for lens_plan in plan["lenses"]:
        roster_id = lens_plan["roster_id"]
        for rank, slice_ in enumerate(lens_plan["slices"]):
            row = {
                "assign_id": new_id("ASGN"),
                "roster_id": roster_id,
                "slice_spec": json.dumps(
                    {
                        "round_id": round_id,
                        "candidate_id": slice_["candidate_id"],
                        "distance_score": slice_["distance_score"],
                        "cluster_id": slice_["cluster_id"],
                        "rank": rank,
                    },
                    ensure_ascii=False,
                ),
                "arm": slice_["arm"],
                "weights": weights_json,
                "far_floor": far_floor,
                "inter_cluster_mandate": int(inter_cluster_mandate),
                "seed": seed,
                "launch_id": launch_id,
                "created_ts": ts,
            }
            rows.append(insert(store, "lens_assignment", row))

    return {"plan": plan, "rows": rows}


def list_assignments(store: Store, *, round_id: str) -> list[dict[str, Any]]:
    """Every ``lens_assignment`` row for ``round_id``'s roster, joined back
    to its lens (``lens_roster``), oldest-first. ``round_id`` lives on
    ``lens_roster`` (design Section 4.2) — ``lens_assignment`` itself only
    carries ``roster_id``, so this is a join, not a direct column filter."""
    rows = store.ops.execute(
        """
        SELECT a.*, r.round_id, r.lens_name, r.vantage, r.seat, r.model_class,
               a.rowid AS _rowid
        FROM lens_assignment a
        JOIN lens_roster r ON a.roster_id = r.roster_id
        WHERE r.round_id = ?
        ORDER BY a.created_ts ASC, _rowid ASC
        """,
        (round_id,),
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]
