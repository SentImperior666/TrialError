"""Per-pool truth: which pool a check may judge, which launches count
against it, and whether the three surfaces that report it agree.

Lane FB-3 items 5, 6 and 7 (D-FB-14 (1)/(2) and the custodian's
2026-09-11/15 observation (a)). The fixture throughout is the shape that
produced the wrong readings: an account whose weekly pool has rolled over
more than once, with live bookings on both sides of the roll.
"""

from __future__ import annotations

import json

import pytest

from trialerror.budget.pools import (
    book_launch,
    budget_status,
    committed_visible_tokens,
    create_pool,
    current_pool_row,
    pool_report,
    reconcile_launch,
)
from trialerror.stores import get
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks

from tests._budget_fixtures import open_account_session


def _run_check(name):
    clear_registry()
    discover_and_register_checks()
    return {r.name: r for r in run_checks(DoctorContext(), only=[name])}[name]


def _book(store, session_id, **overrides):
    kwargs = dict(
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=1000,
    )
    kwargs.update(overrides)
    return book_launch(store, **kwargs)


@pytest.fixture()
def three_pools(store):
    """Three weekly pools for one account+class, oldest to newest. Only the
    third is bookable; the first two are frozen history."""
    account_id, session_id = open_account_session(store)
    pools = [
        create_pool(
            store, account_id=account_id, model_class="mid", period="weekly",
            cap_tokens=1_000_000, period_start=start, billed_multiplier=1.0,
        )
        for start in ("2026-08-29T09:00:00.000Z", "2026-09-05T09:00:00.000Z", "2026-09-12T09:00:00.000Z")
    ]
    return account_id, session_id, pools


# ---------------------------------------------------------------------------
# the current-pool rule is book_launch's own target
# ---------------------------------------------------------------------------


def test_the_current_pool_is_the_one_book_launch_targets(store, three_pools):
    account_id, session_id, pools = three_pools
    booked = _book(store, session_id)
    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)

    current = current_pool_row(store.platform, account_id, "mid")
    assert current["pool_id"] == pools[-1]["pool_id"]
    assert row["pool_id"] == pools[-1]["pool_id"]


def test_a_refused_booking_still_records_the_pool_that_refused_it(store):
    """A REFUSED booking is evidence about one pool's cap. Losing which pool
    would make it evidence about nothing."""
    account_id, session_id = open_account_session(store)
    pool = create_pool(
        store, account_id=account_id, model_class="mid", period="weekly",
        cap_tokens=10, billed_multiplier=1.0,
    )
    refused = _book(store, session_id, est_tokens=10_000)
    assert refused.state == "REFUSED"
    row = get(store, "launch", pk_column="launch_id", pk_value=refused.launch_id)
    assert row["pool_id"] == pool["pool_id"]


def test_a_booking_made_with_no_pool_at_all_records_none(store):
    _account, session_id = open_account_session(store)
    booked = _book(store, session_id)
    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["pool_id"] is None


# ---------------------------------------------------------------------------
# per-pool committed sums
# ---------------------------------------------------------------------------


def test_a_superseded_pools_commitment_is_its_own_rows_only(store, three_pools):
    """The bug this closes: before pool_id, a live booking made under LAST
    week's pool was swept into THIS week's committed number by the
    (account_id, model_class) inference, and the superseded pool's own
    commitment could not be read at all."""
    account_id, session_id, pools = three_pools
    old = _book(store, session_id, est_tokens=5000)
    # move it onto the middle pool, as if it had been booked before the roll
    store.platform.execute(
        "UPDATE launch SET pool_id = ? WHERE launch_id = ?", (pools[1]["pool_id"], old.launch_id)
    )
    store.platform.commit()
    _book(store, session_id, est_tokens=700)

    report = {entry["pool_id"]: entry for entry in pool_report(store.platform, account_id=account_id)}
    assert report[pools[1]["pool_id"]]["committed_visible_tokens"] == 5000
    assert report[pools[2]["pool_id"]]["committed_visible_tokens"] == 700
    assert report[pools[0]["pool_id"]]["committed_visible_tokens"] == 0


def test_a_pre_platform_v2_booking_counts_toward_the_current_pool(store, three_pools):
    """A row with no pool_id was written before the column existed. It counts
    where the pre-v2 arithmetic put it -- the current pool -- because that is
    the only reading available, and dropping it would silently shrink the
    number a cap check reads."""
    account_id, session_id, pools = three_pools
    legacy = _book(store, session_id, est_tokens=4000)
    store.platform.execute("UPDATE launch SET pool_id = NULL WHERE launch_id = ?", (legacy.launch_id,))
    store.platform.commit()

    committed = committed_visible_tokens(
        store.platform, account_id=account_id, model_class="mid",
        pool_id=pools[-1]["pool_id"], is_current=True,
    )
    assert committed["total"] == 4000
    superseded = committed_visible_tokens(
        store.platform, account_id=account_id, model_class="mid",
        pool_id=pools[0]["pool_id"], is_current=False,
    )
    assert superseded["total"] == 0


# ---------------------------------------------------------------------------
# item 7 -- a PROVISIONAL booking from a hookless session is counted, and
# visible AS provisional
# ---------------------------------------------------------------------------


def test_a_provisional_booking_from_a_session_with_no_hook_events_is_committed(store, three_pools):
    """Custodian observation (a), 2026-09-11 (launch 0604) and 2026-09-15
    (0.5M). The commitment is computed from the BOOKING ROWS -- PROVISIONAL
    and RUNNING alike -- so nothing about it waits on a hook. This session
    records no hook_alive event at all and the booking never reaches RUNNING,
    which is exactly the hookless shape (the Workflow tool fires no hooks)."""
    account_id, session_id, pools = three_pools
    assert store.ops.execute("SELECT COUNT(*) c FROM event WHERE type='hook_alive'").fetchone()["c"] == 0

    booked = _book(store, session_id, est_tokens=500_000)
    assert get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)["state"] == "PROVISIONAL"

    status = budget_status(store, account_id=account_id)
    entry = next(p for p in status["pools"] if p["model_class"] == "mid")
    assert entry["committed_visible_tokens"] == 500_000
    assert entry["committed_by_state"] == {"PROVISIONAL": 500_000}


def test_the_committed_breakdown_separates_provisional_from_running(store, three_pools):
    """One number could not tell a set of bookings nothing has spawned
    against from a set of running agents; the breakdown can."""
    account_id, session_id, pools = three_pools
    _book(store, session_id, est_tokens=300)
    running = _book(store, session_id, est_tokens=700)
    store.platform.execute("UPDATE launch SET state='RUNNING' WHERE launch_id=?", (running.launch_id,))
    store.platform.commit()

    entry = next(
        p for p in pool_report(store.platform, account_id=account_id) if p["pool_id"] == pools[-1]["pool_id"]
    )
    assert entry["committed_by_state"] == {"PROVISIONAL": 300, "RUNNING": 700}
    assert entry["committed_visible_tokens"] == 1000


# ---------------------------------------------------------------------------
# the doctor judges only what can move
# ---------------------------------------------------------------------------


def _blow_the_cap(store, pool_id):
    store.platform.execute(
        "UPDATE budget_pool SET spent_visible_tokens = 2000000 WHERE pool_id = ?", (pool_id,)
    )
    store.platform.commit()


def test_the_doctor_never_names_a_superseded_pool(store, three_pools):
    account_id, session_id, pools = three_pools
    _blow_the_cap(store, pools[0]["pool_id"])

    result = _run_check("budget_pool_overspend")
    assert result.status == "pass"
    assert result.details["offenders"] == []
    assert [entry["pool_id"] for entry in result.details["superseded"]] == [
        pools[1]["pool_id"], pools[0]["pool_id"],
    ]
    assert "frozen numbers" in result.message


def test_a_superseded_pool_is_still_reported_with_its_frozen_numbers(store, three_pools):
    account_id, session_id, pools = three_pools
    _blow_the_cap(store, pools[0]["pool_id"])
    result = _run_check("budget_pool_overspend")
    frozen = next(e for e in result.details["superseded"] if e["pool_id"] == pools[0]["pool_id"])
    assert frozen["spent_visible_tokens"] == 2000000
    assert frozen["standing"] == "superseded"
    # reported, never judged
    assert frozen["over_hard"] is None and frozen["over_soft"] is None


def test_the_doctor_names_a_current_offender_with_its_numbers_and_period(store, three_pools):
    account_id, session_id, pools = three_pools
    _blow_the_cap(store, pools[-1]["pool_id"])

    result = _run_check("budget_pool_overspend")
    assert result.status == "warn"
    assert [e["pool_id"] for e in result.details["offenders"]] == [pools[-1]["pool_id"]]
    assert pools[-1]["pool_id"] in result.message
    assert "2026-09-12T09:00:00.000Z" in result.message
    assert "hard cap" in result.message


# ---------------------------------------------------------------------------
# the doctor, budget status and budget pools agree
# ---------------------------------------------------------------------------

_SHARED_COLUMNS = (
    "projected_billed_tokens", "soft_cap", "hard_cap", "committed_visible_tokens",
    "headroom_tokens", "spent_visible_tokens", "billed_multiplier", "over_hard", "over_soft",
)


def test_the_doctor_and_budget_status_agree_about_every_pool_they_both_show(store, three_pools):
    account_id, session_id, pools = three_pools
    _book(store, session_id, est_tokens=900_000)
    _blow_the_cap(store, pools[-1]["pool_id"])

    status = budget_status(store, account_id=account_id)
    by_id = {entry["pool_id"]: entry for entry in status["pools"]}
    result = _run_check("budget_pool_overspend")

    shown_by_both = [e for e in result.details["judged"] if e["pool_id"] in by_id]
    assert shown_by_both, "the fixture must have at least one pool on both surfaces"
    for entry in shown_by_both:
        for column in _SHARED_COLUMNS:
            assert entry[column] == by_id[entry["pool_id"]][column], column


def test_budget_pools_prints_what_status_computes(store, three_pools, capsys):
    from trialerror.cli import main

    account_id, session_id, pools = three_pools
    _book(store, session_id, est_tokens=900_000)
    status = budget_status(store, account_id=account_id)
    by_id = {entry["pool_id"]: entry for entry in status["pools"]}
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = main([
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "budget", "pools", "--account-id", account_id,
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out

    printed = {entry["pool_id"]: entry for entry in out["result"]["pools"]}
    assert set(printed) == {p["pool_id"] for p in pools}
    assert out["result"]["current"] == [pools[-1]["pool_id"]]
    assert len(out["result"]["superseded"]) == 2
    for pool_id, entry in by_id.items():
        for column in _SHARED_COLUMNS:
            assert printed[pool_id][column] == entry[column], column


def test_budget_pools_says_why_a_superseded_pool_carries_no_verdict(store, three_pools, capsys):
    from trialerror.cli import main

    account_id, _session_id, _pools = three_pools
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    main([
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "budget", "pools", "--account-id", account_id,
    ])
    out = json.loads(capsys.readouterr().out)
    assert "only ever targets the current pool" in out["result"]["note"]


def test_budget_pools_create_still_works(store, capsys):
    from trialerror.cli import main

    account_id, _session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = main([
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "budget", "pools", "--create", "--account-id", account_id,
        "--model-class", "top", "--period", "weekly", "--cap-tokens", "100",
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["result"]["created"]["cap_tokens"] == 100


# ---------------------------------------------------------------------------
# item 7 -- `budget check` says a commitment is not a spend
# ---------------------------------------------------------------------------


def test_budget_check_names_the_outstanding_commitment(store, three_pools, capsys):
    """The reading that made a hookless PROVISIONAL booking look missing: it
    moves neither the pool's spent total nor the plan meter, because it is a
    commitment rather than a spend. The summary now says so."""
    from trialerror.cli import main

    account_id, session_id, _pools = three_pools
    _book(store, session_id, est_tokens=500_000)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = main([
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "budget", "check", "--account-id", account_id, "--quota-dir", str(program_root / "no-quota"),
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["result"]["outstanding_commitments"] == {
        "committed_visible_tokens": 500_000,
        "by_state": {"PROVISIONAL": 500_000},
    }
    assert "500,000 visible tokens are committed and not yet settled" in out["result"]["summary"]
    assert "PROVISIONAL" in out["result"]["summary"]


def test_budget_check_says_nothing_about_commitments_when_there_are_none(store, three_pools, capsys):
    from trialerror.cli import main

    account_id, session_id, _pools = three_pools
    booked = _book(store, session_id, est_tokens=1000)
    reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=1000)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    main([
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "budget", "check", "--account-id", account_id, "--quota-dir", str(program_root / "no-quota"),
    ])
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["outstanding_commitments"]["committed_visible_tokens"] == 0
    assert "committed and not yet settled" not in out["result"]["summary"]


# ---------------------------------------------------------------------------
# Fix pass V-4: the read-only surfaces on a platform.db still at v1
# ---------------------------------------------------------------------------


def _v1_platform_root(tmp_path):
    """A platform.db on v1 — the state of the machine immediately after a
    deploy, before anything has opened a writable Store to migrate it."""
    import sqlite3

    from trialerror.stores.migrate import apply_migrations
    from trialerror.stores.schema import platform

    ts = "2026-09-15T12:00:00.000Z"
    root = tmp_path / "v1_platform"
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "platform.db")
    conn.execute("PRAGMA journal_mode=WAL")
    apply_migrations(conn, tuple(m for m in platform.MIGRATIONS if m.version <= 1))
    conn.execute("INSERT INTO account (account_id, label, created_ts) VALUES ('ACC-1','a',?)", (ts,))
    conn.execute(
        "INSERT INTO budget_pool (pool_id, account_id, model_class, period, period_start, cap_tokens, "
        "spent_visible_tokens, billed_multiplier, soft_pct, hard_pct, updated_ts) "
        "VALUES ('POOL-1','ACC-1','mid','weekly',?,1000000,10000,1.0,95,100,?)",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO launch (launch_id, account_id, program_id, session_id, parent_launch, agent_kind, "
        "model_class, model, purpose, est_tokens, booked_ts, booking_ttl_s, state, actual_tokens, "
        "reconciled_ts, reconcile_source, workpackage, attrs) "
        "VALUES ('LNCH-live','ACC-1','PROG-1','SESS-1',NULL,'lens','mid','sonnet','mechanical',9000,?,"
        "3600,'PROVISIONAL',NULL,NULL,NULL,'WKP-1',NULL)",
        (ts,),
    )
    conn.commit()
    conn.close()
    return root


def test_the_doctor_reads_a_v1_platform_db_instead_of_raising(tmp_path, monkeypatch):
    """`budget_pool_overspend` opens platform.db READ-ONLY, so it cannot
    migrate the file it is asked about -- and the boot ritual runs the doctor
    before anything else has opened a writable Store. Querying
    `launch.pool_id` unconditionally made the first doctor run after a deploy
    report `OperationalError: no such column: pool_id`, which is a fact about
    the reader, not about the budget."""
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(_v1_platform_root(tmp_path)))
    result = _run_check("budget_pool_overspend")
    assert result.status == "pass", result.message
    judged = result.details["judged"]
    assert [p["pool_id"] for p in judged] == ["POOL-1"]
    # The pre-v2 reading, and it says so rather than claiming per-pool truth.
    assert judged[0]["committed_visible_tokens"] == 9000
    assert judged[0]["committed_attribution"].startswith("account+class")
    assert "v1" in judged[0]["committed_attribution"]


def test_the_dashboard_budget_panel_reads_a_v1_platform_db_too(tmp_path, monkeypatch):
    from trialerror.dashboard.data import build_budget_panel
    from trialerror.dashboard.store_ro import open_store_ro

    platform_root = _v1_platform_root(tmp_path)
    program = tmp_path / "v1_program"
    program.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    with open_store_ro(program, platform_root=platform_root) as ro:
        panel = build_budget_panel(ro)
    assert panel["status"] == "ok"
    pool = panel["accounts"][0]["budget_status"]["pools"][0]
    assert pool["committed_visible_tokens"] == 9000
    assert pool["committed_attribution"].startswith("account+class")


def test_a_migrated_store_says_its_commitment_is_attributed_by_pool_id(store, three_pools):
    account_id, session_id, pools = three_pools
    _book(store, session_id, est_tokens=5_000)
    entry = pool_report(store.platform, account_id=account_id)[0]
    assert entry["committed_attribution"] == "pool_id"


def test_budget_pools_still_prints_the_pools_own_percentages_and_last_write(store, three_pools):
    """Fix pass V-8. Moving `budget pools` onto pool_report dropped
    soft_pct/hard_pct/updated_ts, which `list_pools` had always returned.
    soft_cap/hard_cap make the percentages recoverable by division, which is
    not the same as printing what the row says."""
    account_id, _session_id, pools = three_pools
    entry = pool_report(store.platform, account_id=account_id)[0]
    assert entry["soft_pct"] == pools[0]["soft_pct"]
    assert entry["hard_pct"] == pools[0]["hard_pct"]
    assert entry["updated_ts"]
