"""Unit-level coverage of ``trialerror.dashboard.writes.dispatch`` -- one success
path + one clean-refusal path + one missing-required-field path per write
action, called directly (no HTTP, no subprocess -- see
``tests/test_dashboard_serve.py::test_dashboard_write_actions_full_loop_subprocess``
for the real-HTTP, real-token, real-subprocess end-to-end proof; this module
is the fast, exhaustive-per-action complement to it).

Every business-logic call here goes through the SAME module function the
CLI uses (``trialerror.artifacts.gates``, ``trialerror.ingest.extract``,
``trialerror.ingest.requests``, ``trialerror.rooms.api``, ``trialerror.events.api``) --
these tests are really proving ``trialerror.dashboard.writes`` wires the field
names correctly and surfaces refusals verbatim, not re-testing those
modules' own state machines (already covered by their own test files)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trialerror.artifacts.gates import open_gate, record_verdict, submit_gate
from trialerror.artifacts.registry import create_artifact
from trialerror.dashboard import writes
from trialerror.rooms.api import create_room
from trialerror.stores import insert as store_insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def seeded(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()
    return ids


def _dispatch(program_root, platform_root, action, body):
    return writes.dispatch(action, program_root=program_root, platform_root=platform_root, body=body)


# ---------------------------------------------------------------------------
# dispatch-level plumbing (unknown action / no program root / missing field)
# ---------------------------------------------------------------------------


def test_dispatch_unknown_action_is_a_clean_refusal(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "not-a-real-action", {})
    assert result == {
        "ok": False, "status": "unknown_action",
        "message": "no such write action: 'not-a-real-action'",
    }


def test_dispatch_no_program_root_refuses_every_action(platform_root):
    result = writes.dispatch("feed-post", program_root=None, platform_root=platform_root, body={"thread_id": "x", "body": "y"})
    assert result["ok"] is False
    assert result["status"] == "no_program_root"


@pytest.mark.parametrize(
    "action", sorted(a for a in writes.WRITABLE_ACTIONS if writes.REQUIRED_FIELDS.get(a))
)
def test_dispatch_missing_required_fields_never_opens_a_store(program_root, platform_root, action, monkeypatch):
    """A missing required field is refused BEFORE ``open_store`` is ever
    called (design: no write connection should be opened for a client
    bug) -- proven by monkeypatching ``open_store`` to explode if reached.

    Actions with an EMPTY ``REQUIRED_FIELDS`` entry are excluded: they have
    no field this table can pre-check (``feed-translate`` takes exactly one
    of two alternatives), so they validate inside the handler and their
    refusal is a ``ValueError``-shaped one -- covered by
    ``tests/test_dashboard_feed_translation.py`` instead."""

    def _boom(*_a, **_k):
        raise AssertionError(f"open_store must not be called for a missing-field refusal on {action!r}")

    monkeypatch.setattr(writes, "open_store", _boom)
    result = _dispatch(program_root, platform_root, action, {})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    for field in writes.REQUIRED_FIELDS[action]:
        assert field in result["message"]


# ---------------------------------------------------------------------------
# verify-edit
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate_with_blocking_edit(program_root, platform_root, seeded):
    store = open_store(program_root, platform_root=platform_root)
    artifact = create_artifact(
        store, type_key=seeded["template"], title="edit-test artifact", path="artifacts/edit-test.md",
        sha256="9" * 64, by_launch=seeded["launch"], purpose="test",
    )
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=seeded["launch"])
    verdict = record_verdict(
        store, gate_id=gate["gate_id"], verdict="PASS_WITH_EDITS", critic_launch=seeded["launch"],
        edits=[{"text": "fix the tally", "blocking": True}],
    )
    edit_id = json.loads(verdict["edits"])[0]["edit_id"]
    store.close()
    return {"gate_id": gate["gate_id"], "edit_id": edit_id}


def test_verify_edit_success(program_root, platform_root, seeded, gate_with_blocking_edit):
    result = _dispatch(program_root, platform_root, "verify-edit", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": seeded["launch"], "verified_note": "looks good",
    })
    assert result["ok"] is True
    edits = json.loads(result["result"]["edits"])
    assert edits[0]["verified"] is True
    assert edits[0]["verified_note"] == "looks good"


def test_verify_edit_refusal_wrong_edit_id(program_root, platform_root, seeded, gate_with_blocking_edit):
    result = _dispatch(program_root, platform_root, "verify-edit", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": "EDIT-does-not-exist", "by_launch": seeded["launch"],
    })
    assert result["ok"] is False
    assert "EDIT-does-not-exist" in result["message"]


def test_verify_edit_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "verify-edit", {"gate_id": "CR-001"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "edit_id" in result["message"] and "by_launch" in result["message"]


# ---------------------------------------------------------------------------
# merge-accept / merge-reject
# ---------------------------------------------------------------------------


@pytest.fixture()
def draft_merge_proposal(program_root, platform_root, seeded):
    """A real ``PROP-``-prefixed draft merge proposal -- NOT
    ``seeded["merge_proposal"]`` (``tests/_store_fixtures.py``'s own
    minimal fixture row uses an ``MRG-`` id, a schema round-trip
    placeholder that never goes through ``trialerror.ingest.extract``'s readers;
    that module's real proposals are always ``PROP-``-prefixed,
    ``trialerror.ingest.extract._MERGE_PROPOSAL_ID_PREFIX``, and
    ``accept``/``reject`` dispatch on that exact prefix)."""
    store = open_store(program_root, platform_root=platform_root)
    ts = now()
    e1, e2 = new_id("ENT"), new_id("ENT")
    for eid, name in ((e1, "Alpha"), (e2, "Alpha II")):
        store_insert(store, "entity", {
            "entity_id": eid, "name": name, "entity_type": "concept", "resolution": "draft",
            "created_by_launch": seeded["launch"], "created_at": ts,
        })
    prop_id = new_id("PROP")
    store_insert(store, "merge_proposal", {
        "prop_id": prop_id, "canonical_entity": e1, "members": json.dumps([e1, e2]),
        "reason": "test dedup", "status": "draft", "proposed_by_launch": seeded["launch"],
    })
    store.close()
    return prop_id


def test_merge_accept_success(program_root, platform_root, seeded, draft_merge_proposal):
    result = _dispatch(program_root, platform_root, "merge-accept", {
        "prop_id": draft_merge_proposal, "by_launch": seeded["launch"],
    })
    assert result["ok"] is True
    assert result["result"]["status"] == "confirmed"


def test_merge_reject_success(program_root, platform_root, seeded, draft_merge_proposal):
    result = _dispatch(program_root, platform_root, "merge-reject", {
        "prop_id": draft_merge_proposal, "by_launch": seeded["launch"],
    })
    assert result["ok"] is True
    assert result["result"]["status"] == "rejected"


def test_merge_accept_refusal_already_decided(program_root, platform_root, seeded, draft_merge_proposal):
    first = _dispatch(program_root, platform_root, "merge-accept", {
        "prop_id": draft_merge_proposal, "by_launch": seeded["launch"],
    })
    assert first["ok"] is True
    second = _dispatch(program_root, platform_root, "merge-accept", {
        "prop_id": draft_merge_proposal, "by_launch": seeded["launch"],
    })
    assert second["ok"] is False
    assert "not draft" in second["message"]


def test_merge_reject_refusal_already_decided(program_root, platform_root, seeded, draft_merge_proposal):
    """The refusal half of merge-reject's trio (section 6's success /
    refusal / missing-field contract) -- the mirror of
    ``test_merge_accept_refusal_already_decided``. A second decline of the
    same proposal is refused by the same ``status != 'draft'`` guard, so
    neither verb can silently re-decide a settled proposal."""
    first = _dispatch(program_root, platform_root, "merge-reject", {
        "prop_id": draft_merge_proposal, "by_launch": seeded["launch"],
    })
    assert first["ok"] is True
    second = _dispatch(program_root, platform_root, "merge-reject", {
        "prop_id": draft_merge_proposal, "by_launch": seeded["launch"],
    })
    assert second["ok"] is False
    assert "not draft" in second["message"]
    assert "rejected" in second["message"]


def test_merge_accept_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "merge-accept", {"prop_id": "PROP-x"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"


def _merge_proposal_status(program_root, platform_root, prop_id):
    store = open_store(program_root, platform_root=platform_root)
    try:
        row = store.knowledge.execute(
            "SELECT status FROM merge_proposal WHERE prop_id = ?", (prop_id,)
        ).fetchone()
        return None if row is None else row["status"]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("action", "event_type"),
    [("merge-accept", "merge_proposal_accepted"), ("merge-reject", "merge_proposal_rejected")],
)
def test_merge_decision_refuses_an_unknown_launch_before_it_writes(
    program_root, platform_root, draft_merge_proposal, action, event_type
):
    """L-C2, the half that is easy to get backwards (lane C, finding F1).

    ``event.launch_id`` is an XID column, so an unknown ``by_launch`` was
    always refused -- but it used to be refused by the audit insert, which
    runs LAST. The proposal had already moved to confirmed/rejected and the
    member entities had already been rewritten by then, so the page said
    "refused", the store said "decided", no audit row explained it, and the
    operator's retry hit "is not draft" with no way back.

    The contract asserted here is the whole of it: named error, the id in
    the message, and THE STORE UNTOUCHED -- proposal still draft, no
    event."""
    result = _dispatch(program_root, platform_root, action, {
        "prop_id": draft_merge_proposal, "by_launch": "LNCH-does-not-exist",
    })
    assert result["ok"] is False
    assert result["status"] == "XidTargetMissingError"
    assert "LNCH-does-not-exist" in result["message"]
    assert _merge_proposal_status(program_root, platform_root, draft_merge_proposal) == "draft"
    assert _events_of_type(program_root, platform_root, event_type) == []
    # ...and because nothing moved, the operator's retry with a real launch
    # still works -- the refusal left a recoverable state, not a dead item.
    retry = _dispatch(program_root, platform_root, action, {
        "prop_id": draft_merge_proposal, "by_launch": _one_launch(program_root, platform_root),
    })
    assert retry["ok"] is True


def _one_launch(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    try:
        return store.platform.execute("SELECT launch_id FROM launch LIMIT 1").fetchone()["launch_id"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# acquisition-delivered
# ---------------------------------------------------------------------------


def test_acquisition_delivered_success(program_root, platform_root, seeded):
    store = open_store(program_root, platform_root=platform_root)
    from trialerror.ingest.pipeline import register_source

    row = register_source(
        store, kind="paper", title="wanted paper", license_tier="open", acquisition_route="web",
        registered_by_launch=seeded["launch"], request_state="requested",
    )
    store.close()
    result = _dispatch(program_root, platform_root, "acquisition-delivered", {"source_id": row["source_id"]})
    assert result["ok"] is True
    assert result["result"]["request_state"] == "delivered"


def test_acquisition_delivered_refusal_wrong_state(program_root, platform_root, seeded):
    # the fixture's source lands at request_state='indexed' -- terminal,
    # 'delivered' is not a legal transition from there.
    result = _dispatch(program_root, platform_root, "acquisition-delivered", {"source_id": seeded["source"]})
    assert result["ok"] is False
    assert "not a permitted request-queue transition" in result["message"]


def test_acquisition_delivered_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "acquisition-delivered", {})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"


def test_acquisition_delivered_refuses_an_unknown_launch_before_it_writes(
    program_root, platform_root, seeded
):
    """The request-queue half of finding F1. ``launch_id`` is optional on
    this action, but when it IS given and names no launch, the transition
    must not happen: the source stays `requested`, no
    `ingest_request_transition` event is written, and the operator can
    deliver it again once the id is right. Before the fix the source read
    `delivered` while the page reported a refusal."""
    store = open_store(program_root, platform_root=platform_root)
    from trialerror.ingest.pipeline import register_source

    row = register_source(
        store, kind="paper", title="a paper nobody has delivered", license_tier="open",
        acquisition_route="web", registered_by_launch=seeded["launch"], request_state="requested",
    )
    store.close()
    source_id = row["source_id"]

    result = _dispatch(program_root, platform_root, "acquisition-delivered", {
        "source_id": source_id, "launch_id": "LNCH-does-not-exist",
    })
    assert result["ok"] is False
    assert result["status"] == "XidTargetMissingError"
    assert "LNCH-does-not-exist" in result["message"]

    store = open_store(program_root, platform_root=platform_root)
    try:
        fresh = store.knowledge.execute(
            "SELECT request_state, delivered_ts FROM source WHERE source_id = ?", (source_id,)
        ).fetchone()
        assert fresh["request_state"] == "requested"
        assert fresh["delivered_ts"] is None
    finally:
        store.close()
    assert _events_of_type(program_root, platform_root, "ingest_request_transition") == []

    retry = _dispatch(program_root, platform_root, "acquisition-delivered", {
        "source_id": source_id, "launch_id": seeded["launch"],
    })
    assert retry["ok"] is True
    assert retry["result"]["request_state"] == "delivered"


# ---------------------------------------------------------------------------
# rooms: room-turn / room-score / room-freeze
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_room(program_root, platform_root, seeded):
    store = open_store(program_root, platform_root=platform_root)
    room = create_room(
        store, topic="e2e room", discussion_points=[{"prompt": "does it hold?"}],
        participants=["p1", "p2"], by_launch=seeded["launch"],
    )
    store.close()
    return room["room_id"]


def test_room_turn_success(program_root, platform_root, seeded, real_room):
    result = _dispatch(program_root, platform_root, "room-turn", {
        "room_id": real_room, "launch_id": seeded["launch"], "dp_id": "DP1", "body": "my turn",
    })
    assert result["ok"] is True
    assert result["result"]["body"] == "my turn"


def test_room_turn_refusal_unknown_dp(program_root, platform_root, seeded, real_room):
    result = _dispatch(program_root, platform_root, "room-turn", {
        "room_id": real_room, "launch_id": seeded["launch"], "dp_id": "DP-nope", "body": "x",
    })
    assert result["ok"] is False
    assert "DP-nope" in result["message"]


def test_room_turn_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "room-turn", {"room_id": "ROOM-x"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"


def test_room_score_success(program_root, platform_root, seeded, real_room):
    result = _dispatch(program_root, platform_root, "room-score", {
        "room_id": real_room, "dp_id": "DP1", "agreement_pct": 95, "by_launch": seeded["launch"], "note": "great",
    })
    assert result["ok"] is True
    assert result["result"]["agreement_pct"] == 95.0
    assert result["result"]["converged"] is True


def test_room_score_refusal_out_of_range(program_root, platform_root, seeded, real_room):
    result = _dispatch(program_root, platform_root, "room-score", {
        "room_id": real_room, "dp_id": "DP1", "agreement_pct": 150, "by_launch": seeded["launch"],
    })
    assert result["ok"] is False
    assert "0, 100" in result["message"] or "[0, 100]" in result["message"]


def test_room_score_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "room-score", {"room_id": "ROOM-x", "dp_id": "DP1"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "agreement_pct" in result["message"] and "by_launch" in result["message"]


def test_room_freeze_success(program_root, platform_root, seeded, real_room):
    result = _dispatch(program_root, platform_root, "room-freeze", {
        "room_id": real_room, "by_launch": seeded["launch"], "reason": "deadlock on DP1",
    })
    assert result["ok"] is True
    assert result["result"]["state"] == "frozen"


def test_room_freeze_refusal_already_frozen(program_root, platform_root, seeded, real_room):
    first = _dispatch(program_root, platform_root, "room-freeze", {
        "room_id": real_room, "by_launch": seeded["launch"], "reason": "deadlock",
    })
    assert first["ok"] is True
    second = _dispatch(program_root, platform_root, "room-freeze", {
        "room_id": real_room, "by_launch": seeded["launch"], "reason": "still deadlocked",
    })
    assert second["ok"] is False


def test_room_freeze_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "room-freeze", {"room_id": "ROOM-x"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "by_launch" in result["message"] and "reason" in result["message"]


# ---------------------------------------------------------------------------
# feed-post
# ---------------------------------------------------------------------------


def test_feed_post_success_authorship_is_server_derived(program_root, platform_root, seeded):
    result = _dispatch(program_root, platform_root, "feed-post", {
        "thread_id": seeded["thread"], "body": "operator directive", "launch_id": "LNCH-should-be-ignored",
    })
    assert result["ok"] is True
    # authorship is server-derived -- a caller-supplied launch_id in the
    # body is silently ignored (the CLI/HTTP layer never accepts a
    # launch_id for feed-post at all; this proves the underlying dispatch
    # function itself hardcodes launch_id=None regardless of extra keys).
    assert result["result"]["author"].startswith("orchestrator:")
    assert result["result"]["author"].split(":", 1)[1] == seeded["session"]


def test_feed_post_refusal_unknown_thread(program_root, platform_root, seeded):
    # feed_post.thread_id REFERENCES thread(thread_id) -- a bad thread_id
    # fails at the SQLite FK layer, translated by trialerror.stores.writer.insert
    # into a clean ValidationError, never a raw sqlite3.IntegrityError.
    result = _dispatch(program_root, platform_root, "feed-post", {"thread_id": "THR-nope", "body": "x"})
    assert result["ok"] is False
    assert result["message"]


def test_feed_post_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "feed-post", {"thread_id": "THR-x"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "body" in result["message"]


# ---------------------------------------------------------------------------
# W3 -- field-type validation (M-WA-1 / M-WA-2, sweep §5 promoted to batch W3)
#
# `_missing_fields` only ever asked "is this absent or blank". A JSON object
# in a string field is neither, so it passed, reached sqlite as a `dict`, and
# died there with `sqlite3.ProgrammingError` -- an exception nothing caught,
# so the HTTP layer answered with a closed socket. A JSON NUMBER in the same
# field was worse: sqlite binds it happily, so `verified_note` / `reason`
# quietly stored a value that was never text. `_validate_fields` closes both
# with one table.
# ---------------------------------------------------------------------------


def test_a_json_object_in_a_string_field_is_a_named_bad_request(program_root, platform_root, seeded):
    result = _dispatch(program_root, platform_root, "feed-post", {
        "thread_id": seeded["thread"], "body": {"x": 1},
    })
    assert result["ok"] is False
    assert result["status"] == "bad_request"
    assert "body must be a string, got dict" in result["message"]


def test_a_number_in_a_text_column_field_is_refused_not_silently_stored(program_root, platform_root, seeded):
    """M-WA-2: `verified_note` is a text column; before the type table a
    JSON number landed in it and nothing said so."""
    result = _dispatch(program_root, platform_root, "verify-edit", {
        "gate_id": "CR-001", "edit_id": "EDIT-x", "by_launch": seeded["launch"], "verified_note": 42,
    })
    assert result["ok"] is False
    assert result["status"] == "bad_request"
    assert "verified_note must be a string, got int" in result["message"]


def test_a_bad_type_never_opens_a_store(program_root, platform_root, monkeypatch):
    """Same discipline the missing-field check has always had: a client bug
    gets no write connection."""

    def _boom(*_a, **_k):
        raise AssertionError("open_store must not be called for a bad-type refusal")

    monkeypatch.setattr(writes, "open_store", _boom)
    result = _dispatch(program_root, platform_root, "feed-post", {"thread_id": "T", "body": ["a", "list"]})
    assert result["status"] == "bad_request"


def test_missing_fields_is_reported_before_bad_types(program_root, platform_root):
    """One refusal at a time, and the more basic one first -- a caller
    that omitted a field should be told that, not handed a type lecture
    about a different field."""
    result = _dispatch(program_root, platform_root, "feed-post", {"body": {"x": 1}})
    assert result["status"] == "missing_fields"
    assert "thread_id" in result["message"]


def test_agreement_pct_accepts_a_json_number_and_a_numeric_string(program_root, platform_root, seeded):
    """The one non-string field in the table. Both shapes a real caller
    produces -- a JSON number, and the string an `<input type="number">`
    hands back -- must pass the type gate; whether the ACTION then succeeds
    is the room state machine's business, not this check's."""
    for value in (91.5, "91.5"):
        result = _dispatch(program_root, platform_root, "room-score", {
            "room_id": "ROOM-nope", "dp_id": "DP1", "agreement_pct": value, "by_launch": seeded["launch"],
        })
        assert result["status"] != "bad_request", f"{value!r} should pass the type gate"


def test_agreement_pct_refuses_a_boolean(program_root, platform_root, seeded):
    """`True` is an `int` in Python and would score a discussion point at
    1%."""
    result = _dispatch(program_root, platform_root, "room-score", {
        "room_id": "ROOM-x", "dp_id": "DP1", "agreement_pct": True, "by_launch": seeded["launch"],
    })
    assert result["status"] == "bad_request"
    assert "agreement_pct must be a int/float/str, got bool" in result["message"]


def test_verify_errors_are_clean_refusals_not_server_faults(program_root, platform_root):
    """`_EXPECTED_ERRORS` gains `trialerror.verify.errors.VerifyError` (spec
    §4). A tampered pre-registration escrow is a FINDING the operator must
    read, and it would otherwise reach the HTTP layer as a 500 with the
    message buried in a server log."""
    from trialerror.verify.errors import PreregTamperedError, VerifyError

    assert issubclass(PreregTamperedError, VerifyError)
    assert any(issubclass(VerifyError, expected) or expected is VerifyError for expected in writes._EXPECTED_ERRORS)

    def _raiser(_store, _body):
        raise PreregTamperedError("escrow hash does not match the sealed procedure")

    original = writes.WRITABLE_ACTIONS.get("__verify_probe__")
    writes.WRITABLE_ACTIONS["__verify_probe__"] = _raiser
    try:
        result = _dispatch(program_root, platform_root, "__verify_probe__", {})
    finally:
        if original is None:
            del writes.WRITABLE_ACTIONS["__verify_probe__"]
        else:  # pragma: no cover - defensive
            writes.WRITABLE_ACTIONS["__verify_probe__"] = original
    assert result["ok"] is False
    assert result["status"] == "PreregTamperedError"
    assert result["message"] == "escrow hash does not match the sealed procedure"


# ===========================================================================
# Lane C step C7 -- the four actions the Determinations queue drew disabled.
# Spec section 4; rulings L-C2 (identity), L-C3 (reveal), L-C6 (no REJECT).
# Each gets the house trio: success / clean refusal / missing field.
# ===========================================================================


def _events_of_type(program_root, platform_root, event_type):
    store = open_store(program_root, platform_root=platform_root)
    try:
        rows = store.ops.execute(
            "SELECT * FROM event WHERE type = ? ORDER BY ts, rowid", (event_type,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# prereg-reveal
# ---------------------------------------------------------------------------


@pytest.fixture()
def committed_prereg(program_root, platform_root, seeded):
    from trialerror.verify.prereg import commit_prereg

    store = open_store(program_root, platform_root=platform_root)
    try:
        row = commit_prereg(
            store, title="the sealed thing", procedure="run the thing twice", params={"n": 2}
        )
    finally:
        store.close()
    return row


def test_prereg_reveal_success_writes_the_file_the_row_and_one_event(
    program_root, platform_root, committed_prereg
):
    result = _dispatch(program_root, platform_root, "prereg-reveal", {"prereg_id": committed_prereg["prereg_id"]})
    assert result["ok"] is True, result
    revealed = result["result"]
    assert revealed["procedure"] == "run the thing twice"
    assert revealed["params"] == {"n": 2}
    assert revealed["status"] == "revealed"

    # the file lands under the PROGRAM tree, at the server's own chosen path
    path = Path(revealed["revealed_path"])
    assert path.is_file()
    assert path.parent == Path(program_root) / "prereg" / "revealed"
    assert json.loads(path.read_text(encoding="utf-8"))["procedure"] == "run the thing twice"

    store = open_store(program_root, platform_root=platform_root)
    try:
        row = store.ops.execute(
            "SELECT status, revealed_ts FROM prereg WHERE prereg_id = ?", (committed_prereg["prereg_id"],)
        ).fetchone()
    finally:
        store.close()
    assert row["status"] == "revealed"
    assert row["revealed_ts"] == revealed["revealed_ts"]

    # exactly one prereg_revealed event, carrying the HASHES and not the content
    events = _events_of_type(program_root, platform_root, "prereg_revealed")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["prereg_id"] == committed_prereg["prereg_id"]
    assert payload["procedure_sha256"] == committed_prereg["procedure_sha256"]
    assert payload["params_sha256"] == committed_prereg["params_sha256"]
    assert payload["revealed_path"] == revealed["revealed_path"]
    assert "procedure" not in payload and "params" not in payload, \
        "an event log is not the place to un-blind a procedure a second time"


def test_prereg_reveal_a_second_time_is_a_clean_refusal(program_root, platform_root, committed_prereg):
    """Finding F2, at the dispatch surface. The Determinations queue drops
    the item after the first reveal, so only a direct HTTP or CLI call can
    reach this -- and it must come back as a refusal the operator can read,
    not a 500 (`PreregAlreadyRevealedError` is a `VerifyError`, which
    `_EXPECTED_ERRORS` already carries)."""
    first = _dispatch(program_root, platform_root, "prereg-reveal", {"prereg_id": committed_prereg["prereg_id"]})
    assert first["ok"] is True, first
    second = _dispatch(program_root, platform_root, "prereg-reveal", {"prereg_id": committed_prereg["prereg_id"]})
    assert second["ok"] is False
    assert second["status"] == "PreregAlreadyRevealedError"
    assert "already revealed" in second["message"]
    assert len(_events_of_type(program_root, platform_root, "prereg_revealed")) == 1


def test_prereg_reveal_records_the_dashboard_session(program_root, platform_root, seeded, committed_prereg):
    """L-C3: the event carries the session that broke the blind."""
    result = _dispatch(program_root, platform_root, "prereg-reveal", {
        "prereg_id": committed_prereg["prereg_id"], "session_id": seeded["session"],
    })
    assert result["ok"] is True, result
    events = _events_of_type(program_root, platform_root, "prereg_revealed")
    assert [e["session_id"] for e in events] == [seeded["session"]]


def test_prereg_reveal_refuses_an_invented_session_before_breaking_the_blind(
    program_root, platform_root, committed_prereg
):
    """An attribution problem must refuse BEFORE the irreversible act, not
    after it -- `event.session_id` is a same-file FK, so the insert would
    otherwise fail with the blind already broken and no audit row written."""
    result = _dispatch(program_root, platform_root, "prereg-reveal", {
        "prereg_id": committed_prereg["prereg_id"], "session_id": "SESS-invented",
    })
    assert result["ok"] is False
    assert "SESS-invented" in result["message"]

    store = open_store(program_root, platform_root=platform_root)
    try:
        status = store.ops.execute(
            "SELECT status FROM prereg WHERE prereg_id = ?", (committed_prereg["prereg_id"],)
        ).fetchone()["status"]
    finally:
        store.close()
    assert status == "committed", "nothing was revealed"
    assert _events_of_type(program_root, platform_root, "prereg_revealed") == []


def test_prereg_reveal_of_a_tampered_escrow_refuses_cleanly_and_voids_the_row(
    program_root, platform_root, committed_prereg
):
    """A tampered escrow is a FINDING, surfaced verbatim -- never a 500. The
    row is voided as a side effect, so the item leaves the queue, and the
    message says so."""
    escrow = Path(committed_prereg["escrow_path"])
    escrow.write_text(json.dumps({"title": "x", "procedure": "SOMETHING ELSE", "params": {}}), encoding="utf-8")

    result = _dispatch(program_root, platform_root, "prereg-reveal", {"prereg_id": committed_prereg["prereg_id"]})
    assert result["ok"] is False
    assert result["status"] == "PreregTamperedError"
    assert "voided" in result["message"]

    store = open_store(program_root, platform_root=platform_root)
    try:
        status = store.ops.execute(
            "SELECT status FROM prereg WHERE prereg_id = ?", (committed_prereg["prereg_id"],)
        ).fetchone()["status"]
    finally:
        store.close()
    assert status == "voided"
    assert _events_of_type(program_root, platform_root, "prereg_revealed") == [], \
        "nothing was revealed, so nothing is recorded as revealed"


def test_prereg_reveal_of_an_unknown_id_is_a_clean_refusal(program_root, platform_root, seeded):
    result = _dispatch(program_root, platform_root, "prereg-reveal", {"prereg_id": "PREG-nope"})
    assert result["ok"] is False
    assert result["status"] == "PreregNotFoundError"
    assert "PREG-nope" in result["message"]


def test_prereg_reveal_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "prereg-reveal", {})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "prereg_id" in result["message"]


def test_prereg_reveal_never_takes_dest_dir_from_the_browser(program_root, platform_root, committed_prereg):
    """A caller naming a write path is a path-traversal primitive. The field
    is not in the handler at all, so it is refused by the closed type table's
    sibling rule: an unknown field is simply never read."""
    import inspect

    source = inspect.getsource(writes._do_prereg_reveal)
    assert "dest_dir" not in source.split('"""')[-1], "dest_dir is not passed through from the body"

    result = _dispatch(program_root, platform_root, "prereg-reveal", {
        "prereg_id": committed_prereg["prereg_id"], "dest_dir": str(Path(program_root) / "elsewhere"),
    })
    assert result["ok"] is True, result
    assert Path(result["result"]["revealed_path"]).parent == Path(program_root) / "prereg" / "revealed"
    assert not (Path(program_root) / "elsewhere").exists()


# ---------------------------------------------------------------------------
# memory-resolve
# ---------------------------------------------------------------------------


@pytest.fixture()
def memory_conflict(program_root, platform_root, tmp_path):
    """One open conflict group in THIS program's ops.db. Built the way the
    real harness produces one: two accounts' memory reconciled through the
    markdown export/import boundary, never a shared ops.db."""
    from trialerror.memory.api import put_item
    from trialerror.memory.render import export_memory, import_memory
    from tests._memory_fixtures import make_account

    other_root = tmp_path / "other_program"
    other_root.mkdir()
    other = open_store(other_root, platform_root=platform_root)
    mine = open_store(program_root, platform_root=platform_root)
    try:
        account_other = make_account(other, label="other account")
        account_mine = make_account(mine, label="my account")
        put_item(other, key="topic", tier="L0", kind="rule", body="THEIR body", account_id=account_other)
        put_item(mine, key="topic", tier="L0", kind="rule", body="MY body", account_id=account_mine)
        export_dir = tmp_path / "export"
        export_memory(other, out_dir=export_dir)
        result = import_memory(mine, in_dir=export_dir)
        group_id = result.conflicts[0]["group_id"]
    finally:
        other.close()
        mine.close()
    return group_id


@pytest.mark.parametrize(
    "keep,expected",
    [
        ("left", ("active", "superseded")),
        ("right", ("superseded", "active")),
        ("both", ("active", "active")),
    ],
)
def test_memory_resolve_each_keep_value(program_root, platform_root, memory_conflict, keep, expected):
    result = _dispatch(program_root, platform_root, "memory-resolve", {
        "group_id": memory_conflict, "keep": keep,
    })
    assert result["ok"] is True, result
    assert result["result"]["keep"] == keep

    store = open_store(program_root, platform_root=platform_root)
    try:
        statuses = tuple(
            store.ops.execute(
                "SELECT status FROM memory_item WHERE memory_item_id = ?", (result["result"][side],)
            ).fetchone()["status"]
            for side in ("left_id", "right_id")
        )
    finally:
        store.close()
    assert statuses == expected


def test_memory_resolve_writes_one_event(program_root, platform_root, memory_conflict):
    """The rows record the OUTCOME; nothing in them says somebody chose."""
    _dispatch(program_root, platform_root, "memory-resolve", {"group_id": memory_conflict, "keep": "left"})
    events = _events_of_type(program_root, platform_root, "memory_conflict_resolved")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["group_id"] == memory_conflict
    assert payload["keep"] == "left"
    assert payload["key"] == "topic"
    assert payload["left_id"] and payload["right_id"]


def test_memory_resolve_is_one_shot(program_root, platform_root, memory_conflict):
    """A double-click cannot quietly re-answer the group differently."""
    first = _dispatch(program_root, platform_root, "memory-resolve", {"group_id": memory_conflict, "keep": "left"})
    assert first["ok"] is True
    second = _dispatch(program_root, platform_root, "memory-resolve", {"group_id": memory_conflict, "keep": "right"})
    assert second["ok"] is False
    assert "one-shot" in second["message"]
    assert len(_events_of_type(program_root, platform_root, "memory_conflict_resolved")) == 1


def test_memory_resolve_refusals_come_from_the_module(program_root, platform_root, memory_conflict, seeded):
    unknown = _dispatch(program_root, platform_root, "memory-resolve", {"group_id": "no-such-group", "keep": "left"})
    assert unknown["ok"] is False
    assert "no conflict group" in unknown["message"]

    bad_keep = _dispatch(program_root, platform_root, "memory-resolve", {"group_id": memory_conflict, "keep": "middle"})
    assert bad_keep["ok"] is False
    assert "'left'|'right'|'both'" in bad_keep["message"], "the three legal values are named once, in the module"


def test_memory_resolve_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "memory-resolve", {"group_id": "g"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "keep" in result["message"]


# ---------------------------------------------------------------------------
# gate-send-back
# ---------------------------------------------------------------------------


def test_gate_send_back_success_marks_the_entry_and_writes_one_event(
    program_root, platform_root, seeded, gate_with_blocking_edit
):
    result = _dispatch(program_root, platform_root, "gate-send-back", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": seeded["launch"], "note": "the tally is right; the caption is not",
    })
    assert result["ok"] is True, result
    entry = json.loads(result["result"]["edits"])[0]
    assert entry["sent_back"] is True
    assert entry["sent_back_note"] == "the tally is right; the caption is not"
    assert entry["sent_back_by_launch"] == seeded["launch"]
    assert entry["sent_back_ts"]
    assert entry["verified"] is False and entry["applied"] is False

    events = _events_of_type(program_root, platform_root, "gate_edit_sent_back")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["gate_id"] == gate_with_blocking_edit["gate_id"]
    assert payload["edit_id"] == gate_with_blocking_edit["edit_id"]
    assert payload["note"] == "the tally is right; the caption is not"


def test_a_sent_back_edit_still_blocks_the_union(
    program_root, platform_root, seeded, gate_with_blocking_edit
):
    """Sending back is a REQUEST FOR WORK, not a way around the gate."""
    from trialerror.artifacts.errors import ArtifactsError
    from trialerror.artifacts.gates import apply_union

    _dispatch(program_root, platform_root, "gate-send-back", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": seeded["launch"], "note": "no",
    })
    store = open_store(program_root, platform_root=platform_root)
    try:
        with pytest.raises(ArtifactsError) as exc:
            apply_union(store, gate_id=gate_with_blocking_edit["gate_id"], by_launch=seeded["launch"])
    finally:
        store.close()
    assert "verified" in str(exc.value)


def test_gate_send_back_after_a_verify_is_refused(
    program_root, platform_root, seeded, gate_with_blocking_edit
):
    """Re-opening a verification is a verdict-level act, not an applier-level
    one."""
    verified = _dispatch(program_root, platform_root, "verify-edit", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": seeded["launch"],
    })
    assert verified["ok"] is True
    result = _dispatch(program_root, platform_root, "gate-send-back", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": seeded["launch"], "note": "changed my mind",
    })
    assert result["ok"] is False
    assert "verified" in result["message"]
    assert _events_of_type(program_root, platform_root, "gate_edit_sent_back") == []


def test_gate_send_back_refuses_an_unknown_launch_and_never_falls_back(
    program_root, platform_root, gate_with_blocking_edit
):
    """L-C2's interim rule, stated as a test: `by_launch` stays free text on
    the wire, and an id with no `platform.launch` row FAILS WITH THE NAMED
    ERROR. It must never quietly attribute the objection to somebody else --
    an unattributable objection is worse than a refused one."""
    result = _dispatch(program_root, platform_root, "gate-send-back", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": "LNCH-does-not-exist", "note": "no",
    })
    assert result["ok"] is False
    assert result["status"] == "XidTargetMissingError"
    assert "LNCH-does-not-exist" in result["message"]
    assert _events_of_type(program_root, platform_root, "gate_edit_sent_back") == []


@pytest.mark.parametrize("by_launch_action", ["verify-edit", "gate-send-back"])
def test_every_by_launch_write_refuses_on_a_missing_launch(
    program_root, platform_root, gate_with_blocking_edit, by_launch_action
):
    """The same rule for both verbs on this queue -- one of them refusing and
    the other falling back would be the worst of both."""
    body = {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": "LNCH-does-not-exist",
    }
    if by_launch_action == "gate-send-back":
        body["note"] = "no"
    result = _dispatch(program_root, platform_root, by_launch_action, body)
    assert result["ok"] is False
    assert result["status"] == "XidTargetMissingError"


def test_gate_send_back_missing_field(program_root, platform_root, seeded, gate_with_blocking_edit):
    """`note` is required: a send-back with no stated objection is the
    freeze-without-reason case."""
    result = _dispatch(program_root, platform_root, "gate-send-back", {
        "gate_id": gate_with_blocking_edit["gate_id"], "edit_id": gate_with_blocking_edit["edit_id"],
        "by_launch": seeded["launch"],
    })
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "note" in result["message"]


def test_no_reject_verb_is_wired(program_root, platform_root):
    """Ruling L-C6: REJECT (`gated -> failed`) is legal in the state machine
    and stays a CLI verdict path. A destructive verb driven by a free-text
    identity is not auditable."""
    assert "gate-reject" not in writes.WRITABLE_ACTIONS
    assert not any("reject" in a and "gate" in a for a in writes.WRITABLE_ACTIONS)
    result = _dispatch(program_root, platform_root, "gate-reject", {})
    assert result["status"] == "unknown_action"


# ---------------------------------------------------------------------------
# thread-create
# ---------------------------------------------------------------------------


def test_thread_create_success_is_authored_by_the_orchestrator(program_root, platform_root, seeded):
    from trialerror.events.api import get_thread_posts, list_threads

    result = _dispatch(program_root, platform_root, "thread-create", {
        "title": "a thread the operator opened", "body": "and the first thing said in it",
    })
    assert result["ok"] is True, result
    payload = result["result"]
    assert payload["author"].startswith("orchestrator:")
    assert payload["author"] == f"orchestrator:{seeded['session']}"

    store = open_store(program_root, platform_root=platform_root)
    try:
        thread = next(t for t in list_threads(store) if t["thread_id"] == payload["thread_id"])
        posts = get_thread_posts(store, thread_id=payload["thread_id"])
    finally:
        store.close()
    assert thread["title"] == "a thread the operator opened"
    assert thread["created_by_launch"] is None, "ops v8: nullable, and the operator has no launch"
    assert thread["created_by"] == payload["author"]
    assert [p["post_id"] for p in posts] == [payload["post_id"]]
    assert posts[0]["body"] == "and the first thing said in it"
    assert posts[0]["author"] == payload["author"]


def test_thread_create_never_takes_an_author_from_the_body(program_root, platform_root, seeded):
    """Authorship is server-derived. A body field claiming a launch is simply
    not read -- the same guarantee `feed-post` has always had."""
    result = _dispatch(program_root, platform_root, "thread-create", {
        "title": "t", "body": "b", "launch_id": seeded["launch"], "author": "somebody-else",
    })
    assert result["ok"] is True, result
    assert result["result"]["author"] == f"orchestrator:{seeded['session']}"


def test_thread_create_refused_with_no_open_session(program_root, platform_root, seeded):
    from trialerror.stores.writer import update as store_update

    store = open_store(program_root, platform_root=platform_root)
    try:
        store_update(
            store, "session", pk_column="session_id", pk_value=seeded["session"],
            changes={"status": "closed"},
        )
    finally:
        store.close()

    result = _dispatch(program_root, platform_root, "thread-create", {"title": "t", "body": "b"})
    assert result["ok"] is False
    assert "no open session" in result["message"]

    store = open_store(program_root, platform_root=platform_root)
    try:
        titles = [r["title"] for r in store.ops.execute("SELECT title FROM thread").fetchall()]
    finally:
        store.close()
    assert "t" not in titles, "the refusal comes before the thread row, not after it"


def test_thread_create_missing_field(program_root, platform_root):
    """An empty thread is a room with nobody in it."""
    result = _dispatch(program_root, platform_root, "thread-create", {"title": "a title and nothing else"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"
    assert "body" in result["message"]


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------


def test_every_lane_c_action_is_registered_with_its_required_fields():
    for action, required in (
        ("prereg-reveal", ("prereg_id",)),
        ("memory-resolve", ("group_id", "keep")),
        ("gate-send-back", ("gate_id", "edit_id", "by_launch", "note")),
        ("thread-create", ("title", "body")),
    ):
        assert action in writes.WRITABLE_ACTIONS, action
        assert writes.REQUIRED_FIELDS[action] == required, action


def test_the_lane_c_actions_add_no_non_string_fields():
    """`_NON_STRING_FIELDS` is what makes `_validate_fields` a CLOSED check.
    All four new actions take strings only, so the table is unchanged -- and
    this asserts that rather than leaving it to be noticed later."""
    assert set(writes._NON_STRING_FIELDS) == {"agreement_pct"}
