"""``trialerror.memory.merge`` — the two-way, conflict-surfacing merge engine.
Design Section 9.7: "export/import to ``memory/*.md`` for git sync with
**MegaMemory merge port** (vendored MIT: conflict-group UUIDs, ``::left/
::right`` divergence surfaced to the operator, never auto-resolved)."
Design Section 12 (M11 row, THE adversarial acceptance criterion): "a
divergent two-store merge must SURFACE conflicts, not drop a side."

This module is the harness-specific glue around
``vendored/MegaMemory/merge_port.py`` (the ported, storage-agnostic
per-id classification pass) — see that file's own docstring for exactly
what was ported vs. reimplemented. The glue's job: (1) build the
``{key: row}`` maps ``classify()`` needs from ``memory_item`` (LOCAL) and
a caller-supplied list of foreign items (e.g. parsed from an imported
export — see ``trialerror.memory.render``); (2) apply the classification back
onto ``memory_item`` using only columns that table already has.

**Encoding conflicts without a schema change (in-lane constraint — this
build's lane is ``trialerror/memory/`` only, NOT ``trialerror/stores/schema/``,
which M1 already built and froze).** Upstream MegaMemory spends two
dedicated SQLite columns on a node (``merge_group``, ``needs_merge``).
``memory_item`` has neither. Both are folded onto columns the table
already has, mirroring upstream's OWN ``id::left``/``id::right`` id
convention one level further:

- the group id is embedded in the row's OWN ``memory_item_id``:
  ``"MEM-<group_id>::left"`` / ``"MEM-<group_id>::right"`` (upstream:
  ``"<id>::left"`` / ``"<id>::right"``, no group-id-as-id trick needed
  because it has a separate ``merge_group`` column to hold it);
- the ``needs_merge`` flag is ``memory_item.status = 'needs_merge'``
  (``status`` has no CHECK constraint in the M1-built DDL — free-form
  text, deliberately left open per that module's own DDL comment — this
  is exactly the kind of extension it was left open for);
- the ORIGINAL, now-disputed local row is demoted ``active ->
  superseded`` (never deleted — its content lives on verbatim as the new
  ``::left`` row) so nothing continues silently answering queries for
  ``key`` as the single source of truth while a conflict sits open.

Nothing about this needs a migration: every write here goes through the
same ``trialerror.stores.insert``/``update`` validated API every other module
uses, against columns M1 already shipped.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from trialerror.memory.content import items_identical
from trialerror.stores import get as store_get
from trialerror.stores import insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

# ---------------------------------------------------------------------------
# repo-root sys.path convention for consuming vendored/ code from an
# editable install (pyproject.toml's `packages.find` includes only
# `trialerror*` -- `vendored/` is a sibling directory, not part of the
# installed distribution). Mirrors the existing precedent in
# `tests/_concurrent_writer_worker.py` (`sys.path.insert(0, repo_root)`).
# See `vendored/__init__.py` for the fuller rationale.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# TRIALERROR-DEV-NOTE (cross-cutting, out-of-lane, flagged not fixed): importing
# ANY vendored Python module normally leaves a `__pycache__/*.pyc` behind
# under `vendored/<item>/` -- gitignored, so it never lands in a commit,
# but `trialerror.util.checks.check_license_audit` (M0's, `trialerror/util/
# checks.py`, not this build's lane) walks `item_dir.rglob("*")` and
# excludes files/dirs named literally `__pycache__`, which does NOT catch
# the *.pyc files living ONE LEVEL INSIDE that directory (their own
# `.name` is e.g. `merge_port.cpython-312.pyc`, not `__pycache__`) -- so a
# `.pyc` gets scanned as if it were vendored source and fails the header
# check. Reproducible: run this repo's full test suite once (any test
# that imports this module — including pytest's own collection pass over
# `tests/test_memory_*.py`, which happens before ANY test body executes —
# leaves the cache behind), then `tests/test_cli_doctor.py::
# test_cli_doctor_on_repo_own_vendored_dir_is_clean` fails. `sys.
# dont_write_bytecode` is set for exactly this one import (saved/restored
# immediately after, not left changed process-wide) so THIS build never
# creates the problem locally -- but any OTHER vendored Python module
# (e.g. M7's book-to-skill sanitizer, once something imports it as a
# package) hits the identical bug, so the real fix belongs in M0's
# checker (exclude by suffix `.pyc`/`.pyo`, or check `"__pycache__" in
# f.relative_to(root).parts` instead of `f.name in _EXCLUDED_NAMES`) —
# flagged to the orchestrator, not patched here (lane isolation).
_prev_dont_write_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    from vendored.MegaMemory.merge_port import (  # noqa: E402  (see sys.path note above)
        MERGE_SUFFIX_LEFT,
        MERGE_SUFFIX_RIGHT,
        classify,
        strip_merge_suffix,
    )
finally:
    sys.dont_write_bytecode = _prev_dont_write_bytecode

__all__ = [
    "MERGE_SUFFIX_LEFT",
    "MERGE_SUFFIX_RIGHT",
    "MergeResult",
    "two_way_merge",
    "resolve_conflict",
    "list_conflicts",
    "group_id_from_item_id",
]

_ID_PREFIX = "MEM-"


def group_id_from_item_id(item_id: str) -> str:
    """Recover the bare conflict-group id from a ``::left``/``::right``
    suffixed ``memory_item_id`` (``"MEM-<group_id>::left"`` ->
    ``"<group_id>"``). Public so ``trialerror/memory/checks.py`` (and any other
    reader of ``needs_merge`` rows) doesn't re-derive this string format
    ad hoc — one place owns the encoding this module's docstring
    describes."""
    canonical = strip_merge_suffix(item_id)
    return canonical[len(_ID_PREFIX) :] if canonical.startswith(_ID_PREFIX) else canonical


@dataclass
class MergeResult:
    """Everything :func:`two_way_merge` did, in full — the shape
    ``trialerror memory sync-import`` returns as its envelope result."""

    imported: list[str] = field(default_factory=list)
    left_only_keys: list[str] = field(default_factory=list)
    dedup_keys: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": list(self.imported),
            "left_only_keys": list(self.left_only_keys),
            "dedup_keys": list(self.dedup_keys),
            "conflicts": list(self.conflicts),
            "summary": {
                "imported": len(self.imported),
                "left_only": len(self.left_only_keys),
                "dedup": len(self.dedup_keys),
                "conflicts": len(self.conflicts),
            },
        }


def _local_active_by_key(store: Store) -> dict[str, dict[str, Any]]:
    """One representative ACTIVE row per ``key`` (the most recently
    updated, if more than one is somehow active for the same key — e.g. a
    prior ``keep both`` resolution). Other coexisting actives for that key
    are left untouched by this pass; this function only decides who
    STANDS IN for ``key`` when classifying against the incoming foreign
    side."""
    rows = [dict(r) for r in store.ops.execute("SELECT * FROM memory_item WHERE status = 'active'").fetchall()]
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = by_key.get(row["key"])
        if current is None or row["updated_ts"] > current["updated_ts"]:
            by_key[row["key"]] = row
    return by_key


def _insert_new_active(store: Store, foreign_item: Mapping[str, Any], *, key: str, ts: str) -> dict[str, Any]:
    row = {
        "memory_item_id": new_id("MEM"),
        "key": key,
        "tier": foreign_item["tier"],
        "kind": foreign_item["kind"],
        "body": foreign_item["body"],
        "l0_abstract": foreign_item.get("l0_abstract"),
        "updated_ts": foreign_item.get("updated_ts") or ts,
        "account_id": foreign_item["account_id"],
        "status": "active",
    }
    return insert(store, "memory_item", row)


def _materialize_conflict(
    store: Store,
    *,
    key: str,
    group_id: str,
    local_row: Mapping[str, Any],
    foreign_item: Mapping[str, Any],
    ts: str,
) -> dict[str, Any]:
    left_id = f"{_ID_PREFIX}{group_id}{MERGE_SUFFIX_LEFT}"
    right_id = f"{_ID_PREFIX}{group_id}{MERGE_SUFFIX_RIGHT}"

    # Demote the disputed local row -- its content survives verbatim as
    # the ::left row inserted below, so this never loses data; it just
    # stops being the (now ambiguous) single active answer for `key`.
    update(
        store,
        "memory_item",
        pk_column="memory_item_id",
        pk_value=local_row["memory_item_id"],
        changes={"status": "superseded"},
    )

    left_row = {
        "memory_item_id": left_id,
        "key": key,
        "tier": local_row["tier"],
        "kind": local_row["kind"],
        "body": local_row["body"],
        "l0_abstract": local_row.get("l0_abstract"),
        "updated_ts": local_row["updated_ts"],
        "account_id": local_row["account_id"],
        "status": "needs_merge",
    }
    insert(store, "memory_item", left_row)

    right_row = {
        "memory_item_id": right_id,
        "key": key,
        "tier": foreign_item["tier"],
        "kind": foreign_item["kind"],
        "body": foreign_item["body"],
        "l0_abstract": foreign_item.get("l0_abstract"),
        "updated_ts": foreign_item.get("updated_ts") or ts,
        "account_id": foreign_item["account_id"],
        "status": "needs_merge",
    }
    insert(store, "memory_item", right_row)

    return {"group_id": group_id, "key": key, "left_id": left_id, "right_id": right_id}


def two_way_merge(store: Store, *, foreign_items: Iterable[Mapping[str, Any]], ts: str | None = None) -> MergeResult:
    """Merge ``foreign_items`` (the "right" side — e.g. parsed from a git-
    synced export written by another account, ``trialerror.memory.render.
    import_memory``'s caller) into ``store``'s current ``memory_item``
    rows (the "left"/local side), by ``key``.

    Delegates the per-key LEFT/RIGHT/IDENTICAL/CONFLICT decision to
    ``vendored.MegaMemory.merge_port.classify`` (content-hash comparator:
    :func:`trialerror.memory.content.items_identical`) and applies the result:

    - **right_only** (key only on the foreign side): imported as a new
      ACTIVE row.
    - **left_only** (key only exists locally): untouched — nothing to do.
    - **identical** (same content on both sides): untouched, no-op — the
      content-hash DEDUP rule (never re-writes, never bumps
      ``updated_ts``; this is what makes re-importing your own unchanged
      export idempotent).
    - **conflict** (same key, DIFFERENT content): **both versions kept**
      under a shared group id (see module docstring) — never silently
      resolved, never last-writer-wins. This is the adversarial
      acceptance bar (design Section 12, M11 row).
    """
    ts = ts or now()
    foreign_by_key: dict[str, dict[str, Any]] = {item["key"]: dict(item) for item in foreign_items}
    local_by_key = _local_active_by_key(store)

    classification = classify(local_by_key, foreign_by_key, is_identical=items_identical)
    result = MergeResult()

    for key, foreign_item in classification.right_only.items():
        row = _insert_new_active(store, foreign_item, key=key, ts=ts)
        result.imported.append(row["memory_item_id"])

    result.left_only_keys.extend(sorted(classification.left_only.keys()))
    result.dedup_keys.extend(sorted(classification.identical.keys()))

    for conflict in classification.conflicts:
        group = _materialize_conflict(
            store,
            key=conflict.canonical_id,
            group_id=conflict.group_id,
            local_row=conflict.left,
            foreign_item=conflict.right,
            ts=ts,
        )
        result.conflicts.append(group)

    return result


def resolve_conflict(store: Store, *, group_id: str, keep: str, ts: str | None = None) -> dict[str, Any]:
    """Resolve one open conflict group (``trialerror memory merge --group
    <group_id> --keep left|right|both``) — the ONLY way a ``needs_merge``
    pair leaves that status; never automatic, always an explicit call by
    a human or an agent that looked at both sides (design Section 9.7:
    "never auto-resolved").

    - ``keep="left"``: left -> ``active``, right -> ``superseded``.
    - ``keep="right"``: right -> ``active``, left -> ``superseded``.
    - ``keep="both"``: BOTH -> ``active`` (both survive under the same
      ``key`` — legitimate when the two versions are both true, e.g.
      differing per-account preferences that were never really a single
      fact to begin with).

    Refuses (raising :class:`ValueError`) an unknown ``group_id`` or a
    group whose sides are already resolved — resolution is a one-shot
    transition per group, not an idempotent re-apply, so a caller cannot
    silently "resolve" a group twice with different answers.
    """
    if keep not in ("left", "right", "both"):
        raise ValueError(f"resolve_conflict: keep must be 'left'|'right'|'both', got {keep!r}")

    left_id = f"{_ID_PREFIX}{group_id}{MERGE_SUFFIX_LEFT}"
    right_id = f"{_ID_PREFIX}{group_id}{MERGE_SUFFIX_RIGHT}"
    left = store_get(store, "memory_item", pk_column="memory_item_id", pk_value=left_id)
    right = store_get(store, "memory_item", pk_column="memory_item_id", pk_value=right_id)

    if left is None and right is None:
        raise ValueError(f"resolve_conflict: no conflict group {group_id!r} found")
    for side_name, row in (("left", left), ("right", right)):
        if row is not None and row["status"] != "needs_merge":
            raise ValueError(
                f"resolve_conflict: group {group_id!r} {side_name} side is already resolved "
                f"(status={row['status']!r}) — resolution is one-shot, not re-appliable"
            )

    keep_left = keep in ("left", "both")
    keep_right = keep in ("right", "both")
    if left is not None:
        update(
            store, "memory_item", pk_column="memory_item_id", pk_value=left_id,
            changes={"status": "active" if keep_left else "superseded"},
        )
    if right is not None:
        update(
            store, "memory_item", pk_column="memory_item_id", pk_value=right_id,
            changes={"status": "active" if keep_right else "superseded"},
        )

    return {
        "group_id": group_id,
        "keep": keep,
        "left_id": left_id if left is not None else None,
        "right_id": right_id if right is not None else None,
        "resolved_ts": ts or now(),
    }


def list_conflicts(store: Store, *, account_id: str | None = None) -> list[dict[str, Any]]:
    """Every currently open (``status='needs_merge'``) conflict, grouped
    by group id — the ``trialerror doctor`` "unresolved-conflict-group count"
    check and the ``trialerror memory merge`` (no ``--group``) CLI action both
    read this. ``account_id``, if given, filters to groups where at least
    one side belongs to that account."""
    rows = [dict(r) for r in store.ops.execute("SELECT * FROM memory_item WHERE status = 'needs_merge'").fetchall()]
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        gid = group_id_from_item_id(row["memory_item_id"])
        side = "left" if row["memory_item_id"].endswith(MERGE_SUFFIX_LEFT) else "right"
        g = groups.setdefault(gid, {"group_id": gid, "key": row["key"], "versions": []})
        g["versions"].append({"side": side, **row})

    out = sorted(groups.values(), key=lambda g: g["group_id"])
    if account_id is not None:
        out = [g for g in out if any(v["account_id"] == account_id for v in g["versions"])]
    return out
