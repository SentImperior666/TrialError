"""Lane FB-6 item 3: a recording run reads the BATCH's declared sets.

``--labels-file`` is resolved against the sets the run declares, and its hash
is computed over them -- so recording against an R2,R4 batch under the CLI's
default (R3,R4) refused a labels file that matched the batch exactly, twice
over: the R2 block was "a block for a set this round did not declare", and
had it loaded, its hash would not have matched the batch's.

The only way through was to repeat ``--judged-sets R2,R4`` on every recording
command -- a flag whose entire content is a restatement of what the batch
file already says. The batch is now the authority, and the flag is checked
for agreement instead.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import main
from trialerror.lens.novelty import round_dir
from trialerror.stores.store import open_store

from tests._novelty_fixtures import build_round

SEED = "seed-batch-authority"

#: An R2 block is what makes this bite: R4 alone would load under the default
#: declaration too, and only the hash would have disagreed.
ARCHIVE_VOCABULARY = {
    "R2": {
        "labels": ["requested", "variant", "new"],
        "canonical": {"requested": "same", "variant": "variant", "new": "new-mechanism"},
    },
    "R4": {
        "labels": ["present", "adjacent", "absent"],
        "canonical": {"present": "stated", "adjacent": "adjacent", "absent": "absent"},
    },
}

CALIBRATION_PLANTS = [
    {"plant_id": "C-1", "kind": "area", "statement": "Calibration plant one, a rewritten archive row.",
     "expected_labels": {"R2": ["requested", "variant"]}},
    {"plant_id": "C-2", "kind": "custom", "statement": "Calibration plant two, unrelated to everything.",
     "expected_labels": {"R2": ["new"]}},
]


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


def _run(capsys, argv: list[str]) -> dict:
    exit_code = main(argv)
    envelope = json.loads(capsys.readouterr().out.strip())
    envelope["_exit_code"] = exit_code
    return envelope


def _screen_argv(program_root, round_id, *rest) -> list[str]:
    return ["lens", "--program-root", str(program_root), "screen", "--round-id", round_id, *rest]


def _batch_file(program_root, round_id, batch_id="judged-0") -> dict:
    return json.loads(
        (round_dir(program_root, round_id) / "judged" / f"{batch_id}.json").read_text(encoding="utf-8")
    )


@pytest.fixture()
def labels_file(tmp_path):
    path = tmp_path / "vocab.json"
    path.write_text(json.dumps(ARCHIVE_VOCABULARY), encoding="utf-8")
    return path


@pytest.fixture()
def archive_batch(cli_program_root, seeded_round, labels_file, capsys):
    """A judged batch declared against R2,R4 -- built WITH the flag, because
    that is the invocation that declares the instrument."""
    _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--judged-sets", "R2,R4", "--labels-file", str(labels_file), "--plants", "0",
        ),
    )
    assert env["ok"] is True, env
    return _batch_file(cli_program_root, seeded_round["round_id"])


def _sheet(tmp_path, batch, name="sheet.json"):
    sheet = {
        i: {"label_archive": "variant", "label_corpus": "present"} for i in batch["scope"]["scope"]
    }
    path = tmp_path / name
    path.write_text(json.dumps(sheet), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# --record-verdicts
# ---------------------------------------------------------------------------


def test_recording_against_an_r2_r4_batch_needs_no_repeat_of_the_flag(
    cli_program_root, seeded_round, archive_batch, labels_file, tmp_path, capsys
):
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"],
            "--record-verdicts", str(_sheet(tmp_path, archive_batch)),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["record_verdicts"]["labels_sha256"] == archive_batch["labels_sha256"]

    store = open_store(cli_program_root, platform_root=cli_program_root.parent / "platform_root")
    rows = store.knowledge.execute(
        "SELECT label, label_canonical FROM verdict WHERE label LIKE 'R2:variant%'"
    ).fetchall()
    store.close()
    assert rows, "the round's own R2 word was recorded"
    for row in rows:
        assert row["label_canonical"].startswith("R2:variant")


def test_the_batch_is_the_authority_with_no_labels_file_either(
    cli_program_root, seeded_round, archive_batch, tmp_path, capsys
):
    """No labels file on the command: the batch carries the vocabulary it
    was judged under, and the sets it declared. ``label_archive`` is accepted
    on a sheet that a run resolved against the default sets would have
    called a label for a set the batch never declared."""
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"],
            "--record-verdicts", str(_sheet(tmp_path, archive_batch)),
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["record_verdicts"]["labels_sha256"] == archive_batch["labels_sha256"]


def test_a_disagreeing_flag_is_refused_by_name(
    cli_program_root, seeded_round, archive_batch, labels_file, tmp_path, capsys
):
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"],
            "--record-verdicts", str(_sheet(tmp_path, archive_batch)),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
            "--judged-sets", "R3,R4",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "judged_sets_disagree"
    message = env["error"]["message"]
    assert "['R3', 'R4']" in message and "['R2', 'R4']" in message
    assert "--judged-sets" in message


def test_a_config_row_that_disagrees_is_refused_the_same_way(
    cli_program_root, seeded_round, archive_batch, labels_file, tmp_path, capsys
):
    (cli_program_root / "trialerror.toml").write_text(
        '[program]\nid = "fixture-program"\n\n[lens.novelty]\njudged_sets = ["R3", "R4"]\n',
        encoding="utf-8",
    )
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"],
            "--record-verdicts", str(_sheet(tmp_path, archive_batch)),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "judged_sets_disagree"


def test_an_agreeing_flag_is_still_accepted(
    cli_program_root, seeded_round, archive_batch, labels_file, tmp_path, capsys
):
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"],
            "--record-verdicts", str(_sheet(tmp_path, archive_batch)),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
            "--judged-sets", "R2,R4",
        ),
    )
    assert env["ok"] is True, env


# ---------------------------------------------------------------------------
# --record-calibration
# ---------------------------------------------------------------------------


@pytest.fixture()
def calibration_batch(cli_program_root, seeded_round, labels_file, tmp_path, capsys):
    plants = tmp_path / "plants.json"
    plants.write_text(json.dumps(CALIBRATION_PLANTS), encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--calibration", "--seed", SEED,
            "--plants-file", str(plants), "--batch-fail-on", "area",
            "--judged-sets", "R2,R4", "--labels-file", str(labels_file),
        ),
    )
    assert env["ok"] is True, env
    return _batch_file(cli_program_root, seeded_round["round_id"], "calibration-0")


def _judge_sheets(tmp_path):
    a, b = tmp_path / "judge-a.json", tmp_path / "judge-b.json"
    a.write_text(json.dumps({
        "C-1": {"label_archive": "requested", "label_corpus": "absent"},
        "C-2": {"label_archive": "new", "label_corpus": "absent"},
    }), encoding="utf-8")
    b.write_text(json.dumps({
        "C-1": {"label_archive": "variant", "label_corpus": "absent"},
        "C-2": {"label_archive": "new", "label_corpus": "adjacent"},
    }), encoding="utf-8")
    return a, b


def test_recording_a_calibration_needs_no_repeat_of_the_flag(
    cli_program_root, seeded_round, calibration_batch, labels_file, tmp_path, capsys
):
    a, b = _judge_sheets(tmp_path)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-calibration",
            "--judge-sheet-a", str(a), "--judge-sheet-b", str(b),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
        ),
    )
    assert env["ok"] is True, env
    card = env["result"]["record_calibration"]
    assert card["judged_sets"] == ["R2", "R4"]
    assert set(card["kappa_by_set"]) == {"R2", "R4"}
    assert card["labels_sha256"] == calibration_batch["labels_sha256"]


def test_a_disagreeing_flag_on_a_calibration_is_refused_by_name(
    cli_program_root, seeded_round, calibration_batch, labels_file, tmp_path, capsys
):
    a, b = _judge_sheets(tmp_path)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-calibration",
            "--judge-sheet-a", str(a), "--judge-sheet-b", str(b),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
            "--judged-sets", "R3,R4",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "judged_sets_disagree"
    assert "--record-calibration" in env["error"]["message"]


def test_a_run_that_records_nothing_still_honours_the_flag(
    cli_program_root, seeded_round, labels_file, capsys
):
    """The authority is the batch a run RECORDS against; a --judged-prep
    still declares its own instrument."""
    _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--judged-sets", "R2,R4", "--labels-file", str(labels_file), "--plants", "0",
            "--batch-id", "judged-7",
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["judged_prep"]["judged_sets"] == ["R2", "R4"]


# ---------------------------------------------------------------------------
# the scope of the resolution (stage-3, finding N2)
# ---------------------------------------------------------------------------


def test_one_invocation_that_preps_and_records_carries_one_declaration(
    cli_program_root, seeded_round, calibration_batch, labels_file, tmp_path, capsys
):
    """The resolution is the INVOCATION's, not the recording phase's, and
    that is a decision rather than an accident.

    A single ``lens screen`` may both prep and record. When it does, and when
    nothing declares the sets -- no ``--judged-sets``, no ``[lens.novelty]
    judged_sets`` -- the batch being RECORDED against also supplies the sets
    the new batch is PREPPED under, where before FB-6 the prep would have
    taken the CLI default. One invocation therefore ends up with one
    declaration instead of silently mixing two, which is the argument the
    batch-as-authority rule rests on in the first place."""
    _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    assert calibration_batch["judged_sets"] == ["R2", "R4"]
    a, b = _judge_sheets(tmp_path)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"],
            "--judged-prep", "--seed", SEED, "--plants", "0",
            "--record-calibration", "--judge-sheet-a", str(a), "--judge-sheet-b", str(b),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["record_calibration"]["judged_sets"] == ["R2", "R4"]
    assert env["result"]["judged_prep"]["judged_sets"] == ["R2", "R4"], (
        "the prep in a combined invocation reads the same declaration the recording does"
    )
    assert _batch_file(cli_program_root, seeded_round["round_id"])["judged_sets"] == ["R2", "R4"]


def test_a_combined_invocation_never_overrides_an_explicit_declaration(
    cli_program_root, seeded_round, calibration_batch, labels_file, tmp_path, capsys
):
    """The other half of the same decision: the batch steers only a run that
    declared nothing. A flag that disagrees is still refused by name, so no
    combined invocation can quietly prep under sets its operator did not
    ask for."""
    _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    a, b = _judge_sheets(tmp_path)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"],
            "--judged-prep", "--seed", SEED, "--plants", "0",
            "--record-calibration", "--judge-sheet-a", str(a), "--judge-sheet-b", str(b),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", str(labels_file),
            "--judged-sets", "R3,R4",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "judged_sets_disagree"
