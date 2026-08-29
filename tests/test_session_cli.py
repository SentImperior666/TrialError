"""Tests for the ``trialerror session`` CLI group (``trialerror/cli/session.py``) —
argv parsing + AgentEnvelope wrapping around ``trialerror.sessions``.

Argument ordering note: ``--program-root``/``--platform-root`` are
registered on each ACTION subparser (``boot``, ``close``, ...), not on the
``session`` group parser itself -- the same shape ``trialerror/cli/jobs.py``
uses (see its own ``_common`` helper) -- so they must come AFTER the
subcommand token: ``["session", "boot", "--program-root", ...]``, never
``["session", "--program-root", ..., "boot"]``.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from trialerror.cli import discover_groups, main
from trialerror.events.api import append_event
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
def account(roots):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    store.close()
    return account_id


def test_session_group_discovered():
    names = {getattr(m, "GROUP_NAME", None) for m in discover_groups()}
    assert "session" in names


def test_session_boot_and_status_round_trip(roots, account):
    platform_root, program_root = roots
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]

    rc, env = _run_cli(["session", "boot", *common])
    assert rc == 0, env
    assert env["ok"] is True
    session_id = env["result"]["session_id"]
    assert env["result"]["account_id"] == account

    rc, env = _run_cli(["session", "status", *common])
    assert rc == 0
    assert env["result"]["open"] is True
    assert env["result"]["session"]["session_id"] == session_id


def test_session_boot_ambiguous_account_refused(roots, account):
    platform_root, program_root = roots
    store = open_store(program_root, platform_root=platform_root)
    insert(store, "account", {"account_id": new_id("ACC"), "label": "second", "created_ts": now()})
    store.close()

    rc, env = _run_cli(
        ["session", "boot", "--program-root", str(program_root), "--platform-root", str(platform_root)]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "account_required"


def test_session_boot_create_account_bootstrap(roots):
    platform_root, program_root = roots
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    rc, env = _run_cli(["session", "boot", "--create-account", "fresh", *common])
    assert rc == 0, env
    assert env["ok"] is True


def test_session_close_requires_course_check_json(roots, account):
    platform_root, program_root = roots
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    _run_cli(["session", "boot", *common])

    rc, env = _run_cli(["session", "close", "--course-check", "not json", *common])
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_course_check_json"


def test_session_close_hooks_disabled_refused_then_succeeds_after_hook_alive(roots, account):
    platform_root, program_root = roots
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    rc, env = _run_cli(["session", "boot", *common])
    session_id = env["result"]["session_id"]

    rc, env = _run_cli(["session", "close", "--course-check", '{"rungs":"1"}', *common])
    assert env["ok"] is False
    assert env["error"]["code"] == "hooks_disabled"

    store = open_store(program_root, platform_root=platform_root)
    append_event(store, event_type="hook_alive", session_id=session_id, payload={})
    store.close()

    rc, env = _run_cli(["session", "close", "--course-check", '{"rungs":"1"}', *common])
    assert rc == 0, env
    assert env["ok"] is True
    assert env["result"]["code"] == "closed"


def test_session_close_and_render_handoff_respect_configured_handoffs_dir(roots, account, tmp_path):
    """the import-design notes (internal, not in this export) Sec 5 knob #3: the CLI actually loads
    trialerror.toml and threads it through boot/close/render-handoff -- not
    just trialerror.sessions.lifecycle/.handoff's own library-level ``config``
    parameter."""
    platform_root, program_root = roots
    external = tmp_path / "external-handoffs"
    (program_root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n\n[paths]\nhandoffs_dir = {str(external)!r}\n', encoding="utf-8"
    )
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    rc, env = _run_cli(["session", "boot", *common])
    assert rc == 0, env
    session_id = env["result"]["session_id"]

    store = open_store(program_root, platform_root=platform_root)
    append_event(store, event_type="hook_alive", session_id=session_id, payload={})
    store.close()

    rc, env = _run_cli(["session", "close", "--course-check", '{"rungs":"1"}', *common])
    assert rc == 0, env
    handoff_filename = env["result"]["close_report"]["handoff_filename"]
    assert (external / handoff_filename).is_file()
    assert not (program_root / "handoffs").exists()

    rc, env = _run_cli(["session", "render-handoff", "--session-id", session_id, *common])
    assert rc == 0, env
    assert env["result"]["path"] == str(external / handoff_filename)


def test_session_render_handoff_after_close(roots, account):
    platform_root, program_root = roots
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    rc, env = _run_cli(["session", "boot", *common])
    session_id = env["result"]["session_id"]

    store = open_store(program_root, platform_root=platform_root)
    append_event(store, event_type="hook_alive", session_id=session_id, payload={})
    store.close()

    rc, env = _run_cli(["session", "close", "--course-check", '{"rungs":"1"}', *common])
    assert env["ok"] is True

    rc, env = _run_cli(["session", "render-handoff", "--session-id", session_id, *common])
    assert rc == 0, env
    assert env["ok"] is True
    assert env["result"]["filename"].startswith("HANDOFF_")


def test_session_abandon_then_boot_fresh_succeeds(roots, account):
    platform_root, program_root = roots
    common = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    rc, env = _run_cli(["session", "boot", *common])
    session_id = env["result"]["session_id"]

    rc, env = _run_cli(["session", "boot", "--fresh", *common])
    assert env["ok"] is False
    assert env["error"]["code"] == "session_already_open"

    rc, env = _run_cli(["session", "abandon", "--session-id", session_id, "--reason", "crashed", *common])
    assert rc == 0, env
    assert env["ok"] is True

    rc, env = _run_cli(["session", "boot", "--fresh", *common])
    assert rc == 0, env
    assert env["ok"] is True
    assert env["result"]["session_id"] != session_id


def test_session_program_root_not_found(tmp_path, monkeypatch):
    empty_cwd = tmp_path / "no-trialerror-toml-here"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    rc, env = _run_cli(["session", "status"])
    assert env["ok"] is False
    assert env["error"]["code"] == "program_root_not_found"
