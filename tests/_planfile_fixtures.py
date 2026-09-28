"""Not a test module -- shared builders for the ``lens assign --plan-file``
tests (``tests/test_lens_planfile*.py``).

One fixture round, built to pass every rule the plan-file verb and the four
assignment doctor checks hold it to:

- seven lenses: five standard, one assumption-buster, one control;
- the six non-control lenses split 3 near / 1 moderate / 2 far, which is
  ``compute_quota_counts(6, (40, 40, 20), far_floor=2)``; the buster is one of
  the two far lenses, and the control sits in the modal arm (near);
- card blocks that satisfy the rotation rules: two cards per standard lens,
  four cards in play, each held by at least two standard lenses, NEGATE on the
  buster alone, none on the control;
- eight documents per lens, each lens's first four ranks one half and its last
  four the other, so a lens can be launched twice over its own halves.

The plan dict is in the plan-file format (``trialerror-plan-file/1``) with the
planner-side ``annex`` a real planner would carry, including the projection
hash the verb must reproduce.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from trialerror.lens.planfile import PLAN_FILE_FORMAT, canonical_bytes, plan_projection, plan_sha256, sha256_of
from trialerror.lens.roster import add_lens
from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._lens_fixtures import bootstrap_launch

ROUND_ID = "R-TEST-1"
SEED = "seed-1"
ROWS_PER_LENS = 8

#: (lens_name, seat, cards, arm). Non-control arms: 3 near, 1 moderate, 2 far.
LENSES: tuple[tuple[str, str, list[str] | None, str], ...] = (
    ("lens-a", "standard", ["CARD-A", "CARD-B"], "near"),
    ("lens-b", "standard", ["CARD-C", "CARD-D"], "near"),
    ("lens-c", "standard", ["CARD-A", "CARD-C"], "near"),
    ("lens-d", "standard", ["CARD-B", "CARD-D"], "moderate"),
    ("lens-e", "standard", ["CARD-A", "CARD-D"], "far"),
    ("lens-f", "assumption_buster", ["NEGATE"], "far"),
    ("lens-g", "control", None, "near"),
)


def add_documents(store: Store, *, launch_id: str, n: int, kind: str = "paper") -> list[str]:
    """``n`` documents under one new source of ``kind``. No chunks and no
    vectors: the plan-file verb draws nothing, so it needs none."""
    source_id = new_id("SRC")
    insert(
        store, "source",
        {"source_id": source_id, "kind": kind, "title": f"fixture {kind}", "license_tier": "open",
         "acquisition_route": "web", "request_state": "indexed", "registered_ts": now(),
         "registered_by_launch": launch_id},
    )
    doc_ids = []
    for i in range(n):
        doc_id = new_id("DOC")
        insert(
            store, "document",
            {"doc_id": doc_id, "source_id": source_id, "rel_path": f"archive/{kind}_{i}.md",
             "media_type": "text/markdown", "normalizer_id": "n", "normalizer_version": "1",
             "sha256": doc_id, "status": "indexed"},
        )
        doc_ids.append(doc_id)
    return doc_ids


def add_roster(store: Store, lenses=LENSES, *, round_id: str = ROUND_ID) -> dict[str, dict]:
    return {
        name: add_lens(
            store, round_id=round_id, lens_name=name, vantage=f"vantage of {name}",
            model_class="top", seat=seat, recipe_cards=cards,
        )
        for name, seat, cards, _arm in lenses
    }


def rehash(plan: dict[str, Any]) -> dict[str, Any]:
    """Refresh the annex's projection hash and the plan's own hash after an edit."""
    if isinstance(plan.get("annex"), dict):
        plan["annex"]["projection_sha256"] = sha256_of(plan_projection(plan))
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def make_plan(
    docs_by_lens: dict[str, list[str]],
    lenses=LENSES,
    *,
    round_id: str = ROUND_ID,
    weights=(40, 40, 20),
    far_lens_floor: int = 2,
) -> dict[str, Any]:
    plan_lenses = []
    for index, (name, _seat, _cards, arm) in enumerate(lenses):
        docs = docs_by_lens[name]
        rows = [
            {
                "candidate_id": doc_id,
                "cluster_id": f"C{(index + rank) % 5 + 1}",
                "rank": rank,
                "distance_score": round(0.1 * (index + 1) + 0.01 * rank, 9),
                "extra": {"set_id": f"SET-{index + 1}", "home_cluster": f"C{index % 5 + 1}", "set_distance": 0.25 * (index + 1)},
            }
            for rank, doc_id in enumerate(docs)
        ]
        plan_lenses.append(
            {"lens_name": name, "arm": arm, "far_floor": len(rows) if arm == "far" else 0, "rows": rows}
        )
    plan = {
        "format": PLAN_FILE_FORMAT,
        "round_id": round_id,
        "seed": SEED,
        "weights": list(weights),
        "far_lens_floor": far_lens_floor,
        "arm_mode": "per_lens",
        "inter_cluster_mandate": True,
        "salt_scheme": "lens-name",
        "lenses": plan_lenses,
        "annex": {"planner": {"version": "fixture-1"}, "projection_sha256": None},
        "plan_sha256": None,
    }
    return rehash(plan)


def write_plan_file(path: Path, plan: dict[str, Any]) -> Path:
    path.write_bytes(canonical_bytes(plan))
    return path


def build_round(store: Store, lenses=LENSES, *, round_id: str = ROUND_ID, **plan_kwargs) -> dict[str, Any]:
    """Launch, documents, roster and a valid plan for one fixture round."""
    launch_id = bootstrap_launch(store)
    doc_ids = add_documents(store, launch_id=launch_id, n=ROWS_PER_LENS * len(lenses))
    docs_by_lens = {
        name: doc_ids[i * ROWS_PER_LENS:(i + 1) * ROWS_PER_LENS] for i, (name, *_rest) in enumerate(lenses)
    }
    roster = add_roster(store, lenses, round_id=round_id)
    plan = make_plan(docs_by_lens, lenses, round_id=round_id, **plan_kwargs)
    return {"launch_id": launch_id, "docs_by_lens": docs_by_lens, "roster": roster, "plan": plan}


def edited(plan: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(plan)


def lens_entry(plan: dict[str, Any], name: str) -> dict[str, Any]:
    return next(lens for lens in plan["lenses"] if lens["lens_name"] == name)
