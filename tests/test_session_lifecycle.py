"""Tests for ``trialerror.sessions.lifecycle`` — boot/close/abandon/status and
the account-resolution + close-readiness helpers they're built from."""

from __future__ import annotations

import json

import pytest

from trialerror.budget.pools import book_launch, create_pool, reconcile_launch
from trialerror.events.api import post_inbox
from trialerror.law.chain import compute_ledger_hash
from trialerror.law.service import append_ruling
from trialerror.sessions.lifecycle import (
    abandon_session,
    boot_session,
    close_session,
    evaluate_close_readiness,
    resolve_account_for_boot,
    session_status,
)
from trialerror.stores import get, insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests.test_session_helpers import add_hook_alive, add_ruling, seed_account, seed_launch, seed_open_session

_COURSE_CHECK = {"rungs": "climbed 2", "build_vs_theory": "all build", "drift_flag": False}


# ---------------------------------------------------------------------------
# account resolution (F14)
# ---------------------------------------------------------------------------


def test_resolve_account_no_accounts_refused(store):
    result = resolve_account_for_boot(store)
    assert result.ok is False
    assert result.code == "no_accounts"


def test_resolve_account_single_account_default(store):
    account_id = seed_account(store)
    result = resolve_account_for_boot(store)
    assert result.ok is True
    assert result.account_id == account_id
    assert result.code == "single_account_default"


def test_resolve_account_multiple_accounts_requires_explicit(store):
    seed_account(store, label="one")
    seed_account(store, label="two")
    result = resolve_account_for_boot(store)
    assert result.ok is False
    assert result.code == "account_required"
    assert len(result.accounts) == 2


def test_resolve_account_explicit_given(store):
    a1 = seed_account(store, label="one")
    seed_account(store, label="two")
    result = resolve_account_for_boot(store, account_id=a1)
    assert result.ok is True
    assert result.account_id == a1
    assert result.code == "given"


def test_resolve_account_unknown_explicit_refused(store):
    result = resolve_account_for_boot(store, account_id="ACC-does-not-exist")
    assert result.ok is False
    assert result.code == "unknown_account"


def test_resolve_account_create_account_bootstraps(store):
    result = resolve_account_for_boot(store, create_account_label="fresh label")
    assert result.ok is True
    assert result.code == "created"
    row = get(store, "account", pk_column="account_id", pk_value=result.account_id)
    assert row["label"] == "fresh label"


# ---------------------------------------------------------------------------
# boot_session
# ---------------------------------------------------------------------------


def test_boot_session_single_account_default(store):
    account_id = seed_account(store)
    result = boot_session(store)
    assert result.ok is True
    assert result.code == "booted"
    assert result.account_id == account_id
    row = get(store, "session", pk_column="session_id", pk_value=result.session_id)
    assert row["status"] == "open"
    assert row["account_id"] == account_id
    assert row["boot_bundle_sha"]  # stamped after the fact


def test_boot_session_ambiguous_account_refused(store):
    seed_account(store, label="one")
    seed_account(store, label="two")
    result = boot_session(store)
    assert result.ok is False
    assert result.code == "account_required"
    assert result.session_id is None


def test_boot_session_stamps_current_law_pin(store):
    account_id = seed_account(store)
    append_result = append_ruling(store, summary="a real ruling", render_to_disk=False)
    result = boot_session(store, account_id=account_id)
    assert result.ok is True
    assert result.bundle["boot_pin_version"] == append_result.pin
    row = get(store, "session", pk_column="session_id", pk_value=result.session_id)
    assert row["boot_pin_version"] == append_result.pin


def test_boot_session_no_law_yet_pin_is_none(store):
    account_id = seed_account(store)
    result = boot_session(store, account_id=account_id)
    assert result.ok is True
    assert result.bundle["boot_pin_version"] is None
    assert result.bundle["pin_status"] is None


def test_boot_session_tampered_ledger_refused(store):
    account_id = seed_account(store)
    append_ruling(store, summary="first ruling", render_to_disk=False)
    # Tamper the chain directly (bypassing the validated API) -- the exact
    # adversarial shape trialerror.law's own acceptance suite uses.
    store.ops.execute("UPDATE ruling SET summary = 'TAMPERED' WHERE ruling_id = 'C-0001'")
    store.ops.commit()

    result = boot_session(store, account_id=account_id)
    assert result.ok is False
    assert result.code == "law_chain_tampered"
    assert result.session_id is None


def test_boot_session_already_open_refused_without_reuse(store):
    account_id, session_id = seed_open_session(store)
    result = boot_session(store, account_id=account_id, reuse_open=False)
    assert result.ok is False
    assert result.code == "session_already_open"
    assert result.session_id == session_id


def test_boot_session_already_open_reused_by_default(store):
    account_id, session_id = seed_open_session(store)
    result = boot_session(store, account_id=account_id)
    assert result.ok is True
    assert result.code == "reused_open_session"
    assert result.session_id == session_id
    # No second session row was created.
    open_rows = store.ops.execute("SELECT * FROM session WHERE status='open'").fetchall()
    assert len(open_rows) == 1


def test_boot_session_bundle_reads_inbox_and_marks_read(store):
    account_id = seed_account(store)
    post_inbox(store, body="please read this")
    result = boot_session(store, account_id=account_id)
    assert result.bundle["inbox_unread_count"] == 1
    assert result.bundle["inbox_items"][0]["body"] == "please read this"
    # Marked read as part of boot.
    still_unread = store.ops.execute("SELECT COUNT(*) c FROM inbox_item WHERE read_ts IS NULL").fetchone()["c"]
    assert still_unread == 0


def test_boot_session_bundle_latest_handoff_respects_configured_handoffs_dir(store, tmp_path):
    """the import-design notes (internal, not in this export) Sec 5 knob #3: the SAME resolved handoffs_dir
    a prior close wrote to is what a later boot's bundle looks in --
    proof the handoff.py/lifecycle.py duplication fix actually shares one
    resolver rather than merely not crashing."""
    external = tmp_path / "external-handoffs"
    config = {"paths": {"handoffs_dir": str(external)}}

    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    close_result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK, config=config)
    assert close_result.ok is True
    handoff_filename = close_result.close_report["handoff_filename"]
    assert (external / handoff_filename).is_file()
    assert not (store.program_root / "handoffs").exists()

    boot_result = boot_session(store, config=config)
    assert boot_result.ok is True
    assert boot_result.bundle["latest_handoff_path"] == str(external / handoff_filename)
    assert boot_result.bundle["latest_handoff_markdown"] is not None


def test_boot_session_bundle_surfaces_dangling_launches_from_dead_session(store):
    account_id = seed_account(store)
    _, dead_session = seed_open_session(store, account_id=account_id)
    seed_launch(store, account_id=account_id, session_id=dead_session, state="RUNNING")
    # dead_session never closes -- simulate a crash, so its launch stays
    # RUNNING. Booting a NEW session (after abandoning the dead one, so
    # resolve_open_session doesn't collide) should surface it as dangling.
    abandon_session(store, session_id=dead_session)

    result = boot_session(store, account_id=account_id)
    assert result.ok is True
    assert len(result.bundle["dangling_launches"]) == 1


def test_boot_session_bundle_includes_budget_and_memory(store):
    account_id = seed_account(store)
    create_pool(store, account_id=account_id, model_class="top", period="weekly", cap_tokens=1000)
    insert(
        store,
        "memory_item",
        {
            "memory_item_id": new_id("MEM"),
            "key": "some-rule",
            "tier": "L0",
            "kind": "rule",
            "body": "always do X",
            "updated_ts": now(),
            "account_id": account_id,
        },
    )
    result = boot_session(store, account_id=account_id)
    assert any(p["model_class"] == "top" for p in result.bundle["budget"]["pools"])
    assert result.bundle["memory_l0"][0]["key"] == "some-rule"


def test_boot_session_first_session_flag(store):
    account_id = seed_account(store)
    first = boot_session(store, account_id=account_id)
    assert first.bundle["first_session"] is True
    add_hook_alive(store, first.session_id)
    close_result = close_session(store, session_id=first.session_id, course_check=_COURSE_CHECK)
    assert close_result.ok is True

    second = boot_session(store, account_id=account_id)
    assert second.bundle["first_session"] is False


def test_boot_session_foreign_since_last_diff(store):
    account_id = seed_account(store)
    first = boot_session(store, account_id=account_id)
    add_hook_alive(store, first.session_id)
    close_result = close_session(store, session_id=first.session_id, course_check=_COURSE_CHECK)
    assert close_result.ok is True

    # A ruling appended after the first session closed.
    append_ruling(store, summary="appended after close", render_to_disk=False)

    second = boot_session(store, account_id=account_id)
    assert len(second.bundle["foreign_since_last"]) == 1
    assert second.bundle["foreign_since_last"][0]["summary"] == "appended after close"


# ---------------------------------------------------------------------------
# evaluate_close_readiness
# ---------------------------------------------------------------------------


def test_evaluate_close_readiness_clean(store):
    _, session_id = seed_open_session(store)
    readiness = evaluate_close_readiness(store, session_id)
    assert readiness.ready is True
    assert readiness.problems == []


def test_evaluate_close_readiness_dangling(store):
    account_id, session_id = seed_open_session(store)
    seed_launch(store, account_id=account_id, session_id=session_id, state="RUNNING")
    readiness = evaluate_close_readiness(store, session_id)
    assert readiness.ready is False
    assert len(readiness.dangling_launches) == 1


def test_evaluate_close_readiness_stale_pin(store):
    append_ruling(store, summary="ruling one", render_to_disk=False)
    account_id, session_id = seed_open_session(store, boot_pin_version="v1@2020-01-01")
    readiness = evaluate_close_readiness(store, session_id)
    assert readiness.ready is False
    assert readiness.pin_check.valid is False


def test_evaluate_close_readiness_unknown_session_raises(store):
    with pytest.raises(ValueError):
        evaluate_close_readiness(store, "SESS-does-not-exist")


# ---------------------------------------------------------------------------
# close_session
# ---------------------------------------------------------------------------


def test_close_session_not_open_refused(store):
    account_id, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    # Second close attempt.
    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "not_open"


def test_close_session_requires_course_check(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    result = close_session(store, session_id=session_id, course_check={})
    assert result.ok is False
    assert result.code == "course_check_required"


def test_close_session_hooks_disabled_refused(store):
    _, session_id = seed_open_session(store)
    # No hook_alive event recorded.
    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "hooks_disabled"


def test_close_session_hooks_disabled_override_with_valid_ruling(store):
    _, session_id = seed_open_session(store)
    ruling_id = add_ruling(store)
    result = close_session(
        store, session_id=session_id, course_check=_COURSE_CHECK, hook_alive_override_ruling_id=ruling_id
    )
    assert result.ok is True
    assert result.close_report["hook_alive_override_ruling_id"] == ruling_id


def test_close_session_hooks_disabled_override_with_unknown_ruling_refused(store):
    _, session_id = seed_open_session(store)
    result = close_session(
        store, session_id=session_id, course_check=_COURSE_CHECK, hook_alive_override_ruling_id="C-9999"
    )
    assert result.ok is False
    assert result.code == "unknown_override_ruling"


def test_close_session_dangling_launch_fixture_refused(store):
    """Design Section 12 M6 acceptance: 'close refused w/ dangling launch fixture'."""
    account_id, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    seed_launch(store, account_id=account_id, session_id=session_id, state="PROVISIONAL")

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "dangling_launches"
    row = get(store, "session", pk_column="session_id", pk_value=session_id)
    assert row["status"] == "open"  # not mutated by a refused close


def test_close_session_dangling_launch_cleared_by_reconcile(store):
    account_id, session_id = seed_open_session(store)
    # FX-8 (C-0064): a RECONCILED launch under this session counts as
    # "consumed" -- close's hooks_partial check needs the spawn_gate
    # marker specifically, not just any hook_alive event.
    add_hook_alive(store, session_id, hook="spawn_gate")
    create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=100_000)
    booked = book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=50,
    )
    reconcile_launch(store, launch_id=booked.launch_id, actual_tokens=40)

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is True


# ---------------------------------------------------------------------------
# FX-8 (C-0064 lens B EP-1 Bypass C): the spawn_gate liveness marker
# ---------------------------------------------------------------------------


def test_close_session_hooks_partial_refused_on_subagent_return_with_no_spawn_gate_marker(store):
    """Partial-manifest scenario: SessionStart alive (a generic hook_alive
    event), no spawn_gate marker, but a subagent_return event present --
    exactly the "SessionStart armed, PreToolUse:Task off" quiet corner
    EP-1 Bypass C named. Close must refuse (``hooks_partial``), not go
    through clean."""
    from trialerror.events.api import append_event

    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)  # session_start only -- no spawn_gate marker
    append_event(store, event_type="subagent_return", session_id=session_id, launch_id=None, payload={"response_size_bytes": 10, "duration_ms": None})

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "hooks_partial"


def test_close_session_hooks_partial_refused_on_consumed_launch_with_no_spawn_gate_marker(store):
    """Same quiet corner, the OTHER trigger: a launch this session's own
    booking actually consumed (state RUNNING) with no subagent_return
    event at all (e.g. still mid-flight) and no spawn_gate marker."""
    account_id, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    seed_launch(store, account_id=account_id, session_id=session_id, state="RUNNING")
    # A dangling RUNNING launch would ALSO trip dangling_launches -- reconcile
    # it first so hooks_partial is what's actually under test here, isolated.
    launch_row = store.platform.execute(
        "SELECT launch_id FROM launch WHERE session_id = ?", (session_id,)
    ).fetchone()
    reconcile_launch(store, launch_id=launch_row["launch_id"], actual_tokens=5)

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "hooks_partial"


def test_close_session_hooks_partial_override_with_valid_ruling(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    from trialerror.events.api import append_event

    append_event(store, event_type="subagent_return", session_id=session_id, launch_id=None, payload={"response_size_bytes": 10, "duration_ms": None})
    ruling_id = add_ruling(store)

    result = close_session(
        store, session_id=session_id, course_check=_COURSE_CHECK, hook_alive_override_ruling_id=ruling_id
    )
    assert result.ok is True
    assert result.close_report["hook_alive_override_ruling_id"] == ruling_id


def test_close_session_full_manifest_with_spawn_gate_marker_and_subagent_return_closes_clean(store):
    """Full-manifest scenario: session_start AND spawn_gate markers both
    present, plus a real subagent_return event -- close succeeds with no
    override needed."""
    from trialerror.events.api import append_event

    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id, hook="session_start")
    add_hook_alive(store, session_id, hook="spawn_gate")
    append_event(store, event_type="subagent_return", session_id=session_id, launch_id=None, payload={"response_size_bytes": 10, "duration_ms": None})

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is True


def test_close_session_no_subagent_activity_at_all_does_not_need_a_spawn_gate_marker(store):
    """A session with zero subagent activity (no subagent_return events,
    no consumed launches) never needs the spawn_gate marker at all -- the
    hooks_partial check has nothing to be suspicious of."""
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)  # generic marker only, no spawn_gate

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is True


def test_close_session_stale_digest_fixture_refused(store):
    """Design Section 12 M6 acceptance: 'refused w/ stale digest'."""
    append_ruling(store, summary="ruling before boot", render_to_disk=False)
    _, session_id = seed_open_session(store, boot_pin_version="v1@2020-01-01")  # a pin that was never real/current
    add_hook_alive(store, session_id)

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "stale_digest"


def test_close_session_unread_inbox_refused(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    post_inbox(store, body="unread thing")

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "unread_checklist"


def test_close_session_success_writes_close_report_and_course_check(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)

    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK, notes="a good session")
    assert result.ok is True
    assert result.code == "closed"

    row = get(store, "session", pk_column="session_id", pk_value=session_id)
    assert row["status"] == "closed"
    assert row["closed_ts"] is not None
    assert json.loads(row["course_check"]) == _COURSE_CHECK
    close_report = json.loads(row["close_report"])
    assert close_report["notes"] == "a good session"
    assert "handoff_filename" in close_report


def test_close_session_respects_configured_handoffs_dir(store, tmp_path):
    external = tmp_path / "external-handoffs"
    config = {"paths": {"handoffs_dir": str(external)}}

    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK, config=config)

    assert result.ok is True
    handoff_filename = result.close_report["handoff_filename"]
    assert result.handoff_path == str(external / handoff_filename)
    assert (external / handoff_filename).is_file()
    assert not (store.program_root / "handoffs").exists()


def test_close_session_default_config_matches_unconfigured_behavior(store):
    """``config={}`` (no ``[paths]`` table) must land in the SAME default
    ``handoffs/`` dir as ``config=None`` -- zero behavior change."""
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    result = close_session(store, session_id=session_id, course_check=_COURSE_CHECK, config={})
    assert result.ok is True
    handoff_filename = result.close_report["handoff_filename"]
    assert (store.program_root / "handoffs" / handoff_filename).is_file()


def test_close_session_defaults_to_open_session_when_none_given(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    result = close_session(store, course_check=_COURSE_CHECK)
    assert result.ok is True
    assert result.session_id == session_id


def test_close_session_no_open_session_at_all_refused(store):
    result = close_session(store, course_check=_COURSE_CHECK)
    assert result.ok is False
    assert result.code == "not_open"


# ---------------------------------------------------------------------------
# abandon_session
# ---------------------------------------------------------------------------


def test_abandon_session_marks_abandoned(store):
    _, session_id = seed_open_session(store)
    result = abandon_session(store, session_id=session_id, reason="process crashed")
    assert result.ok is True
    row = get(store, "session", pk_column="session_id", pk_value=session_id)
    assert row["status"] == "abandoned"
    assert row["closed_ts"] is not None


def test_abandon_session_refuses_when_not_open(store):
    _, session_id = seed_open_session(store)
    abandon_session(store, session_id=session_id)
    result = abandon_session(store, session_id=session_id)
    assert result.ok is False
    assert result.code == "not_open"


def test_abandon_session_unblocks_next_boot(store):
    account_id, session_id = seed_open_session(store)
    refused = boot_session(store, account_id=account_id, reuse_open=False)
    assert refused.ok is False
    assert refused.code == "session_already_open"

    abandon_session(store, session_id=session_id)
    result = boot_session(store, account_id=account_id, reuse_open=False)
    assert result.ok is True
    assert result.session_id != session_id


# ---------------------------------------------------------------------------
# session_status
# ---------------------------------------------------------------------------


def test_session_status_nothing_open(store):
    result = session_status(store)
    assert result["open"] is False


def test_session_status_open_session_peeks_inbox_without_marking_read(store):
    _, session_id = seed_open_session(store)
    post_inbox(store, body="peek me")

    result = session_status(store)
    assert result["open"] is True
    assert result["unread_inbox_count"] == 1

    # Still unread -- status must not have side effects.
    still_unread = store.ops.execute("SELECT COUNT(*) c FROM inbox_item WHERE read_ts IS NULL").fetchone()["c"]
    assert still_unread == 1


def test_session_status_reports_hook_alive_count(store):
    _, session_id = seed_open_session(store)
    add_hook_alive(store, session_id)
    add_hook_alive(store, session_id)
    result = session_status(store)
    assert result["hook_alive_count"] == 2
