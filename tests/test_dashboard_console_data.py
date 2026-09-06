"""The server half of the Console renderer (2026-09 bug sweep §3.10, K4
items (a) and tests 24-31).

Everything the Console draws is a pure function of the ``/dashboard/api/all``
bundle -- that is what keeps the static export honest -- so every reading the
new cards need had to become a FIELD rather than a client-side convention.
This module holds the tests for those fields: the decoded job payload and
checkpoint, the derived subject and duration, the offload queue summary, and
the session timeline.

Kept in its own file rather than appended to ``test_dashboard_data.py``:
three worktrees are adding panel fields to that module in parallel this
lane, and a shared file is a merge conflict per builder for no reason.
"""

from __future__ import annotations

import json

import pytest

from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def seeded(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    yield store, ids, program_root, platform_root
    store.close()


def panel(builder, program_root, platform_root, **kwargs):
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        return builder(rostore, **kwargs)
    finally:
        rostore.close()


# ---------------------------------------------------------------------------
# jobs panel (sweep §3.10 item 1; tests 24-26)
# ---------------------------------------------------------------------------


def test_jobs_panel_decodes_payload_and_checkpoint_and_subject(seeded):
    """console-3's core: the ingest checkpoint is the most information-dense
    field in the panel and used to reach the client as one long JSON string
    printed into a table cell. It arrives decoded, with the two derived
    facts the k9s-style table needs beside it."""
    store, ids, program_root, platform_root = seeded
    update(
        store, "job", pk_column="job_id", pk_value=ids["job"],
        changes={
            "kind": "custom",
            "payload": json.dumps({"handler": "arxiv_index_build", "shard": 3}),
            "checkpoint": json.dumps({"rows_ingested": 3569548, "current_member": "2410"}),
        },
    )
    store.close()

    p = panel(data.build_jobs_panel, program_root, platform_root)
    row = p["recent_jobs"][0]
    assert row["checkpoint"]["rows_ingested"] == 3569548
    assert row["payload"]["handler"] == "arxiv_index_build"
    assert row["subject"] == "arxiv_index_build"


def test_jobs_panel_leaves_unparseable_text_alone(seeded):
    """A column holding a plain note must survive decoding untouched, not
    become ``None`` -- the non-JSON fallback is the whole reason
    ``_decode_json_text`` exists rather than a bare ``json.loads``."""
    store, ids, program_root, platform_root = seeded
    update(
        store, "job", pk_column="job_id", pk_value=ids["job"],
        changes={"checkpoint": "resumed by hand after the power cut"},
    )
    store.close()

    row = panel(data.build_jobs_panel, program_root, platform_root)["recent_jobs"][0]
    assert row["checkpoint"] == "resumed by hand after the power cut"


def test_job_subject_prefers_the_most_specific_thing_the_payload_names():
    assert data._job_subject({"handler": "feed_translate", "doc_id": "DOC-1"}) == "feed_translate"
    assert data._job_subject({"doc_id": "DOC-1"}) == "DOC-1"
    assert data._job_subject({"source_id": "SRC-9"}) == "SRC-9"
    assert data._job_subject({"zip_path": "C:\\tmp\\batch-07.zip"}) == "batch-07.zip"
    assert data._job_subject({"unknown": 1, "other": 2}) == "2 field(s)"
    assert data._job_subject({}) is None
    assert data._job_subject("not a dict") is None


def test_jobs_panel_duration_for_settled_only(seeded):
    """A settled row has no live lease, so the LEASE / DURATION column shows
    how long it took instead (console-4's accepted downgrade)."""
    store, ids, program_root, platform_root = seeded
    settled = new_id("JOB")
    insert(
        store, "job",
        {
            "job_id": settled, "kind": "embed", "payload": "{}", "state": "complete",
            "created_ts": "2026-09-05T10:00:00.000Z", "settled_ts": "2026-09-05T14:00:00.000Z",
        },
    )
    store.close()

    rows = {r["job_id"]: r for r in panel(data.build_jobs_panel, program_root, platform_root)["recent_jobs"]}
    assert rows[settled]["duration_s"] == 4 * 3600
    assert rows[ids["job"]]["duration_s"] is None  # the fixture job is still pending


def test_jobs_panel_offload_summary_absent_and_present(seeded):
    """Two independent sources, one count. Before the queue directory exists
    the ledger's own parked rows are the only evidence; after it exists the
    manifests are, and a job must never be counted twice."""
    store, ids, program_root, platform_root = seeded
    update(
        store, "job", pk_column="job_id", pk_value=ids["job"],
        changes={
            "last_error": "awaiting DEV GPU: offload ocr job JOB-1 queued (run the GPU worker on DEV)",
            "failure_class": "environmental",
        },
    )
    store.close()

    absent = panel(data.build_jobs_panel, program_root, platform_root)["offload"]
    assert absent["available"] is False
    assert absent["counts"] == {"pending": 0, "claimed": 0, "done": 0, "failed": 0}
    assert absent["awaiting"] == 1  # the ledger row alone
    assert absent["jobs"] == {}

    root = program_root / "offload"
    (root / "pending").mkdir(parents=True)
    (root / "pending" / "JOB-QUEUED.json").write_text(
        json.dumps({"job_id": "JOB-QUEUED", "offload_attempts": 2}), encoding="utf-8"
    )
    (root / "claimed" / "DEV-1").mkdir(parents=True)
    (root / "claimed" / "DEV-1" / "JOB-CLAIMED.json").write_text("{}", encoding="utf-8")
    (root / "claimed" / "DEV-1" / "JOB-CLAIMED.heartbeat").write_text(now(), encoding="utf-8")

    present = panel(data.build_jobs_panel, program_root, platform_root)["offload"]
    assert present["available"] is True
    assert present["counts"] == {"pending": 1, "claimed": 1, "done": 0, "failed": 0}
    assert present["jobs"]["JOB-CLAIMED"]["worker_id"] == "DEV-1"
    assert present["jobs"]["JOB-QUEUED"]["offload_attempts"] == 2
    # the two manifests plus the one parked ledger row, none double-counted
    assert present["awaiting"] == 3


# ---------------------------------------------------------------------------
# session panel (sweep §3.10 item 2; tests 27-28)
# ---------------------------------------------------------------------------


def test_session_panel_queue_is_decoded(seeded):
    """M-CON-3: ``boot_bundle_stats.queue`` was the other raw JSON string on
    the page. The SESSION card prints its length."""
    store, ids, program_root, platform_root = seeded
    update(
        store, "session", pk_column="session_id", pk_value=ids["session"],
        changes={"queue": json.dumps(["read the handoff", "reconcile 2 launches"])},
    )
    store.close()

    p = panel(data.build_session_panel, program_root, platform_root)
    assert p["open_session"]["boot_bundle_stats"]["queue"] == [
        "read the handoff", "reconcile 2 launches",
    ]


def test_session_panel_timeline_spans_from_launch_job_room(seeded):
    """The timeline is a derivation, not a new table: one span per launch,
    per job created inside the window, and per room the session opened; the
    hook heartbeats and gate transitions arrive as instants."""
    store, ids, program_root, platform_root = seeded
    claimed_ts = now()
    insert(store, "job_event", {"job_id": ids["job"], "ts": claimed_ts, "type": "claimed"})
    room_id = new_id("ROOM")
    for event_type, payload in (
        ("room_created", {"room_id": room_id, "question": "does the anchor hold?"}),
        ("hook_alive", {}),
        ("room_frozen", {"room_id": room_id}),
    ):
        insert(
            store, "event",
            {
                "event_id": new_id("EVT"), "ts": now(), "session_id": ids["session"],
                "type": event_type, "payload": json.dumps(payload),
            },
        )
    store.close()

    timeline = panel(data.build_session_panel, program_root, platform_root)["open_session"]["timeline"]
    assert timeline["window"]["end_ts"] is None  # the session is still open
    by_kind = {s["kind"]: s for s in timeline["spans"]}
    assert set(by_kind) == {"launch", "job", "room"}
    assert by_kind["launch"]["status"] == "booked"  # PROVISIONAL
    assert by_kind["launch"]["ref"] == {"launch_id": ids["launch"]}
    assert by_kind["job"]["start_ts"] == claimed_ts  # not created_ts
    assert by_kind["job"]["status"] == "booked"  # the fixture job is pending
    assert by_kind["room"]["status"] == "frozen"
    assert by_kind["room"]["end_ts"] is not None
    instant_kinds = {i["kind"] for i in timeline["instants"]}
    assert "hook_alive" in instant_kinds
    assert "gate_transition" in instant_kinds
    assert timeline["truncated"]["spans_dropped"] == 0


def test_session_timeline_is_none_when_no_session_is_open(seeded):
    store, ids, program_root, platform_root = seeded
    update(
        store, "session", pk_column="session_id", pk_value=ids["session"],
        changes={"status": "closed", "closed_ts": now()},
    )
    store.close()

    p = panel(data.build_session_panel, program_root, platform_root)
    assert p["open_session"] is None


def test_session_timeline_truncation_reports_counts(seeded):
    """House rule: truncation reports itself. 250 launches under one session
    is 200 spans and a count of what was dropped, never a silently short
    list."""
    store, ids, program_root, platform_root = seeded
    for i in range(249):
        insert(
            store, "launch",
            {
                "launch_id": new_id("LNCH"), "account_id": ids["account"], "program_id": "PROG-test",
                "session_id": ids["session"], "agent_kind": "tester", "model_class": "top",
                "model": "sonnet", "purpose": f"fixture {i}", "est_tokens": 100,
                "booked_ts": now(), "state": "RECONCILED", "reconciled_ts": now(),
            },
        )
    store.close()

    timeline = panel(data.build_session_panel, program_root, platform_root)["open_session"]["timeline"]
    assert len(timeline["spans"]) == data.MAX_TIMELINE_SPANS
    # 250 launches + the fixture's one job = 251 spans before the cap
    assert timeline["truncated"]["spans_dropped"] == 251 - data.MAX_TIMELINE_SPANS


# ---------------------------------------------------------------------------
# gates panel (sweep §3.10 item 3; test 31)
# ---------------------------------------------------------------------------


def test_gates_panel_pending_edits_decoded_with_unverified_count(seeded):
    """The worklist the rail badge counts must not be a string the client
    has to parse. ``unverified_count`` is a property of the array, computed
    once, on the server."""
    store, ids, program_root, platform_root = seeded
    update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={
            "state": "gated",
            "edits": json.dumps([
                {"edit_id": "E1", "note": "fix the citation", "verified": True},
                {"edit_id": "E2", "note": "re-run the check", "verified": False},
            ]),
        },
    )
    store.close()

    p = panel(data.build_gates_panel, program_root, platform_root)
    entry = p["pending_edits"][0]
    assert isinstance(entry["edits"], list)
    assert entry["edits"][0]["edit_id"] == "E1"
    assert entry["unverified_count"] == 1
