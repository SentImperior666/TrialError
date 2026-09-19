"""Mining adoption engram-F5: type-keyed staleness decay driving a
``needs_review`` surface (``docs/reviews/MINING_2026-09_OPERATOR_LINKS.md``
section 3, orchestrator verdict "adopt-now:memory as a DOCTOR CHECK
(needs_review surfacing by type-keyed age); never mutates a pin or a
ruling").

Review section 5.7 is the constraint this module was built around, so the
tests that matter most here are the negative ones:
``test_decay_never_writes_anything`` and
``test_the_doctor_check_holds_a_read_only_connection`` exist so that a
future change turning decay into a timer that expires a law fails in CI
rather than in the corrections ledger.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trialerror.memory.api import put_item
from trialerror.memory.staleness import (
    DEFAULT_HALF_LIFE_DAYS,
    HALF_LIFE_DAYS,
    REVIEW_THRESHOLD,
    freshness,
    half_life_days,
    mark_reviewed,
    review_state,
    stale_items,
    summarize,
)
from trialerror.util.timeutil import now_dt

from tests._memory_fixtures import make_account


def _ts(days_ago: float) -> str:
    dt = now_dt() - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# the decay curve
# ---------------------------------------------------------------------------


def test_every_declared_kind_has_a_half_life():
    from trialerror.memory.api import KINDS

    assert set(HALF_LIFE_DAYS) == set(KINDS)


def test_an_unknown_kind_falls_back_to_the_shortest_clock(store):
    """Surfaced sooner rather than never: an unrecognised kind (a later
    schema addition, an old import) must not become invisible to review."""
    assert half_life_days("something-new") == DEFAULT_HALF_LIFE_DAYS
    assert DEFAULT_HALF_LIFE_DAYS == min(HALF_LIFE_DAYS.values())


def test_freshness_is_exactly_one_half_at_one_half_life():
    assert freshness("rule", HALF_LIFE_DAYS["rule"]) == pytest.approx(0.5)


def test_needs_review_is_exactly_older_than_one_half_life():
    """The continuous curve and the source's discrete ``review_after``
    date must agree at the threshold, or the doctor message and the
    freshness number would tell an operator different stories."""
    hl = HALF_LIFE_DAYS["preference"]
    just_under = review_state({"kind": "preference", "updated_ts": _ts(hl - 1)})
    just_over = review_state({"kind": "preference", "updated_ts": _ts(hl + 1)})
    assert just_under["needs_review"] is False
    assert just_over["needs_review"] is True
    assert REVIEW_THRESHOLD == 0.5


def test_a_future_timestamp_reads_as_fresh_not_as_more_than_fresh():
    """A clock-skewed import must not outrank a just-written row."""
    assert freshness("rule", -500) == 1.0


def test_an_unparseable_timestamp_does_not_raise():
    """One malformed row must not break a doctor run."""
    state = review_state({"kind": "rule", "updated_ts": "not-a-timestamp"})
    assert state["needs_review"] is False


def test_a_rule_outlives_a_preference():
    """The kind mapping's whole point: a standing rule gets the longest
    clock (the source's ``policy`` offset), a preference the shortest."""
    assert HALF_LIFE_DAYS["rule"] > HALF_LIFE_DAYS["fact"] > HALF_LIFE_DAYS["preference"]


# ---------------------------------------------------------------------------
# reading the store
# ---------------------------------------------------------------------------


def test_stale_items_finds_only_the_overdue_ones(store):
    account_id = make_account(store)
    put_item(store, key="fresh-rule", tier="L0", kind="rule", body="new", account_id=account_id)
    put_item(
        store, key="ancient-preference", tier="L2", kind="preference", body="old",
        account_id=account_id, ts=_ts(HALF_LIFE_DAYS["preference"] + 30),
    )
    stale = stale_items(store)
    assert [s["key"] for s in stale] == ["ancient-preference"]


def test_stale_items_are_ordered_most_overdue_first(store):
    account_id = make_account(store)
    put_item(store, key="a-pref", tier="L2", kind="preference", body="x", account_id=account_id, ts=_ts(200))
    put_item(store, key="b-pref", tier="L2", kind="preference", body="y", account_id=account_id, ts=_ts(400))
    assert [s["key"] for s in stale_items(store)] == ["b-pref", "a-pref"]


def test_stale_items_ignores_non_active_rows(store):
    account_id = make_account(store)
    row = put_item(store, key="retired", tier="L2", kind="preference", body="x", account_id=account_id, ts=_ts(400))
    store.ops.execute(
        "UPDATE memory_item SET status = 'superseded' WHERE memory_item_id = ?", (row["memory_item_id"],)
    )
    store.ops.commit()
    assert stale_items(store) == []


def test_stale_items_accepts_a_bare_connection(store):
    """The doctor check and the dashboard hold different handles."""
    account_id = make_account(store)
    put_item(store, key="old-pref", tier="L2", kind="preference", body="x", account_id=account_id, ts=_ts(400))
    assert [s["key"] for s in stale_items(store.ops)] == ["old-pref"]


def test_summarize_counts_by_kind(store):
    account_id = make_account(store)
    put_item(store, key="p1", tier="L2", kind="preference", body="x", account_id=account_id, ts=_ts(400))
    put_item(store, key="p2", tier="L2", kind="preference", body="y", account_id=account_id, ts=_ts(400))
    put_item(store, key="i1", tier="L0", kind="index", body="z", account_id=account_id, ts=_ts(400))
    assert summarize(stale_items(store)) == {"count": 3, "by_kind": {"preference": 2, "index": 1}}


# ---------------------------------------------------------------------------
# review section 5.7: decay surfaces, never mutates
# ---------------------------------------------------------------------------


def test_decay_never_writes_anything(store):
    """THE constraint. Reading staleness must leave the store
    byte-identical -- a decay pass that quietly updated a row would be the
    silent law mutation review section 5.7 forbids."""
    account_id = make_account(store)
    put_item(store, key="ancient-rule", tier="L0", kind="rule", body="x", account_id=account_id, ts=_ts(2000))
    before = store.ops.execute("SELECT * FROM memory_item ORDER BY memory_item_id").fetchall()
    before_rows = [dict(r) for r in before]

    stale_items(store)
    stale_items(store)
    review_state(before_rows[0])

    after = [dict(r) for r in store.ops.execute("SELECT * FROM memory_item ORDER BY memory_item_id").fetchall()]
    assert after == before_rows


def test_a_fully_decayed_item_is_still_active_and_unchanged(store):
    """Nothing expires, unpins, or downgrades on a timer."""
    account_id = make_account(store)
    row = put_item(store, key="very-old-rule", tier="L0", kind="rule", body="x", account_id=account_id, ts=_ts(5000))
    assert stale_items(store)[0]["freshness"] < 0.001
    fetched = store.ops.execute(
        "SELECT status, tier, body FROM memory_item WHERE memory_item_id = ?", (row["memory_item_id"],)
    ).fetchone()
    assert (fetched["status"], fetched["tier"], fetched["body"]) == ("active", "L0", "x")


def test_mark_reviewed_resets_the_clock_without_editing_the_item(store):
    """"I looked and left it alone" must be expressible. Without it the
    only way to reset a clock would be a pointless edit to the body."""
    account_id = make_account(store)
    row = put_item(store, key="old-rule", tier="L0", kind="rule", body="unchanged", account_id=account_id, ts=_ts(2000))
    assert stale_items(store)

    reviewed = mark_reviewed(store, row["memory_item_id"])
    assert reviewed["reviewed_ts"]
    assert stale_items(store) == []

    after = store.ops.execute(
        "SELECT updated_ts, body, tier, kind, status FROM memory_item WHERE memory_item_id = ?",
        (row["memory_item_id"],),
    ).fetchone()
    assert after["updated_ts"] == row["updated_ts"]  # NOT an edit
    assert after["body"] == "unchanged"
    assert after["status"] == "active"


def test_a_review_eventually_decays_too(store):
    account_id = make_account(store)
    row = put_item(store, key="old-pref", tier="L2", kind="preference", body="x", account_id=account_id, ts=_ts(2000))
    mark_reviewed(store, row["memory_item_id"], ts=_ts(HALF_LIFE_DAYS["preference"] + 10))
    stale = stale_items(store)
    assert [s["key"] for s in stale] == ["old-pref"]
    assert stale[0]["last_touched_was_review"] is True


def test_mark_reviewed_refuses_an_unknown_id(store):
    with pytest.raises(ValueError, match="no memory_item"):
        mark_reviewed(store, "MEM-nope")


# ---------------------------------------------------------------------------
# the doctor check
# ---------------------------------------------------------------------------


def _doctor(program_root, names):
    from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    return {r.name: r for r in run_checks(ctx, only=names)}


def test_memory_stale_items_check_passes_on_a_fresh_store(store, program_root):
    account_id = make_account(store)
    put_item(store, key="fresh", tier="L0", kind="rule", body="new", account_id=account_id)
    result = _doctor(program_root, ["memory_stale_items"])["memory_stale_items"]
    assert result.status == "pass"
    assert result.details["count"] == 0


def test_memory_stale_items_check_warns_never_fails(store, program_root):
    """``warn``, not ``fail``: an item nobody has looked at in a year is a
    prompt, not a broken store, and failing here would block every gate
    that wants a clean doctor run over a judgement call."""
    account_id = make_account(store)
    put_item(store, key="ancient-rule", tier="L0", kind="rule", body="x", account_id=account_id, ts=_ts(2000))
    result = _doctor(program_root, ["memory_stale_items"])["memory_stale_items"]
    assert result.status == "warn"
    assert result.details["count"] == 1
    assert "ancient-rule" in result.message


def test_the_doctor_check_holds_a_read_only_connection(store, program_root):
    """The strongest available guarantee that decay cannot mutate: the
    check's own handle refuses writes at the driver."""
    import sqlite3

    from trialerror.memory.checks import _ops_conn_or_none
    from trialerror.util.doctor import DoctorContext

    conn = _ops_conn_or_none(DoctorContext(program_root=program_root))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE memory_item SET status = 'superseded'")
    finally:
        conn.close()


def test_pending_candidate_check_warns_when_candidates_are_unjudged(store, program_root):
    account_id = make_account(store)
    put_item(
        store, key="spawn-booking-rule", tier="L0", kind="rule",
        body="Every agent run is booked in the spend ledger before spawn.", account_id=account_id,
    )
    put_item(
        store, key="spawn-booking-restated", tier="L0", kind="rule",
        body="Every agent run is booked in the spend ledger before spawn.", account_id=account_id,
    )
    result = _doctor(program_root, ["memory_pending_conflict_candidates"])["memory_pending_conflict_candidates"]
    assert result.status == "warn"
    assert result.details["count"] >= 1


def test_the_dashboard_determinations_panel_carries_both_new_kinds(store, program_root, platform_root):
    from trialerror.dashboard.data import build_determinations_panel
    from trialerror.dashboard.store_ro import open_store_ro

    account_id = make_account(store)
    put_item(store, key="ancient-rule", tier="L0", kind="rule", body="x", account_id=account_id, ts=_ts(2000))
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = build_determinations_panel(rostore)
    finally:
        rostore.close()
    assert panel["counts_by_kind"].get("memory_stale") == 1
    stale = [i for i in panel["items"] if i["kind"] == "memory_stale"][0]
    assert stale["blocking"] is False
    assert "changes nothing else" in stale["consequence"]


# ---------------------------------------------------------------------------
# a store that predates the ops v7 migration
# ---------------------------------------------------------------------------


def _build_pre_v7_program(program_root, platform_root) -> None:
    """A program whose ops.db stopped at v6 -- i.e. every program that
    existed before this lane landed, opened by something that never
    migrates. ``memory_relation`` does not exist and ``memory_item`` has
    no ``reviewed_ts`` column."""
    from trialerror.stores import paths
    from trialerror.stores.connection import connect
    from trialerror.stores.migrate import apply_migrations, current_version
    from trialerror.stores.schema import jobs as jobs_schema
    from trialerror.stores.schema import knowledge, ops, platform

    platform_root.mkdir(parents=True, exist_ok=True)
    store_dir = paths.program_store_dir(program_root)
    store_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        (paths.platform_db_path(root=platform_root), platform.MIGRATIONS),
        (store_dir / "knowledge.db", knowledge.MIGRATIONS),
        (store_dir / "jobs.db", jobs_schema.MIGRATIONS),
        (store_dir / "ops.db", tuple(m for m in ops.MIGRATIONS if m.version <= 6)),
    ]
    for path, migrations in targets:
        conn = connect(path)
        try:
            apply_migrations(conn, migrations)
            conn.commit()
        finally:
            conn.close()

    conn = connect(paths.ops_db_path(program_root), read_only=True)
    try:
        assert current_version(conn) == 6
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_item)")}
        assert "reviewed_ts" not in cols
    finally:
        conn.close()


def test_the_stale_check_skips_instead_of_failing_on_a_pre_v7_store(program_root, platform_root):
    """A missing ``reviewed_ts`` must read as ``skip``, never ``fail``.
    ``run_checks`` turns an escaped exception into ``status='fail'``, which
    is exactly the hard failure this check's contract forbids -- it would
    block every other gate that wants a clean doctor run, on a program
    whose only sin is not having been opened read-write yet."""
    _build_pre_v7_program(program_root, platform_root)
    result = _doctor(program_root, ["memory_stale_items"])["memory_stale_items"]
    assert result.status == "skip"
    assert "reviewed_ts" in result.message
    assert "v7" in result.message


def test_the_pending_candidate_check_skips_on_a_pre_v7_store(program_root, platform_root):
    """The engram-F4 sibling names the same migration number the migration
    actually shipped under (it landed as v7, not the v8 it was briefly
    renumbered to on the lane branch)."""
    _build_pre_v7_program(program_root, platform_root)
    result = _doctor(program_root, ["memory_pending_conflict_candidates"])["memory_pending_conflict_candidates"]
    assert result.status == "skip"
    assert "memory_relation" in result.message
    assert "v7" in result.message
    assert "v8" not in result.message


def test_a_pre_v7_store_still_renders_the_whole_dashboard_payload(program_root, platform_root):
    """``build_all_panels`` has no per-panel ``try``/``except``, and the
    dashboard only ever opens a READ-ONLY store (which never migrates), so
    an unguarded ``SELECT ... reviewed_ts`` here does not degrade one
    panel -- it takes down the entire payload with no way to heal."""
    from trialerror.dashboard.data import build_all_panels, build_determinations_panel
    from trialerror.dashboard.store_ro import open_store_ro

    _build_pre_v7_program(program_root, platform_root)
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = build_determinations_panel(rostore)
        panels = build_all_panels(rostore)
    finally:
        rostore.close()

    assert panel["status"] == "ok"
    assert panel["counts_by_kind"].get("memory_stale") is None
    assert panel["counts_by_kind"].get("memory_conflict_candidate") is None
    assert "determinations" in panels
    assert panels["determinations"]["status"] == "ok"
