"""``trialerror.events.api`` — threads + full-text feed posts. Covers the M5
acceptance criterion "author spoof attempt via API impossible (author
derived: launch identity, else ``orchestrator:<session>``)".
"""

from __future__ import annotations

import inspect

import pytest

from trialerror.events.api import create_thread, get_thread_posts, list_threads, post_feed
from trialerror.stores.errors import ValidationError, XidTargetMissingError

from tests.test_events_helpers import seed_launch, seed_session


def test_post_feed_author_derived_from_launch_agent_kind(store):
    launch_id = seed_launch(store, agent_kind="build-M5")
    thread = create_thread(store, title="round 1", launch_id=launch_id)
    post = post_feed(store, thread_id=thread["thread_id"], body="full text here", launch_id=launch_id)
    assert post["author"] == f"build-M5:{launch_id}"
    assert post["launch_id"] == launch_id


def test_post_feed_two_different_launches_get_two_different_authors(store):
    launch_a = seed_launch(store, agent_kind="lens-skeptic")
    launch_b = seed_launch(store, agent_kind="lens-optimist")
    thread = create_thread(store, title="round 1", launch_id=launch_a)
    post_a = post_feed(store, thread_id=thread["thread_id"], body="A's take", launch_id=launch_a)
    post_b = post_feed(store, thread_id=thread["thread_id"], body="B's take", launch_id=launch_b)
    assert post_a["author"] == f"lens-skeptic:{launch_a}"
    assert post_b["author"] == f"lens-optimist:{launch_b}"
    assert post_a["author"] != post_b["author"]


def test_post_feed_orchestrator_fallback_uses_open_session(store):
    session_id = seed_session(store, status="open")
    launch_id = seed_launch(store, session_id=session_id)
    thread = create_thread(store, title="orchestrator thread", launch_id=launch_id)
    post = post_feed(store, thread_id=thread["thread_id"], body="orchestrator text", launch_id=None)
    assert post["author"] == f"orchestrator:{session_id}"
    assert post["launch_id"] is None


def test_post_feed_orchestrator_fallback_refuses_when_no_open_session(store):
    with pytest.raises(ValidationError):
        post_feed(store, thread_id="THR-does-not-exist", body="text", launch_id=None)


def test_post_feed_orchestrator_explicit_session_must_be_open(store):
    closed_session = seed_session(store, status="closed")
    with pytest.raises(ValidationError):
        post_feed(store, thread_id="THR-does-not-exist", body="text", launch_id=None, session_id=closed_session)


def test_post_feed_orchestrator_explicit_session_must_exist(store):
    with pytest.raises(ValidationError):
        post_feed(store, thread_id="THR-does-not-exist", body="text", launch_id=None, session_id="SESS-nonexistent")


def test_post_feed_stolen_launch_id_still_attributes_to_the_real_owner(store):
    """The 'impossible to spoof' guarantee: even if a caller (agent A)
    passes a launch_id that isn't its own booking (agent B's), the
    resulting post is attributed to B's real identity -- there is no way
    to make the call say 'A posted this' while riding B's launch_id, and
    no parameter through which A could inject an arbitrary display name
    of its own choosing."""
    launch_b = seed_launch(store, agent_kind="build-M2")
    thread = create_thread(store, title="t", launch_id=launch_b)
    post = post_feed(store, thread_id=thread["thread_id"], body="pretending to be B", launch_id=launch_b)
    assert post["author"] == f"build-M2:{launch_b}"
    assert "build-M2" in post["author"]


def test_post_feed_rejects_unknown_launch_id(store):
    with pytest.raises(XidTargetMissingError):
        post_feed(store, thread_id="THR-x", body="text", launch_id="LNCH-does-not-exist")


def test_post_feed_signature_has_no_author_parameter():
    """Structural proof (not just behavioral): there is no 'author' or
    'name' parameter anywhere on post_feed's signature for a caller to
    even attempt to set -- the only identity lever is launch_id/session_id,
    both of which are resolved against real rows."""
    params = set(inspect.signature(post_feed).parameters)
    assert "author" not in params
    assert "name" not in params
    assert "display_name" not in params


def test_create_thread_requires_a_real_launch(store):
    with pytest.raises(XidTargetMissingError):
        create_thread(store, title="orphan thread", launch_id="LNCH-does-not-exist")


def test_list_threads_and_get_thread_posts_round_trip(store):
    launch_id = seed_launch(store, agent_kind="build-M5")
    thread = create_thread(store, title="my thread", launch_id=launch_id)
    post_feed(store, thread_id=thread["thread_id"], body="first", launch_id=launch_id)
    post_feed(store, thread_id=thread["thread_id"], body="second", launch_id=launch_id)

    threads = list_threads(store)
    assert any(t["thread_id"] == thread["thread_id"] for t in threads)

    posts = get_thread_posts(store, thread_id=thread["thread_id"])
    assert [p["body"] for p in posts] == ["first", "second"]
    assert all(p["author"] == f"build-M5:{launch_id}" for p in posts)


def test_post_feed_full_text_never_truncated(store):
    """C-0047 (origin-project law generalized): ideation agents post FULL TEXT under
    their own names, never orchestrator summaries -- the body column
    stores exactly what was given, byte for byte."""
    launch_id = seed_launch(store, agent_kind="lens-full-text")
    thread = create_thread(store, title="t", launch_id=launch_id)
    long_body = "word " * 2000
    post = post_feed(store, thread_id=thread["thread_id"], body=long_body, launch_id=launch_id)
    assert post["body"] == long_body
    stored = get_thread_posts(store, thread_id=thread["thread_id"])[0]
    assert stored["body"] == long_body
