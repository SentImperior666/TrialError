"""``trialerror room`` CLI surface (``trialerror/cli/room.py``) — argv parsing +
AgentEnvelope wrapping around ``trialerror.rooms.api``. Mirrors
``tests/test_artifacts_cli.py``'s style: seed prerequisite rows via a
directly-opened+closed ``Store``, then drive everything else through
``trialerror.cli.main``.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

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
def seeded(program_root, platform_root):
    """One account/session/launch + a 'room_theory_doc' template — closed
    before the CLI opens its own connection to the same WAL files (same
    pattern ``tests/test_artifacts_cli.py``'s ``seeded`` fixture uses)."""
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store, "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-test",
            "session_id": session_id, "agent_kind": "tester", "model_class": "top", "model": "sonnet",
            "purpose": "fixture", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
        },
    )
    insert(store, "template", {"type_key": "room_theory_doc", "title": "Room Theory Doc", "version": "1", "path": "templates/room_theory_doc.md", "gated": 0})
    store.close()
    return platform_root, program_root, launch_id


def _pr(program_root) -> list[str]:
    return ["--program-root", str(program_root)]


def test_room_group_discovered():
    names = {getattr(m, "GROUP_NAME", None) for m in discover_groups()}
    assert "room" in names


def test_room_no_action_is_a_structured_error(program_root, platform_root):
    rc, env = _run_cli(["room", *_pr(program_root)])
    assert rc == 1
    assert env["error"]["code"] == "no_action"


def test_room_program_root_not_found(tmp_path, monkeypatch):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc, env = _run_cli(["room", "status", "--id", "ROOM-x"])
    assert rc == 1
    assert env["error"]["code"] == "program_root_not_found"


def _create_room(program_root, *, dps=None, participants="P1,P2") -> str:
    dps = dps or [{"prompt": "does it generalize?"}, {"prompt": "does it survive a hostile edit?"}]
    rc, env = _run_cli(
        ["room", "create", *_pr(program_root), "--topic", "cli room", "--dps", json.dumps(dps), "--participants", participants]
    )
    assert rc == 0, env
    return env["result"]["room_id"]


def test_room_create_ok_envelope(seeded):
    _platform_root, program_root, _launch_id = seeded
    room_id = _create_room(program_root)
    assert room_id.startswith("ROOM-")


def test_room_create_bad_participant_count_refused(seeded):
    _platform_root, program_root, _launch_id = seeded
    rc, env = _run_cli(
        ["room", "create", *_pr(program_root), "--topic", "t", "--dps", json.dumps([{"prompt": "p"}]), "--participants", "P1,P2,P3,P4"]
    )
    assert rc == 1
    assert env["error"]["code"] == "create_refused"


def test_room_status_not_found(seeded):
    _platform_root, program_root, _launch_id = seeded
    rc, env = _run_cli(["room", "status", *_pr(program_root), "--id", "ROOM-bogus"])
    assert rc == 1
    assert env["error"]["code"] == "not_found"


def test_room_full_cli_lifecycle(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)

    rc, env = _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body", "opening turn"])
    assert rc == 0, env
    assert env["result"]["seq"] == 1

    rc, env = _run_cli(["room", "status", *_pr(program_root), "--id", room_id])
    assert rc == 0
    assert env["result"]["turn_count"] == 1

    rc, env = _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--agreement-pct", "95", "--by-launch", launch_id])
    assert rc == 0, env
    assert env["result"]["converged"] is True

    rc, env = _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP2", "--agreement-pct", "95", "--by-launch", launch_id])
    assert rc == 0

    rc, env = _run_cli(["room", "converge-check", *_pr(program_root), "--id", room_id])
    assert rc == 0
    assert env["result"]["applied"] is False
    assert env["result"]["convergence"]["all_converged"] is True

    rc, env = _run_cli(["room", "converge-check", *_pr(program_root), "--id", room_id, "--apply", "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["applied"] is True
    assert env["result"]["room"]["state"] == "converged"

    rc, env = _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body", "too late"])
    assert rc == 1
    assert env["error"]["code"] == "post_refused"


def test_room_freeze_via_cli(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    rc, env = _run_cli(["room", "freeze", *_pr(program_root), "--id", room_id, "--reason", "deadlocked", "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["state"] == "frozen"


def test_room_export_via_cli(seeded, tmp_path):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body", "exported turn"])
    out_path = tmp_path / "doc.md"
    rc, env = _run_cli(["room", "export", *_pr(program_root), "--id", room_id, "--out", str(out_path)])
    assert rc == 0, env
    assert out_path.is_file()
    assert "exported turn" in out_path.read_text(encoding="utf-8")


def test_room_post_body_file(seeded, tmp_path):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    body_file = tmp_path / "body.txt"
    body_file.write_text("turn from a file", encoding="utf-8")
    rc, env = _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body-file", str(body_file)])
    assert rc == 0, env
    assert env["result"]["body"] == "turn from a file"


def test_room_converge_check_apply_without_by_launch_refused(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--agreement-pct", "95", "--by-launch", launch_id])
    _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP2", "--agreement-pct", "95", "--by-launch", launch_id])
    rc, env = _run_cli(["room", "converge-check", *_pr(program_root), "--id", room_id, "--apply"])
    assert rc == 1
    assert env["error"]["code"] == "by_launch_required"
