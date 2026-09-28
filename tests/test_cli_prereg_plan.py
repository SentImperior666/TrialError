"""``trialerror prereg commit --plan-suite ...`` and ``prereg check``, end to end
through ``trialerror.cli.main``."""

from __future__ import annotations

import codecs
import json
from pathlib import Path

import pytest

from trialerror.cli import main
from trialerror.eval.gate_suites import AIIF_MODEL_FLOORS

FIXTURE = Path(__file__).parent / "fixtures" / "plan_check" / "round0_like_params.json"
ROUND = "R-TEST-0"


@pytest.fixture()
def full_models(program_root):
    lines = ['[program]', 'id = "plan-test"', '', '[models]']
    lines += [f'{purpose} = "top"' for purpose in AIIF_MODEL_FLOORS]
    (program_root / "trialerror.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def call(argv, program_root, platform_root, capsys):
    rc = main([*argv, "--program-root", str(program_root), "--platform-root", str(platform_root)])
    return rc, json.loads(capsys.readouterr().out.strip())


def params_file(tmp_path, **changes) -> Path:
    params = json.loads(FIXTURE.read_text(encoding="utf-8"))
    params.update(changes)
    path = tmp_path / "params.json"
    path.write_text(json.dumps(params), encoding="utf-8")
    return path


def escrow_files(platform_root):
    return sorted((platform_root / "escrow").rglob("*.json")) if (platform_root / "escrow").exists() else []


def n_prereg_rows(program_root, platform_root):
    from trialerror.stores.store import open_store

    store = open_store(program_root, platform_root=platform_root)
    try:
        return store.ops.execute("SELECT COUNT(*) FROM prereg").fetchone()[0]
    finally:
        store.close()


COMMIT = ["prereg", "commit", "--title", "t", "--procedure", "the procedure"]


def test_prereg_check_dry_run_writes_nothing(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        ["prereg", "check", "--plan-suite", "aiif_round", "--round-id", ROUND, "--params-file", str(params_file(tmp_path))],
        program_root, platform_root, capsys,
    )
    assert rc == 0 and env["ok"] is True
    assert env["result"]["overall"] == "fail"
    assert env["result"]["must_failures"] == ["admission_escrow_planned"]
    assert escrow_files(platform_root) == []
    assert n_prereg_rows(program_root, platform_root) == 0

    rc, env = call(
        ["prereg", "check", "--plan-suite", "aiif_round", "--round-id", ROUND,
         "--params-file", str(params_file(tmp_path, rooms=False))],
        program_root, platform_root, capsys,
    )
    assert rc == 0 and env["result"]["overall"] == "pass"


def test_plan_check_failed_envelope_carries_the_items(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round", "--round-id", ROUND, "--params-file", str(params_file(tmp_path))],
        program_root, platform_root, capsys,
    )
    assert rc == 1 and env["ok"] is False
    assert env["error"]["code"] == "plan_check_failed"
    details = env["error"]["details"]
    assert details["must_failures"] == ["admission_escrow_planned"]
    assert {i["check_id"] for i in details["items"]} >= {"admission_escrow_planned", "declarations_readable"}
    assert "rooms" in env["error"]["message"] and "--accept-deviation" in env["error"]["message"]
    assert len(env["nextActions"]) == 2
    assert escrow_files(platform_root) == []
    assert n_prereg_rows(program_root, platform_root) == 0


def test_params_file_commit_and_status_fields(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round", "--round-id", ROUND,
         "--params-file", str(params_file(tmp_path, rooms=False))],
        program_root, platform_root, capsys,
    )
    assert rc == 0, env
    result = env["result"]
    assert result["plan_check_status"] == "pass" and result["round_id"] == ROUND
    assert len(result["plan_check_items"]) == 8
    assert len(escrow_files(platform_root)) == 1


def test_accept_deviation_through_the_cli(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round", "--round-id", ROUND, "--params-file", str(params_file(tmp_path)),
         "--accept-deviation", "admission_escrow_planned=no rooms are run before the operator decides",
         "--decided-by", "DEC-7"],
        program_root, platform_root, capsys,
    )
    assert rc == 0, env
    assert env["result"]["plan_check_status"] == "deviations_accepted"
    record = json.loads(env["result"]["plan_check"])
    assert record["decided_by"] == "DEC-7"
    assert record["accepted_deviations"] == {
        "admission_escrow_planned": "no rooms are run before the operator decides"
    }

    # a deviation for a check that did not fail is refused, and writes nothing more
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round", "--round-id", ROUND, "--params-file", str(params_file(tmp_path)),
         "--accept-deviation", "arm_mode_declared=x", "--decided-by", "DEC-7"],
        program_root, platform_root, capsys,
    )
    assert rc == 1 and env["error"]["code"] == "commit_refused"
    assert n_prereg_rows(program_root, platform_root) == 1


@pytest.mark.parametrize(
    "extra",
    [
        ["--plan-suite", "aiif_round", "--round-id", ROUND, "--accept-deviation", "a=b"],  # no --decided-by
        ["--parent-prereg", "PREG-1"],  # parent without a suite
        ["--plan-suite", "aiif_round", "--round-id", ROUND, "--parent-prereg", "PREG-1"],  # parent, wrong suite
        ["--plan-suite", "aiif_round_admission", "--round-id", ROUND],  # admission needs a parent
        ["--plan-suite", "aiif_round"],  # no round id
        ["--round-id", ROUND],  # round id without a suite
        ["--plan-suite", "aiif_round", "--round-id", ROUND, "--accept-deviation", "nonsense", "--decided-by", "D"],
    ],
)
def test_flag_misuse_is_bad_input(extra, program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call([*COMMIT, *extra, "--params-file", str(params_file(tmp_path))], program_root, platform_root, capsys)
    assert rc == 1 and env["error"]["code"] == "bad_input", env
    assert escrow_files(platform_root) == [] and n_prereg_rows(program_root, platform_root) == 0


def test_unknown_plan_suite_and_bad_params(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        [*COMMIT, "--plan-suite", "nope", "--round-id", ROUND, "--params-file", str(params_file(tmp_path))],
        program_root, platform_root, capsys,
    )
    assert rc == 1 and env["error"]["code"] == "unknown_plan_suite"
    listed = tmp_path / "list.json"
    listed.write_text("[1, 2]", encoding="utf-8")
    rc, env = call([*COMMIT, "--params-file", str(listed)], program_root, platform_root, capsys)
    assert rc == 1 and env["error"]["code"] == "bad_input"
    rc, env = call([*COMMIT, "--params-file", str(tmp_path / "missing.json")], program_root, platform_root, capsys)
    assert rc == 1 and env["error"]["code"] == "bad_input"


def test_params_and_params_file_are_exclusive(program_root, platform_root, tmp_path):
    with pytest.raises(SystemExit):
        main([*COMMIT, "--params", "{}", "--params-file", str(params_file(tmp_path)),
              "--program-root", str(program_root), "--platform-root", str(platform_root)])


def test_check_flag_misuse(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        ["prereg", "check", "--plan-suite", "aiif_round", "--params-file", str(params_file(tmp_path))],
        program_root, platform_root, capsys,
    )
    assert rc == 1 and env["error"]["code"] == "bad_input"  # no round id
    rc, env = call(["prereg", "check", "--params", "{}"], program_root, platform_root, capsys)
    assert rc == 1 and env["error"]["code"] == "bad_input"  # no suite


def test_admission_commit_through_the_cli(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round", "--round-id", ROUND,
         "--params-file", str(params_file(tmp_path, admission_escrow={"by": "second_prereg_commit"}))],
        program_root, platform_root, capsys,
    )
    assert rc == 0, env
    parent_id = env["result"]["prereg_id"]
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round_admission", "--round-id", ROUND, "--parent-prereg", parent_id,
         "--params", json.dumps({"admission_order_hash": "c" * 64})],
        program_root, platform_root, capsys,
    )
    assert rc == 0, env
    assert env["result"]["parent_prereg_id"] == parent_id
    rc, env = call(
        ["prereg", "check", "--plan-suite", "aiif_round_admission", "--round-id", ROUND, "--parent-prereg", "PREG-x",
         "--params", "{}"],
        program_root, platform_root, capsys,
    )
    assert rc == 1 and env["error"]["code"] == "not_found"


def _accept_action(env):
    (action,) = [a for a in env["nextActions"] if "--accept-deviation" in a["argv"]]
    return action["argv"]


def _fill(argv):
    """Substitute the two things the printed command leaves to the operator."""
    return [
        a.replace("<reason>", "the operator decided the round runs as it is").replace("<operator decision id>", "DEC-9")
        for a in argv
    ]


def test_the_accept_deviation_next_action_runs_as_printed(program_root, platform_root, full_models, tmp_path, capsys):
    argv = [*COMMIT, "--plan-suite", "aiif_round", "--round-id", ROUND, "--params-file", str(params_file(tmp_path))]
    rc, env = call(argv, program_root, platform_root, capsys)
    assert env["error"]["code"] == "plan_check_failed"
    printed = _accept_action(env)
    assert printed[:3] == ["trialerror", "prereg", "commit"]
    for flag in ("--title", "--procedure", "--round-id", "--params-file", "--plan-suite"):
        assert flag in printed
    assert printed.count("--accept-deviation") == len(env["error"]["details"]["must_failures"])
    assert n_prereg_rows(program_root, platform_root) == 0

    # run it as printed (minus the leading program name), with the reasons and the decision filled in
    main(_fill(printed)[1:])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True, out
    assert out["result"]["plan_check_status"] == "deviations_accepted"


def test_the_next_action_carries_the_parent_and_every_must_failure(program_root, platform_root, full_models, tmp_path, capsys):
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round", "--round-id", ROUND,
         "--params-file", str(params_file(tmp_path, admission_escrow={"by": "second_prereg_commit"}))],
        program_root, platform_root, capsys,
    )
    parent_id = env["result"]["prereg_id"]
    rc, env = call(
        [*COMMIT, "--plan-suite", "aiif_round_admission", "--round-id", "R-OTHER", "--parent-prereg", parent_id,
         "--params", json.dumps({"admission_order_hash": "nope"})],
        program_root, platform_root, capsys,
    )
    assert env["error"]["code"] == "plan_check_failed"
    printed = _accept_action(env)
    assert printed[printed.index("--parent-prereg") + 1] == parent_id
    assert "--params" in printed and "--params-file" not in printed
    deviations = [printed[i + 1] for i, a in enumerate(printed) if a == "--accept-deviation"]
    assert sorted(d.split("=")[0] for d in deviations) == sorted(env["error"]["details"]["must_failures"])
    assert len(deviations) == 2


def test_params_file_with_a_utf8_bom_is_read(program_root, platform_root, full_models, tmp_path, capsys):
    """A file saved by an editor or PowerShell may start with a BOM."""
    path = tmp_path / "bom.json"
    path.write_bytes(codecs.BOM_UTF8 + json.dumps(json.loads(FIXTURE.read_text(encoding="utf-8")) | {"rooms": False}).encode("utf-8"))
    rc, env = call(
        ["prereg", "check", "--plan-suite", "aiif_round", "--round-id", ROUND, "--params-file", str(path)],
        program_root, platform_root, capsys,
    )
    assert rc == 0 and env["ok"] is True and env["result"]["overall"] == "pass", env
