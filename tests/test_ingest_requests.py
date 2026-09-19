"""Tests for ``trialerror.ingest.requests``: the request-queue state machine
(design Section 6: "wanted -> requested -> delivered -> verifying ->
archived -> indexed (+ rejected/failed)") and the REQUESTS.md renderer."""

from __future__ import annotations

import pytest

from trialerror.ingest import pipeline, requests as ingest_requests
from trialerror.ingest.errors import InvalidRequestTransitionError
from tests._ingest_fixtures import bootstrap_launch


def _wanted_source(store):
    launch_id = bootstrap_launch(store)
    row = pipeline.register_source(
        store, kind="book", title="Wanted Book", license_tier="unknown", acquisition_route="web",
        registered_by_launch=launch_id, request_state="wanted",
    )
    return row, launch_id


def test_valid_transition_chain_wanted_to_indexed(store):
    row, launch_id = _wanted_source(store)
    sid = row["source_id"]
    row = ingest_requests.transition(store, sid, "requested", launch_id=launch_id)
    assert row["request_state"] == "requested"
    assert row["requested_ts"] is not None
    row = ingest_requests.transition(store, sid, "delivered", launch_id=launch_id)
    assert row["delivered_ts"] is not None
    row = ingest_requests.transition(store, sid, "verifying", launch_id=launch_id)
    row = ingest_requests.transition(store, sid, "archived", launch_id=launch_id)
    row = ingest_requests.transition(store, sid, "indexed", launch_id=launch_id)
    assert row["request_state"] == "indexed"


def test_illegal_transition_skipping_states_refused(store):
    row, launch_id = _wanted_source(store)
    with pytest.raises(InvalidRequestTransitionError):
        ingest_requests.transition(store, row["source_id"], "indexed", launch_id=launch_id)


def test_terminal_indexed_state_has_no_further_transitions(store):
    row, launch_id = _wanted_source(store)
    sid = row["source_id"]
    for state in ("requested", "delivered", "verifying", "archived", "indexed"):
        ingest_requests.transition(store, sid, state, launch_id=launch_id)
    with pytest.raises(InvalidRequestTransitionError):
        ingest_requests.transition(store, sid, "archived", launch_id=launch_id)


def test_failed_can_be_retried_via_requested(store):
    row, launch_id = _wanted_source(store)
    sid = row["source_id"]
    ingest_requests.transition(store, sid, "requested", launch_id=launch_id)
    row = ingest_requests.transition(store, sid, "failed", launch_id=launch_id)
    assert row["request_state"] == "failed"
    row = ingest_requests.transition(store, sid, "requested", launch_id=launch_id)
    assert row["request_state"] == "requested"


def test_every_transition_logs_an_event(store):
    row, launch_id = _wanted_source(store)
    before = store.ops.execute("SELECT COUNT(*) FROM event WHERE type='ingest_request_transition'").fetchone()[0]
    ingest_requests.transition(store, row["source_id"], "requested", launch_id=launch_id)
    after = store.ops.execute("SELECT COUNT(*) FROM event WHERE type='ingest_request_transition'").fetchone()[0]
    assert after == before + 1


def test_render_requests_md_groups_by_state(store):
    row, launch_id = _wanted_source(store)
    ingest_requests.transition(store, row["source_id"], "requested", launch_id=launch_id)
    md = ingest_requests.render_requests_md(store)
    assert "## requested (1)" in md
    assert row["source_id"] in md


def test_write_requests_md_atomic_write_to_disk(store, program_root):
    row, launch_id = _wanted_source(store)
    out_path = ingest_requests.write_requests_md(store, program_root)
    assert out_path.is_file()
    assert out_path == program_root / "requests" / "REQUESTS.md"
    assert row["source_id"] in out_path.read_text(encoding="utf-8")


# ---- [paths].requests_path knob (the import-design notes (internal, not in this export) Sec 5 knob #4) ------


def test_write_requests_md_default_config_matches_unconfigured_behavior(store, program_root):
    row, launch_id = _wanted_source(store)
    out_path = ingest_requests.write_requests_md(store, program_root, config={})
    assert out_path == program_root / "requests" / "REQUESTS.md"
    assert out_path.is_file()


def test_write_requests_md_respects_configured_relative_requests_path(store, program_root):
    row, launch_id = _wanted_source(store)
    config = {"paths": {"requests_path": "acquire/WANTED.md"}}
    out_path = ingest_requests.write_requests_md(store, program_root, config=config)

    assert out_path == program_root / "acquire" / "WANTED.md"
    assert out_path.is_file()
    assert row["source_id"] in out_path.read_text(encoding="utf-8")
    assert not (program_root / "requests").exists()


def test_write_requests_md_respects_configured_absolute_requests_path(store, program_root, tmp_path):
    row, launch_id = _wanted_source(store)
    external = tmp_path / "external-requests" / "WANTED.md"
    config = {"paths": {"requests_path": str(external)}}
    out_path = ingest_requests.write_requests_md(store, program_root, config=config)

    assert out_path == external
    assert external.is_file()
    assert not (program_root / "requests").exists()
