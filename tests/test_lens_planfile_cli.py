"""``trialerror lens assign --plan-file``: the CLI mode, and a round it wrote
as every doctor check that reads assignment rows sees it.

- the envelope, and the mode's exclusivity with ``lens assign``'s draw flags;
- a fixture round with synthetic lens posts, on which ``no_duplicate_slice``,
  ``recipe_rotation_honored``, ``far_arm_floor_honored`` and
  ``far_lens_floor_honored`` read ``pass`` (a ``skip`` fails the test);
- ``lens_citations_within_slice`` over half-launches: each lens launched twice,
  once per half of its slice, each launch booked through ``budget book`` with
  its own four ``--assign-id`` and ``--phase card|plain``. A post citing its own
  half passes; a post citing the other half fails.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from trialerror.cli import main
from trialerror.events.api import create_thread, post_feed
from trialerror.lens.checks import (
    check_far_arm_floor_honored,
    check_far_lens_floor_honored,
    check_lens_citations_within_slice,
    check_no_duplicate_slice,
    check_recipe_rotation_honored,
)
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._planfile_fixtures import (
    LENSES,
    ROUND_ID,
    ROWS_PER_LENS,
    build_round,
    edited,
    lens_entry,
    rehash,
    write_plan_file,
)

HALF = ROWS_PER_LENS // 2


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


@pytest.fixture()
def cli_round(tmp_path, program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    try:
        built = build_round(store)
    finally:
        store.close()
    built["plan_path"] = write_plan_file(tmp_path / "plan.json", built["plan"])
    built["program_root"] = program_root
    built["platform_root"] = platform_root
    return built


def _assign_argv(built, *extra: str) -> list[str]:
    return [
        "lens", "--program-root", str(built["program_root"]), "assign",
        "--plan-file", str(built["plan_path"]), "--round-id", ROUND_ID,
        "--launch-id", built["launch_id"], *extra,
    ]


def _row_count(built) -> int:
    store = open_store(built["program_root"], platform_root=built["platform_root"])
    try:
        return store.ops.execute("SELECT COUNT(*) AS n FROM lens_assignment").fetchone()["n"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# the mode
# ---------------------------------------------------------------------------


def test_the_plan_file_mode_returns_its_envelope(cli_round):
    rc, env = _run_cli(_assign_argv(cli_round, "--expect-plan-sha256", cli_round["plan"]["plan_sha256"]))
    assert rc == 0, env
    assert env["ok"] is True and env["command"] == "lens assign"
    result = env["result"]
    assert set(result) == {"n_rows", "n_lenses", "projection_sha256", "plan_file_sha256", "assign_ids"}
    assert result["n_rows"] == ROWS_PER_LENS * len(LENSES)
    assert result["n_lenses"] == len(LENSES)
    assert result["projection_sha256"] == cli_round["plan"]["annex"]["projection_sha256"]
    assert sorted(result["assign_ids"]) == sorted(name for name, *_ in LENSES)
    assert _row_count(cli_round) == result["n_rows"]


@pytest.mark.parametrize(
    "flag",
    [
        ["--model-key", "fake"], ["--home", "DOC-x"], ["--candidate", "DOC-x"],
        ["--slices-per-lens", "8"], ["--slices-per-lens", "0"], ["--seed", "s"],
        ["--roster-id", "ROST-x"], ["--weights", "40,40,20"], ["--far-floor", "2"], ["--far-floor", "0"],
        ["--arm-per-lens"],
        ["--slice-salt", "lens-name"], ["--inter-cluster-mandate"], ["--home-cluster", "C1"],
        ["--cluster-of", "{}"],
    ],
)
def test_the_plan_file_mode_takes_none_of_the_draw_flags(cli_round, flag):
    rc, env = _run_cli(_assign_argv(cli_round, *flag))
    assert rc == 1
    assert env["error"]["code"] == "plan_file_conflict"
    assert flag[0] in env["error"]["message"]
    assert _row_count(cli_round) == 0


def test_the_plan_file_mode_needs_a_launch_id(cli_round):
    argv = [a for a in _assign_argv(cli_round) if a not in ("--launch-id", cli_round["launch_id"])]
    rc, env = _run_cli(argv)
    assert rc == 1 and env["error"]["code"] == "missing_fields"
    assert "--launch-id" in env["error"]["message"]


def test_the_draw_mode_still_needs_its_own_flags(cli_round):
    rc, env = _run_cli(["lens", "--program-root", str(cli_round["program_root"]), "assign", "--round-id", ROUND_ID])
    assert rc == 1 and env["error"]["code"] == "missing_fields"
    for flag in ("--model-key", "--home", "--candidate", "--slices-per-lens", "--seed"):
        assert flag in env["error"]["message"]
    rc, env = _run_cli([
        "lens", "--program-root", str(cli_round["program_root"]), "assign", "--round-id", ROUND_ID,
        "--expect-plan-sha256", "0" * 64,
    ])
    assert rc == 1 and env["error"]["code"] == "plan_file_conflict"


def test_the_draw_mode_refuses_empty_weights_instead_of_defaulting(cli_round):
    rc, env = _run_cli([
        "lens", "--program-root", str(cli_round["program_root"]), "assign", "--round-id", ROUND_ID,
        "--model-key", "fake", "--home", "DOC-x", "--candidate", "DOC-y", "--slices-per-lens", "3",
        "--seed", "s", "--weights", "",
    ])
    assert rc == 1 and env["error"]["code"] == "assign_error"
    assert _row_count(cli_round) == 0


def test_a_refusal_names_its_check_and_items(cli_round, tmp_path):
    plan = edited(cli_round["plan"])
    lens_entry(plan, "lens-b")["rows"][0]["rank"] = 7
    write_plan_file(cli_round["plan_path"], rehash(plan))
    rc, env = _run_cli(_assign_argv(cli_round))
    assert rc == 1
    assert env["error"]["code"] == "plan_ranks_refused"
    assert env["error"]["details"] == {
        "check": "V6", "items": ["lens 'lens-b': ranks [1, 2, 3, 4, 5, 6, 7, 7], expected 0..7"],
    }
    assert _row_count(cli_round) == 0


def test_an_unreadable_plan_file_is_a_refusal(cli_round, tmp_path):
    cli_round["plan_path"] = tmp_path / "absent.json"
    rc, env = _run_cli(_assign_argv(cli_round))
    assert rc == 1 and env["error"]["code"] == "plan_file_unreadable"


# ---------------------------------------------------------------------------
# the fixture round, as the doctor reads it
# ---------------------------------------------------------------------------


def _open_session(store) -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    return session_id


def _book_half(built, session_id: str, *, lens_name: str, assign_ids: list[str], phase: str) -> str:
    argv = [
        "budget", "--program-root", str(built["program_root"]), "--platform-root", str(built["platform_root"]),
        "book", "--session-id", session_id, "--program-id", "PROG-test", "--agent-kind", "lens",
        "--model-class", "mid", "--model", "sonnet", "--purpose", "mechanical", "--est-tokens", "100",
        "--lens-name", lens_name, "--phase", phase,
    ]
    for assign_id in assign_ids:
        argv += ["--assign-id", assign_id]
    rc, env = _run_cli(argv)
    assert rc == 0, env
    return env["result"]["launch_id"]


def _post(built, *, launch_id: str, cites: list[str]) -> str:
    store = open_store(built["program_root"], platform_root=built["platform_root"])
    try:
        thread = create_thread(store, title="round thread", launch_id=launch_id)
        body = "Records from this launch. Read: " + ", ".join(cites)
        return post_feed(store, thread_id=thread["thread_id"], body=body, launch_id=launch_id)["post_id"]
    finally:
        store.close()


@pytest.fixture()
def written_round(cli_round):
    rc, env = _run_cli(_assign_argv(cli_round))
    assert rc == 0, env
    cli_round["assign_ids"] = env["result"]["assign_ids"]
    return cli_round


def test_the_four_assignment_checks_read_pass_on_the_round(written_round):
    ctx = DoctorContext(program_root=written_round["program_root"], platform_root=written_round["platform_root"])
    for check in (
        check_no_duplicate_slice, check_recipe_rotation_honored,
        check_far_arm_floor_honored, check_far_lens_floor_honored,
    ):
        result = check(ctx)
        assert result.status == "pass", (result.name, result.status, result.message, result.details)


def test_half_launches_cite_their_own_half_and_are_caught_citing_the_other(written_round):
    """Every lens launched twice, each launch bound to one half of its slice
    (ranks 0-3 under ``card``, 4-7 under ``plain``), each posting records that
    cite its own half: the citation audit passes. One more post, by a
    ``plain`` launch citing a document of its lens's ``card`` half, fails --
    and is the only offender."""
    built = written_round
    store = open_store(built["program_root"], platform_root=built["platform_root"])
    try:
        session_id = _open_session(store)
    finally:
        store.close()
    docs = built["docs_by_lens"]

    launches: dict[tuple[str, str], str] = {}
    for name, *_rest in LENSES:
        ids = built["assign_ids"][name]
        for phase, half_ids, half_docs in (
            ("card", ids[:HALF], docs[name][:HALF]),
            ("plain", ids[HALF:], docs[name][HALF:]),
        ):
            launch_id = _book_half(built, session_id, lens_name=name, assign_ids=half_ids, phase=phase)
            launches[(name, phase)] = launch_id
            _post(built, launch_id=launch_id, cites=half_docs[:2])

    ctx = DoctorContext(program_root=built["program_root"], platform_root=built["platform_root"])
    result = check_lens_citations_within_slice(ctx)
    assert result.status == "pass", (result.message, result.details)
    assert result.details["posts_checked"] == 2 * len(LENSES)

    # Each half-launch is bound to exactly its own four rows, labelled with its phase.
    store = open_store(built["program_root"], platform_root=built["platform_root"])
    try:
        bound = store.ops.execute(
            "SELECT launch_id, phase, COUNT(*) AS n FROM lens_assignment_launch GROUP BY launch_id, phase"
        ).fetchall()
    finally:
        store.close()
    assert sorted((r["launch_id"], r["phase"], r["n"]) for r in bound) == sorted(
        (launch_id, phase, HALF) for (_name, phase), launch_id in launches.items()
    )

    crossing = _post(built, launch_id=launches[("lens-a", "plain")], cites=[docs["lens-a"][0]])
    result = check_lens_citations_within_slice(ctx)
    assert result.status == "fail"
    assert [o["post_id"] for o in result.details["offenders"]] == [crossing]
    assert result.details["offenders"][0]["resolved_docs"] == [docs["lens-a"][0]]
