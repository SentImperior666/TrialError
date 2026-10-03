"""``trialerror.units.paths.classify`` -- path-only classification of a file
under one project slug directory (design Section 2.2)."""

from __future__ import annotations

from trialerror.units.paths import FileKind, classify


def test_main_jsonl_at_depth_one():
    c = classify("SESS-1.jsonl")
    assert c.kind == FileKind.MAIN
    assert c.session_id == "SESS-1"
    assert c.agent_id is None


def test_subagent_jsonl():
    c = classify("SESS-1/subagents/agent-abc123.jsonl")
    assert c.kind == FileKind.SUBAGENT_JSONL
    assert c.session_id == "SESS-1"
    assert c.agent_id == "abc123"


def test_subagent_meta():
    c = classify("SESS-1/subagents/agent-abc123.meta.json")
    assert c.kind == FileKind.SUBAGENT_META
    assert c.agent_id == "abc123"


def test_workflow_agent_meta():
    c = classify("SESS-1/subagents/workflows/wf_9/agent-def456.meta.json")
    assert c.kind == FileKind.WORKFLOW_AGENT_META
    assert c.session_id == "SESS-1"
    assert c.agent_id == "def456"
    assert c.workflow_run_id == "wf_9"


def test_workflow_journal():
    c = classify("SESS-1/subagents/workflows/wf_9/journal.jsonl")
    assert c.kind == FileKind.WORKFLOW_JOURNAL
    assert c.workflow_run_id == "wf_9"
    assert c.agent_id is None


def test_workflow_manifest_is_ignored():
    assert classify("SESS-1/workflows/wf_9.json") is None


def test_tool_results_is_ignored():
    assert classify("SESS-1/tool-results/foo.json") is None


def test_backslash_paths_are_handled():
    c = classify("SESS-1\\subagents\\agent-abc123.jsonl")
    assert c.kind == FileKind.SUBAGENT_JSONL
    assert c.agent_id == "abc123"


def test_a_stray_non_jsonl_top_level_file_is_ignored():
    assert classify("README.md") is None


# ---------------------------------------------------------------------------
# S-5 fix round: the agent id is the WHOLE stem after "agent-", and a
# subagent file must never collapse to agent_id=None (the main unit's key).
# ---------------------------------------------------------------------------


def test_two_agent_ids_that_share_a_hex_prefix_do_not_collide():
    a = classify("SESS-1/subagents/agent-acompact-7f3e2a.jsonl")
    b = classify("SESS-1/subagents/agent-acompact-9d1c44.jsonl")
    assert a.agent_id == "acompact-7f3e2a"
    assert b.agent_id == "acompact-9d1c44"
    assert a.agent_id != b.agent_id


def test_an_id_with_no_leading_hex_run_is_never_dropped_to_none():
    c = classify("SESS-1/subagents/agent-xyz.jsonl")
    assert c is not None
    assert c.kind == FileKind.SUBAGENT_JSONL
    assert c.agent_id == "xyz"


def test_a_non_hex_id_is_preserved_for_meta_json_too():
    c = classify("SESS-1/subagents/agent-xyz.meta.json")
    assert c.agent_id == "xyz"


def test_a_non_hex_id_is_preserved_for_workflow_agent_meta_too():
    c = classify("SESS-1/subagents/workflows/wf_9/agent-xyz.meta.json")
    assert c.agent_id == "xyz"


def test_a_subagent_filename_with_no_agent_prefix_is_ignored_not_none():
    """Never a Classified with agent_id=None for a subagent file -- that
    would collide with the SESSION's own main unit key
    (<host>/<session>/-). Ignored (classify() returns None) instead."""
    assert classify("SESS-1/subagents/not-an-agent-file.jsonl") is None
    assert classify("SESS-1/subagents/not-an-agent-file.meta.json") is None
    assert classify("SESS-1/subagents/workflows/wf_9/not-an-agent-file.meta.json") is None


def test_an_empty_agent_id_is_ignored_not_none():
    assert classify("SESS-1/subagents/agent-.jsonl") is None
    assert classify("SESS-1/subagents/agent-.meta.json") is None
