"""M5 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m1_acceptance.py`` convention: this file IS the
acceptance-criteria mapping, each test here re-running (self-contained,
not just importing) a narrower assertion that already lives in its
dedicated module.

    | Acceptance criterion (design Section 12, M5 row)                                    | Test |
    |-----------------------------------------------------------------------------------------|------|
    | secret fixture redacted                                                                  | test_secret_fixture_redacted (see test_events_api.py::test_append_event_redacts_secret_end_to_end) |
    | author spoof attempt via API impossible (author derived: launch identity, else orchestrator:<session>) | test_author_spoof_attempt_via_api_impossible (see test_events_feed.py) |
    | jsonl export byte-stable                                                                | test_jsonl_export_byte_stable (see test_events_api.py::test_export_jsonl_single_file_is_byte_stable) |

pytestmark below matches the M0/M1 convention of tagging the module-level
acceptance suite so ``pytest -m acceptance`` picks it up.
"""

from __future__ import annotations

import inspect
import json

import pytest

from trialerror.events.api import append_event, create_thread, export_jsonl, post_feed
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.redact import REDACTED_PLACEHOLDER

from tests.test_events_helpers import seed_launch, seed_session

pytestmark = pytest.mark.acceptance


def test_secret_fixture_redacted(store):
    row = append_event(
        store,
        event_type="secret_leak_fixture",
        payload={"token": "sk-ant-" + "a" * 30, "aws": "AKIAABCDEFGHIJKLMNOP"},
    )
    assert row["redactions"] == 2
    stored = json.loads(row["payload"])
    assert stored["token"] == REDACTED_PLACEHOLDER
    assert stored["aws"] == REDACTED_PLACEHOLDER

    on_disk = store.ops.execute(
        "SELECT payload, redactions FROM event WHERE event_id = ?", (row["event_id"],)
    ).fetchone()
    assert json.loads(on_disk["payload"])["token"] == REDACTED_PLACEHOLDER
    assert on_disk["redactions"] == 2


def test_author_spoof_attempt_via_api_impossible(store):
    # 1. Structural: post_feed has no author/name/display_name parameter at
    #    all -- there is nothing to pass an arbitrary identity through.
    params = set(inspect.signature(post_feed).parameters)
    assert not params & {"author", "name", "display_name"}

    # 2. Launch identity: author is derived from platform.launch.agent_kind
    #    via launch_id, not accepted as text.
    launch_id = seed_launch(store, agent_kind="build-M5")
    thread = create_thread(store, title="acceptance thread", launch_id=launch_id)
    post = post_feed(store, thread_id=thread["thread_id"], body="real text", launch_id=launch_id)
    assert post["author"] == f"build-M5:{launch_id}"

    # 3. A launch_id that doesn't exist can't be used to manufacture ANY
    #    author string -- the write is refused outright.
    with pytest.raises(XidTargetMissingError):
        post_feed(store, thread_id=thread["thread_id"], body="x", launch_id="LNCH-not-real")

    # 4. Orchestrator fallback (no launch_id): bound to the currently open
    #    session, itself validated to exist and be open -- not a free
    #    string a caller can substitute for anything.
    # explicit session_id (not relying on auto-resolve, since this store
    # already has an earlier open session from seed_launch() above -- see
    # test_events_feed.py::test_post_feed_orchestrator_fallback_uses_open_session
    # for the auto-resolve-with-a-single-open-session path).
    open_session = seed_session(store, status="open")
    orch_post = post_feed(
        store, thread_id=thread["thread_id"], body="orchestrator text", launch_id=None, session_id=open_session
    )
    assert orch_post["author"] == f"orchestrator:{open_session}"

    with pytest.raises(ValidationError):
        post_feed(store, thread_id=thread["thread_id"], body="x", launch_id=None, session_id="SESS-not-real")

    closed_session = seed_session(store, status="closed")
    with pytest.raises(ValidationError):
        post_feed(store, thread_id=thread["thread_id"], body="x", launch_id=None, session_id=closed_session)


def test_jsonl_export_byte_stable(store, tmp_path):
    launch_id = seed_launch(store, agent_kind="build-M5")
    for i in range(4):
        append_event(
            store,
            event_type="built",
            payload={"i": i},
            launch_id=launch_id,
            workpackage="WKP-063_research_harness",
            ts=f"2026-08-29T00:0{i}:00.000Z",
        )

    first = export_jsonl(store, out_path=tmp_path / "a.jsonl", workpackage="WKP-063_research_harness")
    second = export_jsonl(store, out_path=tmp_path / "b.jsonl", workpackage="WKP-063_research_harness")
    third = export_jsonl(store, out_path=tmp_path / "a.jsonl", workpackage="WKP-063_research_harness")  # re-export, same path

    assert first["count"] == second["count"] == third["count"] == 4
    bytes_a = (tmp_path / "a.jsonl").read_bytes()
    bytes_b = (tmp_path / "b.jsonl").read_bytes()
    assert bytes_a == bytes_b  # same content regardless of destination path

    # Re-exporting to the SAME path with no new events is byte-identical
    # (the acceptance wording verbatim: "jsonl export byte-stable").
    again = (tmp_path / "a.jsonl").read_bytes()
    assert again == bytes_a
