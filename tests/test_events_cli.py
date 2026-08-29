"""``trialerror events`` / ``trialerror feed`` / ``trialerror inbox`` — CLI groups.
Exercises the real ``trialerror.cli`` auto-discovery (design Section 5.2's
registration rule: dropping a ``trialerror/cli/<group>.py`` file is the entire
registration step) and each handler end to end via
``AgentEnvelope``-shaped output, mirroring
``tests/test_cli_group_autodiscovery.py``'s style of calling
``args.handler(args)`` directly rather than shelling out.
"""

from __future__ import annotations

import json

from trialerror.cli import build_parser, discover_groups
from trialerror.stores.store import open_store

from tests.test_events_helpers import seed_launch


def test_events_feed_inbox_groups_are_auto_discovered():
    names = {getattr(m, "GROUP_NAME", None) for m in discover_groups()}
    assert {"events", "feed", "inbox"} <= names


def _parse(argv):
    parser = build_parser()
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_cli_events_append_then_tail(program_root, platform_root):
    root = str(program_root)
    args = _parse(
        ["events", "append", "--program-root", root, "--type", "boarded", "--payload", '{"note": "hi"}']
    )
    env = args.handler(args)
    assert env["ok"] is True
    assert env["result"]["event_id"].startswith("EVT-")

    tail_args = _parse(["events", "tail", "--program-root", root, "--limit", "5"])
    tail_env = tail_args.handler(tail_args)
    assert tail_env["ok"] is True
    assert tail_env["result"]["count"] == 1
    assert tail_env["result"]["events"][0]["type"] == "boarded"


def test_cli_events_append_redacts_secret(program_root, platform_root):
    root = str(program_root)
    payload = json.dumps({"token": "sk-ant-" + "a" * 30})
    args = _parse(["events", "append", "--program-root", root, "--type", "secret", "--payload", payload])
    env = args.handler(args)
    assert env["ok"] is True
    assert env["result"]["redactions"] == 1


def test_cli_events_append_bad_json_payload_is_an_error_envelope(program_root, platform_root):
    args = _parse(["events", "append", "--program-root", str(program_root), "--type", "x", "--payload", "{not json"])
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_payload_json"


def test_cli_events_export_writes_jsonl_file(program_root, platform_root, tmp_path):
    root = str(program_root)
    for i in range(2):
        args = _parse(
            ["events", "append", "--program-root", root, "--type", f"e{i}", "--payload", "{}", "--workpackage", "WKP-x"]
        )
        args.handler(args)

    out_path = tmp_path / "out.jsonl"
    export_args = _parse(["events", "export", "--program-root", root, "--out", str(out_path), "--workpackage", "WKP-x"])
    env = export_args.handler(export_args)
    assert env["ok"] is True
    assert env["result"]["count"] == 2
    assert out_path.exists()
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_cli_program_root_not_found_is_an_error_envelope(tmp_path, monkeypatch, platform_root):
    empty_dir = tmp_path / "nowhere"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)
    args = _parse(["events", "tail"])
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "program_root_not_found"


def test_cli_events_no_action_is_an_error_envelope(program_root, platform_root):
    args = _parse(["events"])
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_action"


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------


def test_cli_feed_post_new_thread_then_read(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    launch_id = seed_launch(store, agent_kind="build-M5")
    store.close()

    root = str(program_root)
    post_args = _parse(
        [
            "feed",
            "post",
            "--program-root",
            root,
            "--body",
            "full text of the post",
            "--new-thread",
            "round 1",
            "--launch-id",
            launch_id,
        ]
    )
    env = post_args.handler(post_args)
    assert env["ok"] is True
    assert env["result"]["author"] == f"build-M5:{launch_id}"
    thread_id = env["result"]["thread_id"]

    read_args = _parse(["feed", "read", "--program-root", root, "--thread-id", thread_id])
    read_env = read_args.handler(read_args)
    assert read_env["ok"] is True
    assert read_env["result"]["count"] == 1
    assert read_env["result"]["posts"][0]["body"] == "full text of the post"
    assert read_env["result"]["posts"][0]["author"] == f"build-M5:{launch_id}"

    threads_args = _parse(["feed", "threads", "--program-root", root])
    threads_env = threads_args.handler(threads_args)
    assert threads_env["ok"] is True
    assert any(t["thread_id"] == thread_id for t in threads_env["result"]["threads"])


def test_cli_feed_post_new_thread_without_launch_id_is_refused(program_root, platform_root):
    args = _parse(
        ["feed", "post", "--program-root", str(program_root), "--body", "x", "--new-thread", "t"]
    )
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "new_thread_needs_launch"


def test_cli_feed_post_missing_thread_target_is_refused(program_root, platform_root):
    args = _parse(["feed", "post", "--program-root", str(program_root), "--body", "x"])
    env = args.handler(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "missing_thread"


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


def test_cli_inbox_post_then_read(program_root, platform_root):
    root = str(program_root)
    post_args = _parse(["inbox", "post", "--program-root", root, "--body", "check the queue"])
    env = post_args.handler(post_args)
    assert env["ok"] is True
    assert env["result"]["item_id"].startswith("INBX-")

    read_args = _parse(["inbox", "read", "--program-root", root])
    read_env = read_args.handler(read_args)
    assert read_env["ok"] is True
    assert read_env["result"]["count"] == 1
    assert read_env["result"]["items"][0]["body"] == "check the queue"

    # Second read: already marked read, nothing left unread.
    read_again = _parse(["inbox", "read", "--program-root", root])
    env_again = read_again.handler(read_again)
    assert env_again["result"]["count"] == 0


def test_cli_inbox_read_no_mark_read_is_a_peek(program_root, platform_root):
    root = str(program_root)
    post_args = _parse(["inbox", "post", "--program-root", root, "--body", "peek me"])
    post_args.handler(post_args)

    peek_args = _parse(["inbox", "read", "--program-root", root, "--no-mark-read"])
    peek_env = peek_args.handler(peek_args)
    assert peek_env["result"]["count"] == 1

    peek_again_env = peek_args.handler(peek_args)
    assert peek_again_env["result"]["count"] == 1
