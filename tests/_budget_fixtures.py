"""Not a test module (pytest only collects ``test_*.py``) — shared helpers
for the M3 (``trialerror.budget``) test suite, mirroring ``tests/_store_fixtures.py``'s
role for M1's suite: build the minimum valid account+session (and
optionally a law_digest row) a budget test needs, without re-deriving the
same handful of inserts in every module.
"""

from __future__ import annotations

from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def open_account_session(store: Store, *, status: str = "open", boot_pin_version: str | None = None) -> tuple[str, str]:
    """Insert one ``account`` row and one ``session`` row (OPEN by
    default) bound to it. Returns ``(account_id, session_id)``."""
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "test account", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {
            "session_id": session_id,
            "account_id": account_id,
            "opened_ts": now(),
            "status": status,
            "boot_pin_version": boot_pin_version,
        },
    )
    return account_id, session_id


def add_law_digest(store: Store, version: str, *, generated_ts: str | None = None) -> None:
    insert(
        store,
        "law_digest",
        {
            "version": version,
            "generated_ts": generated_ts or now(),
            "content_sha256": "0" * 64,
            "rendered_path": "law/LAW_DIGEST.md",
        },
    )


def add_ruling(store: Store, ruling_id: str = "C-0001") -> str:
    insert(
        store,
        "ruling",
        {
            "ruling_id": ruling_id,
            "ts": now(),
            "summary": "test ruling (override citation target)",
            "status": "active",
            "ledger_sha256_after": "1" * 64,
        },
    )
    return ruling_id
