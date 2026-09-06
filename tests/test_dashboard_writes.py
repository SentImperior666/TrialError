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


def test_merge_accept_missing_field(program_root, platform_root):
    result = _dispatch(program_root, platform_root, "merge-accept", {"prop_id": "PROP-x"})
    assert result["ok"] is False
    assert result["status"] == "missing_fields"


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
