"""``trialerror.events.api`` — the user inbox (``post_inbox`` / ``read_inbox``).
Design Section 4.2: "The user's write path is ``trialerror inbox post`` (the
one API-backed inbox writer — no hand-appended files, per P2)."
"""

from __future__ import annotations

from trialerror.events.api import post_inbox, read_inbox
from trialerror.stores import get

from tests.test_events_helpers import seed_session


def test_post_inbox_always_source_user(store):
    row = post_inbox(store, body="please check the queue")
    assert row["source"] == "user"
    assert row["body"] == "please check the queue"
    # insert() echoes back only what was written (design: "returns the row
    # as written"); read_ts/read_by_session are DB-default NULLs, not
    # supplied here, so they're simply absent from the returned dict --
    # confirmed by reading the row back below.
    assert "read_ts" not in row

    stored = get(store, "inbox_item", pk_column="item_id", pk_value=row["item_id"])
    assert stored["read_ts"] is None
    assert stored["read_by_session"] is None


def test_post_inbox_signature_has_no_source_parameter():
    import inspect

    params = set(inspect.signature(post_inbox).parameters)
    assert "source" not in params


def test_read_inbox_returns_unread_oldest_first_and_marks_read(store):
    post_inbox(store, body="first", ts="2026-01-01T00:00:00.000Z")
    post_inbox(store, body="second", ts="2026-01-02T00:00:00.000Z")

    items = read_inbox(store, mark_read=False)
    assert [i["body"] for i in items] == ["first", "second"]
    assert all(i["read_ts"] is None for i in items)

    # A second unmarked read returns the same unread set (idempotent peek).
    items_again = read_inbox(store, mark_read=False)
    assert len(items_again) == 2

    session_id = seed_session(store)
    marked = read_inbox(store, session_id=session_id, mark_read=True)
    assert len(marked) == 2
    assert all(i["read_ts"] is not None for i in marked)
    assert all(i["read_by_session"] == session_id for i in marked)

    # Now nothing is unread.
    remaining = read_inbox(store, mark_read=False)
    assert remaining == []


def test_read_inbox_mark_read_without_session_id_leaves_it_null(store):
    post_inbox(store, body="no session context yet")
    items = read_inbox(store, mark_read=True)
    assert len(items) == 1
    assert items[0]["read_ts"] is not None
    assert items[0]["read_by_session"] is None

    row = store.ops.execute(
        "SELECT read_ts, read_by_session FROM inbox_item WHERE item_id = ?", (items[0]["item_id"],)
    ).fetchone()
    assert row["read_ts"] is not None
    assert row["read_by_session"] is None


def test_read_inbox_empty_inbox_returns_empty_list(store):
    assert read_inbox(store) == []
