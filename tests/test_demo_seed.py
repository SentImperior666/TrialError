"""Tests for ``trialerror demo seed``.

The demo's whole job is to make the dashboard non-empty, so that is what
these assert -- not "the seeder ran without raising", but "every panel
builder returns ``ok`` with data in it". A seeder that silently stopped
populating half the panels would still exit 0, and nothing else in the suite
would notice.

Several assertions here are pinned to specific states that are easy to seed
*almost* right, and that failed silently the first time:

- two sessions, one open and one closed. ``close_session`` REFUSES rather
  than raising, so an unchecked call left a single open session pretending
  to be two.
- a dangling booking. "Dangling" is ``booked_ts + booking_ttl_s < now``, not
  merely ``PROVISIONAL``, so a freshly-booked launch is healthy and the panel
  correctly reported none.
- a non-empty ``since_you_left``. Its cursor is the most recent
  ``session.closed_ts``, so closing a session *after* seeding hides
  everything behind it.
- the demo's own platform root. ``account`` and ``budget_pool`` live in
  platform.db, which is shared across programs -- the demo must not write
  into the real one.
"""

from __future__ import annotations

import json

import pytest

from trialerror.dashboard.data import PANEL_BUILDERS
from trialerror.dashboard.doctor_run import doctor_state_path
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.demo import SeedRefused, seed_demo_program
from trialerror.demo.seed import DEMO_PLATFORM_DIRNAME, default_platform_root
from trialerror.stores.store import open_store


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Seed once and share it -- seeding runs a real ingest pipeline and a
    real worker drain, so it is far too slow to repeat per test."""
    program_root = tmp_path_factory.mktemp("demo") / "demo-program"
    program_root.mkdir(parents=True)
    (program_root / "trialerror.toml").write_text('[program]\nid = "demo"\n', encoding="utf-8")
    result = seed_demo_program(program_root)
    return result, program_root, default_platform_root(program_root)


@pytest.fixture(scope="module")
def rostore(seeded):
    _result, program_root, platform_root = seeded
    return open_store_ro(program_root, platform_root=platform_root)


# ---------------------------------------------------------------------------
# the point of the command
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("panel", sorted(PANEL_BUILDERS))
def test_every_panel_reports_ok(panel, rostore):
    built = PANEL_BUILDERS[panel](rostore)
    assert built.get("status") == "ok", f"{panel}: {built}"


@pytest.mark.parametrize("panel", sorted(PANEL_BUILDERS))
def test_every_panel_has_data_in_it(panel, rostore):
    """An `ok` status with every collection empty is exactly the failure this
    command exists to prevent."""
    built = PANEL_BUILDERS[panel](rostore)
    populated = [
        key for key, value in built.items()
        if isinstance(value, (list, dict)) and value
    ]
    assert populated, f"{panel} rendered ok but entirely empty: {built}"


def test_doctor_sidecar_is_written(seeded):
    """The doctor panel reads a JSON sidecar, not the DB -- and `trialerror
    doctor` does not write it. Without the seeder doing so the DIAGNOSTICS
    ribbon reads NEVER RUN on a fully-populated program."""
    _result, program_root, _platform_root = seeded
    state_path = doctor_state_path(program_root)
    assert state_path.is_file(), f"no doctor state at {state_path}"
    assert json.loads(state_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# states that are easy to seed almost-right
# ---------------------------------------------------------------------------
def test_seeds_one_open_and_one_closed_session(seeded):
    result, program_root, platform_root = seeded
    assert result.open_session_id != result.closed_session_id

    store = open_store(program_root, platform_root=platform_root)
    try:
        rows = store.ops.execute("SELECT session_id, status FROM session").fetchall()
    finally:
        store.close()
    by_status = {r["status"] for r in rows}
    assert len(rows) == 2, rows
    assert by_status == {"open", "closed"}


def test_leaves_exactly_one_dangling_booking(rostore):
    """Backdated at seed time -- a booking made 'now' has an hour of TTL left
    and is not dangling, however abandoned it looks."""
    budget = PANEL_BUILDERS["budget"](rostore)
    assert len(budget["dangling_bookings"]) == 1, budget["dangling_bookings"]


def test_budget_pools_are_tracked_with_real_spend(rostore):
    budget = PANEL_BUILDERS["budget"](rostore)
    pools = [p for a in budget["accounts"] for p in a["budget_status"]["pools"]]
    assert len(pools) == 2, pools
    assert all(p["cap_tokens"] > 0 for p in pools)
    assert sum(p["spent_visible_tokens"] for p in pools) > 0, "reconciliation recorded no spend"


def test_since_you_left_is_not_empty(rostore):
    """Guards the ordering trap: the cursor is the last session.closed_ts, so
    seeding work and THEN closing a session empties the dashboard's main lane
    on a program full of data."""
    panel = PANEL_BUILDERS["since_you_left"](rostore)
    assert panel["items"], "home's main lane would render 'nothing since'"


def test_decide_queue_has_blocking_and_non_blocking_items(rostore):
    panel = PANEL_BUILDERS["determinations"](rostore)
    kinds = panel["counts_by_kind"]
    # gate_edit and room_escalation are the only two BLOCKING kinds.
    assert "gate_edit" in kinds
    assert "room_escalation" in kinds
    assert panel["blocking_count"] >= 2, panel


def test_extraction_leaves_a_review_queue_and_a_merge_proposal(rostore, seeded):
    """The never-silent-auto-merge posture: candidates land as proposals, and
    a duplicate of an already-confirmed entity becomes a merge proposal
    rather than being folded in. That only happens if the seeder accepts
    per-document instead of extracting everything first."""
    result, program_root, _platform_root = seeded
    assert result.counts["extract_pending"] > 0
    assert result.counts["merge_proposals"] > 0
    panel = PANEL_BUILDERS["determinations"](rostore)
    assert "kg_merge" in panel["counts_by_kind"]


def test_rooms_have_multi_point_agreement_trajectories(rostore):
    """A DP scored once is a dot; the trajectory chart needs a line, and it is
    built from room_dp_scored events rather than the single room_score row."""
    panel = PANEL_BUILDERS["rooms"](rostore)
    assert panel["active_room"]["state"] == "open"
    series = panel["dp_agreement_series"]
    assert series, "no agreement series -- the trajectory chart would be empty"
    assert all(len(points) >= 2 for points in series.values()), series


def test_course_panel_has_criteria_across_phases_and_a_drift_note(rostore):
    panel = PANEL_BUILDERS["course"](rostore)
    assert len(panel["criteria"]) >= 5
    assert len({c["phase"] for c in panel["criteria"]}) >= 2
    assert any(c["state"] == "discharged" for c in panel["criteria"])
    assert panel["drift_log"], "session.course_check is what fills this"


def test_corpus_spans_more_than_one_license_tier(rostore):
    """A single-tier corpus renders as one flat bar and demonstrates nothing
    about the license fence."""
    panel = PANEL_BUILDERS["corpus"](rostore)
    assert len(panel["license_tier_counts"]) >= 2, panel["license_tier_counts"]
    assert "commercial_restricted" in panel["license_tier_counts"]


# ---------------------------------------------------------------------------
# not touching the operator's real data
# ---------------------------------------------------------------------------
def test_platform_store_lives_inside_the_program(seeded):
    _result, program_root, platform_root = seeded
    assert platform_root == program_root / DEMO_PLATFORM_DIRNAME
    assert (platform_root / "platform.db").is_file()


def test_seeding_refuses_a_program_that_is_already_in_use(seeded):
    _result, program_root, _platform_root = seeded
    with pytest.raises(SeedRefused) as exc:
        seed_demo_program(program_root)
    assert "already has" in str(exc.value)


def test_force_reseeds_a_used_program(tmp_path):
    program_root = tmp_path / "demo-program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "demo"\n', encoding="utf-8")

    first = seed_demo_program(program_root)
    second = seed_demo_program(program_root, force=True)
    assert second.open_session_id != first.open_session_id

    store = open_store(program_root, platform_root=default_platform_root(program_root))
    try:
        sessions = store.ops.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]
        accounts = store.platform.execute("SELECT COUNT(*) AS n FROM account").fetchone()["n"]
    finally:
        store.close()
    # --force wipes both stores, so a reseed must not leave the program with
    # two operators or four sessions.
    assert sessions == 2, sessions
    assert accounts == 1, accounts


# ---------------------------------------------------------------------------
# the corpus must not be mistakable for real literature
# ---------------------------------------------------------------------------
def test_every_seeded_source_is_marked_as_a_demo_fixture(seeded):
    _result, program_root, platform_root = seeded
    store = open_store(program_root, platform_root=platform_root)
    try:
        titles = [r["title"] for r in store.knowledge.execute("SELECT title FROM source").fetchall()]
    finally:
        store.close()
    assert titles
    for title in titles:
        assert title.startswith("[DEMO]"), f"{title!r} could be mistaken for a real source"


def test_every_seeded_document_body_declares_itself_synthetic(seeded):
    _result, program_root, _platform_root = seeded
    raw_files = list((program_root / "raw").glob("*.md"))
    assert raw_files
    for path in raw_files:
        assert "Synthetic demo fixture" in path.read_text(encoding="utf-8"), path
