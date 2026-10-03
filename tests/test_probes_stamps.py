"""``trialerror.probes.stamps.answer_stamp`` (design Section 3.5) and its two
attach points (MCP ``search``/``similar``, CLI ``query search``)."""

from __future__ import annotations

import datetime

import pytest

from trialerror.probes.stamps import answer_stamp
from trialerror.stores.store import open_store
from trialerror.util.config import resolve_program_id
from trialerror.util.timeutil import now


@pytest.fixture()
def local_store(platform_root, program_root):
    s = open_store(program_root, platform_root=platform_root)
    yield s
    s.close()


def _ts_ago(hours: float) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _seed_probe_run(store, *, name: str, status: str, started_ts: str, program_id: str | None = None) -> None:
    """B-2 fix round: ``answer_stamp`` now filters by ``program_id``, so a
    seeded row must carry the SAME program_id ``answer_stamp(store)`` will
    resolve for this ``store`` (its default) -- otherwise every one of these
    fixtures would seed a row the filtered query can never match."""
    if program_id is None:
        program_id = resolve_program_id(store.program_root)
    with store.platform:
        store.platform.execute(
            "INSERT INTO probe_run (name, kind, host, program_id, started_ts, status, detail) "
            "VALUES (?, 'canary', 'h', ?, ?, ?, '{}')",
            (name, program_id, started_ts, status),
        )


def test_never_run_canaries_report_never_and_are_not_degraded(local_store):
    stamp = answer_stamp(local_store)
    assert stamp["canary"] == {"fulltext": "never", "vector": "never", "last_ts": None}
    assert stamp["degraded"] is False
    assert stamp["degraded_reason"] is None
    assert stamp["coverage_embedded_pct"] is None


def test_a_recent_pass_reports_pass(local_store):
    _seed_probe_run(local_store, name="fulltext_canary", status="pass", started_ts=_ts_ago(0.1))
    stamp = answer_stamp(local_store)
    assert stamp["canary"]["fulltext"] == "pass"
    assert stamp["degraded"] is False


def test_a_recent_failure_degrades_the_stamp(local_store):
    _seed_probe_run(local_store, name="fulltext_canary", status="fail", started_ts=_ts_ago(1))
    stamp = answer_stamp(local_store)
    assert stamp["canary"]["fulltext"] == "fail"
    assert stamp["degraded"] is True
    assert "fulltext_canary failed" in stamp["degraded_reason"]


def test_an_error_status_row_degrades_too_not_only_fail(local_store):
    """N-4 fix round: `_display_status` already showed an 'error' row (a
    timed-out or raised probe) as "fail" to a caller, but `degraded` used to
    check the raw status against 'fail' only -- a stamp could read
    `fulltext: fail, degraded: false` for the exact same row."""
    _seed_probe_run(local_store, name="fulltext_canary", status="error", started_ts=_ts_ago(1))
    stamp = answer_stamp(local_store)
    assert stamp["canary"]["fulltext"] == "fail"
    assert stamp["degraded"] is True


def test_a_failure_older_than_24h_does_not_degrade(local_store):
    _seed_probe_run(local_store, name="fulltext_canary", status="fail", started_ts=_ts_ago(30))
    stamp = answer_stamp(local_store)
    assert stamp["degraded"] is False


def test_vector_canary_goes_stale_after_24h_regardless_of_its_last_status(local_store):
    _seed_probe_run(local_store, name="vector_canary", status="pass", started_ts=_ts_ago(25))
    stamp = answer_stamp(local_store)
    assert stamp["canary"]["vector"] == "stale"


def test_fulltext_canary_has_no_stale_state(local_store):
    """Design's own vocabulary: fulltext is pass|fail|skip|never -- never
    "stale", unlike vector."""
    _seed_probe_run(local_store, name="fulltext_canary", status="pass", started_ts=_ts_ago(48))
    stamp = answer_stamp(local_store)
    assert stamp["canary"]["fulltext"] == "pass"


def test_a_later_pass_clears_degradation(local_store):
    _seed_probe_run(local_store, name="fulltext_canary", status="fail", started_ts=_ts_ago(2))
    _seed_probe_run(local_store, name="fulltext_canary", status="pass", started_ts=_ts_ago(1))
    stamp = answer_stamp(local_store)
    assert stamp["canary"]["fulltext"] == "pass"
    assert stamp["degraded"] is False


def test_build_is_always_a_non_empty_string(local_store):
    stamp = answer_stamp(local_store)
    assert isinstance(stamp["build"], str) and stamp["build"]


def test_stamp_is_scoped_to_one_program_not_the_whole_machine(platform_root, tmp_path):
    """B-2: probe_run lives in the machine-wide platform.db. Program A's
    failure must not degrade program B's stamp, and a LATER pass in B must
    not clear A's degradation."""
    program_root_a = tmp_path / "program_a"
    program_root_b = tmp_path / "program_b"
    store_a = open_store(program_root_a, platform_root=platform_root)
    store_b = open_store(program_root_b, platform_root=platform_root)
    try:
        _seed_probe_run(store_a, name="fulltext_canary", status="fail", started_ts=_ts_ago(1))
        _seed_probe_run(store_b, name="fulltext_canary", status="pass", started_ts=_ts_ago(1))

        stamp_a = answer_stamp(store_a)
        stamp_b = answer_stamp(store_b)
        assert stamp_a["degraded"] is True
        assert stamp_a["canary"]["fulltext"] == "fail"
        assert stamp_b["degraded"] is False
        assert stamp_b["canary"]["fulltext"] == "pass"

        # a later pass in B must not clear A's already-recorded degradation
        _seed_probe_run(store_b, name="fulltext_canary", status="pass", started_ts=_ts_ago(0.1))
        assert answer_stamp(store_a)["degraded"] is True
    finally:
        store_a.close()
        store_b.close()


# ---------------------------------------------------------------------------
# attach points
# ---------------------------------------------------------------------------


def test_mcp_search_envelope_carries_a_stamp(local_store):
    from trialerror.mcp.knowledge import _tool_search

    env = _tool_search({"query": "nothing matches this empty corpus"}, store=local_store)
    assert env["ok"] is True
    assert "stamp" in env["result"]


def test_mcp_search_absence_note_appears_only_when_degraded_and_empty(local_store):
    from trialerror.mcp.knowledge import _tool_search

    env_clean = _tool_search({"query": "nothing here"}, store=local_store)
    assert "absence_note" not in env_clean["result"]

    _seed_probe_run(local_store, name="fulltext_canary", status="fail", started_ts=_ts_ago(1))
    env_degraded = _tool_search({"query": "still nothing here"}, store=local_store)
    assert env_degraded["result"]["stamp"]["degraded"] is True
    assert "search is degraded" in env_degraded["result"]["absence_note"]


def test_cli_query_search_envelope_carries_a_stamp(local_store, monkeypatch):
    import argparse

    from trialerror.cli import query as query_cli

    monkeypatch.setattr(query_cli, "_open", lambda args, cmd: (local_store, None))
    args = argparse.Namespace(
        query="nothing matches", k=10, mode="auto", source_ids=None, kinds=None, license_tiers=None,
        years=None, unfenced=False, launch_id=None, program_root=None, platform_root=None,
    )
    env = query_cli._run_search(args)
    assert "stamp" in env["result"]
