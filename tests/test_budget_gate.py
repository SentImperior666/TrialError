"""Tests for ``trialerror.budget.gate`` — the atomic spawn-time booking claim
(design Section 5.4 PreToolUse row; review finding F2). This is the
design's central original contribution; every M3 acceptance criterion
about spawning lives here, including the adversarial token-reuse case the
design review explicitly added.
"""

from __future__ import annotations

import json

import pytest

from trialerror.budget.gate import (
    evaluate_spawn,
    evaluate_spawn_for_open_session,
    extract_launch_id_token,
    resolve_open_session,
)
from trialerror.budget.pools import book_launch
from trialerror.stores import get, insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._budget_fixtures import add_law_digest, open_account_session


def _book(store, session_id, **overrides):
    kwargs = dict(
        session_id=session_id,
        program_id="PROG-test",
        agent_kind="lens",
        model_class="mid",
        model="sonnet",
        purpose="mechanical",
        est_tokens=100,
    )
    kwargs.update(overrides)
    return book_launch(store, **kwargs)


def _prompt(token: str) -> str:
    return f"You are agent X. launch_id: {token} — go do the thing."


# ---- token extraction -------------------------------------------------


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("launch_id: LNCH-01ARZ3NDEKTSV4RRFFQ69G5FAV", "LNCH-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("launch_id=LNCH-01ARZ3NDEKTSV4RRFFQ69G5FAV", "LNCH-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("prefix text\nlaunch_id:   LNCH-01ARZ3NDEKTSV4RRFFQ69G5FAV\nsuffix", "LNCH-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("no token here at all", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_launch_id_token(prompt, expected):
    assert extract_launch_id_token(prompt) == expected


# ---- no token / unknown token -------------------------------------------


def test_no_token_in_prompt_refused(store):
    account_id, session_id = open_account_session(store)
    result = evaluate_spawn(store, "go do research, no ids here", session_id=session_id)
    assert result.allowed is False
    assert result.code == "no_launch_id_token"


def test_unknown_launch_id_refused(store):
    account_id, session_id = open_account_session(store)
    fake_token = new_id("LNCH")
    result = evaluate_spawn(store, _prompt(fake_token), session_id=session_id)
    assert result.allowed is False
    assert result.code == "unknown_launch_id"


# ---- THE core F2 test: consumption + adversarial reuse ---------------------


def test_booked_task_passes_and_consumes_the_booking(store):
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id)

    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id)

    assert result.allowed is True
    assert result.code == "consumed"
    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["state"] == "RUNNING"


def test_same_launch_id_token_on_a_second_spawn_is_refused(store):
    """The adversarial case the design review explicitly added (design
    Section 12 M3 row: 'same launch_id token on a second spawn refused')
    — a copy-pasted stale token cannot ride an old booking."""
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id)
    prompt = _prompt(booked.launch_id)

    first = evaluate_spawn(store, prompt, session_id=session_id)
    assert first.allowed is True

    second = evaluate_spawn(store, prompt, session_id=session_id)
    assert second.allowed is False
    assert second.code == "token_not_provisional"
    assert second.details["state"] == "RUNNING"

    # And a third attempt behaves identically -- not just "blocked once".
    third = evaluate_spawn(store, prompt, session_id=session_id)
    assert third.allowed is False
    assert third.code == "token_not_provisional"


def test_unbooked_spawn_is_refused_end_to_end(store):
    """'unbooked Task refused' (design Section 12 M3 row) — no launch row
    exists at all for the token in the prompt."""
    account_id, session_id = open_account_session(store)
    result = evaluate_spawn(store, "launch_id: " + new_id("LNCH"), session_id=session_id)
    assert result.allowed is False
    assert result.code == "unknown_launch_id"


def test_already_terminal_state_refused(store):
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id)
    # Force the booking into a terminal state without going through the gate.
    store.platform.execute(
        "UPDATE launch SET state = 'ABANDONED' WHERE launch_id = ?", (booked.launch_id,)
    )
    store.platform.commit()

    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id)
    assert result.allowed is False
    assert result.code == "token_not_provisional"
    assert result.details["state"] == "ABANDONED"


# ---- wrong session -------------------------------------------------------


def test_wrong_session_refused(store):
    account_a, session_a = open_account_session(store)
    booked = _book(store, session_a)

    # A second, unrelated open session (different account).
    account_b, session_b = open_account_session(store)

    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_b)
    assert result.allowed is False
    assert result.code == "wrong_session"

    # The booking is untouched -- still claimable by its real session.
    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["state"] == "PROVISIONAL"


# ---- TTL expiry -----------------------------------------------------------


def test_ttl_expired_refused(store):
    account_id, session_id = open_account_session(store)
    stale_ts = "2020-01-01T00:00:00.000Z"
    booked = _book(store, session_id, booking_ttl_s=1, now_ts=stale_ts)

    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id)
    assert result.allowed is False
    assert result.code == "ttl_expired"

    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["state"] == "PROVISIONAL"  # never consumed


def test_within_ttl_not_expired(store):
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id, booking_ttl_s=3600)
    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id)
    assert result.allowed is True


# ---- model-policy re-check at spawn time (defense in depth) ---------------


def test_model_policy_violation_at_spawn_time_refused(store):
    account_id, session_id = open_account_session(store)
    # Booked with NO policy in effect (so book_launch didn't refuse it) --
    # the gate re-checks against the policy handed to IT.
    booked = _book(store, session_id, model_class="small", purpose="ideation")

    result = evaluate_spawn(
        store, _prompt(booked.launch_id), session_id=session_id, policy={"ideation": "top"}
    )
    assert result.allowed is False
    assert result.code == "model_policy_violation"

    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["state"] == "PROVISIONAL"  # refused, not consumed


def test_model_policy_satisfied_allows_spawn(store):
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id, model_class="top", purpose="ideation")
    result = evaluate_spawn(
        store, _prompt(booked.launch_id), session_id=session_id, policy={"ideation": "top"}
    )
    assert result.allowed is True


def test_override_ruling_unknown_refused_for_hand_forged_launch_row(store):
    """EP-1 Bypass D (C-0064 fix-tier3, IMPL_REVIEW_B): a launch row
    created OUTSIDE ``book_launch`` (so booking-time ``override_ruling_id``
    validation never ran, ``trialerror.budget.pools._check_model_policy``) with
    a bogus ``attrs.override_ruling_id`` must still be refused at spawn
    time — not trusted by mere presence."""
    account_id, session_id = open_account_session(store)
    launch_id = new_id("LNCH")
    insert(
        store, "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-test",
            "session_id": session_id, "agent_kind": "lens", "model_class": "small", "model": "sonnet",
            "purpose": "ideation", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
            "attrs": json.dumps({"override_ruling_id": "C-9999"}),
        },
    )

    result = evaluate_spawn(store, _prompt(launch_id), session_id=session_id, policy={"ideation": "top"})
    assert result.allowed is False
    assert result.code == "override_ruling_unknown"

    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    assert row["state"] == "PROVISIONAL"  # refused, not consumed


def test_override_ruling_known_still_allows_spawn(store):
    """The EP-1 Bypass D re-check is additive, not a regression — a REAL
    ruling id (the normal, book_launch-validated path) still passes."""
    from trialerror.law.service import append_ruling

    ruling = append_ruling(store, summary="test override ruling", render_to_disk=False)
    account_id, session_id = open_account_session(store)
    booked = _book(
        store, session_id, model_class="small", purpose="ideation", override_ruling_id=ruling.ruling_id,
    )
    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id, policy={"ideation": "top"})
    assert result.allowed is True


# ---- law-pin freshness -----------------------------------------------------


def test_stale_law_pin_refused(store):
    account_id, session_id = open_account_session(store, boot_pin_version="v1")
    add_law_digest(store, "v1", generated_ts="2026-01-01T00:00:00.000Z")
    add_law_digest(store, "v2", generated_ts="2026-02-01T00:00:00.000Z")
    booked = _book(store, session_id)

    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id)
    assert result.allowed is False
    assert result.code == "stale_law_pin"


def test_fresh_law_pin_allows_spawn(store):
    account_id, session_id = open_account_session(store, boot_pin_version="v2")
    add_law_digest(store, "v1", generated_ts="2026-01-01T00:00:00.000Z")
    add_law_digest(store, "v2", generated_ts="2026-02-01T00:00:00.000Z")
    booked = _book(store, session_id)

    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id)
    assert result.allowed is True


def test_no_boot_pin_version_is_not_a_staleness_failure(store):
    account_id, session_id = open_account_session(store, boot_pin_version=None)
    add_law_digest(store, "v1")
    booked = _book(store, session_id)
    result = evaluate_spawn(store, _prompt(booked.launch_id), session_id=session_id)
    assert result.allowed is True


# ---- open-session resolution ----------------------------------------------


def test_resolve_open_session_none(store):
    assert resolve_open_session(store) is None


def test_resolve_open_session_single(store):
    account_id, session_id = open_account_session(store)
    resolved = resolve_open_session(store)
    assert resolved["session_id"] == session_id


def test_resolve_open_session_multiple_raises(store):
    open_account_session(store)
    open_account_session(store)
    with pytest.raises(RuntimeError):
        resolve_open_session(store)


def test_evaluate_spawn_for_open_session_no_open_session(store):
    result = evaluate_spawn_for_open_session(store, "launch_id: " + new_id("LNCH"))
    assert result.allowed is False
    assert result.code == "no_open_session"


def test_evaluate_spawn_for_open_session_happy_path(store):
    account_id, session_id = open_account_session(store)
    booked = _book(store, session_id)
    result = evaluate_spawn_for_open_session(store, _prompt(booked.launch_id))
    assert result.allowed is True
    row = get(store, "launch", pk_column="launch_id", pk_value=booked.launch_id)
    assert row["state"] == "RUNNING"
