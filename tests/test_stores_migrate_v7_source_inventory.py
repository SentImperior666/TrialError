"""``knowledge_v7_source_kind_inventory`` — the table-rebuild migration that
widens ``source.kind`` with ``inventory``.

``source`` is a harder rebuild than ``idea`` was in v6, in two ways this
file tests directly rather than reading off the DDL:

1. Two other tables reference it (``document.source_id``,
   ``web_fetch.source_id``), so the DROP happens while live FK clauses name
   the table being dropped. That is legal only under the migration runner's
   own ``PRAGMA foreign_keys`` bracketing, and the references must resolve
   again after the rename — asserted here by writing a real document
   against a migrated source and by running ``PRAGMA foreign_key_check``.
2. ``source`` carries an index (the partial UNIQUE on ``content_sha256``
   that makes a silent duplicate registration impossible). An index dies
   with its table. Losing it fails nothing loudly — dedup just quietly
   stops working — so its survival is asserted by trying the duplicate
   insert the index exists to refuse.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.migrate import apply_migrations, current_version, latest_version
from trialerror.stores.schema import knowledge

TS = "2026-09-07T09:00:00.000Z"

_SOURCE_COLUMNS = (
    "source_id", "kind", "title", "authors", "year", "venue", "url", "doi", "arxiv_id",
    "isbn", "content_sha256", "license_tier", "acquisition_route", "rights_notes",
    "request_state", "requested_ts", "delivered_ts", "registered_ts",
    "registered_by_launch", "dedup_of",
)


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _upto(migrations, max_version: int):
    return tuple(m for m in migrations if m.version <= max_version)


def _knowledge_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, version))
    return conn


def _insert_source(conn: sqlite3.Connection, **overrides) -> tuple:
    values = {
        "source_id": "SRC-a", "kind": "paper", "title": "A Title", "authors": "A. Author",
        "year": 2026, "venue": "A Venue", "url": None, "doi": None, "arxiv_id": None,
        "isbn": None, "content_sha256": None, "license_tier": "open",
        "acquisition_route": "web", "rights_notes": None, "request_state": "indexed",
        "requested_ts": None, "delivered_ts": None, "registered_ts": TS,
        "registered_by_launch": "LNCH-a", "dedup_of": None,
    }
    values.update(overrides)
    ordered = tuple(values[c] for c in _SOURCE_COLUMNS)
    conn.execute(
        f"INSERT INTO source ({', '.join(_SOURCE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_SOURCE_COLUMNS))})",
        ordered,
    )
    return ordered


def test_v7_is_contiguous_and_uniquely_named():
    versions = [m.version for m in knowledge.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert len({m.name for m in knowledge.MIGRATIONS}) == len(knowledge.MIGRATIONS)
    v7 = next(m for m in knowledge.MIGRATIONS if m.version == 7)
    assert v7.name == "knowledge_v7_source_kind_inventory"


def test_v6_refuses_the_inventory_kind_and_v7_accepts_it():
    """The before/after that makes this migration worth having."""
    at_v6 = _knowledge_at(6)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(at_v6, kind="inventory")
    at_v6.rollback()

    apply_migrations(at_v6, knowledge.MIGRATIONS)
    _insert_source(at_v6, kind="inventory")
    assert at_v6.execute("SELECT kind FROM source WHERE source_id='SRC-a'").fetchone()["kind"] == "inventory"


def test_v7_still_refuses_a_kind_outside_the_widened_check():
    conn = _knowledge_at(7)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(conn, kind="register")


def test_the_rebuild_carries_every_source_column_value_across():
    conn = _knowledge_at(6)
    values = _insert_source(
        conn,
        kind="rulebook", authors="B. Author", url="https://example.invalid/x",
        doi="10.0000/x", arxiv_id="2601.00001", isbn="978-0-000-00000-0",
        content_sha256="a" * 64, license_tier="commercial_restricted",
        acquisition_route="user_scan", rights_notes="owner-supplied scan",
        request_state="archived", requested_ts=TS, delivered_ts=TS,
    )
    conn.commit()

    apply_migrations(conn, knowledge.MIGRATIONS)

    row = conn.execute("SELECT * FROM source WHERE source_id = 'SRC-a'").fetchone()
    assert tuple(row[c] for c in _SOURCE_COLUMNS) == values


def test_the_content_sha_unique_index_survives_the_rebuild():
    """The index exists so a duplicate registration is structurally
    impossible, not merely discouraged. A rebuild that forgot to recreate it
    would pass every other test in this file."""
    conn = _knowledge_at(7)
    _insert_source(conn, source_id="SRC-a", content_sha256="b" * 64)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(conn, source_id="SRC-b", content_sha256="b" * 64)
    conn.rollback()
    # ... and the index is still PARTIAL: two NULL hashes do not collide.
    _insert_source(conn, source_id="SRC-c", content_sha256=None)
    _insert_source(conn, source_id="SRC-d", content_sha256=None)
    assert conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 3


def test_documents_pointing_at_a_migrated_source_still_resolve():
    """The FK half: ``document.source_id`` names ``source`` by name, and the
    migration drops the table that name pointed at. If the reference did not
    re-resolve after the rename, this insert would fail (or, worse,
    ``foreign_key_check`` would report the row as an orphan)."""
    conn = _knowledge_at(6)
    _insert_source(conn)
    conn.commit()
    apply_migrations(conn, knowledge.MIGRATIONS)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO document (doc_id, source_id, rel_path, media_type, normalizer_id, "
        "normalizer_version, sha256, status) VALUES (?,?,?,?,?,?,?,?)",
        ("DOC-a", "SRC-a", "archive/a.md", "md", "fixture", "1", "0" * 64, "registered"),
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO document (doc_id, source_id, rel_path, media_type, normalizer_id, "
            "normalizer_version, sha256, status) VALUES (?,?,?,?,?,?,?,?)",
            ("DOC-b", "SRC-missing", "archive/b.md", "md", "fixture", "1", "0" * 64, "registered"),
        )


@pytest.mark.parametrize("prefix", [1, 2, 3, 4, 5, 6])
def test_stepping_from_any_earlier_version_lands_the_same_schema(prefix: int):
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, knowledge.MIGRATIONS)

    stepped = sqlite3.connect(":memory:")
    apply_migrations(stepped, _upto(knowledge.MIGRATIONS, prefix))
    apply_migrations(stepped, knowledge.MIGRATIONS)

    assert current_version(stepped) == latest_version(knowledge.MIGRATIONS)
    assert _schema_snapshot(stepped) == _schema_snapshot(fresh)


def test_reapplying_v7_is_a_noop():
    """v7 is a table REBUILD, so running it twice would drop and recreate
    ``source`` for nothing. Asserted as "no migration at or below 7 is
    applied again" rather than as "nothing at all is applied", so the
    assertion stays about v7 when a later migration lands."""
    conn = _knowledge_at(7)
    applied = apply_migrations(conn, knowledge.MIGRATIONS)
    assert [m for m in applied if m <= 7] == []
    assert current_version(conn) == latest_version(knowledge.MIGRATIONS)


def test_the_rebuilt_table_is_named_source_and_the_scratch_table_is_gone():
    conn = _knowledge_at(7)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "source" in names
    assert "source__v7new" not in names


def test_the_cli_kind_choices_and_the_check_constraint_are_the_same_list():
    """A CLI whose ``--kind`` choices drift from the constraint either
    refuses a legal kind or lets an illegal one reach a DB round-trip. Both
    now read :data:`trialerror.ingest.pipeline.SOURCE_KINDS`; this asserts
    that constant against the DDL itself."""
    from trialerror.ingest.pipeline import SOURCE_KINDS

    conn = _knowledge_at(7)
    for kind in SOURCE_KINDS:
        _insert_source(conn, source_id=f"SRC-{kind}", kind=kind)
    assert conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] == len(SOURCE_KINDS)
