"""``knowledge_v9_idea_status_archived`` — the table rebuild that widens
``idea.status`` with ``archived``.

A round judged against its ARCHIVE (reference set R2) needs the prior
rounds' candidates and request rows to BE idea rows, and every pre-v9 status
says something false about them: ``raw`` puts them in the judged scope and
consolidates them at the end of a round they did not take part in,
``eliminated`` asserts a convergence ruling nobody made, ``merged`` asserts
a near-duplicate fold.

The same rebuild recipe v6 used on this very table, so what is tested here
is what a rebuild can lose: the rows, all twenty-three columns' values, the
other statuses' CHECK, and the foreign keys that resolve through it.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.migrate import apply_migrations, current_version, latest_version
from trialerror.stores.schema import knowledge

TS = "2026-09-16T09:00:00.000Z"

_STATUSES_BEFORE = ("raw", "consolidated", "promoted", "eliminated", "merged")


def _upto(migrations, max_version: int):
    return tuple(m for m in migrations if m.version <= max_version)


def _knowledge_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, version))
    return conn


def _insert_idea(conn: sqlite3.Connection, idea_id: str, status: str, **overrides) -> None:
    row = {
        "idea_id": idea_id,
        "round_id": "round-1",
        "author_launch": "LNCH-1",
        "body": "A mechanism stated in full.",
        "slice_ref": '{"arm": "near"}',
        "feed_post_ref": None,
        "status": status,
        "created_ts": TS,
        "home": "family-a/row-1",
        "assumed_circle": "a small table",
        "provenance": '{"docs": ["DOC-1"]}',
        "tier": "near",
        "set_distance": 0.25,
        "requirements": "- one line",
        "recipe_card": "TRANSFER",
        "operation_declared": "bridge/formalize",
        "probe": "simulate twenty turns",
        "surprise": "the rate falls",
        "author_rationale": "the track holds the state",
        "parent_ids": '["IDEA-0"]',
        "statement_sha256": "a" * 64,
        "corpus_snapshot_id": "SNAP-1",
        "convergent_with": '["IDEA-9"]',
        **overrides,
    }
    conn.execute(
        f"INSERT INTO idea ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values())
    )
    conn.commit()


def test_v9_is_the_knowledge_migration_that_added_archived():
    """Not the latest any more -- lane FB-6 added v10 (``vec_ideas``) -- but
    still v9, and still under its own name: a migration that renumbered
    would re-run on a store that had already applied it."""
    assert latest_version(knowledge.MIGRATIONS) >= 9
    v9 = next(m for m in knowledge.MIGRATIONS if m.version == 9)
    assert v9.name == "knowledge_v9_idea_status_archived"


def test_archived_is_accepted_after_v9_and_refused_before_it():
    before = _knowledge_at(8)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_idea(before, "IDEA-a", "archived")

    after = _knowledge_at(9)
    _insert_idea(after, "IDEA-a", "archived")
    assert after.execute("SELECT status FROM idea WHERE idea_id='IDEA-a'").fetchone()["status"] == "archived"
    assert current_version(after) == 9


def test_every_other_status_still_round_trips_and_a_bogus_one_is_still_refused():
    conn = _knowledge_at(9)
    for i, status in enumerate(_STATUSES_BEFORE):
        _insert_idea(conn, f"IDEA-{i}", status)
    kept = {r["idea_id"]: r["status"] for r in conn.execute("SELECT idea_id, status FROM idea")}
    assert sorted(kept.values()) == sorted(_STATUSES_BEFORE)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_idea(conn, "IDEA-bogus", "shelved")


def test_the_rebuild_carries_every_column_value_across():
    conn = _knowledge_at(8)
    _insert_idea(conn, "IDEA-keep", "consolidated")
    before = dict(conn.execute("SELECT * FROM idea WHERE idea_id='IDEA-keep'").fetchone())
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, 9))
    after = dict(conn.execute("SELECT * FROM idea WHERE idea_id='IDEA-keep'").fetchone())
    assert after == before
    assert len(after) == 23


def test_the_rebuild_leaves_no_dangling_reference():
    conn = _knowledge_at(9)
    _insert_idea(conn, "IDEA-keep", "raw")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not [
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'idea__%'")
    ]
