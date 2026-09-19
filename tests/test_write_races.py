"""WA-1 regression suite: the check-then-write races, reproduced.

The finding (``docs/reviews/lane-bugs/DASHBOARD_BUG_SWEEP_2026-09.md``, WA-1,
severity blocker) is that four write paths read a row, decided the write was
legal, and then wrote -- with nothing holding the row still in between. Under
a concurrent double-submit (two operators on the dashboard, or an operator
and an agent) EVERY caller passed the check and EVERY caller "succeeded":

- ``rooms.freeze_room`` -- N freezes on one open room, N ``room_frozen``
  events, N different stated reasons, one room.
- ``rooms.converge_room`` -- same shape on the other terminal edge.
- ``ingest.requests.transition`` -- N ``ingest_request_transition`` events
  for one real ``requested -> delivered`` step.
- ``artifacts.gates.verify_edit`` -- the worst one: ``edits`` is a single
  JSON column holding the whole array, so each caller read the array,
  changed its own entry and wrote the whole array back. Six appliers
  verifying six different edits left ONE verification standing and five
  silently discarded -- and ``verify_edit`` writes no event (design 12.12),
  so nothing recorded that they ever happened.

Every test below is written the way the verifier reproduced it: six real
threads, each with its OWN ``open_store`` (separate sqlite connections --
the same-connection case proves nothing about WAL contention), released
simultaneously by a ``threading.Barrier(6)``. They are regression tests, not
demonstrations: each FAILS on the pre-fix code path and passes on the
compare-and-swap-under-``BEGIN IMMEDIATE`` one.

TRIALERROR-DEV-NOTE (why threads and not subprocesses): unlike
``tests/test_stores_concurrency.py``, whose claim is literally "2 procs",
the claim here is about a read-modify-write window inside ONE process's
business logic, which threads reproduce exactly and far faster. The
connections are genuinely separate either way -- ``open_store`` is called
inside each thread, never shared.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

import pytest

from trialerror.artifacts.errors import GateEntryConditionError
from trialerror.artifacts.gates import (
    apply_union,
    get_gate,
    open_gate,
    record_verdict,
    send_back_edit,
    submit_gate,
    verify_edit,
)
from trialerror.artifacts.registry import create_artifact
from trialerror.ingest import pipeline
from trialerror.ingest import requests as ingest_requests
from trialerror.ingest.errors import InvalidRequestTransitionError
from trialerror.rooms.api import converge_room, create_room, freeze_room, score_dp
from trialerror.rooms.errors import IllegalRoomTransitionError
from trialerror.stores.store import open_store

from tests._rooms_fixtures import bootstrap_launch, seed_template

THREADS = 6


def _race(program_root, platform_root, work: Callable[[Any, int], Any], n: int = THREADS) -> list[Any]:
    """Run ``work(store, i)`` on ``n`` threads that all pass the same
    barrier first, and return one result per thread in launch order --
    either the returned value or the raised exception object.

    Each thread opens and closes its OWN store: the race under test is
    between separate SQLite connections, which is what a second dashboard
    tab, a CLI verb, or an agent actually is."""
    results: list[Any] = [None] * n
    barrier = threading.Barrier(n)

    def _run(i: int) -> None:
        store = open_store(program_root, platform_root=platform_root)
        try:
            barrier.wait(timeout=30)
            try:
                results[i] = work(store, i)
            except Exception as exc:  # noqa: BLE001 - the exception IS the result here
                results[i] = exc
        finally:
            store.close()

    threads = [threading.Thread(target=_run, args=(i,), name=f"race-{i}") for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a racing thread never finished (deadlock?)"
    return results


def _winners_and_losers(results: list[Any], loser_type: type[Exception]) -> tuple[list[Any], list[Exception]]:
    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, Exception)]
    unexpected = [r for r in losers if not isinstance(r, loser_type)]
    assert not unexpected, f"unexpected exception type(s) from the race: {unexpected!r}"
    return winners, losers


def _count_events(store, event_type: str, key: str, value: str) -> int:
    rows = store.ops.execute("SELECT payload FROM event WHERE type = ?", (event_type,)).fetchall()
    return sum(1 for r in rows if json.loads(r["payload"]).get(key) == value)


# ---------------------------------------------------------------------------
# rooms: freeze / converge
# ---------------------------------------------------------------------------
def _open_room(store) -> tuple[str, str]:
    launch_id = bootstrap_launch(store)
    room = create_room(
        store,
        topic="race room",
        discussion_points=[{"prompt": "does the mechanism generalize?"}],
        participants=["P1", "P2"],
        by_launch=launch_id,
    )
    return room["room_id"], launch_id


def test_six_concurrent_freezes_leave_one_frozen_room_and_one_event(program_root, platform_root):
    """WA-1's headline case, live-reproduced by the finder on freeze."""
    setup = open_store(program_root, platform_root=platform_root)
    room_id, launch_id = _open_room(setup)
    setup.close()

    results = _race(
        program_root, platform_root,
        lambda store, i: freeze_room(store, room_id=room_id, by_launch=launch_id, reason=f"reason from operator {i}"),
    )
    winners, losers = _winners_and_losers(results, IllegalRoomTransitionError)

    assert len(winners) == 1, f"expected exactly one freeze to land, got {len(winners)}"
    assert len(losers) == THREADS - 1
    # the refusal has to name the state actually found, not a generic
    # "illegal transition" -- the operator's next question is "who beat me".
    assert all("frozen" in str(e) for e in losers)

    check = open_store(program_root, platform_root=platform_root)
    try:
        state = check.ops.execute("SELECT state FROM room WHERE room_id = ?", (room_id,)).fetchone()["state"]
        assert state == "frozen"
        assert _count_events(check, "room_frozen", "room_id", room_id) == 1
    finally:
        check.close()


def test_six_concurrent_converges_leave_one_converged_room_and_one_event(program_root, platform_root):
    setup = open_store(program_root, platform_root=platform_root)
    room_id, launch_id = _open_room(setup)
    score_dp(setup, room_id=room_id, dp_id="DP1", judge=lambda _e: 97.0, by_launch=launch_id)
    setup.close()

    results = _race(
        program_root, platform_root,
        lambda store, i: converge_room(store, room_id=room_id, by_launch=launch_id),
    )
    winners, losers = _winners_and_losers(results, IllegalRoomTransitionError)

    assert len(winners) == 1
    assert len(losers) == THREADS - 1

    check = open_store(program_root, platform_root=platform_root)
    try:
        state = check.ops.execute("SELECT state FROM room WHERE room_id = ?", (room_id,)).fetchone()["state"]
        assert state == "converged"
        assert _count_events(check, "room_converged", "room_id", room_id) == 1
    finally:
        check.close()


# ---------------------------------------------------------------------------
# ingest: the acquisition queue
# ---------------------------------------------------------------------------
def test_six_concurrent_deliveries_transition_the_source_once(program_root, platform_root):
    """The dashboard's ACQUISITIONS · ONLY YOU CAN DELIVER THESE button,
    double-clicked. One transition, one event, five named refusals."""
    setup = open_store(program_root, platform_root=platform_root)
    launch_id = bootstrap_launch(setup)
    source = pipeline.register_source(
        setup, kind="book", title="Raced Book", license_tier="unknown", acquisition_route="web",
        registered_by_launch=launch_id, request_state="wanted",
    )
    source_id = source["source_id"]
    ingest_requests.transition(setup, source_id, "requested", launch_id=launch_id)
    setup.close()

    results = _race(
        program_root, platform_root,
        lambda store, i: ingest_requests.transition(store, source_id, "delivered", launch_id=launch_id),
    )
    winners, losers = _winners_and_losers(results, InvalidRequestTransitionError)

    assert len(winners) == 1
    assert len(losers) == THREADS - 1

    check = open_store(program_root, platform_root=platform_root)
    try:
        row = check.knowledge.execute(
            "SELECT request_state, delivered_ts FROM source WHERE source_id = ?", (source_id,)
        ).fetchone()
        assert row["request_state"] == "delivered"
        assert row["delivered_ts"] is not None
        assert _count_events(check, "ingest_request_transition", "source_id", source_id) == 2  # requested + delivered
    finally:
        check.close()


# ---------------------------------------------------------------------------
# gates: the whole-array read-modify-write
# ---------------------------------------------------------------------------
def _gated_gate_with_edits(store, *, n_edits: int) -> tuple[str, list[str], str]:
    """A REAL gate at ``state='gated'`` carrying ``n_edits`` blocking
    edits, built through the state machine rather than planted as a
    fixture row (``verify_edit`` refuses anything but ``gated``)."""
    launch_id = bootstrap_launch(store)
    seed_template(store, type_key="race_doc")
    artifact = create_artifact(
        store, type_key="race_doc", title="raced artifact", path="artifacts/raced.md",
        sha256="b" * 64, by_launch=launch_id, purpose="write-race regression",
    )
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    verdict = record_verdict(
        store, gate_id=gate["gate_id"], verdict="PASS_WITH_EDITS", critic_launch=launch_id,
        edits=[{"text": f"fix item {i}", "blocking": True} for i in range(n_edits)],
        reproduction_status="match",
    )
    edit_ids = [e["edit_id"] for e in json.loads(verdict["edits"])]
    return gate["gate_id"], edit_ids, launch_id


def test_six_concurrent_verifications_of_six_edits_all_survive(program_root, platform_root):
    """The severe half of WA-1: six appliers, six DIFFERENT edits, one
    gate. Pre-fix, five of the six acknowledged verifications were
    silently discarded by the last writer's whole-array write -- and
    ``apply_union`` then refused a gate the operators had every reason to
    believe was ready."""
    setup = open_store(program_root, platform_root=platform_root)
    gate_id, edit_ids, launch_id = _gated_gate_with_edits(setup, n_edits=THREADS)
    setup.close()

    results = _race(
        program_root, platform_root,
        lambda store, i: verify_edit(
            store, gate_id=gate_id, edit_id=edit_ids[i], by_launch=launch_id,
            verified_note=f"applied by operator {i}",
        ),
    )
    winners, _ = _winners_and_losers(results, Exception)
    assert len(winners) == THREADS, "every verification of a DISTINCT edit must land"

    check = open_store(program_root, platform_root=platform_root)
    try:
        edits = json.loads(get_gate(check, gate_id)["edits"])
        assert len(edits) == THREADS
        assert all(e["verified"] for e in edits), f"lost verification(s): {[e['edit_id'] for e in edits if not e['verified']]}"
        assert all(e["applied"] for e in edits)
        # every applier's own note survived -- not just the last writer's.
        assert len({e["verified_note"] for e in edits}) == THREADS
        # and the gate is genuinely ready: the union entry condition passes.
        applied = apply_union(check, gate_id=gate_id, by_launch=launch_id)
        assert applied["state"] == "union_applied"
    finally:
        check.close()


def test_six_concurrent_verifications_of_one_edit_are_idempotent(program_root, platform_root):
    """Same edit, six times over: verifying is idempotent, so all six are
    clean successes and the array holds exactly one verified entry (no
    duplicated entry, no half-written array)."""
    setup = open_store(program_root, platform_root=platform_root)
    gate_id, edit_ids, launch_id = _gated_gate_with_edits(setup, n_edits=1)
    setup.close()

    results = _race(
        program_root, platform_root,
        lambda store, i: verify_edit(store, gate_id=gate_id, edit_id=edit_ids[0], by_launch=launch_id, verified_note="applied"),
    )
    winners, _ = _winners_and_losers(results, Exception)
    assert len(winners) == THREADS

    check = open_store(program_root, platform_root=platform_root)
    try:
        edits = json.loads(get_gate(check, gate_id)["edits"])
        assert len(edits) == 1
        assert edits[0]["verified"] is True
    finally:
        check.close()


def test_six_concurrent_send_backs_of_six_edits_all_survive_and_still_block_union(program_root, platform_root):
    """``send_back_edit`` shares :func:`_mutate_edit_in_txn` with
    ``verify_edit``, so it inherits the same race -- and the same fix.
    Six objections on six edits must ALL be recorded (an erased objection
    is an erased request for work), each with its own event, and the gate
    must still refuse the union: a sent-back edit is unverified."""
    setup = open_store(program_root, platform_root=platform_root)
    gate_id, edit_ids, launch_id = _gated_gate_with_edits(setup, n_edits=THREADS)
    setup.close()

    results = _race(
        program_root, platform_root,
        lambda store, i: send_back_edit(
            store, gate_id=gate_id, edit_id=edit_ids[i], by_launch=launch_id, note=f"objection {i}",
        ),
    )
    winners, _ = _winners_and_losers(results, Exception)
    assert len(winners) == THREADS

    check = open_store(program_root, platform_root=platform_root)
    try:
        edits = json.loads(get_gate(check, gate_id)["edits"])
        assert all(e.get("sent_back") for e in edits), "lost objection(s)"
        assert len({e["sent_back_note"] for e in edits}) == THREADS
        assert not any(e["verified"] for e in edits)
        assert _count_events(check, "gate_edit_sent_back", "gate_id", gate_id) == THREADS
        with pytest.raises(GateEntryConditionError) as exc:
            apply_union(check, gate_id=gate_id, by_launch=launch_id)
        assert "not yet verified" in str(exc.value)
    finally:
        check.close()
