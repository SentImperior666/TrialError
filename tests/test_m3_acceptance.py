"""M3 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m0_acceptance.py``/``test_m1_acceptance.py``
convention: this file IS the acceptance-criteria mapping, each test here
re-running (not replacing) a narrower assertion that already lives in its
dedicated module.

    | Acceptance criterion (design Section 12, M3 row)                    | Test |
    |-----------------------------------------------------------------------|------|
    | unbooked Task refused                                                  | test_unbooked_task_refused (see test_budget_gate.py::test_unbooked_spawn_is_refused_end_to_end, test_no_token_in_prompt_refused) |
    | booked Task passes AND consumes the booking                           | test_booked_task_passes_and_consumes_booking (see test_budget_gate.py::test_booked_task_passes_and_consumes_the_booking) |
    | same launch_id token on a second spawn refused (adversarial)          | test_token_reuse_on_second_spawn_refused (see test_budget_gate.py::test_same_launch_id_token_on_a_second_spawn_is_refused, test_spawn_gate_hook.py::test_hook_refuses_the_same_token_reused_on_a_second_spawn) |
    | booking w/o an open session refused                                   | test_booking_without_open_session_refused (see test_budget_pools.py::test_book_launch_requires_open_session, F13) |
    | over-cap book refused                                                 | test_over_cap_book_refused (see test_budget_pools.py::test_book_launch_over_cap_refused) |
    | tree rollup sums correctly                                            | test_tree_rollup_sums_correctly (see test_budget_pools.py::test_tree_rollup_sums_correctly) |
    | calibrate reproduces multiplier from fixture snapshots                | test_calibrate_reproduces_multiplier (see test_budget_pools.py::test_calibrate_reproduces_multiplier_from_fixture_snapshots) |

Design "What" column items with their own tests here too (not bulleted
acceptance criteria, but stated build scope: "model-policy check", "DEFER"):

    | Scope item                                                            | Test |
    |-----------------------------------------------------------------------|------|
    | model-policy check (book AND spawn-time re-check)                    | test_model_policy_enforced_at_book_and_spawn |
    | DEFER (pool can't afford top-tier for a top-tier-required purpose)   | test_defer_when_pool_cannot_afford_top_tier |
    | account bound at session boot, read by book_launch (F14)             | test_account_bound_at_session_boot_not_caller_supplied |
"""

from __future__ import annotations

import pytest

from trialerror.budget.errors import ModelPolicyViolationError, NoOpenSessionError
from trialerror.budget.gate import evaluate_spawn
from trialerror.budget.pools import book_launch, calibrate, create_pool, reconcile_launch, snapshot_ingest, tree_rollup
from trialerror.stores import get
from trialerror.util.ids import new_id

from tests._budget_fixtures import open_account_session

pytestmark = pytest.mark.acceptance


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


def test_unbooked_task_refused(store):
    account_id, session_id = open_account_session(store)
    result = evaluate_spawn(store, "launch_id: " + new_id("LNCH"), session_id=session_id)
    assert result.allowed is False
    assert result.code == "unknown_launch_id"

    result_no_token = evaluate_spawn(store, "no id present at all", session_id=session_id)
    assert result_no_token.allowed is False
    assert result_no_token.code == "no_launch_id_token"


def test_booked_task_passes_and_consumes_booking(store):
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id)
    result = evaluate_spawn(store, f"launch_id: {booked.launch_id}", session_id=session_id)
    assert result.allowed is True
    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["state"] == "RUNNING"


def test_token_reuse_on_second_spawn_refused(store):
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id)
    prompt = f"launch_id: {booked.launch_id}"

    first = evaluate_spawn(store, prompt, session_id=session_id)
    assert first.allowed is True

    second = evaluate_spawn(store, prompt, session_id=session_id)
    assert second.allowed is False
    assert second.code == "token_not_provisional"


def test_booking_without_open_session_refused(store):
    account_id, session_id = open_account_session(store, status="closed")
    with pytest.raises(NoOpenSessionError):
        _book(store, session_id)


def test_over_cap_book_refused(store):
    account_id, session_id = open_account_session(store)
    create_pool(
        store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=1000, billed_multiplier=1.0
    )
    result = _book(store, session_id, model_class="mid", est_tokens=1100)
    assert result.ok is False
    assert result.state == "REFUSED"


def test_tree_rollup_sums_correctly(store):
    account_id, session_id = open_account_session(store)
    root = _book(store, session_id, est_tokens=100)
    reconcile_launch(store, launch_id=root.launch_id, actual_tokens=90)
    child = _book(store, session_id, est_tokens=50, parent_launch=root.launch_id)
    reconcile_launch(store, launch_id=child.launch_id, actual_tokens=45)

    out = tree_rollup(store, root.launch_id)
    assert out["est_tokens_total"] == 150
    assert out["actual_tokens_total"] == 135
    assert out["member_count"] == 2


def test_calibrate_reproduces_multiplier(store):
    account_id, session_id = open_account_session(store)
    create_pool(
        store, account_id=account_id, model_class="top", period="weekly", cap_tokens=1_000_000, billed_multiplier=1.0
    )
    t1, t_mid, t2 = "2026-03-01T00:00:00.000Z", "2026-03-02T00:00:00.000Z", "2026-03-03T00:00:00.000Z"
    snapshot_ingest(store, account_id=account_id, source="screenshot", payload={"model_class": "top", "used_tokens": 2000}, ts=t1)
    booked = _book(store, session_id, model_class="top", est_tokens=400)
    reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=400, now_ts=t_mid)
    # delta_real=800, delta_visible=400 -> multiplier=2.0 exactly.
    snapshot_ingest(store, account_id=account_id, source="screenshot", payload={"model_class": "top", "used_tokens": 2800}, ts=t2)

    calib = calibrate(store, account_id=account_id, model_class="top")
    assert calib["multiplier"] == pytest.approx(2.0)


def test_model_policy_enforced_at_book_and_spawn(store):
    account_id, session_id = open_account_session(store)
    policy = {"ideation": "top"}

    with pytest.raises(ModelPolicyViolationError):
        _book(store, session_id, model_class="small", purpose="ideation", policy=policy)

    # Booked when no policy was in effect; the gate still catches it at
    # spawn time (defense in depth -- "the same check verifies model_class
    # against model policy for the stated purpose", design Section 5.4).
    slipped = _book(store, session_id, model_class="small", purpose="ideation")
    result = evaluate_spawn(store, f"launch_id: {slipped.launch_id}", session_id=session_id, policy=policy)
    assert result.allowed is False
    assert result.code == "model_policy_violation"


def test_defer_when_pool_cannot_afford_top_tier(store):
    account_id, session_id = open_account_session(store)
    create_pool(store, account_id=account_id, model_class="top", period="weekly", cap_tokens=100, billed_multiplier=1.0)
    result = _book(
        store, session_id, model_class="top", purpose="ideation", est_tokens=1000, policy={"ideation": "top"}
    )
    assert result.ok is False
    assert result.state == "DEFERRED"


def test_account_bound_at_session_boot_not_caller_supplied(store):
    account_id, session_id = open_account_session(store)
    result = _book(store, session_id)
    assert result.account_id == account_id
    # book_launch's signature has no account_id parameter at all (F14) --
    # the only way to influence attribution is via session_id.
    import inspect

    assert "account_id" not in inspect.signature(book_launch).parameters
