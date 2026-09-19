"""M11 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m4_acceptance.py`` convention: this file IS the
acceptance-criteria mapping, each test here re-running (not replacing) a
narrower assertion that already lives in its dedicated module.

    | Acceptance criterion (design Section 12, M11 row)              | Test |
    |------------------------------------------------------------------|------|
    | divergent two-account edit fixture -> conflict group surfaced    | test_divergent_two_account_edit_surfaces_conflict_group_not_silently_resolved (see test_memory_merge.py) |
    | export->import round-trip idempotent                             | test_export_import_round_trip_is_idempotent (see test_memory_merge.py, test_memory_render.py) |
    | boot bundle L0 <= configured token budget                        | test_boot_bundle_l0_never_exceeds_configured_token_budget (see test_memory_api.py) |
"""

from __future__ import annotations

import pytest

from trialerror.memory.api import boot_bundle, put_item
from trialerror.memory.render import export_memory, import_memory
from trialerror.stores.store import open_store
from tests._memory_fixtures import make_account

pytestmark = pytest.mark.acceptance


@pytest.fixture()
def two_stores(tmp_path, platform_root):
    root_a = tmp_path / "acc_a"
    root_a.mkdir()
    root_b = tmp_path / "acc_b"
    root_b.mkdir()
    store_a = open_store(root_a, platform_root=platform_root)
    store_b = open_store(root_b, platform_root=platform_root)
    yield store_a, store_b
    store_a.close()
    store_b.close()


def test_divergent_two_account_edit_surfaces_conflict_group_not_silently_resolved(two_stores, tmp_path):
    """THE adversarial bar: two accounts edit the SAME memory key
    differently between syncs. The merge must keep BOTH versions under
    one conflict group id, never pick a winner and never drop a side."""
    store_a, store_b = two_stores
    account_a = make_account(store_a, label="account A")
    account_b = make_account(store_b, label="account B")

    put_item(store_a, key="collision-key", tier="L0", kind="rule", body="account A's edit", account_id=account_a)
    put_item(store_b, key="collision-key", tier="L0", kind="rule", body="account B's edit", account_id=account_b)

    export_dir = tmp_path / "export"
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)

    # surfaced, not resolved: exactly one conflict group, zero silent
    # imports/dedups for this key.
    assert len(result.conflicts) == 1
    assert result.conflicts[0]["key"] == "collision-key"

    rows = store_b.ops.execute(
        "SELECT body, status FROM memory_item WHERE key = ?", ("collision-key",)
    ).fetchall()
    bodies = {r["body"] for r in rows}
    # BOTH sides present verbatim -- nothing dropped.
    assert bodies == {"account A's edit", "account B's edit"}
    # not silently resolved: neither version is the sole 'active' answer.
    assert "active" not in {r["status"] for r in rows}
    statuses = sorted(r["status"] for r in rows)
    assert statuses.count("needs_merge") == 2  # both flagged, awaiting an explicit resolve


def test_export_import_round_trip_is_idempotent(store, tmp_path):
    account_id = make_account(store)
    put_item(store, key="round-trip-a", tier="L0", kind="rule", body="alpha", account_id=account_id, l0_abstract="a")
    put_item(store, key="round-trip-b", tier="L1", kind="fact", body="beta", account_id=account_id)

    before = sorted(
        (dict(r) for r in store.ops.execute("SELECT * FROM memory_item").fetchall()),
        key=lambda d: d["memory_item_id"],
    )

    out_dir = tmp_path / "memory"
    export_memory(store, out_dir=out_dir)
    result = import_memory(store, in_dir=out_dir)

    assert result.imported == []
    assert result.conflicts == []
    assert sorted(result.dedup_keys) == ["round-trip-a", "round-trip-b"]

    after = sorted(
        (dict(r) for r in store.ops.execute("SELECT * FROM memory_item").fetchall()),
        key=lambda d: d["memory_item_id"],
    )
    assert before == after  # byte-identical store state after the round trip

    # running the round trip a SECOND time changes nothing further either
    # -- true idempotency, not "happens to be stable once".
    export_memory(store, out_dir=out_dir)
    result2 = import_memory(store, in_dir=out_dir)
    assert result2.imported == []
    assert result2.conflicts == []


def test_boot_bundle_l0_never_exceeds_configured_token_budget(store):
    account_id = make_account(store)
    long_abstract = "word " * 100  # deliberately oversized vs a small budget
    for i in range(20):
        put_item(
            store, key=f"lots-{i}", tier="L0", kind="rule", body="irrelevant body",
            account_id=account_id, l0_abstract=long_abstract,
        )

    budget = 250
    bundle = boot_bundle(store, account_id=account_id, token_budget=budget)
    assert bundle["total_estimated_tokens"] <= budget
    assert bundle["token_budget"] == budget
    # the guarantee holds even when the L0 tier itself is far bigger than
    # budget (truncation must be visible, never silently exact-fit by luck)
    assert bundle["truncated"] is True
