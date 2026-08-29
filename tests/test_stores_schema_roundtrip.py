"""Acceptance criterion: "schema round-trip test per table." Inserts one
valid row into every base table across all four DBs (via
``_store_fixtures.populate_one_of_everything``) and confirms each row can
be read back byte-for-byte through the same write API, and that the total
table count matches the design's Section 4 (+ 9.6-9.8) enumeration.
"""

from __future__ import annotations

from trialerror.stores.store import SCHEMA_MODULES, TABLE_DB

from tests._store_fixtures import populate_one_of_everything

# design Section 4.1 (16 tables + build-v2-summary's "summary" = 17) +
# 4.2/9.6-9.8 (18 tables + build-v2-polish's ops_v3 "room_link" = 19 +
# build-v2dash-data's ops_v4 "criterion"/"feed_post_translation" = 21) +
# 4.3 (5 tables) + 4.4 (2 tables) = 45 base tables total. The two ops_v4
# additions are NOT in the original design's Section 4 enumeration -- they
# are the V2 dashboard redesign's own additive seams
# (docs/reviews/REDESIGN_V2_RATIONALE.md Section 5.3 items 6/8) -- counted
# here anyway since this test's real job is "every table TABLE_DB knows
# about has exactly one declared home," not a frozen historical count.
EXPECTED_TABLE_COUNT_BY_DB = {"platform": 5, "ops": 21, "knowledge": 17, "jobs": 2}


def test_table_counts_match_design_section_4():
    for db_kind, expected in EXPECTED_TABLE_COUNT_BY_DB.items():
        actual = len(SCHEMA_MODULES[db_kind].TABLES)
        assert actual == expected, f"{db_kind}: expected {expected} tables, schema module declares {actual}"
    assert len(TABLE_DB) == sum(EXPECTED_TABLE_COUNT_BY_DB.values()) == 45


def test_round_trip_one_row_per_table(store):
    ids = populate_one_of_everything(store)

    # every declared table got a row inserted by the fixture
    assert set(ids) == set(TABLE_DB)

    # spot-check round-trip reads across all four DBs, not just knowledge.
    acct = store.platform.execute(
        "SELECT * FROM account WHERE account_id = ?", (ids["account"],)
    ).fetchone()
    assert acct["label"] == "test account"

    sess = store.ops.execute("SELECT * FROM session WHERE session_id = ?", (ids["session"],)).fetchone()
    assert sess["account_id"] == ids["account"]
    assert sess["status"] == "open"

    src = store.knowledge.execute("SELECT * FROM source WHERE source_id = ?", (ids["source"],)).fetchone()
    assert src["title"] == "test source"
    assert src["registered_by_launch"] == ids["launch"]

    job = store.jobs.execute("SELECT * FROM job WHERE job_id = ?", (ids["job"],)).fetchone()
    assert job["state"] == "pending"

    # every base table across all four DBs has at least the one row the
    # fixture inserted (nothing dropped silently); `entity` legitimately
    # gets 2 (relation.src_entity/dst_entity need two distinct rows).
    for table, db_kind in TABLE_DB.items():
        conn = getattr(store, db_kind)
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        expected = 2 if table == "entity" else 1
        assert count == expected, f"{db_kind}.{table}: expected {expected} row(s), found {count}"


def test_chunk_fts_virtual_table_present(store):
    rows = store.knowledge.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = 'chunk_fts'"
    ).fetchall()
    assert len(rows) == 1
    # FTS5-backed: insertable/searchable like any table.
    store.knowledge.execute("INSERT INTO chunk_fts(chunk_id, text) VALUES (?, ?)", ("CHK-x", "hello world"))
    store.knowledge.commit()
    hit = store.knowledge.execute("SELECT chunk_id FROM chunk_fts WHERE chunk_fts MATCH 'hello'").fetchall()
    assert [r["chunk_id"] for r in hit] == ["CHK-x"]
