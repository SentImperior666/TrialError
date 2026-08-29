"""Tests for the ``trialerror budget`` CLI group (``trialerror/cli/budget.py``) —
argv parsing + AgentEnvelope wrapping around ``trialerror.budget.pools``."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from trialerror.cli import discover_groups, main
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


@pytest.fixture()
def roots(tmp_path):
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    return platform_root, program_root


@pytest.fixture()
def account_and_session(roots):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()
    return account_id, session_id


def test_budget_group_discovered():
    names = {getattr(m, "GROUP_NAME", None) for m in discover_groups()}
    assert "budget" in names


def test_budget_no_subcommand():
    rc, env = _run_cli(["budget"])
    assert env["ok"] is False
    assert env["error"]["code"] == "no_subcommand"


def test_budget_pools_create_and_list(roots, account_and_session):
    platform_root, program_root = roots
    account_id, _ = account_and_session

    rc, env = _run_cli(
        [
            "budget",
            "--program-root", str(program_root),
            "--platform-root", str(platform_root),
            "pools", "--create",
            "--account-id", account_id,
            "--model-class", "top",
            "--period", "weekly",
            "--cap-tokens", "100000",
        ]
    )
    assert rc == 0, env
    assert env["ok"] is True
    pool_id = env["result"]["created"]["pool_id"]

    rc2, env2 = _run_cli(
        ["budget", "--program-root", str(program_root), "--platform-root", str(platform_root), "pools",
         "--account-id", account_id]
    )
    assert rc2 == 0
    assert any(p["pool_id"] == pool_id for p in env2["result"]["pools"])


def test_budget_pools_create_missing_args(roots):
    platform_root, program_root = roots
    rc, env = _run_cli(
        ["budget", "--program-root", str(program_root), "--platform-root", str(platform_root), "pools", "--create"]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "missing_arguments"


def test_budget_book_reconcile_status_rollup_round_trip(roots, account_and_session):
    platform_root, program_root = roots
    account_id, session_id = account_and_session
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]

    rc, env = _run_cli(
        [
            "budget", *common, "book",
            "--session-id", session_id,
            "--program-id", "PROG-test",
            "--agent-kind", "lens",
            "--model-class", "mid",
            "--model", "sonnet",
            "--purpose", "mechanical",
            "--est-tokens", "100",
        ]
    )
    assert rc == 0, env
    assert env["ok"] is True
    launch_id = env["result"]["launch_id"]
    assert env["result"]["state"] == "PROVISIONAL"
    assert env["meta"]["prompt_fragment"] == f"launch_id: {launch_id}"

    rc, env = _run_cli(["budget", *common, "status", "--account-id", account_id])
    assert rc == 0
    assert env["ok"] is True

    rc, env = _run_cli(
        ["budget", *common, "reconcile", "--launch-id", launch_id, "--actual-tokens", "88"]
    )
    assert rc == 0
    assert env["ok"] is True
    assert env["result"]["state"] == "RECONCILED"

    rc, env = _run_cli(["budget", *common, "rollup", "--launch-id", launch_id])
    assert rc == 0
    assert env["result"]["actual_tokens_total"] == 88


def test_budget_book_no_open_session_refused(roots, tmp_path):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "closed"},
    )
    store.close()

    rc, env = _run_cli(
        [
            "budget", "--program-root", str(program_root), "--platform-root", str(platform_root), "book",
            "--session-id", session_id, "--program-id", "PROG-test", "--agent-kind", "lens",
            "--model-class", "mid", "--model", "sonnet", "--purpose", "mechanical", "--est-tokens", "10",
        ]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "no_open_session"


def test_budget_snapshot_ingest_and_calibrate(roots, account_and_session):
    platform_root, program_root = roots
    account_id, session_id = account_and_session
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]

    rc, env = _run_cli(
        [
            "budget", *common, "snapshot-ingest",
            "--account-id", account_id, "--source", "screenshot",
            "--payload", '{"model_class":"mid","used_tokens":1000}',
        ]
    )
    assert rc == 0
    assert env["ok"] is True

    rc, env = _run_cli(
        [
            "budget", *common, "snapshot-ingest",
            "--account-id", account_id, "--source", "screenshot",
            "--payload", "not json",
        ]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "invalid_payload"

    # Not enough snapshots yet for a real calibration -> structured refusal.
    rc, env = _run_cli(["budget", *common, "calibrate", "--account-id", account_id, "--model-class", "mid"])
    assert env["ok"] is False
    assert env["error"]["code"] == "calibrate_refused"
