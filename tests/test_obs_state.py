"""``trialerror.obs.state``: the durable, cross-process span-drop counter."""

from __future__ import annotations

import json

import pytest

from trialerror.obs import state


@pytest.fixture(autouse=True)
def _reset_process_counter():
    """The in-process fallback counter is a module-level global (see
    ``trialerror.obs.state``'s docstring on why it exists at all) -- it survives
    across test functions/files in the same pytest session unless reset,
    so every test in this module gets a clean baseline."""
    state.reset_for_tests()
    yield


def test_read_span_drop_state_is_clean_zero_when_no_file_exists(tmp_path):
    result = state.read_span_drop_state(tmp_path)
    assert result == {"count": 0, "last_ts": None, "last_reason": None}


def test_record_span_drop_persists_across_a_fresh_read(tmp_path):
    state.record_span_drop(tmp_path, count=3, reason="connection refused")
    result = state.read_span_drop_state(tmp_path)
    assert result["count"] == 3
    assert result["last_reason"] == "connection refused"
    assert result["last_ts"] is not None


def test_record_span_drop_accumulates_across_multiple_calls(tmp_path):
    state.record_span_drop(tmp_path, count=1, reason="first")
    state.record_span_drop(tmp_path, count=2, reason="second")
    result = state.read_span_drop_state(tmp_path)
    assert result["count"] == 3
    assert result["last_reason"] == "second"  # most recent wins


def test_record_span_drop_always_increments_the_in_process_counter_too(tmp_path):
    state.reset_for_tests(tmp_path)
    state.record_span_drop(None, count=5, reason="no program_root given")  # persistence skipped, in-process still counts
    assert state.process_drop_count() == 5
    assert state.read_span_drop_state(tmp_path) == {"count": 0, "last_ts": None, "last_reason": None}


def test_record_span_drop_is_atomic_write_not_partial(tmp_path):
    state.record_span_drop(tmp_path, count=1, reason="x")
    path = tmp_path / "obs" / "span_drop_state.json"
    assert path.is_file()
    # A file readable by plain json.loads with no half-written trailing
    # bytes is exactly what trialerror.util.atomic.atomic_write_text guarantees.
    json.loads(path.read_text(encoding="utf-8"))


def test_record_span_drop_never_raises_on_an_unwritable_path(tmp_path):
    # program_root that is actually a FILE, not a directory -- mkdir under
    # it must fail; record_span_drop must swallow that, per its own
    # "bookkeeping must never break emission" contract.
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("x", encoding="utf-8")
    state.record_span_drop(blocked, count=1, reason="should not raise")  # must not raise


def test_reset_for_tests_clears_both_in_process_and_persisted_state(tmp_path):
    state.reset_for_tests(tmp_path)  # start from a clean in-process counter, not whatever earlier tests left behind
    state.record_span_drop(tmp_path, count=7, reason="x")
    assert state.process_drop_count() == 7
    state.reset_for_tests(tmp_path)
    assert state.process_drop_count() == 0
    assert state.read_span_drop_state(tmp_path)["count"] == 0
