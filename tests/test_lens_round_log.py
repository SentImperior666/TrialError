"""``trialerror.lens.export.lens_log`` — the round's per-lens
reconciliation, and the gate check that reads it.

The reproduction this suite pins: ``lens log`` returned the assignment rows
and a count, the gate suite's ``lens_log_reconciled`` check had no per-lens
posting state to read, fell back to counting rows, found no offender — and
PASSED a round in which one lens of three had posted. Budget spent, two arms
unrepresented, and the round gated on an arm mix it did not run.
"""

from __future__ import annotations

import json

import pytest

from trialerror.budget.pools import book_launch, create_pool
from trialerror.eval.gate_suites import lens_log_reconciled
from trialerror.events.api import create_thread, post_feed
from trialerror.lens.assign import run_assignment
from trialerror.lens.export import export_launch_bookable, lens_log
from trialerror.lens.ideas import write_idea
from trialerror.lens.roster import add_lens
from trialerror.stores import get

from tests._lens_fixtures import build_doc_pool

ROUND_ID = "round-log"


def _round(store, *, n_lenses: int = 3):
    pool = build_doc_pool(store, n_docs=24)
    lens_rows = [
        add_lens(store, round_id=ROUND_ID, lens_name=f"lens-{i}", vantage="v", model_class="top")
        for i in range(n_lenses)
    ]
    home_id, *candidate_ids = pool["doc_ids"]
    run_assignment(
        store, round_id=ROUND_ID, model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": r["roster_id"]} for r in lens_rows],
        slices_per_lens=5, seed="seed-A",
    )
    launch = get(store, "launch", pk_column="launch_id", pk_value=pool["launch_id"])
    session = get(store, "session", pk_column="session_id", pk_value=launch["session_id"])
    create_pool(store, account_id=session["account_id"], model_class="top", period="weekly", cap_tokens=10_000_000)
    return pool, lens_rows, session


def _book_every_lens(store, session) -> dict[str, str]:
    """Book each exported row exactly as an orchestrator does, and return
    ``{roster_id: launch_id}``."""
    out: dict[str, str] = {}
    for row in export_launch_bookable(store, round_id=ROUND_ID):
        result = book_launch(
            store, session_id=session["session_id"], program_id="PROG-test",
            est_tokens=1000, model="sonnet", **row,
        )
        assert result.ok
        out[row["attrs"]["roster_id"]] = result.launch_id
    return out


def _post(store, *, launch_id: str, body: str = "the lens's full record text") -> str:
    thread = create_thread(store, title="round thread", launch_id=launch_id)
    return post_feed(store, thread_id=thread["thread_id"], body=body, launch_id=launch_id)["post_id"]


# ---------------------------------------------------------------------------
# the log itself
# ---------------------------------------------------------------------------


def test_the_log_reports_one_row_per_assigned_lens_with_its_own_launch(store):
    _pool, lens_rows, session = _round(store)
    launches = _book_every_lens(store, session)

    log = lens_log(store, round_id=ROUND_ID)
    assert log["n_lenses"] == 3
    assert {r["roster_id"] for r in log["rows"]} == {r["roster_id"] for r in lens_rows}
    for row in log["rows"]:
        assert row["launch_id"] == launches[row["roster_id"]]
        assert row["booking_consumed"] is True
        assert row["n_slices"] == 5
        assert set(row) >= {"roster_id", "lens_name", "seat", "launch_id", "n_ideas", "n_feed_posts", "posted"}


def test_one_lens_of_three_posted_is_two_offenders_not_a_clean_log(store):
    """The observed round, exactly."""
    _pool, lens_rows, session = _round(store)
    launches = _book_every_lens(store, session)
    posted_roster = lens_rows[0]["roster_id"]
    _post(store, launch_id=launches[posted_roster])

    log = lens_log(store, round_id=ROUND_ID)
    assert log["n_posted"] == 1
    assert [r["posted"] for r in log["rows"]].count(True) == 1
    assert len(log["offenders"]) == 2
    assert posted_roster not in {o["roster_id"] for o in log["offenders"]}
    assert all(o["reason"] for o in log["offenders"])


def test_a_lens_that_wrote_records_and_never_posted_them_is_still_an_offender(store):
    _pool, lens_rows, session = _round(store)
    launches = _book_every_lens(store, session)
    silent = lens_rows[1]["roster_id"]
    write_idea(
        store, round_id=ROUND_ID, author_launch=launches[silent],
        body="a record that never reached the round's thread",
    )

    log = lens_log(store, round_id=ROUND_ID)
    row = next(r for r in log["rows"] if r["roster_id"] == silent)
    assert row["n_ideas"] == 1
    assert row["n_feed_posts"] == 0
    assert row["posted"] is False
    offender = next(o for o in log["offenders"] if o["roster_id"] == silent)
    assert "posted none" in offender["reason"]


def test_a_record_linked_only_by_its_assignment_still_counts_for_its_lens(store):
    """Not every writer sets ``author_launch`` to the lens's own launch; the
    record's ``slice_ref.assign_id`` is the other link, and the count is a
    union over the two rather than a choice between them."""
    _pool, lens_rows, session = _round(store)
    _book_every_lens(store, session)
    roster_id = lens_rows[2]["roster_id"]
    assign_id = store.ops.execute(
        "SELECT assign_id FROM lens_assignment WHERE roster_id = ? ORDER BY created_ts LIMIT 1", (roster_id,)
    ).fetchone()["assign_id"]
    write_idea(
        store, round_id=ROUND_ID, author_launch=_pool_launch(store),
        body="written under the assignment, attributed elsewhere",
        slice_ref=json.dumps({"assign_id": assign_id}),
    )

    log = lens_log(store, round_id=ROUND_ID)
    row = next(r for r in log["rows"] if r["roster_id"] == roster_id)
    assert row["n_ideas"] == 1


def _pool_launch(store) -> str:
    return store.platform.execute("SELECT launch_id FROM launch ORDER BY booked_ts LIMIT 1").fetchone()["launch_id"]


def test_every_lens_posted_is_a_clean_log(store):
    _pool, _lens_rows, session = _round(store)
    launches = _book_every_lens(store, session)
    for launch_id in launches.values():
        _post(store, launch_id=launch_id)

    log = lens_log(store, round_id=ROUND_ID)
    assert log["offenders"] == []
    assert log["n_posted"] == log["n_lenses"] == 3
    assert lens_log_reconciled({"lens_log": log}).passed


def test_a_lens_booked_with_assign_id_reads_as_posted(store):
    """Fix pass N-2. ``budget book --assign-id`` is the OTHER way to link a
    lens launch to its slice (lane FB-4 item 5) and it carries no attrs, so
    the log used to report the lens as never posted — and its offender reason
    told the operator to book a second launch for a lens that had run."""
    _pool, lens_rows, session = _round(store)
    row = export_launch_bookable(store, round_id=ROUND_ID)[0]
    roster_id = row["attrs"]["roster_id"]
    result = book_launch(
        store, session_id=session["session_id"], program_id="PROG-test",
        agent_kind="lens", model_class="top", model="sonnet", purpose="ideation",
        est_tokens=1000, assign_ids=row["attrs"]["assign_ids"],
    )
    assert result.ok
    assert get(store, "launch", pk_column="launch_id", pk_value=result.launch_id)["attrs"] is None
    _post(store, launch_id=result.launch_id)

    log = lens_log(store, round_id=ROUND_ID)
    mine = next(r for r in log["rows"] if r["roster_id"] == roster_id)
    assert mine["launch_id"] == result.launch_id
    assert mine["booking_consumed"] is True
    assert mine["n_feed_posts"] == 1
    assert mine["posted"] is True
    assert roster_id not in {o["roster_id"] for o in log["offenders"]}


def test_a_launch_is_never_counted_twice_for_one_lens(store):
    """A launch carrying BOTH the exported attrs and the assignment link is
    one launch, not two."""
    _pool, _lens_rows, session = _round(store)
    row = export_launch_bookable(store, round_id=ROUND_ID)[0]
    result = book_launch(
        store, session_id=session["session_id"], program_id="PROG-test",
        est_tokens=1000, model="sonnet", assign_ids=row["attrs"]["assign_ids"], **row,
    )
    mine = next(
        r for r in lens_log(store, round_id=ROUND_ID)["rows"]
        if r["roster_id"] == row["attrs"]["roster_id"]
    )
    assert mine["launch_ids"] == [result.launch_id]


def test_a_lens_nobody_booked_names_the_missing_link(store):
    _pool, _lens_rows, _session = _round(store)
    log = lens_log(store, round_id=ROUND_ID)
    assert len(log["offenders"]) == 3
    assert all("no launch is linked" in o["reason"] for o in log["offenders"])
    assert all("budget book --assign-id" in o["reason"] for o in log["offenders"])
    assert all(r["launch_id"] is None for r in log["rows"])


def test_an_unassigned_round_logs_nothing_rather_than_guessing(store):
    assert lens_log(store, round_id="round-nonexistent") == {
        "round_id": "round-nonexistent", "rows": [], "n_lenses": 0, "n_posted": 0, "offenders": []
    }


# ---------------------------------------------------------------------------
# the gate check reads exactly that shape
# ---------------------------------------------------------------------------


def _log(rows):
    return {"rows": rows, "n_lenses": len(rows), "offenders": [r for r in rows if not r["posted"]]}


def test_three_lenses_one_posted_fails_the_check_naming_the_other_two():
    rows = [
        {"lens_name": "lens-0", "roster_id": "ROST-0", "posted": True},
        {"lens_name": "lens-1", "roster_id": "ROST-1", "posted": False},
        {"lens_name": "lens-2", "roster_id": "ROST-2", "posted": False},
    ]
    result = lens_log_reconciled({"lens_log": _log(rows)})
    assert not result.passed
    assert "lens-1" in result.message and "lens-2" in result.message


def test_every_lens_posted_passes_the_check():
    rows = [{"lens_name": f"lens-{i}", "roster_id": f"ROST-{i}", "posted": True} for i in range(3)]
    assert lens_log_reconciled({"lens_log": _log(rows)}).passed


def test_an_unposted_row_fails_even_when_the_offender_list_forgot_it():
    """``rows`` is the state; ``offenders`` is a rendering of it. A subject
    whose offender list disagrees with its own rows is not a PASS."""
    rows = [{"lens_name": "lens-0", "posted": False}]
    result = lens_log_reconciled({"lens_log": {"rows": rows, "n_lenses": 1, "offenders": []}})
    assert not result.passed
    assert "lens-0" in result.message


def test_the_old_assignments_plus_count_shape_fails_as_unrecognised():
    """The vacuous PASS: the check used to count these rows, find no
    ``posted`` key on any of them, report no offender and pass."""
    old_shape = {"assignments": [{"assign_id": "ASGN-1"}, {"assign_id": "ASGN-2"}], "count": 2}
    result = lens_log_reconciled({"lens_log": old_shape})
    assert not result.passed
    assert "log shape unrecognised" in result.message


def test_a_log_that_reconciles_no_lens_fails_rather_than_passing_vacuously():
    """Fix pass B-2. ``lens log`` returns exactly this mapping for a round id
    nothing was assigned under — a mistyped ``--round-id``, or a gate run
    before ``lens assign``. The list branch always failed the empty case; the
    mapping branch reported "every booked lens posted (count not reported)"."""
    result = lens_log_reconciled({"lens_log": {"rows": [], "n_lenses": 0, "offenders": []}})
    assert not result.passed
    assert "no lens at all" in result.message


def test_the_real_log_of_an_unassigned_round_fails_the_check(store):
    log = lens_log(store, round_id="round-that-does-not-exist")
    assert log["rows"] == [] and log["offenders"] == []
    assert not lens_log_reconciled({"lens_log": log}).passed


def test_a_declared_population_with_no_offender_still_passes():
    """The offender-only shape the suite has always accepted: a population is
    declared, and nothing offends it."""
    assert lens_log_reconciled({"lens_log": {"n_lenses": 4, "offenders": []}}).passed


@pytest.mark.parametrize("shape", [{}, {"count": 3}, {"assignments": []}])
def test_no_shape_without_rows_or_offenders_can_pass(shape):
    assert not lens_log_reconciled({"lens_log": shape}).passed
