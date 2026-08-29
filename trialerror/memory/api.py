"""``trialerror.memory.api`` — the ``memory_item`` read/write API. Design
Section 9.7: "ops-shaped, reimplemented thin: ``memory_item(id, key, tier
L0/L1/L2, kind rule/fact/lesson/preference/index, body, l0_abstract,
updated_ts, account_id, status)``; boot loads L0 index + targeted L1
(Athena tiered-boot); progressive-disclosure search." Design Section 12
(M11 row): "tiered items, L0 index, progressive search ... boot bundle L0
<= configured token budget."

Two operating modes over the same table:

- **Full read/write** (:func:`put_item`, :func:`get_item`) — used by a
  session capturing a lesson/rule/fact/preference (M6's close-time
  capture; see module-level notes at the bottom of this file), or by an
  operator resolving a conflict.
- **Progressive disclosure** (:func:`search_items`, :func:`boot_bundle`)
  — INDEX-ONLY rows (id/key/tier/kind/``l0_abstract``/account/updated_ts/
  status; the ``body`` column is never returned by these two) so a caller
  spends a handful of tokens per hit instead of the full body, then calls
  :func:`get_item` for the specific ids it actually wants (claude-mem's
  3-layer progressive-disclosure pattern, ``docs/mining/
  G02-memory-2__claude-mem.md``, generalized to this table).

``put_item`` upserts BY ``(key, account_id)`` — an idempotent
"understand -> work -> update" write (MegaMemory's own framing, ``docs/
mining/G01-memory-1__MegaMemory.md``: "prefer ``update_concept`` over new
nodes"): a second `put_item` call for the same key/account with unchanged
content is a no-op (the same content-hash dedup rule
:mod:`trialerror.memory.merge` uses for the CROSS-account case — see
:mod:`trialerror.memory.content`); with changed content it updates the existing
row in place (a same-account edit is a normal linear history, not a
collision — collisions only arise ACROSS accounts, which is
:mod:`trialerror.memory.merge`'s job, never this module's).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from trialerror.memory.content import content_sha256
from trialerror.stores import get as store_get
from trialerror.stores import insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "TIERS",
    "KINDS",
    "INDEX_FIELDS",
    "DEFAULT_TOKEN_BUDGET",
    "put_item",
    "get_item",
    "search_items",
    "boot_bundle",
    "estimate_tokens",
]

#: Mirrors the ``memory_item`` DDL CHECK constraints verbatim (design
#: Section 4.2 / ``trialerror/stores/schema/ops.py``) — declared here too so
#: this module can give a clean :class:`ValueError` instead of letting a
#: bad value fall through to a raw ``sqlite3.IntegrityError`` translated by
#: ``trialerror.stores.writer`` into a less specific message.
TIERS: tuple[str, ...] = ("L0", "L1", "L2")
KINDS: tuple[str, ...] = ("rule", "fact", "lesson", "preference", "index")

#: The progressive-disclosure "index" projection of a ``memory_item`` row
#: — deliberately excludes ``body`` (full-text fetch is :func:`get_item`).
INDEX_FIELDS: tuple[str, ...] = (
    "memory_item_id",
    "key",
    "tier",
    "kind",
    "l0_abstract",
    "account_id",
    "updated_ts",
    "status",
)

#: A conservative chars-per-token approximation (~4, the common
#: rule-of-thumb for English text) — no tokenizer dependency (stdlib-only
#: constraint, design Section 1's Windows/local-first posture). Good
#: enough for a BUDGET GATE (design Section 12 M11 row: "boot bundle L0 <=
#: configured token budget"), not claimed to be exact.
_CHARS_PER_TOKEN = 4

#: Default boot-bundle budget when a caller (M6) doesn't supply its own
#: ``trialerror.toml``-configured value.
DEFAULT_TOKEN_BUDGET = 2000


def estimate_tokens(text: str | None) -> int:
    """Approximate token count for ``text`` (``0`` for ``None``/empty)."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _validate_tier_kind(*, tier: str, kind: str) -> None:
    if tier not in TIERS:
        raise ValueError(f"put_item: tier must be one of {TIERS!r}, got {tier!r}")
    if kind not in KINDS:
        raise ValueError(f"put_item: kind must be one of {KINDS!r}, got {kind!r}")


def _find_active_by_key(store: Store, *, key: str, account_id: str) -> dict[str, Any] | None:
    row = store.ops.execute(
        "SELECT * FROM memory_item WHERE key = ? AND account_id = ? AND status = 'active' "
        "ORDER BY updated_ts DESC, rowid DESC LIMIT 1",
        (key, account_id),
    ).fetchone()
    return dict(row) if row is not None else None


def put_item(
    store: Store,
    *,
    key: str,
    tier: str,
    kind: str,
    body: str,
    account_id: str,
    l0_abstract: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Upsert one memory item, scoped to ``(key, account_id)``.

    - No existing ACTIVE row for this ``(key, account_id)``: inserts a new
      row (``status='active'``).
    - An existing row with IDENTICAL content (:mod:`trialerror.memory.content`):
      no-op, returns the existing row unchanged (idempotent — re-running a
      capture step twice never duplicates or bumps ``updated_ts``
      pointlessly).
    - An existing row with DIFFERENT content: updates it in place
      (same-account edit — a normal linear history, never a collision).

    Raises :class:`ValueError` for a bad ``tier``/``kind`` (checked here
    for a clean message; the DDL's own CHECK constraints would refuse the
    write regardless, wrapped in a less specific
    :class:`~trialerror.stores.errors.ValidationError` — see module docstring).
    """
    if not key or not key.strip():
        raise ValueError("put_item: key is required and must be non-empty")
    if not body or not body.strip():
        raise ValueError("put_item: body is required and must be non-empty")
    _validate_tier_kind(tier=tier, kind=kind)

    ts = ts or now()
    candidate = {"tier": tier, "kind": kind, "l0_abstract": l0_abstract, "body": body}

    existing = _find_active_by_key(store, key=key, account_id=account_id)
    if existing is not None:
        if content_sha256(existing) == content_sha256(candidate):
            return existing
        changes = {"tier": tier, "kind": kind, "body": body, "l0_abstract": l0_abstract, "updated_ts": ts}
        update(store, "memory_item", pk_column="memory_item_id", pk_value=existing["memory_item_id"], changes=changes)
        merged = dict(existing)
        merged.update(changes)
        return merged

    row = {
        "memory_item_id": new_id("MEM"),
        "key": key,
        "tier": tier,
        "kind": kind,
        "body": body,
        "l0_abstract": l0_abstract,
        "updated_ts": ts,
        "account_id": account_id,
        "status": "active",
    }
    return insert(store, "memory_item", row)


def get_item(store: Store, memory_item_id: str) -> dict[str, Any] | None:
    """Full row (including ``body``) by id — the "L1/L2 fetch" step of
    progressive disclosure, called only for ids a caller already picked
    out of a :func:`search_items` result."""
    return store_get(store, "memory_item", pk_column="memory_item_id", pk_value=memory_item_id)


def _index_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in INDEX_FIELDS}


def search_items(
    store: Store,
    *,
    query: str | None = None,
    tier: str | None = None,
    kind: str | None = None,
    account_id: str | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Progressive-disclosure search: INDEX-ONLY rows (see
    :data:`INDEX_FIELDS` — ``body`` is never returned here), newest-first.
    ``query`` substring-matches ``key``/``l0_abstract``/``body`` (body
    participates in MATCHING but is still excluded from what's RETURNED —
    a hit on body text surfaces the item's index row, not its content).
    ``status=None`` removes the status filter entirely (searches every
    status, including ``needs_merge`` — used by conflict tooling); the
    default ``"active"`` is what a normal caller wants."""
    clauses: list[str] = []
    params: list[Any] = []
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(account_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if query:
        clauses.append("(key LIKE ? OR l0_abstract LIKE ? OR body LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT *, rowid AS _rowid FROM memory_item {where} ORDER BY updated_ts DESC, _rowid DESC LIMIT ?"
    params.append(limit)
    rows = store.ops.execute(sql, params).fetchall()
    return [_index_row(dict(r)) for r in rows]


def boot_bundle(
    store: Store,
    *,
    account_id: str | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> dict[str, Any]:
    """The M6 boot-time payload: L0 index + targeted L1/L2 abstracts,
    truncated to ``token_budget`` (design Section 9.7 "Athena tiered-boot";
    Section 12 M11 acceptance: "boot bundle L0 <= configured token
    budget"). Every entry is an INDEX row (:data:`INDEX_FIELDS` — never
    full ``body``); a caller that wants an item's full text follows up
    with :func:`get_item`.

    Ordering (highest priority first, so truncation drops the LEAST
    important items, never a partial item): tier L0 items first, then L1,
    then L2, each tier ordered by ``key``. Cost per item is
    :func:`estimate_tokens` over its ``l0_abstract`` (falling back to
    ``key`` if no abstract is set) — deliberately never the full body,
    since this bundle never carries bodies at all.

    Truncation is WHOLE-ITEM: the first item that would push the running
    total over budget, and every item after it in priority order, are
    omitted (never a half-included item) — so the returned bundle's
    estimated cost is ALWAYS <= ``token_budget`` (the acceptance bar), by
    construction, not by chance.
    """
    rows = search_items(store, account_id=account_id, status="active", limit=1_000_000)
    tier_rank = {"L0": 0, "L1": 1, "L2": 2}
    ordered = sorted(rows, key=lambda r: (tier_rank.get(r["tier"], 9), r["key"]))

    items: list[dict[str, Any]] = []
    total = 0
    omitted = 0
    for i, row in enumerate(ordered):
        cost = estimate_tokens(row.get("l0_abstract") or row["key"])
        if total + cost > token_budget:
            omitted = len(ordered) - i
            break
        items.append(row)
        total += cost

    return {
        "items": items,
        "total_estimated_tokens": total,
        "token_budget": token_budget,
        "truncated": omitted > 0,
        "omitted_count": omitted,
    }
