"""``trialerror units`` CLI group -- ``scan``/``list``/``show``/``cost``
envelopes (design Section 2.3). Calls the group's own ``run`` handlers
directly (same convention as other CLI-group test files in this tree),
against the ``tests/fixtures/units/`` tree and a scratch ``--platform-root``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from trialerror.cli import units as units_cli

FIXTURES = Path(__file__).resolve().parents[0] / "fixtures" / "units"


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture()
def platform_root(tmp_path):
    return tmp_path / "platform"


def test_scan_cmd_ok_envelope(platform_root):
    args = _ns(
        platform_root=str(platform_root), projects=str(FIXTURES), host="test-host",
        statusline_dir=None, dry_run=False,
    )
    env = units_cli._run_scan_cmd(args)
    assert env["ok"] is True
    assert env["result"]["files_read"] > 0


def test_scan_then_list_and_show(platform_root):
    scan_args = _ns(
        platform_root=str(platform_root), projects=str(FIXTURES), host="test-host",
        statusline_dir=None, dry_run=False,
    )
    units_cli._run_scan_cmd(scan_args)

    list_args = _ns(
        platform_root=str(platform_root), host="test-host", session_id=None, usage_source=None,
        since=None, limit=50,
    )
    env = units_cli._run_list(list_args)
    assert env["ok"] is True
    assert env["result"]["count"] > 0
    unit_key = env["result"]["units"][0]["unit_key"]

    show_args = _ns(platform_root=str(platform_root), unit_key=unit_key)
    show_env = units_cli._run_show(show_args)
    assert show_env["ok"] is True
    assert show_env["result"]["unit_key"] == unit_key


def test_show_missing_unit_is_an_error(platform_root):
    args = _ns(platform_root=str(platform_root), unit_key="nope/nope/-")
    env = units_cli._run_show(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "not_found"


def test_cost_by_project_matches_hand_computation(platform_root):
    scan_args = _ns(
        platform_root=str(platform_root), projects=str(FIXTURES), host="test-host",
        statusline_dir=None, dry_run=False,
    )
    units_cli._run_scan_cmd(scan_args)

    cost_args = _ns(platform_root=str(platform_root), host="test-host", group_by="project", since=None, until=None)
    env = units_cli._run_cost(cost_args)
    assert env["ok"] is True
    by_group = {row["key"]: row for row in env["result"]["by_group"]}
    assert "proj-alpha" in by_group
    # SESS-MAIN(450) + SESS-SUB(18) + SESS-RESUME-A(33) + SESS-RESUME-B(33) = 534
    assert by_group["proj-alpha"]["usage_input"] == 450 + 18 + 33 + 33


def test_cost_footer_reports_units_with_no_transcript(platform_root):
    scan_args = _ns(
        platform_root=str(platform_root), projects=str(FIXTURES), host="test-host",
        statusline_dir=None, dry_run=False,
    )
    units_cli._run_scan_cmd(scan_args)
    cost_args = _ns(platform_root=str(platform_root), host="test-host", group_by="project", since=None, until=None)
    env = units_cli._run_cost(cost_args)
    assert env["result"]["units_by_source"].get("none", 0) >= 1
    assert "no transcript on disk" in env["result"]["footer"]


def test_open_platform_only_never_touches_disk_for_ops_knowledge_jobs(platform_root):
    """This CLI group's whole reason to exist (module docstring): only
    ``platform.db`` is a real file. The other three connections must be
    in-memory, never a path under any program root."""
    args = _ns(platform_root=str(platform_root))
    store = units_cli._open_platform_only(args)
    try:
        assert (platform_root / "platform.db").exists()
        for conn in (store.ops, store.knowledge, store.jobs):
            filename = conn.execute("PRAGMA database_list").fetchone()["file"]
            assert filename == ""
    finally:
        store.close()
