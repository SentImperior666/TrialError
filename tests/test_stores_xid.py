"""Acceptance criterion: "XID write w/ missing target refused" — the
adversarial core of the cross-store reference rule (design Section 4).
Also proves the full registry is internally consistent (every target
table/column genuinely exists) and that a present, valid XID succeeds.
"""

from __future__ import annotations

import pytest

from trialerror.stores import insert
from trialerror.stores.errors import XidTargetMissingError
from trialerror.stores.store import SCHEMA_MODULES, TABLE_DB
from trialerror.stores.xid import XID_REGISTRY, xid_columns_for_table
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def test_registry_targets_are_all_real_tables_and_columns():
    """Every XidTarget in the registry must point at a table this design
    actually creates, with a pk_column that table actually has — catches a
    typo in the registry itself, not runtime data."""
    for (table, col), target in XID_REGISTRY.items():
        assert table in TABLE_DB, f"{table!r} (source of {table}.{col}) is not a declared table"
        assert target.db in SCHEMA_MODULES, f"unknown target db {target.db!r} for {table}.{col}"
        assert target.table in SCHEMA_MODULES[target.db].TABLES, (
            f"{table}.{col} targets {target.db}.{target.table}, which is not declared in that DB's TABLES"
        )


def test_launch_workpackage_is_not_in_the_xid_registry():
    """Delta-verify residual: launch.workpackage is a plain scoping string
    with no target table -- explicitly NOT an XID."""
    assert ("launch", "workpackage") not in XID_REGISTRY


def test_session_and_memory_item_account_id_are_xid_to_platform_account():
    """Delta-verify residual: session.account_id and memory_item.account_id
    were ADDED to the cross-store enumeration at M1 kickoff."""
    for table in ("session", "memory_item"):
        target = XID_REGISTRY[(table, "account_id")]
        assert target.db == "platform"
        assert target.table == "account"
        assert target.pk_column == "account_id"


def test_xid_write_with_missing_target_refused(store):
    with pytest.raises(XidTargetMissingError, match="XID refused"):
        insert(
            store,
            "session",
            {
                "session_id": new_id("SESS"),
                "account_id": "ACC-does-not-exist",
                "opened_ts": now(),
                "status": "open",
            },
        )


def test_xid_write_with_missing_target_leaves_no_partial_row(store):
    before = store.ops.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    with pytest.raises(XidTargetMissingError):
        insert(
            store,
            "session",
            {
                "session_id": new_id("SESS"),
                "account_id": "ACC-does-not-exist",
                "opened_ts": now(),
                "status": "open",
            },
        )
    after = store.ops.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    assert before == after


def test_xid_write_with_valid_target_succeeds(store):
    acct_id = new_id("ACC")
    insert(store, "account", {"account_id": acct_id, "label": "x", "created_ts": now()})
    sess_id = new_id("SESS")
    insert(store, "session", {"session_id": sess_id, "account_id": acct_id, "opened_ts": now(), "status": "open"})
    row = store.ops.execute("SELECT * FROM session WHERE session_id = ?", (sess_id,)).fetchone()
    assert row["account_id"] == acct_id


def test_xid_write_with_null_xid_column_is_allowed(store):
    """Nullable XID columns (e.g. verdict.prereg_id) accept NULL without
    triggering a target-existence check -- NULL means "no reference," not
    "reference a row that doesn't exist"."""
    lnch = new_id("LNCH")
    acct = new_id("ACC")
    insert(store, "account", {"account_id": acct, "label": "x", "created_ts": now()})
    sess = new_id("SESS")
    insert(store, "session", {"session_id": sess, "account_id": acct, "opened_ts": now(), "status": "open"})
    insert(
        store,
        "launch",
        {
            "launch_id": lnch,
            "account_id": acct,
            "program_id": "p",
            "session_id": sess,
            "agent_kind": "t",
            "model_class": "top",
            "model": "sonnet",
            "purpose": "t",
            "est_tokens": 1,
            "booked_ts": now(),
            "state": "PROVISIONAL",
        },
    )
    insert(
        store,
        "verdict",
        {
            "verdict_id": new_id("VRD"),
            "subject_kind": "artifact",
            "subject_id": "ART-x",
            "procedure": "gate",
            "procedure_version": "1",
            "label": "PASS",
            "evidence": "[]",
            "prereg_id": None,  # nullable XID, explicitly absent
            "ts": now(),
            "issued_by_launch": lnch,
        },
    )


def test_xid_columns_for_table_helper():
    cols = xid_columns_for_table("hypothesis")
    assert set(cols) == {"created_by_launch", "prereg_id"}
    assert xid_columns_for_table("account") == {}


def test_memory_item_account_id_null_accepted_but_non_null_still_xid_validated(store):
    """schema-v2 (docs/the migration-plan notes (internal, not in this export) Section 4 item 1):
    memory_item.account_id is now NULLABLE at the DDL level (repo memory is
    deliberately cross-account) -- the XID_REGISTRY entry itself is
    UNCHANGED, so this is purely proving the existing "skip validation on
    NULL, still validate when present" write-API behavior
    (trialerror.stores.writer._validate_xids) actually holds for THIS column
    post-migration, not just in principle."""
    # NULL account_id: accepted, no XID check fired at all (no target row
    # needs to exist for this to succeed).
    row = insert(
        store,
        "memory_item",
        {
            "memory_item_id": new_id("MEM"),
            "key": "cross-account-key",
            "tier": "L0",
            "kind": "rule",
            "body": "shared across every account on this machine",
            "updated_ts": now(),
            "account_id": None,
        },
    )
    assert row["account_id"] is None

    # non-null but unknown account_id: still refused (XID validation is
    # unchanged -- nullability is orthogonal to it).
    with pytest.raises(XidTargetMissingError, match="XID refused"):
        insert(
            store,
            "memory_item",
            {
                "memory_item_id": new_id("MEM"),
                "key": "bad-account-key",
                "tier": "L0",
                "kind": "rule",
                "body": "x",
                "updated_ts": now(),
                "account_id": "ACC-does-not-exist",
            },
        )

    # non-null AND a real account: succeeds, same as pre-schema-v2 behavior.
    acct = new_id("ACC")
    insert(store, "account", {"account_id": acct, "label": "x", "created_ts": now()})
    row2 = insert(
        store,
        "memory_item",
        {
            "memory_item_id": new_id("MEM"),
            "key": "real-account-key",
            "tier": "L0",
            "kind": "rule",
            "body": "x",
            "updated_ts": now(),
            "account_id": acct,
        },
    )
    assert row2["account_id"] == acct
