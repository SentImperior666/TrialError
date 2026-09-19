"""``trialerror.memory.checks`` — the M11 doctor checks."""

from __future__ import annotations

from trialerror.memory.api import put_item
from trialerror.memory.checks import check_memory_l0_index_budget, check_memory_unresolved_conflict_groups
from trialerror.memory.merge import two_way_merge
from trialerror.util.doctor import DoctorContext
from tests._memory_fixtures import make_account


def _ctx(program_root):
    return DoctorContext(program_root=program_root)


def test_unresolved_conflict_groups_skips_when_ops_db_missing(tmp_path):
    result = check_memory_unresolved_conflict_groups(_ctx(tmp_path / "does-not-exist"))
    assert result.status == "skip"


def test_unresolved_conflict_groups_pass_when_none_open(store, program_root):
    account_id = make_account(store)
    put_item(store, key="fine", tier="L0", kind="rule", body="ok", account_id=account_id)
    result = check_memory_unresolved_conflict_groups(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["count"] == 0


def test_unresolved_conflict_groups_warns_and_counts_groups(store, program_root):
    account_id = make_account(store)
    put_item(store, key="topic", tier="L0", kind="rule", body="local", account_id=account_id)
    foreign = [{"key": "topic", "tier": "L0", "kind": "rule", "body": "different", "account_id": account_id}]
    result = two_way_merge(store, foreign_items=foreign)
    assert len(result.conflicts) == 1

    check_result = check_memory_unresolved_conflict_groups(_ctx(program_root))
    assert check_result.status == "warn"
    assert check_result.details["count"] == 1
    assert result.conflicts[0]["group_id"] in check_result.details["groups"]


def test_l0_budget_skips_when_ops_db_missing(tmp_path):
    result = check_memory_l0_index_budget(_ctx(tmp_path / "does-not-exist"))
    assert result.status == "skip"


def test_l0_budget_pass_under_default_budget(store, program_root):
    account_id = make_account(store)
    put_item(store, key="tiny", tier="L0", kind="rule", body="x", account_id=account_id, l0_abstract="short")
    result = check_memory_l0_index_budget(_ctx(program_root))
    assert result.status == "pass"


def test_l0_budget_warns_when_l0_tier_exceeds_configured_budget(store, program_root):
    account_id = make_account(store)
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "test"\n\n[memory]\ntoken_budget = 10\n', encoding="utf-8"
    )
    long_abstract = "word " * 40
    put_item(store, key="big", tier="L0", kind="rule", body="x", account_id=account_id, l0_abstract=long_abstract)
    result = check_memory_l0_index_budget(_ctx(program_root))
    assert result.status == "warn"
    assert result.details["token_budget"] == 10
