"""The calibration recording path stamps ``prereg_compliant`` the way the
round path does (lane FB-8b item 5).

The observed gap: ``lens screen --record-calibration --prereg-id …
--executed-procedure-file … --executed-params …`` linked ``prereg_id`` onto
every verdict row it wrote and left ``prereg_compliant`` NULL, while
``--record-verdicts`` stamped it by recomputing the hash. A row that names a
pre-registration and says nothing about whether it was followed reads, to
anything counting rows later, exactly like a row recorded under no
pre-registration at all -- and a calibration is the instrument the round's
own labels are judged against, so "was the instrument the one we committed
to?" is not a question it may leave blank.

The answers are not two. Compliant; the procedure moved; the params moved;
nothing promised; promised but unchecked; and -- found by this lane's probe
(c) -- a pre-registration that has been VOIDED, where compliance is
undefined and the recording must still land. Two claims under each: what the
CARD says, and what the verdict ROWS carry -- the card is what a person reads
and the rows are what a later count reads, and this item is about the rows.

Driven through ``trialerror.cli.main``, because the CLI is where the gap
was: the recording function could have been given the arguments all along.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import main
from trialerror.stores.store import open_store
from trialerror.verify.prereg import commit_prereg

from tests._novelty_fixtures import build_round

SEED = "seed-calibration-prereg"
PROCEDURE = "aiif-round-v2: stratify, diverge, screen, converge\n"
PARAMS = {"k": 12, "seed": SEED}

CALIBRATION_PLANTS = [
    {"plant_id": "C-1", "kind": "area", "statement": "Calibration plant one, a rewritten reference row.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-2", "kind": "area", "statement": "Calibration plant two, another rewritten row.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-3", "kind": "custom", "statement": "Calibration plant three, unrelated to everything.",
     "expected_labels": {"R3": ["new-mechanism"]}},
]

SHEET_A = {
    "C-1": {"label_inventory": "same"},
    "C-2": {"label_inventory": "variant"},
    "C-3": {"label_inventory": "new-mechanism"},
}
SHEET_B = {
    "C-1": {"label_inventory": "same"},
    "C-2": {"label_inventory": "new-mechanism"},
    "C-3": {"label_inventory": "new-mechanism"},
}


@pytest.fixture()
def cli_program_root(tmp_path, monkeypatch):
    platform_root = tmp_path / "platform_root"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir(parents=True, exist_ok=True)
    return program_root


@pytest.fixture()
def seeded_round(cli_program_root, tmp_path):
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    fixture = build_round(store)
    store.close()
    return fixture


@pytest.fixture()
def prereg(cli_program_root, tmp_path):
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    row = commit_prereg(store, title="round", procedure=PROCEDURE, params=PARAMS)
    store.close()
    return row


def _run(capsys, argv: list[str]) -> dict:
    exit_code = main(argv)
    envelope = json.loads(capsys.readouterr().out.strip())
    envelope["_exit_code"] = exit_code
    return envelope


def _screen_argv(program_root, round_id, *rest) -> list[str]:
    return ["lens", "--program-root", str(program_root), "screen", "--round-id", round_id, *rest]


def _files(tmp_path):
    plants = tmp_path / "calibration-plants.json"
    plants.write_text(json.dumps(CALIBRATION_PLANTS), encoding="utf-8")
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(SHEET_A), encoding="utf-8")
    b.write_text(json.dumps(SHEET_B), encoding="utf-8")
    procedure = tmp_path / "procedure.md"
    procedure.write_text(PROCEDURE, encoding="utf-8")
    return plants, a, b, procedure


def _record(capsys, cli_program_root, seeded_round, tmp_path, *extra) -> dict:
    """Mechanical half, calibration batch, then the recording under test."""
    plants, a, b, _procedure = _files(tmp_path)
    _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--mechanical",
        "--launch-id", seeded_round["launches"]["lens-1"],
    ))
    _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--calibration", "--seed", SEED,
        "--plants-file", str(plants), "--batch-fail-on", "area",
    ))
    env = _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--record-calibration",
        "--judge-sheet-a", str(a), "--judge-sheet-b", str(b),
        "--launch-id", seeded_round["launches"]["lens-1"], "--batch-fail-on", "area",
        *extra,
    ))
    assert env["ok"] is True, env
    return env["result"]["record_calibration"]


def _verdict_rows(program_root, tmp_path) -> list[dict]:
    store = open_store(program_root, platform_root=tmp_path / "platform_root")
    try:
        return [
            dict(r)
            for r in store.knowledge.execute(
                "SELECT verdict_id, prereg_id, prereg_compliant FROM verdict "
                "WHERE procedure_version = 'novelty-v2-calibration' ORDER BY verdict_id"
            ).fetchall()
        ]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# the four answers
# ---------------------------------------------------------------------------


def test_a_compliant_calibration_is_stamped_true_on_the_card_and_on_every_row(
    cli_program_root, seeded_round, prereg, tmp_path, capsys
):
    _plants, _a, _b, procedure = _files(tmp_path)
    card = _record(
        capsys, cli_program_root, seeded_round, tmp_path,
        "--prereg-id", prereg["prereg_id"],
        "--executed-procedure-file", str(procedure),
        "--executed-params", json.dumps(PARAMS),
    )
    assert card["prereg_id"] == prereg["prereg_id"]
    assert card["prereg_compliant"] is True
    assert "both hashes match" in card["prereg_compliance"]
    assert card["prereg_compliance_detail"]["mismatched"] == []

    rows = _verdict_rows(cli_program_root, tmp_path)
    assert rows, "the calibration wrote no verdict rows at all"
    assert {r["prereg_id"] for r in rows} == {prereg["prereg_id"]}
    assert {r["prereg_compliant"] for r in rows} == {1}


def test_a_procedure_that_moved_is_recorded_and_named_never_refused(
    cli_program_root, seeded_round, prereg, tmp_path, capsys
):
    """The exact shape the round path was taught to report: a `$(cat file)`
    strips the trailing newline, the hash differs, and the answer must say
    WHICH half disagreed rather than a bare false."""
    card = _record(
        capsys, cli_program_root, seeded_round, tmp_path,
        "--prereg-id", prereg["prereg_id"],
        "--executed-procedure", PROCEDURE.rstrip("\n"),
        "--executed-params", json.dumps(PARAMS),
    )
    assert card["prereg_compliant"] is False
    assert "procedure hash disagrees" in card["prereg_compliance"]
    assert "params hash disagrees" not in card["prereg_compliance"]
    assert card["prereg_compliance_detail"]["mismatched"] == ["procedure"]
    # written, not refused: the rows are there, saying so
    rows = _verdict_rows(cli_program_root, tmp_path)
    assert rows
    assert {r["prereg_compliant"] for r in rows} == {0}


def test_params_that_moved_are_named_as_the_half_that_moved(
    cli_program_root, seeded_round, prereg, tmp_path, capsys
):
    _plants, _a, _b, procedure = _files(tmp_path)
    card = _record(
        capsys, cli_program_root, seeded_round, tmp_path,
        "--prereg-id", prereg["prereg_id"],
        "--executed-procedure-file", str(procedure),
        "--executed-params", json.dumps({**PARAMS, "k": 13}),
    )
    assert card["prereg_compliant"] is False
    assert "params hash disagrees" in card["prereg_compliance"]
    assert "procedure hash disagrees" not in card["prereg_compliance"]
    assert card["prereg_compliance_detail"]["mismatched"] == ["params"]
    assert {r["prereg_compliant"] for r in _verdict_rows(cli_program_root, tmp_path)} == {0}


def test_a_prereg_with_no_executed_procedure_stays_null_and_says_why(
    cli_program_root, seeded_round, prereg, tmp_path, capsys
):
    """Unchanged behaviour, stated: nothing was checked, so nothing is
    claimed. What is new is that the card says so instead of being silent."""
    card = _record(
        capsys, cli_program_root, seeded_round, tmp_path,
        "--prereg-id", prereg["prereg_id"],
    )
    assert card["prereg_compliant"] is None
    assert "not stamped" in card["prereg_compliance"]
    assert card["prereg_compliance_detail"] is None
    rows = _verdict_rows(cli_program_root, tmp_path)
    assert {r["prereg_id"] for r in rows} == {prereg["prereg_id"]}
    assert {r["prereg_compliant"] for r in rows} == {None}


def test_no_prereg_at_all_is_the_third_state_not_the_second(
    cli_program_root, seeded_round, tmp_path, capsys
):
    card = _record(capsys, cli_program_root, seeded_round, tmp_path)
    assert card["prereg_id"] is None
    assert card["prereg_compliant"] is None
    assert card["prereg_compliance"] == "no prereg_id given"
    assert {r["prereg_compliant"] for r in _verdict_rows(cli_program_root, tmp_path)} == {None}


def test_the_card_on_disk_carries_the_same_answer_as_the_envelope(
    cli_program_root, seeded_round, prereg, tmp_path, capsys
):
    """The card file is the artefact that outlives the invocation."""
    _plants, _a, _b, procedure = _files(tmp_path)
    card = _record(
        capsys, cli_program_root, seeded_round, tmp_path,
        "--prereg-id", prereg["prereg_id"],
        "--executed-procedure-file", str(procedure),
        "--executed-params", json.dumps(PARAMS),
    )
    on_disk = json.loads(open(card["card_file"], encoding="utf-8").read())
    assert on_disk["prereg_compliant"] is True
    assert on_disk["prereg_id"] == prereg["prereg_id"]
    assert on_disk["prereg_compliance"] == card["prereg_compliance"]


# ---------------------------------------------------------------------------
# the shared helper, at its own seam
# ---------------------------------------------------------------------------


def test_both_recording_paths_stamp_through_the_same_helper():
    """Not a copy (the brief's own requirement). Asserted structurally: both
    functions call `prereg_stamp`, so a fix to one is a fix to both."""
    import inspect

    from trialerror.lens import novelty

    for fn in (novelty.record_calibration, novelty.record_novelty_verdicts):
        assert "prereg_stamp(" in inspect.getsource(fn), fn.__name__


def test_the_helpers_three_states_are_distinguishable(cli_program_root, prereg, tmp_path):
    from trialerror.lens.novelty import prereg_stamp

    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    try:
        none_given = prereg_stamp(store, prereg_id=None)
        unchecked = prereg_stamp(store, prereg_id=prereg["prereg_id"])
        checked = prereg_stamp(
            store, prereg_id=prereg["prereg_id"],
            executed_procedure=PROCEDURE, executed_params=PARAMS,
        )
    finally:
        store.close()
    assert none_given[0] is None and none_given[2] is None
    assert unchecked[0] is None and unchecked[2] is None
    assert none_given[1] != unchecked[1], (
        "'nothing promised' and 'promised but unchecked' are different answers"
    )
    assert checked[0] is True and checked[2]["compliant"] is True


# ---------------------------------------------------------------------------
# probe (c): the stamp must not turn a previously ACCEPTED recording into a
# refusal. It did, twice, and both are fixed with these tests kept.
# ---------------------------------------------------------------------------


def test_a_voided_prereg_is_recorded_as_unstamped_not_refused(
    cli_program_root, seeded_round, tmp_path, capsys
):
    """Probe (c), finding 1.

    Compliance with a voided commitment is undefined, and the compliance
    helper says so by raising. On the calibration path that raise turned a
    call that was accepted before this item -- `prereg_id` linked, the column
    NULL -- into a refusal, throwing away two judges' labels over a column
    nobody could have filled in. The rows land, and now say why nothing was
    claimed."""
    from trialerror.stores import update as store_update

    _plants, _a, _b, procedure = _files(tmp_path)
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    row = commit_prereg(store, title="round", procedure=PROCEDURE, params=PARAMS)
    store_update(
        store, "prereg", pk_column="prereg_id", pk_value=row["prereg_id"],
        changes={"status": "voided"},
    )
    store.close()

    card = _record(
        capsys, cli_program_root, seeded_round, tmp_path,
        "--prereg-id", row["prereg_id"],
        "--executed-procedure-file", str(procedure),
        "--executed-params", json.dumps(PARAMS),
    )
    assert card["prereg_compliant"] is None
    assert "not stamped" in card["prereg_compliance"]
    assert "voided" in card["prereg_compliance"]
    rows = _verdict_rows(cli_program_root, tmp_path)
    assert rows, "a voided prereg must not cost the calibration its rows"
    assert {r["prereg_compliant"] for r in rows} == {None}


@pytest.mark.parametrize("phase", ["calibration", "verdicts"])
def test_a_prereg_that_does_not_exist_is_an_envelope_not_a_traceback(
    cli_program_root, seeded_round, tmp_path, capsys, phase
):
    """Probe (c), finding 2 -- and it was never calibration-only.

    `--prereg-id` naming a prereg that does not exist came out of the screen
    as an uncaught `PreregNotFoundError` on BOTH recording paths, from the day
    compliance was first stamped: a stack trace where an agent needs an
    envelope. It is refused now, by name, before a single row is written --
    and it stays a refusal, because it was one before (the verdict table's own
    XID check caught it, mid-write)."""
    plants, a, b, procedure = _files(tmp_path)
    _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--mechanical",
        "--launch-id", seeded_round["launches"]["lens-1"],
    ))
    if phase == "calibration":
        _run(capsys, _screen_argv(
            cli_program_root, seeded_round["round_id"], "--calibration", "--seed", SEED,
            "--plants-file", str(plants), "--batch-fail-on", "area",
        ))
        argv = _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-calibration",
            "--judge-sheet-a", str(a), "--judge-sheet-b", str(b),
            "--launch-id", seeded_round["launches"]["lens-1"], "--batch-fail-on", "area",
            "--prereg-id", "PREREG-does-not-exist",
            "--executed-procedure-file", str(procedure), "--executed-params", json.dumps(PARAMS),
        )
    else:
        prep = _run(capsys, _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
        ))["result"]["judged_prep"]
        batch = json.loads(open(prep["batch_file"], encoding="utf-8").read())
        labels = {
            i: {"label_inventory": "variant", "label_corpus": "adjacent"}
            for i in batch["scope"]["scope"]
        }
        for plant in batch["plants"]:
            labels[plant["plant_id"]] = {"label_inventory": "same"}
        labels_file = tmp_path / "labels.json"
        labels_file.write_text(json.dumps(labels), encoding="utf-8")
        argv = _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
            "--launch-id", seeded_round["launches"]["lens-1"],
            "--prereg-id", "PREREG-does-not-exist",
            "--executed-procedure-file", str(procedure), "--executed-params", json.dumps(PARAMS),
        )

    env = _run(capsys, argv)
    assert env["ok"] is False
    assert env["error"]["code"] == "screen_error"
    assert "no such prereg" in env["error"]["message"]
    # nothing was written on the way to that refusal
    assert _verdict_rows(cli_program_root, tmp_path) == []
