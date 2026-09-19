"""``budget book --assign-id`` — the link between a lens's own launch and
the assignment rows it was booked for (ops schema-v10).

The gap: ``lens_assignment.launch_id`` is the launch that WROTE the row (the
orchestrator running ``lens assign``). The lens's own launch is booked
later, off ``lens export``, and nothing recorded which assignment rows it
covered — so a booking made through the CLI (which carries none of the
exported attrs) read as "not a lens launch" to both the per-launch retrieval
scope and the citation audit. The barrier was off for exactly the launches
it exists for.
"""

from __future__ import annotations

import json

import pytest

from trialerror.budget.errors import UnknownAssignmentError
from trialerror.budget.pools import book_launch, create_pool, link_launch_to_assignments
from trialerror.lens.assign import run_assignment
from trialerror.lens.export import export_launch_bookable
from trialerror.lens.roster import add_lens
from trialerror.retrieve.engine import launch_slice_doc_ids
from trialerror.stores import get
from trialerror.stores.schema import ops

from tests._lens_fixtures import build_doc_pool

ROUND_ID = "round-link"


def _round(store, *, slices_per_lens: int = 3):
    pool = build_doc_pool(store, n_docs=12)
    lens_row = add_lens(store, round_id=ROUND_ID, lens_name="lens-1", vantage="v", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]
    run_assignment(
        store, round_id=ROUND_ID, model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": lens_row["roster_id"]}], slices_per_lens=slices_per_lens, seed="seed-A",
    )
    launch = get(store, "launch", pk_column="launch_id", pk_value=pool["launch_id"])
    session = get(store, "session", pk_column="session_id", pk_value=launch["session_id"])
    create_pool(store, account_id=session["account_id"], model_class="top", period="weekly", cap_tokens=10_000_000)
    return pool, lens_row, session


def _assign_ids(store):
    return [
        row["assign_id"]
        for row in store.ops.execute(
            "SELECT assign_id FROM lens_assignment ORDER BY created_ts, rowid"
        ).fetchall()
    ]


def test_the_migration_is_the_next_contiguous_one(store):
    versions = [m.version for m in ops.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    v10 = next(m for m in ops.MIGRATIONS if m.version == 10)
    assert v10.name == "ops_v10_lens_assignment_lens_launch_id"
    columns = {r["name"] for r in store.ops.execute("PRAGMA table_info(lens_assignment)")}
    assert "lens_launch_id" in columns
    assert "launch_id" in columns  # the writer's launch, untouched


def test_booking_with_two_assign_ids_links_both_rows(store):
    _pool, _lens_row, session = _round(store)
    assign_ids = _assign_ids(store)[:2]
    result = book_launch(
        store, session_id=session["session_id"], program_id="PROG-test",
        agent_kind="lens", model_class="top", model="sonnet", purpose="ideation",
        est_tokens=1000, assign_ids=assign_ids,
    )
    assert result.ok
    linked = {
        row["assign_id"]
        for row in store.ops.execute(
            "SELECT assign_id FROM lens_assignment WHERE lens_launch_id = ?", (result.launch_id,)
        )
    }
    assert linked == set(assign_ids)


def test_an_assign_id_that_names_no_row_is_refused_rather_than_linked_to_nothing(store):
    _pool, _lens_row, session = _round(store)
    with pytest.raises(UnknownAssignmentError) as excinfo:
        book_launch(
            store, session_id=session["session_id"], program_id="PROG-test",
            agent_kind="lens", model_class="top", model="sonnet", purpose="ideation",
            est_tokens=1000, assign_ids=[_assign_ids(store)[0], "ASGN-nonexistent"],
        )
    assert "ASGN-nonexistent" in str(excinfo.value)


def test_a_partial_link_is_never_written(store):
    """The refusal is the whole set: two of three slices linked would put
    the third outside the launch's own scope, which the audit reports as a
    crossed barrier the lens never crossed."""
    _pool, _lens_row, session = _round(store)
    with pytest.raises(UnknownAssignmentError):
        link_launch_to_assignments(
            store, launch_id=_pool_launch(store), assign_ids=[_assign_ids(store)[0], "ASGN-nope"]
        )
    assert store.ops.execute(
        "SELECT COUNT(*) AS n FROM lens_assignment WHERE lens_launch_id IS NOT NULL"
    ).fetchone()["n"] == 0


def _pool_launch(store) -> str:
    return store.platform.execute("SELECT launch_id FROM launch ORDER BY booked_ts LIMIT 1").fetchone()["launch_id"]


def test_the_retrieval_scope_resolves_a_slice_through_the_link_alone(store):
    """A booking carrying NO attrs at all used to fall out of
    ``launch_slice_doc_ids`` as "no scope" -- the barrier off."""
    _pool, _lens_row, session = _round(store)
    assign_ids = _assign_ids(store)
    result = book_launch(
        store, session_id=session["session_id"], program_id="PROG-test",
        agent_kind="lens", model_class="top", model="sonnet", purpose="ideation",
        est_tokens=1000, assign_ids=assign_ids,
    )
    row = get(store, "launch", pk_column="launch_id", pk_value=result.launch_id)
    assert row["attrs"] is None

    expected = {
        json.loads(r["slice_spec"])["candidate_id"]
        for r in store.ops.execute("SELECT slice_spec FROM lens_assignment")
    }
    assert set(launch_slice_doc_ids(store, result.launch_id) or []) == expected


def test_a_launch_with_no_link_and_no_attrs_still_declares_no_slice(store):
    _pool, _lens_row, session = _round(store)
    result = book_launch(
        store, session_id=session["session_id"], program_id="PROG-test",
        agent_kind="orchestrator", model_class="top", model="sonnet", purpose="ideation",
        est_tokens=1000,
    )
    assert launch_slice_doc_ids(store, result.launch_id) is None


def test_the_exported_attrs_still_win_over_the_link(store):
    """Both paths resolve the same slice; the attrs are the more direct
    source and stay first, so an export-booked launch is unchanged."""
    _pool, _lens_row, session = _round(store)
    row = export_launch_bookable(store, round_id=ROUND_ID)[0]
    result = book_launch(
        store, session_id=session["session_id"], program_id="PROG-test",
        est_tokens=1000, model="sonnet", assign_ids=row["attrs"]["assign_ids"], **row,
    )
    assert set(launch_slice_doc_ids(store, result.launch_id) or []) == set(row["attrs"]["slice_doc_ids"])


# ---------------------------------------------------------------------------
# Fix pass B-1 — a refused booking is a refusal, not a dangling launch
# ---------------------------------------------------------------------------


def test_a_refused_assign_id_leaves_no_launch_row(store):
    """The assign ids are resolved BEFORE the ``launch`` row is written.

    Before the fix, one mistyped ``--assign-id`` inserted a PROVISIONAL
    launch and only then raised: the row held pool headroom, ``session
    close`` refused with ``dangling_launches``, and the caller had no launch
    id to reconcile it with (the refusal is an exception, so no
    ``BookResult`` ever reaches it)."""
    _pool, _lens_row, session = _round(store)
    before = store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"]
    with pytest.raises(UnknownAssignmentError):
        book_launch(
            store, session_id=session["session_id"], program_id="PROG-test",
            agent_kind="lens", model_class="top", model="sonnet", purpose="ideation",
            est_tokens=1000, assign_ids=["ASGN-does-not-exist"],
        )
    after = store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"]
    assert after == before


def test_a_refused_assign_id_holds_no_pool_headroom(store):
    """The same fact the way the operator meets it: the live commitment is
    unchanged, so the refused booking costs the pool nothing."""
    _pool, _lens_row, session = _round(store)

    def committed() -> int:
        row = store.platform.execute(
            "SELECT COALESCE(SUM(est_tokens), 0) AS n FROM launch "
            "WHERE account_id = ? AND state IN ('PROVISIONAL', 'RUNNING')",
            (session["account_id"],),
        ).fetchone()
        return row["n"]

    before = committed()
    with pytest.raises(UnknownAssignmentError):
        book_launch(
            store, session_id=session["session_id"], program_id="PROG-test",
            agent_kind="lens", model_class="top", model="sonnet", purpose="ideation",
            est_tokens=1_000_000, assign_ids=["ASGN-does-not-exist"],
        )
    assert committed() == before


def test_a_bare_string_of_assign_ids_is_refused_not_read_per_character(store):
    """``assign_ids="ASGN-1"`` used to iterate into one id per character (the
    MCP surface coerces with a comprehension). Eight bogus ids is not a
    diagnosis; the refusal names the shape."""
    _pool, _lens_row, session = _round(store)
    before = store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"]
    with pytest.raises(UnknownAssignmentError) as excinfo:
        book_launch(
            store, session_id=session["session_id"], program_id="PROG-test",
            agent_kind="lens", model_class="top", model="sonnet", purpose="ideation",
            est_tokens=1000, assign_ids=_assign_ids(store)[0],
        )
    assert "not a single string" in str(excinfo.value)
    assert store.platform.execute("SELECT COUNT(*) AS n FROM launch").fetchone()["n"] == before
