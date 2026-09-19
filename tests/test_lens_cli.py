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
    # the per-lens reconciliation rides beside the raw rows: one lens, one
    # row, and it never posted, so the round has an offender to chase
    assert env["result"]["n_lenses"] == 1
    assert env["result"]["rows"][0]["roster_id"] == lens_row["roster_id"]
    assert env["result"]["rows"][0]["posted"] is False
    assert len(env["result"]["offenders"]) == 1
    assert any("export" in " ".join(a["argv"]) for a in env["nextActions"])

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


def test_lens_roster_add_control_seat_with_cards_is_refused(cli_program_root, capsys):
    env = _run_cli(
        capsys,
        [
            "lens", "--program-root", str(cli_program_root), "roster", "--add",
            "--round-id", "round-c", "--lens-name", "control-1",
            "--vantage", "CONTROL:no-recipe", "--model-class", "top",
            "--seat", "control", "--recipe-card", "MISMATCH",
        ],
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "roster_refused"


def test_lens_arm_per_lens_end_to_end(cli_program_root, capsys):
    """One arm per lens, whole slice from it, cards recorded, and the
    export attrs naming both -- through the real argv path."""
    store = open_store(cli_program_root)
    pool = build_doc_pool(store, n_docs=61)
    home_id, *candidate_ids = pool["doc_ids"]
    store.close()

    seats = [
        ("standard", ["MISMATCH", "TRANSFER"]),
        ("standard", ["TRANSFER", "MISMATCH"]),
        ("standard", ["MISMATCH", "TRANSFER"]),
        ("standard", ["TRANSFER", "MISMATCH"]),
        ("assumption_buster", ["NEGATE"]),
        ("control", []),
    ]
    for i, (seat, cards) in enumerate(seats):
        argv = [
            "lens", "--program-root", str(cli_program_root), "roster", "--add",
            "--round-id", "round-apl", "--lens-name", f"lens-{i}", "--vantage", f"v{i}",
            "--model-class", "top", "--seat", seat,
        ]
        for card in cards:
            argv += ["--recipe-card", card]
        assert _run_cli(capsys, argv)["ok"] is True

    assign_argv = [
        "lens", "--program-root", str(cli_program_root), "assign",
        "--model-key", pool["model_key"], "--home", home_id,
        "--round-id", "round-apl", "--slices-per-lens", "5", "--seed", "seed-apl",
        "--arm-per-lens",
    ]
    for cid in candidate_ids:
        assign_argv += ["--candidate", cid]
    env = _run_cli(capsys, assign_argv)
    assert env["ok"] is True
    assert env["result"]["count"] == 30
    assert env["result"]["arm_mode"] == "per_lens"
    assert env["result"]["roster_quota"] == {"near": 3, "moderate": 1, "far": 2}

    env = _run_cli(capsys, ["lens", "--program-root", str(cli_program_root), "export", "--round-id", "round-apl"])
    assert env["ok"] is True
    attrs = [row["attrs"] for row in env["result"]["bookable"]]
    assert sorted(a["arm"] for a in attrs) == ["far", "far", "moderate", "near", "near", "near"]
    buster = next(a for a in attrs if a["seat"] == "assumption_buster")
    assert buster["arm"] == "far"
    assert buster["recipe_cards"] == ["NEGATE"]


def test_lens_assign_without_the_flag_still_writes_per_slice(cli_program_root, capsys):
    store = open_store(cli_program_root)
    pool = build_doc_pool(store, n_docs=12)
    add_lens(store, round_id="round-ps", lens_name="l", vantage="v", model_class="top")
    home_id, *candidate_ids = pool["doc_ids"]
    store.close()

    assign_argv = [
        "lens", "--program-root", str(cli_program_root), "assign",
        "--model-key", pool["model_key"], "--home", home_id,
        "--round-id", "round-ps", "--slices-per-lens", "5", "--seed", "seed-A",
    ]
    for cid in candidate_ids:
        assign_argv += ["--candidate", cid]
    env = _run_cli(capsys, assign_argv)
    assert env["ok"] is True
    assert env["result"]["arm_mode"] == "per_slice"
    assert env["result"]["roster_quota"] is None
