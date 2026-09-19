"""``trialerror.lens.export`` — launch-bookable rows, and the actual integration
proof that a returned row books straight through ``trialerror.budget.book_launch``."""

from __future__ import annotations

from trialerror.budget.pools import book_launch
from trialerror.lens.assign import run_assignment
from trialerror.lens.export import AGENT_KIND, PURPOSE, export_launch_bookable
from trialerror.lens.roster import add_lens
from trialerror.stores import get
from tests._lens_fixtures import build_doc_pool


def _setup_round(store, *, n_lenses: int = 2, n_docs: int = 20, slices_per_lens: int = 5):
    pool = build_doc_pool(store, n_docs=n_docs)
    lens_rows = [
        add_lens(store, round_id="round-1", lens_name=f"lens-{i}", vantage="v", model_class="top")
        for i in range(n_lenses)
    ]
    home_id, *candidate_ids = pool["doc_ids"]
    run_assignment(
        store, round_id="round-1", model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": r["roster_id"]} for r in lens_rows], slices_per_lens=slices_per_lens, seed="seed-A",
    )
    return pool, lens_rows


def test_export_launch_bookable_empty_round_is_empty_list(store):
    assert export_launch_bookable(store, round_id="round-nonexistent") == []


def test_export_launch_bookable_one_row_per_lens_with_full_arm_breakdown(store):
    pool, lens_rows = _setup_round(store, n_lenses=3)
    rows = export_launch_bookable(store, round_id="round-1")
    assert len(rows) == 3
    roster_ids = {r["roster_id"] for r in lens_rows}
    assert {row["attrs"]["roster_id"] for row in rows} == roster_ids
    for row in rows:
        assert row["agent_kind"] == AGENT_KIND
        assert row["purpose"] == PURPOSE
        assert row["model_class"] == "top"
        assert row["workpackage"] == "round-1"
        assert row["attrs"]["slice_count"] == 5
        assert sum(row["attrs"]["arms"].values()) == 5


def test_export_launch_bookable_row_books_straight_through_book_launch(store):
    pool, lens_rows = _setup_round(store, n_lenses=1)
    launch_id = pool["launch_id"]
    session = get(store, "session", pk_column="session_id", pk_value=get(store, "launch", pk_column="launch_id", pk_value=launch_id)["session_id"])

    from trialerror.budget.pools import create_pool

    create_pool(store, account_id=session["account_id"], model_class="top", period="weekly", cap_tokens=1_000_000)

    rows = export_launch_bookable(store, round_id="round-1")
    assert len(rows) == 1
    row = rows[0]

    result = book_launch(
        store,
        session_id=session["session_id"],
        program_id="PROG-test",
        est_tokens=1000,
        model="sonnet",
        **row,
    )
    assert result.ok
    assert result.state == "PROVISIONAL"
