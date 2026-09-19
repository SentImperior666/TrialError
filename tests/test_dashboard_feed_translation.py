"""lane-b-translator: the translator's DASHBOARD surface -- the feed
panel's five ``translation_state`` values
(``trialerror.dashboard.data.build_feed_panel``) and the ``feed-translate``
write action (``trialerror.dashboard.writes``).

The load-bearing assertion in this file is
:func:`test_a_withheld_translation_never_leaves_the_server`: the fail-closed
gate is only as good as the payload, and a withheld body that reaches the
browser is one CSS rule away from being displayed. So the API omits it
entirely and sends the REASONS instead.

Kept in its own file rather than appended to ``test_dashboard_data_v2.py``
for the same reason that file was split off from ``test_dashboard_data.py``:
one build's own surface, one file.
"""

from __future__ import annotations

import pytest

from trialerror.dashboard import data, writes
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.feed_translate.api import build_translation_envelope, store_translation
from trialerror.feed_translate.gate import run_translation_gate
from trialerror.jobs.ledger import enqueue as enqueue_job
from trialerror.stores.store import open_store

from tests._feed_translate_fixtures import DENSE_POST, FAITHFUL_TRANSLATION, UNFAITHFUL_TRANSLATION, build_feed


@pytest.fixture()
def feed(program_root, platform_root):
    """One thread, one dense post, no translation yet."""
    store = open_store(program_root, platform_root=platform_root)
    built = build_feed(store)
    yield store, built
    store.close()


def _panel(program_root, platform_root):
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        return data.build_feed_panel(rostore)
    finally:
        rostore.close()


def _translate(store, post_id: str, body: str, **gate_kwargs) -> dict:
    envelope = build_translation_envelope(store, post_id=post_id)
    gate = run_translation_gate(
        None, post_id=post_id, original_body=DENSE_POST, translation_body=body, **gate_kwargs
    )
    return store_translation(store, envelope=envelope, body=body, gate=gate.as_row())


# ---------------------------------------------------------------------------
# the five states
# ---------------------------------------------------------------------------


def test_state_absent_when_nothing_was_ever_asked_for(feed, program_root, platform_root):
    store, built = feed
    store.close()
    panel = _panel(program_root, platform_root)
    post = panel["posts"][0]
    assert post["translation_state"] == "absent"
    assert post["translation"] is None
    assert panel["translator_table_available"] is True
    assert panel["translation_withheld_count"] == 0


def test_state_pending_while_a_translation_job_is_in_flight(feed, program_root, platform_root):
    store, built = feed
    enqueue_job(
        store, kind="custom",
        payload={"handler": "feed_translate", "post_ids": [built["post_ids"][0]]},
    )
    store.close()
    post = _panel(program_root, platform_root)["posts"][0]
    assert post["translation_state"] == "pending"
    assert post["translation"] is None


def test_a_thread_wide_sweep_marks_every_untranslated_post_pending(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    built = build_feed(store, bodies=[DENSE_POST, "A second post."])
    enqueue_job(store, kind="custom", payload={"handler": "feed_translate", "thread_id": built["thread_id"]})
    store.close()
    panel = _panel(program_root, platform_root)
    assert [p["translation_state"] for p in panel["posts"]] == ["pending", "pending"]


def test_a_settled_job_no_longer_marks_a_post_pending(feed, program_root, platform_root):
    store, built = feed
    job = enqueue_job(
        store, kind="custom", payload={"handler": "feed_translate", "post_ids": [built["post_ids"][0]]}
    )
    store.jobs.execute("UPDATE job SET state = 'complete' WHERE job_id = ?", (job["job_id"],))
    store.jobs.commit()
    store.close()
    assert _panel(program_root, platform_root)["posts"][0]["translation_state"] == "absent"


def test_a_job_for_another_subsystem_never_marks_a_post_pending(feed, program_root, platform_root):
    store, built = feed
    enqueue_job(store, kind="custom", payload={"handler": "summarize", "subject_kind": "document"})
    store.close()
    assert _panel(program_root, platform_root)["posts"][0]["translation_state"] == "absent"


def test_state_translated_serves_the_body_and_the_verdict(feed, program_root, platform_root):
    store, built = feed
    row = _translate(store, built["post_ids"][0], FAITHFUL_TRANSLATION)
    store.close()
    post = _panel(program_root, platform_root)["posts"][0]
    assert post["translation_state"] == "translated"
    assert post["translation"]["body"] == FAITHFUL_TRANSLATION
    assert post["translation"]["translation_id"] == row["translation_id"]
    assert post["translation"]["gate_status"] == "pass"
    assert post["translation"]["gate_reasons"]["passed"] is True


def test_a_withheld_translation_never_leaves_the_server(feed, program_root, platform_root):
    store, built = feed
    _translate(store, built["post_ids"][0], UNFAITHFUL_TRANSLATION)
    store.close()
    panel = _panel(program_root, platform_root)
    post = panel["posts"][0]
    assert post["translation_state"] == "withheld"
    assert post["translation"]["body"] is None  # the body itself is NOT in the payload
    assert post["translation"]["gate_status"] == "fail"
    assert post["translation"]["gate_reasons"]["reasons"]  # the operator still learns why
    assert panel["translation_withheld_count"] == 1
    # belt-and-braces: the withheld text appears nowhere in the payload
    import json

    assert UNFAITHFUL_TRANSLATION not in json.dumps(panel)
    assert post["body"] == DENSE_POST  # the original stays


def test_a_withheld_translations_style_block_does_not_leak_a_quoted_span(feed, program_root, platform_root):
    """FT-4, fix pass: r1_sentence_length's own violation ``detail`` quotes
    up to 80 characters of the TRANSLATION -- the exact text a withheld
    row's ``"body": None`` promises never crosses the wire. A translation
    that is BOTH a fidelity failure (so gate_status='fail', withheld) and
    long enough to also trip the register-only sentence-length rule
    exercises the leak this test used to find with the style block still
    attached."""
    import json

    store, built = feed
    long_sentence = "The launch definitely completed successfully " + "and it went very well " * 4 + "today."
    assert len(long_sentence.split()) > 25  # actually trips r1_sentence_length
    translation = long_sentence  # drops the original's LNCH-01JXYZ4 id -- a fidelity failure too
    _translate(store, built["post_ids"][0], translation)
    store.close()

    panel = _panel(program_root, platform_root)
    post = panel["posts"][0]
    assert post["translation_state"] == "withheld"
    dumped = json.dumps(panel)
    assert translation[:80] not in dumped
    assert "style" not in post["translation"]["gate_reasons"]
    assert post["translation"]["gate_reasons"]["reasons"]  # the fidelity reason is still there


def test_state_ungated_for_a_row_written_outside_the_gate(feed, program_root, platform_root):
    store, built = feed
    envelope = build_translation_envelope(store, post_id=built["post_ids"][0])
    store_translation(store, envelope=envelope, body=FAITHFUL_TRANSLATION)  # no gate= at all
    store.close()
    post = _panel(program_root, platform_root)["posts"][0]
    assert post["translation_state"] == "ungated"
    assert post["translation"]["body"] == FAITHFUL_TRANSLATION  # served...
    assert post["translation"]["gate_status"] == "ungated"  # ...but flagged as unchecked


# ---------------------------------------------------------------------------
# the write action
# ---------------------------------------------------------------------------


def _dispatch(program_root, platform_root, body):
    return writes.dispatch("feed-translate", program_root=program_root, platform_root=platform_root, body=body)


def test_feed_translate_is_a_registered_write_action():
    assert "feed-translate" in writes.WRITABLE_ACTIONS
    assert "feed-translate" in writes.REQUIRED_FIELDS


def test_the_write_action_enqueues_a_job_and_never_translates_inline(feed, program_root, platform_root):
    store, built = feed
    store.close()
    result = _dispatch(program_root, platform_root, {"post_id": built["post_ids"][0]})
    assert result["ok"] is True
    assert result["result"]["state"] == "pending"
    assert result["result"]["target"] == built["post_ids"][0]

    # the post is now "pending" in the panel, and no translation row exists
    panel = _panel(program_root, platform_root)
    assert panel["posts"][0]["translation_state"] == "pending"

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        job = reopened.jobs.execute(
            "SELECT payload FROM job WHERE job_id = ?", (result["result"]["job_id"],)
        ).fetchone()
        import json

        payload = json.loads(job["payload"])
        assert payload["handler"] == "feed_translate"
        assert payload["post_ids"] == [built["post_ids"][0]]
        # a dashboard operator has no launch identity -- never invented here
        assert payload["created_by_launch"] is None
        assert reopened.ops.execute("SELECT COUNT(*) AS n FROM feed_post_translation").fetchone()["n"] == 0
    finally:
        reopened.close()


def test_the_write_action_accepts_a_thread_target(feed, program_root, platform_root):
    store, built = feed
    store.close()
    result = _dispatch(program_root, platform_root, {"thread_id": built["thread_id"]})
    assert result["ok"] is True
    assert result["result"]["target"] == built["thread_id"]


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({}, "exactly one of post_id / thread_id"),
        ({"post_id": "POST-1", "thread_id": "THR-1"}, "exactly one of post_id / thread_id"),
        ({"post_id": "POST-1", "style_mode": "terse"}, "style_mode"),
    ],
)
def test_the_write_action_refuses_bad_input_cleanly(feed, program_root, platform_root, body, fragment):
    store, _ = feed
    store.close()
    result = _dispatch(program_root, platform_root, body)
    assert result["ok"] is False
    assert fragment in result["message"]
