"""Lane FB-5: the instrument's CLI surface -- ``--judged-sets``,
``--labels-file``, ``--plants-file``, ``--calibration``,
``--record-calibration`` -- and the ``[lens.novelty]`` config rows behind
them, driven through ``trialerror.cli.main`` exactly as a real invocation
would be.

Separate from ``tests/test_lens_screen_cli.py`` for the reason that module's
own header gives: it is about the three phases handing work to each other
across invocations, and this one is about which instrument each invocation
declares.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trialerror.cli import main
from trialerror.lens.novelty import round_dir
from trialerror.stores.store import open_store

from tests._novelty_fixtures import build_round

SEED = "seed-instrument-cli"


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


def _mechanical(capsys, cli_program_root, seeded_round) -> dict:
    return _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )


def _write_config(program_root, body: str) -> None:
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "fixture-program"\n\n' + body, encoding="utf-8"
    )


def _batch_file(program_root, round_id, batch_id="judged-0") -> dict:
    path = round_dir(program_root, round_id) / "judged" / f"{batch_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# item 1 -- --judged-sets, and [lens.novelty] judged_sets behind it
# ---------------------------------------------------------------------------


def test_judged_prep_with_no_flag_declares_the_default_sets(cli_program_root, seeded_round, capsys):
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED),
    )
    assert env["ok"] is True, env
    assert env["result"]["judged_prep"]["judged_sets"] == ["R3", "R4"]


def test_the_flag_declares_the_sets_and_the_batch_file_records_them(
    cli_program_root, seeded_round, capsys
):
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--judged-sets", "R2,R4", "--plants", "0",
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["judged_prep"]["judged_sets"] == ["R2", "R4"]
    batch = _batch_file(cli_program_root, seeded_round["round_id"])
    assert batch["judged_sets"] == ["R2", "R4"]
    assert all("inventory_rows" not in e for e in batch["envelopes"])
    assert all("archive_rows" in e for e in batch["envelopes"])


def test_the_config_row_declares_the_sets_when_no_flag_does(cli_program_root, seeded_round, capsys):
    _write_config(cli_program_root, '[lens.novelty]\njudged_sets = ["R2", "R4"]\n')
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED, "--plants", "0",
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["judged_prep"]["judged_sets"] == ["R2", "R4"]


def test_the_flag_overrides_the_config_row(cli_program_root, seeded_round, capsys):
    _write_config(cli_program_root, '[lens.novelty]\njudged_sets = ["R2"]\n')
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--judged-sets", "R3,R4",
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["judged_prep"]["judged_sets"] == ["R3", "R4"]


def test_an_unknown_set_is_an_error_envelope_not_a_traceback(cli_program_root, seeded_round, capsys):
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--judged-sets", "R3,R7",
        ),
    )
    assert env["ok"] is False
    assert "R7" in env["error"]["message"]


def test_recording_under_sets_the_batch_did_not_declare_is_refused(
    cli_program_root, seeded_round, tmp_path, capsys
):
    _mechanical(capsys, cli_program_root, seeded_round)
    _run(
        capsys,
        _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED),
    )
    batch = _batch_file(cli_program_root, seeded_round["round_id"])
    labels = {i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]}
    labels_file = tmp_path / "labels.json"
    labels_file.write_text(json.dumps(labels), encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
            "--launch-id", seeded_round["launches"]["lens-1"], "--judged-sets", "R2,R4",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "judged_sets_disagree"


# ---------------------------------------------------------------------------
# item 2 -- --labels-file, and [lens.novelty] labels_file behind it
# ---------------------------------------------------------------------------


ROUND_VOCABULARY = {
    "R4": {
        "labels": ["present", "adjacent", "absent"],
        "canonical": {"present": "stated", "adjacent": "adjacent", "absent": "absent"},
    }
}


def _write_labels(path, obj=None) -> str:
    path.write_text(json.dumps(obj if obj is not None else ROUND_VOCABULARY), encoding="utf-8")
    return str(path)


def test_the_labels_file_reaches_the_judge_and_the_batch_records_its_hash(
    cli_program_root, seeded_round, tmp_path, capsys
):
    labels = _write_labels(tmp_path / "vocab.json")
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--labels-file", labels,
        ),
    )
    assert env["ok"] is True, env
    batch = _batch_file(cli_program_root, seeded_round["round_id"])
    assert batch["labels_sha256"] == batch["label_vocabularies"]["sha256"]
    for envelope in batch["envelopes"]:
        assert envelope["labels"]["literature"] == ["present", "adjacent", "absent"]
        # R3 was declared and not re-spelled, so it keeps the design's.
        assert envelope["labels"]["inventory"] == [
            "same", "variant", "recombination", "new-mechanism", "unscreenable",
        ]


def test_a_partial_canonical_mapping_is_an_error_envelope(
    cli_program_root, seeded_round, tmp_path, capsys
):
    labels = _write_labels(
        tmp_path / "partial.json",
        {"R4": {"labels": ["present", "absent"], "canonical": {"present": "stated"}}},
    )
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--labels-file", labels,
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "labels_file_refused"
    assert "absent" in env["error"]["message"]


def test_the_config_labels_file_is_read_against_the_program_root(
    cli_program_root, seeded_round, capsys
):
    _write_labels(cli_program_root / "vocab.json")
    _write_config(cli_program_root, '[lens.novelty]\nlabels_file = "vocab.json"\n')
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED),
    )
    assert env["ok"] is True, env
    batch = _batch_file(cli_program_root, seeded_round["round_id"])
    assert batch["label_vocabularies"]["sets"]["R4"]["labels"] == ["present", "adjacent", "absent"]


def test_round_labels_are_recorded_with_their_canonical_labels_beside_them(
    cli_program_root, seeded_round, tmp_path, capsys
):
    labels_file = _write_labels(tmp_path / "vocab.json")
    _mechanical(capsys, cli_program_root, seeded_round)
    _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--labels-file", labels_file,
        ),
    )
    batch = _batch_file(cli_program_root, seeded_round["round_id"])
    sheet = {i: {"label_inventory": "variant", "label_corpus": "present"} for i in batch["scope"]["scope"]}
    for plant in batch["plants"]:
        sheet[plant["plant_id"]] = {"label_inventory": "same"}
    sheet_file = tmp_path / "sheet.json"
    sheet_file.write_text(json.dumps(sheet), encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-verdicts", str(sheet_file),
            "--launch-id", seeded_round["launches"]["lens-1"], "--labels-file", labels_file,
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["record_verdicts"]["labels_sha256"] == batch["labels_sha256"]

    store = open_store(cli_program_root, platform_root=cli_program_root.parent / "platform_root")
    rows = store.knowledge.execute(
        "SELECT label, label_canonical FROM verdict WHERE label LIKE 'R4:%'"
    ).fetchall()
    store.close()
    assert rows
    for row in rows:
        assert row["label"].startswith("R4:present")
        assert row["label_canonical"].startswith("R4:stated")


# ---------------------------------------------------------------------------
# item 3 -- --plants-file and --batch-fail-on
# ---------------------------------------------------------------------------


EXTERNAL_PLANTS = [
    {
        "plant_id": "P-area-1",
        "kind": "area",
        "statement": "A request row restated as a mechanism: the shared pool refills on a fixed cadence.",
        "expected_labels": {"R3": ["same", "variant"]},
    },
    {
        "plant_id": "P-custom-1",
        "kind": "custom",
        "statement": "An unrelated mechanism nothing in the reference sets states.",
        "expected_labels": {"R3": ["new-mechanism"]},
    },
]


def test_the_plants_file_seeds_the_rounds_own_plants_only(
    cli_program_root, seeded_round, tmp_path, capsys
):
    plants_file = tmp_path / "plants.json"
    plants_file.write_text(json.dumps(EXTERNAL_PLANTS), encoding="utf-8")
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--plants-file", str(plants_file), "--plants", "0", "--batch-fail-on", "area",
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["judged_prep"]["n_plants"] == 2
    assert env["result"]["judged_prep"]["batch_fail_on"] == ["area"]
    batch = _batch_file(cli_program_root, seeded_round["round_id"])
    assert {p["plant_id"] for p in batch["plants"]} == {"P-area-1", "P-custom-1"}
    assert "P-area-1" not in json.dumps(batch["judge_views"])


def test_a_malformed_plants_file_is_an_error_envelope(
    cli_program_root, seeded_round, tmp_path, capsys
):
    plants_file = tmp_path / "bad.json"
    plants_file.write_text(json.dumps([{"kind": "area", "statement": "x"}]), encoding="utf-8")
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--plants-file", str(plants_file), "--plants", "0",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "plants_file_refused"


def test_an_unknown_fail_kind_is_an_error_envelope(cli_program_root, seeded_round, capsys):
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--batch-fail-on", "sideways",
        ),
    )
    assert env["ok"] is False
    assert "sideways" in env["error"]["message"]


def test_the_config_rows_name_the_plants_file_and_the_fail_kinds(
    cli_program_root, seeded_round, capsys
):
    (cli_program_root / "plants.json").write_text(json.dumps(EXTERNAL_PLANTS), encoding="utf-8")
    _write_config(
        cli_program_root,
        '[lens.novelty]\nplants_file = "plants.json"\nbatch_fail_on = ["area", "custom"]\n',
    )
    _mechanical(capsys, cli_program_root, seeded_round)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED, "--plants", "0",
        ),
    )
    assert env["ok"] is True, env
    assert env["result"]["judged_prep"]["batch_fail_on"] == ["area", "custom"]
    assert env["result"]["judged_prep"]["n_plants"] == 2


def test_a_miss_on_a_failing_kind_reopens_the_batch_through_the_cli(
    cli_program_root, seeded_round, tmp_path, capsys
):
    plants_file = tmp_path / "plants.json"
    plants_file.write_text(json.dumps(EXTERNAL_PLANTS), encoding="utf-8")
    _mechanical(capsys, cli_program_root, seeded_round)
    _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--plants-file", str(plants_file), "--plants", "0", "--batch-fail-on", "area",
        ),
    )
    batch = _batch_file(cli_program_root, seeded_round["round_id"])
    sheet = {i: {"label_inventory": "variant", "label_corpus": "adjacent"} for i in batch["scope"]["scope"]}
    sheet["P-area-1"] = {"label_inventory": "new-mechanism"}
    sheet["P-custom-1"] = {"label_inventory": "new-mechanism"}
    sheet_file = tmp_path / "sheet.json"
    sheet_file.write_text(json.dumps(sheet), encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-verdicts", str(sheet_file),
            "--launch-id", seeded_round["launches"]["lens-1"], "--batch-fail-on", "area",
        ),
    )
    assert env["ok"] is True, env
    recorded = env["result"]["record_verdicts"]
    assert recorded["status"] == "reopened_with_caveat"
    assert recorded["plants"]["failures"] == ["P-area-1"]
    assert recorded["plants"]["by_kind"]["custom"]["catch_rate"] == 1.0


# ---------------------------------------------------------------------------
# item 6 -- --calibration and --record-calibration
# ---------------------------------------------------------------------------


CALIBRATION_PLANTS = [
    {"plant_id": "C-1", "kind": "area", "statement": "Calibration plant one, a rewritten reference row.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-2", "kind": "area", "statement": "Calibration plant two, another rewritten row.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-3", "kind": "custom", "statement": "Calibration plant three, unrelated to everything.",
     "expected_labels": {"R3": ["new-mechanism"]}},
]


def _calibration_argv(cli_program_root, seeded_round, plants_file, *rest):
    return _screen_argv(
        cli_program_root, seeded_round["round_id"], "--calibration", "--seed", SEED,
        "--plants-file", str(plants_file), "--batch-fail-on", "area", *rest,
    )


def _write_plants(tmp_path):
    path = tmp_path / "calibration-plants.json"
    path.write_text(json.dumps(CALIBRATION_PLANTS), encoding="utf-8")
    return path


def test_calibration_builds_a_batch_of_plants_before_any_record_exists(
    cli_program_root, seeded_round, tmp_path, capsys
):
    # No --mechanical first: a calibration exists for exactly this moment.
    plants_file = _write_plants(tmp_path)
    env = _run(capsys, _calibration_argv(cli_program_root, seeded_round, plants_file))
    assert env["ok"] is True, env
    block = env["result"]["calibration"]
    assert block["batch_id"] == "calibration-0"
    assert block["n_plants"] == 3
    # Lane FB-6 item 5: over the plants, because there are no records yet.
    assert block["baseline_cosine_distribution"]["over"] == "plants"
    assert block["baseline_cosine_distribution"]["n"] == 3
    path = round_dir(cli_program_root, seeded_round["round_id"]) / "judged" / "calibration-0.json"
    assert path.is_file()
    assert "C-1" not in json.dumps(json.loads(path.read_text(encoding="utf-8"))["judge_views"])
    assert any("--record-calibration" in " ".join(a["argv"]) for a in env["nextActions"])


def test_calibration_with_no_seed_is_refused(cli_program_root, seeded_round, tmp_path, capsys):
    plants_file = _write_plants(tmp_path)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--calibration",
            "--plants-file", str(plants_file),
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "seed_required"


def test_calibration_with_no_plants_file_is_refused(cli_program_root, seeded_round, capsys):
    env = _run(
        capsys,
        _screen_argv(cli_program_root, seeded_round["round_id"], "--calibration", "--seed", SEED),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "plants_file_required"


def test_record_calibration_needs_two_judges(cli_program_root, seeded_round, tmp_path, capsys):
    sheet = tmp_path / "a.json"
    sheet.write_text(json.dumps({"C-1": {"label_inventory": "same"}}), encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-calibration",
            "--judge-sheet-a", str(sheet), "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "two_judges_required"


def test_record_calibration_with_no_batch_on_file_says_so(cli_program_root, seeded_round, tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for path in (a, b):
        path.write_text(json.dumps({"C-1": {"label_inventory": "same"}}), encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-calibration",
            "--judge-sheet-a", str(a), "--judge-sheet-b", str(b),
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "no_calibration_batch"


def test_two_judge_sheets_through_the_cli_yield_the_card(
    cli_program_root, seeded_round, tmp_path, capsys
):
    plants_file = _write_plants(tmp_path)
    _mechanical(capsys, cli_program_root, seeded_round)
    _run(capsys, _calibration_argv(cli_program_root, seeded_round, plants_file))

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(
        json.dumps({
            "C-1": {"label_inventory": "same"},
            "C-2": {"label_inventory": "variant"},
            "C-3": {"label_inventory": "new-mechanism"},
        }),
        encoding="utf-8",
    )
    b.write_text(
        json.dumps({
            "C-1": {"label_inventory": "same"},
            "C-2": {"label_inventory": "new-mechanism"},
            "C-3": {"label_inventory": "new-mechanism"},
        }),
        encoding="utf-8",
    )
    ratings = tmp_path / "pairs.json"
    ratings.write_text(
        json.dumps([
            {"a": "C-1", "b": "C-2", "human": 0.8},
            {"a": "C-1", "b": "C-3", "human": 0.2},
            {"a": "C-2", "b": "C-3", "human": 0.4},
        ]),
        encoding="utf-8",
    )
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-calibration",
            "--judge-sheet-a", str(a), "--judge-sheet-b", str(b), "--pair-ratings", str(ratings),
            "--launch-id", seeded_round["launches"]["lens-1"], "--batch-fail-on", "area",
        ),
    )
    assert env["ok"] is True, env
    card = env["result"]["record_calibration"]
    assert set(card["kappa_by_set"]) == {"R3", "R4"}
    assert card["kappa_by_set"]["R3"]["n"] == 3
    assert card["catch_by_kind"]["a"]["area"]["catch_rate"] == 1.0
    assert card["catch_by_kind"]["b"]["area"]["catch_rate"] == 0.5
    assert card["r_embedding_human"]["n"] == 3
    assert card["misses_by_id"]["C-2"]["b"]["label"] == "new-mechanism"
    # The baseline rides on the card, read off the dossiers the mechanical
    # half wrote.
    assert card["baseline_cosine_distribution"]["n_records"] > 0
    assert Path(card["card_file"]).is_file()
    assert card["verdict_ids"]

    store = open_store(cli_program_root, platform_root=cli_program_root.parent / "platform_root")
    versions = {
        r["procedure_version"]
        for r in store.knowledge.execute("SELECT procedure_version FROM verdict")
    }
    ideas = {r["status"] for r in store.knowledge.execute("SELECT status FROM idea")}
    store.close()
    assert versions == {"novelty-v2-calibration"}
    # `merged` is the mechanical half's own near-duplicate fold on this
    # fixture; what a calibration must never produce is a CONSOLIDATED row.
    assert ideas <= {"raw", "merged"}, "a calibration consolidates nothing"
