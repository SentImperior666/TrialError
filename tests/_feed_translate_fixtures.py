"""Not a test module (pytest only collects ``test_*.py``) -- shared fixture
builders for the lane-b-translator (:mod:`trialerror.feed_translate`) suite.

Deliberately smaller than ``tests._store_fixtures.populate_one_of_everything``:
that builder seeds one row in EVERY table including a ``feed_post_translation``
of its own, which is exactly the state these tests must be able to start
without (a post with no translation yet). Same account/session/launch
bootstrap shape ``tests/_retrieve_fixtures.py::bootstrap_launch`` uses.
"""

from __future__ import annotations

from typing import Any

from trialerror.events.api import create_thread, post_feed
from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["bootstrap_launch", "build_feed", "DENSE_POST", "FAITHFUL_TRANSLATION", "UNFAITHFUL_TRANSLATION"]

#: A Feed post in the register this feature exists for: typed ids, a date,
#: counts, a semicolon, and two load-bearing hedges ("pending", "DEFERRED").
DENSE_POST = (
    "Launch LNCH-01JXYZ4 was launch-booked against pool P-2 on 2026-09-05; the reconcile is pending, "
    "so the 3 downstream gates stay DEFERRED."
)

#: A rendering that keeps every id, number, date and hedge -- must pass the
#: gate.
FAITHFUL_TRANSLATION = (
    "We booked LNCH-01JXYZ4 against pool P-2 on 2026-09-05. "
    "The match-up is still pending. "
    "So the 3 gates after it stay deferred."
)

#: The exact failure the fail-closed gate exists for: the hedge is promoted
#: to a fact, the launch id is dropped, and a count is invented.
UNFAITHFUL_TRANSLATION = "The booking failed and all 5 gates were cancelled."


def bootstrap_launch(store: Store) -> dict[str, str]:
    """A minimal account/session/launch chain. Returns the three ids --
    ``post_feed`` needs the launch for author derivation, and
    ``feed_post_translation.created_by_launch`` is XID-validated against
    ``platform.launch``."""
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "test account", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id,
            "account_id": account_id,
            "program_id": "PROG-test",
            "session_id": session_id,
            "agent_kind": "lens",
            "model_class": "small",
            "model": "sonnet",
            "purpose": "fixture",
            "est_tokens": 100,
            "booked_ts": now(),
            "state": "PROVISIONAL",
        },
    )
    return {"account_id": account_id, "session_id": session_id, "launch_id": launch_id}


def build_feed(store: Store, *, bodies: list[str] | None = None) -> dict[str, Any]:
    """One thread with one post per entry in ``bodies`` (default: a single
    :data:`DENSE_POST`). Returns ``{"launch_id", "session_id", "thread_id",
    "post_ids", "posts"}``."""
    ids = bootstrap_launch(store)
    thread = create_thread(store, title="lane-b translator thread", launch_id=ids["launch_id"])
    posts = [
        post_feed(store, thread_id=thread["thread_id"], body=body, launch_id=ids["launch_id"])
        for body in (bodies if bodies is not None else [DENSE_POST])
    ]
    return {
        **ids,
        "thread_id": thread["thread_id"],
        "post_ids": [p["post_id"] for p in posts],
        "posts": posts,
    }
