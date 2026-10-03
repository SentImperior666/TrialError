"""A spawn that never started gives its booking back; a spawn that did start
never does.

Fixtures come from the shapes Claude Code's hooks actually send: PreToolUse,
PostToolUse and PostToolUseFailure payloads with exactly the documented keys,
and ``agent-<id>.meta.json`` files with every observed key. Every store lives
in a temporary directory (the autouse fixtures in ``conftest.py`` isolate the
home, the platform root and the probe log).
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trialerror.budget import spawn_release
from trialerror.budget.pools import book_launch
from trialerror.hooks import post_task, spawn_failure, spawn_gate
from trialerror.hooks.probe_log import hook_events_path
from trialerror.stores import get, insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now, parse

SESSION_UUID = "0b1c2d3e-aaaa-bbbb-cccc-123456789abc"
TOOL_USE_ID = "toolu_01ABCDEFGHIJKLMNOPQRSTUV"
AGENT_ID = "a0244521ba2d63792"


@pytest.fixture()
def world(tmp_path, platform_root, program_root):
    """A program with one open session and a helper that books launches."""
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()

    transcript_dir = tmp_path / "claude_projects" / "proj"
    transcript_dir.mkdir(parents=True)

    class World:
        pass

    w = World()
    w.program_root = program_root
    w.platform_root = platform_root
    w.session_id = session_id
    w.transcript_path = str(transcript_dir / f"{SESSION_UUID}.jsonl")
    w.spawn_dir = transcript_dir / SESSION_UUID
    w.subagents = w.spawn_dir / "subagents"

    def book(**over):
        s = open_store(program_root, platform_root=platform_root)
        try:
            kwargs = dict(
                session_id=session_id,
                program_id="PROG-test",
                agent_kind="lens",
                model_class="mid",
                model="sonnet",
                purpose="mechanical",
                est_tokens=100,
            )
            kwargs.update(over)
            return book_launch(s, **kwargs).launch_id
        finally:
            s.close()

    def launch(launch_id):
        s = open_store(program_root, platform_root=platform_root)
        try:
            return dict(get(s, "launch", pk_column="launch_id", pk_value=launch_id))
        finally:
            s.close()

    def pre_payload(launch_id, **over):
        payload = {
            "cwd": str(program_root),
            "hook_event_name": "PreToolUse",
            "permission_mode": "default",
            "prompt_id": "p-1",
            "scratchpad_dir": str(tmp_path / "scratch"),
            "session_id": SESSION_UUID,
            "tool_input": {
                "subagent_type": "trialerror:critic",
                "description": "x",
                "prompt": f"you are a lens. launch_id: {launch_id}",
            },
            "tool_name": "Agent",
            "tool_use_id": TOOL_USE_ID,
            "transcript_path": w.transcript_path,
        }
        payload.update(over)
        return payload

    w.book, w.launch, w.pre_payload = book, launch, pre_payload
    return w


def _gate(w, payload):
    return spawn_gate._evaluate(payload)


# ---------------------------------------------------------------------------
# A2: the gate stores the spawn's identity, and cannot fail on a missing key
# ---------------------------------------------------------------------------


def test_the_gate_stores_the_spawn_identity(world):
    launch_id = world.book()
    code, message = _gate(world, world.pre_payload(launch_id))
    assert (code, message) == (0, None)
    row = world.launch(launch_id)
    assert row["state"] == "RUNNING"
    assert row["spawn_tool_use_id"] == TOOL_USE_ID
    assert row["spawn_ts"]
    assert row["spawn_transcript_dir"] == str(Path(world.transcript_path).parent / SESSION_UUID)
    assert Path(row["spawn_transcript_dir"]).is_absolute()
    assert row["agent_id"] is None


def test_the_gate_behaves_as_before_when_no_ids_are_given(world):
    """Direct callers (and every existing test) pass nothing: the columns stay NULL."""
    from trialerror.budget.gate import evaluate_spawn

    launch_id = world.book()
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        result = evaluate_spawn(s, f"launch_id: {launch_id}", session_id=world.session_id)
        assert result.allowed and result.code == "consumed"
    finally:
        s.close()
    row = world.launch(launch_id)
    assert row["state"] == "RUNNING"
    assert row["spawn_tool_use_id"] is None
    assert row["spawn_ts"] is None
    assert row["spawn_transcript_dir"] is None


@pytest.mark.parametrize(
    "drop",
    [
        ("tool_use_id",),
        ("transcript_path",),
        ("session_id",),
        ("tool_use_id", "transcript_path", "session_id"),
    ],
)
def test_a_payload_without_the_new_keys_still_exits_zero(world, drop):
    launch_id = world.book()
    payload = world.pre_payload(launch_id)
    for key in drop:
        del payload[key]
    assert _gate(world, payload) == (0, None)
    row = world.launch(launch_id)
    assert row["state"] == "RUNNING"
    if "transcript_path" in drop or "session_id" in drop:
        assert row["spawn_transcript_dir"] is None


@pytest.mark.parametrize(
    "over",
    [
        {"tool_use_id": 7},
        {"tool_use_id": ""},
        {"transcript_path": 12},
        {"transcript_path": ""},
        {"session_id": ["x"]},
        {"transcript_path": "relative/only.jsonl"},
    ],
)
def test_oddly_typed_or_relative_values_never_refuse_the_spawn(world, over):
    launch_id = world.book()
    assert _gate(world, world.pre_payload(launch_id, **over)) == (0, None)
    row = world.launch(launch_id)
    assert row["state"] == "RUNNING"
    if "transcript_path" in over:
        assert row["spawn_transcript_dir"] is None


# ---------------------------------------------------------------------------
# A3: PostToolUse records the agent
# ---------------------------------------------------------------------------


def _post_payload(world, launch_id, **over):
    payload = {
        "cwd": str(world.program_root),
        "duration_ms": 1353,
        "hook_event_name": "PostToolUse",
        "permission_mode": "default",
        "prompt_id": "p-1",
        "scratchpad_dir": "s",
        "session_id": SESSION_UUID,
        "tool_input": {
            "subagent_type": "trialerror:critic",
            "description": "x",
            "prompt": f"you are a lens. launch_id: {launch_id}",
        },
        "tool_name": "Agent",
        "tool_response": {
            "status": "completed",
            "agentId": AGENT_ID,
            "agentType": "trialerror:critic",
            "resolvedModel": "claude-opus-5[1m]",
            "totalTokens": 11461,
        },
        "tool_use_id": TOOL_USE_ID,
        "transcript_path": world.transcript_path,
    }
    payload.update(over)
    return payload


def test_post_task_records_the_agent_id_and_the_resolved_model(world):
    launch_id = world.book()
    _gate(world, world.pre_payload(launch_id))
    assert post_task._evaluate(_post_payload(world, launch_id)) is None
    row = world.launch(launch_id)
    assert row["agent_id"] == AGENT_ID
    assert json.loads(row["attrs"])["spawned_model"] == "claude-opus-5[1m]"
    assert row["state"] == "RUNNING"


def test_post_task_keeps_an_existing_spawned_model(world):
    launch_id = world.book()
    _gate(world, world.pre_payload(launch_id))
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute(
                "UPDATE launch SET attrs = ? WHERE launch_id = ?",
                (json.dumps({"spawned_model": "already"}), launch_id),
            )
    finally:
        s.close()
    post_task._evaluate(_post_payload(world, launch_id))
    assert json.loads(world.launch(launch_id)["attrs"])["spawned_model"] == "already"


def test_post_task_ignores_a_different_tool_use_id(world):
    launch_id = world.book()
    _gate(world, world.pre_payload(launch_id))
    post_task._evaluate(_post_payload(world, launch_id, tool_use_id="toolu_other"))
    assert world.launch(launch_id)["agent_id"] is None


@pytest.mark.parametrize("response", [None, "text", ["x"], {"status": "async_launched"}, {"agentId": 5}])
def test_post_task_swallows_odd_responses(world, response):
    launch_id = world.book()
    _gate(world, world.pre_payload(launch_id))
    assert post_task._evaluate(_post_payload(world, launch_id, tool_response=response)) is None
    assert world.launch(launch_id)["agent_id"] is None


# ---------------------------------------------------------------------------
# A4: the spawn-failure hook
# ---------------------------------------------------------------------------

BOGUS_ERROR = "Agent type 'no-such-agent' not found. Available agents: general-purpose, critic"
MARKER = "ZEBRA-MARKER-7741"


def _fail_payload(world, launch_id, **over):
    payload = {
        "cwd": str(world.program_root),
        "duration_ms": 12,
        "error": BOGUS_ERROR,
        "hook_event_name": "PostToolUseFailure",
        "permission_mode": "default",
        "prompt_id": "p-1",
        "session_id": SESSION_UUID,
        "tool_input": {
            "subagent_type": "no-such-agent",
            "description": "x",
            "prompt": f"you are a lens. launch_id: {launch_id}",
            "run_in_background": False,
        },
        "tool_name": "Agent",
        "tool_use_id": TOOL_USE_ID,
        "transcript_path": world.transcript_path,
    }
    payload.update(over)
    return payload


def _write_meta(world, *, tool_use_id=TOOL_USE_ID, sub="", mtime=None, name="agent-a1.meta.json"):
    folder = world.subagents / sub if sub else world.subagents
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(
        json.dumps(
            {
                "agentType": "trialerror:critic",
                "description": "x",
                "toolUseId": tool_use_id,
                "spawnDepth": 1,
                "requestShape": "foreground",
            }
        ),
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _events(world, event_type):
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        rows = s.ops.execute("SELECT payload FROM event WHERE type = ?", (event_type,)).fetchall()
        return [json.loads(r["payload"]) for r in rows]
    finally:
        s.close()


def _spawn(world, **book_over):
    launch_id = world.book(**book_over)
    assert _gate(world, world.pre_payload(launch_id)) == (0, None)
    return launch_id


def test_a_spawn_that_never_started_gives_its_booking_back(world):
    launch_id = _spawn(world)
    spawn_failure._evaluate(_fail_payload(world, launch_id))

    row = world.launch(launch_id)
    assert row["state"] == "PROVISIONAL"
    for column in ("spawn_tool_use_id", "spawn_ts", "spawn_transcript_dir", "agent_id"):
        assert row[column] is None
    failures = json.loads(row["attrs"])["spawn_failures"]
    assert len(failures) == 1
    assert failures[0]["tool_use_id"] == TOOL_USE_ID
    assert failures[0]["error_class"] == "agent_type_unknown"
    assert failures[0]["run_in_background"] is False
    assert "spawn_failure_after_start" not in json.loads(row["attrs"])

    released = _events(world, "launch_spawn_released")
    assert len(released) == 1
    assert released[0]["launch_id"] == launch_id
    assert released[0]["meta_found"] is False and released[0]["search_complete"] is True
    assert _events(world, "launch_spawn_failed_after_start") == []

    # The corrected retry uses the same booking.
    assert _gate(world, world.pre_payload(launch_id, tool_use_id="toolu_retry")) == (0, None)
    retry = world.launch(launch_id)
    assert retry["state"] == "RUNNING" and retry["spawn_tool_use_id"] == "toolu_retry"
    assert len(json.loads(retry["attrs"])["spawn_failures"]) == 1


def _no_release_asserts(world, launch_id, *, expect):
    row = world.launch(launch_id)
    assert row["state"] == "RUNNING"
    assert row["spawn_tool_use_id"] == TOOL_USE_ID
    attrs = json.loads(row["attrs"])
    assert "spawn_failures" not in attrs
    record = attrs["spawn_failure_after_start"]
    assert record["tool_use_id"] == TOOL_USE_ID
    for key, value in expect.items():
        assert record[key] == value, (key, record)
    assert len(_events(world, "launch_spawn_failed_after_start")) == 1
    assert _events(world, "launch_spawn_released") == []


def test_no_release_when_a_matching_meta_json_exists(world):
    launch_id = _spawn(world)
    _write_meta(world)
    spawn_failure._evaluate(_fail_payload(world, launch_id, error="Agent stopped: max turns reached"))
    _no_release_asserts(world, launch_id, expect={"meta_found": True, "search_complete": True})


def test_no_release_after_a_heartbeat_moved_booked_ts_past_the_meta_json(world):
    """The critic's case: ``booked_ts`` moves to a later moment than the
    spawn, and the agent's meta.json is older than that moment. The search
    filter is built on ``spawn_ts``, so the file is still found."""
    launch_id = _spawn(world)
    epoch = parse(world.launch(launch_id)["spawn_ts"]).timestamp()
    _write_meta(world, mtime=epoch + 1)
    later = datetime.fromtimestamp(epoch + 300, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute("UPDATE launch SET booked_ts = ? WHERE launch_id = ?", (later, launch_id))
    finally:
        s.close()
    assert parse(world.launch(launch_id)["booked_ts"]).timestamp() > epoch + 1  # not a spawn time
    spawn_failure._evaluate(_fail_payload(world, launch_id, error="Agent stopped: max turns reached"))
    _no_release_asserts(world, launch_id, expect={"meta_found": True})


def test_no_release_when_the_failure_is_an_interrupt(world):
    launch_id = _spawn(world)
    spawn_failure._evaluate(_fail_payload(world, launch_id, is_interrupt=True))
    _no_release_asserts(world, launch_id, expect={"is_interrupt": True, "error_class": "interrupted"})


def test_no_release_when_the_search_overran_its_budget(world, monkeypatch):
    launch_id = _spawn(world)
    _write_meta(world, tool_use_id="toolu_someone_else")
    ticks = iter(range(0, 10_000, 5))
    monkeypatch.setattr(spawn_release, "_clock", lambda: float(next(ticks)))
    spawn_failure._evaluate(_fail_payload(world, launch_id))
    _no_release_asserts(world, launch_id, expect={"search_complete": False, "meta_found": False})


def test_no_release_when_the_launch_has_no_spawn_ts(world):
    """A booking gated before the identity columns existed."""
    launch_id = _spawn(world)
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute(
                "UPDATE launch SET spawn_ts = NULL, spawn_transcript_dir = NULL WHERE launch_id = ?",
                (launch_id,),
            )
    finally:
        s.close()
    spawn_failure._evaluate(_fail_payload(world, launch_id))
    _no_release_asserts(world, launch_id, expect={"meta_found": None, "search_complete": False})


def test_no_release_for_an_error_class_other(world):
    launch_id = _spawn(world)
    spawn_failure._evaluate(_fail_payload(world, launch_id, error="Something else went wrong"))
    _no_release_asserts(world, launch_id, expect={"error_class": "other", "meta_found": False})


def test_no_release_when_a_file_in_the_window_cannot_be_read(world):
    launch_id = _spawn(world)
    world.subagents.mkdir(parents=True)
    (world.subagents / "agent-broken.meta.json").write_text("{not json", encoding="utf-8")
    spawn_failure._evaluate(_fail_payload(world, launch_id))
    _no_release_asserts(world, launch_id, expect={"search_complete": False})


def test_a_meta_json_older_than_the_spawn_window_is_not_a_match(world):
    """Only files at or after ``spawn_ts - 60 s`` are read: an old file for
    the same id (which cannot belong to this spawn) does not block a release."""
    launch_id = _spawn(world)
    epoch = parse(world.launch(launch_id)["spawn_ts"]).timestamp()
    _write_meta(world, mtime=epoch - 3600)
    spawn_failure._evaluate(_fail_payload(world, launch_id))
    assert world.launch(launch_id)["state"] == "PROVISIONAL"


def test_workflow_folders_are_not_searched(world):
    launch_id = _spawn(world)
    _write_meta(world, sub="workflows/wf_x")
    spawn_failure._evaluate(_fail_payload(world, launch_id))
    assert world.launch(launch_id)["state"] == "PROVISIONAL"


def test_the_error_text_is_never_stored(world):
    launch_id = _spawn(world)
    error = f"{BOGUS_ERROR} {MARKER}"
    spawn_failure._evaluate(
        _fail_payload(
            world,
            launch_id,
            error=error,
            tool_input={
                "subagent_type": "x",
                "description": "d",
                "prompt": f"launch_id: {launch_id} {MARKER}",
                "run_in_background": True,
            },
        )
    )
    text = hook_events_path().read_text(encoding="utf-8")
    record = [json.loads(line) for line in text.splitlines() if '"spawn_failure"' in line][-1]
    assert record["error_class"] == "agent_type_unknown"
    assert record["error_len"] == len(error)
    assert record["run_in_background"] is True
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        dump = json.dumps(
            [dict(r) for r in s.ops.execute("SELECT * FROM event").fetchall()]
            + [dict(r) for r in s.platform.execute("SELECT * FROM launch").fetchall()],
            default=str,
        )
    finally:
        s.close()
    assert MARKER not in dump
    assert MARKER not in text


def test_no_launch_found_writes_the_record_and_exits_zero(world, capsys, monkeypatch):
    payload = _fail_payload(world, "L-none", tool_use_id="toolu_unbooked")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert spawn_failure.main() == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert '"spawn_failure"' in hook_events_path().read_text(encoding="utf-8")


def test_robustness_malformed_stdin_missing_store_and_unwritable_probes(world, capsys, monkeypatch, tmp_path):
    for raw in ("not json", "", "[1, 2]", "null"):
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        assert spawn_failure.main() == 0

    # A platform root with no store: nothing is created, nothing is printed.
    empty_root = tmp_path / "no_store_here"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(empty_root))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_fail_payload(world, "L-x"))))
    assert spawn_failure.main() == 0
    assert not (empty_root / "platform.db").exists()
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(world.platform_root))

    # An unwritable probes directory (a path that is a file): the release does not depend on the log.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(blocker / "sub"))
    launch_id = _spawn(world)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_fail_payload(world, launch_id))))
    assert spawn_failure.main() == 0
    assert world.launch(launch_id)["state"] == "PROVISIONAL"
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_a_failed_event_write_leaves_the_launch_update_standing(world, capsys, monkeypatch):
    launch_id = _spawn(world)

    def boom(*args, **kwargs):
        raise RuntimeError("ops store unavailable")

    monkeypatch.setattr(spawn_failure, "_append_event", boom)
    spawn_failure._evaluate(_fail_payload(world, launch_id))
    assert world.launch(launch_id)["state"] == "PROVISIONAL"
    assert "launch_spawn_released" in capsys.readouterr().err


def test_a_launch_a_later_spawn_consumed_again_is_left_alone(world):
    """The release is conditional on the failed spawn's own tool_use_id."""
    launch_id = _spawn(world)
    spawn_failure._evaluate(_fail_payload(world, launch_id, tool_use_id="toolu_a_different_spawn"))
    row = world.launch(launch_id)
    assert row["state"] == "RUNNING" and row["spawn_tool_use_id"] == TOOL_USE_ID
    assert _events(world, "launch_spawn_released") == []


ZERO_TOOLS_ERROR = (
    "Agent 'trialerror:prompt-only' would be spawned with zero tools — refusing. "
    "Its tools list resolved to nothing: unrecognized [TodoWrite]."
)

#: Texts a subagent that STARTED can fail with, or plain English that merely
#: begins like a guessed prefix. None of them may be released.
STARTED_AGENT_ERRORS = [
    "API Error: 529 overloaded_error",
    "Request timed out after 120000ms",
    "Request was aborted.",
    "Agent stopped: max turns (30) reached",
    "Unknown agent error: stream closed",
    "Error: Permission denied: EACCES, open '/tmp/x'",
    "No tools available to finish the task",
    "Permission to use Agent has been denied",
    "Error: Agent type 'x' not found",  # not the observed shape: no lead-in is accepted
]


@pytest.mark.parametrize(
    "error, expected",
    [
        ("Agent type 'x' not found. Available agents: a", "agent_type_unknown"),
        (ZERO_TOOLS_ERROR, "zero_tools"),
        ("[Request interrupted by user]", "interrupted"),
        ("something the subagent printed", "other"),
        (None, "other"),
        (7, "other"),
        *[(text, "other") for text in STARTED_AGENT_ERRORS],
    ],
)
def test_classify_error_uses_fixed_prefixes(error, expected):
    assert spawn_failure.classify_error(error) == expected


@pytest.mark.parametrize("error", STARTED_AGENT_ERRORS + ["[Request interrupted by user]"])
def test_a_started_agents_error_texts_are_never_releasable(world, error):
    assert spawn_failure.classify_error(error) not in spawn_failure._RELEASABLE_CLASSES
    launch_id = _spawn(world)
    spawn_failure._evaluate(_fail_payload(world, launch_id, error=error))
    assert world.launch(launch_id)["state"] == "RUNNING"


def test_the_releasable_classes_are_only_the_two_observed_refusals():
    assert spawn_failure._RELEASABLE_CLASSES == {"agent_type_unknown", "zero_tools"}


def test_a_refused_tool_list_gives_its_booking_back(world):
    launch_id = _spawn(world)
    spawn_failure._evaluate(_fail_payload(world, launch_id, error=ZERO_TOOLS_ERROR))
    row = world.launch(launch_id)
    assert row["state"] == "PROVISIONAL" and row["spawn_tool_use_id"] is None
    assert json.loads(row["attrs"])["spawn_failures"][0]["error_class"] == "zero_tools"
    assert _events(world, "launch_spawn_released")[0]["error_class"] == "zero_tools"


def test_the_hook_is_bound_in_the_plugin_manifest_and_the_cli():
    from trialerror.cli import hook as hook_cli

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    entry = manifest["hooks"]["PostToolUseFailure"]
    assert [e["matcher"] for e in entry] == ["^(Task|Agent)$"]
    assert entry[0]["hooks"][0]["command"] == "trialerror hook spawn-failure"
    assert "spawn-failure" in hook_cli._HOOKS


# ---------------------------------------------------------------------------
# A5: two verbs for what no hook sees, and the stranded report
# ---------------------------------------------------------------------------


def _cli(world, *argv):
    from contextlib import redirect_stdout

    from trialerror.cli import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["--program-root", str(world.program_root), "--platform-root", str(world.platform_root), *argv])
    return rc, json.loads(buf.getvalue().strip())


def test_cancel_moves_a_provisional_booking_to_abandoned(world):
    launch_id = world.book()
    rc, env = _cli(world, "budget", "cancel", "--launch", launch_id, "--reason", "not needed", "--by", "op")
    assert rc == 0 and env["ok"], env
    row = world.launch(launch_id)
    assert row["state"] == "ABANDONED"
    cancel = json.loads(row["attrs"])["cancel"]
    assert cancel["reason"] == "not needed" and cancel["by"] == "op" and cancel["ts"]
    assert len(_events(world, "launch_cancelled")) == 1


@pytest.mark.parametrize("state", ["RUNNING", "RECONCILED", "ABANDONED", "REFUSED"])
def test_cancel_refuses_any_other_state_in_plain_words(world, state):
    launch_id = world.book()
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute("UPDATE launch SET state = ? WHERE launch_id = ?", (state, launch_id))
    finally:
        s.close()
    rc, env = _cli(world, "budget", "cancel", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "wrong_state"
    assert state in env["error"]["message"] and "not PROVISIONAL" in env["error"]["message"]
    assert world.launch(launch_id)["state"] == state


def test_cancel_refuses_an_unknown_launch_and_a_missing_reason(world):
    rc, env = _cli(world, "budget", "cancel", "--launch-id", "L-nope", "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "unknown_launch"
    launch_id = world.book()
    rc, env = _cli(world, "budget", "cancel", "--launch-id", launch_id, "--reason", "  ")
    assert rc == 1 and env["error"]["code"] == "reason_required"
    assert world.launch(launch_id)["state"] == "PROVISIONAL"


def test_release_gives_back_a_running_booking_with_no_agent(world):
    launch_id = _spawn(world)
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "denied by a rule")
    assert rc == 0 and env["ok"], env
    row = world.launch(launch_id)
    assert row["state"] == "PROVISIONAL" and row["spawn_tool_use_id"] is None and row["spawn_ts"] is None
    entry = json.loads(row["attrs"])["spawn_releases"][0]
    assert entry["forced"] is False and entry["meta_found"] is False and entry["search_complete"] is True
    assert len(_events(world, "launch_spawn_released")) == 1


@pytest.mark.parametrize("state", ["PROVISIONAL", "RECONCILED", "ABANDONED"])
def test_release_refuses_states_other_than_running(world, state):
    launch_id = world.book()
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute("UPDATE launch SET state = ? WHERE launch_id = ?", (state, launch_id))
    finally:
        s.close()
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "wrong_state" and "not RUNNING" in env["error"]["message"]


def test_release_refuses_when_an_agent_started_and_names_the_file(world):
    launch_id = _spawn(world)
    path = _write_meta(world)
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "agent_started"
    assert str(path) in env["error"]["message"]
    assert world.launch(launch_id)["state"] == "RUNNING"


def test_release_refuses_an_old_meta_json_too_because_the_verb_has_no_time_filter(world):
    launch_id = _spawn(world)
    epoch = parse(world.launch(launch_id)["spawn_ts"]).timestamp()
    _write_meta(world, mtime=epoch - 7200)
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "agent_started"


def test_release_even_if_started_is_recorded_with_decided_by(world):
    launch_id = _spawn(world)
    _write_meta(world)
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--even-if-started")
    assert rc == 1 and env["error"]["code"] == "decided_by_required"
    rc, env = _cli(
        world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--even-if-started",
        "--decided-by", "C-0001",
    )
    assert rc == 0 and env["ok"], env
    entry = json.loads(world.launch(launch_id)["attrs"])["spawn_releases"][0]
    assert entry["even_if_started"] is True and entry["decided_by"] == "C-0001" and entry["meta_found"] is True
    assert entry["forced"] is False
    assert _events(world, "launch_spawn_released")[0]["even_if_started"] is True


def test_an_own_session_release_of_a_started_launch_needs_even_if_started_too(world):
    """--force is the ownership override; it never waives the agent_started refusal."""
    launch_id = _spawn(world)
    path = _write_meta(world)
    rc, env = _cli(
        world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--force", "--decided-by", "C-1"
    )
    assert rc == 1 and env["error"]["code"] == "agent_started" and str(path) in env["error"]["message"]
    assert world.launch(launch_id)["state"] == "RUNNING"


def test_a_cross_session_force_does_not_skip_agent_started_or_search_incomplete(world):
    started = _spawn(world)
    _write_meta(world)
    _give_to_another_session(world, started)
    rc, env = _cli(
        world, "budget", "release", "--launch-id", started, "--reason", "x", "--force", "--decided-by", "C-7"
    )
    assert rc == 1 and env["error"]["code"] == "agent_started"
    assert world.launch(started)["state"] == "RUNNING"

    # the same launch, with both flags, goes through and records both
    rc, env = _cli(
        world, "budget", "release", "--launch-id", started, "--reason", "x", "--force", "--even-if-started",
        "--decided-by", "C-7",
    )
    assert rc == 0 and env["ok"], env
    entry = json.loads(world.launch(started)["attrs"])["spawn_releases"][0]
    assert entry["forced"] is True and entry["even_if_started"] is True and entry["cross_session"] is True
    assert entry["decided_by"] == "C-7" and entry["meta_found"] is True
    payload = _events(world, "launch_spawn_released")[0]
    assert payload["forced"] is True and payload["even_if_started"] is True


def test_a_cross_session_force_does_not_skip_search_incomplete(world):
    launch_id = _spawn(world)
    world.subagents.mkdir(parents=True)
    (world.subagents / "agent-broken.meta.json").write_text("{not json", encoding="utf-8")
    _give_to_another_session(world, launch_id)
    rc, env = _cli(
        world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--force", "--decided-by", "C-7"
    )
    assert rc == 1 and env["error"]["code"] == "search_incomplete"
    rc, env = _cli(
        world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--force", "--even-if-started",
        "--decided-by", "C-7",
    )
    assert rc == 0 and world.launch(launch_id)["state"] == "PROVISIONAL"


def test_the_plain_own_session_release_of_a_never_started_launch_is_unchanged(world):
    launch_id = _spawn(world)
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "denied by a rule")
    assert rc == 0 and env["ok"], env
    entry = json.loads(world.launch(launch_id)["attrs"])["spawn_releases"][0]
    assert entry["forced"] is False and entry["even_if_started"] is False and entry["cross_session"] is False


def test_release_of_a_launch_gated_before_the_identity_columns_needs_even_if_started(world):
    launch_id = _spawn(world)
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute(
                "UPDATE launch SET spawn_tool_use_id = NULL, spawn_ts = NULL, spawn_transcript_dir = NULL "
                "WHERE launch_id = ?",
                (launch_id,),
            )
    finally:
        s.close()
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "needs_force"
    # --force is the ownership override and does not answer "did an agent start?"
    rc, env = _cli(
        world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--force", "--decided-by", "C-1"
    )
    assert rc == 1 and env["error"]["code"] == "needs_force"
    rc, env = _cli(
        world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--even-if-started",
        "--decided-by", "C-1",
    )
    assert rc == 0 and world.launch(launch_id)["state"] == "PROVISIONAL"


def test_release_refuses_when_the_search_cannot_finish(world):
    launch_id = _spawn(world)
    world.subagents.mkdir(parents=True)
    (world.subagents / "agent-broken.meta.json").write_text("{not json", encoding="utf-8")
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "search_incomplete"
    assert world.launch(launch_id)["state"] == "RUNNING"


def _age_spawn(world, launch_id, minutes):
    ts = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - minutes * 60, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute("UPDATE launch SET spawn_ts = ? WHERE launch_id = ?", (ts, launch_id))
    finally:
        s.close()


def test_status_reports_stranded_bookings_and_only_those(world):
    stranded = _spawn(world)
    _age_spawn(world, stranded, 45)

    fresh = world.book()
    assert _gate(world, world.pre_payload(fresh, tool_use_id="toolu_fresh")) == (0, None)  # under 30 minutes

    started = world.book()
    assert _gate(world, world.pre_payload(started, tool_use_id="toolu_started")) == (0, None)
    _age_spawn(world, started, 45)
    _write_meta(world, tool_use_id="toolu_started", name="agent-b2.meta.json")

    recorded = world.book()
    assert _gate(world, world.pre_payload(recorded, tool_use_id="toolu_recorded")) == (0, None)
    _age_spawn(world, recorded, 45)
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute("UPDATE launch SET agent_id = 'a9' WHERE launch_id = ?", (recorded,))
    finally:
        s.close()

    rc, env = _cli(world, "budget", "status")
    assert rc == 0 and env["ok"], env
    assert env["result"]["stranded_count"] == 1
    assert env["result"]["stranded_ids"] == [stranded]
    (warning,) = env["warnings"]
    assert warning["message"].startswith("1 bookings look stranded: their spawn never started an agent.")
    assert "trialerror budget release" in warning["message"]


def test_status_reports_zero_stranded_and_no_warning(world):
    world.book()
    rc, env = _cli(world, "budget", "status")
    assert rc == 0
    assert env["result"]["stranded_count"] == 0 and env["result"]["stranded_ids"] == []
    assert "warnings" not in env


# ---------------------------------------------------------------------------
# ownership: cancel and release follow heartbeat's rule
# ---------------------------------------------------------------------------


def _give_to_another_session(world, launch_id):
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute("UPDATE launch SET session_id = 'SESS-someone-else' WHERE launch_id = ?", (launch_id,))
    finally:
        s.close()


def test_cancel_refuses_another_sessions_launch_unless_forced_with_a_decision(world):
    launch_id = world.book()
    _give_to_another_session(world, launch_id)
    rc, env = _cli(world, "budget", "cancel", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "launch_not_owned"
    assert world.launch(launch_id)["state"] == "PROVISIONAL"
    rc, env = _cli(world, "budget", "cancel", "--launch-id", launch_id, "--reason", "x", "--force")
    assert rc == 1 and env["error"]["code"] == "decided_by_required"
    assert world.launch(launch_id)["state"] == "PROVISIONAL"
    rc, env = _cli(
        world, "budget", "cancel", "--launch-id", launch_id, "--reason", "x", "--force", "--decided-by", "C-7"
    )
    assert rc == 0 and env["ok"], env
    row = world.launch(launch_id)
    assert row["state"] == "ABANDONED"
    cancel = json.loads(row["attrs"])["cancel"]
    assert cancel["forced"] is True and cancel["decided_by"] == "C-7"
    payload = _events(world, "launch_cancelled")[0]
    assert payload["forced"] is True and payload["decided_by"] == "C-7"


def test_release_refuses_another_sessions_launch_unless_forced_with_a_decision(world):
    launch_id = _spawn(world)
    _give_to_another_session(world, launch_id)
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "launch_not_owned"
    assert world.launch(launch_id)["state"] == "RUNNING"
    rc, env = _cli(
        world, "budget", "release", "--launch-id", launch_id, "--reason", "x", "--force", "--decided-by", "C-7"
    )
    assert rc == 0 and env["ok"], env
    row = world.launch(launch_id)
    assert row["state"] == "PROVISIONAL"
    entry = json.loads(row["attrs"])["spawn_releases"][0]
    assert entry["forced"] is True and entry["decided_by"] == "C-7"


@pytest.mark.parametrize("verb", ["cancel", "release"])
def test_the_verbs_refuse_when_no_session_is_open(world, verb):
    launch_id = world.book() if verb == "cancel" else _spawn(world)
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.ops:
            s.ops.execute("UPDATE session SET status = 'closed'")
    finally:
        s.close()
    rc, env = _cli(world, "budget", verb, "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "no_open_session"


# ---------------------------------------------------------------------------
# the stranded search stays out of session open and the dashboard's panel
# ---------------------------------------------------------------------------


def _count_stranded_searches(monkeypatch):
    from trialerror.budget import pools

    calls = []
    real = pools.find_stranded_launches

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(pools, "find_stranded_launches", counting)
    return calls


def test_budget_status_searches_only_when_asked(world, monkeypatch):
    from trialerror.budget.pools import budget_status

    calls = _count_stranded_searches(monkeypatch)
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        account_id = s.ops.execute("SELECT account_id FROM session").fetchone()[0]
        plain = budget_status(s, account_id=account_id)
        assert calls == [] and "stranded_ids" not in plain
        asked = budget_status(s, account_id=account_id, include_stranded=True)
    finally:
        s.close()
    assert calls == [1] and asked["stranded_count"] == 0 and asked["stranded_ids"] == []


def test_the_cli_status_and_check_search_and_the_dashboard_and_session_open_do_not(world, monkeypatch, tmp_path):
    calls = _count_stranded_searches(monkeypatch)
    rc, env = _cli(world, "budget", "status")
    assert rc == 0 and env["result"]["stranded_count"] == 0 and calls == [1]
    rc, env = _cli(world, "budget", "check", "--quota-dir", str(tmp_path / "q"))
    assert rc == 0 and env["result"]["status"]["stranded_count"] == 0 and calls == [1, 1]

    # The dashboard's budget panel and session open call budget_status(store, account_id=..., [model_class=...])
    # with no include_stranded, so they never search.
    calls.clear()
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        account_id = s.ops.execute("SELECT account_id FROM session").fetchone()[0]
        from trialerror.budget.pools import budget_status

        budget_status(s, account_id=account_id, model_class=None)  # the shape both call
    finally:
        s.close()
    assert calls == []


# ---------------------------------------------------------------------------
# N3: every connection the hook opens waits at most 1000 ms on a lock
# ---------------------------------------------------------------------------


def test_the_hook_opens_every_database_with_a_one_second_busy_timeout(world, monkeypatch):
    import trialerror.stores.connection as connection_module
    import trialerror.stores.store as store_module

    real = connection_module.connect
    opened = []

    def recording(path, **kwargs):
        opened.append((str(path), kwargs.get("busy_timeout_ms", connection_module.DEFAULT_BUSY_TIMEOUT_MS)))
        return real(path, **kwargs)

    monkeypatch.setattr(connection_module, "connect", recording)
    monkeypatch.setattr(store_module, "connect", recording)
    launch_id = _spawn(world)
    opened.clear()  # the gate's own connections are not the hook's
    spawn_failure._evaluate(_fail_payload(world, launch_id))
    by_the_hook = list(opened)  # before the test's own reads open stores of their own
    assert world.launch(launch_id)["state"] == "PROVISIONAL"
    assert any(path.endswith("ops.db") for path, _ in by_the_hook), by_the_hook  # the event step ran
    assert _events(world, "launch_spawn_released")
    assert by_the_hook and all(timeout == 1000 for _, timeout in by_the_hook), by_the_hook


# ---------------------------------------------------------------------------
# N4: the hook's required-keys row, and release on the states it must refuse
# ---------------------------------------------------------------------------


def test_the_hook_payload_keys_probe_requires_session_id_and_tool_use_id_of_this_hook(world):
    from trialerror.probes.registry import ProbeContext
    from trialerror.units import probes as units_probes

    assert units_probes._REQUIRED_KEYS_BY_HOOK["spawn_failure"] == {"session_id", "tool_use_id"}
    launch_id = _spawn(world)
    payload = _fail_payload(world, launch_id)
    spawn_failure._evaluate(payload)  # the hook's own record, written with the real payload's keys
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        ctx = ProbeContext(host="test", platform_store=s, cc_version="2.1.280")
        assert units_probes.probe_hook_payload_keys(ctx).status == "pass"
        # a payload without tool_use_id is a finding, named under this hook
        without = {k: v for k, v in payload.items() if k != "tool_use_id"}
        from trialerror.hooks.probe_log import append_hook_record

        append_hook_record(without, hook="spawn_failure", extra={"error_class": "other"})
        result = units_probes.probe_hook_payload_keys(ctx)
    finally:
        s.close()
    assert result.status == "fail"
    assert "tool_use_id" in result.detail["missing_required_keys"]["spawn_failure"]


@pytest.mark.parametrize("state", ["DEFERRED", "REFUSED"])
def test_release_refuses_a_deferred_and_a_refused_launch(world, state):
    launch_id = world.book()
    s = open_store(world.program_root, platform_root=world.platform_root)
    try:
        with s.platform:
            s.platform.execute("UPDATE launch SET state = ? WHERE launch_id = ?", (state, launch_id))
    finally:
        s.close()
    rc, env = _cli(world, "budget", "release", "--launch-id", launch_id, "--reason", "x")
    assert rc == 1 and env["error"]["code"] == "wrong_state" and "not RUNNING" in env["error"]["message"]
    assert world.launch(launch_id)["state"] == state


# ---------------------------------------------------------------------------
# N6: the hook's own failure leaves one stderr line, and never changes the exit code
# ---------------------------------------------------------------------------


def test_a_store_one_version_behind_gives_exit_zero_and_exactly_one_stderr_line(
    world, capsys, monkeypatch, tmp_path
):
    import sqlite3

    from trialerror.stores.migrate import apply_migrations
    from trialerror.stores.schema import platform as platform_schema

    behind_root = tmp_path / "platform_one_version_behind"
    behind_root.mkdir()
    conn = sqlite3.connect(str(behind_root / "platform.db"))
    apply_migrations(conn, tuple(m for m in platform_schema.MIGRATIONS if m.version <= 4))
    conn.close()
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(behind_root))

    payload = _fail_payload(world, "L-any", error=BOGUS_ERROR + " " + MARKER)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert spawn_failure.main() == 0
    out = capsys.readouterr()
    assert out.out == ""
    lines = out.err.splitlines()
    assert len(lines) == 1, out.err
    assert lines[0].startswith("spawn_failure: ") and "OperationalError" in lines[0]
    assert MARKER not in out.err and BOGUS_ERROR not in out.err  # the type only, never message text
