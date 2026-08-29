"""Unit tests for the panel data-builders (``trialerror/dashboard/data.py``)
against a fixture store populated via ``tests._store_fixtures.
populate_one_of_everything`` -- one valid row per table, so every panel has
something real to report on. Each builder is exercised twice: once against
a seeded ``RoStore`` (the "ok" shape) and once against an empty/uninitialized
one (the "not_initialized" shape) -- mirrors the doctor-check convention of
never crashing on a program that doesn't exist yet.
"""

from __future__ import annotations

import pytest

from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.stores.store import open_store
from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def seeded(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    yield rostore, ids
    rostore.close()


@pytest.fixture()
def empty_rostore(tmp_path):
    # neither open_store() nor populate_one_of_everything() ever ran here
    # -- every DB file is genuinely absent.
    rostore = open_store_ro(tmp_path / "no-program", platform_root=tmp_path / "no-platform")
    yield rostore
    rostore.close()


# ---------------------------------------------------------------------------
# session panel
# ---------------------------------------------------------------------------
def test_session_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_session_panel(rostore)
    assert panel["status"] == "ok"
    open_session = panel["open_session"]
    assert open_session is not None
    assert open_session["session_id"] == ids["session"]
    assert open_session["account_id"] == ids["account"]
    assert open_session["boot_bundle_stats"]["boot_pin_version"] is None
    assert open_session["close_readiness"] is not None
    assert "ready" in open_session["close_readiness"]
    assert open_session["unread_inbox_count"] >= 1  # the fixture's one inbox_item is unread
    assert isinstance(open_session["active_jobs_count"], int)
    assert any(s["session_id"] == ids["session"] for s in panel["recent_sessions"])


def test_session_panel_not_initialized(empty_rostore):
    panel = data.build_session_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# budget panel
# ---------------------------------------------------------------------------
def test_budget_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_budget_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["launch_state_counts_total"] == {"PROVISIONAL": 1}
    accounts = panel["accounts"]
    assert len(accounts) == 1
    acct = accounts[0]
    assert acct["account"]["account_id"] == ids["account"]
    assert acct["launch_state_counts"] == {"PROVISIONAL": 1}
    pools = acct["budget_status"]["pools"]
    assert len(pools) == 1
    assert pools[0]["model_class"] == "top"
    assert pools[0]["headroom_tokens"] >= 0
    # fixture's one launch is fresh (booked_ts=now(), default 3600s TTL) --
    # not yet past its booking TTL, so it must NOT show up as dangling.
    assert panel["dangling_bookings"] == []


def test_budget_panel_not_initialized(empty_rostore):
    panel = data.build_budget_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# jobs panel
# ---------------------------------------------------------------------------
def test_jobs_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_jobs_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["state_counts"] == {"pending": 1}
    assert panel["live_jobs"] == []  # the fixture job is 'pending', not claimed/running
    assert panel["stale_leases"] == []
    assert len(panel["recent_jobs"]) == 1
    assert panel["recent_jobs"][0]["job_id"] == ids["job"]


def test_jobs_panel_not_initialized(empty_rostore):
    panel = data.build_jobs_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# gates panel
# ---------------------------------------------------------------------------
def test_gates_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_gates_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["gate_state_counts"] == {"draft": 1}
    assert panel["gate_verdict_counts"] == {"__null__": 1}
    assert panel["reproduction_status_counts"] == {"__null__": 1}
    assert panel["pending_edits"] == []  # fixture gate.edits is NULL
    assert len(panel["recent_transitions"]) == 1
    assert panel["recent_transitions"][0]["gate_id"] == ids["gate"]
    assert panel["artifact_status_counts"] == {"draft": 1}
    assert len(panel["recent_artifacts"]) == 1
    assert panel["recent_artifacts"][0]["artifact_id"] == ids["artifact"]


def test_gates_panel_pending_edits_detected(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    from trialerror.stores.writer import update

    update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={"edits": '[{"note": "fix the thing"}]', "state": "submitted"},
    )
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_gates_panel(rostore)
    finally:
        rostore.close()
    assert len(panel["pending_edits"]) == 1
    assert panel["pending_edits"][0]["gate_id"] == ids["gate"]


def test_gates_panel_not_initialized(empty_rostore):
    panel = data.build_gates_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# corpus panel
# ---------------------------------------------------------------------------
def test_corpus_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_corpus_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["counts"] == {"sources": 1, "documents": 1, "chunks": 1, "quote_anchors": 1}
    assert panel["license_tier_counts"] == {"open": 1}
    assert panel["request_state_counts"] == {"indexed": 1}
    assert panel["document_status_counts"] == {"indexed": 1}
    assert panel["summary_coverage"] == {"documents_with_current_summary": 1, "total_documents": 1}
    # fixture's record row uses register_key="test-register", not the KG
    # extraction pipeline's own EXTRACT_REGISTER_KEY -- no pending backlog;
    # its one entity is resolution='draft' (not 'confirmed'), but its one
    # relation and one claim are both live (expired_at IS NULL).
    assert panel["extract_coverage"] == {
        "pending_records": 0, "confirmed_entities": 0, "live_relations": 1, "live_claims": 1,
    }
    assert panel["stale_anchors"] == 0  # fixture's anchor.doc_sha256 matches document.sha256


def test_corpus_panel_not_initialized(empty_rostore):
    panel = data.build_corpus_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# doctor panel
# ---------------------------------------------------------------------------
def test_doctor_panel_never_run():
    panel = data.build_doctor_panel(None)
    assert panel["status"] == "never_run"


def test_doctor_panel_reports_last_run():
    fake_state = {"schema": "trialerror-dashboard-doctor-state@v1", "summary": {"total": 3, "passed": 3}}
    panel = data.build_doctor_panel(fake_state)
    assert panel["status"] == "ok"
    assert panel["last_run"] == fake_state


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------
def test_build_all_panels_has_every_panel(seeded):
    rostore, _ids = seeded
    panels = data.build_all_panels(rostore, doctor_state=None)
    assert set(panels) == {
        "session", "budget", "jobs", "gates", "corpus", "doctor",
        # build-v2dash-data: the V2 dashboard's new panel builders.
        "feed", "rooms", "determinations", "dossier", "lexicon", "course", "since_you_left",
    }
    for name, panel in panels.items():
        assert "status" in panel, f"{name} panel missing a status field"
