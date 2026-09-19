"""``trialerror.stores`` — DDL, migrations, and the validated write API for
TrialError's four SQLite-WAL databases (platform, ops, knowledge, jobs). Design
Section 12, M1 row: "DDL for knowledge/ops/jobs/platform DBs ... migration
runner ... validated write API incl. XID cross-store target validation;
redaction pass; FTS5+vec index setup."

This is the spine every other module writes through (design Section 4:
"All writes go through ``trialerror.stores``"). Public surface:

- :func:`open_store` / :class:`Store` — one handle per program, all four
  DBs migrated and connected.
- :func:`insert` / :func:`update` / :func:`get` — the validated write API.
- ``trialerror.stores.bitemporal`` — assert/expire/supersede/as_of over
  ``claim``/``relation``.
- ``trialerror.stores.vecindex`` — the sqlite-vec factory with pure-stdlib
  fallback.
- ``trialerror.stores.xid`` — the cross-store reference registry.
- ``trialerror.stores.errors`` — ``StoreError`` and its subclasses.

Nothing above this layer (M2-M15) opens ``sqlite3`` directly against a
TrialError store; everything routes through here.
"""

from __future__ import annotations

from trialerror.stores.errors import (
    MigrationError,
    StoreError,
    UnknownTableError,
    ValidationError,
    XidTargetMissingError,
)
from trialerror.stores.migrate import Migration, apply_migrations, current_version, latest_version
from trialerror.stores.store import Store, TABLE_DB, open_store
from trialerror.stores.writer import get, insert, table_columns, update

__all__ = [
    "Store",
    "TABLE_DB",
    "open_store",
    "insert",
    "update",
    "get",
    "table_columns",
    "Migration",
    "apply_migrations",
    "current_version",
    "latest_version",
    "StoreError",
    "ValidationError",
    "XidTargetMissingError",
    "MigrationError",
    "UnknownTableError",
]
