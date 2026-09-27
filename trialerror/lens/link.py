"""The accumulating lens-launch link (``lens_assignment_launch``, ops
schema-v11) and the one way everything reads it.

A lens is a NAME, and a name can hold several launches: a round that spawns
its agents per phase (or re-spawns one after a cut-off) books the same lens
more than once, against the same assignment rows. The original link was a
single column on the assignment row -- ``lens_assignment.lens_launch_id`` --
so the second booking OVERWROTE the first, moving the join that the first
launch's records and its feed post hang off. Booking the second launch
without assign ids kept that join intact but left the second launch with no
slice binding at all: outside the retrieval scope, and invisible to
``lens_citations_within_slice``. Neither reading was true about the round
that was actually run.

``lens_assignment_launch`` holds every (assignment, launch) pair. The column
stays, FIRST-WINS -- ``book_launch`` writes it only where it is NULL -- so
every pre-v11 reader keeps resolving the launch it always did, and the
readers here resolve the UNION of both: a launch is bound if the link table
names it, or if the column does. The column half is the fallback that keeps
these functions honest against a store that has not been migrated yet
(:func:`link_table_present`), which is exactly the store a read-only doctor
pass can be handed.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

__all__ = [
    "LINK_TABLE",
    "link_table_present",
    "assignment_rows_for_launch",
    "assign_ids_for_launch",
    "bound_assignment_count",
    "assignment_launch_pairs",
]

#: The link table's name, in one place so a reader and the migration cannot
#: drift apart.
LINK_TABLE = "lens_assignment_launch"

#: The columns a caller of :func:`assignment_rows_for_launch` may ask for.
#: The SELECT list is interpolated into the statement (a column list cannot
#: be a bound parameter), so it is taken from this set rather than from the
#: caller's string.
_SELECTABLE = frozenset({"*", "assign_id", "slice_spec", "roster_id"})


def link_table_present(conn: sqlite3.Connection) -> bool:
    """Whether this ops connection's file actually carries the v11 link
    table.

    A doctor pass opens whatever store it is pointed at, read-only, and a
    store one version behind is a store that has not been migrated YET --
    not a fault to report from inside a slice lookup. Every reader below
    degrades to the pre-v11 column when this is ``False``."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (LINK_TABLE,)
    ).fetchone()
    return row is not None


def assignment_rows_for_launch(
    conn: sqlite3.Connection, launch_id: str | None, *, columns: str = "*"
) -> list[Any]:
    """Every ``lens_assignment`` row bound to ``launch_id``, by the link
    table or by the legacy column -- the "which assignment rows belong to
    launch X" question, asked once.

    Empty list for a launch nothing is bound to; the caller decides whether
    that means "not a lens launch" (it does, everywhere this is used today)."""
    if columns not in _SELECTABLE:
        raise ValueError(f"assignment_rows_for_launch: columns must be one of {sorted(_SELECTABLE)!r}")
    if not launch_id:
        return []
    launch = str(launch_id)
    if link_table_present(conn):
        return conn.execute(
            f"SELECT {columns} FROM lens_assignment WHERE assign_id IN "
            f"(SELECT assign_id FROM {LINK_TABLE} WHERE launch_id = ?) OR lens_launch_id = ?",
            (launch, launch),
        ).fetchall()
    return conn.execute(
        f"SELECT {columns} FROM lens_assignment WHERE lens_launch_id = ?", (launch,)
    ).fetchall()


def assign_ids_for_launch(conn: sqlite3.Connection, launch_id: str | None) -> list[str]:
    """The assign ids bound to ``launch_id``, sorted."""
    rows = assignment_rows_for_launch(conn, launch_id, columns="assign_id")
    return sorted({str(row["assign_id"]) for row in rows})


def bound_assignment_count(conn: sqlite3.Connection) -> int:
    """How many ``lens_assignment`` rows name a lens launch at all -- the
    number the citation audit's skip message reports, counted the same way
    the audit itself resolves a launch."""
    if link_table_present(conn):
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM lens_assignment WHERE lens_launch_id IS NOT NULL "
            f"OR assign_id IN (SELECT assign_id FROM {LINK_TABLE})"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM lens_assignment WHERE lens_launch_id IS NOT NULL"
        ).fetchone()
    return int(row["n"])


def assignment_launch_pairs(
    conn: sqlite3.Connection, assign_ids: Sequence[str]
) -> list[tuple[str, str]]:
    """``(assign_id, launch_id)`` for every launch bound to any of
    ``assign_ids``, oldest binding first within an assignment.

    The bulk form of :func:`assignment_rows_for_launch`'s inverse, for a
    caller listing a LENS's launches rather than resolving one launch's
    slice. Ordering is by ``bound_ts`` so "the first binding" is the first
    pair returned for an assignment -- the legacy column's own launch, on
    any store the migration has touched."""
    ids = [str(a) for a in assign_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if link_table_present(conn):
        for row in conn.execute(
            f"SELECT assign_id, launch_id FROM {LINK_TABLE} WHERE assign_id IN ({placeholders}) "
            "ORDER BY bound_ts, rowid",
            ids,
        ).fetchall():
            pair = (str(row["assign_id"]), str(row["launch_id"]))
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    for row in conn.execute(
        f"SELECT assign_id, lens_launch_id FROM lens_assignment "
        f"WHERE lens_launch_id IS NOT NULL AND assign_id IN ({placeholders})",
        ids,
    ).fetchall():
        pair = (str(row["assign_id"]), str(row["lens_launch_id"]))
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs
