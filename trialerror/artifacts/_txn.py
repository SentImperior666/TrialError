"""Private transaction-mechanics helpers shared by :mod:`trialerror.artifacts.gates`
and :mod:`trialerror.artifacts.registry`.

TRIALERROR-DEV-NOTE (same split as ``trialerror/law/service.py``): ``trialerror.stores.
writer.insert``/``update`` each commit per call (``with conn:`` internally)
— calling either twice in a row for e.g. ``gate`` then ``gate_transition``
would NOT be one transaction. M10's binding contract is "register-then-
close-gate ordering in ONE transaction" (and, symmetrically, "advance a
gate's state + log its ``gate_transition`` row" in one transaction) so
these two helpers reuse ``trialerror.stores.writer.table_columns`` (read-only,
already exported) for the same unknown-column validation M1's writer
performs, and issue their own parameterized INSERT/UPDATE under the
caller's already-open ``BEGIN IMMEDIATE`` transaction — the exact pattern
``trialerror.law.service._raw_insert`` established and ``trialerror.stores.migrate.
apply_migrations`` uses for the same reason (DDL/DML that must land as one
unit under Python sqlite3's legacy transaction control).

Neither helper applies XID validation — every XID column this subsystem
writes (``artifact.registered_by_launch``, ``gate.critic_launch``,
``gate_transition.by_launch``) is validated by the PUBLIC functions in
``gates.py``/``registry.py`` before entering the transaction (so a bad
launch id is refused before any row is touched, keeping the "no partial
write on a doomed call" property ``append_ruling`` demonstrates) — see
each call site's own comment for why the specific XID column in play is
covered.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from trialerror.stores.errors import ValidationError
from trialerror.stores.writer import table_columns

__all__ = ["raw_insert", "raw_update"]


def raw_insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    """Same unknown-column validation as ``trialerror.stores.writer.insert``,
    minus the auto-commit."""
    columns = table_columns(conn, table)
    unknown = set(row) - columns
    if unknown:
        raise ValidationError(f"{table}: unknown column(s) {sorted(unknown)!r} (not in {sorted(columns)!r})")
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, [row[c] for c in cols])


def raw_update(
    conn: sqlite3.Connection,
    table: str,
    *,
    pk_column: str,
    pk_value: Any,
    changes: Mapping[str, Any],
) -> None:
    """Same unknown-column validation as ``trialerror.stores.writer.update``,
    minus the auto-commit. Raises :class:`ValidationError` if ``pk_value``
    does not name an existing row (a silent no-op UPDATE would otherwise
    let a caller believe a state change landed when it did not)."""
    columns = table_columns(conn, table)
    unknown = set(changes) - columns
    if unknown:
        raise ValidationError(f"{table}: unknown column(s) {sorted(unknown)!r} (not in {sorted(columns)!r})")
    set_clause = ", ".join(f"{c} = ?" for c in changes)
    params = list(changes.values()) + [pk_value]
    cur = conn.execute(f"UPDATE {table} SET {set_clause} WHERE {pk_column} = ?", params)
    if cur.rowcount == 0:
        raise ValidationError(f"{table}: no row with {pk_column} = {pk_value!r} (update matched nothing)")
