"""End-to-end tests of ``plugin/hooks/spawn_gate.py`` AS A SCRIPT — real
subprocess, real stdin JSON, real exit code, exactly the interface Claude
Code's PreToolUse hook protocol uses. ``trialerror.budget.gate`` (imported and
called directly, no subprocess) already covers the decision logic
exhaustively; this file's job is narrower and different: prove the stdin
JSON -> exit-code plumbing this specific script does is correct, since
design Section 12 M3 row explicitly calls out that "live-CC hook tests are
orchestrator-executed integration items" — this is the closest a
non-live-Claude-Code pytest run can get to that same round trip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.budget.pools import book_launch
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "plugin" / "hooks" / "spawn_gate.py"


def _run_hook(payload: dict, *, platform_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    # A script subprocess puts the SCRIPT's directory on sys.path, never the
    # caller's cwd, so `import trialerror` would otherwise resolve through
    # whatever editable install the interpreter happens to carry -- which
    # can be a different checkout entirely. Pin it to the tree this test
    # file lives in, or the test silently exercises someone else's code.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@pytest.fixture()
def roots(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    return platform_root, program_root


@pytest.fixture()
def booked(roots):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    result = book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=100,
    )
    store.close()
    return result.launch_id


def test_hook_passes_through_non_task_tools(roots):
    platform_root, program_root = roots
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Task->Agent rename (found 2026-09-05, C-0064-era live evidence): Claude
# Code 2.1.x invokes the subagent tool as "Agent", not "Task". The gate
# must treat both names identically -- these mirror the "Task" tests above
# with ``tool_name: "Agent"`` substituted in, one per booking-lifecycle
# state so the fix is proven at each of the same points the rename broke.
# ---------------------------------------------------------------------------


def test_hook_refuses_agent_with_no_launch_id_token(roots):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    insert(
        store, "session",
        {"session_id": new_id("SESS"), "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": "go do research, no ids anywhere"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "SPAWN REFUSED" in proc.stderr
    assert "no_launch_id_token" in proc.stderr


def test_hook_allows_booked_agent_and_consumes_the_token(roots, booked):
    """The exact live-evidence shape: a subagent spawn issued as the
    renamed ``Agent`` tool with a booked launch_id must be gated (and
    consumed) exactly like ``Task`` was -- this is the case that regressed
    silently (spawn went through with no booking check at all)."""
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    from trialerror.stores import get

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    # FU-11 verification findings F3/FU11-V6: the absent hook_alive{hook=
    # "spawn_gate"} row was HALF the live incident's own signature (the
    # other half being the missing booking check proven above) -- assert it
    # explicitly for the Agent-named payload, not just transitively via the
    # exit code/launch-state assertions.
    hook_alive_rows = store.ops.execute(
        "SELECT payload FROM event WHERE type='hook_alive'"
    ).fetchall()
    store.close()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in hook_alive_rows]
    assert "spawn_gate" in hooks_seen, f"no hook_alive{{hook='spawn_gate'}} row, got {hooks_seen!r}"
    assert row["state"] == "RUNNING"


def test_hook_refuses_the_same_token_reused_on_a_second_agent_spawn(roots, booked):
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }

    first = _run_hook(payload, platform_root=platform_root)
    assert first.returncode == 0, first.stderr

    second = _run_hook(payload, platform_root=platform_root)
    assert second.returncode == 2
    assert "SPAWN REFUSED" in second.stderr
    assert "token_not_provisional" in second.stderr


def test_hook_refuses_task_with_no_launch_id_token(roots):
    platform_root, program_root = roots
    # An open session must exist first -- otherwise the gate refuses on
    # "no_open_session" before it ever gets to look for a token (correctly:
    # no session means no spawn regardless of what the prompt says).
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    insert(
        store, "session",
        {"session_id": new_id("SESS"), "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": "go do research, no ids anywhere"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "SPAWN REFUSED" in proc.stderr
    assert "no_launch_id_token" in proc.stderr


def test_hook_refuses_task_with_no_open_session_at_all(roots):
    platform_root, program_root = roots
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": "launch_id: " + new_id("LNCH")},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "no_open_session" in proc.stderr


def test_hook_allows_booked_task_and_consumes_the_token(roots, booked):
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    from trialerror.stores import get

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    store.close()
    assert row["state"] == "RUNNING"


def test_hook_refuses_the_same_token_reused_on_a_second_spawn(roots, booked):
    """Adversarial token-reuse case, exercised through the actual script a
    live Claude Code session would invoke."""
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }

    first = _run_hook(payload, platform_root=platform_root)
    assert first.returncode == 0, first.stderr

    second = _run_hook(payload, platform_root=platform_root)
    assert second.returncode == 2
    assert "SPAWN REFUSED" in second.stderr
    assert "token_not_provisional" in second.stderr


def test_hook_records_a_spawn_gate_hook_alive_marker_distinct_from_session_start(roots, booked):
    """FX-8 (C-0064 lens B EP-1 Bypass C): the spawn gate must leave its own
    ``payload.hook == "spawn_gate"`` liveness marker, distinct from
    ``session_start.py``'s ``"session_start"`` value -- this is the marker
    ``close_session``'s ``hooks_partial`` check looks for. Recorded even
    when the gate itself REFUSES (a no-token Task call), same "the hook
    fired" posture as session_start.py."""
    platform_root, program_root = roots
    launch_id = booked

    refused = _run_hook(
        {
            "hook_event_name": "PreToolUse", "tool_name": "Task",
            "tool_input": {"prompt": "no launch_id token here at all"},
            "cwd": str(program_root),
        },
        platform_root=platform_root,
    )
    assert refused.returncode == 2

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'hook_alive'").fetchall()
    store.close()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in rows]
    assert hooks_seen == ["spawn_gate"], f"expected exactly one spawn_gate marker, got {hooks_seen!r}"

    # A second Task call (this time consuming the real booking) must NOT
    # add a second marker -- first-fire-per-session only.
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Task",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    consumed = _run_hook(payload, platform_root=platform_root)
    assert consumed.returncode == 0, consumed.stderr

    store = open_store(program_root, platform_root=platform_root)
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'hook_alive'").fetchall()
    store.close()
    hooks_seen = [json.loads(r["payload"])["hook"] for r in rows]
    assert hooks_seen == ["spawn_gate"], f"expected still exactly one marker (de-duped), got {hooks_seen!r}"


# ---------------------------------------------------------------------------
# FU-11 verification finding FU11-V5: the tool_name rename (Task -> Agent)
# proved an assumption about Claude Code's tool surface can go stale
# unnoticed; the sibling assumption -- that the launch_id-bearing prompt
# text lives under tool_input["prompt"]/["description"] -- was never
# checked. These prove the cheap fallback (scan the whole serialized
# tool_input) actually finds a launch_id token stashed under some other
# key, rather than only asserting it by reading the source.
# ---------------------------------------------------------------------------


def test_hook_finds_launch_id_token_under_an_unexpected_tool_input_key(roots, booked):
    """If a future Claude Code tool-input schema change moves the prompt
    text off ``prompt``/``description`` (unverified sibling assumption to
    the Task->Agent rename), the gate must still find a `launch_id:` token
    embedded in the tool_input rather than refusing every real spawn."""
    platform_root, program_root = roots
    launch_id = booked
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"instructions": f"you are a lens. launch_id: {launch_id}"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr

    store = open_store(program_root, platform_root=platform_root)
    from trialerror.stores import get

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    store.close()
    assert row["state"] == "RUNNING"


def test_hook_refuses_agent_when_tool_input_has_no_token_anywhere(roots):
    """The fallback must not manufacture a false match: an ``Agent`` call
    whose tool_input carries fields but no `launch_id:` token anywhere
    still refuses as ``no_launch_id_token``."""
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    insert(
        store, "session",
        {"session_id": new_id("SESS"), "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"instructions": "go do research, no ids anywhere"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "no_launch_id_token" in proc.stderr


def test_hook_unparseable_stdin_passes_through(roots):
    platform_root, program_root = roots
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not json at all {{{",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# agent_model_matches_booking: the hook's half of the guard is deciding WHAT
# model this spawn names -- the Task call's own `model`, or, failing that,
# the model the named subagent definition file pins. Both routes have to
# work, or "just leave model out of the call" walks around the guard.
# ---------------------------------------------------------------------------


def _top_booked(roots) -> str:
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    result = book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens",
        model_class="top", model="opus", purpose="ideation", est_tokens=1000,
    )
    store.close()
    return result.launch_id


def test_hook_refuses_a_top_booking_spawned_with_a_cheap_model(roots):
    platform_root, program_root = roots
    launch_id = _top_booked(roots)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}", "model": "haiku"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "agent_model_below_booking" in proc.stderr


def test_hook_allows_a_top_booking_spawned_with_a_top_model(roots):
    platform_root, program_root = roots
    launch_id = _top_booked(roots)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": f"you are a lens. launch_id: {launch_id}", "model": "opus"},
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr


def test_hook_falls_back_to_the_agent_files_frontmatter_model(roots):
    """No `model` on the call -- the agent file's pin is what will actually
    be used, so it is what the guard has to read."""
    platform_root, program_root = roots
    (program_root / "plugin" / "agents").mkdir(parents=True)
    (program_root / "plugin" / "agents" / "cheapskate.md").write_text(
        "---\nname: cheapskate\ndescription: a test agent\nmodel: haiku\n---\n\n# Cheapskate\n",
        encoding="utf-8",
    )
    launch_id = _top_booked(roots)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": f"you are a lens. launch_id: {launch_id}",
            "subagent_type": "cheapskate",
        },
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 2
    assert "agent_model_below_booking" in proc.stderr
    assert "haiku" in proc.stderr


def test_hook_allows_when_neither_the_call_nor_any_agent_file_names_a_model(roots):
    platform_root, program_root = roots
    launch_id = _top_booked(roots)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": f"you are a lens. launch_id: {launch_id}",
            "subagent_type": "no-such-agent-anywhere",
        },
        "cwd": str(program_root),
    }
    proc = _run_hook(payload, platform_root=platform_root)
    assert proc.returncode == 0, proc.stderr


def test_frontmatter_model_reader_is_path_safe_and_absence_tolerant(tmp_path):
    from trialerror.hooks.spawn_gate import _frontmatter_model

    agents = tmp_path / "plugin" / "agents"
    agents.mkdir(parents=True)
    (agents / "reader.md").write_text("---\nname: reader\nmodel: 'opus'\n---\n", encoding="utf-8")
    assert _frontmatter_model("reader", tmp_path) == "opus"
    assert _frontmatter_model("missing", tmp_path) is None
    assert _frontmatter_model(None, tmp_path) is None
    # A traversal-shaped subagent_type never becomes a filesystem read.
    assert _frontmatter_model("../../etc/passwd", tmp_path) is None
    assert _frontmatter_model(".hidden", tmp_path) is None


def test_frontmatter_model_strips_this_plugins_own_namespace_prefix(tmp_path):
    """N1 (review finding): under ``--plugin-dir``, every plugin agent's
    real ``subagent_type`` is namespaced ``<plugin>:<agent>`` -- the bare
    name is refused outright by Claude Code itself -- so the qualified
    form is the ONLY one a real spawn ever names. Before this fix,
    ``_frontmatter_model`` joined the qualified string onto ``_AGENT_DIRS``
    verbatim (looking for a file literally named
    ``trialerror:myagent.md``), which never exists, so the guard silently
    made no claim for every real plugin-qualified spawn.

    The prefix stripped is THIS plugin's own name -- ``trialerror:``, read
    live from ``plugin/.claude-plugin/plugin.json`` (see
    :func:`test_frontmatter_model_strips_only_this_plugins_own_namespace`
    below for why it is not any prefix at all)."""
    from trialerror.hooks.spawn_gate import _frontmatter_model

    agents = tmp_path / "plugin" / "agents"
    agents.mkdir(parents=True)
    (agents / "myagent.md").write_text("---\nname: myagent\nmodel: opus\n---\n", encoding="utf-8")
    assert _frontmatter_model("trialerror:myagent", tmp_path) == "opus"
    # A qualified name for a file that genuinely doesn't exist still makes no claim.
    assert _frontmatter_model("trialerror:no-such-agent", tmp_path) is None
    # A traversal-shaped agent half of the name is still never a filesystem read.
    assert _frontmatter_model("trialerror:../../etc/passwd", tmp_path) is None
    assert _frontmatter_model("trialerror:.hidden", tmp_path) is None
    # The prefix with nothing after it names no agent at all.
    assert _frontmatter_model("trialerror:", tmp_path) is None


def test_frontmatter_model_resolves_the_real_shipped_agents_by_qualified_name():
    """N1, against the real repo rather than a synthetic fixture -- exactly
    what the review checked directly: ``trialerror:prompt-only`` and
    ``trialerror:critic`` are the actual ``subagent_type`` strings a real
    ``--plugin-dir`` session spawns, and both must resolve to the real
    files' pinned ``model: opus`` (the module's own fallback root, when
    ``program_root`` has no ``plugin/agents`` of its own, is this repo's
    root). A namespaced name for an agent this plugin does not ship stays
    unknown -- namespacing must never manufacture a claim from nothing."""
    from trialerror.hooks.spawn_gate import _frontmatter_model

    unrelated_root = REPO_ROOT / "docs"  # has no plugin/agents of its own
    assert _frontmatter_model("trialerror:prompt-only", unrelated_root) == "opus"
    assert _frontmatter_model("trialerror:critic", unrelated_root) == "opus"
    assert _frontmatter_model("trialerror:no-such-agent", unrelated_root) is None


def test_frontmatter_model_strips_only_this_plugins_own_namespace():
    """new-N1 (second review pass): the first fix stripped up to the LAST
    ``:`` unconditionally, so a name that merely LOOKS namespaced --
    another loaded plugin's agent sharing a bare name with one of ours, a
    bare drive letter, a multi-colon name -- resolved against THIS
    plugin's own file and made a claim about a spawn that has nothing to
    do with it. Checked directly by the review: ``otherplugin:critic``,
    ``C:critic`` and ``a:b:critic`` all resolved to ``'opus'`` (this
    plugin's real ``critic.md``) before this fix. None of them may make
    ANY claim now -- only this plugin's own ``trialerror:`` prefix
    (read from its manifest) is ever stripped."""
    from trialerror.hooks.spawn_gate import _frontmatter_model

    unrelated_root = REPO_ROOT / "docs"  # has no plugin/agents of its own
    assert _frontmatter_model("otherplugin:critic", unrelated_root) is None
    assert _frontmatter_model("C:critic", unrelated_root) is None
    assert _frontmatter_model("a:b:critic", unrelated_root) is None
    # This plugin's own qualified name is unaffected by the narrowing.
    assert _frontmatter_model("trialerror:critic", unrelated_root) == "opus"


def test_only_the_frontmatter_block_can_pin_a_model(tmp_path):
    """Finding V-8: the pin is read from the ``---`` block, never from the
    body. A prose line beginning ``model:`` used to be read as the file's
    pin, and the direction that bites is a FALSE REFUSAL -- an agent file
    that merely discusses ``model: haiku`` refusing a correct top spawn."""
    from trialerror.hooks.spawn_gate import _frontmatter_model

    agents = tmp_path / "plugin" / "agents"
    agents.mkdir(parents=True)

    (agents / "prose.md").write_text(
        "---\nname: prose\ndescription: an agent\n---\n\n# Prose\n\n"
        "Your spawn prompt states the model, e.g.\n\nmodel: haiku\n",
        encoding="utf-8",
    )
    assert _frontmatter_model("prose", tmp_path) is None

    # No frontmatter at all: a body line is still not a pin.
    (agents / "bare.md").write_text("# Bare\n\nmodel: opus\n", encoding="utf-8")
    assert _frontmatter_model("bare", tmp_path) is None

    # An unterminated fence is not a frontmatter block either.
    (agents / "unclosed.md").write_text("---\nname: unclosed\nmodel: opus\n", encoding="utf-8")
    assert _frontmatter_model("unclosed", tmp_path) is None

    # The real shape still reads, body noise and all.
    (agents / "real.md").write_text(
        "---\nname: real\nmodel: fable\n---\n\n# Real\n\nmodel: haiku appears here as prose.\n",
        encoding="utf-8",
    )
    assert _frontmatter_model("real", tmp_path) == "fable"


def _plugin_root(tmp_path, name, agents):
    """A plugin folder as Claude Code loads it: a manifest and an agents/ folder."""
    root = tmp_path / f"loaded_{name}"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (root / "agents").mkdir()
    for agent, model in agents.items():
        (root / "agents" / f"{agent}.md").write_text(f"---\nname: {agent}\nmodel: {model}\n---\n", encoding="utf-8")
    return root


def test_frontmatter_model_reads_the_plugin_root_the_host_exports(tmp_path, monkeypatch):
    """A6: ``CLAUDE_PLUGIN_ROOT`` names the plugin whose hook is running. Its
    manifest gives the plugin's name, and its own ``agents/`` folder is
    searched first, before the repository's roots."""
    from trialerror.hooks.spawn_gate import _frontmatter_model

    root = _plugin_root(tmp_path, "trialerror", {"critic": "haiku"})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    unrelated = REPO_ROOT / "docs"
    # The loaded copy wins over the repository's own critic.md (which pins opus).
    assert _frontmatter_model("trialerror:critic", unrelated) == "haiku"
    # An agent only the repository ships is still found afterwards.
    assert _frontmatter_model("trialerror:prompt-only", unrelated) == "opus"
    # Another plugin's name, and the bare-prefix and traversal shapes, make no claim.
    assert _frontmatter_model("other:critic", unrelated) is None
    assert _frontmatter_model("trialerror:", unrelated) is None
    assert _frontmatter_model("trialerror:../x", unrelated) is None
    assert _frontmatter_model("trialerror:a:b", unrelated) is None


def test_frontmatter_model_takes_the_plugin_name_from_the_exported_root(tmp_path, monkeypatch):
    """When the exported root's manifest names a different plugin, that name
    is the one whose qualified agents are recognised -- not the repository's."""
    from trialerror.hooks.spawn_gate import _frontmatter_model

    root = _plugin_root(tmp_path, "loadedplug", {"scout": "sonnet"})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    unrelated = REPO_ROOT / "docs"
    assert _frontmatter_model("loadedplug:scout", unrelated) == "sonnet"
    assert _frontmatter_model("trialerror:critic", unrelated) is None


def test_frontmatter_model_falls_back_to_the_repository_when_the_root_is_unusable(tmp_path, monkeypatch):
    from trialerror.hooks.spawn_gate import _frontmatter_model

    unrelated = REPO_ROOT / "docs"
    for bad in (tmp_path / "does-not-exist", tmp_path):
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(bad))
        assert _frontmatter_model("trialerror:critic", unrelated) == "opus"
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    assert _frontmatter_model("trialerror:critic", unrelated) == "opus"


def test_the_plugin_name_is_read_once_per_manifest(tmp_path, monkeypatch):
    from trialerror.hooks import spawn_gate

    root = _plugin_root(tmp_path, "cachedplug", {})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    manifest = root / ".claude-plugin" / "plugin.json"
    assert spawn_gate._this_plugins_namespace_prefix() == "cachedplug:"
    manifest.write_text(json.dumps({"name": "renamed"}), encoding="utf-8")
    assert spawn_gate._this_plugins_namespace_prefix() == "cachedplug:"


def test_n9_program_root_is_harness_passes_the_spawn_through_instead_of_failing_closed(tmp_path, monkeypatch):
    """N9 (fix check): a ProgramRootIsHarnessError from find_program_root()
    must not reach main()'s own catch-all, which fails CLOSED ("an
    unexpected bug ... must not fail OPEN") -- every subagent spawn would
    then be refused for a session whose cwd happens to be the harness
    checkout. Treated here as "no program root": the spawn passes through,
    with a stderr note."""
    from trialerror.hooks import spawn_gate
    import trialerror.util.config as config_mod

    repo = tmp_path / "fake_checkout"
    (repo / "trialerror").mkdir(parents=True)
    (repo / "trialerror" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "trialerror.toml").write_text('[program]\nid = "fake"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "_HARNESS_PACKAGE_PARENT", repo)

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": "go do research"},
        "cwd": str(repo),
    }
    code, message = spawn_gate._evaluate(payload)

    assert code == 0  # passed through, not refused
    assert message is not None
    assert "no program root was given" in message  # the underlying ProgramRootIsHarnessError's text
    assert "passing the spawn through" in message
