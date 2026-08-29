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
from trialerror.lens.stratify import ARMS
from trialerror.stores.store import Store

__all__ = ["AGENT_KIND", "PURPOSE", "export_launch_bookable"]

AGENT_KIND = "lens"
PURPOSE = "ideation"


def export_launch_bookable(store: Store, *, round_id: str) -> list[dict[str, Any]]:
    """One launch-bookable dict per lens that has at least one logged
    ``lens_assignment`` row in ``round_id``. Lenses are ordered by their
    first assignment's ``created_ts`` (i.e. the same order they were
    assigned in) — deterministic for a given store, not re-sorted by name."""
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
                "assign_ids": [],
                "arms": {arm: 0 for arm in ARMS},
            }
        entry = by_lens[roster_id]
        entry["assign_ids"].append(row["assign_id"])
        entry["arms"][row["arm"]] += 1

    out: list[dict[str, Any]] = []
    for roster_id in order:
        entry = by_lens[roster_id]
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
                    "assign_ids": entry["assign_ids"],
                    "slice_count": len(entry["assign_ids"]),
                    "arms": entry["arms"],
                },
            }
        )
    return out


def render_json(rows: list[dict[str, Any]]) -> str:
    """Byte-stable rendering for the CLI/log surfaces."""
    return json.dumps(rows, ensure_ascii=False, indent=2)
