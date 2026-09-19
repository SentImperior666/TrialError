"""Acceptance criterion (design Section 4.2 / build brief M1 deliverable):
"secret fixture redacted" — both the standalone ``redact_payload`` pass and
its automatic application on ``event.payload`` at insert time.
"""

from __future__ import annotations

import json

from trialerror.stores import insert
from trialerror.stores.redact import REDACTED_PLACEHOLDER, redact_payload, redact_text
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything


def test_redact_text_replaces_openai_style_key():
    text = "here is my key: sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out, count = redact_text(text)
    assert count == 1
    assert "sk-ABCDEF" not in out
    assert REDACTED_PLACEHOLDER in out


def test_redact_text_replaces_generic_key_value_assignment():
    text = "config: api_key=abcd1234efgh5678 was set yesterday"
    out, count = redact_text(text)
    assert count == 1
    assert "abcd1234efgh5678" not in out
    assert "api_key=" in out  # label survives, only the value is scrubbed


def test_redact_text_leaves_ordinary_prose_untouched():
    text = "the launch booked 12000 tokens for the citecheck pass"
    out, count = redact_text(text)
    assert count == 0
    assert out == text


def test_redact_payload_walks_nested_structures():
    payload = {
        "note": "no secret here",
        "creds": {"aws": "AKIAABCDEFGHIJKLMNOP", "list": ["fine", "sk-" + "x" * 24]},
    }
    redacted, count = redact_payload(payload)
    assert count == 2
    assert redacted["creds"]["aws"] == REDACTED_PLACEHOLDER
    assert redacted["creds"]["list"][0] == "fine"
    assert REDACTED_PLACEHOLDER in redacted["creds"]["list"][1]
    assert redacted["note"] == "no secret here"


def test_event_insert_applies_redaction_pass_and_records_count(store):
    ids = populate_one_of_everything(store)
    secret_payload = json.dumps({"token": "ghp_" + "a" * 36})
    written = insert(
        store,
        "event",
        {
            "event_id": new_id("EVT"),
            "ts": now(),
            "session_id": ids["session"],
            "launch_id": ids["launch"],
            "type": "secret_leak_fixture",
            "payload": secret_payload,
        },
    )
    assert written["redactions"] == 1
    stored_payload = json.loads(written["payload"])
    assert stored_payload["token"] == REDACTED_PLACEHOLDER

    row = store.ops.execute("SELECT payload, redactions FROM event WHERE event_id = ?", (written["event_id"],)).fetchone()
    assert json.loads(row["payload"])["token"] == REDACTED_PLACEHOLDER
    assert row["redactions"] == 1


def test_event_insert_with_no_secrets_records_zero_redactions(store):
    ids = populate_one_of_everything(store)
    written = insert(
        store,
        "event",
        {
            "event_id": new_id("EVT"),
            "ts": now(),
            "type": "clean_event",
            "payload": json.dumps({"note": "nothing secret"}),
        },
    )
    assert written["redactions"] == 0


def test_event_insert_respects_explicit_redactions_override(store):
    """An explicit `redactions` value (e.g. a caller replaying an
    already-redacted payload) is not clobbered by the auto-count."""
    ids = populate_one_of_everything(store)
    written = insert(
        store,
        "event",
        {
            "event_id": new_id("EVT"),
            "ts": now(),
            "type": "replay",
            "payload": json.dumps({"note": "clean"}),
            "redactions": 3,
        },
    )
    assert written["redactions"] == 3
