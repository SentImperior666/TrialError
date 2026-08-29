"""``trialerror memory`` CLI surface: argv parsing, envelope shaping, and
``--program-root`` resolution over ``trialerror.memory.*`` (mirrors
``tests/test_cli_law.py``'s convention)."""

from __future__ import annotations

import json

from trialerror.cli import main
from trialerror.stores.store import open_store
from tests._memory_fixtures import make_account


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def _seed_account(program_root, platform_root) -> str:
    store = open_store(program_root, platform_root=platform_root)
    try:
        return make_account(store)
    finally:
        store.close()


def test_memory_no_action_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(["memory", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "no_action"


def test_memory_program_root_not_found(tmp_path, platform_root, monkeypatch, capsys):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc, env = _call(["memory", "search"], capsys)
    assert rc == 1
    assert env["error"]["code"] == "program_root_not_found"


def test_memory_put_ok_envelope(program_root, platform_root, capsys):
    account_id = _seed_account(program_root, platform_root)
    rc, env = _call(
        [
            "memory", "put", "--program-root", str(program_root),
            "--key", "cli-topic", "--tier", "L0", "--kind", "rule",
            "--body", "put via CLI", "--account", account_id,
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["item"]["key"] == "cli-topic"
    assert env["nextActions"][0]["argv"][:3] == ["trialerror", "memory", "search"]


def test_memory_put_refuses_bad_tier(program_root, platform_root, capsys):
    import pytest

    account_id = _seed_account(program_root, platform_root)
    # argparse itself rejects an out-of-choices --tier (its `choices=`
    # constraint) before the handler ever runs -- SystemExit(2), same as
    # any other malformed `trialerror` invocation.
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "memory", "put", "--program-root", str(program_root),
                "--key", "x", "--tier", "L9", "--kind", "rule", "--body", "b", "--account", account_id,
            ]
        )
    assert exc_info.value.code == 2


def test_memory_search_returns_index_only(program_root, platform_root, capsys):
    account_id = _seed_account(program_root, platform_root)
    _call(
        [
            "memory", "put", "--program-root", str(program_root),
            "--key", "searchable", "--tier", "L1", "--kind", "fact",
            "--body", "the full body text", "--account", account_id,
        ],
        capsys,
    )
    rc, env = _call(["memory", "search", "--program-root", str(program_root), "--account", account_id], capsys)
    assert rc == 0
    assert env["result"]["count"] == 1
    assert "body" not in env["result"]["items"][0]


def test_memory_search_by_id_returns_full_body(program_root, platform_root, capsys):
    account_id = _seed_account(program_root, platform_root)
    _, put_env = _call(
        [
            "memory", "put", "--program-root", str(program_root),
            "--key", "full-fetch", "--tier", "L2", "--kind", "lesson",
            "--body", "the whole text", "--account", account_id,
        ],
        capsys,
    )
    item_id = put_env["result"]["item"]["memory_item_id"]
    rc, env = _call(["memory", "search", "--program-root", str(program_root), "--id", item_id], capsys)
    assert rc == 0
    assert env["result"]["item"]["body"] == "the whole text"


def test_memory_sync_export_then_sync_import_round_trip(program_root, platform_root, capsys):
    account_id = _seed_account(program_root, platform_root)
    _call(
        [
            "memory", "put", "--program-root", str(program_root),
            "--key", "roundtrip", "--tier", "L0", "--kind", "rule",
            "--body", "round trip body", "--account", account_id,
        ],
        capsys,
    )
    rc, export_env = _call(["memory", "sync-export", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert export_env["result"]["count"] == 1

    rc, import_env = _call(["memory", "sync-import", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert import_env["result"]["summary"]["conflicts"] == 0
    assert import_env["result"]["summary"]["dedup"] == 1  # re-importing your own export is a no-op


# ---- [paths].memory_dir knob (the import-design notes (internal, not in this export) Sec 5 knob #6) --------


def test_memory_sync_export_respects_configured_memory_dir(program_root, platform_root, tmp_path, capsys):
    account_id = _seed_account(program_root, platform_root)
    external = tmp_path / "external-memory"
    (program_root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n\n[paths]\nmemory_dir = {external.as_posix()!r}\n', encoding="utf-8"
    )
    _call(
        [
            "memory", "put", "--program-root", str(program_root),
            "--key", "relocated", "--tier", "L0", "--kind", "rule",
            "--body", "relocated body", "--account", account_id,
        ],
        capsys,
    )
    rc, export_env = _call(["memory", "sync-export", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert export_env["result"]["out_dir"] == str(external)
    assert external.is_dir()
    assert not (program_root / "memory").exists()

    rc, import_env = _call(["memory", "sync-import", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert import_env["result"]["summary"]["dedup"] == 1


def test_memory_sync_export_explicit_out_dir_still_wins_over_configured_memory_dir(
    program_root, platform_root, tmp_path, capsys
):
    account_id = _seed_account(program_root, platform_root)
    configured = tmp_path / "configured-memory"
    explicit = tmp_path / "explicit-memory"
    (program_root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n\n[paths]\nmemory_dir = {configured.as_posix()!r}\n', encoding="utf-8"
    )
    _call(
        [
            "memory", "put", "--program-root", str(program_root),
            "--key", "explicit-wins", "--tier", "L0", "--kind", "rule",
            "--body", "body", "--account", account_id,
        ],
        capsys,
    )
    rc, export_env = _call(
        ["memory", "sync-export", "--program-root", str(program_root), "--out-dir", str(explicit)], capsys
    )
    assert rc == 0
    assert export_env["result"]["out_dir"] == str(explicit)
    assert explicit.is_dir()
    assert not configured.exists()


def test_memory_merge_lists_conflicts_and_resolves(program_root, platform_root, capsys):
    account_id = _seed_account(program_root, platform_root)
    store = open_store(program_root, platform_root=platform_root)
    try:
        from trialerror.memory.api import put_item
        from trialerror.memory.merge import two_way_merge

        put_item(store, key="conflicted", tier="L0", kind="rule", body="local", account_id=account_id)
        result = two_way_merge(
            store,
            foreign_items=[{"key": "conflicted", "tier": "L0", "kind": "rule", "body": "foreign", "account_id": account_id}],
        )
        group_id = result.conflicts[0]["group_id"]
    finally:
        store.close()

    rc, list_env = _call(["memory", "merge", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert list_env["result"]["count"] == 1

    rc, resolve_env = _call(
        ["memory", "merge", "--program-root", str(program_root), "--group", group_id, "--keep", "left"], capsys
    )
    assert rc == 0
    assert resolve_env["result"]["keep"] == "left"

    rc, list_after = _call(["memory", "merge", "--program-root", str(program_root)], capsys)
    assert list_after["result"]["count"] == 0


def test_memory_merge_requires_group_and_keep_together(program_root, platform_root, capsys):
    _seed_account(program_root, platform_root)
    rc, env = _call(["memory", "merge", "--program-root", str(program_root), "--group", "x"], capsys)
    assert rc == 1
    assert env["error"]["code"] == "incomplete_resolution"
