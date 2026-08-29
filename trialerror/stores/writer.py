"""The validated write API. Design Section 12 (M1 row): "validated write
API incl. XID cross-store target validation." Design Section 4 (intro):
"All writes go through ``trialerror.stores`` (validated, transactional,
event-emitting)."

One generic, schema-driven :func:`insert` — not 41 hand-written per-table
functions — is the deliberate M1 scope boundary (design Section 12: M1
"stays deliberately schema-only, no business logic, to protect the
spine"). "Typed" (per the build brief: "typed write-APIs for each table
that VALIDATE XIDs") is delivered declaratively: every table's real column
set comes straight from SQLite's own ``PRAGMA table_info`` (so it can never
drift from the DDL that created it), NOT NULL/CHECK/UNIQUE constraints are
enforced by SQLite itself and translated into :class:`ValidationError`
here, and the ``trialerror.stores.xid`` registry supplies the one thing SQLite
structurally cannot check — cross-file XID target existence.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from trialerror.stores.errors import UnknownTableError, ValidationError, XidTargetMissingError
from trialerror.stores.redact import redact_payload
from trialerror.stores.store import Store
from trialerror.stores.xid import xid_columns_for_table

__all__ = ["table_columns", "insert", "get", "update"]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The real column set of ``table`` as SQLite itself knows it (via
    ``PRAGMA table_info``) — the single source of truth ``insert`` checks
    unknown fields against, so it can never disagree with the DDL that
    created the table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise UnknownTableError(f"table {table!r} has no columns (does it exist in this DB?)")
    return {row["name"] for row in rows}


def _validate_xids(store: Store, table: str, row: Mapping[str, Any]) -> None:
    xid_cols = xid_columns_for_table(table)
    if not xid_cols:
        return
    for col, target in xid_cols.items():
        if col not in row or row[col] is None:
            continue
        value = row[col]
        target_conn = getattr(store, target.db)
        found = target_conn.execute(
            f"SELECT 1 FROM {target.table} WHERE {target.pk_column} = ? LIMIT 1", (value,)
        ).fetchone()
        if found is None:
            raise XidTargetMissingError(
                f"{table}.{col} = {value!r} has no matching row in "
                f"{target.db}.{target.table}.{target.pk_column} (XID refused)"
            )


def _apply_event_redaction(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Design Section 4.2: the event table's payload gets a secret-redaction
    pass before write, applied here so every writer (M1's own tests, and
    every later module's ``events.append``) inherits it structurally."""
    if table != "event" or "payload" not in row or row["payload"] is None:
        return row
    raw = row["payload"]
    payload_obj = json.loads(raw) if isinstance(raw, str) else raw
    redacted_obj, count = redact_payload(payload_obj)
    row = dict(row)
    row["payload"] = json.dumps(redacted_obj, ensure_ascii=False)
    if row.get("redactions") is None:
        row["redactions"] = count
    return row


def insert(store: Store, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Validated insert. Refuses (raising, never a silent partial write):

    - an unknown table name (:class:`UnknownTableError`);
    - a row containing a column the table doesn't have (:class:`ValidationError`
      — "write w/ bad field refused");
    - a row that violates a NOT NULL/CHECK/UNIQUE constraint SQLite itself
      enforces (:class:`ValidationError`, wrapping the underlying
      ``sqlite3.IntegrityError`` with a clean message);
    - a row whose XID column names a target row that does not exist
      (:class:`XidTargetMissingError` — "XID write w/ missing target
      refused").

    Returns the row as written (post-redaction, if the ``event`` table's
    payload triggered one).
    """
    conn = store.conn_for_table(table)  # raises UnknownTableError for a bad table name
    columns = table_columns(conn, table)
    unknown = set(row) - columns
    if unknown:
        raise ValidationError(f"{table}: unknown column(s) {sorted(unknown)!r} (not in {sorted(columns)!r})")

    _validate_xids(store, table, row)
    row = _apply_event_redaction(table, dict(row))

    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    try:
        with conn:
            conn.execute(sql, [row[c] for c in cols])
    except sqlite3.IntegrityError as exc:
        raise ValidationError(f"{table}: integrity violation on insert: {exc}") from exc
    return row


def get(store: Store, table: str, *, pk_column: str, pk_value: Any) -> dict[str, Any] | None:
    """Fetch one row by primary key, or ``None``. Convenience used by
    tests and by ``trialerror.stores.bitemporal``; not a query engine (that's
    M8's retrieval layer)."""
    conn = store.conn_for_table(table)
    row = conn.execute(f"SELECT * FROM {table} WHERE {pk_column} = ?", (pk_value,)).fetchone()
    return dict(row) if row is not None else None


def update(
    store: Store,
    table: str,
    *,
    pk_column: str,
    pk_value: Any,
    changes: Mapping[str, Any],
) -> None:
    """Validated update of an existing row by primary key. Same unknown-
    column and XID checks as :func:`insert`; used by
    ``trialerror.stores.bitemporal`` for edge invalidation and available to
    later modules for any other same-row update that needs XID/column
    validation rather than a hand-rolled ``UPDATE`` statement."""
    conn = store.conn_for_table(table)
    columns = table_columns(conn, table)
    unknown = set(changes) - columns
    if unknown:
        raise ValidationError(f"{table}: unknown column(s) {sorted(unknown)!r} (not in {sorted(columns)!r})")
    _validate_xids(store, table, changes)

    set_clause = ", ".join(f"{c} = ?" for c in changes)
    params = list(changes.values()) + [pk_value]
    try:
        with conn:
            conn.execute(f"UPDATE {table} SET {set_clause} WHERE {pk_column} = ?", params)
    except sqlite3.IntegrityError as exc:
        raise ValidationError(f"{table}: integrity violation on update: {exc}") from exc
