"""The content-hash dedup rule shared by :mod:`trialerror.memory.api` (same-
account upsert) and :mod:`trialerror.memory.merge` (cross-account two-way
merge). Design build brief (M11): "two-way merge with content-hash
dedup" — this module is the ONE place that hash is computed, so both
callers agree on what "identical content" means.

Ported convention (upstream ``nodesAreIdentical``, ``vendored/MegaMemory/
merge_port.py``'s docstring): compare CONTENT fields only, deliberately
excluding ``memory_item_id`` (identity, not content), ``updated_ts``
(volatile), ``account_id`` (differs BY DEFINITION when comparing two
accounts' versions of the same ``key`` — that's the whole point of a
cross-account merge), and ``status`` (merge bookkeeping, not content).
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping

__all__ = ["CONTENT_FIELDS", "content_sha256", "items_identical"]

#: The exact fields (in this order) that participate in the content hash.
#: ``key`` is deliberately excluded too: callers only ever hash two items
#: they've already matched on the same ``key`` (see
#: ``trialerror.memory.merge.two_way_merge``), so including it would be
#: redundant, never distinguishing — the hash is purely "what does this
#: item SAY", not "what is it filed under".
CONTENT_FIELDS: tuple[str, ...] = ("tier", "kind", "l0_abstract", "body")


def content_sha256(item: Mapping[str, object]) -> str:
    """sha256 over ``item``'s content fields, canonicalized (fixed key
    order via ``CONTENT_FIELDS``, ``None`` normalized to ``""`` so a row
    with ``l0_abstract=NULL`` hashes identically to one explicitly written
    as ``l0_abstract=""``) so the same logical content always hashes the
    same regardless of which optional fields a caller happened to set."""
    payload = {field: (item.get(field) or "") for field in CONTENT_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def items_identical(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """``True`` iff ``left`` and ``right`` have identical CONTENT (see
    :data:`CONTENT_FIELDS`) — the dedup predicate handed to
    ``vendored.MegaMemory.merge_port.classify`` as ``is_identical``."""
    return content_sha256(left) == content_sha256(right)
