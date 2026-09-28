"""Lane FB-acq item 4 (F16), the store half: knowledge schema v14's two
``source`` columns, what NULL means in them, and the one thing this item
deliberately does NOT change -- the fence, which still reads the route-derived
``license_tier`` and knows nothing about a grant.
"""

from __future__ import annotations

import sqlite3

from trialerror.ingest import pipeline
from trialerror.stores.migrate import apply_migrations, is_additive, latest_version, read_provenance
from trialerror.stores.schema import knowledge as knowledge_schema
from tests._ingest_fixtures import bootstrap_launch

_GRANT_COLUMNS = {"license_grant", "license_grant_source"}


def _version_of(name_fragment: str) -> int:
    return next(m.version for m in knowledge_schema.MIGRATIONS if name_fragment in m.name)


def test_the_migration_adds_two_nullable_columns_to_an_existing_store(tmp_path):
    """A store that stops one version short, with a row already in it, gains
    both columns as NULL -- which is what "nobody read a grant for this row"
    looks like for every row registered before this migration."""
    v14 = _version_of("source_license_grant")
    path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        apply_migrations(conn, tuple(m for m in knowledge_schema.MIGRATIONS if m.version < v14))
        before = {r["name"] for r in conn.execute("PRAGMA table_info(source)")}
        assert not (_GRANT_COLUMNS & before)
        conn.execute(
            "INSERT INTO source (source_id, kind, title, license_tier, acquisition_route, request_state, "
            "registered_ts, registered_by_launch) VALUES "
            "('SRC-example', 'paper', 'Example Paper', 'open', 'author_posted', 'delivered', "
            "'2026-01-01T00:00:00Z', 'LNCH-example')"
        )
        conn.commit()

        # v14 and whatever landed after it (lane SI part B added v15), in order
        assert apply_migrations(conn, knowledge_schema.MIGRATIONS) == [
            m.version for m in knowledge_schema.MIGRATIONS if m.version >= v14
        ]

        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version(knowledge_schema.MIGRATIONS)
        after = {r["name"] for r in conn.execute("PRAGMA table_info(source)")}
        assert _GRANT_COLUMNS <= after
        row = conn.execute("SELECT * FROM source WHERE source_id = 'SRC-example'").fetchone()
        assert row["license_grant"] is None
        assert row["license_grant_source"] is None
        # and nothing else about the row moved
        assert row["license_tier"] == "open"
        assert row["acquisition_route"] == "author_posted"
    finally:
        conn.close()


def test_the_migration_classifies_as_additive_and_is_recorded_as_such(tmp_path):
    """ADD COLUMN only, so ``is_additive`` says yes by its own mechanical rule
    -- which is what makes an older client opening a v14 store a doctor WARNING
    ("upgrade the client") rather than a failure. This is also the first
    migration the provenance table RECORDS rather than backfills."""
    v14 = _version_of("source_license_grant")
    migration = next(m for m in knowledge_schema.MIGRATIONS if m.version == v14)
    assert is_additive(migration) is True

    path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        apply_migrations(conn, knowledge_schema.MIGRATIONS)
        rows = {row["version"]: row for row in read_provenance(conn)}
        recorded = rows[v14]
        assert recorded["provenance"] == "recorded"
        assert recorded["name"] == "knowledge_v14_source_license_grant"
        assert recorded["additive"] == 1
        assert recorded["applied_ts"]
        assert recorded["client_version"]
        assert recorded["client_schema_latest"] == latest_version(knowledge_schema.MIGRATIONS)
    finally:
        conn.close()


def test_register_source_round_trips_both_grant_values(store):
    launch_id = bootstrap_launch(store)

    row = pipeline.register_source(
        store, kind="paper", title="Example Paper", license_tier="open", acquisition_route="author_posted",
        registered_by_launch=launch_id, doi="10.1000/example",
        license_grant="arxiv-nonexclusive-distrib-1.0", license_grant_source="arxiv_oai",
    )

    assert row["license_grant"] == "arxiv-nonexclusive-distrib-1.0"
    assert row["license_grant_source"] == "arxiv_oai"
    stored = store.knowledge.execute(
        "SELECT license_grant, license_grant_source FROM source WHERE source_id = ?", (row["source_id"],)
    ).fetchone()
    assert stored["license_grant"] == "arxiv-nonexclusive-distrib-1.0"
    assert stored["license_grant_source"] == "arxiv_oai"


def test_register_source_leaves_both_null_when_nobody_read_a_grant(store):
    launch_id = bootstrap_launch(store)

    row = pipeline.register_source(
        store, kind="paper", title="Example Paper", license_tier="unknown",
        acquisition_route="user_delivered", registered_by_launch=launch_id, request_state="wanted",
    )

    assert row["license_grant"] is None
    assert row["license_grant_source"] is None


def test_the_fence_still_reads_the_tier_and_knows_nothing_about_a_grant(store):
    """The half of F16 this item deliberately does NOT change. A restricted
    source is still fenced whatever its grant says, and an open one is still
    unfenced whatever its grant says -- the tier means exactly what it meant,
    and changing that is a later decision this item only supplies data for."""
    from trialerror.retrieve import fence

    launch_id = bootstrap_launch(store)
    restricted = pipeline.register_source(
        store, kind="book", title="Example Restricted Work", license_tier="commercial_restricted",
        acquisition_route="user_delivered", registered_by_launch=launch_id,
        license_grant="cc-by-4.0", license_grant_source="unpaywall_best_oa_location",
    )
    open_row = pipeline.register_source(
        store, kind="paper", title="Example Paper", license_tier="open", acquisition_route="author_posted",
        registered_by_launch=launch_id,
        license_grant="arxiv-nonexclusive-distrib-1.0", license_grant_source="arxiv_oai",
    )

    assert fence.is_fenced_license(restricted["license_tier"]) is True
    assert fence.is_fenced_license(open_row["license_tier"]) is False
    assert "license_grant" not in fence.FENCED_LICENSE_TIERS
