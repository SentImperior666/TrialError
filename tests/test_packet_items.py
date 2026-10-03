"""The decision packet's items: validation, the plain-words lint, answers,
withdrawal, and the ``[packet]`` config accessor. Temporary program folders only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trialerror.cli import main
from trialerror.packet import store as ps
from trialerror.packet.store import PacketError, packet_settings
from trialerror.util.config import ConfigError, load_config

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def good(**over):
    item = {
        "what": "Should the transcript archive keep every file for ten years?",
        "why": "Without a decision the host deletes old transcripts, and the only copy is lost.",
        "options": [
            {"key": "a", "label": "Keep everything", "consequence": "The archive grows; nothing is ever lost."},
            {"key": "b", "label": "Keep one year", "consequence": "Older transcripts are gone for good."},
        ],
        "recommended": "a",
        "if_undecided": "Everything is kept.",
        "needed_by": "next-session",
        "asked_by": "the custodian",
    }
    item.update(over)
    return item


@pytest.fixture()
def prog(tmp_path) -> Path:
    root = tmp_path / "prog"
    root.mkdir()
    return root


@pytest.fixture()
def settings(prog):
    return packet_settings(prog)


def cli(capsys, *argv):
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize("field", ["what", "why", "options", "recommended", "if_undecided", "needed_by"])
def test_each_required_field_missing_is_bad_input(settings, field):
    raw = good()
    del raw[field]
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, raw)
    assert exc.value.code == "bad_input"
    assert field in exc.value.message
    assert ps.read_jsonl(settings.pending) == []


def test_fewer_than_two_options_and_a_missing_consequence_are_refused(settings):
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good(options=[{"key": "a", "label": "Only one", "consequence": "x"}]))
    assert exc.value.code == "bad_input" and "two options" in exc.value.message
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good(options=[{"key": "a", "label": "One"}, {"key": "b", "label": "Two", "consequence": "y"}]))
    assert exc.value.code == "bad_input" and "consequence" in exc.value.message


def test_recommended_must_be_one_of_the_option_keys(settings):
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good(recommended="z"))
    assert exc.value.code == "bad_input" and "not one of the option keys" in exc.value.message


def test_length_limits_and_needed_by_format(settings):
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good(what="w" * 301))
    assert "300" in exc.value.message
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good(why="w" * 501))
    assert "500" in exc.value.message
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good(needed_by="soon"))
    assert "needed_by" in exc.value.message
    item, _ = ps.add_item(settings, good(needed_by="2026-04-01"))
    assert item["needed_by"] == "2026-04-01"


def test_a_valid_item_gets_an_id_a_status_and_defaults(settings):
    item, warnings = ps.add_item(settings, good(), now=T0)
    assert item["id"].startswith("PKT-") and item["status"] == "open"
    assert item["created_ts"] == "2026-03-02T09:00:00Z"
    assert item["priority"] == "normal" and item["est_minutes"] == 3 and item["refs"] == []
    assert warnings == []
    assert ps.read_jsonl(settings.pending) == [item]


# ------------------------------------------------------------------------ lint


def test_lint_flags_an_unexplained_id_hex_run_and_path():
    """L10 part C (design §4 item 3): the id pattern now catches a
    ULID-shaped id -- ``ROOM-01M2XAX6268A0YE6WMR2ABETFY``, the exact shape
    of the operator's own complaint -- which the old ``\\b[A-Z]{2,}-\\d+\\b``
    pattern missed entirely."""
    item = good(
        what="Close ROOM-01M2XAX6268A0YE6WMR2ABETFY?",
        why="See a1b2c3d4e5 and docs/plan_v2.md for the background.",
    )
    text = " | ".join(ps.lint_item(item))
    assert "'ROOM-01M2XAX6268A0YE6WMR2ABETFY'" in text
    assert "'a1b2c3d4e5'" in text and "'docs/plan_v2.md'" in text


def test_the_old_generic_ticket_style_token_is_no_longer_flagged():
    """L10 part C (design §4 item 3): the new pattern replaces the old
    generic ``[A-Z]{2,}-\\d+`` match with ULID ids plus the two named legacy
    styles (``CR-\\d+``, ``C-\\d{3,}``) -- a plain ticket-style token like
    ``TE-104`` (never one of this harness's own id shapes) is not an id the
    lint needs to explain any more."""
    assert ps.lint_item(good(what="Merge TE-104 into the plan?")) == []


def test_a_ref_label_that_explains_the_token_silences_the_lint():
    item = good(
        what="Close ROOM-01M2XAX6268A0YE6WMR2ABETFY?",
        refs=[{"label": "ROOM-01M2XAX6268A0YE6WMR2ABETFY: the retention proposal", "ref": "notes/retention.md"}],
    )
    assert ps.lint_item(item) == []


def test_lint_flags_a_long_sentence_and_an_empty_consequence():
    long_sentence = " ".join(["word"] * 41) + "."
    item = good(why=long_sentence, options=[
        {"key": "a", "label": "Do it", "consequence": ""},
        {"key": "b", "label": "Skip it", "consequence": "Nothing changes."},
    ])
    text = " | ".join(ps.lint_item(item))
    assert "41 words" in text and "option 'a' has an empty consequence" in text
    assert ps.lint_item(good(why=" ".join(["word"] * 40) + ".")) == []


def test_lint_warns_but_the_item_is_stored(settings):
    item, warnings = ps.add_item(settings, good(what="Close ROOM-01M2XAX6268A0YE6WMR2ABETFY?"))
    assert warnings and ps.read_jsonl(settings.pending)[0]["id"] == item["id"]


def test_strict_turns_the_warnings_into_a_refusal_and_stores_nothing(settings):
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good(what="Close ROOM-01M2XAX6268A0YE6WMR2ABETFY?"), strict=True)
    assert exc.value.code == "lint_refused" and exc.value.details["warnings"]
    assert ps.read_jsonl(settings.pending) == []


# ------------------------------------------------------------------- CLI: add


def test_cli_add_from_flags_and_from_a_file(prog, capsys, tmp_path):
    code, env = cli(
        capsys, "packet", "add", "--program-root", str(prog),
        "--what", "Pick a colour for the report cover?", "--why", "The printer needs it by Friday.",
        "--option", "a=Blue::Reads as calm.", "--option", "b=Green::Reads as fresh.",
        "--recommend", "a", "--if-undecided", "Blue is used.", "--needed-by", "2026-04-01",
        "--asked-by", "the layout unit", "--est-minutes", "1.5", "--priority", "low",
        "--ref", "the mock-up::designs/cover.png",
    )
    assert code == 0 and env["ok"]
    item = env["result"]["item"]
    assert item["options"][1] == {"key": "b", "label": "Green", "consequence": "Reads as fresh."}
    assert item["est_minutes"] == 1.5 and item["priority"] == "low"
    assert item["refs"] == [{"label": "the mock-up", "ref": "designs/cover.png"}]

    src = tmp_path / "item.json"
    src.write_text(json.dumps(good()), encoding="utf-8")
    code, env = cli(capsys, "packet", "add", "--program-root", str(prog), "--file", str(src))
    assert code == 0 and env["result"]["item"]["asked_by"] == "the custodian"
    code, env = cli(capsys, "packet", "list", "--program-root", str(prog))
    assert len(env["result"]["open"]) == 2


def test_cli_add_reports_lint_as_warnings_and_refuses_under_strict(prog, capsys):
    argv = ["packet", "add", "--program-root", str(prog), "--what", "Close ROOM-01M2XAX6268A0YE6WMR2ABETFY?", "--why", "Because.",
            "--option", "a=Yes::It merges.", "--option", "b=No::It stays.", "--recommend", "a",
            "--if-undecided", "Nothing.", "--needed-by", "next-session"]
    code, env = cli(capsys, *argv)
    assert code == 0 and env["warnings"][0]["code"] == "plain_words"
    code, env = cli(capsys, *argv, "--strict")
    assert code == 1 and env["error"]["code"] == "lint_refused"


def test_cli_add_option_without_a_consequence_is_bad_input(prog, capsys):
    code, env = cli(
        capsys, "packet", "add", "--program-root", str(prog), "--what", "x?", "--why", "y.",
        "--option", "a=Yes", "--option", "b=No::Stays.", "--recommend", "a", "--if-undecided", "n",
        "--needed-by", "next-session",
    )
    assert code == 1 and env["error"]["code"] == "bad_input"


# ---------------------------------------------------------- answer / list / withdraw


def test_answer_and_list_answered_since_round_trip(settings):
    item, _ = ps.add_item(settings, good(), now=T0)
    other, _ = ps.add_item(settings, good(what="Second question?"), now=T0)
    ans = ps.answer_item(settings, item["id"], "b", note="one year is plenty", by="the operator", now=T0 + timedelta(hours=5))
    assert ans == {"item_id": item["id"], "choice": "b", "note": "one year is plenty",
                   "decided_by": "the operator", "decided_ts": "2026-03-02T14:00:00Z"}
    assert [r["id"] for r in ps.list_items(settings)["open"]] == [other["id"]]
    since = ps.list_items(settings, open_only=False, since="2026-03-02T12:00:00Z")
    assert "open" not in since and len(since["answered"]) == 1
    assert since["answered"][0]["choice_label"] == "Keep one year" and since["answered"][0]["what"] == item["what"]
    assert since["answered"][0]["asked_by"] == item["asked_by"]
    # L10 part C item 5: answered_since(program_root, ts) is the same pure
    # read `packet list --answered-since` now goes through.
    assert ps.answered_since(settings.program_root, "2026-03-02T12:00:00Z") == since["answered"]
    assert ps.list_items(settings, open_only=False, since="2026-03-02T15:00:00Z")["answered"] == []
    assert ps.read_jsonl(settings.answers) == [ans]
    assert [r["status"] for r in ps.read_jsonl(settings.pending)] == ["answered", "open"]


def test_answer_refuses_a_wrong_key_an_unknown_item_and_a_second_answer(settings):
    item, _ = ps.add_item(settings, good())
    with pytest.raises(PacketError) as exc:
        ps.answer_item(settings, item["id"], "z")
    assert exc.value.code == "bad_choice"
    with pytest.raises(PacketError) as exc:
        ps.answer_item(settings, "PKT-nope", "a")
    assert exc.value.code == "not_found"
    ps.answer_item(settings, item["id"], "a")
    with pytest.raises(PacketError) as exc:
        ps.answer_item(settings, item["id"], "a")
    assert exc.value.code == "not_open"


def test_withdraw_needs_a_reason_and_removes_the_item_from_open(settings):
    item, _ = ps.add_item(settings, good())
    with pytest.raises(PacketError):
        ps.withdraw_item(settings, item["id"], "  ")
    row = ps.withdraw_item(settings, item["id"], "no longer relevant", now=T0)
    assert row["status"] == "withdrawn" and row["withdrawn_reason"] == "no longer relevant"
    assert ps.list_items(settings)["open"] == []
    with pytest.raises(PacketError) as exc:
        ps.answer_item(settings, item["id"], "a")
    assert exc.value.code == "not_open"


def test_cli_answer_list_and_withdraw(prog, capsys):
    settings = packet_settings(prog)
    item, _ = ps.add_item(settings, good())
    second, _ = ps.add_item(settings, good(what="Another question?"))
    code, env = cli(capsys, "packet", "answer", item["id"], "--choice", "a", "--note", "yes", "--by", "me",
                    "--program-root", str(prog))
    assert code == 0 and env["result"]["decided_by"] == "me"
    code, env = cli(capsys, "packet", "list", "--answered-since", "2000-01-01T00:00:00Z", "--program-root", str(prog))
    assert [a["item_id"] for a in env["result"]["answered"]] == [item["id"]]
    code, env = cli(capsys, "packet", "withdraw", second["id"], "--reason", "dropped", "--program-root", str(prog))
    assert code == 0 and env["result"]["item"]["status"] == "withdrawn"
    code, env = cli(capsys, "packet", "answer", "PKT-none", "--choice", "a", "--program-root", str(prog))
    assert code == 1 and env["error"]["code"] == "not_found"


def test_a_corrupt_pending_file_is_reported_not_overwritten(settings):
    settings.dir.mkdir(parents=True)
    settings.pending.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(PacketError) as exc:
        ps.add_item(settings, good())
    assert exc.value.code == "corrupt_file"
    assert settings.pending.read_text(encoding="utf-8") == "{not json\n"


# ----------------------------------------------------------------------- config


def test_program_config_packet_accessor_defaults_to_an_empty_table(prog):
    (prog / "trialerror.toml").write_text('[program]\nid = "demo"\n', encoding="utf-8")
    assert load_config(prog / "trialerror.toml").packet == {}
    s = packet_settings(prog)
    assert s.dir == prog / "packet" and s.max_minutes == 30 and s.remind_after_days == 3
    assert s.notify_cmd is None and s.link is None and s.course_file is None and s.archive_dirs == []
    assert s.pending == prog / "packet" / "pending.jsonl" and s.built == prog / "packet" / "built"


def test_program_config_packet_accessor_reads_the_table(prog):
    (prog / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n[packet]\ndir = "ops/packet"\nmax_minutes = 20\nremind_after_days = 5\n'
        'notify_cmd = ["sender", "--flag"]\nlink = "https://example.invalid/p"\ncourse_file = "course.txt"\n'
        '[archive]\ndirs = ["arc-a"]\n',
        encoding="utf-8",
    )
    cfg = load_config(prog / "trialerror.toml")
    assert cfg.packet["max_minutes"] == 20
    s = packet_settings(prog)
    assert s.dir == prog / "ops" / "packet" and s.max_minutes == 20 and s.remind_after_days == 5
    assert s.notify_cmd == ["sender", "--flag"] and s.link == "https://example.invalid/p"
    assert s.course_file == prog / "course.txt" and s.archive_dirs == [prog / "arc-a"]


@pytest.mark.parametrize(
    "body",
    ['max_minutes = 0', 'max_minutes = "thirty"', 'notify_cmd = "sender"', 'notify_cmd = []', 'remind_after_days = -1'],
)
def test_a_bad_packet_setting_is_a_config_error(prog, body):
    (prog / "trialerror.toml").write_text(f'[program]\nid = "demo"\n[packet]\n{body}\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        packet_settings(prog)


def test_cli_reports_a_bad_config_and_a_missing_program(prog, capsys, tmp_path, monkeypatch):
    (prog / "trialerror.toml").write_text('[program]\nid = "demo"\n[packet]\nmax_minutes = 0\n', encoding="utf-8")
    code, env = cli(capsys, "packet", "list", "--program-root", str(prog))
    assert code == 1 and env["error"]["code"] == "bad_config"
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    code, env = cli(capsys, "packet", "list")
    assert code == 1 and env["error"]["code"] == "program_root_not_found"


@pytest.mark.parametrize("name", ["notify.cmd", "notify.BAT"])
def test_a_batch_file_notifier_is_refused_but_an_interpreter_call_is_not(prog, capsys, name):
    stub = prog / name
    stub.write_text("@echo off\r\n", encoding="utf-8")
    py_stub = prog / "notify.py"
    py_stub.write_text("import sys\n", encoding="utf-8")
    import sys

    def write(cmd):
        (prog / "trialerror.toml").write_text(
            '[program]\nid = "demo"\n[packet]\nnotify_cmd = [' + ", ".join(json.dumps(c) for c in cmd) + "]\n",
            encoding="utf-8",
        )

    write([stub.as_posix(), "--flag"])
    with pytest.raises(ConfigError) as exc:
        packet_settings(prog)
    assert "interpreter" in str(exc.value)
    code, env = cli(capsys, "packet", "list", "--program-root", str(prog))
    assert code == 1 and env["error"]["code"] == "bad_config"
    write([sys.executable.replace("\\", "/"), py_stub.as_posix()])
    assert packet_settings(prog).notify_cmd[1] == py_stub.as_posix()
