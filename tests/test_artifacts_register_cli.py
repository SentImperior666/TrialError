"""``trialerror artifact register --with-deviation / --as-failed`` and the
``disposition`` field of ``artifact show`` / ``artifact list``."""

from __future__ import annotations

import codecs
import io
import json
from contextlib import redirect_stdout

import pytest

from tests.test_artifacts_register_dispositions import CHECK, DEC, DEVIATION_TEXT, FAILURE_TEXT, World
from trialerror.artifacts.gates import get_gate
from trialerror.cli import main


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


@pytest.fixture()
def world(store, program_root):
    return World(store, program_root)


def register(world, aid, *extra):
    return _run_cli(
        ["artifact", "register", "--program-root", str(world.root), "--id", aid, "--by-launch", world.launch, *extra]
    )


DEV = f"{CHECK}=no rooms ran before the decision@{DEVIATION_TEXT}"


def test_with_deviation_through_the_cli(world):
    aid, gid = world.gated()
    rc, env = register(world, aid, "--with-deviation", "--deviation", DEV, "--decided-by", DEC)
    assert rc == 0, env
    assert env["result"]["status"] == "registered"
    assert env["result"]["disposition"] == "registered_with_deviation"
    gate = get_gate(world.store, gid)
    assert gate["disposition"] == "deviation_disclosed"
    assert json.loads(gate["deviation_ref"]) == [
        {"check": CHECK, "reason": "no rooms ran before the decision", "report_ref": DEVIATION_TEXT}
    ]


def test_as_failed_through_the_cli(world):
    aid, gid = world.failed()
    rc, env = register(world, aid, "--as-failed", "--failure-ref", FAILURE_TEXT, "--decided-by", DEC)
    assert rc == 0, env
    assert env["result"]["disposition"] == "registered_failed"
    assert get_gate(world.store, gid)["disposition"] == "failure_registered"


def test_refusals_carry_the_registration_refused_code(world):
    aid, _gid = world.gated()
    rc, env = register(world, aid, "--with-deviation", "--deviation", f"{CHECK}=r@not in the file", "--decided-by", DEC)
    assert rc == 1 and env["error"]["code"] == "registration_refused"
    assert "does not contain" in env["error"]["message"]
    rc, env = register(world, aid, "--as-failed", "--failure-ref", FAILURE_TEXT, "--decided-by", DEC)
    assert rc == 1 and env["error"]["code"] == "registration_refused"  # a gated gate is not a failed one


@pytest.mark.parametrize(
    "extra",
    [
        ["--with-deviation", "--as-failed"],  # mutually exclusive (argparse refuses)
        ["--with-deviation", "--decided-by", DEC],  # no deviation
        ["--with-deviation", "--deviation", DEV],  # no decision
        ["--with-deviation", "--deviation", "no separators", "--decided-by", DEC],
        ["--with-deviation", "--deviation", DEV, "--decided-by", DEC, "--failure-ref", "x"],
        ["--as-failed", "--decided-by", DEC],  # no failure ref
        ["--as-failed", "--failure-ref", "x"],  # no decision
        ["--as-failed", "--failure-ref", "x", "--decided-by", DEC, "--deviation", DEV],
        ["--deviation", DEV],  # a mode flag's argument with no mode
        ["--decided-by", DEC],
    ],
)
def test_flag_misuse(world, extra):
    aid, gid = world.gated()
    if extra[:2] == ["--with-deviation", "--as-failed"]:
        with pytest.raises(SystemExit):
            register(world, aid, *extra)
        return
    rc, env = register(world, aid, *extra)
    assert rc == 1 and env["error"]["code"] == "bad_input", env
    assert get_gate(world.store, gid)["state"] == "gated"


def test_no_mode_is_the_normal_path(world):
    aid, _gid = world.gated()
    rc, env = register(world, aid)
    assert rc == 1 and env["error"]["code"] == "registration_refused"  # a gated gate is not union_applied


def test_show_and_list_carry_disposition(world):
    aid, _gid = world.gated()
    register(world, aid, "--with-deviation", "--deviation", DEV, "--decided-by", DEC)
    other, _ = world.gated()
    rc, env = _run_cli(["artifact", "show", "--program-root", str(world.root), "--id", aid])
    assert rc == 0 and env["result"]["disposition"] == "registered_with_deviation"
    rc, env = _run_cli(["artifact", "show", "--program-root", str(world.root), "--id", other])
    assert env["result"]["disposition"] is None
    rc, env = _run_cli(["artifact", "list", "--program-root", str(world.root)])
    by_id = {a["artifact_id"]: a for a in env["result"]["artifacts"]}
    assert by_id[aid]["disposition"] == "registered_with_deviation" and by_id[other]["disposition"] is None


def test_a_second_at_sign_is_refused_as_ambiguous(world):
    aid, gid = world.gated()
    rc, env = register(
        world, aid, "--with-deviation", "--deviation", f"{CHECK}=mail a@b.example@{DEVIATION_TEXT}", "--decided-by", DEC
    )
    assert rc == 1 and env["error"]["code"] == "bad_input"
    assert "@" in env["error"]["message"] and "--deviations-file" in env["error"]["message"]
    assert get_gate(world.store, gid)["state"] == "gated"


def test_deviations_file_carries_a_reference_that_needs_an_at_sign(world, tmp_path):
    ref = "deviation 22: mail a@b escrow note"
    aid, gid = world.gated(text=ref + "\n")
    path = tmp_path / "dev.json"
    path.write_text(json.dumps([{"check": CHECK, "reason": "no rooms ran", "report_ref": ref}]), encoding="utf-8")
    rc, env = register(world, aid, "--with-deviation", "--deviations-file", str(path), "--decided-by", DEC)
    assert rc == 0, env
    assert json.loads(get_gate(world.store, gid)["deviation_ref"])[0]["report_ref"] == ref


def test_deviations_file_and_deviation_are_exclusive(world, tmp_path):
    aid, _gid = world.gated()
    path = tmp_path / "dev.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        register(world, aid, "--with-deviation", "--deviation", DEV, "--deviations-file", str(path), "--decided-by", DEC)


@pytest.mark.parametrize(
    "content",
    ["not json", '{"check": "x"}', '[{"check": "x", "reason": "r"}]', '["a"]', "[]"],
)
def test_bad_deviations_file_is_bad_input(world, tmp_path, content):
    aid, gid = world.gated()
    path = tmp_path / "dev.json"
    path.write_text(content, encoding="utf-8")
    rc, env = register(world, aid, "--with-deviation", "--deviations-file", str(path), "--decided-by", DEC)
    assert rc == 1 and env["error"]["code"] == "bad_input", env
    rc, env = register(world, aid, "--with-deviation", "--deviations-file", str(tmp_path / "nope.json"), "--decided-by", DEC)
    assert rc == 1 and env["error"]["code"] == "bad_input"
    assert get_gate(world.store, gid)["state"] == "gated"


def test_deviations_file_needs_the_mode(world, tmp_path):
    aid, _gid = world.gated()
    path = tmp_path / "dev.json"
    path.write_text("[]", encoding="utf-8")
    rc, env = register(world, aid, "--deviations-file", str(path))
    assert rc == 1 and env["error"]["code"] == "bad_input"
    rc, env = register(world, aid, "--as-failed", "--failure-ref", "x", "--decided-by", DEC, "--deviations-file", str(path))
    assert rc == 1 and env["error"]["code"] == "bad_input"


def test_deviations_file_with_a_utf8_bom_is_read(world, tmp_path):
    aid, gid = world.gated()
    path = tmp_path / "bom.json"
    body = json.dumps([{"check": CHECK, "reason": "no rooms ran", "report_ref": DEVIATION_TEXT}])
    path.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))
    rc, env = register(world, aid, "--with-deviation", "--deviations-file", str(path), "--decided-by", DEC)
    assert rc == 0, env
