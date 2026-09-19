"""``knowledge_v8_verdict_label_canonical`` — the column that lets a round
spell its own label vocabulary without breaking every count downstream.

The judged novelty screen can now be handed a round's own labels plus a
canonical mapping onto the design's fixed ones (lane FB-5 item 2). Both
halves have to be on the ROW: ``label`` so the round reads back what its
judge actually returned, ``label_canonical`` so an adjudication draft, a
gate check or a report that counts ``same`` keeps reading one vocabulary
however many rounds spell it differently.

A plain ADD COLUMN, so what is tested here is small and exact: the column
arrives, the rows that were already there keep their values with NULL
beside them (which says "``label`` is already canonical"), and nothing else
about the table moves.
"""

from __future__ import annotations

import sqlite3

from trialerror.stores.migrate import apply_migrations, current_version, latest_version
from trialerror.stores.schema import knowledge

TS = "2026-09-16T09:00:00.000Z"


def _upto(migrations, max_version: int):
    return tuple(m for m in migrations if m.version <= max_version)


def _knowledge_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, version))
    return conn


def _insert_verdict(conn: sqlite3.Connection, verdict_id: str, label: str) -> None:
    conn.execute(
        "INSERT INTO verdict (verdict_id, subject_kind, subject_id, procedure, procedure_version, "
        "label, evidence, ts, issued_by_launch) VALUES (?,?,?,?,?,?,?,?,?)",
        (verdict_id, "claim", "IDEA-1", "custom", "novelty-v2", label, "[]", TS, "LNCH-1"),
    )
    conn.commit()


def test_v8_is_a_knowledge_migration_by_that_name():
    v8 = next(m for m in knowledge.MIGRATIONS if m.version == 8)
    assert v8.name == "knowledge_v8_verdict_label_canonical"
    assert latest_version(knowledge.MIGRATIONS) >= 8


def test_the_column_arrives_and_is_nullable():
    conn = _knowledge_at(8)
    columns = {r["name"]: r for r in conn.execute("PRAGMA table_info(verdict)")}
    assert "label_canonical" in columns
    assert columns["label_canonical"]["notnull"] == 0
    assert columns["label_canonical"]["type"] == "TEXT"
    assert current_version(conn) == 8


def test_a_row_written_before_v8_keeps_its_label_and_reads_null_beside_it():
    conn = _knowledge_at(7)
    _insert_verdict(conn, "VRD-old", "R3:new-mechanism")
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, 8))
    row = conn.execute("SELECT label, label_canonical FROM verdict WHERE verdict_id = 'VRD-old'").fetchone()
    assert row["label"] == "R3:new-mechanism"
    # NULL, not a copy of `label`: a procedure with ONE vocabulary has
    # nothing to map, and duplicating the value would assert a mapping
    # nobody declared.
    assert row["label_canonical"] is None


def test_the_rest_of_the_verdict_table_is_untouched():
    before = {r["name"] for r in _knowledge_at(7).execute("PRAGMA table_info(verdict)")}
    after = {r["name"] for r in _knowledge_at(8).execute("PRAGMA table_info(verdict)")}
    assert after - before == {"label_canonical"}
    assert not before - after


def test_a_v8_row_carries_both_halves():
    conn = _knowledge_at(8)
    conn.execute(
        "INSERT INTO verdict (verdict_id, subject_kind, subject_id, procedure, procedure_version, "
        "label, label_canonical, evidence, ts, issued_by_launch) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("VRD-new", "claim", "IDEA-2", "custom", "novelty-v2", "R4:present", "R4:stated", "[]", TS, "LNCH-1"),
    )
    conn.commit()
    row = conn.execute("SELECT label, label_canonical FROM verdict WHERE verdict_id = 'VRD-new'").fetchone()
    assert (row["label"], row["label_canonical"]) == ("R4:present", "R4:stated")
