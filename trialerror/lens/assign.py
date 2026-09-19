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

Two assignment MODES, and the difference between them is the whole point of
``arm_mode``:

- ``per_slice`` (the default, and everything that existed before the mode
  did): the weights split each LENS'S OWN SLICE across the three arms, so
  every lens reads a near/moderate/far mix.
- ``per_lens``: the weights split the ROSTER across the three arms — the
  same :func:`~trialerror.lens.quota.compute_quota_counts`, applied to the lens
  count instead of the slice count — and each lens then draws its WHOLE
  slice from its own single arm. The arm becomes a property of the LENS,
  inherited by every idea it writes, which is what makes "n per arm" mean
  "n lenses" rather than "n documents"; the assumption-buster is pre-placed
  in the far arm (dissent needs a stake of its own) and the control seat in
  the modal arm (a matched comparison has to sit where the mass is).
  ``per_slice`` mixes the arms INSIDE each lens, which measures something
  else and cannot answer a per-arm question at the lens level.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from trialerror.lens.errors import ArmAllocationError, InsufficientCandidatesError
from trialerror.lens.quota import compute_quota_counts, derive_rng, draw_quota
from trialerror.lens.stratify import ARMS, Arm, score_candidates, stratify
from trialerror.lens.vectors import fetch_doc_vectors, program_config
from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "ARM_MODES",
    "modal_arm",
    "allocate_lens_arms",
    "build_assignment_plan",
    "run_assignment",
    "list_assignments",
    "slice_distances",
    "SLICE_DISTANCE_DP",
]

#: ``lens_assignment.arm_mode`` CHECK constraint (ops schema-v9),
#: transcribed for caller-side validation. See the module docstring for what
#: the two modes actually measure.
ARM_MODES: tuple[str, ...] = ("per_slice", "per_lens")

#: Decimal places every distance :func:`slice_distances` reports is rounded
#: to -- AND decided on. The two have to be the same number: a pick made on
#: unrounded float64 while the output shows nine places is a pick the output
#: cannot explain, and the command's whole purpose is that two runs are
#: compared by one hash (lane FB-7 fix pass, V-7).
SLICE_DISTANCE_DP = 9

#: Seats :func:`allocate_lens_arms` pre-places before the seeded draw, and
#: where each goes. ``"@modal"`` resolves to whichever arm the roster quota
#: gave the most lenses (:func:`modal_arm`).
_PRE_PLACED_SEATS: dict[str, str] = {"assumption_buster": "far", "control": "@modal"}


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


def modal_arm(quota: Mapping[str, int]) -> Arm:
    """The arm the quota gave the most lenses; ties broken by arm order
    (near, moderate, far) so the answer never depends on dict iteration.
    This is where the control seat sits: a matched comparison belongs where
    the mass of the roster is, not in whichever arm happens to be thin."""
    return max(ARMS, key=lambda arm: (quota.get(arm, 0), -ARMS.index(arm)))


def allocate_lens_arms(
    lenses: Sequence[Mapping[str, Any]],
    *,
    seed: str,
    weights: Sequence[int] = (40, 40, 20),
    far_lens_floor: int = 2,
) -> tuple[list[Arm], dict[Arm, int]]:
    """Split the ROSTER across the three arms (``arm_mode="per_lens"``).

    Returns ``(arms, quota)`` — one arm per lens, in the order ``lenses``
    was given, plus the roster-level quota those arms were drawn against.
    The quota is :func:`~trialerror.lens.quota.compute_quota_counts` over the LENS
    COUNT, so the familiar numbers fall straight out of the existing
    apportionment: a roster of 6 at 40/40/20 with a floor of 2 far lenses is
    3 near / 1 moderate / 2 far, a roster of 12 is 5 / 5 / 2.

    Seats in :data:`_PRE_PLACED_SEATS` are placed FIRST and count against
    the quota; every remaining lens is drawn into what is left by one
    seeded shuffle (``derive_rng(seed, salt="arm-per-lens")`` — its own
    stream, so the arm draw never consumes the per-lens slice streams).

    That separation is not per-lens stability across roster changes
    (finding V-9). At the same seed and pool, growing a roster from 6 to 7
    moves the quota from 3/1/2 to 3/2/2, reshuffles the bag and depletes
    the shared arm pools in roster order, so other lenses can change both
    their arm and their drawn slice. A seed reproduces THE ROUND IT WAS
    PRE-REGISTERED FOR; it does not survive adding a lens to that round.

    Raises :class:`~trialerror.lens.errors.ArmAllocationError` if the
    pre-placed seats over-subscribe an arm, or if the quota does not sum to
    the roster size (which is how ``compute_quota_counts`` reports a floor
    larger than the roster itself).

    One conflict between the two contract documents, resolved deliberately
    and recorded here (finding V-10): the charter amendment's control-seat
    item says CONTROL counts never toward the arm mix or the far floor,
    while the design says buster and CONTROL are both counted against the
    quota — and the design's own worked round-0 numbers (a roster of 6
    splitting 3/1/2 with CONTROL near) only come out if CONTROL is counted.
    This function follows the design and counts it; the amendment item is
    flagged for an add-only clarification."""
    quota = compute_quota_counts(len(lenses), weights=weights, far_floor=far_lens_floor)
    remaining: dict[Arm, int] = dict(quota)
    modal = modal_arm(quota)

    placed: dict[int, Arm] = {}
    for index, lens in enumerate(lenses):
        target = _PRE_PLACED_SEATS.get(str(lens.get("seat") or "standard"))
        if target is None:
            continue
        arm: Arm = modal if target == "@modal" else target  # type: ignore[assignment]
        if remaining.get(arm, 0) <= 0:
            raise ArmAllocationError(
                f"allocate_lens_arms: seat {lens.get('seat')!r} (lens {lens.get('roster_id')!r}) must sit "
                f"in the {arm!r} arm, but the roster quota {dict(quota)!r} has no {arm!r} seat left — "
                "raise --far-floor, add lenses, or drop a pre-placed seat"
            )
        remaining[arm] -= 1
        placed[index] = arm

    bag: list[Arm] = [arm for arm in ARMS for _ in range(remaining[arm])]
    free = [i for i in range(len(lenses)) if i not in placed]
    if len(bag) != len(free):
        raise ArmAllocationError(
            f"allocate_lens_arms: roster quota {dict(quota)!r} seats {sum(quota.values())} lens(es) but the "
            f"roster has {len(lenses)} — a far-lens floor of {far_lens_floor} does not fit this roster"
        )
    derive_rng(seed, salt="arm-per-lens").shuffle(bag)
    for index, arm in zip(free, bag):
        placed[index] = arm

    return [placed[i] for i in range(len(lenses))], quota


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
    arm_mode: str = "per_slice",
    config: Any = None,
) -> dict[str, Any]:
    """Pure planner. ``lenses`` is processed in the given order (each item
    at least ``{"roster_id": ...}``, plus ``"seat"`` when ``arm_mode`` is
    ``per_lens``); returns a JSON-serializable dict with the full stratified
    candidate list plus, per lens, its arm, quota, far floor and drawn
    slices. Raises
    :class:`~trialerror.lens.errors.InsufficientCandidatesError` (via
    :func:`~trialerror.lens.quota.draw_quota`) the moment any lens's draw cannot
    be satisfied from what remains in its arm's pool — no partial plan is
    returned in that case.

    ``arm_mode`` selects which of the two semantics the module docstring
    describes applies. Under ``per_lens``, ``far_floor`` changes what it
    counts: it is the minimum number of far LENSES on the roster (the
    roster-level floor), while each lens's own recorded ``far_floor``
    becomes ``slices_per_lens`` for a far lens and 0 for every other — which
    is exactly what keeps the existing ``far_arm_floor_honored`` doctor
    check true in both modes without it having to know a mode exists.

    ``config`` is the program config the candidate scan reads
    ``[retrieve] numpy_fastpath`` from. This function holds no store, so it
    cannot read one for itself; :func:`run_assignment` resolves it and
    passes it down (lane FB-7 fix pass, V-1)."""
    if arm_mode not in ARM_MODES:
        raise ValueError(f"build_assignment_plan: arm_mode must be one of {ARM_MODES!r}, got {arm_mode!r}")

    scores = score_candidates(candidates, home, config=config)
    stratified = stratify(scores, cluster_of=cluster_of)

    pools: dict[str, list[str]] = {arm: [] for arm in ARMS}
    for sc in stratified:
        pools[sc.arm].append(sc.candidate_id)
    pools = _apply_inter_cluster_mandate(
        pools, inter_cluster_mandate=inter_cluster_mandate, cluster_of=cluster_of, home_cluster=home_cluster
    )

    by_id = {sc.candidate_id: sc for sc in stratified}

    if arm_mode == "per_lens":
        lens_arms, roster_quota = allocate_lens_arms(
            lenses, seed=seed, weights=weights, far_lens_floor=far_floor
        )
    else:
        lens_arms, roster_quota = [], {}
        slice_quota = compute_quota_counts(slices_per_lens, weights=weights, far_floor=far_floor)

    lens_plans: list[dict[str, Any]] = []
    for index, lens in enumerate(lenses):
        roster_id = lens["roster_id"]
        if arm_mode == "per_lens":
            lens_arm: Arm | None = lens_arms[index]
            quota: dict[str, int] = {arm: (slices_per_lens if arm == lens_arm else 0) for arm in ARMS}
            lens_far_floor = slices_per_lens if lens_arm == "far" else 0
        else:
            lens_arm = None
            quota = dict(slice_quota)
            lens_far_floor = far_floor
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
        lens_plans.append(
            {
                "roster_id": roster_id,
                "seat": lens.get("seat") or "standard",
                "arm": lens_arm,
                "quota": dict(quota),
                "far_floor": lens_far_floor,
                "slices": slices,
            }
        )

    return {
        "seed": seed,
        "weights": list(weights),
        "far_floor": far_floor,
        "arm_mode": arm_mode,
        "far_lens_floor": far_floor if arm_mode == "per_lens" else None,
        "roster_quota": dict(roster_quota) if arm_mode == "per_lens" else None,
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
    arm_mode: str = "per_slice",
    launch_id: str | None = None,
    now_ts: str | None = None,
    config: Any = None,
) -> dict[str, Any]:
    """Fetch doc-pooled vectors, build the plan, then write one
    ``lens_assignment`` row per (lens, drawn candidate). Returns
    ``{"plan": <pure plan dict>, "rows": [<written lens_assignment rows>]}``.

    Each row carries the mode it was written under (``arm_mode``), the
    roster-level far-LENS floor (``far_lens_floor``, ``NULL`` outside
    ``per_lens``) and the lens's card block (``recipe_cards``, copied off
    the ``lenses`` mapping) — so a doctor check or an export reads one
    table and never has to re-derive a decision that was made here.

    ``home_doc_ids``/``candidate_doc_ids`` that resolve to no vector
    (:mod:`trialerror.lens.vectors`'s "missing is absent" contract) are simply
    excluded rather than raising HERE — :func:`~trialerror.lens.stratify.score_candidates`
    raises :class:`~trialerror.lens.errors.MissingEmbeddingError` if that leaves
    either side empty, which is the actual failure condition worth naming.

    ``config`` is resolved off the store when the caller passes none
    (:func:`trialerror.lens.vectors.program_config`), so ``[retrieve]
    numpy_fastpath = "off"`` reaches both the pooling and the candidate
    scan -- lane FB-7 fix pass, V-1.
    """
    config = program_config(store, config)
    home = fetch_doc_vectors(store, model_key=model_key, doc_ids=home_doc_ids, config=config)
    candidates = fetch_doc_vectors(
        store, model_key=model_key, doc_ids=candidate_doc_ids, config=config
    )

    plan = build_assignment_plan(
        config=config,
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
        arm_mode=arm_mode,
    )

    ts = now_ts or now()
    weights_json = json.dumps(list(weights))
    cards_by_roster = {
        lens["roster_id"]: lens.get("recipe_cards") for lens in lenses if lens.get("recipe_cards")
    }
    rows: list[dict[str, Any]] = []
    for lens_plan in plan["lenses"]:
        roster_id = lens_plan["roster_id"]
        cards = cards_by_roster.get(roster_id)
        cards_json = cards if isinstance(cards, str) or cards is None else json.dumps(list(cards), ensure_ascii=False)
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
                # Per-LENS floor (slices), not the roster-level far-lens
                # floor -- see build_assignment_plan's docstring for why
                # `far_arm_floor_honored` stays true in both modes.
                "far_floor": lens_plan["far_floor"],
                "arm_mode": arm_mode,
                "far_lens_floor": plan["far_lens_floor"],
                "recipe_cards": cards_json,
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


# ---------------------------------------------------------------------------
# slice distances (lane FB-7 item 7)
# ---------------------------------------------------------------------------


def _canonical_json(payload: Any) -> str:
    """The canonical form a hash is taken over: sorted keys, no whitespace
    in the separators, UTF-8. Written out here rather than left to
    ``json.dumps``'s defaults because a hash two runs are compared by is a
    promise about the BYTES, and ``json.dumps``'s default separators put a
    space after every comma."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def slice_distances(
    store: Store,
    *,
    round_id: str,
    home_doc_ids: Sequence[str],
    model_key: str,
    lens_names: Sequence[str] | None = None,
    config: Any = None,
) -> dict[str, Any]:
    """Per lens: which home its slice sits nearest, and which slice document
    is farthest from that home. Lane FB-7 item 7. READ-ONLY.

    **What this is for.** A round pre-registered a rule of the form "the
    slice document farthest from the lens's home medoid (the home medoid
    nearest to the slice), ties to the lower id" and then had to compute it
    with an outside script that read ``lens_assignment.slice_spec``, pooled
    document vectors and cosined them by hand. A pre-registered rule whose
    answer comes from a script nobody else has is a rule the round cannot
    reproduce, and the harness already holds every piece of it.

    The two steps, in the rule's own order:

    1. For each home document, its MEAN cosine distance to the lens's slice
       documents. The nearest home is the one with the smallest mean --
       ties broken on the LOWER document id, which is the tie rule stated
       here because the caller's rule states it.
    2. Each slice document's distance to THAT home, and ``farthest`` is the
       largest -- ties again to the lower id.

    Distances are cosine distances between doc-pooled vectors
    (:mod:`trialerror.lens.vectors`), which is the same metric
    :func:`trialerror.lens.stratify.score_candidates` cut the arms with, so
    a rule expressed over "distance" means here what it meant there.
    Documents with no resolvable vector under ``model_key`` are reported in
    ``unvectorized`` and take no part in any mean -- silently dropping them
    would move a medoid without saying so.

    ``canonical_sha256`` is SHA-256 over the canonical JSON of
    ``{lens_name: {"home": <nearest home>, "farthest": <farthest slice
    doc>}}`` -- sorted keys, ``,``/``:`` separators, UTF-8 -- so two runs,
    or a run and an outside script, are compared by ONE value rather than
    by reading two tables side by side.

    ``config`` is resolved off the store when none is passed, so the
    pooling scan behind these distances honours ``[retrieve]
    numpy_fastpath`` (lane FB-7 fix pass, V-1).

    Writes nothing.
    """
    import hashlib

    from trialerror.lens.stratify import cosine_distance

    config = program_config(store, config)
    homes = list(dict.fromkeys(str(d) for d in home_doc_ids))
    if not homes:
        raise InsufficientCandidatesError(
            "lens slice-distances: no --home document given; the rule is defined against a home set"
        )
    rows = list_assignments(store, round_id=round_id)
    if lens_names:
        wanted = {str(n) for n in lens_names}
        rows = [r for r in rows if str(r.get("lens_name")) in wanted]

    by_lens: dict[str, list[str]] = {}
    for row in rows:
        try:
            spec = json.loads(row.get("slice_spec") or "{}")
        except (TypeError, ValueError):
            spec = {}
        candidate = spec.get("candidate_id")
        if candidate is None:
            continue
        bucket = by_lens.setdefault(str(row.get("lens_name")), [])
        if str(candidate) not in bucket:
            bucket.append(str(candidate))

    wanted_docs = list(dict.fromkeys([*homes, *(d for docs in by_lens.values() for d in docs)]))
    vectors = fetch_doc_vectors(store, model_key=model_key, doc_ids=wanted_docs, config=config)
    unvectorized = sorted(d for d in wanted_docs if d not in vectors)

    lenses: dict[str, Any] = {}
    canonical: dict[str, dict[str, str | None]] = {}
    for lens_name in sorted(by_lens):
        slice_docs = sorted(by_lens[lens_name])
        scoreable = [d for d in slice_docs if d in vectors]
        home_means: dict[str, float | None] = {}
        for home in homes:
            if home not in vectors or not scoreable:
                home_means[home] = None
                continue
            # Rounded HERE, before anything is decided on it (lane FB-7 fix
            # pass, V-7). Both picks below used to be made on unrounded
            # float64 while every distance was reported to
            # :data:`SLICE_DISTANCE_DP`, so a per-component change of 1.2e-16
            # in a home vector -- which is inside the divergence between
            # ``mean_pool_l2``'s own two paths for a document of 256 chunks
            # or more -- flipped ``nearest_home`` and the hash while every
            # printed number stayed identical. The command's stated purpose
            # is that two runs are compared by ONE value; a pick decided
            # finer than the output can express is a difference the output
            # cannot explain.
            home_means[home] = round(
                sum(cosine_distance(vectors[doc], vectors[home]) for doc in scoreable)
                / len(scoreable),
                SLICE_DISTANCE_DP,
            )
        ranked = sorted(
            ((mean, home) for home, mean in home_means.items() if mean is not None),
            key=lambda pair: (pair[0], pair[1]),  # ties -> lower id
        )
        nearest_home = ranked[0][1] if ranked else None
        per_doc: dict[str, float | None] = {}
        if nearest_home is not None:
            for doc in slice_docs:
                per_doc[doc] = (
                    round(cosine_distance(vectors[doc], vectors[nearest_home]), SLICE_DISTANCE_DP)
                    if doc in vectors
                    else None
                )
        farthest = None
        scored_docs = [(d, v) for d, v in per_doc.items() if v is not None]
        if scored_docs:
            farthest = sorted(scored_docs, key=lambda pair: (-pair[1], pair[0]))[0][0]
        lenses[lens_name] = {
            "slice_doc_ids": slice_docs,
            "home_mean_distance": dict(home_means),
            "nearest_home": nearest_home,
            "distance_to_nearest_home": dict(per_doc),
            "farthest": farthest,
            "n_unvectorized": sum(1 for d in slice_docs if d not in vectors),
        }
        canonical[lens_name] = {"home": nearest_home, "farthest": farthest}

    payload = _canonical_json(canonical)
    return {
        "round_id": round_id,
        "model_key": model_key,
        "home": homes,
        "lenses": lenses,
        "unvectorized": unvectorized,
        "canonical": canonical,
        "canonical_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
