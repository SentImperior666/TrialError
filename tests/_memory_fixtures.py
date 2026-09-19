"""Not a test module — a tiny shared helper for the M11 (``trialerror.memory``)
test suite: memory items are XID-validated against ``platform.account``
(design Section 4, delta-verify residual: ``memory_item.account_id ->
platform.account``), so every test needs at least one real account row.
"""

from __future__ import annotations

from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def make_account(store: Store, *, label: str = "test account") -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": label, "created_ts": now()})
    return account_id
