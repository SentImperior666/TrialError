"""Versioned migration runner, ``PRAGMA user_version``-gated (design Section
12, M1 row: "migration runner (numbered scripts)"; the MegaMemory-derived
pattern referenced in the build brief).

A schema module (``trialerror/stores/schema/<db>.py``) declares an ordered tuple
of :class:`Migration` objects, each a numbered, named batch of DDL
statements. :func:`apply_migrations` compares the DB file's current
``PRAGMA user_version`` against each migration's ``version`` and applies —
inside one transaction per migration, DDL included — only the ones that
haven't run yet. Re-running the exact same migration list against an
already-migrated DB is therefore a no-op: every ``version <= current`` is
skipped, so ``apply_migrations`` returns an empty list of newly-applied
versions on a second call (the acceptance criterion "migration up from
empty" plus "idempotency" both reduce to this one function).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from trialerror.stores.errors import MigrationError

__all__ = ["Migration", "current_version", "latest_version", "apply_migrations"]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise MigrationError(f"migration {self.name!r}: version must be >= 1, got {self.version}")


def current_version(conn: sqlite3.Connection) -> int:
    """The DB file's current ``PRAGMA user_version`` (0 for a fresh file)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def latest_version(migrations: Sequence[Migration]) -> int:
    """The highest version number declared across ``migrations`` (0 if empty)."""
    return max((m.version for m in migrations), default=0)


def apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> list[int]:
    """Apply every migration in ``migrations`` whose version is newer than
    the DB's current ``PRAGMA user_version``, in ascending version order.

    Each migration runs in its own transaction (its DDL statements plus the
    ``PRAGMA user_version`` bump that commits it as applied) so a failure
    partway through one migration cannot leave the version pointer ahead of
    what actually landed. Raises :class:`MigrationError` — wrapping the
    underlying ``sqlite3`` error — on any statement failure, and refuses a
    migration list with duplicate version numbers (a authoring bug, not a
    runtime one, but cheap to catch here).

    Returns the list of version numbers actually applied this call (empty
    on a no-op re-run).
    """
    ordered = sorted(migrations, key=lambda m: m.version)
    seen: set[int] = set()
    for m in ordered:
        if m.version in seen:
            raise MigrationError(f"duplicate migration version {m.version} ({m.name!r})")
        seen.add(m.version)

    start = current_version(conn)
    applied: list[int] = []
    for m in ordered:
        if m.version <= start:
            continue
        # NOTE: deliberately NOT `with conn:` here. Python's sqlite3 module
        # (under its default "legacy" transaction control, the only mode
        # portable across the py>=3.11 range this package targets) only
        # auto-opens an implicit transaction ahead of DML statements
        # (INSERT/UPDATE/DELETE/REPLACE) — a bare CREATE TABLE executes and
        # commits immediately, outside any transaction `with conn:` could
        # roll back. Explicit BEGIN/COMMIT/ROLLBACK is the only portable way
        # to make a DDL-heavy migration (this is nothing BUT DDL) actually
        # atomic — verified against the failure-path test in
        # tests/test_stores_migrate.py, which failed under `with conn:`
        # (the CREATE TABLE survived a later statement's syntax error)
        # before this fix.
        #
        # TRIALERROR-DEV-NOTE (schema-v2, build-v1-schemav2): ``PRAGMA
        # foreign_keys`` is toggled OFF/ON around the transaction, not
        # inside it, per SQLite's own documented "Making Other Kinds Of
        # Table Schema Changes" recipe (lang_altertable.html, steps 1/12) --
        # the pragma is a documented no-op when set WHILE a transaction is
        # already open (verified empirically: reading it back inside an
        # active `BEGIN` still reports the pre-toggle value), so it must be
        # issued before `BEGIN IMMEDIATE` and restored after `COMMIT`/
        # `ROLLBACK`, never as one of `m.statements`. This is what makes the
        # table-rebuild recipe (new table, copy, DROP the old one, RENAME
        # the new one into place) safe for a table with an existing same-DB
        # FK child that already has rows: SQLite refuses a bare `DROP TABLE`
        # on a still-referenced parent under enforcement (confirmed against
        # jobs.db's job/job_event pair -- job_event.job_id REFERENCES
        # job(job_id) -- which schema-v2's job.kind CHECK-constraint
        # migration rebuilds), even though the very next statement in the
        # same migration recreates that parent table under the identical
        # name before the transaction commits. Re-enabled unconditionally in
        # both the success and failure paths (`finally`) so a mid-migration
        # error never leaves the connection permanently FK-unchecked for
        # whatever the caller does with it next.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in m.statements:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {m.version:d}")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise MigrationError(f"migration {m.version} ({m.name!r}) failed: {exc}") from exc
            else:
                conn.execute("COMMIT")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        applied.append(m.version)
    return applied
