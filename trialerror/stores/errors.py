"""Store-layer exceptions. Design Section 12 (M1 row): "validated write API
incl. XID cross-store target validation" — the API refuses, it never lets a
raw ``sqlite3`` exception (or a silent corruption) reach the caller.

All three are catchable as :class:`StoreError`; callers that only care "did
the write fail" can catch that one base class, callers that need to branch
on *why* (present it to the user vs. retry vs. bug) catch the specific
subclass.
"""

from __future__ import annotations

__all__ = [
    "StoreError",
    "ValidationError",
    "XidTargetMissingError",
    "MigrationError",
    "UnknownTableError",
]


class StoreError(Exception):
    """Base class for every error the ``trialerror.stores`` write API raises."""


class ValidationError(StoreError):
    """A write was refused: unknown column, NOT NULL/CHECK/UNIQUE violation,
    or any other same-file constraint SQLite itself enforces, translated
    into a clean message instead of a raw ``sqlite3.IntegrityError`` /
    ``sqlite3.OperationalError`` leaking to the caller."""


class XidTargetMissingError(StoreError):
    """A write was refused because a cross-store ``XID`` column's target row
    does not exist in the referenced database (design Section 4's binding
    cross-store reference rule: "refuse-on-missing" at write time)."""


class MigrationError(StoreError):
    """A migration could not be applied (bad statement, version conflict,
    or the ledger of applied versions is inconsistent with what the schema
    module declares)."""


class UnknownTableError(StoreError):
    """A table name was not found in any of the four DBs' table registries
    (``trialerror.stores.TABLE_DB``) — almost always a caller typo."""
