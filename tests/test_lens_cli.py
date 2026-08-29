"""``trialerror lens`` CLI group — envelope shape + argv wiring over
``trialerror.lens.*``. Uses ``trialerror.cli.build_parser``/``main`` exactly the way a
real invocation would, with ``--program-root`` pointed at an isolated
program (same convention ``tests/test_cli_law.py`` and friends use)."""

from __future__ import annotations

import json

import pytest

from trialerror.cli import main
from trialerror.lens.roster import add_lens
from trialerror.stores.store import open_store
from tests._lens_fixtures import build_doc_pool


@pytest.fixture()
def cli_program_root(tmp_path, monkeypatch):
    platform_root = tmp_path / "platform_root"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir(parents=True, exist_ok=True)
    return program_root


def _run_cli(capsys, argv: list[str]) -> dict:
    exit_code = main(argv)
    out = capsys.readouterr().out.strip()
    envelope = json.loads(out)
    envelope["_exit_code"] = exit_code
    return envelope


def test_lens_no_action_error_envelope(cli_program_root, capsys):
    env = _run_cli(capsys, ["lens", "--program-root", str(cli_program_root)])
    assert env["ok"] is False
    assert env["error"]["code"] == "no_action"


def test_lens_roster_add_and_list(cli_program_root, capsys):
    env = _run_cli(
        capsys,
        [
            "lens", "--program-root", str(cli_program_root), "roster", "--add",
            "--round-id", "round-1", "--lens-name", "skeptic", "--vantage", "adversarial",
            "--model-class", "top",
        ],
    )
    assert env["ok"] is True
    assert env["result"]["lens_name"] == "skeptic"

    env = _run_cli(capsys, ["lens", "--program-root", str(cli_program_root), "roster", "--round-id", "round-1"])
    assert env["ok"] is True
    assert env["result"]["count"] == 1


def test_lens_roster_add_missing_fields_error_envelope(cli_program_root, capsys):
    env = _run_cli(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "roster", "--add", "--round-id", "round-1"],
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "missing_fields"


def test_lens_roster_add_bad_seat_rejected_by_argparse_choices(cli_program_root, capsys):
    # argparse's own `choices=` constraint calls sys.exit(2) before the
    # handler (and therefore any envelope) is ever reached.
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "lens", "--program-root", str(cli_program_root), "roster", "--add",
                "--round-id", "round-1", "--lens-name", "x", "--vantage", "v",
                "--model-class", "top", "--seat", "bogus",
            ]
        )
    assert exc_info.value.code == 2


def test_lens_stratify_assign_log_export_end_to_end(cli_program_root, capsys):
    store = open_store(cli_program_root)
    pool = build_doc_pool(store, n_docs=12)
    lens_row = add_lens(store, round_id="round-1", lens_name="skeptic", vantage="adversarial", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]
    store.close()

    stratify_argv = [
        "lens", "--program-root", str(cli_program_root), "stratify",
        "--model-key", pool["model_key"], "--home", home_id,
    ]
    for cid in candidate_ids:
        stratify_argv += ["--candidate", cid]
    env = _run_cli(capsys, stratify_argv)
    assert env["ok"] is True
    assert env["result"]["count"] == len(candidate_ids)

    assign_argv = [
        "lens", "--program-root", str(cli_program_root), "assign",
        "--model-key", pool["model_key"], "--home", home_id,
        "--round-id", "round-1", "--slices-per-lens", "5", "--seed", "seed-A",
    ]
    for cid in candidate_ids:
        assign_argv += ["--candidate", cid]
    env = _run_cli(capsys, assign_argv)
    assert env["ok"] is True
    assert env["result"]["count"] == 5

    env = _run_cli(capsys, ["lens", "--program-root", str(cli_program_root), "log", "--round-id", "round-1"])
    assert env["ok"] is True
    assert env["result"]["count"] == 5

    env = _run_cli(capsys, ["lens", "--program-root", str(cli_program_root), "export", "--round-id", "round-1"])
    assert env["ok"] is True
    assert env["result"]["count"] == 1
    assert env["result"]["bookable"][0]["attrs"]["roster_id"] == lens_row["roster_id"]


def test_lens_assign_empty_roster_error_envelope(cli_program_root, capsys):
    store = open_store(cli_program_root)
    pool = build_doc_pool(store, n_docs=6)
    home_id, *candidate_ids = pool["doc_ids"]
    store.close()

    assign_argv = [
        "lens", "--program-root", str(cli_program_root), "assign",
        "--model-key", pool["model_key"], "--home", home_id,
        "--round-id", "round-nope", "--slices-per-lens", "2", "--seed", "seed-A",
    ]
    for cid in candidate_ids:
        assign_argv += ["--candidate", cid]
    env = _run_cli(capsys, assign_argv)
    assert env["ok"] is False
    assert env["error"]["code"] == "empty_roster"


def test_lens_program_root_not_found_error_envelope(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = _run_cli(capsys, ["lens", "roster", "--round-id", "round-1"])
    assert env["ok"] is False
    assert env["error"]["code"] == "program_root_not_found"
