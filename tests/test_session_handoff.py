"""Tests for ``trialerror.sessions.handoff`` — the pure renderer, filename
sequencing, the supersession-notice writer, and the recovery re-render."""

from __future__ import annotations

import json

import pytest

from trialerror.sessions.handoff import (
    HANDOFFS_DIR_NAME,
    latest_handoff,
    list_handoffs,
    next_handoff_filename,
    render_handoff,
    rerender_handoff,
    resolve_handoffs_dir,
    write_handoff_with_supersession,
)
from trialerror.sessions.lifecycle import close_session
from trialerror.stores import get

from tests.test_session_helpers import add_hook_alive, seed_open_session

_COURSE_CHECK = {"rungs": "1", "build_vs_theory": "build", "drift_flag": False}


# ---------------------------------------------------------------------------
# next_handoff_filename / list_handoffs / latest_handoff
# ---------------------------------------------------------------------------


def test_next_handoff_filename_first_of_day_no_suffix(tmp_path):
    assert next_handoff_filename(tmp_path / "handoffs", "2026-08-29") == "HANDOFF_2026-08-29.md"


def test_next_handoff_filename_second_of_day_is_b(tmp_path):
    d = tmp_path / "handoffs"
    d.mkdir()
    (d / "HANDOFF_2026-08-29.md").write_text("x", encoding="utf-8")
    assert next_handoff_filename(d, "2026-08-29") == "HANDOFF_2026-08-29b.md"


def test_next_handoff_filename_sequence_skips_no_letters(tmp_path):
    d = tmp_path / "handoffs"
    d.mkdir()
    (d / "HANDOFF_2026-08-29.md").write_text("x", encoding="utf-8")
    (d / "HANDOFF_2026-08-29b.md").write_text("x", encoding="utf-8")
    assert next_handoff_filename(d, "2026-08-29") == "HANDOFF_2026-08-29c.md"


def test_next_handoff_filename_new_date_no_suffix_even_if_other_dates_exist(tmp_path):
    d = tmp_path / "handoffs"
    d.mkdir()
    (d / "HANDOFF_2026-08-28.md").write_text("x", encoding="utf-8")
    (d / "HANDOFF_2026-08-28b.md").write_text("x", encoding="utf-8")
    assert next_handoff_filename(d, "2026-08-29") == "HANDOFF_2026-08-29.md"


def test_list_handoffs_sorted_newest_last(tmp_path):
    d = tmp_path / "handoffs"
    d.mkdir()
    names = ["HANDOFF_2026-08-29c.md", "HANDOFF_2026-08-28.md", "HANDOFF_2026-08-29.md", "HANDOFF_2026-08-29b.md"]
    for n in names:
        (d / n).write_text("x", encoding="utf-8")
    ordered = [p.name for p in list_handoffs(d)]
    assert ordered == [
        "HANDOFF_2026-08-28.md",
        "HANDOFF_2026-08-29.md",
        "HANDOFF_2026-08-29b.md",
        "HANDOFF_2026-08-29c.md",
    ]
    assert latest_handoff(d).name == "HANDOFF_2026-08-29c.md"


def test_list_handoffs_empty_dir(tmp_path):
    assert list_handoffs(tmp_path / "nonexistent") == []
    assert latest_handoff(tmp_path / "nonexistent") is None


# ---------------------------------------------------------------------------
# render_handoff purity
# ---------------------------------------------------------------------------


def test_render_handoff_is_pure_and_deterministic():
    session = {
        "session_id": "SESS-abc",
        "account_id": "ACC-xyz",
        "status": "closed",
        "opened_ts": "2026-08-29T00:00:00.000Z",
        "closed_ts": "2026-08-29T01:00:00.000Z",
        "boot_pin_version": "v3@2026-08-29",
    }
    close_report = {"notes": "went fine", "launch_counts": {"RECONCILED": 2}}
    course_check = {"rungs": "2", "build_vs_theory": "all build", "drift_flag": False}

    first = render_handoff(session, close_report=close_report, course_check=course_check)
    second = render_handoff(session, close_report=close_report, course_check=course_check)
    assert first == second
    assert "SESS-abc" in first
    assert "went fine" in first
    assert "RENDERED VIEW" in first
    assert "never hand-edit" in first


def test_render_handoff_never_hand_edited_language_and_pin_present():
    session = {"session_id": "SESS-1", "account_id": "ACC-1", "opened_ts": "t", "boot_pin_version": "v1@2026-08-29"}
    text = render_handoff(session, close_report={}, course_check={})
    assert "v1@2026-08-29" in text


# ---------------------------------------------------------------------------
# resolve_handoffs_dir ([paths].handoffs_dir, the import-design notes (internal, not in this export) Sec 5
# knob #3 -- also the fix for the "hardcoded twice, independently" drift
# bug that section flags between this module and trialerror.sessions.lifecycle)
# ---------------------------------------------------------------------------


def test_resolve_handoffs_dir_defaults_unconfigured(tmp_path):
    assert resolve_handoffs_dir(tmp_path) == tmp_path / HANDOFFS_DIR_NAME
    assert resolve_handoffs_dir(tmp_path, None) == tmp_path / HANDOFFS_DIR_NAME
    assert resolve_handoffs_dir(tmp_path, {}) == tmp_path / HANDOFFS_DIR_NAME


def test_resolve_handoffs_dir_relative_override(tmp_path):
    config = {"paths": {"handoffs_dir": "closes/handoffs"}}
    assert resolve_handoffs_dir(tmp_path, config) == tmp_path / "closes" / "handoffs"


def test_resolve_handoffs_dir_absolute_override_ignores_program_root(tmp_path):
    external = tmp_path / "elsewhere" / "handoffs"
    config = {"paths": {"handoffs_dir": str(external)}}
    assert resolve_handoffs_dir(tmp_path / "program", config) == external


# ---------------------------------------------------------------------------
# write_handoff_with_supersession
# ---------------------------------------------------------------------------


def test_write_handoff_creates_file_with_no_prior(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is True

    path = store.program_root / "handoffs" / result.close_report["handoff_filename"]
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == open(result.handoff_path, encoding="utf-8").read()


def test_write_handoff_supersedes_prior_same_day(store, monkeypatch):
    account_id, s1 = seed_open_session(store)
    add_hook_alive(store, s1)
    r1 = close_session(store, session_id=s1, course_check=_COURSE_CHECK, now_ts="2026-08-29T10:00:00.000Z")
    assert r1.ok is True
    first_filename = r1.close_report["handoff_filename"]
    assert first_filename == "HANDOFF_2026-08-29.md"

    _, s2 = seed_open_session(store, account_id=account_id)
    add_hook_alive(store, s2)
    r2 = close_session(store, session_id=s2, course_check=_COURSE_CHECK, now_ts="2026-08-29T20:00:00.000Z")
    assert r2.ok is True
    second_filename = r2.close_report["handoff_filename"]
    assert second_filename == "HANDOFF_2026-08-29b.md"

    handoffs_dir = store.program_root / "handoffs"
    prior_text = (handoffs_dir / first_filename).read_text(encoding="utf-8")
    assert prior_text.startswith("> **SUPERSEDED**")
    assert second_filename in prior_text

    # The newest file is untouched by any supersession marker.
    newest_text = (handoffs_dir / second_filename).read_text(encoding="utf-8")
    assert not newest_text.startswith("> **SUPERSEDED**")


def test_write_handoff_double_supersession_never_double_marks(store):
    """Three closes same day: the FIRST file gets marked once (by the
    second close) and is never re-marked by the third."""
    account_id, s1 = seed_open_session(store)
    add_hook_alive(store, s1)
    r1 = close_session(store, session_id=s1, course_check=_COURSE_CHECK, now_ts="2026-08-29T09:00:00.000Z")

    _, s2 = seed_open_session(store, account_id=account_id)
    add_hook_alive(store, s2)
    r2 = close_session(store, session_id=s2, course_check=_COURSE_CHECK, now_ts="2026-08-29T10:00:00.000Z")

    _, s3 = seed_open_session(store, account_id=account_id)
    add_hook_alive(store, s3)
    r3 = close_session(store, session_id=s3, course_check=_COURSE_CHECK, now_ts="2026-08-29T11:00:00.000Z")

    handoffs_dir = store.program_root / "handoffs"
    first_text = (handoffs_dir / r1.close_report["handoff_filename"]).read_text(encoding="utf-8")
    second_text = (handoffs_dir / r2.close_report["handoff_filename"]).read_text(encoding="utf-8")
    third_text = (handoffs_dir / r3.close_report["handoff_filename"]).read_text(encoding="utf-8")

    assert first_text.count("SUPERSEDED") == 1
    assert second_text.count("SUPERSEDED") == 1  # r2 is superseded by r3
    assert third_text.count("SUPERSEDED") == 0  # r3 is current


# ---------------------------------------------------------------------------
# rerender_handoff (recovery re-flush)
# ---------------------------------------------------------------------------


def test_rerender_handoff_reproduces_original_after_deletion(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    original_text = open(result.handoff_path, encoding="utf-8").read()

    import os

    os.remove(result.handoff_path)
    assert not os.path.exists(result.handoff_path)

    rerendered = rerender_handoff(store, session_id=session_id)
    assert rerendered.path == result.handoff_path
    assert open(rerendered.path, encoding="utf-8").read() == original_text


def test_rerender_handoff_unclosed_session_raises(store):
    _, session_id = seed_open_session(store)
    with pytest.raises(ValueError):
        rerender_handoff(store, session_id=session_id)


def test_rerender_handoff_unknown_session_raises(store):
    with pytest.raises(ValueError):
        rerender_handoff(store, session_id="SESS-nope")


def test_rerender_handoff_abandoned_session_has_no_filename_raises(store):
    from trialerror.sessions.lifecycle import abandon_session

    _, session_id = seed_open_session(store)
    abandon_session(store, session_id=session_id, reason="crashed")
    with pytest.raises(ValueError):
        rerender_handoff(store, session_id=session_id)
