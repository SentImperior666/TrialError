"""Unit tests for the V2-dashboard panel data-builders added in
build-v2dash-data (``trialerror/dashboard/data.py``): ``feed``, ``rooms``,
``determinations``, ``dossier``, ``lexicon``, ``course``, ``since_you_left``,
and the ``run_search`` endpoint wrapper. Follows ``tests/test_dashboard_data.py``'s
established pattern (fixture-seeded ``RoStore`` for the "ok" shape, an empty
one for "not_initialized") -- kept in a separate file rather than appended to
that one, scoped to this build's own seven builders.
"""

from __future__ import annotations

import json

import pytest

from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.rooms.api import create_room, freeze_room, post_message, score_dp
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert as store_insert
from trialerror.stores.writer import update as store_update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
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
    rostore = open_store_ro(tmp_path / "no-program", platform_root=tmp_path / "no-platform")
    yield rostore
    rostore.close()


def _reopen_ro(program_root, platform_root):
    return open_store_ro(program_root, platform_root=platform_root)


# ---------------------------------------------------------------------------
# feed panel
# ---------------------------------------------------------------------------
def test_feed_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_feed_panel(rostore)
    assert panel["status"] == "ok"
    assert any(t["thread_id"] == ids["thread"] for t in panel["threads"])
    assert panel["active_thread_id"] == ids["thread"]
    assert len(panel["posts"]) == 1
    post = panel["posts"][0]
    assert post["post_id"] == ids["feed_post"]
    assert post["kind"] == "launch"  # fixture author is "launch:<LNCH-...>"
    # the shared fixture now seeds one 'current' translation for this exact
    # post (schema-roundtrip coverage of ops_v4's feed_post_translation).
    assert post["translation"] is not None
    assert post["translation"]["translation_id"] == ids["feed_post_translation"]
    assert post["translation"]["body"] == "test translation body"
    assert panel["translator_table_available"] is True  # v4 migration ran via open_store()
    assert any(item["item_id"] == ids["inbox_item"] for item in panel["unread_directives"])


def test_feed_panel_explicit_thread_id_with_no_posts(seeded, program_root, platform_root):
    rostore, ids = seeded
    store = open_store(program_root, platform_root=platform_root)
    from trialerror.events.api import create_thread

    empty_thread = create_thread(store, title="empty thread", launch_id=ids["launch"])
    store.close()
    rostore.close()
    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_feed_panel(rostore, thread_id=empty_thread["thread_id"])
        assert panel["active_thread_id"] == empty_thread["thread_id"]
        assert panel["posts"] == []
    finally:
        rostore.close()


def test_feed_panel_surfaces_current_translation(seeded, program_root, platform_root):
    rostore, ids = seeded
    store = open_store(program_root, platform_root=platform_root)
    # the shared fixture already seeds one 'current' translation for this
    # post -- supersede it first so there is exactly one 'current' row,
    # matching the real versioned-row-chain contract (AISPEAK design
    # Section 4.2) instead of two same-status rows racing on created_ts.
    store_update(
        store, "feed_post_translation", pk_column="translation_id", pk_value=ids["feed_post_translation"],
        changes={"status": "superseded"},
    )
    store_insert(
        store,
        "feed_post_translation",
        {
            "translation_id": new_id("XLAT"),
            "post_id": ids["feed_post"],
            "translator_version": "2",
            "style_mode": "flavored",
            "body": "plain english body",
            "original_sha256": "a" * 64,
            "status": "current",
            "supersedes": ids["feed_post_translation"],
            "created_ts": now(),
        },
    )
    store.close()
    rostore.close()
    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_feed_panel(rostore)
        translation = panel["posts"][0]["translation"]
        assert translation is not None
        assert translation["body"] == "plain english body"
        assert translation["style_mode"] == "flavored"
    finally:
        rostore.close()


def test_feed_panel_not_initialized(empty_rostore):
    panel = data.build_feed_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# rooms panel
# ---------------------------------------------------------------------------
def test_rooms_panel_series_and_moderator_events(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    room = create_room(
        store,
        topic="does IDEA-1 cover the family",
        discussion_points=[{"dp_id": "DP1", "prompt": "does it cover?"}],
        participants=["p1", "p2"],
        by_launch=ids["launch"],
    )
    post_message(store, room_id=room["room_id"], launch_id=ids["launch"], dp_id="DP1", body="round 1 position")
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: {"agreement_pct": 50.0, "note": "not yet"}, by_launch=ids["launch"])
    post_message(store, room_id=room["room_id"], launch_id=ids["launch"], dp_id="DP1", body="round 2 position")
    score_dp(store, room_id=room["room_id"], dp_id="DP1", judge=lambda env: {"agreement_pct": 95.0}, by_launch=ids["launch"])
    store.close()

    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_rooms_panel(rostore, room_id=room["room_id"])
        assert panel["status"] == "ok"
        assert panel["detail_error"] is None
        assert panel["active_room_id"] == room["room_id"]
        assert len(panel["turns"]) == 2
        assert panel["convergence"]["all_converged"] is True
        # the TRAJECTORY, not just the latest score -- two scoring rounds,
        # both present in order.
        series = panel["dp_agreement_series"]["DP1"]
        assert [s["agreement_pct"] for s in series] == [50.0, 95.0]
        assert series[0]["converged"] is False
        assert series[1]["converged"] is True
        mod_types = [e["type"] for e in panel["moderator_events"]]
        assert mod_types == ["room_created", "room_turn", "room_dp_scored", "room_turn", "room_dp_scored"]
    finally:
        rostore.close()


def test_rooms_panel_freeze_reason_surfaced(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    room = create_room(
        store, topic="t", discussion_points=[{"dp_id": "DP1", "prompt": "p"}],
        participants=["p1", "p2"], by_launch=ids["launch"],
    )
    freeze_room(store, room_id=room["room_id"], by_launch=ids["launch"], reason="stuck on DP1")
    store.close()

    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_rooms_panel(rostore, room_id=room["room_id"])
        assert panel["active_room"]["state"] == "frozen"
        assert panel["freeze_reason"] == "stuck on DP1"
    finally:
        rostore.close()


def test_rooms_panel_defaults_to_an_open_room_over_a_converged_one(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    converged_room = create_room(
        store, topic="closed", discussion_points=[{"dp_id": "DP1", "prompt": "p"}],
        participants=["p1", "p2"], by_launch=ids["launch"],
    )
    score_dp(store, room_id=converged_room["room_id"], dp_id="DP1", judge=lambda env: {"agreement_pct": 99.0}, by_launch=ids["launch"])
    from trialerror.rooms.api import converge_room

    converge_room(store, room_id=converged_room["room_id"], by_launch=ids["launch"])
    open_room = create_room(
        store, topic="still open", discussion_points=[{"dp_id": "DP1", "prompt": "p"}],
        participants=["p1", "p2"], by_launch=ids["launch"],
    )
    store.close()

    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_rooms_panel(rostore)  # no room_id given
        assert panel["active_room_id"] == open_room["room_id"]
        assert {r["room_id"] for r in panel["rooms"]} >= {converged_room["room_id"], open_room["room_id"], ids["room"]}
    finally:
        rostore.close()


def test_rooms_panel_not_initialized(empty_rostore):
    panel = data.build_rooms_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# determinations panel
# ---------------------------------------------------------------------------
def test_determinations_panel_all_kinds_present(seeded, program_root, platform_root):
    rostore, ids = seeded
    store = open_store(program_root, platform_root=platform_root)

    store_update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={
            "state": "submitted",
            "edits": json.dumps([{"edit_id": "E1", "text": "fix the tally", "blocking": True, "verified": False}]),
            "critic_launch": ids["launch"],
            "verdict_ts": now(),
            "reproduction_status": "unrun",
        },
    )
    store_insert(
        store, "source",
        {
            "source_id": new_id("SRC"), "kind": "paper", "title": "wanted paper",
            "license_tier": "unknown", "acquisition_route": "web", "request_state": "wanted",
            "registered_ts": now(), "registered_by_launch": ids["launch"],
        },
    )
    freeze_room(store, room_id=ids["room"], by_launch=ids["launch"], reason="deadlocked")
    left = f"MEM-G1{'::left'}"
    right = f"MEM-G1{'::right'}"
    for item_id, side in ((left, "left"), (right, "right")):
        store_insert(
            store, "memory_item",
            {
                "memory_item_id": item_id, "key": "some-rule", "tier": "L0", "kind": "rule",
                "body": f"{side} version", "updated_ts": now(), "status": "needs_merge",
            },
        )
    store.close()

    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_determinations_panel(rostore)
        assert panel["status"] == "ok"
        kinds = panel["counts_by_kind"]
        assert kinds.get("gate_edit") == 1
        assert kinds.get("kg_merge") == 1  # fixture's draft merge_proposal
        assert kinds.get("acquisition") == 1  # the new 'wanted' source
        assert kinds.get("prereg_reveal") == 1  # fixture's committed prereg
        assert kinds.get("room_escalation") == 1
        assert kinds.get("memory_conflict") == 1
        assert panel["blocking_count"] >= 2  # gate_edit + room_escalation are blocking=True

        gate_item = next(i for i in panel["items"] if i["kind"] == "gate_edit")
        assert gate_item["gate_id"] == ids["gate"]
        assert "union_applied" in gate_item["consequence"] or "registration" in gate_item["consequence"]

        acq_item = next(i for i in panel["items"] if i["kind"] == "acquisition")
        assert "requested" in acq_item["consequence"] or "rejected" in acq_item["consequence"]
    finally:
        rostore.close()


def test_determinations_panel_not_initialized(empty_rostore):
    panel = data.build_determinations_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


def test_determinations_panel_ok_when_knowledge_db_absent(tmp_path, monkeypatch):
    """ops.db exists (a program that has done SOME setup) but knowledge.db
    does not yet -- the kg_merge and acquisition kinds both read
    knowledge.db; this must degrade to reporting zero of those two kinds,
    never crash the whole queue (regression: _kg_merge_items originally
    called trialerror.ingest.extract.list_pending unconditionally, which raises
    AttributeError on a RoStore whose .knowledge connection is None)."""
    from trialerror.stores import paths as store_paths
    from trialerror.stores.connection import connect
    from trialerror.stores.migrate import apply_migrations
    from trialerror.stores.schema import ops as ops_schema

    platform_root = tmp_path / "platform"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir()

    ops_path = store_paths.ops_db_path(program_root)
    ops_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(ops_path)
    apply_migrations(conn, ops_schema.MIGRATIONS)
    conn.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        assert rostore.is_available("ops") and not rostore.is_available("knowledge")
        panel = data.build_determinations_panel(rostore)
        assert panel["status"] == "ok"
        assert panel["counts_by_kind"].get("kg_merge", 0) == 0
        assert panel["counts_by_kind"].get("acquisition", 0) == 0
    finally:
        rostore.close()


# ---------------------------------------------------------------------------
# dossier panel
# ---------------------------------------------------------------------------
def test_dossier_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_dossier_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["active_artifact_id"] == ids["artifact"]
    assert panel["artifact"]["artifact_id"] == ids["artifact"]
    assert any(t["type_key"] == "note" for t in panel["type_filters"])
    # the fixture's artifact row never sets gate_id (populate_one_of_everything
    # inserts `gate` referencing `artifact_id`, but never links it back) --
    # honestly reported as no gate, not fabricated.
    assert panel["gate"] is None
    assert panel["gate_history"] == []
    assert panel["verdicts"] == []  # fixture's one verdict is subject_kind='hypothesis', not 'artifact'
    assert [v["artifact_id"] for v in panel["version_chain"]] == [ids["artifact"]]
    assert panel["lineage"]["produced_by_launch"]["launch_id"] == ids["launch"]
    assert panel["lineage"]["registers_records"] == 1  # fixture's record.artifact_id == artifact_id
    # the shared fixture now seeds one criterion ("G-01") discharged by this
    # exact artifact (schema-roundtrip coverage of ops_v4's criterion table).
    assert [c["criterion_id"] for c in panel["lineage"]["discharges_criteria"]] == [ids["criterion"]]
    assert "prov_edge" in panel["lineage"]["note"]


def test_dossier_panel_version_chain_and_criterion_discharge(seeded, program_root, platform_root):
    rostore, ids = seeded
    store = open_store(program_root, platform_root=platform_root)
    newer_id = new_id("ART")
    store_insert(
        store, "artifact",
        {
            "artifact_id": newer_id, "type": ids["template"], "title": "v2", "path": "artifacts/v2.md",
            "sha256": "9" * 64, "status": "registered", "registered_ts": now(),
            "registered_by_launch": ids["launch"], "supersedes": ids["artifact"],
        },
    )
    # G-01 already exists (the shared fixture's own criterion row, also
    # discharged by ids["artifact"]) -- this test adds a SECOND one to prove
    # the panel lists every criterion an artifact discharges, not just one.
    store_insert(
        store, "criterion",
        {"criterion_id": "G-02", "label": "hole viability", "phase": "ideation", "state": "discharged", "discharged_by_artifact": ids["artifact"]},
    )
    store.close()

    rostore.close()
    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_dossier_panel(rostore, artifact_id=ids["artifact"])
        chain_ids = {v["artifact_id"] for v in panel["version_chain"]}
        assert chain_ids == {ids["artifact"], newer_id}
        assert panel["lineage"]["superseded_by"] == [newer_id]
        assert {c["criterion_id"] for c in panel["lineage"]["discharges_criteria"]} == {ids["criterion"], "G-02"}
    finally:
        rostore.close()


def test_dossier_panel_not_initialized(empty_rostore):
    panel = data.build_dossier_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# lexicon panel
# ---------------------------------------------------------------------------
def test_lexicon_panel_ok(seeded):
    rostore, ids = seeded
    panel = data.build_lexicon_panel(rostore)
    assert panel["status"] == "ok"
    assert len(panel["entities"]) == 2
    assert any(e["entity_id"] == ids["entity"] and e["relation_count"] == 1 for e in panel["entities"])
    assert panel["definition_claims"] == []  # fixture's claim kind='finding'
    assert panel["claim_kind_counts"] == {"finding": 1}
    assert len(panel["merge_proposals_draft"]) == 1
    assert panel["contradiction_edges"] == []  # prov_edge has zero writers / fixture role='supports'
    assert "term_sense" in panel["seam_note"]


def test_lexicon_panel_definition_claim_surfaced(seeded, program_root, platform_root):
    rostore, ids = seeded
    store = open_store(program_root, platform_root=platform_root)
    anchor_id = new_id("ANC")
    store_insert(
        store, "quote_anchor",
        {
            "anchor_id": anchor_id, "doc_id": ids["document"], "chunk_id": ids["chunk"],
            "page_number": 1, "char_start": 0, "char_end": 5, "doc_sha256": "5" * 64,
            "quote_sha256": "8" * 64, "quote_text": "a defined term is X",
            "created_by_launch": ids["launch"], "created_ts": now(),
        },
    )
    store_insert(
        store, "claim",
        {
            "claim_id": new_id("CLM"), "text": "term means X", "kind": "definition",
            "anchor_id": anchor_id, "created_at": now(), "created_by_launch": ids["launch"],
        },
    )
    store.close()
    rostore.close()
    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_lexicon_panel(rostore)
        assert len(panel["definition_claims"]) == 1
        assert panel["definition_claims"][0]["quote_text"] == "a defined term is X"
    finally:
        rostore.close()


def test_lexicon_panel_not_initialized(empty_rostore):
    panel = data.build_lexicon_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# course panel
# ---------------------------------------------------------------------------
def test_course_panel_awaiting_migration_before_v4(tmp_path, monkeypatch):
    import sqlite3 as _sqlite3

    from trialerror.stores import paths as store_paths
    from trialerror.stores.connection import connect
    from trialerror.stores.migrate import apply_migrations
    from trialerror.stores.schema import ops as ops_schema

    platform_root = tmp_path / "platform"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir()

    ops_path = store_paths.ops_db_path(program_root)
    ops_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(ops_path)
    v1v2v3 = tuple(m for m in ops_schema.MIGRATIONS if m.version <= 3)
    apply_migrations(conn, v1v2v3)
    conn.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_course_panel(rostore)
        assert panel["status"] == "awaiting_migration"
    finally:
        rostore.close()


def test_course_panel_ok_with_fixture_criterion(seeded):
    rostore, ids = seeded
    panel = data.build_course_panel(rostore)
    assert panel["status"] == "ok"
    # the shared fixture now seeds one criterion row ("G-01"/"test-phase",
    # discharged by the fixture artifact) -- schema-roundtrip coverage.
    assert [c["criterion_id"] for c in panel["criteria"]] == [ids["criterion"]]
    assert panel["criteria"][0]["discharged_by_artifact_title"] == "test artifact"
    assert panel["phases"] == [{"phase": "test-phase", "total": 1, "open": 0, "blocked": 0, "discharged": 1}]
    assert panel["drift_log"] == []  # fixture's session.course_check IS NULL


def test_course_panel_criteria_phases_and_drift(seeded, program_root, platform_root):
    rostore, ids = seeded
    store = open_store(program_root, platform_root=platform_root)
    # ids["criterion"] ("G-01"/"test-phase") is the shared fixture's own
    # row, already present -- this test adds three more under different ids
    # spanning two more phases.
    store_insert(store, "criterion", {"criterion_id": "G-02", "label": "breadth", "phase": "ideation", "state": "discharged", "discharged_by_artifact": ids["artifact"]})
    store_insert(store, "criterion", {"criterion_id": "G-03", "label": "hole viability", "phase": "ideation", "state": "open"})
    store_insert(store, "criterion", {"criterion_id": "G-04", "label": "coverage proof", "phase": "derivation", "state": "blocked"})
    store_update(store, "session", pk_column="session_id", pk_value=ids["session"], changes={"closed_ts": now(), "course_check": json.dumps({"on_course": True, "note": "traces to CH-001"})})
    store.close()

    rostore.close()
    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_course_panel(rostore)
        assert panel["status"] == "ok"
        assert len(panel["criteria"]) == 4
        g02 = next(c for c in panel["criteria"] if c["criterion_id"] == "G-02")
        assert g02["discharged_by_artifact_title"] == "test artifact"
        assert panel["phases"] == [
            {"phase": "test-phase", "total": 1, "open": 0, "blocked": 0, "discharged": 1},
            {"phase": "ideation", "total": 2, "open": 1, "blocked": 0, "discharged": 1},
            {"phase": "derivation", "total": 1, "open": 0, "blocked": 1, "discharged": 0},
        ]
        assert len(panel["drift_log"]) == 1
        assert panel["drift_log"][0]["source"] == "session_close"
        assert panel["drift_log"][0]["course_check"]["on_course"] is True
    finally:
        rostore.close()


def test_course_panel_not_initialized(empty_rostore):
    panel = data.build_course_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# since_you_left panel
# ---------------------------------------------------------------------------
def test_since_you_left_24h_fallback_when_no_closed_session(seeded):
    rostore, _ids = seeded
    panel = data.build_since_you_left_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["since_source"] == "24h_fallback"
    kinds = {i["kind"] for i in panel["items"]}
    assert "feed_post" in kinds
    assert "artifact_registered" not in kinds  # fixture's artifact.registered_ts IS NULL (still draft)


def test_since_you_left_explicit_since_excludes_everything_in_the_future(seeded):
    rostore, _ids = seeded
    from datetime import timedelta

    from trialerror.util.timeutil import now_dt

    far_future = (now_dt() + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    panel = data.build_since_you_left_panel(rostore, since=far_future)
    assert panel["items"] == []
    assert panel["since_source"] == "given"


def test_since_you_left_last_session_close_default(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    # captured strictly after every fixture row (including feed_post) was
    # written, so it is guaranteed >= the fixture post's own `ts` -- and
    # `after_close_ts` is a fixed constant far enough in the future that no
    # wall-clock jitter can put it before `close_ts`.
    close_ts = now()
    after_close_ts = "2099-01-01T00:00:00.000Z"
    store_update(store, "session", pk_column="session_id", pk_value=ids["session"], changes={"status": "closed", "closed_ts": close_ts})
    from trialerror.events.api import post_feed

    new_post = post_feed(store, thread_id=ids["thread"], body="posted after close", launch_id=ids["launch"], ts=after_close_ts)
    store.close()

    rostore = _reopen_ro(program_root, platform_root)
    try:
        panel = data.build_since_you_left_panel(rostore)
        assert panel["since_source"] == "last_session_close"
        post_ids = {i["ref"]["post_id"] for i in panel["items"] if i["kind"] == "feed_post"}
        assert new_post["post_id"] in post_ids
        assert ids["feed_post"] not in post_ids  # posted before the close ts
    finally:
        rostore.close()


def test_since_you_left_not_initialized(empty_rostore):
    panel = data.build_since_you_left_panel(empty_rostore)
    assert panel["status"] == "not_initialized"


# ---------------------------------------------------------------------------
# run_search
# ---------------------------------------------------------------------------
def test_run_search_not_initialized(empty_rostore):
    result = data.run_search(empty_rostore, query="anything")
    assert result["status"] == "not_initialized"


def test_run_search_invalid_mode(seeded):
    rostore, _ids = seeded
    result = data.run_search(rostore, query="hello", mode="not-a-real-mode")
    assert result["status"] == "invalid_mode"


def test_run_search_empty_query_returns_empty_ok(seeded):
    rostore, _ids = seeded
    result = data.run_search(rostore, query="")
    assert result["status"] == "ok"
    assert result["results"] == []


def test_run_search_k_is_clamped_to_max(seeded):
    rostore, _ids = seeded
    result = data.run_search(rostore, query="", k=10_000)
    assert result["status"] == "ok"  # never raises for an over-large k


def test_run_search_fts_hit_end_to_end(seeded, program_root, platform_root):
    rostore, ids = seeded
    store = open_store(program_root, platform_root=platform_root)
    # the fixture's raw `chunk` insert does not populate the chunk_fts
    # virtual table (only the real ingest pipeline does) -- insert directly
    # so this test proves run_search's real wiring end to end rather than
    # only its not_initialized/invalid_mode/empty-query guard paths.
    store.knowledge.execute("INSERT INTO chunk_fts (chunk_id, text) VALUES (?, ?)", (ids["chunk"], "hello world"))
    store.knowledge.commit()
    store.close()

    rostore.close()
    rostore = _reopen_ro(program_root, platform_root)
    try:
        result = data.run_search(rostore, query="hello", k=5, mode="fts")
        assert result["status"] == "ok"
        assert "fts" in result["tiers_used"]
        assert len(result["results"]) == 1
        row = result["results"][0]
        assert row["chunk_id"] == ids["chunk"]
        assert row["citation"]["source_id"] == ids["source"]
        assert row["fenced"] is False  # fixture source license_tier='open'
    finally:
        rostore.close()
