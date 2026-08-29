"""Shared minimal fixture builders for M5's own test suite (``account`` ->
``session`` -> ``launch`` chains), kept separate from ``tests/_store_
fixtures.py`` (M1-owned, all-41-tables). The M5 build brief restricts new
test files to ``tests/test_events_*.py`` + ``tests/test_m5_acceptance.py``
— this file's name satisfies that glob while playing the same
shared-helper role a leading-underscore module would, without adding a new
file outside the lane's stated pattern. Pytest imports this as a test
module (the name matches) but collects zero tests from it (no ``test_*``
functions defined) — that is expected, not a bug.
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


def seed_session(store: Store, *, account_id: str | None = None, status: str = "open") -> str:
    if account_id is None:
        account_id = seed_account(store)
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": status},
    )
    return session_id


def seed_launch(
    store: Store,
    *,
    account_id: str | None = None,
    session_id: str | None = None,
    agent_kind: str = "build-M5-fixture",
    program_id: str = "PROG-test",
) -> str:
    if account_id is None:
        account_id = seed_account(store)
    if session_id is None:
        session_id = seed_session(store, account_id=account_id)
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
            "state": "PROVISIONAL",
        },
    )
    return launch_id
