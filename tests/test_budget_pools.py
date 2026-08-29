"""Tests for ``trialerror.budget.pools``: book/reconcile/status/pools/
snapshot-ingest/calibrate/tree-rollup."""

from __future__ import annotations

import pytest

from trialerror.budget.errors import BudgetError, ModelPolicyViolationError, NoOpenSessionError, UnknownOverrideRulingError
from trialerror.budget.pools import (
    book_launch,
    budget_status,
    calibrate,
    create_pool,
    list_pools,
    reconcile_launch,
    snapshot_ingest,
    tree_rollup,
)
from trialerror.stores import get, insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._budget_fixtures import add_ruling, open_account_session


def _book(store, session_id, **overrides):
    kwargs = dict(
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=100,
    )
    kwargs.update(overrides)
    return book_launch(store, **kwargs)


# ---- open-session requirement (F13) ---------------------------------------


def test_book_launch_requires_open_session(store):
    account_id, session_id = open_account_session(store, status="closed")
    with pytest.raises(NoOpenSessionError):
        _book(store, session_id)


def test_book_launch_refuses_unknown_session(store):
    with pytest.raises(NoOpenSessionError):
        _book(store, "SESS-does-not-exist")


# ---- happy path + account binding (F14) ------------------------------------


def test_book_launch_creates_provisional_token_bound_to_session_account(store):
    account_id, session_id = open_account_session(store)
    result = _book(store, session_id)

    assert result.ok is True
    assert result.state == "PROVISIONAL"
    assert result.account_id == account_id  # derived from the session, never a caller param

    row = get(store, "launch", pk_column="launch_id", pk_value=result.launch_id)
    assert row is not None
    assert row["state"] == "PROVISIONAL"
    assert row["account_id"] == account_id
    assert row["session_id"] == session_id


# ---- model policy (Section 5.4 / 1.11) -------------------------------------


def test_book_launch_model_policy_violation_without_override(store):
    account_id, session_id = open_account_session(store)
    with pytest.raises(ModelPolicyViolationError):
        _book(store, session_id, model_class="small", purpose="ideation", policy={"ideation": "top"})


def test_book_launch_model_policy_override_with_valid_ruling(store):
    account_id, session_id = open_account_session(store)
    ruling_id = add_ruling(store)
    result = _book(
        store,
        session_id,
        model_class="small",
        purpose="ideation",
        policy={"ideation": "top"},
        override_ruling_id=ruling_id,
    )
    assert result.ok is True
    row = get(store, "launch", pk_column="launch_id", pk_value=result.launch_id)
    assert "override_ruling_id" in (row["attrs"] or "")


def test_book_launch_override_ruling_id_must_exist(store):
    account_id, session_id = open_account_session(store)
    with pytest.raises(UnknownOverrideRulingError):
        _book(
            store,
            session_id,
            model_class="small",
            purpose="ideation",
            policy={"ideation": "top"},
            override_ruling_id="C-9999-nonexistent",
        )


def test_book_launch_purpose_not_in_policy_has_no_floor(store):
    account_id, session_id = open_account_session(store)
    result = _book(store, session_id, model_class="small", purpose="unlisted", policy={"ideation": "top"})
    assert result.ok is True


# ---- over-cap refusal (explicit M3 acceptance criterion) -------------------


def test_book_launch_over_cap_refused(store):
    account_id, session_id = open_account_session(store)
    create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=1000, billed_multiplier=1.0)

    result = _book(store, session_id, model_class="mid", est_tokens=1100)

    assert result.ok is False
    assert result.state == "REFUSED"
    row = get(store, "launch", pk_column="launch_id", pk_value=result.launch_id)
    assert row["state"] == "REFUSED"


def test_book_launch_over_cap_top_tier_purpose_defers_instead_of_refusing(store):
    account_id, session_id = open_account_session(store)
    create_pool(store, account_id=account_id, model_class="top", period="weekly", cap_tokens=1000, billed_multiplier=1.0)

    result = _book(
        store,
        session_id,
        model_class="top",
        purpose="ideation",
        est_tokens=1100,
        policy={"ideation": "top"},
    )

    assert result.ok is False
    assert result.state == "DEFERRED"


def test_book_launch_within_cap_not_refused(store):
    account_id, session_id = open_account_session(store)
    create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=1000, billed_multiplier=1.0)
    result = _book(store, session_id, model_class="mid", est_tokens=500)
    assert result.ok is True
    assert result.state == "PROVISIONAL"


def test_book_launch_with_no_pool_configured_is_unconstrained(store):
    account_id, session_id = open_account_session(store)
    result = _book(store, session_id, model_class="mid", est_tokens=10_000_000)
    assert result.ok is True
    assert result.details["pool_configured"] is False


# ---- reconcile ---------------------------------------------------------


def test_reconcile_launch_updates_state_and_pool_spend(store):
    account_id, session_id = open_account_session(store)
    create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=100_000, billed_multiplier=1.0)
    result = _book(store, session_id, model_class="mid", est_tokens=100)

    out = reconcile_launch(store, launch_id=result.launch_id, actual_tokens=77, reconcile_source="transcript")
    assert out["state"] == "RECONCILED"
    assert out["pool_updated"] is True

    row = get(store, "launch", pk_column="launch_id", pk_value=result.launch_id)
    assert row["state"] == "RECONCILED"
    assert row["actual_tokens"] == 77
    assert row["reconcile_source"] == "transcript"

    pool = get(store, "budget_pool", pk_column="pool_id", pk_value=list_pools(store, account_id=account_id)[0]["pool_id"])
    assert pool["spent_visible_tokens"] == 77


def test_reconcile_launch_refuses_double_reconcile(store):
    account_id, session_id = open_account_session(store)
    result = _book(store, session_id)
    reconcile_launch(store, launch_id=result.launch_id, actual_tokens=10)
    with pytest.raises(BudgetError):
        reconcile_launch(store, launch_id=result.launch_id, actual_tokens=10)


def test_reconcile_launch_refuses_unknown_launch(store):
    with pytest.raises(BudgetError):
        reconcile_launch(store, launch_id="LNCH-nonexistent", actual_tokens=10)


# ---- tree rollup (explicit M3 acceptance criterion) -------------------


def test_tree_rollup_sums_correctly(store):
    account_id, session_id = open_account_session(store)
    root = _book(store, session_id, est_tokens=100)
    reconcile_launch(store, launch_id=root.launch_id, actual_tokens=90)

    child1 = _book(store, session_id, est_tokens=50, parent_launch=root.launch_id)
    reconcile_launch(store, launch_id=child1.launch_id, actual_tokens=40)

    child2 = _book(store, session_id, est_tokens=30, parent_launch=root.launch_id)
    reconcile_launch(store, launch_id=child2.launch_id, actual_tokens=25)

    grandchild = _book(store, session_id, est_tokens=10, parent_launch=child1.launch_id)
    reconcile_launch(store, launch_id=grandchild.launch_id, actual_tokens=5)

    out = tree_rollup(store, root.launch_id)
    assert out["member_count"] == 4
    assert out["descendant_count"] == 3
    assert out["est_tokens_total"] == 100 + 50 + 30 + 10
    assert out["actual_tokens_total"] == 90 + 40 + 25 + 5
    assert out["states"] == {"RECONCILED": 4}


def test_tree_rollup_unknown_launch_raises(store):
    with pytest.raises(BudgetError):
        tree_rollup(store, "LNCH-nonexistent")


# ---- pools list/create --------------------------------------------------


def test_create_and_list_pools(store):
    account_id, _ = open_account_session(store)
    row = create_pool(store, account_id=account_id, model_class="small", period="monthly", cap_tokens=5000)
    pools = list_pools(store, account_id=account_id)
    assert any(p["pool_id"] == row["pool_id"] for p in pools)

    all_pools = list_pools(store)
    assert any(p["pool_id"] == row["pool_id"] for p in all_pools)


# ---- budget status / DEFER advisories -----------------------------------


def test_budget_status_headroom_and_defer_advisory(store):
    account_id, session_id = open_account_session(store)
    create_pool(
        store,
        account_id=account_id,
        model_class="top",
        period="weekly",
        cap_tokens=1000,
        billed_multiplier=1.0,
        soft_pct=50,
        hard_pct=100,
    )
    _book(store, session_id, model_class="top", est_tokens=600)

    status = budget_status(store, account_id=account_id, model_class="top")
    pool_entry = status["pools"][0]
    assert pool_entry["committed_visible_tokens"] == 600
    assert pool_entry["projected_billed_tokens"] == 600
    assert pool_entry["over_soft"] is True
    assert pool_entry["over_hard"] is False
    assert any(a["model_class"] == "top" for a in status["defer_advisories"])


def test_budget_status_no_pool_for_class_is_omitted(store):
    account_id, _ = open_account_session(store)
    status = budget_status(store, account_id=account_id, model_class="top")
    assert status["pools"] == []
    assert status["defer_advisories"] == []


# ---- snapshot ingest + calibrate (explicit M3 acceptance criterion) -------


def test_snapshot_ingest_records_screenshot_ground_truth(store):
    account_id, _ = open_account_session(store)
    row = snapshot_ingest(store, account_id=account_id, source="screenshot", payload={"model_class": "top", "used_tokens": 100})
    fetched = get(store, "quota_snapshot", pk_column="snap_id", pk_value=row["snap_id"])
    assert fetched["source"] == "screenshot"


def test_calibrate_reproduces_multiplier_from_fixture_snapshots(store):
    account_id, session_id = open_account_session(store)
    create_pool(
        store, account_id=account_id, model_class="top", period="weekly", cap_tokens=1_000_000, billed_multiplier=1.0
    )

    t1 = "2026-01-01T00:00:00.000Z"
    t_mid = "2026-01-02T00:00:00.000Z"
    t2 = "2026-01-03T00:00:00.000Z"

    snapshot_ingest(store, account_id=account_id, source="screenshot", payload={"model_class": "top", "used_tokens": 1000}, ts=t1)

    booked = _book(store, session_id, model_class="top", est_tokens=200)
    reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=200, now_ts=t_mid)

    # delta_real / delta_visible = 550 / 200 = 2.75 exactly.
    snapshot_ingest(store, account_id=account_id, source="screenshot", payload={"model_class": "top", "used_tokens": 1550}, ts=t2)

    calib = calibrate(store, account_id=account_id, model_class="top", window="7d")
    assert calib["multiplier"] == pytest.approx(2.75)

    pool = list_pools(store, account_id=account_id)[0]
    assert pool["billed_multiplier"] == pytest.approx(2.75)


def test_calibrate_needs_two_snapshots(store):
    account_id, _ = open_account_session(store)
    snapshot_ingest(store, account_id=account_id, source="screenshot", payload={"model_class": "top", "used_tokens": 100})
    with pytest.raises(BudgetError):
        calibrate(store, account_id=account_id, model_class="top")


def test_calibrate_negative_delta_real_raises(store):
    account_id, _ = open_account_session(store)
    snapshot_ingest(
        store, account_id=account_id, source="screenshot",
        payload={"model_class": "top", "used_tokens": 1000}, ts="2026-01-01T00:00:00.000Z",
    )
    snapshot_ingest(
        store, account_id=account_id, source="screenshot",
        payload={"model_class": "top", "used_tokens": 500}, ts="2026-01-02T00:00:00.000Z",
    )
    with pytest.raises(BudgetError):
        calibrate(store, account_id=account_id, model_class="top")


def test_calibrate_zero_delta_visible_raises(store):
    account_id, _ = open_account_session(store)
    snapshot_ingest(
        store, account_id=account_id, source="screenshot",
        payload={"model_class": "top", "used_tokens": 1000}, ts="2026-01-01T00:00:00.000Z",
    )
    snapshot_ingest(
        store, account_id=account_id, source="screenshot",
        payload={"model_class": "top", "used_tokens": 1200}, ts="2026-01-02T00:00:00.000Z",
    )
    with pytest.raises(BudgetError):
        calibrate(store, account_id=account_id, model_class="top")
