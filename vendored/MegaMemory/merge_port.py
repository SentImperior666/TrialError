# upstream: https://github.com/0xK3vin/MegaMemory
# commit: e0bb3c270d7fb4f6f280ae4685e0c538eb225d93
# license: MIT
# verified-by: build-M11
# date: 2026-08-29
"""A Python port of MegaMemory's two-way merge CLASSIFICATION algorithm
(``src/merge.ts``, class ``MergeEngine.performMerge`` — see
``docs/mining/G01-memory-1__MegaMemory.md`` and
``docs/STACK_DECISIONS_draft.md``'s "adopt-code for merge.ts" row).

What's ported: the per-id classification pass — for every canonical id
present on either side, decide ``left_only`` / ``right_only`` / ``identical``
(dedup, keep one copy) / ``conflict`` (keep BOTH, suffixed ``::left``/
``::right``, grouped under a freshly-minted group id, flagged for later
resolution — never silently resolved). This is the load-bearing half of
the upstream algorithm and is genuinely storage-agnostic (the upstream
mining note: "the diff/conflict-group logic is ~250 lines and mostly
storage-agnostic").

What's deliberately NOT ported: upstream's node/edge KNOWLEDGE-GRAPH
machinery (``leftEdgeMap``/``rightEdgeMap``, the deferred-edge remapping
pass, ``insertEdgeRaw``) and its ``needs_merge``/``merge_group`` SQLITE
COLUMNS on a ``nodes`` table. TrialError's ``memory_item`` (a flat, non-graph
table — see ``trialerror/stores/schema/ops.py``) has no edges and no such
columns; ``trialerror/memory/merge.py`` is the harness-specific glue that reads
``memory_item`` rows, calls :func:`classify` below, and encodes the result
back onto ``memory_item`` using ONLY columns that table already has (the
group id + side folded into the suffixed ``memory_item_id`` itself, exactly
mirroring upstream's own ``id::left``/``id::right`` convention, plus
``status='needs_merge'`` as the flag column upstream spends a dedicated
``needs_merge`` INTEGER on). Also not ported: upstream's SQLite I/O layer
(``KnowledgeDB``), its CLI, and its embedding/web-explorer pieces — none of
that is the algorithm this build brief names.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Generic, Hashable, Mapping, TypeVar

__all__ = [
    "MERGE_SUFFIX_LEFT",
    "MERGE_SUFFIX_RIGHT",
    "strip_merge_suffix",
    "has_merge_suffix",
    "ConflictGroup",
    "MergeClassification",
    "classify",
]

#: Verbatim from upstream (``merge.ts`` lines 5-6).
MERGE_SUFFIX_LEFT = "::left"
MERGE_SUFFIX_RIGHT = "::right"

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


def strip_merge_suffix(item_id: str) -> str:
    """Port of ``stripMergeSuffix`` (merge.ts l.27-31)."""
    if item_id.endswith(MERGE_SUFFIX_LEFT):
        return item_id[: -len(MERGE_SUFFIX_LEFT)]
    if item_id.endswith(MERGE_SUFFIX_RIGHT):
        return item_id[: -len(MERGE_SUFFIX_RIGHT)]
    return item_id


def has_merge_suffix(item_id: str) -> bool:
    """Port of ``hasMergeSuffix`` (merge.ts l.36-38)."""
    return item_id.endswith(MERGE_SUFFIX_LEFT) or item_id.endswith(MERGE_SUFFIX_RIGHT)


@dataclass(frozen=True)
class ConflictGroup(Generic[K, V]):
    """One divergent id: both versions, never dropped. Port of upstream's
    per-conflict bookkeeping (merge.ts l.304-317: ``mergeGroup =
    randomUUID()``, suffixed ids, both sides inserted)."""

    group_id: str
    canonical_id: K
    left: V
    right: V
    left_suffixed_id: str
    right_suffixed_id: str


@dataclass
class MergeClassification(Generic[K, V]):
    """The full result of one classification pass — nothing here drops a
    side; ``conflicts`` keeps both, ``left_only``/``right_only`` are
    single-sided by definition (the other side never had the id), and
    ``identical`` records what was deduplicated (for audit, not silence)."""

    left_only: dict[K, V] = field(default_factory=dict)
    right_only: dict[K, V] = field(default_factory=dict)
    identical: dict[K, V] = field(default_factory=dict)  # canonical_id -> kept (left) version
    conflicts: list[ConflictGroup[K, V]] = field(default_factory=list)

    def to_summary(self) -> dict:
        return {
            "left_only": len(self.left_only),
            "right_only": len(self.right_only),
            "identical": len(self.identical),
            "conflicts": len(self.conflicts),
            "conflict_group_ids": [c.group_id for c in self.conflicts],
        }


def classify(
    left_items: Mapping[K, V],
    right_items: Mapping[K, V],
    *,
    is_identical: Callable[[V, V], bool],
    new_group_id: Callable[[], str] | None = None,
) -> MergeClassification[K, V]:
    """Port of the per-id classification loop in ``performMerge``
    (merge.ts l.226-359, PASS 1, minus the edge bookkeeping).

    ``left_items``/``right_items`` map a canonical id to an opaque value;
    ``is_identical(left_value, right_value)`` is the caller's content
    comparator (upstream's ``nodesAreIdentical``, generalized — the caller
    decides what "content" means, e.g. trialerror's content-sha256 over
    tier/kind/l0_abstract/body). Every id present on EITHER side is
    classified exactly once, matching upstream's own ``allIds`` union
    (l.156-159).
    """
    gen_id = new_group_id or (lambda: uuid.uuid4().hex)
    result: MergeClassification[K, V] = MergeClassification()

    all_ids: list[K] = []
    seen: set[K] = set()
    for source in (left_items, right_items):
        for cid in source:
            if cid not in seen:
                seen.add(cid)
                all_ids.append(cid)

    for cid in all_ids:
        left_val = left_items.get(cid)
        right_val = right_items.get(cid)
        has_left = cid in left_items
        has_right = cid in right_items

        if has_left and not has_right:
            result.left_only[cid] = left_val  # type: ignore[assignment]
        elif has_right and not has_left:
            result.right_only[cid] = right_val  # type: ignore[assignment]
        else:
            # present on both sides
            if is_identical(left_val, right_val):  # type: ignore[arg-type]
                result.identical[cid] = left_val  # type: ignore[assignment]
            else:
                group_id = gen_id()
                result.conflicts.append(
                    ConflictGroup(
                        group_id=group_id,
                        canonical_id=cid,
                        left=left_val,  # type: ignore[arg-type]
                        right=right_val,  # type: ignore[arg-type]
                        left_suffixed_id=f"{group_id}{MERGE_SUFFIX_LEFT}",
                        right_suffixed_id=f"{group_id}{MERGE_SUFFIX_RIGHT}",
                    )
                )

    return result
