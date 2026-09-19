"""``trialerror.events.api`` — event append/tail/export. Covers the M5
acceptance criteria "secret fixture redacted" (end to end through this
module's own :func:`append_event`, not re-testing the regex patterns
themselves -- those are M1's ``tests/test_stores_redact.py``) and
"jsonl export byte-stable", plus the general append/tail/export/redaction
contract.
"""

from __future__ import annotations

import json

from trialerror.events.api import append_event, export_events, export_jsonl, render_jsonl, tail_events
from trialerror.stores.errors import ValidationError
from trialerror.stores.redact import REDACTED_PLACEHOLDER
from trialerror.util.timeutil import now

import pytest

from tests.test_events_helpers import seed_launch, seed_session


def test_append_event_auto_generates_id_and_ts(store):
    session_id = seed_session(store)
    row = append_event(store, event_type="boarded", payload={"note": "hi"}, session_id=session_id)
    assert row["event_id"].startswith("EVT-")
    assert row["ts"]
    assert row["type"] == "boarded"
    assert json.loads(row["payload"]) == {"note": "hi"}
    assert row["redactions"] == 0


def test_append_event_accepts_launch_id_and_workpackage(store):
    launch_id = seed_launch(store, agent_kind="build-M5")
    row = append_event(
        store, event_type="built", payload={"files": 3}, launch_id=launch_id, workpackage="WKP-063_research_harness"
    )
    assert row["launch_id"] == launch_id
    assert row["workpackage"] == "WKP-063_research_harness"


def test_append_event_rejects_missing_launch_target(store):
    from trialerror.stores.errors import XidTargetMissingError

    with pytest.raises(XidTargetMissingError):
        append_event(store, event_type="x", payload={}, launch_id="LNCH-does-not-exist")


def test_append_event_none_payload_refused(store):
    """event.payload is NOT NULL; append_event does not silently coerce
    None into a JSON 'null' string (that would satisfy NOT NULL while
    losing the caller's likely intent) -- it passes None straight through
    to the store, which refuses it as a real DB constraint violation."""
    with pytest.raises(ValidationError):
        append_event(store, event_type="x", payload=None)


def test_append_event_redacts_secret_end_to_end(store):
    """The M5 acceptance criterion: 'secret fixture redacted', proven
    through THIS module's own append_event -- trialerror.events does not
    reimplement redaction, it inherits trialerror.stores.insert's pass."""
    row = append_event(
        store,
        event_type="secret_leak_fixture",
        payload={"token": "sk-ant-" + "a" * 30, "note": "safe text stays"},
    )
    assert row["redactions"] == 1
    stored = json.loads(row["payload"])
    assert stored["token"] == REDACTED_PLACEHOLDER
    assert stored["note"] == "safe text stays"

    # And the row actually persisted redacted, not just the return value.
    raw = store.ops.execute("SELECT payload, redactions FROM event WHERE event_id = ?", (row["event_id"],)).fetchone()
    assert json.loads(raw["payload"])["token"] == REDACTED_PLACEHOLDER
    assert raw["redactions"] == 1


def test_append_event_with_no_secret_records_zero_redactions(store):
    row = append_event(store, event_type="clean", payload={"note": "nothing secret here"})
    assert row["redactions"] == 0


def test_tail_events_filters_by_workpackage_and_orders_oldest_first(store):
    launch_id = seed_launch(store)
    t0 = now()
    append_event(store, event_type="a", payload={}, workpackage="WKP-A", launch_id=launch_id, ts="2026-01-01T00:00:00.000Z")
    append_event(store, event_type="b", payload={}, workpackage="WKP-A", launch_id=launch_id, ts="2026-01-02T00:00:00.000Z")
    append_event(store, event_type="c", payload={}, workpackage="WKP-B", launch_id=launch_id, ts="2026-01-03T00:00:00.000Z")

    rows = tail_events(store, workpackage="WKP-A")
    assert [r["type"] for r in rows] == ["a", "b"]
    assert rows[0]["ts"] < rows[1]["ts"]


def test_tail_events_respects_limit(store):
    for i in range(5):
        append_event(store, event_type=f"e{i}", payload={"i": i}, ts=f"2026-01-01T00:00:0{i}.000Z")
    rows = tail_events(store, limit=2)
    assert [r["type"] for r in rows] == ["e3", "e4"]


def test_export_events_scoped_by_session(store):
    s1 = seed_session(store)
    s2 = seed_session(store)
    append_event(store, event_type="x", payload={}, session_id=s1, ts="2026-01-01T00:00:00.000Z")
    append_event(store, event_type="y", payload={}, session_id=s2, ts="2026-01-01T00:00:01.000Z")
    rows = export_events(store, session_id=s1)
    assert len(rows) == 1
    assert rows[0]["type"] == "x"


def test_export_jsonl_single_file_is_byte_stable(store, tmp_path):
    launch_id = seed_launch(store)
    for i in range(3):
        append_event(
            store,
            event_type="built",
            payload={"i": i},
            launch_id=launch_id,
            workpackage="WKP-063_research_harness",
            ts=f"2026-01-01T00:00:0{i}.000Z",
        )

    out1 = tmp_path / "export1.jsonl"
    out2 = tmp_path / "export2.jsonl"
    result1 = export_jsonl(store, out_path=out1, workpackage="WKP-063_research_harness")
    result2 = export_jsonl(store, out_path=out2, workpackage="WKP-063_research_harness")

    assert result1["count"] == 3
    bytes1 = out1.read_bytes()
    bytes2 = out2.read_bytes()
    assert bytes1 == bytes2

    lines = bytes1.decode("utf-8").splitlines()
    assert len(lines) == 3
    for line, i in zip(lines, range(3)):
        obj = json.loads(line)
        assert obj["payload"] == {"i": i}
        assert list(obj.keys()) == [
            "event_id",
            "ts",
            "session_id",
            "launch_id",
            "workpackage",
            "type",
            "payload",
            "redactions",
        ]


def test_export_jsonl_split_by_workpackage_writes_one_file_per_workpackage(store, tmp_path):
    launch_id = seed_launch(store)
    append_event(store, event_type="a", payload={}, workpackage="WKP-A", launch_id=launch_id, ts="2026-01-01T00:00:00.000Z")
    append_event(store, event_type="b", payload={}, workpackage="WKP-B", launch_id=launch_id, ts="2026-01-01T00:00:01.000Z")
    append_event(store, event_type="c", payload={}, workpackage=None, launch_id=launch_id, ts="2026-01-01T00:00:02.000Z")

    out_dir = tmp_path / "events_export"
    result = export_jsonl(store, out_path=out_dir, split_by_workpackage=True)

    assert result["mode"] == "split_by_workpackage"
    assert set(result["files"]) == {"WKP-A", "WKP-B", "_no_workpackage"}
    assert (out_dir / "WKP-A.jsonl").exists()
    assert (out_dir / "WKP-B.jsonl").exists()
    assert (out_dir / "_no_workpackage.jsonl").exists()
    assert json.loads((out_dir / "WKP-A.jsonl").read_text(encoding="utf-8").strip())["type"] == "a"


def test_render_jsonl_empty_list_is_empty_string():
    assert render_jsonl([]) == ""
