"""``trialerror.units.probes`` -- the six conformance probes (design Section
3.3). Fixtures built inline from the observed shapes in design Section 1
(hook_events.jsonl lines, unit rows), never from summaries.
"""

from __future__ import annotations

import json

import pytest

from trialerror.hooks.probe_log import append_hook_record, hook_events_path
from trialerror.probes.registry import ProbeContext
from trialerror.stores import insert
from trialerror.stores.connection import connect
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform as platform_schema
from trialerror.stores.store import Store
from trialerror.units import probes as units_probes
from trialerror.util.timeutil import now

HOST = "test-host"


def _memory_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def store(tmp_path):
    conn = connect(tmp_path / "platform.db", check_same_thread=False)
    apply_migrations(conn, platform_schema.MIGRATIONS)
    s = Store(platform=conn, ops=_memory_conn(), knowledge=_memory_conn(), jobs=_memory_conn(),
              program_root=tmp_path, platform_root=tmp_path)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def probes_dir(tmp_path, monkeypatch):
    d = tmp_path / "probes"
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(d))
    return d


def _write_unit(store, **overrides):
    row = {
        "unit_key": overrides.pop("unit_key"),
        "host": HOST,
        "kind": "subagent",
        "session_id": "SESS-1",
        "project_slug": "proj",
        "usage_source": "transcript",
        "extractor_version": "units-1",
        "scanned_ts": now(),
    }
    row.update(overrides)
    insert(store, "unit", row)


# ---------------------------------------------------------------------------
# cc_version_seen
# ---------------------------------------------------------------------------


def test_cc_version_seen_passes_with_no_prior_recording(store):
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version="2.1.280")
    result = units_probes.probe_cc_version_seen(ctx)
    assert result.status == "pass"
    assert result.detail["previous"] is None


def test_cc_version_seen_warns_on_a_change(store):
    insert(store, "probe_run", {
        "name": "cc_version_seen", "kind": "conformance", "host": HOST, "started_ts": now(),
        "status": "pass", "detail": json.dumps({"current": "2.1.270"}),
    })
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version="2.1.280")
    result = units_probes.probe_cc_version_seen(ctx)
    assert result.status == "warn"
    assert "probes run --kind conformance --live" in result.detail["message"]


def test_cc_version_seen_skips_when_unavailable(store):
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version=None)
    result = units_probes.probe_cc_version_seen(ctx)
    assert result.status == "skip"


# ---------------------------------------------------------------------------
# hook_payload_keys
# ---------------------------------------------------------------------------


def test_hook_payload_keys_skips_with_no_events(store):
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version="2.1.280")
    assert units_probes.probe_hook_payload_keys(ctx).status == "skip"


def test_hook_payload_keys_passes_when_every_required_key_is_present(store):
    append_hook_record({"session_id": "S", "cc_version": "2.1.280"}, hook="session_start")
    append_hook_record({"session_id": "S", "tool_use_id": "T", "cc_version": "2.1.280"}, hook="spawn_gate")
    append_hook_record({"session_id": "S", "tool_use_id": "T", "cc_version": "2.1.280"}, hook="post_task")
    append_hook_record({"session_id": "S", "agent_id": "A", "cc_version": "2.1.280"}, hook="subagent_start")
    append_hook_record(
        {"session_id": "S", "agent_id": "A", "agent_transcript_path": "/x", "cc_version": "2.1.280"},
        hook="subagent_stop",
    )
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version="2.1.280")
    result = units_probes.probe_hook_payload_keys(ctx)
    assert result.status == "pass"


def test_hook_payload_keys_fails_on_a_missing_tool_use_id(store):
    append_hook_record({"session_id": "S", "cc_version": "2.1.280"}, hook="spawn_gate")  # no tool_use_id
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version="2.1.280")
    result = units_probes.probe_hook_payload_keys(ctx)
    assert result.status == "fail"
    assert "tool_use_id" in result.detail["missing_required_keys"]["spawn_gate"]


def test_hook_payload_keys_warns_on_a_key_set_drift_between_versions(store):
    append_hook_record({"session_id": "S", "cc_version": "2.1.270"}, hook="session_start")
    append_hook_record({"session_id": "S", "extra_new_field": 1, "cc_version": "2.1.280"}, hook="session_start")
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version="2.1.280")
    result = units_probes.probe_hook_payload_keys(ctx)
    assert result.status == "warn"
    assert "extra_new_field" in result.detail["key_set_diffs"]["session_start"]["added"]


def test_hook_payload_keys_never_carries_message_text(store):
    append_hook_record(
        {"session_id": "S", "tool_use_id": "T", "prompt": "definitely secret text", "cc_version": "2.1.280"},
        hook="spawn_gate",
    )
    ctx = ProbeContext(host=HOST, platform_store=store, cc_version="2.1.280")
    result = units_probes.probe_hook_payload_keys(ctx)
    assert "definitely secret text" not in json.dumps(result.detail)


# ---------------------------------------------------------------------------
# subagent_stop_fires / subagent_transcript_written
# ---------------------------------------------------------------------------


def test_subagent_stop_fires_skips_with_no_recent_subagents(store):
    ctx = ProbeContext(host=HOST, platform_store=store)
    assert units_probes.probe_subagent_stop_fires(ctx).status == "skip"


def test_subagent_stop_fires_warns_below_full_match(store):
    _write_unit(store, unit_key=f"{HOST}/S/a1", agent_id="a1", agent_type="general-purpose", first_ts=now())
    _write_unit(store, unit_key=f"{HOST}/S/a2", agent_id="a2", agent_type="lens", first_ts=now())
    append_hook_record({"session_id": "S", "agent_id": "a1"}, hook="subagent_stop")
    ctx = ProbeContext(host=HOST, platform_store=store)
    result = units_probes.probe_subagent_stop_fires(ctx)
    assert result.status == "warn"
    assert result.detail["matched"] == 1
    assert result.detail["total"] == 2
    assert result.detail["unmatched_by_agent_type"] == {"lens": 1}


def test_subagent_stop_fires_passes_when_fully_matched(store):
    _write_unit(store, unit_key=f"{HOST}/S/a1", agent_id="a1", first_ts=now())
    append_hook_record({"session_id": "S", "agent_id": "a1"}, hook="subagent_stop")
    ctx = ProbeContext(host=HOST, platform_store=store)
    assert units_probes.probe_subagent_stop_fires(ctx).status == "pass"


def test_subagent_transcript_written_warns_on_a_missing_transcript(store):
    _write_unit(store, unit_key=f"{HOST}/S/a1", agent_id="a1", usage_source="none", first_ts=now())
    append_hook_record({"session_id": "S", "agent_id": "a1", "agent_transcript_path_exists": False}, hook="subagent_stop")
    ctx = ProbeContext(host=HOST, platform_store=store)
    result = units_probes.probe_subagent_transcript_written(ctx)
    assert result.status == "warn"
    assert result.detail["missing"] == 1
    assert result.detail["missing_by_project"] == {"proj": 1}


def test_subagent_transcript_written_passes_when_jsonl_exists(store):
    _write_unit(store, unit_key=f"{HOST}/S/a1", agent_id="a1", usage_source="transcript", first_ts=now())
    ctx = ProbeContext(host=HOST, platform_store=store)
    assert units_probes.probe_subagent_transcript_written(ctx).status == "pass"


# ---------------------------------------------------------------------------
# main_transcript_written
# ---------------------------------------------------------------------------


def test_main_transcript_written_fails_on_the_remote_control_pattern(store):
    insert(store, "unit", {
        "unit_key": f"{HOST}/SESS-RC/-", "host": HOST, "kind": "main", "session_id": "SESS-RC",
        "project_slug": "proj", "usage_source": "transcript_partial", "extractor_version": "units-1",
        "scanned_ts": now(),
    })
    ctx = ProbeContext(host=HOST, platform_store=store)
    result = units_probes.probe_main_transcript_written(ctx)
    assert result.status == "fail"
    assert "SESS-RC" in result.detail["remote_control_sessions"]


def test_main_transcript_written_passes_when_clean(store):
    insert(store, "unit", {
        "unit_key": f"{HOST}/SESS-OK/-", "host": HOST, "kind": "main", "session_id": "SESS-OK",
        "project_slug": "proj", "usage_source": "transcript", "extractor_version": "units-1",
        "scanned_ts": now(),
    })
    ctx = ProbeContext(host=HOST, platform_store=store)
    assert units_probes.probe_main_transcript_written(ctx).status == "pass"


def test_main_transcript_written_skips_with_no_main_units(store):
    ctx = ProbeContext(host=HOST, platform_store=store)
    assert units_probes.probe_main_transcript_written(ctx).status == "skip"


# ---------------------------------------------------------------------------
# usage_final_line
# ---------------------------------------------------------------------------


def test_usage_final_line_always_passes_and_reports_the_share(store, tmp_path):
    transcript = tmp_path / "agent-x.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"id":"m1","model":"x","usage":{"output_tokens":5}}}\n'
        '{"type":"assistant","message":{"id":"m1","model":"x","stop_reason":"end_turn","usage":{"output_tokens":50}}}\n'
        '{"type":"assistant","message":{"id":"m2","model":"x","usage":{"output_tokens":30}}}\n'
        '{"type":"assistant","message":{"id":"m2","model":"x","stop_reason":"end_turn","usage":{"output_tokens":10}}}\n'
        # N-1 fix round: a message whose first and stop_reason values are
        # EQUAL must still be counted (the old filter dropped any pair that
        # didn't differ, which made the reported share 1.0/0.0 by
        # construction regardless of what the transcripts actually showed).
        '{"type":"assistant","message":{"id":"m3","model":"x","usage":{"output_tokens":20}}}\n'
        '{"type":"assistant","message":{"id":"m3","model":"x","stop_reason":"end_turn","usage":{"output_tokens":20}}}\n',
        encoding="utf-8",
    )
    insert(store, "unit", {
        "unit_key": f"{HOST}/S/a1", "host": HOST, "kind": "subagent", "session_id": "S", "agent_id": "a1",
        "project_slug": "proj", "usage_source": "transcript", "transcript_path": str(transcript),
        "extractor_version": "units-1", "scanned_ts": now(),
    })
    ctx = ProbeContext(host=HOST, platform_store=store)
    result = units_probes.probe_usage_final_line(ctx)
    assert result.status == "pass"
    assert result.detail["messages_checked"] == 3
    assert result.detail["share_first_below_final"] == round(1 / 3, 3)


def test_usage_final_line_passes_with_no_subagent_units(store):
    ctx = ProbeContext(host=HOST, platform_store=store)
    result = units_probes.probe_usage_final_line(ctx)
    assert result.status == "pass"
    assert result.detail["messages_checked"] == 0
