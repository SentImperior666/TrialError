"""Acceptance criteria: "write w/ bad field refused" and general write-API
behavior (unknown table, CHECK/NOT NULL violations, ``update``).
"""

from __future__ import annotations

import pytest

from trialerror.stores import get, insert, update
from trialerror.stores.errors import UnknownTableError, ValidationError
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def test_insert_refuses_unknown_column(store):
    with pytest.raises(ValidationError, match="unknown column"):
        insert(store, "account", {"account_id": new_id("ACC"), "label": "x", "created_ts": now(), "typo_field": 1})


def test_insert_refuses_unknown_table(store):
    with pytest.raises(UnknownTableError):
        insert(store, "not_a_real_table", {"x": 1})


def test_insert_refuses_missing_not_null_field(store):
    with pytest.raises(ValidationError, match="integrity violation"):
        insert(store, "account", {"account_id": new_id("ACC")})  # missing label, created_ts


def test_insert_refuses_bad_enum_value(store):
    acct_id = new_id("ACC")
    insert(store, "account", {"account_id": acct_id, "label": "x", "created_ts": now()})
    with pytest.raises(ValidationError, match="CHECK constraint"):
        insert(
            store,
            "session",
            {
                "session_id": new_id("SESS"),
                "account_id": acct_id,
                "opened_ts": now(),
                "status": "not_a_real_status",
            },
        )


def test_insert_refuses_duplicate_primary_key(store):
    acct_id = new_id("ACC")
    insert(store, "account", {"account_id": acct_id, "label": "first", "created_ts": now()})
    with pytest.raises(ValidationError, match="integrity violation"):
        insert(store, "account", {"account_id": acct_id, "label": "second", "created_ts": now()})


def test_insert_a_row_leaves_no_partial_state_on_failure(store):
    """A refused insert must not have written anything."""
    before = store.platform.execute("SELECT COUNT(*) FROM account").fetchone()[0]
    with pytest.raises(ValidationError):
        insert(store, "account", {"account_id": new_id("ACC"), "label": "x", "created_ts": now(), "bogus": 1})
    after = store.platform.execute("SELECT COUNT(*) FROM account").fetchone()[0]
    assert before == after


def test_get_returns_none_for_missing_row(store):
    assert get(store, "account", pk_column="account_id", pk_value="ACC-does-not-exist") is None


def test_update_changes_only_named_columns(store):
    acct_id = new_id("ACC")
    insert(store, "account", {"account_id": acct_id, "label": "original", "created_ts": now()})
    update(store, "account", pk_column="account_id", pk_value=acct_id, changes={"label": "renamed"})
    row = get(store, "account", pk_column="account_id", pk_value=acct_id)
    assert row["label"] == "renamed"


def test_update_refuses_unknown_column(store):
    acct_id = new_id("ACC")
    insert(store, "account", {"account_id": acct_id, "label": "x", "created_ts": now()})
    with pytest.raises(ValidationError, match="unknown column"):
        update(store, "account", pk_column="account_id", pk_value=acct_id, changes={"bogus": 1})


def test_table_columns_matches_pragma_table_info(store):
    from trialerror.stores.writer import table_columns

    cols = table_columns(store.platform, "account")
    assert cols == {"account_id", "label", "created_ts"}
