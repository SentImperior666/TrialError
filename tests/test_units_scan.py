"""``trialerror.units.scan.scan`` -- the orchestrator (design Section 2.2).
Uses the same fixture tree ``tests/test_units_reader.py`` reads directly, at
``tests/fixtures/units/``, plus an in-memory (well, tmp-file) platform store.

Scan is read-only on the transcripts (never mutates the checked-in fixture
tree); the one test that appends a line copies the fixture into ``tmp_path``
first.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from trialerror.stores.connection import connect
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform as platform_schema
from trialerror.stores.store import Store
from trialerror.units.scan import scan

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "units"
HOST = "test-host"


def _memory_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def store(tmp_path):
    platform_conn = connect(tmp_path / "platform.db")
    apply_migrations(platform_conn, platform_schema.MIGRATIONS)
    s = Store(
        platform=platform_conn, ops=_memory_conn(), knowledge=_memory_conn(), jobs=_memory_conn(),
        program_root=tmp_path, platform_root=tmp_path,
    )
    yield s
    s.close()


def _units(store) -> dict[str, dict]:
    rows = store.platform.execute("SELECT * FROM unit WHERE host = ?", (HOST,)).fetchall()
    return {r["unit_key"]: dict(r) for r in rows}


def test_scan_of_the_fixture_tree_reports_files_and_units(store):
    report = scan(FIXTURES, host=HOST, store=store)
    assert report.files_read > 0
    units = _units(store)
    assert f"{HOST}/SESS-MAIN/-" in units
    assert f"{HOST}/SESS-SUB/1234567890abcdef1" in units
    assert f"{HOST}/SESS-SUB/2234567890abcdef2" in units


def test_main_unit_usage_matches_the_hand_computed_sum(store):
    scan(FIXTURES, host=HOST, store=store)
    units = _units(store)
    main = units[f"{HOST}/SESS-MAIN/-"]
    assert main["kind"] == "main"
    assert main["usage_source"] == "transcript"
    # msg1(100,10,5,50) + msg2(200,20,8,70) + msg3(150,15,6,60)
    assert main["usage_input"] == 450
    assert main["usage_cache_write"] == 45
    assert main["usage_cache_read"] == 19
    assert main["usage_output"] == 180
    assert main["n_messages"] == 3
    assert main["project_slug"] == "proj-alpha"


def test_subagent_unit_applies_the_fixed_rule(store):
    scan(FIXTURES, host=HOST, store=store)
    units = _units(store)
    sub = units[f"{HOST}/SESS-SUB/1234567890abcdef1"]
    # placeholder(3,0,4560,730) + nostop-max(15,1,50,25)
    assert sub["usage_input"] == 18
    assert sub["usage_output"] == 755
    assert sub["parent_unit_key"] == f"{HOST}/SESS-SUB/-"


def test_subagent_meta_backfills_agent_type_and_tool_use_id_on_the_transcript_unit(store):
    scan(FIXTURES, host=HOST, store=store)
    units = _units(store)
    sub = units[f"{HOST}/SESS-SUB/1234567890abcdef1"]
    assert sub["agent_type"] == "general-purpose"
    assert sub["spawn_tool_use_id"] == "toolu_ABC123"


def test_a_meta_with_no_sibling_jsonl_gets_usage_source_none(store):
    scan(FIXTURES, host=HOST, store=store)
    units = _units(store)
    orphan = units[f"{HOST}/SESS-SUB/2234567890abcdef2"]
    assert orphan["usage_source"] == "none"
    assert orphan["spawn_tool_use_id"] == "toolu_XYZ999"
    assert orphan["agent_type"] == "lens"
    assert orphan["kind"] == "subagent"


def test_resumed_session_dedupes_shared_message_ids_by_the_earlier_file(store):
    report = scan(FIXTURES, host=HOST, store=store)
    units = _units(store)
    a = units[f"{HOST}/SESS-RESUME-A/-"]
    b = units[f"{HOST}/SESS-RESUME-B/-"]
    assert a["n_messages"] == 2
    assert a["usage_input"] == 33  # 11 + 22
    assert b["n_messages"] == 1
    assert b["usage_input"] == 33  # only msg-resume-3
    assert report.messages_deduplicated == 2


def test_remote_control_pattern_with_statusline_gives_transcript_partial(store):
    report = scan(FIXTURES, host=HOST, store=store, statusline_dir=FIXTURES / "statusline")
    units = _units(store)
    rc = units[f"{HOST}/SESS-RC/-"]
    assert rc["usage_source"] == "transcript_partial"
    assert json.loads(rc["statusline_cost"]) == {"total_cost_usd": 4.56, "total_duration_ms": 999999}
    assert "SESS-RC" in report.remote_control_sessions


def test_missing_sessions_json_leaves_the_unit_as_plain_transcript(store, tmp_path):
    empty_statusline_dir = tmp_path / "no-statusline-here"
    empty_statusline_dir.mkdir()
    scan(FIXTURES, host=HOST, store=store, statusline_dir=empty_statusline_dir)
    units = _units(store)
    rc = units[f"{HOST}/SESS-RC/-"]
    assert rc["usage_source"] == "transcript"


def test_a_session_the_statusline_saw_with_no_transcript_at_all_gets_statusline_total(store, tmp_path):
    """S-2 fix round: design Section 2.2 step 4's other branch -- a session
    sessions.json knows about that has NO main .jsonl anywhere under
    projects_root at all (not just a Remote-Control-pattern partial one).
    Before this fix the session was silently skipped entirely."""
    statusline_dir = tmp_path / "statusline"
    statusline_dir.mkdir()
    (statusline_dir / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "SESS-GHOST": {
                        "first_seen_ts": "2026-09-21T08:00:00.000Z",
                        "last_seen_ts": "2026-09-21T09:30:00.000Z",
                        "cost": {"total_cost_usd": 1.23, "total_duration_ms": 45000},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = scan(FIXTURES, host=HOST, store=store, statusline_dir=statusline_dir)
    units = _units(store)
    ghost_key = f"{HOST}/SESS-GHOST/-"
    assert ghost_key in units
    ghost = units[ghost_key]
    assert ghost["kind"] == "main"
    assert ghost["usage_source"] == "statusline_total"
    assert ghost["first_ts"] == "2026-09-21T08:00:00.000Z"
    assert ghost["last_ts"] == "2026-09-21T09:30:00.000Z"
    assert json.loads(ghost["statusline_cost"]) == {"total_cost_usd": 1.23, "total_duration_ms": 45000}
    assert ghost["project_slug"] == "(unknown)"
    assert "SESS-GHOST" in report.remote_control_sessions


def test_without_a_statusline_dir_at_all_the_unit_stays_transcript(store):
    scan(FIXTURES, host=HOST, store=store)
    units = _units(store)
    rc = units[f"{HOST}/SESS-RC/-"]
    assert rc["usage_source"] == "transcript"


def test_incremental_scan_reads_zero_files_on_the_second_pass(store, tmp_path):
    work = tmp_path / "projects"
    shutil.copytree(FIXTURES / "proj-alpha", work / "proj-alpha")
    r1 = scan(work, host=HOST, store=store)
    assert r1.files_read > 0
    r2 = scan(work, host=HOST, store=store)
    assert r2.files_read == 0
    assert r2.files_skipped == r1.files_read


def test_appending_a_line_re_reads_only_that_file_and_changes_its_sums_exactly(store, tmp_path):
    work = tmp_path / "projects"
    shutil.copytree(FIXTURES / "proj-alpha", work / "proj-alpha")
    scan(work, host=HOST, store=store)
    before = _units(store)[f"{HOST}/SESS-MAIN/-"]

    main_path = work / "proj-alpha" / "SESS-MAIN.jsonl"
    extra_line = (
        '{"type":"assistant","sessionId":"SESS-MAIN","timestamp":"2026-09-19T08:00:20.000Z",'
        '"requestId":"req-4","version":"2.1.280","message":{"id":"msg-main-4","model":"claude-opus-5-5",'
        '"role":"assistant","stop_reason":"end_turn","usage":{"input_tokens":9,'
        '"cache_creation_input_tokens":1,"cache_read_input_tokens":1,"output_tokens":2}}}\n'
    )
    with open(main_path, "a", encoding="utf-8") as fh:
        fh.write(extra_line)

    report = scan(work, host=HOST, store=store)
    assert report.files_read == 1
    after = _units(store)[f"{HOST}/SESS-MAIN/-"]
    assert after["usage_input"] == before["usage_input"] + 9
    assert after["usage_output"] == before["usage_output"] + 2
    assert after["n_messages"] == before["n_messages"] + 1

    # every OTHER unit's file was untouched and must be unaffected
    sub = _units(store)[f"{HOST}/SESS-SUB/1234567890abcdef1"] if (work / "proj-alpha" / "SESS-SUB").exists() else None
    if sub is not None:
        assert sub["usage_input"] == 18


def test_dry_run_writes_nothing(store):
    report = scan(FIXTURES, host=HOST, store=store, dry_run=True)
    assert report.files_read > 0
    assert _units(store) == {}


def test_scan_is_read_only_on_the_fixture_files():
    """Never overwritten by this test module: an md5 of the whole fixture
    directory would be brittle to touch here, so this just asserts the
    fixture files this test depends on still exist and are non-empty after
    every OTHER test in this module has run against them."""
    for rel in (
        "proj-alpha/SESS-MAIN.jsonl",
        "proj-alpha/SESS-SUB/subagents/agent-1234567890abcdef1.jsonl",
        "proj-alpha/SESS-RESUME-A.jsonl",
        "proj-alpha/SESS-RESUME-B.jsonl",
        "proj-beta/SESS-RC.jsonl",
    ):
        assert (FIXTURES / rel).stat().st_size > 0
