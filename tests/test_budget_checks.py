"""Tests for ``trialerror.budget.checks`` — the doctor checks this subsystem
registers (auto-discovered exactly like M0's/M1's own checks)."""

from __future__ import annotations

from trialerror.budget.pools import book_launch, create_pool
from trialerror.stores import insert
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._budget_fixtures import open_account_session


def _run(names=None):
    clear_registry()
    discover_and_register_checks()
    return {r.name: r for r in run_checks(DoctorContext(), only=names)}


def test_checks_auto_discovered():
    results = _run()
    assert "budget_dangling_launches" in results
    assert "budget_pool_overspend" in results


def test_dangling_launches_skips_when_no_platform_db(monkeypatch, tmp_path):
    empty_root = tmp_path / "nope"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(empty_root))
    results = _run(["budget_dangling_launches"])
    assert results["budget_dangling_launches"].status == "skip"


def test_dangling_launches_warns_on_ttl_expired_booking(store):
    account_id, session_id = open_account_session(store)
    book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=10,
        booking_ttl_s=1,
        now_ts="2020-01-01T00:00:00.000Z",
    )
    results = _run(["budget_dangling_launches"])
    result = results["budget_dangling_launches"]
    assert result.status == "warn"
    assert len(result.details["offenders"]) == 1


def test_dangling_launches_pass_when_all_fresh(store):
    account_id, session_id = open_account_session(store)
    book_launch(
        store,
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=10,
        booking_ttl_s=3600,
    )
    results = _run(["budget_dangling_launches"])
    assert results["budget_dangling_launches"].status == "pass"


def test_pool_overspend_skips_when_no_platform_db(monkeypatch, tmp_path):
    empty_root = tmp_path / "nope"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(empty_root))
    results = _run(["budget_pool_overspend"])
    assert results["budget_pool_overspend"].status == "skip"


def test_pool_overspend_warns_when_projected_spend_exceeds_hard_cap(store):
    account_id, _ = open_account_session(store)
    pool = create_pool(
        store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=100, billed_multiplier=1.0
    )
    # Simulate already-reconciled spend directly (checks.py operates on
    # whatever state the DB is in, regardless of how it got there).
    from trialerror.stores import update

    update(
        store,
        "budget_pool",
        pk_column="pool_id",
        pk_value=pool["pool_id"],
        changes={"spent_visible_tokens": 500},
    )

    results = _run(["budget_pool_overspend"])
    result = results["budget_pool_overspend"]
    assert result.status == "warn"
    assert any(o["pool_id"] == pool["pool_id"] for o in result.details["offenders"])


def test_pool_overspend_pass_when_within_cap(store):
    account_id, _ = open_account_session(store)
    create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=100_000, billed_multiplier=1.0)
    results = _run(["budget_pool_overspend"])
    assert results["budget_pool_overspend"].status == "pass"


# ---------------------------------------------------------------------------
# reconcile_provenance (lane FB-3 item 4, D-FB-13 (d))
# ---------------------------------------------------------------------------


def _reconciled(store, session_id, *, source="manual", actual=100):
    from trialerror.budget.pools import reconcile_launch

    booked = book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens",
        model_class="mid", model="sonnet", purpose="mechanical", est_tokens=10,
    )
    reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=actual, reconcile_source=source)
    return booked.launch_id


def _reconciled_from_event(store, session_id, *, total=11461):
    from trialerror.budget.pools import reconcile_launch_from_event
    from trialerror.events.api import append_event

    booked = book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens",
        model_class="mid", model="sonnet", purpose="mechanical", est_tokens=10,
    )
    append_event(
        store, event_type="subagent_return", session_id=session_id, launch_id=booked.launch_id,
        payload={"response_size_bytes": 1, "duration_ms": None,
                 "usage": {"total_tokens": total, "total_source": "totalTokens",
                           "input_tokens": 2, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": total - 6, "output_tokens": 4}},
    )
    reconcile_launch_from_event(store, launch_id=booked.launch_id)
    return booked.launch_id


def test_reconcile_provenance_is_auto_discovered():
    assert "reconcile_provenance" in _run()


def test_reconcile_provenance_skips_when_no_platform_db(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(tmp_path / "nope"))
    assert _run(["reconcile_provenance"])["reconcile_provenance"].status == "skip"


def test_reconcile_provenance_skips_when_nothing_is_reconciled(store):
    open_account_session(store)
    assert _run(["reconcile_provenance"])["reconcile_provenance"].status == "skip"


def test_reconcile_provenance_warns_on_a_hand_asserted_actual(store):
    _account, session_id = open_account_session(store)
    launch_id = _reconciled(store, session_id, source="manual")

    result = _run(["reconcile_provenance"])["reconcile_provenance"]
    assert result.status == "warn"
    assert [row["launch_id"] for row in result.details["manual"]] == [launch_id]
    assert "--from-event" in result.message


def test_reconcile_provenance_passes_when_every_number_was_measured(store):
    _account, session_id = open_account_session(store)
    _reconciled_from_event(store, session_id)

    result = _run(["reconcile_provenance"])["reconcile_provenance"]
    assert result.status == "pass"
    assert result.details["counts"] == {"event": 1}
    assert result.details["measured"] == 1
    assert result.details["asserted"] == 0


def test_reconcile_provenance_counts_transcript_and_event_side_by_side(store):
    """The INFO-level reading: a program moving from asserted to measured
    provenance watches this ratio, and a check that reported only offenders
    could not show it moving."""
    _account, session_id = open_account_session(store)
    _reconciled(store, session_id, source="transcript")
    _reconciled_from_event(store, session_id)
    _reconciled_from_event(store, session_id)

    result = _run(["reconcile_provenance"])["reconcile_provenance"]
    assert result.status == "pass"  # transcript is asserted, but only `manual` is the offender
    assert result.details["counts"] == {"transcript": 1, "event": 2}
    assert "2 event" in result.message and "1 transcript" in result.message


def test_reconcile_provenance_names_pre_platform_v2_rows_apart_from_manual(store):
    """A row settled before this program recorded provenance carries a null
    source. Folding it into `manual` would report a fix that does not exist."""
    _account, session_id = open_account_session(store)
    legacy = book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens",
        model_class="mid", model="sonnet", purpose="mechanical", est_tokens=10,
    )
    store.platform.execute(
        "UPDATE launch SET state='RECONCILED', actual_tokens=50, reconciled_ts=?, reconcile_source=NULL "
        "WHERE launch_id=?",
        (now(), legacy.launch_id),
    )
    store.platform.commit()
    _reconciled_from_event(store, session_id)

    result = _run(["reconcile_provenance"])["reconcile_provenance"]
    assert result.status == "pass"
    assert result.details["unrecorded"] == [legacy.launch_id]
    assert result.details["manual"] == []
    assert "before this program recorded provenance" in result.message


def test_reconcile_provenance_details_do_not_grow_with_the_programs_whole_past(store):
    """Fix pass V-5. Every other budget offender list is an anomaly set that
    stays small; this one is most of a program's history -- 1,839 rows and a
    393 KB envelope on a real store, serialised into the dashboard's sidecar
    state file and doctor panel on every run. The count is the finding; the
    rows are a sample."""
    import json as _json

    from trialerror.budget.checks import _MAX_LISTED_ROWS

    _account, session_id = open_account_session(store)
    for _ in range(_MAX_LISTED_ROWS + 5):
        _reconciled(store, session_id, source="manual")

    result = _run(["reconcile_provenance"])["reconcile_provenance"]
    assert result.status == "warn"
    assert result.details["manual_total"] == _MAX_LISTED_ROWS + 5
    assert len(result.details["manual"]) == _MAX_LISTED_ROWS
    assert result.details["manual_truncated"] is True
    assert result.details["counts"]["manual"] == _MAX_LISTED_ROWS + 5
    assert "most recently settled" in result.message
    # The envelope stays a readable size whatever the history behind it.
    assert len(_json.dumps(result.to_dict(), default=str)) < 20_000


def test_reconcile_provenance_lists_the_most_recently_settled_manual_rows(store):
    """Which rows survive the cap is decided, not incidental: the newest
    settlements are the ones an operator can still do something about."""
    from trialerror.budget.checks import _MAX_LISTED_ROWS

    _account, session_id = open_account_session(store)
    ids = [_reconciled(store, session_id, source="manual") for _ in range(_MAX_LISTED_ROWS + 3)]
    # Distinct, ordered settlement stamps -- `now()` has millisecond
    # resolution and these rows are written in one burst, so a tie-break
    # would otherwise be SQLite's to make.
    for i, launch_id in enumerate(ids):
        store.platform.execute(
            "UPDATE launch SET reconciled_ts = ? WHERE launch_id = ?",
            (f"2026-09-{i + 1:02d}T09:00:00.000Z", launch_id),
        )
    store.platform.commit()

    result = _run(["reconcile_provenance"])["reconcile_provenance"]
    listed = {row["launch_id"] for row in result.details["manual"]}
    # The three oldest fell off the end, not three arbitrary rows.
    assert listed == set(ids[3:])
