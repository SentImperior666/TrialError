"""M5's doctor checks: ``event_secret_leak`` and ``feed_author_integrity``
(``trialerror/events/checks.py``), auto-discovered the same way M1's
``trialerror/stores/checks.py`` checks are (design Section 5.2 doctor row).
"""

from __future__ import annotations

from trialerror.events.api import append_event, create_thread, post_feed
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests.test_events_helpers import seed_launch, seed_session


def _run(names, program_root):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    results = run_checks(ctx, only=names)
    return {r.name: r for r in results}


def test_event_secret_leak_check_registered():
    clear_registry()
    imported = discover_and_register_checks()
    assert "trialerror.events.checks" in imported


def test_event_secret_leak_passes_when_writes_go_through_the_api(store, program_root):
    append_event(store, event_type="clean", payload={"note": "nothing to see"})
    results = _run(["event_secret_leak"], program_root)
    assert results["event_secret_leak"].status == "pass"


def test_event_secret_leak_fails_on_a_direct_db_write_that_bypassed_the_api(store, program_root):
    """A raw INSERT against store.ops directly (skipping trialerror.stores.
    insert's auto-redaction entirely) is exactly the scenario this check
    exists to catch -- "defense in depth against a direct-DB write that
    bypassed the write API"."""
    with store.ops:
        store.ops.execute(
            "INSERT INTO event (event_id, ts, type, payload, redactions) VALUES (?, ?, ?, ?, 0)",
            (new_id("EVT"), now(), "raw_write", '{"token": "sk-ant-' + "a" * 30 + '"}'),
        )

    results = _run(["event_secret_leak"], program_root)
    r = results["event_secret_leak"]
    assert r.status == "fail"
    assert len(r.details["offenders"]) == 1


def test_event_secret_leak_skips_when_program_root_missing():
    results = _run(["event_secret_leak"], None)
    assert results["event_secret_leak"].status == "skip"


def test_feed_author_integrity_passes_when_posts_go_through_the_api(store, program_root):
    launch_id = seed_launch(store, agent_kind="build-M5")
    thread = create_thread(store, title="t", launch_id=launch_id)
    post_feed(store, thread_id=thread["thread_id"], body="hi", launch_id=launch_id)
    session_id = seed_session(store, status="open")
    seed_launch(store, session_id=session_id)
    post_feed(store, thread_id=thread["thread_id"], body="orchestrator text", launch_id=None, session_id=session_id)

    results = _run(["feed_author_integrity"], program_root)
    assert results["feed_author_integrity"].status == "pass"


def test_feed_author_integrity_fails_on_mismatched_launch_derived_author(store, program_root):
    launch_id = seed_launch(store, agent_kind="build-M5")
    thread = create_thread(store, title="t", launch_id=launch_id)
    with store.ops:
        store.ops.execute(
            "INSERT INTO feed_post (post_id, thread_id, author, launch_id, ts, body) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("POST"), thread["thread_id"], "totally-fake-name", launch_id, now(), "spoofed"),
        )

    results = _run(["feed_author_integrity"], program_root)
    r = results["feed_author_integrity"]
    assert r.status == "fail"
    assert len(r.details["offenders"]) == 1


def test_feed_author_integrity_fails_on_malformed_orchestrator_author(store, program_root):
    launch_id = seed_launch(store, agent_kind="build-M5")
    thread = create_thread(store, title="t", launch_id=launch_id)
    with store.ops:
        store.ops.execute(
            "INSERT INTO feed_post (post_id, thread_id, author, launch_id, ts, body) VALUES (?, ?, ?, NULL, ?, ?)",
            (new_id("POST"), thread["thread_id"], "not-an-orchestrator-string", now(), "bad"),
        )

    results = _run(["feed_author_integrity"], program_root)
    assert results["feed_author_integrity"].status == "fail"


def test_feed_author_integrity_fails_on_orchestrator_author_with_unknown_session(store, program_root):
    launch_id = seed_launch(store, agent_kind="build-M5")
    thread = create_thread(store, title="t", launch_id=launch_id)
    with store.ops:
        store.ops.execute(
            "INSERT INTO feed_post (post_id, thread_id, author, launch_id, ts, body) VALUES (?, ?, ?, NULL, ?, ?)",
            (new_id("POST"), thread["thread_id"], "orchestrator:SESS-nonexistent", now(), "bad"),
        )

    results = _run(["feed_author_integrity"], program_root)
    assert results["feed_author_integrity"].status == "fail"
