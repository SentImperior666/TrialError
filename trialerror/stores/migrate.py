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

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from trialerror.stores.errors import MigrationError
from trialerror.util.timeutil import now

__all__ = [
    "Migration",
    "current_version",
    "latest_version",
    "apply_migrations",
    "PROVENANCE_TABLE",
    "is_additive",
    "read_provenance",
]

#: The runner's OWN table, created by :func:`apply_migrations` in whichever DB
#: it is migrating -- one per DB, all four of them.
#:
#: F17: ``PRAGMA user_version`` is a single integer with no direction and no
#: history. An OLDER client opening a NEWER store read exactly like a client
#: opening a store it had half-migrated, and the doctor's schema-version check
#: reported the same flat "not on the expected version" for both -- so "upgrade
#: your client" and "this store is broken" were indistinguishable, and nothing
#: anywhere recorded WHEN a migration ran or WHICH client ran it.
#:
#: Deliberately NOT declared in any schema module's ``TABLES`` tuple:
#: ``trialerror.stores.store`` builds ``TABLE_DB`` from those tuples and refuses a
#: name declared in two DBs (this one lives in all four), and it is not a table
#: the validated write API should route to in the first place -- the migration
#: runner owns it end to end. It is likewise absent from ``XID_REGISTRY`` (it
#: holds no typed id) and from ``tests/_store_fixtures.py`` (nothing populates it
#: but the runner).
PROVENANCE_TABLE = "schema_migration"

_PROVENANCE_DDL = f"""
CREATE TABLE IF NOT EXISTS {PROVENANCE_TABLE} (
    version               INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    additive              INTEGER NOT NULL CHECK (additive IN (0,1)),
    applied_ts            TEXT,
    client_version        TEXT,
    client_schema_latest  INTEGER,
    provenance            TEXT NOT NULL CHECK (provenance IN ('recorded','backfilled'))
)
""".strip()

#: Statement prefixes that only ADD to a schema. A migration made of nothing
#: but these can be opened by an older client without it reading or writing a
#: shape it does not know: every table and column the older client knows is
#: still there, unchanged.
_ADDITIVE_PREFIXES = (
    "CREATE TABLE ",
    "CREATE INDEX ",
    "CREATE UNIQUE INDEX ",
    "CREATE VIRTUAL TABLE ",
)

#: ``ALTER TABLE <name> ADD COLUMN ...`` -- the other additive shape, and the
#: only ``ALTER`` that is one (``RENAME``, ``DROP COLUMN`` are not).
_ADD_COLUMN_RE = re.compile(r"^ALTER TABLE \S+ ADD COLUMN\b")


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


def is_additive(migration: Migration) -> bool:
    """Does ``migration`` only ADD to the schema?

    True iff EVERY statement, whitespace-normalised and uppercased, starts
    with ``CREATE TABLE`` / ``CREATE INDEX`` / ``CREATE UNIQUE INDEX`` /
    ``CREATE VIRTUAL TABLE``, or is an ``ALTER TABLE <name> ADD COLUMN``.
    Anything else -- a ``DROP``, a ``RENAME``, the ``INSERT ... SELECT`` of a
    table-rebuild recipe, an ``UPDATE`` backfill -- makes the whole migration
    non-additive.

    Computed MECHANICALLY from the DDL rather than read off a flag on
    :class:`Migration`, for two reasons: no migration author has to remember
    to set anything (and cannot set it wrongly), and a history of migrations
    written long before this function existed can be classified at backfill
    time exactly as a fresh one is.

    It is a conservative, syntactic answer to one question -- "could an older
    client still read and write every table and column it knows about?" -- and
    not a claim that the migration is semantically harmless.
    """
    statements = tuple(migration.statements)
    if not statements:
        return False
    for stmt in statements:
        normalized = " ".join(str(stmt).split()).upper()
        if any(normalized.startswith(prefix) for prefix in _ADDITIVE_PREFIXES):
            continue
        if _ADD_COLUMN_RE.match(normalized):
            continue
        return False
    return True


def _provenance_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (PROVENANCE_TABLE,)
    ).fetchone()
    return row is not None


def read_provenance(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every :data:`PROVENANCE_TABLE` row, ascending by version.

    An empty list when the table is absent -- a store migrated by a runner
    that predates it has no history to read, which is a fact about that store
    and not an error. Read-only throughout, so a caller holding a read-only
    connection (the doctor's schema-direction check) can use it.
    """
    if not _provenance_table_exists(conn):
        return []
    columns = (
        "version", "name", "additive", "applied_ts", "client_version",
        "client_schema_latest", "provenance",
    )
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {PROVENANCE_TABLE} ORDER BY version"
    ).fetchall()
    return [dict(zip(columns, tuple(row))) for row in rows]


def _record_provenance(
    conn: sqlite3.Connection,
    migration: Migration,
    *,
    client_version: str | None,
    client_schema_latest: int,
) -> None:
    """One ``recorded`` row for a migration this call just applied. Executed
    INSIDE that migration's own transaction, so a migration that fails rolls
    its provenance row back with everything else it was going to do."""
    conn.execute(
        f"INSERT OR REPLACE INTO {PROVENANCE_TABLE} "
        "(version, name, additive, applied_ts, client_version, client_schema_latest, provenance) "
        "VALUES (?, ?, ?, ?, ?, ?, 'recorded')",
        (
            migration.version,
            migration.name,
            1 if is_additive(migration) else 0,
            now(),
            client_version,
            client_schema_latest,
        ),
    )


def _backfill_provenance(conn: sqlite3.Connection, ordered: Sequence[Migration], start: int) -> None:
    """Create :data:`PROVENANCE_TABLE` and seed it with what this client can
    honestly say about the migrations that were applied BEFORE the table
    existed: their version, their name and whether their DDL is additive.

    ``applied_ts``, ``client_version`` and ``client_schema_latest`` stay NULL
    -- nobody recorded them at the time, and inventing them would make a
    backfilled row indistinguishable from a witnessed one. ``provenance =
    'backfilled'`` says which it is.

    A version the store is ON but this client does not KNOW (a store migrated
    by a newer client) gets no row at all: the runner has no name for it and
    will not make one up. That absence is itself the signal the doctor's
    schema-direction check reads.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_PROVENANCE_DDL)
        for m in ordered:
            if m.version > start:
                continue
            conn.execute(
                f"INSERT OR REPLACE INTO {PROVENANCE_TABLE} "
                "(version, name, additive, applied_ts, client_version, client_schema_latest, provenance) "
                "VALUES (?, ?, ?, NULL, NULL, NULL, 'backfilled')",
                (m.version, m.name, 1 if is_additive(m) else 0),
            )
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise MigrationError(f"could not create or backfill {PROVENANCE_TABLE}: {exc}") from exc
    else:
        conn.execute("COMMIT")


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    client_version: str | None = None,
) -> list[int]:
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

    F17 (lane FB-acq item 5): each applied migration additionally lands a row
    in :data:`PROVENANCE_TABLE` -- the version, its name, whether its DDL is
    :func:`is_additive`, the timestamp, and the client that ran it
    (``client_version``, defaulting to ``trialerror.__version__``) together with
    that client's own latest declared version. A store whose runner predates
    the table gets it created and BACKFILLED on the next call, from the
    migration list itself. None of this is a ``Migration``: there is no version
    bump and no ``TABLES`` entry, because the table belongs to the runner
    rather than to any one DB's schema.
    """
    if client_version is None:
        import trialerror

        client_version = trialerror.__version__

    ordered = sorted(migrations, key=lambda m: m.version)
    seen: set[int] = set()
    for m in ordered:
        if m.version in seen:
            raise MigrationError(f"duplicate migration version {m.version} ({m.name!r})")
        seen.add(m.version)

    start = current_version(conn)
    if not _provenance_table_exists(conn):
        _backfill_provenance(conn, ordered, start)
    client_schema_latest = latest_version(ordered)
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
                _record_provenance(
                    conn, m, client_version=client_version, client_schema_latest=client_schema_latest
                )
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise MigrationError(f"migration {m.version} ({m.name!r}) failed: {exc}") from exc
            else:
                conn.execute("COMMIT")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        applied.append(m.version)
    return applied
