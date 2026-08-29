"""M1 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m0_acceptance.py`` convention: this file IS the
acceptance-criteria mapping, each test here re-running (not replacing) a
narrower assertion that already lives in its dedicated module.

    | Acceptance criterion (design Section 12, M1 row)                 | Test |
    |--------------------------------------------------------------------|------|
    | schema round-trip test per table                                   | test_schema_round_trip_per_table (see test_stores_schema_roundtrip.py) |
    | write w/ bad field refused                                          | test_write_with_bad_field_refused (see test_stores_writer.py) |
    | XID write w/ missing target refused                                 | test_xid_write_with_missing_target_refused (see test_stores_xid.py) |
    | migration up from empty                                             | test_migration_up_from_empty (see test_stores_migrate.py) |
    | concurrent-writer test (2 procs, 1k appends, zero loss)             | see test_stores_concurrency.py (subprocess-heavy; not duplicated here) |

Build-brief adversarial additions (M1 kickoff brief, not narrower design
wording but explicitly required by it):

    | Adversarial case                                                   | Test |
    |--------------------------------------------------------------------|------|
    | migration idempotency (re-run = no-op)                              | test_migration_reapply_is_noop (see test_stores_migrate.py) |
    | bi-temporal edge invalidation correctness (as_of returns right edge)| test_bitemporal_as_of_returns_correct_edge_version (see test_stores_bitemporal.py) |
"""

from __future__ import annotations

import pytest

from trialerror.stores import insert
from trialerror.stores.bitemporal import as_of, assert_fact, supersede_fact
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.migrate import apply_migrations, current_version
from trialerror.stores.schema import ops as ops_schema
from trialerror.stores.store import TABLE_DB
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything

pytestmark = pytest.mark.acceptance


def test_schema_round_trip_per_table(store):
    ids = populate_one_of_everything(store)
    assert set(ids) == set(TABLE_DB)
    src = store.knowledge.execute("SELECT title FROM source WHERE source_id = ?", (ids["source"],)).fetchone()
    assert src["title"] == "test source"


def test_write_with_bad_field_refused(store):
    with pytest.raises(ValidationError):
        insert(store, "account", {"account_id": new_id("ACC"), "label": "x", "created_ts": now(), "nope": 1})


def test_xid_write_with_missing_target_refused(store):
    with pytest.raises(XidTargetMissingError):
        insert(
            store,
            "session",
            {"session_id": new_id("SESS"), "account_id": "ACC-nonexistent", "opened_ts": now(), "status": "open"},
        )


def test_migration_up_from_empty():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    applied = apply_migrations(conn, ops_schema.MIGRATIONS)
    assert applied
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert set(ops_schema.TABLES) <= tables


def test_migration_reapply_is_noop():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, ops_schema.MIGRATIONS)
    v1 = current_version(conn)
    second = apply_migrations(conn, ops_schema.MIGRATIONS)
    assert second == []
    assert current_version(conn) == v1


def test_bitemporal_as_of_returns_correct_edge_version(store):
    ids = populate_one_of_everything(store)
    T1, T2 = "2026-01-01T00:00:00.000Z", "2026-06-01T00:00:00.000Z"
    c1 = new_id("CLM")
    assert_fact(
        store,
        "claim",
        {
            "claim_id": c1,
            "text": "before",
            "kind": "finding",
            "anchor_id": ids["quote_anchor"],
            "created_by_launch": ids["launch"],
            "created_at": T1,
        },
    )
    c2 = new_id("CLM")
    supersede_fact(
        store,
        "claim",
        c1,
        {"text": "after", "kind": "finding", "anchor_id": ids["quote_anchor"], "created_by_launch": ids["launch"]},
        new_id_column="claim_id",
        new_id_value=c2,
        tx_at=T2,
    )
    before_rows = as_of(store, "claim", tx_at="2026-03-01T00:00:00.000Z", where="claim_id IN (?,?)", params=(c1, c2))
    after_rows = as_of(store, "claim", tx_at="2026-09-01T00:00:00.000Z", where="claim_id IN (?,?)", params=(c1, c2))
    assert [r["text"] for r in before_rows] == ["before"]
    assert [r["text"] for r in after_rows] == ["after"]
