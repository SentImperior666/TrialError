"""Launch-bookable export. Integration contract (this build's brief): "M3
budget: lens waves are spawned launches — your assignment-table export
should carry launch-bookable rows (agent_kind, purpose, model_class) so an
orchestrator books straight from it."

One row per LENS (a launch = one agent invocation = one lens's ideation
pass over its own assigned slices for the round — not one row per slice;
:mod:`trialerror.lens.assign` already logs the per-slice detail in
``lens_assignment``, this is the coarser unit ``trialerror.budget.book_launch``
actually books against). Every field name in the returned dict either IS a
``book_launch`` keyword argument, or lives under ``attrs`` (also a
``book_launch`` keyword) — a caller can do
``book_launch(store, session_id=..., program_id=..., est_tokens=..., **row)``
directly; ``est_tokens`` is deliberately NOT included here (cost estimation
is the orchestrator's call, not this module's — the design names no
estimation formula for M13 to invent one against).

TRIALERROR-DEV-NOTE (``agent_kind``/``purpose`` values — judgment calls the
design names as concepts but not literal strings, same posture as
``trialerror.budget.policy``'s own model-policy-table TRIALERROR-DEV-NOTE):
``agent_kind="lens"`` for every row; ``purpose="ideation"`` so
``trialerror.toml``'s ``[models] ideation = "top"`` convention
(``trialerror.budget.policy``'s own docstring example) applies to every lens
launch with zero extra config — a program that wants per-seat policy
(e.g. assumption-busters always top-tier) can still branch on
``attrs.seat`` before calling ``book_launch``, since that value travels
with the row.
"""

from __future__ import annotations

import json
from typing import Any

from trialerror.lens.assign import list_assignments
from trialerror.lens.roster import roster_cards
from trialerror.lens.stratify import ARMS
from trialerror.stores.store import Store

__all__ = ["AGENT_KIND", "PURPOSE", "UNCONSUMED_LAUNCH_STATES", "export_launch_bookable", "lens_log"]

AGENT_KIND = "lens"
PURPOSE = "ideation"


def _slice_doc_id(row: dict[str, Any]) -> str | None:
    """The document one ``lens_assignment`` row assigned, read out of its
    ``slice_spec`` blob. ``None`` for a row whose spec will not parse or
    names no candidate — a slice is reported from the rows that actually
    declare one, never padded with a guess."""
    raw = row.get("slice_spec")
    if isinstance(raw, dict):
        spec: Any = raw
    else:
        try:
            spec = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None
    candidate_id = spec.get("candidate_id") if isinstance(spec, dict) else None
    return str(candidate_id) if candidate_id else None


def export_launch_bookable(store: Store, *, round_id: str) -> list[dict[str, Any]]:
    """One launch-bookable dict per lens that has at least one logged
    ``lens_assignment`` row in ``round_id``. Lenses are ordered by their
    first assignment's ``created_ts`` (i.e. the same order they were
    assigned in) — deterministic for a given store, not re-sorted by name.

    ``attrs`` additionally carries ``arm_mode``, the lens's ``arm`` and its
    ``recipe_cards`` block. ``arm`` is the lens's single arm under
    ``arm_mode="per_lens"`` and ``None`` under ``per_slice``, where a lens
    has no single arm to name — the per-arm counts in ``arms`` are the
    honest answer there, and inventing a "dominant" arm for a mixed slice
    would be a number nothing computed.

    ``attrs.slice_doc_ids`` names the documents the lens was assigned, in
    assignment order. It is the key the retrieval engine reads to SCOPE that
    launch's searches (``retrieve/engine.py::launch_slice_doc_ids``), so a
    booking made from this row carries its own barrier rather than relying
    on the prompt that describes it. The engine can also resolve the slice
    from ``assign_ids``/``roster_id``, which this row carries too; the
    explicit list is here so the scope does not depend on those rows still
    being readable at retrieval time."""
    rows = list_assignments(store, round_id=round_id)
    by_lens: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        roster_id = row["roster_id"]
        if roster_id not in by_lens:
            order.append(roster_id)
            by_lens[roster_id] = {
                "roster_id": roster_id,
                "lens_name": row["lens_name"],
                "vantage": row["vantage"],
                "seat": row["seat"],
                "model_class": row["model_class"],
                "arm_mode": row.get("arm_mode"),
                "recipe_cards": roster_cards(row),
                "assign_ids": [],
                "slice_doc_ids": [],
                "arms": {arm: 0 for arm in ARMS},
            }
        entry = by_lens[roster_id]
        entry["assign_ids"].append(row["assign_id"])
        doc_id = _slice_doc_id(row)
        if doc_id and doc_id not in entry["slice_doc_ids"]:
            entry["slice_doc_ids"].append(doc_id)
        entry["arms"][row["arm"]] += 1

    out: list[dict[str, Any]] = []
    for roster_id in order:
        entry = by_lens[roster_id]
        armed = [arm for arm in ARMS if entry["arms"][arm]]
        out.append(
            {
                "agent_kind": AGENT_KIND,
                "purpose": PURPOSE,
                "model_class": entry["model_class"],
                "workpackage": round_id,
                "attrs": {
                    "round_id": round_id,
                    "roster_id": entry["roster_id"],
                    "lens_name": entry["lens_name"],
                    "vantage": entry["vantage"],
                    "seat": entry["seat"],
                    "arm_mode": entry["arm_mode"],
                    "arm": armed[0] if entry["arm_mode"] == "per_lens" and len(armed) == 1 else None,
                    "recipe_cards": entry["recipe_cards"],
                    "assign_ids": entry["assign_ids"],
                    "slice_doc_ids": entry["slice_doc_ids"],
                    "slice_count": len(entry["assign_ids"]),
                    "arms": entry["arms"],
                },
            }
        )
    return out


def render_json(rows: list[dict[str, Any]]) -> str:
    """Byte-stable rendering for the CLI/log surfaces."""
    return json.dumps(rows, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# the round log: which lens posted, and which never did
# ---------------------------------------------------------------------------

#: Launch states that did not consume a booking. A lens whose only launch
#: was refused or deferred never ran, which is a different fact from a lens
#: that ran and dropped its work on the floor.
UNCONSUMED_LAUNCH_STATES: frozenset[str] = frozenset({"REFUSED", "DEFERRED"})


def _launches_by_lens(store: Store, *, assign_ids_by_lens: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    """Each lens's own launches, resolved every way the booking path writes
    them: a ``launch.attrs`` naming this ``roster_id``, a ``launch.attrs``
    naming any of the lens's ``assign_ids``, or (fix pass N-2)
    ``lens_assignment.lens_launch_id`` -- the link ``budget book
    --assign-id`` records, which carries no attrs at all.

    The first two keys come off :func:`export_launch_bookable`, which is one
    of the two ways an orchestrator books a lens; the third is the other one
    (lane FB-4 item 5), and the same three sources are what the per-launch
    retrieval scope and ``lens_citations_within_slice`` resolve a slice
    from. Reading only the attrs made a lens booked the second way read as
    never posted, and sent the operator to book a second launch for a lens
    that had already run."""
    out: dict[str, list[dict[str, Any]]] = {roster_id: [] for roster_id in assign_ids_by_lens}
    owner_of_assign = {
        assign_id: roster_id
        for roster_id, assign_ids in assign_ids_by_lens.items()
        for assign_id in assign_ids
    }
    rows = store.platform.execute(
        "SELECT launch_id, state, attrs FROM launch WHERE attrs IS NOT NULL ORDER BY booked_ts, launch_id"
    ).fetchall()
    for row in rows:
        try:
            attrs = json.loads(row["attrs"])
        except (TypeError, ValueError):
            continue
        if not isinstance(attrs, dict):
            continue
        owner = attrs.get("roster_id") if attrs.get("roster_id") in out else None
        if owner is None:
            for assign_id in attrs.get("assign_ids") or []:
                if assign_id in owner_of_assign:
                    owner = owner_of_assign[assign_id]
                    break
        if owner is not None:
            out[owner].append({"launch_id": row["launch_id"], "state": row["state"]})

    # The third source: the assignment row's own lens_launch_id. A booking
    # made through `budget book --assign-id` carries no attrs, so nothing
    # above sees it.
    seen = {launch["launch_id"] for launches in out.values() for launch in launches}
    owners_by_launch: dict[str, str] = {}
    if owner_of_assign:
        placeholders = ",".join("?" for _ in owner_of_assign)
        for link in store.ops.execute(
            f"SELECT assign_id, lens_launch_id FROM lens_assignment "
            f"WHERE lens_launch_id IS NOT NULL AND assign_id IN ({placeholders})",
            list(owner_of_assign),
        ).fetchall():
            launch_id = link["lens_launch_id"]
            if launch_id in seen:
                continue
            owners_by_launch.setdefault(launch_id, owner_of_assign[link["assign_id"]])
    if owners_by_launch:
        placeholders = ",".join("?" for _ in owners_by_launch)
        for row in store.platform.execute(
            f"SELECT launch_id, state FROM launch WHERE launch_id IN ({placeholders}) "
            "ORDER BY booked_ts, launch_id",
            list(owners_by_launch),
        ).fetchall():
            out[owners_by_launch[row["launch_id"]]].append(
                {"launch_id": row["launch_id"], "state": row["state"]}
            )
    return out


def _ideas_by_lens(
    store: Store, *, round_id: str, launches_by_lens: dict[str, list[dict[str, Any]]],
    assign_ids_by_lens: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Each lens's idea rows, by either link the writer leaves: the record's
    ``author_launch`` (this lens's launch) or its ``slice_ref.assign_id``
    (the assignment it was written under). Counted as a UNION over idea ids,
    so a record carrying both is one record."""
    owner_of_launch = {
        launch["launch_id"]: roster_id
        for roster_id, launches in launches_by_lens.items()
        for launch in launches
    }
    owner_of_assign = {
        assign_id: roster_id
        for roster_id, assign_ids in assign_ids_by_lens.items()
        for assign_id in assign_ids
    }
    out: dict[str, set[str]] = {roster_id: set() for roster_id in assign_ids_by_lens}
    rows = store.knowledge.execute(
        "SELECT idea_id, author_launch, slice_ref FROM idea WHERE round_id = ?", (round_id,)
    ).fetchall()
    for row in rows:
        owner = owner_of_launch.get(row["author_launch"])
        if owner is None:
            try:
                spec = json.loads(row["slice_ref"]) if row["slice_ref"] else None
            except (TypeError, ValueError):
                spec = None
            if isinstance(spec, dict):
                owner = owner_of_assign.get(spec.get("assign_id"))
        if owner is not None:
            out[owner].add(row["idea_id"])
    return {roster_id: sorted(ids) for roster_id, ids in out.items()}


def lens_log(store: Store, *, round_id: str) -> dict[str, Any]:
    """The round's per-lens reconciliation: one row per lens that was
    assigned a slice, saying whether that lens's work reached the round.

    The gap this closes: ``lens log`` reported the assignment rows and a
    count, and nothing else -- so the gate check that reconciles a round's
    booked lenses against its posts had nothing per-lens to read, fell back
    to counting rows, found no offender and PASSED a round in which one lens
    of three had posted. A dropped lens is budget spent and an arm
    unrepresented, and a round that gates with one unreconciled is reporting
    an arm mix it did not run.

    ``posted`` is "this lens's work reached the round's thread", i.e. it has
    at least one feed post under its own launch. A lens that wrote records
    and never posted them is still an offender -- the records are in the
    store, but the round's own thread does not carry the arm.

    The population is the lenses with at least one logged assignment, which
    is exactly the population :func:`export_launch_bookable` emits bookable
    rows for. A roster row nobody assigned a slice to was never run, which
    is a different finding and not this one's."""
    rows = list_assignments(store, round_id=round_id)
    lenses: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        roster_id = row["roster_id"]
        if roster_id not in lenses:
            order.append(roster_id)
            lenses[roster_id] = {
                "roster_id": roster_id,
                "lens_name": row["lens_name"],
                "seat": row["seat"],
                "vantage": row["vantage"],
                "assign_ids": [],
            }
        lenses[roster_id]["assign_ids"].append(row["assign_id"])

    assign_ids_by_lens = {roster_id: lenses[roster_id]["assign_ids"] for roster_id in order}
    launches_by_lens = _launches_by_lens(store, assign_ids_by_lens=assign_ids_by_lens)
    ideas_by_lens = _ideas_by_lens(
        store, round_id=round_id, launches_by_lens=launches_by_lens, assign_ids_by_lens=assign_ids_by_lens
    )

    all_launch_ids = [l["launch_id"] for launches in launches_by_lens.values() for l in launches]
    posts_by_launch: dict[str, int] = {}
    if all_launch_ids:
        placeholders = ",".join("?" for _ in all_launch_ids)
        for row in store.ops.execute(
            f"SELECT launch_id, COUNT(*) AS n FROM feed_post WHERE launch_id IN ({placeholders}) GROUP BY launch_id",
            all_launch_ids,
        ).fetchall():
            posts_by_launch[row["launch_id"]] = int(row["n"])

    out_rows: list[dict[str, Any]] = []
    offenders: list[dict[str, Any]] = []
    for roster_id in order:
        lens = lenses[roster_id]
        launches = launches_by_lens[roster_id]
        n_feed_posts = sum(posts_by_launch.get(l["launch_id"], 0) for l in launches)
        n_ideas = len(ideas_by_lens[roster_id])
        consumed = [l for l in launches if str(l["state"]).upper() not in UNCONSUMED_LAUNCH_STATES]
        row = {
            "roster_id": roster_id,
            "lens_name": lens["lens_name"],
            "seat": lens["seat"],
            "vantage": lens["vantage"],
            "launch_id": consumed[0]["launch_id"] if consumed else (launches[0]["launch_id"] if launches else None),
            "launch_ids": [l["launch_id"] for l in launches],
            "launch_states": [l["state"] for l in launches],
            "booking_consumed": bool(consumed),
            "n_slices": len(lens["assign_ids"]),
            "n_ideas": n_ideas,
            "n_feed_posts": n_feed_posts,
            "posted": n_feed_posts > 0,
        }
        out_rows.append(row)
        if not row["posted"]:
            if not launches:
                reason = (
                    "no launch is linked to this lens's assignments -- book it from `lens export` "
                    "(its attrs carry the roster_id and the assign_ids) or with "
                    "`budget book --assign-id <assign_id>`, which links the rows directly"
                )
            elif not consumed:
                reason = f"every launch for this lens is {row['launch_states']} -- it never ran"
            elif n_ideas:
                reason = f"wrote {n_ideas} record(s) and posted none of them"
            else:
                reason = "booked, no record written and nothing posted"
            offenders.append({**{k: row[k] for k in ("roster_id", "lens_name", "seat", "launch_id", "n_ideas", "n_feed_posts")}, "reason": reason})

    return {
        "round_id": round_id,
        "rows": out_rows,
        "n_lenses": len(out_rows),
        "n_posted": sum(1 for r in out_rows if r["posted"]),
        "offenders": offenders,
    }
