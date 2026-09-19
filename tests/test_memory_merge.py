"""``trialerror.memory.merge`` — two-way merge, conflict surfacing, resolution.

THE adversarial acceptance criterion this file exists to prove (design
Section 12, M11 row): "a divergent two-store merge must SURFACE
conflicts, not drop a side." Simulated the way the real harness actually
hits this (design Section 9.7): two SEPARATE local stores (one per
account), reconciled only through the markdown export/import boundary —
never by sharing one ops.db file directly.
"""

from __future__ import annotations

import pytest

from trialerror.memory.api import put_item, search_items
from trialerror.memory.merge import list_conflicts, resolve_conflict, two_way_merge
from trialerror.memory.render import export_memory, import_memory
from trialerror.stores.store import open_store
from tests._memory_fixtures import make_account


@pytest.fixture()
def two_stores(tmp_path, platform_root):
    """Two independent local stores sharing one platform.db (accounts
    live in platform.db, per design Section 4.3 — the single-machine,
    multi-account setup the merge engine is built for), each with its own
    ops.db — exactly the "two accounts, reconciled only via git-synced
    markdown" topology, never a shared ops.db connection."""
    root_a = tmp_path / "program_a"
    root_a.mkdir()
    root_b = tmp_path / "program_b"
    root_b.mkdir()
    store_a = open_store(root_a, platform_root=platform_root)
    store_b = open_store(root_b, platform_root=platform_root)
    yield store_a, store_b
    store_a.close()
    store_b.close()


def test_divergent_edit_surfaces_conflict_group_no_side_dropped(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a, label="account A")
    account_b = make_account(store_b, label="account B")

    put_item(store_a, key="shared-topic", tier="L0", kind="rule", body="A's version of the rule", account_id=account_a)
    put_item(store_b, key="shared-topic", tier="L0", kind="rule", body="B's DIFFERENT version of the rule", account_id=account_b)

    export_dir = tmp_path / "export_a"
    export_memory(store_a, out_dir=export_dir)

    result = import_memory(store_b, in_dir=export_dir)

    # exactly one conflict group, nothing silently imported or deduped
    assert len(result.conflicts) == 1
    assert result.imported == []
    assert result.dedup_keys == []
    group = result.conflicts[0]
    assert group["key"] == "shared-topic"

    # BOTH bodies survive, verbatim, in store_b -- the adversarial bar.
    rows = store_b.ops.execute(
        "SELECT body, status FROM memory_item WHERE key = ? ORDER BY body", ("shared-topic",)
    ).fetchall()
    all_bodies = {r["body"] for r in rows}
    assert all_bodies == {"A's version of the rule", "B's DIFFERENT version of the rule"}

    # the original local row is demoted, never deleted; the two conflict
    # rows are what's flagged needs_merge.
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["needs_merge", "needs_merge", "superseded"]

    # neither conflict version shows up as the "active" answer while open
    active_rows = search_items(store_b, account_id=None, status="active")
    assert "shared-topic" not in [r["key"] for r in active_rows]


def test_identical_content_dedups_without_writing(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a)
    account_b = make_account(store_b)

    put_item(store_a, key="same-everywhere", tier="L1", kind="fact", body="identical text", account_id=account_a)
    put_item(store_b, key="same-everywhere", tier="L1", kind="fact", body="identical text", account_id=account_b)

    export_dir = tmp_path / "export_a"
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)

    assert result.conflicts == []
    assert result.imported == []
    assert result.dedup_keys == ["same-everywhere"]
    count = store_b.ops.execute("SELECT COUNT(*) FROM memory_item WHERE key = ?", ("same-everywhere",)).fetchone()[0]
    assert count == 1  # still just B's original row -- no duplicate landed


def test_foreign_only_key_is_imported_as_new_active_row(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a)
    make_account(store_b)

    put_item(store_a, key="only-on-a", tier="L2", kind="lesson", body="brand new to B", account_id=account_a)

    export_dir = tmp_path / "export_a"
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)

    assert result.dedup_keys == []
    assert result.conflicts == []
    assert len(result.imported) == 1
    imported_row = store_b.ops.execute(
        "SELECT * FROM memory_item WHERE memory_item_id = ?", (result.imported[0],)
    ).fetchone()
    assert imported_row["key"] == "only-on-a"
    assert imported_row["status"] == "active"
    assert imported_row["body"] == "brand new to B"


def test_local_only_key_is_left_untouched(two_stores, tmp_path):
    store_a, store_b = two_stores
    make_account(store_a)
    account_b = make_account(store_b)
    put_item(store_b, key="only-on-b", tier="L0", kind="rule", body="B's own", account_id=account_b)

    export_dir = tmp_path / "export_a"  # A exports nothing relevant
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)

    assert result.conflicts == []
    assert "only-on-b" in result.left_only_keys
    row = store_b.ops.execute("SELECT status, body FROM memory_item WHERE key = ?", ("only-on-b",)).fetchone()
    assert row["status"] == "active"
    assert row["body"] == "B's own"


def test_reimporting_own_export_is_idempotent(store, tmp_path):
    account_id = make_account(store)
    put_item(store, key="k1", tier="L0", kind="rule", body="one", account_id=account_id)
    put_item(store, key="k2", tier="L1", kind="fact", body="two", account_id=account_id)

    before = sorted((dict(r) for r in store.ops.execute("SELECT * FROM memory_item").fetchall()), key=lambda d: d["memory_item_id"])
    export_dir = tmp_path / "self_export"
    export_memory(store, out_dir=export_dir)
    result = import_memory(store, in_dir=export_dir)

    assert result.imported == []
    assert result.conflicts == []
    assert sorted(result.dedup_keys) == ["k1", "k2"]
    after = sorted((dict(r) for r in store.ops.execute("SELECT * FROM memory_item").fetchall()), key=lambda d: d["memory_item_id"])
    assert before == after


def test_resolve_conflict_keep_left(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a)
    account_b = make_account(store_b)
    put_item(store_a, key="topic", tier="L0", kind="rule", body="A body", account_id=account_a)
    put_item(store_b, key="topic", tier="L0", kind="rule", body="B body", account_id=account_b)
    export_dir = tmp_path / "e"
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)
    group_id = result.conflicts[0]["group_id"]

    resolved = resolve_conflict(store_b, group_id=group_id, keep="left")
    left = store_b.ops.execute("SELECT status FROM memory_item WHERE memory_item_id = ?", (resolved["left_id"],)).fetchone()
    right = store_b.ops.execute("SELECT status FROM memory_item WHERE memory_item_id = ?", (resolved["right_id"],)).fetchone()
    assert left["status"] == "active"
    assert right["status"] == "superseded"

    # left's content is A's (the local side at merge time -- store_b's
    # own pre-import row) -- verify it's the one now active.
    active = search_items(store_b, status="active")
    assert [r["key"] for r in active if r["key"] == "topic"]


def test_resolve_conflict_keep_both_makes_both_active(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a)
    account_b = make_account(store_b)
    put_item(store_a, key="topic", tier="L0", kind="preference", body="A pref", account_id=account_a)
    put_item(store_b, key="topic", tier="L0", kind="preference", body="B pref", account_id=account_b)
    export_dir = tmp_path / "e"
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)
    group_id = result.conflicts[0]["group_id"]

    resolve_conflict(store_b, group_id=group_id, keep="both")
    rows = store_b.ops.execute("SELECT status FROM memory_item WHERE key = ?", ("topic",)).fetchall()
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["active", "active", "superseded"]  # the two conflict rows now active; original stays superseded


def test_resolve_conflict_unknown_group_refused(store):
    with pytest.raises(ValueError):
        resolve_conflict(store, group_id="does-not-exist", keep="left")


def test_resolve_conflict_is_one_shot_not_reappliable(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a)
    account_b = make_account(store_b)
    put_item(store_a, key="topic", tier="L0", kind="rule", body="A", account_id=account_a)
    put_item(store_b, key="topic", tier="L0", kind="rule", body="B", account_id=account_b)
    export_dir = tmp_path / "e"
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)
    group_id = result.conflicts[0]["group_id"]

    resolve_conflict(store_b, group_id=group_id, keep="left")
    with pytest.raises(ValueError):
        resolve_conflict(store_b, group_id=group_id, keep="right")


def test_resolve_conflict_rejects_bad_keep_value(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a)
    account_b = make_account(store_b)
    put_item(store_a, key="topic", tier="L0", kind="rule", body="A", account_id=account_a)
    put_item(store_b, key="topic", tier="L0", kind="rule", body="B", account_id=account_b)
    export_dir = tmp_path / "e"
    export_memory(store_a, out_dir=export_dir)
    result = import_memory(store_b, in_dir=export_dir)
    group_id = result.conflicts[0]["group_id"]
    with pytest.raises(ValueError):
        resolve_conflict(store_b, group_id=group_id, keep="sideways")


def test_list_conflicts_groups_both_versions_under_one_group(two_stores, tmp_path):
    store_a, store_b = two_stores
    account_a = make_account(store_a)
    account_b = make_account(store_b)
    put_item(store_a, key="topic", tier="L0", kind="rule", body="A", account_id=account_a)
    put_item(store_b, key="topic", tier="L0", kind="rule", body="B", account_id=account_b)
    export_dir = tmp_path / "e"
    export_memory(store_a, out_dir=export_dir)
    import_memory(store_b, in_dir=export_dir)

    groups = list_conflicts(store_b)
    assert len(groups) == 1
    assert groups[0]["key"] == "topic"
    sides = sorted(v["side"] for v in groups[0]["versions"])
    assert sides == ["left", "right"]


def test_two_way_merge_direct_call_without_render(store):
    """The merge engine also works with a plain list of foreign-item
    dicts (not necessarily parsed from files) -- render.py is one
    producer of that shape, not the only legal one."""
    account_id = make_account(store)
    put_item(store, key="direct", tier="L0", kind="rule", body="local body", account_id=account_id)

    foreign = [{"key": "direct", "tier": "L0", "kind": "rule", "body": "foreign body", "account_id": account_id}]
    result = two_way_merge(store, foreign_items=foreign)
    assert len(result.conflicts) == 1
