"""``budget reconcile --from-event`` and the provenance it records.

Lane FB-3 items 2, 3 and 4 (D-FB-13 (b)/(c)/(d)). The question these
tests hold down is the one the whole disposition exists for: after a
reconciliation, can anybody tell whether the number came from a measurement
or from a person's best guess? ``reconcile_source`` is the answer, and it is
only an answer if ``'event'`` cannot be typed by hand.
"""

from __future__ import annotations

import json

import pytest

from trialerror.budget.errors import BudgetError
from trialerror.budget.pools import (
    ASSERTABLE_RECONCILE_SOURCES,
    EVENT_RECONCILE_SOURCE,
    USAGE_COLUMNS,
    book_launch,
    latest_subagent_return,
    reconcile_launch,
    reconcile_launch_from_event,
)
from trialerror.events.api import append_event
from trialerror.stores import get

from tests._budget_fixtures import open_account_session

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
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=100,
    )
    kwargs.update(overrides)
    return book_launch(store, **kwargs)


def _return_event(store, session_id, launch_id, usage, *, ts=None):
    return append_event(
        store,
        event_type="subagent_return",
        session_id=session_id,
        launch_id=launch_id,
        payload={"response_size_bytes": 42, "duration_ms": None, "usage": usage},
        ts=ts,
    )


# ---------------------------------------------------------------------------
# the measured path
# ---------------------------------------------------------------------------


def test_from_event_settles_the_launch_from_the_recorded_usage(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    event = _return_event(store, session_id, booked.launch_id, LIVE_USAGE)

    result = reconcile_launch_from_event(store, launch_id=booked.launch_id)

    assert result["actual_tokens"] == 11461
    assert result["reconcile_source"] == EVENT_RECONCILE_SOURCE
    assert result["event"]["event_id"] == event["event_id"]
    assert result["event"]["total_source"] == "totalTokens"

    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["state"] == "RECONCILED"
    assert row["actual_tokens"] == 11461
    assert row["reconcile_source"] == "event"


def test_from_event_fills_the_split_columns(store):
    """D-FB-13 (c): the four columns carry the composition, so a later reader
    can tell 11,455 cache-read tokens from 11,455 fresh input tokens -- which
    is the difference between a cheap launch and an expensive one."""
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    _return_event(store, session_id, booked.launch_id, LIVE_USAGE)

    reconcile_launch_from_event(store, launch_id=booked.launch_id)

    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["usage_input_tokens"] == 2
    assert row["usage_cache_creation_tokens"] == 0
    assert row["usage_cache_read_tokens"] == 11455
    assert row["usage_output_tokens"] == 4
    assert row["actual_tokens"] == sum(row[c] for c in USAGE_COLUMNS)


def test_a_partial_usage_object_leaves_the_unreported_columns_null(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    _return_event(
        store, session_id, booked.launch_id,
        {"total_tokens": 10, "total_source": "sum(split)", "input_tokens": 3, "output_tokens": 7,
         "cache_creation_input_tokens": None, "cache_read_input_tokens": None},
    )

    reconcile_launch_from_event(store, launch_id=booked.launch_id)

    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["usage_input_tokens"] == 3
    assert row["usage_output_tokens"] == 7
    assert row["usage_cache_read_tokens"] is None
    assert row["usage_cache_creation_tokens"] is None


def test_actual_tokens_leaves_every_split_column_null(store):
    """The contrast that makes the columns readable: a hand-reconciled launch
    makes NO claim about composition, and a zero would have been a claim."""
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)

    reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=9000)

    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["reconcile_source"] == "manual"
    assert all(row[column] is None for column in USAGE_COLUMNS)


def test_the_newest_return_event_is_the_one_read(store):
    """A booking that returned twice (a resumed workflow, a retried spawn)
    has two events; the run that finished is the later one."""
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    _return_event(store, session_id, booked.launch_id, dict(LIVE_USAGE, total_tokens=1),
                  ts="2026-09-15T10:00:00.000Z")
    later = _return_event(store, session_id, booked.launch_id, dict(LIVE_USAGE, total_tokens=2222),
                          ts="2026-09-15T11:00:00.000Z")

    assert latest_subagent_return(store, booked.launch_id)["event_id"] == later["event_id"]
    result = reconcile_launch_from_event(store, launch_id=booked.launch_id)
    assert result["actual_tokens"] == 2222


def test_the_pool_running_total_moves_by_the_measured_number(store):
    from trialerror.budget.pools import create_pool

    account_id, session_id = open_account_session(store)
    pool = create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=10**6)
    booked = _book(store, session_id)
    _return_event(store, session_id, booked.launch_id, LIVE_USAGE)

    reconcile_launch_from_event(store, launch_id=booked.launch_id)

    row = get(store, "budget_pool", pk_column="pool_id", pk_value=pool["pool_id"])
    assert row["spent_visible_tokens"] == 11461


# ---------------------------------------------------------------------------
# the three named refusals
# ---------------------------------------------------------------------------


def test_from_event_refuses_a_launch_with_no_return_event(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    with pytest.raises(BudgetError) as exc:
        reconcile_launch_from_event(store, launch_id=booked.launch_id)
    assert "no subagent_return event" in str(exc.value)
    assert "--actual-tokens" in str(exc.value)
    # and the launch is untouched -- a refused reconciliation settles nothing
    assert get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)["state"] == "PROVISIONAL"


def test_from_event_refuses_a_null_usage_naming_the_event(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    event = _return_event(store, session_id, booked.launch_id, None)

    with pytest.raises(BudgetError) as exc:
        reconcile_launch_from_event(store, launch_id=booked.launch_id)
    assert event["event_id"] in str(exc.value)
    assert "usage: null" in str(exc.value)
    assert get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)["state"] == "PROVISIONAL"


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"total_tokens": None}, id="total-null"),
        pytest.param({"total_tokens": "11461"}, id="total-string"),
        pytest.param({"total_tokens": -5}, id="total-negative"),
        pytest.param({"input_tokens": 2}, id="no-total-key"),
    ],
)
def test_from_event_refuses_a_usage_object_with_no_usable_total(store, usage):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    _return_event(store, session_id, booked.launch_id, usage)
    with pytest.raises(BudgetError) as exc:
        reconcile_launch_from_event(store, launch_id=booked.launch_id)
    assert "no usable total" in str(exc.value)


def test_from_event_refuses_an_already_reconciled_launch(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    _return_event(store, session_id, booked.launch_id, LIVE_USAGE)
    reconcile_launch_from_event(store, launch_id=booked.launch_id)
    with pytest.raises(BudgetError) as exc:
        reconcile_launch_from_event(store, launch_id=booked.launch_id)
    assert "cannot reconcile twice" in str(exc.value)


def test_another_launchs_event_is_not_read(store):
    _account, session_id = open_account_session(store)
    mine = _book(store, session_id)
    theirs = _book(store, session_id)
    _return_event(store, session_id, theirs.launch_id, LIVE_USAGE)
    with pytest.raises(BudgetError) as exc:
        reconcile_launch_from_event(store, launch_id=mine.launch_id)
    assert "no subagent_return event" in str(exc.value)


# ---------------------------------------------------------------------------
# 'event' is not a label a caller may assert
# ---------------------------------------------------------------------------


def test_reconcile_launch_refuses_the_event_source_by_hand(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    with pytest.raises(BudgetError) as exc:
        reconcile_launch(
            store, launch_id=booked.launch_id, actual_tokens=9000, reconcile_source=EVENT_RECONCILE_SOURCE
        )
    assert "--from-event" in str(exc.value)
    assert get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)["state"] == "PROVISIONAL"


def test_reconcile_launch_refuses_an_unknown_source(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    with pytest.raises(BudgetError):
        reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=1, reconcile_source="vibes")


@pytest.mark.parametrize("source", ASSERTABLE_RECONCILE_SOURCES)
def test_every_assertable_source_still_works(store, source):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    result = reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=7, reconcile_source=source)
    assert result["reconcile_source"] == source


def test_the_mcp_reconcile_tool_cannot_assert_the_event_source(store):
    """The MCP tool passes ``reconcile_source`` straight through from its
    args, so the refusal has to live in the write path, not in the CLI."""
    from trialerror.mcp.ops import _tool_reconcile_launch

    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    envelope = _tool_reconcile_launch(
        {"launch_id": booked.launch_id, "actual_tokens": 9000, "reconcile_source": "event"}, store=store
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "reconcile_refused"


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------


def _cli(argv, program_root, platform_root):
    from trialerror.cli import main

    return main(
        ["--program-root", str(program_root), "--platform-root", str(platform_root), "budget", *argv]
    )


def test_cli_actual_tokens_and_from_event_are_mutually_exclusive(store, capsys):
    with pytest.raises(SystemExit):
        _cli(
            ["reconcile", "--launch-id", "LNCH-x", "--actual-tokens", "1", "--from-event"],
            store.program_root,
            store.platform_root,
        )
    assert "not allowed with" in capsys.readouterr().err


def test_cli_reconcile_requires_one_of_the_two(store, capsys):
    with pytest.raises(SystemExit):
        _cli(["reconcile", "--launch-id", "LNCH-x"], store.program_root, store.platform_root)
    err = capsys.readouterr().err
    assert "--actual-tokens" in err and "--from-event" in err


def test_cli_reconcile_source_does_not_offer_event(store, capsys):
    with pytest.raises(SystemExit):
        _cli(
            ["reconcile", "--launch-id", "LNCH-x", "--actual-tokens", "1", "--reconcile-source", "event"],
            store.program_root,
            store.platform_root,
        )
    assert "invalid choice" in capsys.readouterr().err


def test_cli_from_event_reports_the_event_row_it_read(store, capsys):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    event = _return_event(store, session_id, booked.launch_id, LIVE_USAGE)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(["reconcile", "--launch-id", booked.launch_id, "--from-event"], program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["ok"] is True
    assert out["result"]["actual_tokens"] == 11461
    assert out["result"]["reconcile_source"] == "event"
    assert out["result"]["event"]["event_id"] == event["event_id"]


def test_cli_from_event_refusal_names_the_other_path(store, capsys):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    _return_event(store, session_id, booked.launch_id, None)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(["reconcile", "--launch-id", booked.launch_id, "--from-event"], program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code != 0
    assert out["error"]["code"] == "reconcile_refused"
    assert any("--actual-tokens" in " ".join(a["argv"]) for a in out["nextActions"])


# ---------------------------------------------------------------------------
# Fix pass V-8: a label the command cannot honour is refused, not dropped
# ---------------------------------------------------------------------------


def test_from_event_refuses_an_explicit_reconcile_source(store, capsys):
    """The sibling pair --actual-tokens/--from-event is refused by argparse,
    which makes the silence here the odd one out: the label was accepted and
    then discarded, so the envelope reported a provenance the operator had
    not asked for."""
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    _return_event(store, session_id, booked.launch_id, LIVE_USAGE)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(
        ["reconcile", "--launch-id", booked.launch_id, "--from-event", "--reconcile-source", "transcript"],
        program_root,
        platform_root,
    )
    out = json.loads(capsys.readouterr().out)
    assert code != 0
    assert out["error"]["code"] == "conflicting_arguments"
    assert "--reconcile-source" in out["error"]["message"]


def test_actual_tokens_still_defaults_to_manual(store, capsys):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(
        ["reconcile", "--launch-id", booked.launch_id, "--actual-tokens", "900"],
        program_root,
        platform_root,
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["result"]["reconcile_source"] == "manual"
