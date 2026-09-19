"""``trialerror lens screen`` — the CLI wiring over
:mod:`trialerror.lens.novelty`, driven through ``trialerror.cli.main``
exactly as a real invocation would be.

The three phases run in SEPARATE invocations by design (usually in different
launches, sometimes in different sittings), so the files the mechanical half
writes under the round are the handover between them. That is what these
tests actually exercise: not just each phase's envelope, but that phase two
can pick up what phase one left on disk.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import main
from trialerror.lens.ideas import read_idea
from trialerror.lens.novelty import neutral_abstract, round_dir
from trialerror.stores.store import open_store

from tests._novelty_fixtures import build_round

SEED = "seed-cli"


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


def _all_labels(batch) -> dict:
    """Every subject labelled, every plant caught."""
    labels = {
        i: {"label_inventory": "variant", "label_corpus": "adjacent"} for i in batch["scope"]["scope"]
    }
    for plant in batch["plants"]:
        labels[plant["plant_id"]] = {"label_inventory": "same"}
    return labels


def test_screen_with_no_phase_is_an_error_envelope(cli_program_root, capsys):
    env = _run(capsys, _screen_argv(cli_program_root, "round-1"))
    assert env["ok"] is False
    assert env["error"]["code"] == "no_phase"


def test_mechanical_reports_the_batch_shape_and_writes_the_dossiers(cli_program_root, seeded_round, capsys):
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    assert env["ok"] is True
    mechanical = env["result"]["mechanical"]
    assert mechanical["n_screened"] == len(seeded_round["idea_ids"])
    assert mechanical["n_merged"] == 1
    assert len(mechanical["flagged"]) == 1
    assert mechanical["collapse"]["mode"] == "descriptive"
    # the envelope reports the batch's SHAPE, not every dossier -- those are
    # on disk, where a reader can open one
    assert "dossiers" not in mechanical
    base = round_dir(cli_program_root, seeded_round["round_id"])
    assert len(list((base / "novelty").glob("*.json"))) == len(seeded_round["idea_ids"]) - 1
    assert (base / "adjudication.md").is_file()


def test_mechanical_points_at_the_judged_phase_next(cli_program_root, seeded_round, capsys):
    env = _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    assert any("judged-prep" in " ".join(a["argv"]) for a in env["nextActions"])


def test_alarms_arrive_as_json_and_arm_the_monitor(cli_program_root, seeded_round, capsys):
    alarms = json.dumps({"median_pairwise_cosine_max": 0.01})
    env = _run(
        capsys,
        _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical", "--alarms", alarms),
    )
    collapse = env["result"]["mechanical"]["collapse"]
    assert collapse["mode"] == "armed"
    assert collapse["flag"] is True


def test_an_unknown_external_query_mode_is_refused_by_the_parser(cli_program_root, seeded_round, capsys):
    with pytest.raises(SystemExit):
        main(_screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical", "--external-query-mode", "everything"))


def test_an_external_query_mode_with_no_provider_names_the_flag_that_fixes_it(
    cli_program_root, seeded_round, capsys
):
    """Both advertised modes used to be unreachable: the CLI constructed no
    provider, so `--external-query-mode neutral_abstract` always came back
    `screen_refused: names a query this call has no provider to issue` with
    no flag an operator could pass to clear it."""
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--external-query-mode", "neutral_abstract",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "external_provider_required"
    assert any("--external-provider" in " ".join(a["argv"]) for a in env["nextActions"])


def test_a_provider_with_no_query_mode_is_refused_rather_than_silently_idle(
    cli_program_root, seeded_round, capsys
):
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--external-provider", "arxiv-index",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "external_mode_required"


def test_the_mode_reaches_a_provider_and_the_dossiers_carry_its_hits(
    cli_program_root, seeded_round, tmp_path, capsys, monkeypatch
):
    """The provider seam, driven for real: a named provider is built, the
    query is issued under the chosen mode, R5 hits land in the dossiers on
    disk, and the egress is logged."""
    from trialerror.cli import lens as lens_cli
    from trialerror.lens.novelty import StaticExternalProvider

    provider = StaticExternalProvider(
        default=[{"id": "EXT-1", "title": "a prior result", "score": 0.71}], name="static-index"
    )
    closed: list[bool] = []
    monkeypatch.setattr(
        lens_cli, "_build_external_provider",
        lambda kind, program_root: (provider, lambda: closed.append(True)),
    )

    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--external-query-mode", "neutral_abstract", "--external-provider", "arxiv-index",
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    assert env["ok"] is True
    assert env["result"]["mechanical"]["reference_snapshot"]["R5"]["provider"] == "static-index"
    assert provider.calls and {q.mode for q in provider.calls} == {"neutral_abstract"}
    # what left the machine is the template over declared fields, not the
    # author's own statement -- checked against the records themselves
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    try:
        for query in provider.calls:
            idea = read_idea(store, idea_id=query.idea_id)
            assert query.text == neutral_abstract(idea)
            assert idea["body"] not in query.text
    finally:
        store.close()

    base = round_dir(cli_program_root, seeded_round["round_id"])
    dossiers = [json.loads(p.read_text(encoding="utf-8")) for p in (base / "novelty").glob("*.json")]
    assert dossiers
    assert all(d["candidate_hits"]["R5"] for d in dossiers)
    assert closed == [True], "whatever the provider opened is closed on the way out"


def test_an_unbuildable_provider_is_one_refusal_envelope_not_a_traceback(
    cli_program_root, seeded_round, capsys, monkeypatch
):
    from trialerror.cli import lens as lens_cli

    def _raise(kind, program_root):
        raise ValueError("no index db at <path>; build it first")

    monkeypatch.setattr(lens_cli, "_build_external_provider", _raise)
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--mechanical",
            "--external-query-mode", "statement", "--external-provider", "arxiv-index",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "external_provider_unavailable"
    assert "build it first" in env["error"]["message"]


def test_judged_prep_reads_the_dossiers_the_mechanical_half_left_on_disk(cli_program_root, seeded_round, capsys):
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    env = _run(
        capsys,
        _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED),
    )
    assert env["ok"] is True
    prep = env["result"]["judged_prep"]
    assert prep["n_plants"] == 10
    assert prep["n_envelopes"] == len(prep["scope"]["scope"]) + 10
    assert prep["second_judge"]
    assert json.loads(open(prep["batch_file"], encoding="utf-8").read())["seed"] == SEED


def test_the_llm_paraphrase_backend_is_declared_and_refused_never_silently_deterministic(
    cli_program_root, seeded_round, capsys
):
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--paraphrase-backend", "llm",
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "paraphrase_backend_unimplemented"
    assert "not implemented" in env["error"]["message"]


def test_the_default_paraphrase_backend_is_the_deterministic_one(cli_program_root, seeded_round, capsys):
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
            "--paraphrase-backend", "deterministic",
        ),
    )
    assert env["ok"] is True
    batch = json.loads(open(env["result"]["judged_prep"]["batch_file"], encoding="utf-8").read())
    paraphrases = [p for p in batch["plants"] if p["kind"] == "paraphrase"]
    assert paraphrases
    assert all(p["paraphrase_method"]["transformations"] for p in paraphrases)


def test_the_procedure_file_is_hashed_byte_exact_where_a_stripped_string_is_not(
    cli_program_root, seeded_round, tmp_path, capsys
):
    """The observed failure: `--executed-procedure "$(cat file)"` strips the
    file's trailing newline, the recomputed hash differs from the committed
    one, and a procedure that WAS followed is stamped non-compliant."""
    from trialerror.stores.store import open_store
    from trialerror.verify.prereg import commit_prereg

    procedure_text = "aiif-round-v2: stratify, diverge, screen, converge\n"
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    prereg = commit_prereg(store, title="round", procedure=procedure_text, params={})
    store.close()

    procedure_file = tmp_path / "procedure.md"
    procedure_file.write_text(procedure_text, encoding="utf-8")

    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    prep = _run(
        capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED)
    )["result"]["judged_prep"]
    batch = json.loads(open(prep["batch_file"], encoding="utf-8").read())
    labels_file = tmp_path / "labels.json"
    labels_file.write_text(json.dumps(_all_labels(batch)), encoding="utf-8")
    launch_id = seeded_round["launches"]["lens-1"]

    stripped = _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
        "--launch-id", launch_id, "--prereg-id", prereg["prereg_id"],
        "--executed-procedure", procedure_text.rstrip("\n"), "--executed-params", "{}",
    ))
    assert stripped["ok"] is True
    assert stripped["result"]["record_verdicts"]["prereg_compliant"] is False
    assert "procedure hash disagrees" in stripped["result"]["record_verdicts"]["prereg_compliance"]

    from_file = _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
        "--launch-id", launch_id, "--prereg-id", prereg["prereg_id"],
        "--executed-procedure-file", str(procedure_file), "--executed-params", "{}", "--supersede",
    ))
    assert from_file["ok"] is True
    assert from_file["result"]["record_verdicts"]["prereg_compliant"] is True


def test_naming_the_procedure_twice_is_refused(cli_program_root, seeded_round, capsys):
    env = _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
        "--executed-procedure", "p", "--executed-procedure-file", "p.md",
    ))
    assert env["ok"] is False
    assert env["error"]["code"] == "executed_procedure_ambiguous"


def test_judge_envelopes_out_writes_masked_views_and_nothing_else(
    cli_program_root, seeded_round, tmp_path, capsys
):
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    out_dir = tmp_path / "judge"
    env = _run(capsys, _screen_argv(
        cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED,
        "--judge-envelopes-out", str(out_dir),
    ))
    assert env["ok"] is True
    prep = env["result"]["judged_prep"]
    files = sorted(out_dir.glob("*.json"))
    assert len(files) == prep["n_envelopes"] == prep["n_judge_envelopes"]
    for path in files:
        assert path.stem.startswith("J-")
        text = path.read_text(encoding="utf-8")
        assert "PLANT-" not in text
        assert "IDEA-" not in text


def test_judged_prep_without_a_seed_is_refused(cli_program_root, seeded_round, capsys):
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    env = _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep"))
    assert env["ok"] is False
    assert env["error"]["code"] == "seed_required"


def test_judged_prep_before_the_mechanical_half_says_what_to_run_first(cli_program_root, seeded_round, capsys):
    env = _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED))
    assert env["ok"] is False
    assert env["error"]["code"] == "no_dossiers"
    assert any("--mechanical" in " ".join(a["argv"]) for a in env["nextActions"])


def test_record_verdicts_takes_the_labels_back_and_consolidates(cli_program_root, seeded_round, tmp_path, capsys):
    launch_id = seeded_round["launches"]["lens-1"]
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    prep = _run(
        capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED)
    )["result"]["judged_prep"]

    batch = json.loads(open(prep["batch_file"], encoding="utf-8").read())
    labels = {i: {"label_inventory": "variant", "label_corpus": "adjacent"} for i in batch["scope"]["scope"]}
    for plant in batch["plants"]:
        labels[plant["plant_id"]] = {"label_inventory": "same"}
    labels_file = tmp_path / "labels.json"
    labels_file.write_text(json.dumps(labels), encoding="utf-8")

    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
            "--launch-id", launch_id,
        ),
    )
    assert env["ok"] is True
    recorded = env["result"]["record_verdicts"]
    assert recorded["status"] == "screened"
    assert recorded["plants"]["catch_rate"] == 1.0
    # two rows per judged idea, one mechanical row per unjudged survivor --
    # every survivor is accounted for, none is left raw behind a retrieval
    # threshold and a seeded sample
    survivors = [*batch["scope"]["scope"], *batch["scope"]["unjudged"]]
    assert len(recorded["verdict_ids"]) == 2 * len(batch["scope"]["scope"]) + len(batch["scope"]["unjudged"])
    assert recorded["n_consolidated"] == len(survivors)
    assert recorded["n_consolidated_unjudged"] == len(batch["scope"]["unjudged"])
    assert recorded["unlabelled_scope"] == []
    # not stamped, and the envelope says why rather than the NULL being silent
    assert recorded["prereg_compliant"] is None
    assert "no prereg_id" in recorded["prereg_compliance"]

    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    try:
        statuses = {
            r["status"]
            for r in store.knowledge.execute(
                "SELECT status FROM idea WHERE idea_id IN ({})".format(",".join("?" for _ in survivors)),
                survivors,
            ).fetchall()
        }
    finally:
        store.close()
    assert statuses == {"consolidated"}


def test_a_second_record_verdicts_run_is_refused_unless_supersede_is_named(
    cli_program_root, seeded_round, tmp_path, capsys
):
    """One submission per idea per judge, reachable from the CLI: the second
    run is a refusal envelope naming the flag, and the flag makes the
    superseding rows say what they replace."""
    launch_id = seeded_round["launches"]["lens-1"]
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    prep = _run(
        capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED)
    )["result"]["judged_prep"]
    batch = json.loads(open(prep["batch_file"], encoding="utf-8").read())
    labels = {i: {"label_inventory": "variant", "label_corpus": "adjacent"} for i in batch["scope"]["scope"]}
    for plant in batch["plants"]:
        labels[plant["plant_id"]] = {"label_inventory": "same"}
    labels_file = tmp_path / "labels.json"
    labels_file.write_text(json.dumps(labels), encoding="utf-8")

    argv = _screen_argv(
        cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
        "--launch-id", launch_id,
    )
    first = _run(capsys, argv)
    assert first["ok"] is True

    refused = _run(capsys, argv)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "screen_refused"
    assert "supersede=True" in refused["error"]["message"]

    again = _run(capsys, [*argv, "--supersede"])
    assert again["ok"] is True
    assert set(again["result"]["record_verdicts"]["superseded"]) == set(
        first["result"]["record_verdicts"]["verdict_ids"]
    )


def test_record_verdicts_without_a_launch_id_is_refused(cli_program_root, seeded_round, tmp_path, capsys):
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED))
    labels_file = tmp_path / "labels.json"
    labels_file.write_text("{}", encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file)),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "launch_id_required"


def test_record_verdicts_with_no_judged_batch_on_file_is_refused(cli_program_root, seeded_round, tmp_path, capsys):
    labels_file = tmp_path / "labels.json"
    labels_file.write_text("{}", encoding="utf-8")
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
            "--launch-id", seeded_round["launches"]["lens-1"],
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "no_judged_batch"


def test_a_bad_label_comes_back_as_a_refusal_envelope_not_a_traceback(cli_program_root, seeded_round, tmp_path, capsys):
    launch_id = seeded_round["launches"]["lens-1"]
    _run(capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--mechanical"))
    prep = _run(
        capsys, _screen_argv(cli_program_root, seeded_round["round_id"], "--judged-prep", "--seed", SEED)
    )["result"]["judged_prep"]
    batch = json.loads(open(prep["batch_file"], encoding="utf-8").read())
    labels_file = tmp_path / "labels.json"
    labels_file.write_text(
        json.dumps({batch["scope"]["scope"][0]: {"label_inventory": "quite-new"}}), encoding="utf-8"
    )
    env = _run(
        capsys,
        _screen_argv(
            cli_program_root, seeded_round["round_id"], "--record-verdicts", str(labels_file),
            "--launch-id", launch_id,
        ),
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "screen_refused"
    assert "label_inventory" in env["error"]["message"]
