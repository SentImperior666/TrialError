"""``trialerror.hooks.post_task.extract_usage`` — the usage reading the
PostToolUse payload carries, and every shape it has to degrade on.

Lane FB-3 item 1 (D-FB-13 (a)). The live shape was verified against Claude
Code's own recorded subagent-tool result before this was written (see the
lane's IMPL report and :func:`extract_usage`'s docstring): the subagent
tool's result DOES carry a provider ``usage`` object plus a ``totalTokens``
that equalled the sum of the split. ``LIVE_TOOL_RESPONSE`` below is that
recorded object with the free-text ``content``/``prompt`` fields dropped —
it is the contract this reader is written against, so a host change that
renames one of these keys shows up here rather than as a silently null
usage column months later.

The other half of this file is the degradation surface. A PostToolUse hook
fires AFTER the tool already ran, so there is no outcome in which raising is
better than reporting nothing: every non-conforming shape must come back as
``None``, and ``None`` must be written into the event payload as an explicit
``usage: null`` so "the host sent nothing" stays distinguishable from
"nobody looked".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.budget.pools import book_launch
from trialerror.hooks.post_task import USAGE_SPLIT_KEYS, extract_usage
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

POST_TASK = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "post_task.py"

#: Claude Code's own recorded subagent-tool result (2026-09-16), verbatim
#: apart from the dropped free-text fields. ``totalTokens`` (11461) is
#: exactly ``2 + 0 + 11455 + 4``.
LIVE_TOOL_RESPONSE = {
    "status": "completed",
    "agentId": "a0244521ba2d63792",
    "agentType": "general-purpose",
    "resolvedModel": "claude-opus-5[1m]",
    "totalDurationMs": 1353,
    "totalTokens": 11461,
    "totalToolUseCount": 0,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 11455,
        "output_tokens": 4,
        "output_tokens_details": {"thinking_tokens": 0},
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
        "inference_geo": "not_available",
        "iterations": [{"input_tokens": 2, "output_tokens": 4, "type": "message"}],
        "speed": "standard",
    },
}


# ---------------------------------------------------------------------------
# the live shape
# ---------------------------------------------------------------------------


def test_the_live_recorded_shape_yields_the_total_and_the_whole_split():
    usage = extract_usage(LIVE_TOOL_RESPONSE)
    assert usage == {
        "total_tokens": 11461,
        "total_source": "totalTokens",
        "input_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 11455,
        "output_tokens": 4,
    }


def test_the_reported_total_equals_the_split_in_the_live_shape():
    """Not a tautology about the reader: a claim about the HOST. If a future
    Claude Code reports a ``totalTokens`` that is not the sum of its own
    split, `--from-event` is reconciling against a number whose composition
    nobody has checked, and this is where that shows up."""
    usage = extract_usage(LIVE_TOOL_RESPONSE)
    assert usage["total_tokens"] == sum(usage[key] for key in USAGE_SPLIT_KEYS)


def test_nothing_but_the_total_and_the_split_is_carried_through():
    """The telemetry around the numbers (iterations, service_tier, the nested
    detail tables) is deliberately dropped: the event payload is a token
    count for reconciliation, not a copy of the host's observability."""
    usage = extract_usage(LIVE_TOOL_RESPONSE)
    assert set(usage) == {"total_tokens", "total_source", *USAGE_SPLIT_KEYS}


def test_a_split_with_no_reported_total_is_summed_and_says_so():
    response = {"usage": {k: 5 for k in USAGE_SPLIT_KEYS}}
    usage = extract_usage(response)
    assert usage["total_tokens"] == 20
    assert usage["total_source"] == "sum(split)"


def test_a_snake_case_total_is_read_too():
    """A raw provider ``usage`` object, should one ever arrive as the whole
    ``tool_response``, spells it ``total_tokens``."""
    usage = extract_usage({"total_tokens": 99, "usage": {"input_tokens": 1}})
    assert usage["total_tokens"] == 99
    assert usage["total_source"] == "total_tokens"


def test_a_total_reported_inside_the_usage_object_is_read():
    usage = extract_usage({"usage": {"totalTokens": 77, "output_tokens": 4}})
    assert usage["total_tokens"] == 77
    assert usage["output_tokens"] == 4


def test_a_partial_split_reports_the_numbers_it_has_and_nulls_the_rest():
    usage = extract_usage({"usage": {"input_tokens": 3, "output_tokens": 7}})
    assert usage["total_tokens"] == 10
    assert usage["cache_read_input_tokens"] is None
    assert usage["cache_creation_input_tokens"] is None


def test_a_total_with_no_split_at_all_still_reports_the_total():
    usage = extract_usage({"totalTokens": 1234})
    assert usage["total_tokens"] == 1234
    assert all(usage[key] is None for key in USAGE_SPLIT_KEYS)


# ---------------------------------------------------------------------------
# every shape that must degrade to None rather than raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_response",
    [
        pytest.param(None, id="absent"),
        pytest.param("done", id="bare-string"),
        pytest.param(["a", "b"], id="content-block-list"),
        pytest.param(42, id="number"),
        pytest.param({}, id="empty-mapping"),
        pytest.param({"content": "done"}, id="no-usage-key"),
        pytest.param({"usage": None}, id="usage-null"),
        pytest.param({"usage": "11455"}, id="usage-string"),
        pytest.param({"usage": []}, id="usage-list"),
        pytest.param({"usage": {"input_tokens": "2"}}, id="numeric-string-split"),
        pytest.param({"usage": {"input_tokens": True}}, id="bool-split"),
        pytest.param({"usage": {"input_tokens": -1}}, id="negative-split"),
        pytest.param({"usage": {"input_tokens": 2.5}}, id="float-split"),
        pytest.param({"usage": {"prompt_tokens": 12}}, id="foreign-key-names"),
        pytest.param({"totalTokens": "1234"}, id="numeric-string-total"),
        pytest.param({"status": "cancelled", "usage": {}}, id="cancelled-empty-usage"),
    ],
)
def test_every_non_conforming_shape_degrades_to_none(tool_response):
    assert extract_usage(tool_response) is None


def test_a_foreign_key_name_is_not_silently_treated_as_a_total():
    """``prompt_tokens``/``completion_tokens`` are another provider's
    spelling. Reading them as ours would put a number nobody verified into a
    reconciliation; reporting nothing is the honest outcome."""
    assert extract_usage({"usage": {"prompt_tokens": 12, "completion_tokens": 3}}) is None


# ---------------------------------------------------------------------------
# the written event payload
# ---------------------------------------------------------------------------


@pytest.fixture()
def roots(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    return platform_root, program_root


def _run_hook(payload: dict, *, platform_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    # ``python plugin/hooks/post_task.py`` puts the SCRIPT's directory on
    # sys.path, not the CWD, so a bare ``import trialerror`` inside the
    # subprocess resolves to whatever copy pip installed -- which, in a git
    # worktree, is the other checkout. Naming the tree this test file lives
    # in makes the subprocess exercise the code under test rather than a
    # sibling checkout's copy of it; on a normal (non-worktree) tree the two
    # are the same directory and this changes nothing.
    tree_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([tree_root, env["PYTHONPATH"]]) if env.get("PYTHONPATH") else tree_root
    return subprocess.run(
        [sys.executable, str(POST_TASK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _booked_launch(platform_root: Path, program_root: Path) -> str:
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    booked = book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=10,
    )
    store.close()
    return booked.launch_id


def _subagent_return_payload(platform_root: Path, program_root: Path) -> dict:
    store = open_store(program_root, platform_root=platform_root)
    try:
        rows = store.ops.execute("SELECT payload FROM event WHERE type='subagent_return'").fetchall()
        assert len(rows) == 1
        return json.loads(rows[0]["payload"])
    finally:
        store.close()


def test_the_hook_writes_the_live_usage_into_the_subagent_return_payload(roots):
    platform_root, program_root = roots
    launch_id = _booked_launch(platform_root, program_root)
    proc = _run_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_input": {"prompt": f"do the thing. launch_id: {launch_id}"},
            "tool_response": LIVE_TOOL_RESPONSE,
            "cwd": str(program_root),
        },
        platform_root=platform_root,
    )
    assert proc.returncode == 0, proc.stderr

    payload = _subagent_return_payload(platform_root, program_root)
    assert payload["response_size_bytes"] > 0
    assert payload["usage"]["total_tokens"] == 11461
    assert payload["usage"]["cache_read_input_tokens"] == 11455


def test_a_payload_with_no_usage_writes_an_explicit_null_and_still_exits_zero(roots):
    """The key is ALWAYS present. An absent key would leave a reader unable
    to tell a host that sent nothing from a hook that never looked."""
    platform_root, program_root = roots
    launch_id = _booked_launch(platform_root, program_root)
    proc = _run_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Task",
            "tool_input": {"prompt": f"do the thing. launch_id: {launch_id}"},
            "tool_response": {"content": "done"},
            "cwd": str(program_root),
        },
        platform_root=platform_root,
    )
    assert proc.returncode == 0, proc.stderr

    payload = _subagent_return_payload(platform_root, program_root)
    assert "usage" in payload
    assert payload["usage"] is None
