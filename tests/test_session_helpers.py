"""Not a test module (pytest only collects ``test_*`` functions) — shared
minimal fixture builders for the M6 (``trialerror.sessions``) test suite,
mirroring ``tests/test_events_helpers.py``'s role for M5's own suite: the
lane's file-list restriction is ``tests/test_session_*.py`` +
``tests/test_m6_acceptance.py``, so this file matches that glob while
playing the same shared-helper role a leading-underscore module would
(the ``tests/_budget_fixtures.py`` precedent from M3), without adding a
file outside the lane's stated pattern. Zero ``test_*`` functions defined
here — that is expected, not a bug.
"""

from __future__ import annotations

from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def seed_account(store: Store, *, label: str = "test account") -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": label, "created_ts": now()})
    return account_id


def seed_open_session(
    store: Store, *, account_id: str | None = None, boot_pin_version: str | None = None
) -> tuple[str, str]:
    """Insert one ``account`` row (unless given) and one OPEN ``session``
    row bound to it. Returns ``(account_id, session_id)``."""
    if account_id is None:
        account_id = seed_account(store)
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {
            "session_id": session_id,
            "account_id": account_id,
            "opened_ts": now(),
            "status": "open",
            "boot_pin_version": boot_pin_version,
        },
    )
    return account_id, session_id


def seed_launch(
    store: Store,
    *,
    account_id: str,
    session_id: str,
    state: str = "PROVISIONAL",
    agent_kind: str = "build-M6-fixture",
    program_id: str = "PROG-test",
) -> str:
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id,
            "account_id": account_id,
            "program_id": program_id,
            "session_id": session_id,
            "agent_kind": agent_kind,
            "model_class": "top",
            "model": "sonnet",
            "purpose": "fixture",
            "est_tokens": 100,
            "booked_ts": now(),
            "state": state,
        },
    )
    return launch_id


def add_hook_alive(store: Store, session_id: str, *, hook: str = "test-fixture") -> None:
    """``hook`` defaults to a value distinct from ``session_start``/
    ``spawn_gate``/``post_task`` (the real hook scripts' own
    ``payload.hook`` values -- see ``trialerror.events.api.record_hook_alive_once``)
    -- most callers only need "some hook fired". A caller whose fixture
    also seeds subagent activity (a RUNNING/RECONCILED launch, or a
    ``subagent_return`` event) needs ``hook="spawn_gate"`` specifically, or
    ``close_session``'s FX-8 ``hooks_partial`` check refuses it."""
    from trialerror.events.api import append_event

    append_event(store, event_type="hook_alive", session_id=session_id, payload={"hook": hook})


def add_ruling(store: Store, ruling_id: str = "C-0001") -> str:
    insert(
        store,
        "ruling",
        {
            "ruling_id": ruling_id,
            "ts": now(),
            "summary": "test ruling (M6 fixture)",
            "status": "active",
            "ledger_sha256_after": "1" * 64,
        },
    )
    return ruling_id
