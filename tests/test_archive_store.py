"""The transcript archive: copy, supersede, keep gone files, prune, refuse unsafe
destinations, lock, restore. Synthetic files in temporary folders only."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trialerror.archive import store
from trialerror.archive.store import ArchiveError, kind_of, run_archive
from trialerror.cli import main

T0 = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)


def put(path: Path, data: bytes, mtime: int = 1_700_000_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows(dest: Path, sql: str, *params):
    db = sqlite3.connect(str(dest / "index.db"))
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


@pytest.fixture()
def area(tmp_path):
    src = tmp_path / "projects"
    src.mkdir()
    return src, tmp_path / "arc"


def test_first_run_then_no_read_when_nothing_changed(area):
    src, dest = area
    for i in range(3):
        put(src / "proj-a" / f"s{i}.jsonl", f'{{"n": {i}}}\n'.encode())
    first = run_archive(src, dest, "hostx", now=T0)
    assert first["scanned"] == 3 and first["new_objects"] == 3
    assert len(rows(dest, "SELECT * FROM object")) == 3
    assert len(rows(dest, "SELECT * FROM path_state")) == 3

    opened: list[str] = []

    def counting_opener(path, mode="rb"):
        opened.append(str(path))
        return open(path, mode)

    second = run_archive(src, dest, "hostx", now=T0 + timedelta(hours=2), opener=counting_opener)
    assert second["skipped_unchanged"] == 3 and second["new_objects"] == 0
    assert opened == []


def test_objects_hold_the_exact_bytes(area):
    src, dest = area
    data = b"line one\r\nline two\n\x00\xff"
    put(src / "p" / "a.jsonl", data)
    run_archive(src, dest, "h", now=T0)
    obj = dest / "objects" / sha(data)[:2] / f"{sha(data)}.gz"
    assert gzip.decompress(obj.read_bytes()) == data


def test_append_supersedes_and_rewrite_keeps_both(area):
    src, dest = area
    f = src / "p" / "a.jsonl"
    v1 = b'{"a":1}\n'
    v2 = v1 + b'{"a":2}\n'
    put(f, v1, 1_700_000_000)
    run_archive(src, dest, "h", now=T0)
    put(f, v2, 1_700_000_100)
    res = run_archive(src, dest, "h", now=T0 + timedelta(hours=2))
    assert res["new_objects"] == 1 and res["superseded"] == 1
    old = rows(dest, "SELECT superseded_by FROM object WHERE sha256 = ?", sha(v1))[0]
    assert old["superseded_by"] == sha(v2)
    assert rows(dest, "SELECT superseded_by FROM object WHERE sha256 = ?", sha(v2))[0]["superseded_by"] is None

    v3 = b'{"different": true}\n'  # not a prefix extension of v2
    put(f, v3, 1_700_000_200)
    res = run_archive(src, dest, "h", now=T0 + timedelta(hours=4))
    assert res["new_objects"] == 1 and res["superseded"] == 0
    assert rows(dest, "SELECT superseded_by FROM object WHERE sha256 = ?", sha(v2))[0]["superseded_by"] is None
    assert len(rows(dest, "SELECT * FROM snapshot WHERE rel_path = 'p/a.jsonl'")) == 3


def test_gone_file_marked_once_and_restored_exactly(area, tmp_path):
    src, dest = area
    data = b"last words\n"
    f = put(src / "p" / "a.jsonl", data)
    run_archive(src, dest, "h", now=T0)
    f.unlink()
    r1 = run_archive(src, dest, "h", now=T0 + timedelta(hours=2))
    assert r1["gone"] == 1
    first_gone = rows(dest, "SELECT gone_ts FROM path_state")[0]["gone_ts"]
    r2 = run_archive(src, dest, "h", now=T0 + timedelta(hours=4))
    assert r2["gone"] == 0
    assert rows(dest, "SELECT gone_ts FROM path_state")[0]["gone_ts"] == first_gone
    out = tmp_path / "restored" / "a.jsonl"
    res = store.restore(dest, out, host="h", rel_path="p/a.jsonl")
    assert out.read_bytes() == data and res["verified"] is True


def test_gone_file_that_returns_is_no_longer_gone(area):
    src, dest = area
    f = put(src / "p" / "a.jsonl", b"x\n")
    run_archive(src, dest, "h", now=T0)
    f.unlink()
    run_archive(src, dest, "h", now=T0 + timedelta(hours=2))
    put(src / "p" / "a.jsonl", b"x\n")  # same bytes, same mtime
    run_archive(src, dest, "h", now=T0 + timedelta(hours=4))
    assert rows(dest, "SELECT gone_ts FROM path_state")[0]["gone_ts"] is None


def test_restore_as_of_and_by_sha_and_no_overwrite(area, tmp_path):
    src, dest = area
    f = src / "p" / "a.jsonl"
    v1, v2 = b"one\n", b"one\ntwo\n"
    put(f, v1, 1_700_000_000)
    run_archive(src, dest, "h", now=T0)
    put(f, v2, 1_700_000_100)
    run_archive(src, dest, "h", now=T0 + timedelta(days=1))
    out1 = tmp_path / "o1"
    store.restore(dest, out1, host="h", rel_path="p/a.jsonl", as_of=(T0 + timedelta(hours=1)).isoformat())
    assert out1.read_bytes() == v1
    out2 = tmp_path / "o2"
    store.restore(dest, out2, sha=sha(v2))
    assert out2.read_bytes() == v2
    with pytest.raises(ArchiveError) as exc:
        store.restore(dest, out2, sha=sha(v2))
    assert exc.value.code == "out_exists"
    with pytest.raises(ArchiveError) as exc:
        store.restore(dest, tmp_path / "o3", sha="0" * 64)
    assert exc.value.code == "not_found"
    with pytest.raises(ArchiveError) as exc:
        store.restore(dest, tmp_path / "o4", sha=sha(v2), host="h", rel_path="p/a.jsonl")
    assert exc.value.code == "bad_input"


def test_restore_refuses_a_corrupted_object(area, tmp_path):
    src, dest = area
    data = b"payload\n"
    put(src / "p" / "a.jsonl", data)
    run_archive(src, dest, "h", now=T0)
    obj = dest / "objects" / sha(data)[:2] / f"{sha(data)}.gz"
    obj.write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(ArchiveError) as exc:
        store.restore(dest, tmp_path / "out", sha=sha(data))
    assert exc.value.code == "hash_mismatch"
    assert not (tmp_path / "out").exists()


def test_prune_removes_only_old_superseded_non_latest(area):
    src, dest = area
    f = src / "p" / "a.jsonl"
    keep = src / "p" / "b.jsonl"
    v1, v2 = b"one\n", b"one\ntwo\n"
    put(f, v1, 1_700_000_000)
    put(keep, b"static\n")
    run_archive(src, dest, "h", now=T0)
    put(f, v2, 1_700_000_100)
    run_archive(src, dest, "h", now=T0 + timedelta(days=1))
    # superseded only a day ago: not pruned even 20 days later
    res = run_archive(src, dest, "h", now=T0 + timedelta(days=20))
    assert res["pruned"] == 0
    old_obj = dest / "objects" / sha(v1)[:2] / f"{sha(v1)}.gz"
    assert old_obj.exists()
    # more than 30 days after the supersession: pruned; latest and unsuperseded stay
    res = run_archive(src, dest, "h", now=T0 + timedelta(days=40))
    assert res["pruned"] == 1
    assert not old_obj.exists()
    assert rows(dest, "SELECT deleted_ts FROM object WHERE sha256 = ?", sha(v1))[0]["deleted_ts"] is not None
    for data in (v2, b"static\n"):
        assert (dest / "objects" / sha(data)[:2] / f"{sha(data)}.gz").exists()


def test_no_prune_flag_keeps_old_objects(area):
    src, dest = area
    f = src / "p" / "a.jsonl"
    put(f, b"one\n", 1_700_000_000)
    run_archive(src, dest, "h", now=T0)
    put(f, b"one\ntwo\n", 1_700_000_100)
    run_archive(src, dest, "h", now=T0 + timedelta(days=1))
    res = run_archive(src, dest, "h", now=T0 + timedelta(days=60), prune=False)
    assert res["pruned"] == 0


SECRET_PATHS = [
    "p/keys/id_thing",
    "p/notes.key",
    "p/.ssh/config",
    ".credentials.json",
    "p/rclone.conf",
    "p/cookies.txt",
]


@pytest.mark.parametrize("rel", SECRET_PATHS)
def test_secret_patterns_are_never_copied(area, rel):
    src, dest = area
    put(src / rel, b"top secret\n")
    put(src / "p" / "ok.jsonl", b"fine\n")
    res = run_archive(src, dest, "h", now=T0)
    assert res["scanned"] == 1 and res["new_objects"] == 1
    assert store.is_secret(rel)
    stored = {r["rel_path"] for r in rows(dest, "SELECT rel_path FROM path_state")}
    assert stored == {"p/ok.jsonl"}
    assert not any(sha(b"top secret\n") in str(p) for p in dest.rglob("*"))


def test_destination_inside_a_git_working_tree_is_refused(tmp_path):
    src = tmp_path / "projects"
    src.mkdir()
    put(src / "p" / "a.jsonl", b"x\n")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ArchiveError) as exc:
        run_archive(src, repo / "deep" / "arc", "h", now=T0)
    assert exc.value.code == "archive_in_repo"
    assert not (repo / "deep").exists()


def test_destination_inside_source_is_refused(area):
    src, _dest = area
    put(src / "p" / "a.jsonl", b"x\n")
    with pytest.raises(ArchiveError) as exc:
        run_archive(src, src / "arc", "h", now=T0)
    assert exc.value.code == "dest_in_src"


def test_missing_source_is_refused(tmp_path):
    with pytest.raises(ArchiveError) as exc:
        run_archive(tmp_path / "nope", tmp_path / "arc", "h", now=T0)
    assert exc.value.code == "src_not_found"


def test_a_second_concurrent_run_reports_locked(area):
    src, dest = area
    put(src / "p" / "a.jsonl", b"x\n")
    with store.exclusive_lock(dest / ".lock") as mine:
        assert mine is True
        res = run_archive(src, dest, "h", now=T0)
    assert res.get("locked") is True
    assert not (dest / "index.db").exists()
    # the lock is released afterwards: a normal run works
    assert run_archive(src, dest, "h", now=T0)["new_objects"] == 1


def test_dry_run_writes_nothing(area):
    src, dest = area
    put(src / "p" / "a.jsonl", b"x\n")
    res = run_archive(src, dest, "h", dry_run=True, now=T0)
    assert res["new_objects"] == 1 and res["dry_run"] is True
    assert not dest.exists()
    run_archive(src, dest, "h", now=T0)
    put(src / "p" / "b.jsonl", b"y\n")
    before = rows(dest, "SELECT COUNT(*) AS n FROM object")[0]["n"]
    res = run_archive(src, dest, "h", dry_run=True, now=T0 + timedelta(hours=2))
    assert res["new_objects"] == 1
    assert rows(dest, "SELECT COUNT(*) AS n FROM object")[0]["n"] == before
    assert len(rows(dest, "SELECT * FROM run")) == 1


def test_run_row_is_written(area):
    src, dest = area
    put(src / "p" / "a.jsonl", b"x\n")
    run_archive(src, dest, "hostx", now=T0)
    run = rows(dest, "SELECT * FROM run")[0]
    assert run["host"] == "hostx" and run["finished_ts"] and run["scanned"] == 1 and run["new_objects"] == 1


def test_identical_content_in_two_files_is_stored_once(area):
    src, dest = area
    put(src / "p" / "a.jsonl", b"same\n")
    put(src / "p" / "b.jsonl", b"same\n")
    res = run_archive(src, dest, "h", now=T0)
    assert res["new_objects"] == 1
    assert len(rows(dest, "SELECT * FROM path_state")) == 2


def test_status_reports_per_host(area):
    src, dest = area
    put(src / "p" / "a.jsonl", b"x\n")
    run_archive(src, dest, "hostx", now=T0)
    report = store.archive_status(dest)["hosts"]["hostx"]
    assert report["files"] == 1 and report["gone"] == 0 and report["last_run"]["finished_ts"]


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "rel, kind",
    [
        ("proj/abc.jsonl", "main"),
        ("proj/abc/subagents/agent-1.jsonl", "subagent"),
        ("proj/abc/subagents/agent-1.meta.json", "subagent_meta"),
        ("proj/abc/workflows/run1/journal.jsonl", "workflow_journal"),
        ("proj/abc/workflows/run1/meta.json", "workflow_meta"),
        ("proj/abc/tool-results/toolu_1.txt", "tool_result"),
        ("proj/memory/MEMORY.md", "other"),
        ("proj/abc/notes.txt", "other"),
        ("toplevel.jsonl", "other"),
    ],
)
def test_kind_of(rel, kind):
    assert kind_of(rel) == kind
    assert kind_of(rel.replace("/", "\\")) == kind  # a Windows-style path classifies the same


def test_rel_paths_are_forward_slash_and_memory_is_archived(area):
    src, dest = area
    put(src / "p" / "sess" / "subagents" / "agent-1.jsonl", b"x\n")
    put(src / "p" / "memory" / "MEMORY.md", b"notes\n")
    run_archive(src, dest, "h", now=T0)
    found = {r["rel_path"]: r["kind"] for r in rows(dest, "SELECT rel_path, kind FROM path_state")}
    assert found == {"p/sess/subagents/agent-1.jsonl": "subagent", "p/memory/MEMORY.md": "other"}
    assert not any("\\" in p for p in found)


# -------------------------------------------------------------------------- CLI


def _cli(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return code, json.loads(out)


def test_cli_run_status_restore_round_trip(area, capsys, tmp_path):
    src, dest = area
    data = b"cli bytes\n"
    put(src / "p" / "a.jsonl", data)
    code, env = _cli(capsys, "archive", "run", "--src", str(src), "--dest", str(dest), "--host", "hx")
    assert code == 0 and env["ok"] and env["result"]["new_objects"] == 1
    code, env = _cli(capsys, "archive", "status", "--dest", str(dest))
    assert code == 0 and env["result"]["hosts"]["hx"]["files"] == 1
    out = tmp_path / "back.jsonl"
    code, env = _cli(
        capsys, "archive", "restore", "--dest", str(dest), "--host", "hx", "--path", "p/a.jsonl", "--out", str(out)
    )
    assert code == 0 and out.read_bytes() == data


def test_cli_refusal_is_a_structured_error(area, capsys, tmp_path):
    src, _ = area
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    code, env = _cli(capsys, "archive", "run", "--src", str(src), "--dest", str(repo / "arc"), "--host", "h")
    assert code == 1 and env["error"]["code"] == "archive_in_repo"
    code, env = _cli(capsys, "archive")
    assert code == 1 and env["error"]["code"] == "no_action"


def test_cli_second_run_says_locked_and_exits_zero(area, capsys):
    src, dest = area
    put(src / "p" / "a.jsonl", b"x\n")
    with store.exclusive_lock(dest / ".lock"):
        code, env = _cli(capsys, "archive", "run", "--src", str(src), "--dest", str(dest), "--host", "h")
    assert code == 0 and env["result"]["locked"] is True


def test_a_source_can_be_deleted_while_the_archive_reads_it(area):
    src, dest = area
    data = b"contents that outlive their file" + bytes([10])
    f = put(src / "p" / "a.jsonl", data)
    mishaps: list[str] = []
    held: list[str] = []

    def hostile_opener(path, mode="rb"):
        handle = store.default_opener(path, mode)
        held.append(str(path))
        try:  # what Claude Code's own clean-up would do meanwhile
            os.remove(f)
        except OSError as exc:
            mishaps.append(f"{path}: {exc}")
        return handle

    res = run_archive(src, dest, "h", now=T0, opener=hostile_opener)
    assert mishaps == [] and len(held) == 1
    assert res["errors"] == [] and res["new_objects"] == 1
    obj = dest / "objects" / sha(data)[:2] / f"{sha(data)}.gz"
    assert gzip.decompress(obj.read_bytes()) == data


def test_the_default_opener_reads_exactly_the_bytes(tmp_path):
    f = put(tmp_path / "x.bin", b"\x00\x01exact\r\n")
    with store.default_opener(f, "rb") as handle:
        assert handle.read() == b"\x00\x01exact\r\n"
    with pytest.raises(FileNotFoundError):
        store.default_opener(tmp_path / "missing.bin", "rb")


def test_a_pruned_version_is_restored_from_the_chain_that_superseded_it(area, tmp_path):
    src, dest = area
    f = src / "p" / "a.jsonl"
    v1 = b"one" + bytes([10])
    v2 = v1 + b"two" + bytes([10])
    v3 = v2 + b"three" + bytes([10])
    put(f, v1, 1_700_000_000)
    run_archive(src, dest, "h", now=T0)
    put(f, v2, 1_700_000_100)
    run_archive(src, dest, "h", now=T0 + timedelta(days=1))
    put(f, v3, 1_700_000_200)
    run_archive(src, dest, "h", now=T0 + timedelta(days=2))
    res = run_archive(src, dest, "h", now=T0 + timedelta(days=60))
    assert res["pruned"] == 2
    for data in (v1, v2):
        assert not (dest / "objects" / sha(data)[:2] / f"{sha(data)}.gz").exists()
    out1 = tmp_path / "v1"
    store.restore(dest, out1, sha=sha(v1))
    assert out1.read_bytes() == v1
    out2 = tmp_path / "v2"
    store.restore(dest, out2, host="h", rel_path="p/a.jsonl", as_of=(T0 + timedelta(days=1, hours=1)).isoformat())
    assert out2.read_bytes() == v2
    # an object with no superseding version is still reported as missing, not invented
    v3_file = dest / "objects" / sha(v3)[:2] / f"{sha(v3)}.gz"
    v3_file.unlink()
    with pytest.raises(ArchiveError) as exc:
        store.restore(dest, tmp_path / "v3", sha=sha(v3))
    assert exc.value.code == "object_missing"
    with pytest.raises(ArchiveError):
        store.restore(dest, tmp_path / "v1b", sha=sha(v1))  # its chain now ends at the lost object


def test_prune_commits_the_deletion_before_it_unlinks_the_file(area, monkeypatch):
    src, dest = area
    f = src / "p" / "a.jsonl"
    v1 = b"one" + bytes([10])
    put(f, v1, 1_700_000_000)
    run_archive(src, dest, "h", now=T0)
    put(f, v1 + b"two" + bytes([10]), 1_700_000_100)
    run_archive(src, dest, "h", now=T0 + timedelta(days=1))
    old = dest / "objects" / sha(v1)[:2] / f"{sha(v1)}.gz"
    seen: dict = {}

    def killed(dest_, shas):
        # the moment before the unlink: the deletion must already be durable in the index
        seen["deleted_ts"] = rows(dest_, "SELECT deleted_ts FROM object WHERE sha256 = ?", sha(v1))[0]["deleted_ts"]
        seen["file_still_there"] = old.exists()
        raise RuntimeError("killed between the commit and the unlink")

    monkeypatch.setattr(store, "_unlink_pruned", killed)
    with pytest.raises(RuntimeError):
        run_archive(src, dest, "h", now=T0 + timedelta(days=40))
    monkeypatch.undo()
    assert seen["deleted_ts"] is not None and seen["file_still_there"] is True
    # the orphan file is harmless (the row says deleted) and the next run sweeps it
    assert rows(dest, "SELECT deleted_ts FROM object WHERE sha256 = ?", sha(v1))[0]["deleted_ts"] is not None
    run_archive(src, dest, "h", now=T0 + timedelta(days=40, hours=2))
    assert not old.exists()


def test_a_run_clears_temp_files_left_by_a_killed_run(area):
    src, dest = area
    put(src / "p" / "a.jsonl", b"x" + bytes([10]))
    orphan = dest / "tmp" / "obj.tmp12345"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"half an object")
    with store.exclusive_lock(dest / ".lock"):  # a run that finds the archive locked must not touch tmp/
        assert run_archive(src, dest, "h", now=T0).get("locked") is True
    assert orphan.exists()
    run_archive(src, dest, "h", now=T0)
    assert not orphan.exists()
    assert list((dest / "tmp").iterdir()) == []


def test_a_dry_run_never_takes_the_index_write_lock(area, monkeypatch):
    src, dest = area
    put(src / "p" / "a.jsonl", b"x" + bytes([10]))
    run_archive(src, dest, "h", now=T0)
    put(src / "p" / "b.jsonl", b"new file" + bytes([10]))  # a dry run has something to record
    monkeypatch.setattr(store, "_BUSY_TIMEOUT_S", 0.3)
    holder = sqlite3.connect(str(dest / "index.db"), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # a real run in the middle of its transaction
    try:
        res = run_archive(src, dest, "h", dry_run=True, now=T0 + timedelta(hours=2))
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert res["dry_run"] is True and res["new_objects"] == 1
    assert len(rows(dest, "SELECT * FROM object")) == 1  # and it wrote nothing
    assert len(rows(dest, "SELECT * FROM run")) == 1
