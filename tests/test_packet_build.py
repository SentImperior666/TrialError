"""Building, pushing and reminding for the decision packet. The notifier is always
a stub script that records its arguments to a temporary file; nothing is ever
sent, and no real store or program is opened."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trialerror.cli import main
from trialerror.packet import build as pb
from trialerror.packet import store as ps
from trialerror.packet.store import PacketError, packet_settings
from trialerror.stores.store import open_store
from trialerror.stores.writer import update as store_update
from tests._store_fixtures import populate_one_of_everything

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
STUB = """import json, sys
with open(sys.argv[1], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[2:]) + '\\n')
"""
FLAKY_STUB = "\n".join(
    [
        "import os, sys, json",
        "if os.path.exists(sys.argv[1] + '.down'):",
        "    sys.stderr.write('channel down')",
        "    sys.exit(255)",
        "with open(sys.argv[1], 'a') as handle:",
        "    handle.write(json.dumps(sys.argv[2:]) + chr(10))",
        "",
    ]
)
FAILING_STUB = "import sys\nsys.stderr.write('channel down')\nsys.exit(3)\n"
SECRET = "hunter2xyz"


def toml_list(values):
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def item(what="A question for you?", **over):
    raw = {
        "what": what,
        "why": "It unblocks the next step.",
        "options": [
            {"key": "a", "label": "Go ahead", "consequence": "The step runs."},
            {"key": "b", "label": "Hold", "consequence": "The step waits."},
        ],
        "recommended": "a",
        "if_undecided": "The step waits.",
        "needed_by": "next-session",
    }
    raw.update(over)
    return raw


@pytest.fixture()
def env(tmp_path):
    """A temporary program with a stub notifier; returns (settings, log path)."""
    root = tmp_path / "prog"
    root.mkdir()
    stub = tmp_path / "notify_stub.py"
    stub.write_text(STUB, encoding="utf-8")
    log = tmp_path / "notified.jsonl"
    (root / "course.txt").write_text("Line one of the course.\nLine two.\nLine three.\nLine four (dropped).\n", encoding="utf-8")
    cmd = [sys.executable, stub.as_posix(), log.as_posix(), f"--token={SECRET}"]
    (root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n[packet]\n'
        f"notify_cmd = {toml_list(cmd)}\nlink = \"https://example.invalid/packet\"\ncourse_file = \"course.txt\"\n",
        encoding="utf-8",
    )
    return packet_settings(root), log


def calls(log: Path):
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def add(settings, what, now=T0, **over):
    row, _ = ps.add_item(settings, item(what, **over), now=now)
    return row


# ------------------------------------------------------------------------ build


def test_order_is_blocking_then_needed_by_then_priority_then_age(env):
    settings, _ = env
    old = T0 - timedelta(days=3)
    a = add(settings, "low, next session", priority="low", now=T0)
    b = add(settings, "normal, dated later", needed_by="2026-06-01", now=T0)
    c = add(settings, "normal, dated sooner", needed_by="2026-04-01", now=T0)
    d = add(settings, "blocking, dated", priority="blocking", needed_by="2026-05-01", now=T0)
    e = add(settings, "blocking, next session, newer", priority="blocking", now=T0)
    f = add(settings, "blocking, next session, older", priority="blocking", now=old)
    g = add(settings, "normal, next session", now=T0)
    result = pb.build_packet(settings, "manual", dry_run=True, now=T0)
    order = [i["what"] for i in result["packet"]["items"]]
    assert order == [
        "blocking, next session, older",
        "blocking, next session, newer",
        "blocking, dated",
        "normal, next session",
        "low, next session",
        "normal, dated sooner",
        "normal, dated later",
    ]
    assert result["packet"]["minutes"] == 21


def test_the_thirty_minute_cap_lists_the_overflow_by_what(env):
    settings, _ = env
    for n in range(1, 7):
        add(settings, f"Question number {n}?", est_minutes=8, now=T0 + timedelta(minutes=n))
    result = pb.build_packet(settings, "manual", dry_run=True, now=T0)["packet"]
    assert [i["what"] for i in result["items"]] == [f"Question number {n}?" for n in (1, 2, 3)]
    assert result["minutes"] == 24
    assert [w["what"] for w in result["waiting"]] == [f"Question number {n}?" for n in (4, 5, 6)]


def test_an_item_longer_than_the_cap_still_opens_the_packet_when_first(env):
    settings, _ = env
    add(settings, "One very long decision?", est_minutes=45)
    add(settings, "A short one?", est_minutes=2, now=T0 + timedelta(minutes=1))
    result = pb.build_packet(settings, "manual", dry_run=True, now=T0)["packet"]
    assert [i["what"] for i in result["items"]] == ["One very long decision?"]
    assert [w["what"] for w in result["waiting"]] == ["A short one?"]


def test_max_minutes_comes_from_the_config(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "trialerror.toml").write_text('[program]\nid = "demo"\n[packet]\nmax_minutes = 5\n', encoding="utf-8")
    settings = packet_settings(root)
    add(settings, "First?", est_minutes=4)
    add(settings, "Second?", est_minutes=4, now=T0 + timedelta(minutes=1))
    result = pb.build_packet(settings, "manual", dry_run=True, now=T0)["packet"]
    assert [i["what"] for i in result["items"]] == ["First?"] and len(result["waiting"]) == 1


def test_blocking_determinations_are_included_and_labelled(tmp_path, program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store_update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={
            "state": "submitted",
            "edits": json.dumps([{"edit_id": "E1", "text": "fix the tally", "blocking": True, "verified": False}]),
            "critic_launch": ids["launch"],
            "verdict_ts": "2026-03-01T00:00:00.000Z",
            "reproduction_status": "unrun",
        },
    )
    store.close()
    settings = packet_settings(program_root)
    add(settings, "An item of our own?")
    result = pb.build_packet(settings, "session_close", dry_run=True, platform_root=platform_root, now=T0)
    decide = [i for i in result["packet"]["items"] if i["source"] == "decide"]
    gate = [i for i in decide if i["what"] == "fix the tally"]
    assert len(gate) == 1
    assert gate[0]["label"] == "from DECIDE: not yet in plain words" and gate[0]["est_minutes"] == 3
    assert gate[0]["consequence"] and gate[0]["priority"] == "blocking"
    assert "from DECIDE: not yet in plain words" in result["markdown"] and "fix the tally" in result["markdown"]
    # non-blocking kinds of the queue are not pulled in
    assert all(i["priority"] == "blocking" for i in decide)
    assert result["packet"]["items"][-1]["what"] == "An item of our own?"  # blocking ones lead


def test_without_a_store_the_packet_still_builds_with_a_note(env, tmp_path):
    settings, _ = env
    add(settings, "Only ours?")
    result = pb.build_packet(settings, "manual", dry_run=True, platform_root=tmp_path / "no-platform", now=T0)["packet"]
    assert [i["what"] for i in result["items"]] == ["Only ours?"]
    assert any("no operations store" in n for n in result["notes"])


def test_the_first_three_course_lines_open_the_packet(env):
    settings, _ = env
    add(settings, "Q?")
    result = pb.build_packet(settings, "manual", dry_run=True, now=T0)
    assert result["packet"]["course"] == ["Line one of the course.", "Line two.", "Line three."]
    md = result["markdown"]
    assert md.index("Line one of the course.") < md.index("## 1. Q?")
    assert "Line four" not in md


def test_dry_run_writes_nothing_and_a_real_build_writes_md_and_json(env):
    settings, _ = env
    add(settings, "Q?")
    pb.build_packet(settings, "manual", dry_run=True, now=T0)
    assert not settings.built.exists()
    result = pb.build_packet(settings, "session_close", now=T0)
    md, js = Path(result["path_md"]), Path(result["path_json"])
    assert md.name == "PACKET_20260302T090000Z.md" and js.name == "PACKET_20260302T090000Z.json"
    assert json.loads(js.read_text(encoding="utf-8"))["trigger"] == "session_close"
    text = md.read_text(encoding="utf-8")
    assert "# Decisions for you: 1 (about 3 min)" in text
    assert "`a`: Go ahead **(recommended)**. The step runs." in text and "`b`: Hold. The step waits." in text
    assert "**If nobody decides.** The step waits." in text
    assert "Answer: https://example.invalid/packet" in text
    again = pb.build_packet(settings, "manual", now=T0)  # same second: never overwrites
    assert Path(again["path_json"]).name == "PACKET_20260302T090000Z_2.json"


def test_the_answer_line_shows_the_command_when_no_link_is_configured(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    settings = packet_settings(root)
    row = add(settings, "Q?")
    md = pb.build_packet(settings, "manual", dry_run=True, now=T0)["markdown"]
    assert f"trialerror packet answer {row['id']} --choice" in md


def test_a_bad_trigger_is_refused(env):
    settings, _ = env
    with pytest.raises(PacketError) as exc:
        pb.build_packet(settings, "sometime", dry_run=True)
    assert exc.value.code == "bad_input"


def _audit_json(directory: Path, *, clean: bool, host="hx"):
    audits = directory / "audits"
    audits.mkdir(parents=True)
    checks = [{"n": 1, "name": "Runs", "status": "pass", "sentence": "fine"}]
    if not clean:
        checks.append({"n": 6, "name": "Retention", "status": "fail",
                       "sentence": "the host's clean-up period is not set"})
    (audits / f"AUDIT_{host}_2026-03-01.json").write_text(
        json.dumps({"host": host, "generated_ts": "2026-03-01T10:00:00Z", "clean": clean, "checks": checks,
                    "report_md": str(audits / f"AUDIT_{host}_2026-03-01.md")}),
        encoding="utf-8",
    )


def test_archive_audit_failures_become_items_and_clean_audits_a_course_line(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    bad, good_dir = tmp_path / "arc-bad", tmp_path / "arc-good"
    _audit_json(bad, clean=False, host="hx")
    _audit_json(good_dir, clean=True, host="hy")
    (root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n[archive]\ndirs = {toml_list([bad.as_posix(), good_dir.as_posix()])}\n', encoding="utf-8"
    )
    settings = packet_settings(root)
    result = pb.build_packet(settings, "manual", dry_run=True, now=T0)
    entries = result["packet"]["items"]
    assert [e["source"] for e in entries] == ["audit"]
    assert "failed a check (Retention)" in entries[0]["what"] and "clean-up period is not set" in entries[0]["what"]
    assert entries[0]["recommended"] == "a" and entries[0]["est_minutes"] == 2
    assert result["packet"]["course"] == ["Transcript archive (hy): clean audit on 2026-03-01."]


# ------------------------------------------------------------------ weekly gate


def _capture(directory: Path, pct, *, epoch, resets):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "latest.json").write_text(
        json.dumps({"epoch": epoch, "captured_ts": "x", "rate_limits": {"seven_day": {"used_percentage": pct, "resets_at": resets}}}),
        encoding="utf-8",
    )


def test_the_weekly_trigger_builds_only_over_the_threshold_once_a_week(env, tmp_path):
    settings, _ = env
    add(settings, "Q?")
    quota = tmp_path / "quota"
    now_epoch = T0.timestamp()
    build = lambda now, pct=85: pb.build_packet(  # noqa: E731
        settings, "weekly_limit", now=now, when_weekly_pct=pct, quota_dir=str(quota)
    )
    assert "no quota capture" in build(T0)["skipped"]
    _capture(quota, 70, epoch=now_epoch, resets=now_epoch + 86400)
    assert "70%, below 85%" in build(T0)["skipped"]
    _capture(quota, 90, epoch=now_epoch - 2 * 86400, resets=now_epoch + 86400)
    assert "more than a day old" in build(T0)["skipped"]
    _capture(quota, 90, epoch=now_epoch, resets=now_epoch - 10)
    assert "before the last weekly reset" in build(T0)["skipped"]
    _capture(quota, 90, epoch=now_epoch, resets=now_epoch + 86400)
    first = build(T0)
    assert first["built"] is True and first["packet"]["trigger"] == "weekly_limit"
    later = T0 + timedelta(hours=1)
    _capture(quota, 92, epoch=later.timestamp(), resets=now_epoch + 86400)
    unsent = build(later)
    assert "never announced" in unsent["skipped"] and unsent["unsent_packet"] == first["packet"]["packet_id"]
    pb.push_packet(settings, packet_id=unsent["unsent_packet"], now=later)
    assert "already announced" in build(later + timedelta(hours=1))["skipped"]
    week_on = T0 + timedelta(days=6)
    _capture(quota, 90, epoch=week_on.timestamp(), resets=week_on.timestamp() + 86400)
    assert build(week_on)["built"] is True


def test_a_failed_weekly_push_is_retried_by_the_next_hourly_run_exactly_once(tmp_path, monkeypatch, capsys):
    import time

    root = tmp_path / "p"
    root.mkdir()
    stub = tmp_path / "flaky.py"
    stub.write_text(FLAKY_STUB, encoding="utf-8")
    log = tmp_path / "flaky.log"
    down = Path(str(log) + ".down")
    down.write_text("x", encoding="utf-8")
    (root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n[packet]\n'
        f"notify_cmd = {toml_list([sys.executable, stub.as_posix(), log.as_posix()])}\n",
        encoding="utf-8",
    )
    settings = packet_settings(root)
    add(settings, "Q?")
    quota = tmp_path / "quota"
    _capture(quota, 90, epoch=time.time(), resets=time.time() + 86400)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota))

    def hourly():
        code = main(["packet", "build", "--trigger", "weekly_limit", "--when-weekly-pct", "85", "--push",
                     "--program-root", str(root)])
        return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    code, out = hourly()  # the channel is down
    assert code == 0 and out["result"]["built"] is True and out["result"]["push_error"]["code"] == "push_failed"
    assert calls(log) == [] and ps.read_jsonl(settings.sent) == []
    built_files = sorted(settings.built.glob("PACKET_*.json"))
    down.unlink()  # the channel is back
    code, out = hourly()
    assert code == 0 and out["result"]["built"] is False and out["result"]["push"]["packet_id"] == built_files[0].stem
    assert len(calls(log)) == 1
    code, out = hourly()  # a later run inside the five days pushes nothing
    assert "already announced" in out["result"]["skipped"] and len(calls(log)) == 1
    assert sorted(settings.built.glob("PACKET_*.json")) == built_files  # and built nothing new


# ------------------------------------------------------------------------- push


def test_push_sends_title_and_body_as_the_last_two_arguments(env):
    settings, log = env
    add(settings, "Should we keep the archive for ten years?", est_minutes=4)
    add(settings, "Second?", est_minutes=6, now=T0 + timedelta(minutes=1))
    built = pb.build_packet(settings, "session_close", now=T0)
    result = pb.push_packet(settings, now=T0 + timedelta(minutes=5))
    (argv,) = calls(log)
    assert argv[-2:] == [result["title"], result["body"]]
    assert result["title"] == "Decisions for you: 2 (about 10 min)"
    assert result["body"] == (
        "2 decisions need you before the next session (about 10 minutes). "
        "First: Should we keep the archive for ten years?. Read: https://example.invalid/packet."
    )
    sent = ps.read_jsonl(settings.sent)
    assert len(sent) == 1 and sent[0]["packet_id"] == built["packet"]["packet_id"]
    assert sent[0]["trigger"] == "session_close" and sent[0]["reminder"] is False and sent[0]["force"] is False
    assert len(sent[0]["body_sha256"]) == 64


def test_the_body_is_at_most_600_characters_and_the_first_item_is_cut_to_200(env):
    settings, log = env
    add(settings, "W" * 300)
    pb.build_packet(settings, "manual", now=T0)
    result = pb.push_packet(settings, now=T0)
    assert len(result["body"]) <= 600
    first = result["body"].split("First: ")[1].split(". Read:")[0]
    assert len(first) <= 200 and first.endswith("...")


def test_a_very_long_link_still_leaves_the_body_within_600(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    stub = tmp_path / "s.py"
    stub.write_text(STUB, encoding="utf-8")
    log = tmp_path / "log.jsonl"
    (root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n[packet]\n'
        f"notify_cmd = {toml_list([sys.executable, stub.as_posix(), log.as_posix()])}\nlink = \"https://example.invalid/{'x' * 400}\"\n",
        encoding="utf-8",
    )
    settings = packet_settings(root)
    add(settings, "Q" * 250)
    pb.build_packet(settings, "manual", now=T0)
    assert len(pb.push_packet(settings, now=T0)["body"]) <= 600


def test_no_config_secret_reaches_the_message_or_the_envelope(env, capsys):
    settings, log = env
    add(settings, "Q?")
    pb.build_packet(settings, "manual", now=T0)
    result = pb.push_packet(settings, now=T0)
    for element in settings.notify_cmd[1:]:
        assert element not in result["body"] and element not in result["title"]
    assert SECRET not in json.dumps(result) and SECRET not in " ".join(calls(log)[0][-2:])
    assert SECRET not in settings.sent.read_text(encoding="utf-8")


def test_a_second_push_of_one_packet_and_a_second_packet_within_24h_are_refused(env):
    settings, log = env
    add(settings, "Q?")
    pb.build_packet(settings, "manual", now=T0)
    pb.push_packet(settings, now=T0)
    with pytest.raises(PacketError) as exc:
        pb.push_packet(settings, now=T0 + timedelta(hours=30))
    assert exc.value.code == "already_pushed"
    later = T0 + timedelta(hours=2)
    pb.build_packet(settings, "manual", now=later)
    with pytest.raises(PacketError) as exc:
        pb.push_packet(settings, now=later)
    assert exc.value.code == "push_limit_24h" and exc.value.details["next_allowed_ts"] == "2026-03-03T09:00:00Z"
    assert len(calls(log)) == 1
    pb.push_packet(settings, now=T0 + timedelta(hours=25))  # the next day it goes out
    assert len(calls(log)) == 2


def test_force_overrides_and_is_recorded(env):
    settings, log = env
    add(settings, "Q?")
    pb.build_packet(settings, "manual", now=T0)
    pb.push_packet(settings, now=T0)
    result = pb.push_packet(settings, force=True, now=T0 + timedelta(minutes=1))
    assert result["forced"] is True and len(calls(log)) == 2
    assert [r["force"] for r in ps.read_jsonl(settings.sent)] == [False, True]


def test_push_needs_a_notifier_a_packet_and_something_to_say(tmp_path, env):
    settings, _ = env
    with pytest.raises(PacketError) as exc:
        pb.push_packet(settings)
    assert exc.value.code == "no_packet"
    pb.build_packet(settings, "manual", now=T0)  # nothing open: an empty packet
    with pytest.raises(PacketError) as exc:
        pb.push_packet(settings)
    assert exc.value.code == "empty_packet"
    bare = tmp_path / "bare"
    bare.mkdir()
    s2 = packet_settings(bare)
    add(s2, "Q?")
    pb.build_packet(s2, "manual", now=T0)
    with pytest.raises(PacketError) as exc:
        pb.push_packet(s2)
    assert exc.value.code == "no_notify_cmd"


def test_a_failing_notifier_records_nothing(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    stub = tmp_path / "fail.py"
    stub.write_text(FAILING_STUB, encoding="utf-8")
    (root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n[packet]\nnotify_cmd = {toml_list([sys.executable, stub.as_posix()])}\n', encoding="utf-8"
    )
    settings = packet_settings(root)
    add(settings, "Q?")
    pb.build_packet(settings, "manual", now=T0)
    with pytest.raises(PacketError) as exc:
        pb.push_packet(settings, now=T0)
    assert exc.value.code == "push_failed" and "channel down" in exc.value.message
    assert ps.read_jsonl(settings.sent) == []
    missing = tmp_path / "m"
    missing.mkdir()
    (missing / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n[packet]\nnotify_cmd = ["no-such-sender-anywhere"]\n', encoding="utf-8"
    )
    s2 = packet_settings(missing)
    add(s2, "Q?")
    pb.build_packet(s2, "manual", now=T0)
    with pytest.raises(PacketError) as exc:
        pb.push_packet(s2, now=T0)
    assert exc.value.code == "push_failed"


def test_an_ssh_notifier_gets_its_arguments_quoted_for_the_remote_shell():
    argv = pb.notify_argv(["ssh", "somehost", "~/sender.sh"], "Title: it's 3", "Body with spaces; and $stuff")
    assert argv[:3] == ["ssh", "somehost", "~/sender.sh"]
    assert argv[3] == "'Title: it'\"'\"'s 3'" and argv[4] == "'Body with spaces; and $stuff'"
    plain = pb.notify_argv(["/usr/bin/sender", "--x"], "T", "B c")
    assert plain == ["/usr/bin/sender", "--x", "T", "B c"]


def test_cli_build_push_and_the_build_and_push_flag(env, capsys):
    settings, log = env
    root = str(settings.program_root)
    add(settings, "Q?")
    code = main(["packet", "build", "--trigger", "session_close", "--program-root", root, "--push"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["result"]["push"]["title"].startswith("Decisions for you: 1")
    assert len(calls(log)) == 1
    code = main(["packet", "push", "--program-root", root])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and out["error"]["code"] == "already_pushed"
    code = main(["packet", "push", "--program-root", root, "--force"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["result"]["forced"] is True


def test_cli_build_push_flag_reports_a_refused_push_as_a_warning(env, capsys):
    settings, _ = env
    root = str(settings.program_root)
    add(settings, "Q?")
    pb.build_packet(settings, "manual", now=T0)
    pb.push_packet(settings, now=datetime.now(timezone.utc))
    code = main(["packet", "build", "--trigger", "manual", "--program-root", root, "--push"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["result"]["push_error"]["code"] == "push_limit_24h"
    assert any(w["code"] == "push_limit_24h" for w in out["warnings"])


# ----------------------------------------------------------------------- remind


def test_remind_waits_then_sends_once_and_never_twice(env):
    settings, log = env
    add(settings, "Still open?")
    add(settings, "Will be answered?")
    pb.build_packet(settings, "manual", now=T0)
    pb.push_packet(settings, now=T0)
    assert calls(log) and len(calls(log)) == 1
    early = pb.remind(settings, now=T0 + timedelta(days=2, hours=23))
    assert early["reminded"] is False and "waits 3" in early["reason"]
    row = ps.list_items(settings)["open"][1]
    ps.answer_item(settings, row["id"], "a")
    due = pb.remind(settings, now=T0 + timedelta(days=3))
    assert due["reminded"] is True and due["open_items"] == 1
    assert due["title"] == "Reminder: 1 decision still open (about 3 min)"
    assert due["body"].startswith("1 decision from the last packet is still open (about 3 minutes). First: Still open?.")
    assert len(calls(log)) == 2
    again = pb.remind(settings, now=T0 + timedelta(days=5))
    assert again["reminded"] is False and "already reminded" in again["reason"]
    assert len(calls(log)) == 2
    rows = ps.read_jsonl(settings.sent)
    assert [r["reminder"] for r in rows] == [False, True]


def test_remind_does_nothing_without_a_push_or_without_open_items(env):
    settings, log = env
    assert pb.remind(settings, now=T0)["reason"] == "no packet has been pushed yet"
    row = add(settings, "Q?")
    pb.build_packet(settings, "manual", now=T0)
    pb.push_packet(settings, now=T0)
    ps.answer_item(settings, row["id"], "b")
    out = pb.remind(settings, now=T0 + timedelta(days=10))
    assert out["reminded"] is False and "still open" in out["reason"]
    assert len(calls(log)) == 1


def test_remind_after_days_is_configurable(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    stub = tmp_path / "s.py"
    stub.write_text(STUB, encoding="utf-8")
    log = tmp_path / "log.jsonl"
    (root / "trialerror.toml").write_text(
        f'[program]\nid = "demo"\n[packet]\nremind_after_days = 1\nnotify_cmd = {toml_list([sys.executable, stub.as_posix(), log.as_posix()])}\n',
        encoding="utf-8",
    )
    settings = packet_settings(root)
    add(settings, "Q?")
    pb.build_packet(settings, "manual", now=T0)
    pb.push_packet(settings, now=T0)
    assert pb.remind(settings, now=T0 + timedelta(days=1))["reminded"] is True


def test_cli_remind(env, capsys):
    settings, _ = env
    code = main(["packet", "remind", "--program-root", str(settings.program_root)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["result"]["reminded"] is False


def test_a_built_packet_appears_whole_or_not_at_all(env, monkeypatch):
    settings, _ = env
    add(settings, "Q?")
    real_replace = os.replace
    seen = []

    def watching_replace(src, dst):
        # at the moment a packet file is published, it is complete and its final name is still free
        dst = Path(dst)
        seen.append((dst.name, dst.exists(), Path(src).read_text(encoding="utf-8")))
        real_replace(src, dst)

    monkeypatch.setattr(pb.os, "replace", watching_replace)
    result = pb.build_packet(settings, "manual", now=T0)
    monkeypatch.undo()
    names = [n for n, _, _ in seen]
    assert names == [Path(result["path_md"]).name, Path(result["path_json"]).name]  # .json is published last
    assert all(existed is False for _, existed, _ in seen)
    assert json.loads(seen[1][2])["packet_id"] == result["packet"]["packet_id"]
    assert seen[0][2].startswith("# Decisions for you")
    assert [p.name for p in settings.built.iterdir() if p.name.startswith(".")] == []  # no temp files left
