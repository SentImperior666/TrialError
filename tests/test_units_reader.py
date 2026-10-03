"""``trialerror.units.reader.read_transcript`` -- the corrected reading rule
for streamed messages (design Section 2.2): per message id, take the
``stop_reason`` line's usage (last one, if several), else each field's
maximum across lines. Fixtures built from the observed shapes in design
Section 1 (never from summaries), under ``tests/fixtures/units/``.
"""

from __future__ import annotations

from pathlib import Path

from trialerror.units.reader import read_transcript

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "units"


def test_main_transcript_sums_three_messages_with_final_usage_on_every_line():
    summary = read_transcript(FIXTURES / "proj-alpha" / "SESS-MAIN.jsonl")
    assert set(summary.messages) == {"msg-main-1", "msg-main-2", "msg-main-3"}
    m1 = summary.messages["msg-main-1"]
    assert (m1.input_tokens, m1.cache_write, m1.cache_read, m1.output) == (100, 10, 5, 50)
    m2 = summary.messages["msg-main-2"]
    assert (m2.input_tokens, m2.cache_write, m2.cache_read, m2.output) == (200, 20, 8, 70)
    assert summary.models == {"claude-opus-5-5"}
    assert summary.entrypoint == "cli"
    assert summary.version == "2.1.280"
    assert summary.first_ts == "2026-09-19T08:00:00.200Z"
    assert summary.last_ts == "2026-09-19T08:00:10.400Z"
    assert summary.conversation_last_ts == "2026-09-19T08:00:10.400Z"


def test_subagent_stop_reason_line_wins_over_the_placeholder():
    """The design's own regression case: 5 -> 730, not 5."""
    summary = read_transcript(
        FIXTURES / "proj-alpha" / "SESS-SUB" / "subagents" / "agent-1234567890abcdef1.jsonl"
    )
    placeholder = summary.messages["msg-sub-placeholder"]
    assert placeholder.output == 730
    assert placeholder.input_tokens == 3
    assert placeholder.cache_read == 4560


def test_a_message_with_no_stop_reason_line_takes_the_per_field_maximum():
    summary = read_transcript(
        FIXTURES / "proj-alpha" / "SESS-SUB" / "subagents" / "agent-1234567890abcdef1.jsonl"
    )
    nostop = summary.messages["msg-sub-nostop"]
    # line1: input=10,cache_write=1,cache_read=50,output=20
    # line2: input=15,cache_write=1,cache_read=40,output=25
    assert (nostop.input_tokens, nostop.cache_write, nostop.cache_read, nostop.output) == (15, 1, 50, 25)


def test_synthetic_model_lines_are_skipped_entirely():
    summary = read_transcript(
        FIXTURES / "proj-alpha" / "SESS-SUB" / "subagents" / "agent-1234567890abcdef1.jsonl"
    )
    assert "msg-sub-synthetic" not in summary.messages
    assert "<synthetic>" not in summary.models


def test_the_resumed_session_files_share_two_message_ids():
    a = read_transcript(FIXTURES / "proj-alpha" / "SESS-RESUME-A.jsonl")
    b = read_transcript(FIXTURES / "proj-alpha" / "SESS-RESUME-B.jsonl")
    assert set(a.messages) == {"msg-resume-1", "msg-resume-2"}
    assert set(b.messages) == {"msg-resume-1", "msg-resume-2", "msg-resume-3"}
    assert a.messages["msg-resume-1"].input_tokens == b.messages["msg-resume-1"].input_tokens == 11


def test_remote_control_fixture_conversation_last_ts_stops_early():
    summary = read_transcript(FIXTURES / "proj-beta" / "SESS-RC.jsonl")
    assert summary.conversation_last_ts == "2026-09-20T10:01:00.000Z"
    assert summary.last_ts == "2026-09-20T19:00:30.000Z"
    assert summary.first_ts == "2026-09-20T10:00:00.000Z"


def test_malformed_lines_are_tolerated(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(
        "not json at all\n"
        '"a bare json string"\n'
        "\n"
        '{"type":"assistant","timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"id":"m1","model":"x","stop_reason":"end_turn",'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n',
        encoding="utf-8",
    )
    summary = read_transcript(path)
    assert summary.lines_skipped == 2
    assert set(summary.messages) == {"m1"}


def test_null_usage_does_not_crash_and_contributes_zero(tmp_path):
    path = tmp_path / "null_usage.jsonl"
    path.write_text(
        '{"type":"assistant","timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"id":"m1","model":"x","stop_reason":"end_turn","usage":null}}\n',
        encoding="utf-8",
    )
    summary = read_transcript(path)
    m1 = summary.messages["m1"]
    assert (m1.input_tokens, m1.cache_write, m1.cache_read, m1.output) == (0, 0, 0, 0)


def test_a_stop_reason_line_with_null_usage_falls_back_to_the_per_field_max(tmp_path):
    """N-3 fix round: a stop_reason line whose own ``usage`` is ``null``
    must not zero out a message that had real usage on an earlier line --
    fall back to the per-field maximum instead."""
    path = tmp_path / "null_stop_usage.jsonl"
    path.write_text(
        '{"type":"assistant","timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"id":"m1","model":"x","usage":{"input_tokens":1,"output_tokens":40}}}\n'
        '{"type":"assistant","timestamp":"2026-01-01T00:00:01.000Z",'
        '"message":{"id":"m1","model":"x","stop_reason":"end_turn","usage":null}}\n',
        encoding="utf-8",
    )
    summary = read_transcript(path)
    m1 = summary.messages["m1"]
    assert m1.output == 40
    assert m1.input_tokens == 1


def test_sha256_is_computed_and_stable(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text('{"type":"user","timestamp":"2026-01-01T00:00:00.000Z"}\n', encoding="utf-8")
    s1 = read_transcript(path)
    s2 = read_transcript(path)
    assert s1.sha256 == s2.sha256
    assert len(s1.sha256) == 64
