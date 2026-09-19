"""``trialerror law`` CLI surface: argv parsing, envelope shaping, and
``--program-root`` resolution over ``trialerror.law.service``."""

from __future__ import annotations

import json

from trialerror.cli import main


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_law_no_action_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(["law", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "no_action"


def test_law_program_root_not_found(tmp_path, platform_root, monkeypatch, capsys):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc, env = _call(["law", "append", "--summary", "x"], capsys)
    assert rc == 1
    assert env["error"]["code"] == "program_root_not_found"


def test_law_append_ok_envelope(program_root, platform_root, capsys):
    rc, env = _call(
        ["law", "append", "--program-root", str(program_root), "--summary", "first ruling"], capsys
    )
    assert rc == 0
    assert env["ok"] is True
    assert env["result"]["ruling_id"] == "C-0001"
    assert env["result"]["digest_version"] == "v1"
    assert env["result"]["pin"].startswith("v1@")
    assert env["nextActions"][0]["argv"][:3] == ["trialerror", "law", "verify"]


def test_law_append_respects_configured_law_digest_path(program_root, platform_root, capsys):
    """the import-design notes (internal, not in this export) Sec 5 knob #2: the CLI actually loads
    trialerror.toml and threads it through -- not just trialerror.law.service's own
    library-level ``config`` parameter."""
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n\n[paths]\nlaw_digest_path = "governance/DIGEST.md"\n', encoding="utf-8"
    )
    rc, env = _call(
        ["law", "append", "--program-root", str(program_root), "--summary", "relocated via CLI"], capsys
    )
    assert rc == 0
    assert env["result"]["rendered_path"] == "governance/DIGEST.md"
    assert (program_root / "governance" / "DIGEST.md").is_file()
    assert not (program_root / "law").exists()


def test_law_append_with_standing_clauses_and_domains(program_root, platform_root, capsys):
    rc, env = _call(
        [
            "law",
            "append",
            "--program-root",
            str(program_root),
            "--summary",
            "GPU only",
            "--standing-clause",
            "never CPU OCR",
            "--standing-clause",
            "batch chunked",
            "--domain",
            "ingest",
        ],
        capsys,
    )
    assert rc == 0
    assert json.loads(env["result"]["ruling"]["standing_clauses"]) == ["never CPU OCR", "batch chunked"]
    assert json.loads(env["result"]["ruling"]["domains"]) == ["ingest"]


def test_law_append_refuses_bad_supersedes(program_root, platform_root, capsys):
    rc, env = _call(
        ["law", "append", "--program-root", str(program_root), "--summary", "x", "--supersedes", "C-9999"],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "append_refused"


def test_law_lookup(program_root, platform_root, capsys):
    _call(["law", "append", "--program-root", str(program_root), "--summary", "alpha", "--domain", "budget"], capsys)
    _call(["law", "append", "--program-root", str(program_root), "--summary", "beta", "--domain", "law"], capsys)

    rc, env = _call(["law", "lookup", "--program-root", str(program_root), "--domain", "law"], capsys)
    assert rc == 0
    assert env["result"]["count"] == 1
    assert env["result"]["rulings"][0]["summary"] == "beta"


def test_law_digest_before_any_append_is_an_error(program_root, platform_root, capsys):
    rc, env = _call(["law", "digest", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "no_digest_yet"


def test_law_digest_after_append(program_root, platform_root, capsys):
    _call(["law", "append", "--program-root", str(program_root), "--summary", "x"], capsys)
    rc, env = _call(["law", "digest", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert env["result"]["version"] == "v1"


def test_law_digest_render_flag(program_root, platform_root, capsys):
    _call(["law", "append", "--program-root", str(program_root), "--summary", "x"], capsys)
    rc, env = _call(["law", "digest", "--program-root", str(program_root), "--render"], capsys)
    assert rc == 0
    assert env["result"]["matches_stored_hash"] is True


def test_law_verify_ok_and_stale(program_root, platform_root, capsys):
    _, env1 = _call(["law", "append", "--program-root", str(program_root), "--summary", "one"], capsys)
    pin1 = env1["result"]["pin"]

    rc_ok, env_ok = _call(["law", "verify", "--program-root", str(program_root), "--pin", pin1], capsys)
    assert rc_ok == 0
    assert env_ok["ok"] is True

    _call(["law", "append", "--program-root", str(program_root), "--summary", "two"], capsys)

    rc_stale, env_stale = _call(["law", "verify", "--program-root", str(program_root), "--pin", pin1], capsys)
    assert rc_stale == 1
    assert env_stale["error"]["code"] == "pin_invalid"
    assert env_stale["error"]["details"]["pin_stale"] is True


def test_law_diff_foreign(program_root, platform_root, capsys):
    _, env1 = _call(["law", "append", "--program-root", str(program_root), "--summary", "one"], capsys)
    pin1 = env1["result"]["pin"]
    _call(["law", "append", "--program-root", str(program_root), "--summary", "two"], capsys)

    rc, env = _call(["law", "diff-foreign", "--program-root", str(program_root), "--pin", pin1], capsys)
    assert rc == 0
    assert env["result"]["count"] == 1
    assert env["result"]["foreign_rulings"][0]["summary"] == "two"


def test_law_diff_foreign_bad_pin(program_root, platform_root, capsys):
    _call(["law", "append", "--program-root", str(program_root), "--summary", "one"], capsys)
    rc, env = _call(["law", "diff-foreign", "--program-root", str(program_root), "--pin", "v999@2026-01-01"], capsys)
    assert rc == 1
    assert env["error"]["code"] == "bad_pin"
