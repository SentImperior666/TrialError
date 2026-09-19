"""What the usage split looks like once it leaves the launch row: the
``invoke_agent`` span, ``budget rollup``, and the dashboard's budget card.

Lane FB-3 item 3 (D-FB-13 (c)). The split is only worth a migration if the
surfaces that report spend can show it, and only honest if every one of them
says so when it is absent -- a card that renders four zeros for a launch
nobody measured is worse than a card that renders nothing.
"""

from __future__ import annotations

import json

import pytest

from trialerror.budget.pools import (
    book_launch,
    reconcile_launch,
    reconcile_launch_from_event,
    tree_rollup,
    usage_split_totals,
)
from trialerror.events.api import append_event
from trialerror.obs import semconv, spans, tracer
from trialerror.stores import get

from tests._budget_fixtures import open_account_session
from tests.test_obs_spans import _SpyTracer


@pytest.fixture(autouse=True)
def spy(monkeypatch):
    """The same spy tracer ``tests/test_obs_spans.py`` uses, reused rather
    than re-declared: a second recording tracer in the suite is a second
    thing to keep in step with the real one."""
    tracer.reset_for_tests()
    s = _SpyTracer()
    monkeypatch.setattr(tracer, "get_tracer", lambda: s)
    yield s
    tracer.reset_for_tests()

LIVE_USAGE = {
    "total_tokens": 11461,
    "total_source": "totalTokens",
    "input_tokens": 2,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 11455,
    "output_tokens": 4,
}


def _book(store, session_id, **overrides):
    kwargs = dict(
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="researcher",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=100,
    )
    kwargs.update(overrides)
    return book_launch(store, **kwargs)


def _measured_launch(store, session_id, usage=None, **overrides):
    booked = _book(store, session_id, **overrides)
    append_event(
        store,
        event_type="subagent_return",
        session_id=session_id,
        launch_id=booked.launch_id,
        payload={"response_size_bytes": 1, "duration_ms": None, "usage": usage or LIVE_USAGE},
    )
    reconcile_launch_from_event(store, launch_id=booked.launch_id)
    return booked.launch_id


# ---------------------------------------------------------------------------
# usage_split_totals -- the one summation both surfaces read
# ---------------------------------------------------------------------------


def test_totals_are_none_when_nothing_was_measured():
    result = usage_split_totals([{"usage_input_tokens": None, "usage_output_tokens": None}])
    assert result["attested"] == 0
    assert result["of_rows"] == 1
    assert result["totals"] is None


def test_totals_count_only_the_rows_that_carry_a_split():
    rows = [
        {"usage_input_tokens": 2, "usage_cache_creation_tokens": 0,
         "usage_cache_read_tokens": 11455, "usage_output_tokens": 4},
        {"usage_input_tokens": None, "usage_cache_creation_tokens": None,
         "usage_cache_read_tokens": None, "usage_output_tokens": None},
    ]
    result = usage_split_totals(rows)
    assert result == {
        "attested": 1,
        "of_rows": 2,
        "totals": {
            "usage_input_tokens": 2,
            "usage_cache_creation_tokens": 0,
            "usage_cache_read_tokens": 11455,
            "usage_output_tokens": 4,
        },
    }


# ---------------------------------------------------------------------------
# the invoke_agent span
# ---------------------------------------------------------------------------


def test_span_input_tokens_sums_the_three_input_columns():
    row = {"usage_input_tokens": 2, "usage_cache_creation_tokens": 30,
           "usage_cache_read_tokens": 11455, "usage_output_tokens": 4}
    assert spans.span_input_tokens(row) == 11487


def test_span_input_tokens_is_none_for_an_unmeasured_row():
    row = {"usage_input_tokens": None, "usage_cache_creation_tokens": None,
           "usage_cache_read_tokens": None, "usage_output_tokens": None}
    assert spans.span_input_tokens(row) is None


def test_the_launch_span_emits_both_usage_attributes_when_the_split_is_present(spy):
    with spans.launch_span(
        launch_id="LNCH-x", agent_kind="researcher", model="sonnet",
        actual_tokens=11461, input_tokens=11457, output_tokens=4,
    ):
        pass
    span = spy.spans[-1]
    assert span.attributes[semconv.GEN_AI_USAGE_INPUT_TOKENS] == 11457
    assert span.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] == 4


def test_the_launch_span_keeps_its_old_shape_without_a_split(spy):
    """The pre-platform-v2 behaviour, unchanged: one real number on
    ``output_tokens``, no fabricated ``input_tokens``."""
    with spans.launch_span(launch_id="LNCH-x", agent_kind="researcher", model="sonnet", actual_tokens=123):
        pass
    span = spy.spans[-1]
    assert span.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] == 123
    assert semconv.GEN_AI_USAGE_INPUT_TOKENS not in span.attributes


def test_traced_reconcile_launch_reads_the_split_off_the_row(store, spy):
    """The launch was reconciled from its event BEFORE this call, so the
    wrapper is emitting a span over a row that already carries a split --
    the shape a `--from-event` reconciliation leaves behind."""
    _account, session_id = open_account_session(store)
    launch_id = _measured_launch(store, session_id)
    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)

    with spans.launch_span(
        launch_id=launch_id,
        agent_kind=row["agent_kind"],
        model=row["model"],
        actual_tokens=row["actual_tokens"],
        input_tokens=spans.span_input_tokens(row),
        output_tokens=row["usage_output_tokens"],
    ):
        pass
    span = spy.spans[-1]
    assert span.attributes[semconv.GEN_AI_USAGE_INPUT_TOKENS] == 11457
    assert span.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] == 4


def test_traced_reconcile_launch_still_emits_the_single_number_for_actual_tokens(store, spy):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    spans.traced_reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=77)
    span = spy.spans[-1]
    assert span.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] == 77
    assert semconv.GEN_AI_USAGE_INPUT_TOKENS not in span.attributes


# ---------------------------------------------------------------------------
# budget rollup
# ---------------------------------------------------------------------------


def test_the_rollup_carries_the_split_over_a_tree(store):
    _account, session_id = open_account_session(store)
    root = _measured_launch(store, session_id)
    child = _measured_launch(store, session_id, parent_launch=root)

    rollup = tree_rollup(store, root)
    assert rollup["member_count"] == 2
    assert rollup["actual_tokens_total"] == 11461 * 2
    assert rollup["usage_split"]["attested"] == 2
    assert rollup["usage_split"]["totals"]["usage_cache_read_tokens"] == 11455 * 2
    assert tree_rollup(store, child)["member_count"] == 1  # the child is a leaf of that tree


def test_the_rollup_says_how_many_members_were_measured(store):
    """A tree of two where one was hand-reconciled: the composition shown is
    one member's, and ``attested`` is what stops it reading as the tree's."""
    _account, session_id = open_account_session(store)
    root = _measured_launch(store, session_id)
    other = _book(store, session_id, parent_launch=root)
    reconcile_launch(store, launch_id=other.launch_id, actual_tokens=500)

    rollup = tree_rollup(store, root)
    assert rollup["actual_tokens_total"] == 11461 + 500
    assert rollup["usage_split"]["attested"] == 1
    assert rollup["usage_split"]["of_rows"] == 2


def test_the_rollup_reports_no_totals_when_nothing_was_measured(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=500)
    rollup = tree_rollup(store, booked.launch_id)
    assert rollup["usage_split"]["totals"] is None


def test_the_rollup_cli_prints_the_split(store, capsys):
    from trialerror.cli import main

    _account, session_id = open_account_session(store)
    root = _measured_launch(store, session_id)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = main([
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "budget", "rollup", "--launch-id", root,
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["result"]["usage_split"]["totals"]["usage_input_tokens"] == 2


# ---------------------------------------------------------------------------
# the dashboard budget card
# ---------------------------------------------------------------------------


def test_the_budget_card_shows_the_split_and_where_the_numbers_came_from(store, program_root, platform_root):
    from trialerror.dashboard.data import build_budget_panel
    from trialerror.dashboard.store_ro import open_store_ro

    _account, session_id = open_account_session(store)
    _measured_launch(store, session_id)
    hand = _book(store, session_id)
    reconcile_launch(store, launch_id=hand.launch_id, actual_tokens=500)
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = build_budget_panel(rostore)
    finally:
        rostore.close()

    account = panel["accounts"][0]
    assert account["usage_split"]["attested"] == 1
    assert account["usage_split"]["of_rows"] == 2
    assert account["usage_split"]["totals"]["usage_cache_read_tokens"] == 11455
    assert account["reconcile_sources"] == {"event": 1, "manual": 1}


def test_the_budget_card_shows_no_composition_when_nothing_was_measured(
    store, program_root, platform_root
):
    from trialerror.dashboard.data import build_budget_panel
    from trialerror.dashboard.store_ro import open_store_ro

    _account, session_id = open_account_session(store)
    hand = _book(store, session_id)
    reconcile_launch(store, launch_id=hand.launch_id, actual_tokens=500)
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = build_budget_panel(rostore)
    finally:
        rostore.close()

    assert panel["accounts"][0]["usage_split"]["totals"] is None
    assert panel["accounts"][0]["reconcile_sources"] == {"manual": 1}
