"""``trialerror.memory.api`` — put/get/search/boot_bundle over ``memory_item``."""

from __future__ import annotations

import pytest

from trialerror.memory.api import (
    DEFAULT_TOKEN_BUDGET,
    boot_bundle,
    estimate_tokens,
    get_item,
    put_item,
    search_items,
)
from tests._memory_fixtures import make_account


def test_put_item_inserts_new_row(store):
    account_id = make_account(store)
    row = put_item(
        store, key="working-rules", tier="L0", kind="rule", body="always book before spawn",
        account_id=account_id, l0_abstract="budget-at-spawn is enforced",
    )
    assert row["memory_item_id"].startswith("MEM-")
    assert row["status"] == "active"
    fetched = get_item(store, row["memory_item_id"])
    assert fetched["body"] == "always book before spawn"
    assert fetched["l0_abstract"] == "budget-at-spawn is enforced"


def test_put_item_same_content_is_idempotent_noop(store):
    account_id = make_account(store)
    first = put_item(store, key="k1", tier="L0", kind="fact", body="same body", account_id=account_id)
    second = put_item(store, key="k1", tier="L0", kind="fact", body="same body", account_id=account_id)
    assert second["memory_item_id"] == first["memory_item_id"]
    assert second["updated_ts"] == first["updated_ts"]  # untouched -- true no-op, not a re-stamp
    rows = store.ops.execute("SELECT COUNT(*) FROM memory_item WHERE key = ?", ("k1",)).fetchone()[0]
    assert rows == 1


def test_put_item_changed_content_updates_in_place(store):
    account_id = make_account(store)
    first = put_item(store, key="k2", tier="L0", kind="fact", body="version 1", account_id=account_id)
    second = put_item(store, key="k2", tier="L1", kind="lesson", body="version 2", account_id=account_id)
    assert second["memory_item_id"] == first["memory_item_id"]  # same row, edited
    assert second["body"] == "version 2"
    assert second["tier"] == "L1"
    rows = store.ops.execute("SELECT COUNT(*) FROM memory_item WHERE key = ?", ("k2",)).fetchone()[0]
    assert rows == 1  # no duplicate row created


def test_put_item_rejects_bad_tier_and_kind(store):
    account_id = make_account(store)
    with pytest.raises(ValueError):
        put_item(store, key="bad", tier="L9", kind="rule", body="x", account_id=account_id)
    with pytest.raises(ValueError):
        put_item(store, key="bad", tier="L0", kind="not-a-kind", body="x", account_id=account_id)


def test_put_item_rejects_empty_key_or_body(store):
    account_id = make_account(store)
    with pytest.raises(ValueError):
        put_item(store, key="", tier="L0", kind="rule", body="x", account_id=account_id)
    with pytest.raises(ValueError):
        put_item(store, key="x", tier="L0", kind="rule", body="   ", account_id=account_id)


def test_search_items_never_returns_body(store):
    account_id = make_account(store)
    put_item(store, key="secret", tier="L1", kind="fact", body="THE FULL BODY TEXT", account_id=account_id)
    rows = search_items(store, account_id=account_id)
    assert len(rows) == 1
    assert "body" not in rows[0]
    assert rows[0]["key"] == "secret"


def test_search_items_query_matches_body_but_still_omits_it(store):
    account_id = make_account(store)
    put_item(store, key="findme", tier="L1", kind="fact", body="a needle in here", account_id=account_id)
    put_item(store, key="other", tier="L1", kind="fact", body="nothing relevant", account_id=account_id)
    rows = search_items(store, query="needle", account_id=account_id)
    assert [r["key"] for r in rows] == ["findme"]
    assert "body" not in rows[0]


def test_search_items_filters_by_tier_kind_status(store):
    account_id = make_account(store)
    put_item(store, key="a", tier="L0", kind="rule", body="a", account_id=account_id)
    put_item(store, key="b", tier="L1", kind="fact", body="b", account_id=account_id)
    l0_only = search_items(store, tier="L0", account_id=account_id)
    assert [r["key"] for r in l0_only] == ["a"]
    fact_only = search_items(store, kind="fact", account_id=account_id)
    assert [r["key"] for r in fact_only] == ["b"]


def test_get_item_full_body_by_id(store):
    account_id = make_account(store)
    row = put_item(store, key="full", tier="L2", kind="lesson", body="the whole thing", account_id=account_id)
    full = get_item(store, row["memory_item_id"])
    assert full["body"] == "the whole thing"
    assert get_item(store, "MEM-does-not-exist") is None


def test_estimate_tokens_monotone_and_zero_for_empty():
    assert estimate_tokens(None) == 0
    assert estimate_tokens("") == 0
    assert estimate_tokens("x") >= 1
    assert estimate_tokens("x" * 400) > estimate_tokens("x" * 40)


def test_boot_bundle_never_carries_full_body(store):
    account_id = make_account(store)
    put_item(store, key="l0-item", tier="L0", kind="rule", body="body text", account_id=account_id, l0_abstract="abstract")
    bundle = boot_bundle(store, account_id=account_id)
    assert bundle["items"]
    for item in bundle["items"]:
        assert "body" not in item


def test_boot_bundle_orders_l0_before_l1_before_l2(store):
    account_id = make_account(store)
    put_item(store, key="z-l2", tier="L2", kind="fact", body="x", account_id=account_id)
    put_item(store, key="a-l1", tier="L1", kind="fact", body="x", account_id=account_id)
    put_item(store, key="m-l0", tier="L0", kind="rule", body="x", account_id=account_id)
    bundle = boot_bundle(store, account_id=account_id, token_budget=DEFAULT_TOKEN_BUDGET)
    tiers_in_order = [item["tier"] for item in bundle["items"]]
    assert tiers_in_order == ["L0", "L1", "L2"]


def test_boot_bundle_respects_token_budget_and_flags_truncation(store):
    account_id = make_account(store)
    # Each item's l0_abstract is long enough to cost several estimated
    # tokens; a tiny budget must not be exceeded, and truncation must be
    # reported (not silently swallowed).
    long_abstract = "word " * 40  # ~200 chars -> ~50 estimated tokens
    for i in range(10):
        put_item(
            store, key=f"item-{i}", tier="L0", kind="rule", body="body",
            account_id=account_id, l0_abstract=long_abstract,
        )
    bundle = boot_bundle(store, account_id=account_id, token_budget=120)
    assert bundle["total_estimated_tokens"] <= 120
    assert bundle["truncated"] is True
    assert bundle["omitted_count"] > 0
    assert len(bundle["items"]) < 10


def test_boot_bundle_fits_everything_under_a_generous_budget(store):
    account_id = make_account(store)
    put_item(store, key="only-item", tier="L0", kind="rule", body="x", account_id=account_id, l0_abstract="short")
    bundle = boot_bundle(store, account_id=account_id, token_budget=DEFAULT_TOKEN_BUDGET)
    assert bundle["truncated"] is False
    assert bundle["omitted_count"] == 0
    assert len(bundle["items"]) == 1
