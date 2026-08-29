"""Tests for ``trialerror.budget.checks`` — the doctor checks this subsystem
registers (auto-discovered exactly like M0's/M1's own checks)."""

from __future__ import annotations

from trialerror.budget.pools import book_launch, create_pool
from trialerror.stores import insert
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._budget_fixtures import open_account_session


def _run(names=None):
    clear_registry()
    discover_and_register_checks()
    return {r.name: r for r in run_checks(DoctorContext(), only=names)}


def test_checks_auto_discovered():
    results = _run()
    assert "budget_dangling_launches" in results
    assert "budget_pool_overspend" in results


def test_dangling_launches_skips_when_no_platform_db(monkeypatch, tmp_path):
    empty_root = tmp_path / "nope"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(empty_root))
    results = _run(["budget_dangling_launches"])
    assert results["budget_dangling_launches"].status == "skip"


def test_dangling_launches_warns_on_ttl_expired_booking(store):
    account_id, session_id = open_account_session(store)
    book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=10,
        booking_ttl_s=1,
        now_ts="2020-01-01T00:00:00.000Z",
    )
    results = _run(["budget_dangling_launches"])
    result = results["budget_dangling_launches"]
    assert result.status == "warn"
    assert len(result.details["offenders"]) == 1


def test_dangling_launches_pass_when_all_fresh(store):
    account_id, session_id = open_account_session(store)
    book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=10,
        booking_ttl_s=3600,
    )
    results = _run(["budget_dangling_launches"])
    assert results["budget_dangling_launches"].status == "pass"


def test_pool_overspend_skips_when_no_platform_db(monkeypatch, tmp_path):
    empty_root = tmp_path / "nope"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(empty_root))
    results = _run(["budget_pool_overspend"])
    assert results["budget_pool_overspend"].status == "skip"


def test_pool_overspend_warns_when_projected_spend_exceeds_hard_cap(store):
    account_id, _ = open_account_session(store)
    pool = create_pool(
        store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=100, billed_multiplier=1.0
    )
    # Simulate already-reconciled spend directly (checks.py operates on
    # whatever state the DB is in, regardless of how it got there).
    from trialerror.stores import update

    update(
        store,
        "budget_pool",
        pk_column="pool_id",
        pk_value=pool["pool_id"],
        changes={"spent_visible_tokens": 500},
    )

    results = _run(["budget_pool_overspend"])
    result = results["budget_pool_overspend"]
    assert result.status == "warn"
    assert any(o["pool_id"] == pool["pool_id"] for o in result.details["offenders"])


def test_pool_overspend_pass_when_within_cap(store):
    account_id, _ = open_account_session(store)
    create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=100_000, billed_multiplier=1.0)
    results = _run(["budget_pool_overspend"])
    assert results["budget_pool_overspend"].status == "pass"
