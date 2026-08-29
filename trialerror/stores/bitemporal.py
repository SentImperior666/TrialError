"""Bi-temporal edge helpers (Graphiti 4-timestamp pattern, native SQLite —
design Appendix A/D1/D3: "v0 implements the bi-temporal edge schema
natively in SQLite"). Applies to ``claim`` and ``relation`` in
knowledge.db, the two tables carrying all four columns:
``created_at``/``expired_at`` (transaction-time: when the DB learned/
un-learned the fact) and ``valid_at``/``invalid_at`` (event-time: when the
fact was/stopped being true in the world).

Semantics, spelled out because the two axes are easy to conflate:

- **assert**: write a new row. ``created_at`` = now (tx-time start),
  ``expired_at`` = NULL (still the DB's current belief), ``valid_at``
  defaults to now (event-time start) unless the caller states the fact
  became true earlier/later, ``invalid_at`` = NULL (still true, so far as
  known).
- **invalidate**: two independent things can happen to an existing row,
  and this module keeps them separate rather than overloading one call:
    - ``expire`` closes the transaction-time window (``expired_at`` = now)
      — "the DB no longer treats this row as current", regardless of
      whether the underlying fact is still true in the world (e.g. it was
      superseded by a corrected extraction of the SAME real-world fact).
    - ``event_end`` closes the event-time window (``invalid_at`` = a
      caller-given or now timestamp) — "the fact itself stopped being
      true", independent of whether the DB is representing that with the
      same row or a new one.
  ``supersede`` does both in one transaction: asserts the replacement row,
  then expires (and links, via ``superseded_by``) the old one — the common
  case ("we now know this differently").
- **as_of**: reconstruct what was true, viewed from two independent
  points — ``valid_at`` (event-time: "what did the world look like on
  date X") and ``tx_at`` (transaction-time: "what did the DB believe on
  date Y, even if it has since learned better"). Omitting ``tx_at`` means
  "the DB's current belief" (``expired_at IS NULL``); omitting ``valid_at``
  means "as of now".
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from trialerror.stores.store import Store
from trialerror.stores.writer import insert, update
from trialerror.util.timeutil import now

__all__ = ["BITEMPORAL_TABLES", "assert_fact", "expire_fact", "end_fact_validity", "supersede_fact", "as_of"]

#: table -> primary key column, for the two tables this module applies to.
BITEMPORAL_TABLES: dict[str, str] = {"claim": "claim_id", "relation": "rel_id"}


def _check_table(table: str) -> str:
    pk = BITEMPORAL_TABLES.get(table)
    if pk is None:
        raise ValueError(f"{table!r} is not a bi-temporal table (expected one of {sorted(BITEMPORAL_TABLES)!r})")
    return pk


def assert_fact(
    store: Store,
    table: str,
    row: Mapping[str, Any],
    *,
    valid_at: str | None = None,
) -> dict[str, Any]:
    """Insert a new bi-temporal row: ``created_at`` = now, ``expired_at`` =
    NULL, ``valid_at`` = ``valid_at`` or now, ``invalid_at`` = NULL (unless
    the caller already supplied any of those four in ``row`` — an explicit
    value always wins, so a migration/backfill can assert historical
    facts). Goes through :func:`trialerror.stores.writer.insert`, so XID columns
    (``created_by_launch`` etc.) are still validated."""
    _check_table(table)
    ts = now()
    full_row: dict[str, Any] = dict(row)
    full_row.setdefault("created_at", ts)
    full_row.setdefault("expired_at", None)
    full_row.setdefault("valid_at", valid_at if valid_at is not None else ts)
    full_row.setdefault("invalid_at", None)
    return insert(store, table, full_row)


def expire_fact(store: Store, table: str, fact_id: Any, *, tx_at: str | None = None, superseded_by: Any = None) -> None:
    """Close the transaction-time window: the DB no longer treats this row
    as its current belief. Does NOT touch ``invalid_at`` — the underlying
    fact may still be true; only the DB's *representation* of it changed."""
    pk = _check_table(table)
    changes: dict[str, Any] = {"expired_at": tx_at or now()}
    if superseded_by is not None:
        changes["superseded_by"] = superseded_by
    update(store, table, pk_column=pk, pk_value=fact_id, changes=changes)


def end_fact_validity(store: Store, table: str, fact_id: Any, *, event_at: str | None = None) -> None:
    """Close the event-time window: the fact itself stopped being true in
    the world, as of ``event_at`` (default: now). Independent of
    :func:`expire_fact` — a still-current row (``expired_at IS NULL``) can
    have a closed validity window if the DB knows the fact ended but hasn't
    (yet) recorded a replacement fact."""
    pk = _check_table(table)
    update(store, table, pk_column=pk, pk_value=fact_id, changes={"invalid_at": event_at or now()})


def supersede_fact(
    store: Store,
    table: str,
    old_fact_id: Any,
    new_row: Mapping[str, Any],
    *,
    new_id_column: str,
    new_id_value: Any,
    valid_at: str | None = None,
    tx_at: str | None = None,
) -> dict[str, Any]:
    """The common combined operation: assert a replacement row, then expire
    (transaction-time) the old one and link ``old.superseded_by ->
    new_id``, in one call. The old row's ``valid_at``/``invalid_at`` (event
    time) are left untouched — this says "we now represent the same
    real-world fact differently," not "the fact stopped being true."

    ``tx_at`` (default: now) is used for BOTH halves — the new row's
    ``created_at`` and the old row's ``expired_at`` — so a caller
    reconstructing history (v1 migration backfill; a deterministic test)
    gets one consistent transaction-time instant for "the moment the DB's
    belief changed," rather than two back-to-back real-``now()`` calls that
    can legitimately land in the same millisecond as an external
    "before"/"after" probe (ISO-8601 millisecond timestamps do not
    guarantee two separate ``now()`` calls a few microseconds apart sort
    strictly after one another as strings)."""
    pk = _check_table(table)
    ts = tx_at or now()
    row_with_id = dict(new_row)
    row_with_id[new_id_column] = new_id_value
    row_with_id.setdefault("created_at", ts)
    written = assert_fact(store, table, row_with_id, valid_at=valid_at)
    expire_fact(store, table, old_fact_id, tx_at=ts, superseded_by=new_id_value)
    del pk  # only needed to validate `table`; new_id_column is caller-supplied (claim_id/rel_id)
    return written


def as_of(
    store: Store,
    table: str,
    *,
    valid_at: str | None = None,
    tx_at: str | None = None,
    where: str | None = None,
    params: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """Return every row of ``table`` that was valid (event-time) at
    ``valid_at`` (default: now — rows whose ``valid_at`` is NULL are
    treated as always-valid from the start, matching a claim asserted
    without a stated event time), as known (transaction-time) at ``tx_at``
    (default: the DB's current belief, i.e. ``expired_at IS NULL``).

    ``where``/``params`` add an additional caller-supplied SQL predicate
    (e.g. ``"claim_id = ?", (some_id,)``) ANDed onto the temporal
    predicate — this is a reconstruction helper, not a general query
    engine (that's M8's retrieval layer).
    """
    _check_table(table)
    valid_at = valid_at or now()
    clauses = [
        "(valid_at IS NULL OR valid_at <= ?)",
        "(invalid_at IS NULL OR invalid_at > ?)",
    ]
    query_params: list[Any] = [valid_at, valid_at]
    if tx_at:
        clauses.append("created_at <= ?")
        clauses.append("(expired_at IS NULL OR expired_at > ?)")
        query_params += [tx_at, tx_at]
    else:
        clauses.append("expired_at IS NULL")
    if where:
        clauses.append(f"({where})")
        query_params += list(params)

    conn = store.conn_for_table(table)
    sql = f"SELECT * FROM {table} WHERE " + " AND ".join(clauses)
    rows = conn.execute(sql, query_params).fetchall()
    return [dict(r) for r in rows]
