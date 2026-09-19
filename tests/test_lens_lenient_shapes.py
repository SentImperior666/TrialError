"""Lane FB-6 item 6: lenient where a HUMAN writes the file.

Three files a person writes by hand refused a shape that said more than the
schema asked for, and each cost a live round a round-trip:

* ``--pair-ratings`` refused ``pair_id`` and ``why`` beside the three fields
  it reads. Now ignored, and NAMED in ``warnings`` -- because a misspelt
  ``human`` is also an extra key, and silence would drop a rating.
* the plants file refused ``literature``/``unlock``/``seeds``, the same three
  keys the round's own records carry, so the text had to be folded into each
  plant's statement by hand. Now they go under ``extra``, which takes any
  keys and reaches the judge as ONE text field.
* intake required a ``probe`` on an archived row -- a prior round's candidate
  written under a schema that had none -- so probes were invented for rows
  nothing will ever run. Now optional for ``archived`` and only there.

The line in every case: lenient about what a person ADDS, strict about what
a schema NEEDS.
"""

from __future__ import annotations

import pytest

from trialerror.lens.ideas import (
    ARCHIVED_OPTIONAL_RECORD_FIELDS,
    ARCHIVED_STATUS,
    intake_records,
    read_idea,
)
from trialerror.lens.novelty import (
    EXTERNAL_PLANT_FIELDS,
    NoveltyError,
    build_calibration_batch,
    load_external_plants,
    record_calibration,
    render_extra_text,
    run_mechanical_screen,
)

from tests._inventory_fixtures import bootstrap_launch, build_corpus_with_inventory
from tests._novelty_fixtures import build_round

SEED = "seed-lenient"


@pytest.fixture()
def screened(store):
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    return {**fixture, "dossiers": mechanical["dossiers"]}


# ---------------------------------------------------------------------------
# --pair-ratings
# ---------------------------------------------------------------------------


PLANTS = [
    {"plant_id": "C-1", "kind": "area", "statement": "Calibration plant one.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-2", "kind": "area", "statement": "Calibration plant two.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-3", "kind": "custom", "statement": "Calibration plant three.",
     "expected_labels": {"R3": ["new-mechanism"]}},
]

SHEET = {
    "C-1": {"label_inventory": "same"},
    "C-2": {"label_inventory": "variant"},
    "C-3": {"label_inventory": "new-mechanism"},
}


def _calibration(store, screened, tmp_path, plants=None, **kwargs):
    return build_calibration_batch(
        store, round_id=screened["round_id"], external_plants=plants or PLANTS, seed=SEED,
        dossiers=screened["dossiers"], batch_fail_on="area", out_dir=tmp_path, **kwargs,
    )


def test_a_pair_rating_may_carry_a_note_and_the_card_names_it(store, screened, tmp_path):
    batch = _calibration(store, screened, tmp_path)
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=SHEET, labels_b=dict(SHEET),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
        pair_ratings=[
            {"a": "C-1", "b": "C-2", "human": 0.8, "pair_id": "PR-1", "why": "both restate a row"},
            {"a": "C-1", "b": "C-3", "human": 0.1},
            {"a": "C-2", "b": "C-3", "human": 0.2},
        ],
    )
    assert card["r_embedding_human"]["n"] == 3, "the rating itself was read"
    assert len(card["warnings"]) == 1
    warning = card["warnings"][0]
    assert "pair ratings[0]" in warning
    assert "pair_id" in warning and "why" in warning


def test_a_card_with_nothing_ignored_carries_an_empty_warnings_list(store, screened, tmp_path):
    batch = _calibration(store, screened, tmp_path)
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=SHEET, labels_b=dict(SHEET),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
        pair_ratings=[{"a": "C-1", "b": "C-2", "human": 0.8}],
    )
    assert card["warnings"] == []


def test_a_pair_that_names_nothing_is_still_a_refusal(store, screened, tmp_path):
    """Lenient about extra keys is not lenient about a missing answer."""
    batch = _calibration(store, screened, tmp_path)
    with pytest.raises(NoveltyError) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch, labels_a=SHEET, labels_b=dict(SHEET),
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
            pair_ratings=[{"a": "C-1", "b": "NOT-A-THING", "human": 0.5}],
        )
    assert "NOT-A-THING" in str(exc.value)


def test_a_misspelt_human_rating_is_named_rather_than_silently_dropped(store, screened, tmp_path):
    batch = _calibration(store, screened, tmp_path)
    with pytest.raises(NoveltyError) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch, labels_a=SHEET, labels_b=dict(SHEET),
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
            pair_ratings=[{"a": "C-1", "b": "C-2", "humna": 0.8}],
        )
    assert "numeric 'human'" in str(exc.value)


# ---------------------------------------------------------------------------
# the plants file's extra block
# ---------------------------------------------------------------------------


def test_extra_is_a_declared_field_and_takes_any_keys():
    assert "extra" in EXTERNAL_PLANT_FIELDS
    plants = load_external_plants(
        [
            {
                "plant_id": "P-1", "kind": "area", "statement": "A plant.",
                "expected_labels": {"R3": ["same"]},
                "extra": {
                    "literature": "Nothing in the corpus states this.",
                    "unlock": "It would let a round drop the second pass.",
                    "seeds": ["SEED-1", "SEED-2"],
                },
            }
        ]
    )
    assert plants[0]["extra"]["unlock"]
    assert plants[0]["extra_text"] == (
        "literature: Nothing in the corpus states this.\n"
        "seeds: SEED-1; SEED-2\n"
        "unlock: It would let a round drop the second pass."
    )


def test_a_top_level_unknown_key_is_still_refused_and_names_extra():
    with pytest.raises(NoveltyError) as exc:
        load_external_plants(
            [
                {"plant_id": "P-1", "kind": "area", "statement": "A plant.",
                 "expected_labels": {"R3": ["same"]}, "literature": "..."},
            ]
        )
    assert "literature" in str(exc.value)
    assert "'extra'" in str(exc.value)


def test_extra_renders_deterministically_and_drops_what_says_nothing():
    assert render_extra_text(None) is None
    assert render_extra_text({}) is None
    assert render_extra_text({"unlock": "", "seeds": [], "why": None}) is None
    assert render_extra_text("a plain note") == "a plain note"
    assert render_extra_text({"b": "second", "a": "first"}) == "a: first\nb: second"
    assert render_extra_text({"n": 3, "obj": {"k": "v"}}) == 'n: 3\nobj: {"k": "v"}'


def test_a_non_object_extra_is_refused_with_the_plants_position():
    with pytest.raises(NoveltyError) as exc:
        load_external_plants(
            [
                {"plant_id": "P-1", "kind": "area", "statement": "A plant.",
                 "expected_labels": {"R3": ["same"]}, "extra": 7},
            ]
        )
    assert "P-1" in str(exc.value) and "extra" in str(exc.value)


def test_the_text_reaches_the_judges_envelope_as_one_field(store, screened, tmp_path):
    plants = [
        {"plant_id": "C-1", "kind": "area", "statement": "A plant with a round's own fields.",
         "expected_labels": {"R3": ["same", "variant"]},
         "extra": {"unlock": "A second pass becomes unnecessary."}},
        {"plant_id": "C-2", "kind": "area", "statement": "A plant with none.",
         "expected_labels": {"R3": ["same", "variant"]}},
    ]
    batch = _calibration(store, screened, tmp_path, plants=plants)
    by_id = {e["subject_id"]: e for e in batch["envelopes"]}
    assert by_id["C-1"]["record"]["extra_text"] == "unlock: A second pass becomes unnecessary."
    assert by_id["C-2"]["record"]["extra_text"] is None
    # ...and the KEY is on both, so the shape is not the tell.
    assert set(by_id["C-1"]["record"]) == set(by_id["C-2"]["record"])
    # The judge's masked view carries it too -- the views are built from the
    # envelopes, so this is what the judge is actually handed.
    view = next(v for v in batch["judge_views"] if batch["mask"][v["subject_id"]] == "C-1")
    assert view["record"]["extra_text"] == "unlock: A second pass becomes unnecessary."


# ---------------------------------------------------------------------------
# intake: probe on an archived row
# ---------------------------------------------------------------------------


@pytest.fixture()
def intake_program(store):
    corpus = build_corpus_with_inventory(store)
    launch = bootstrap_launch(store, purpose="ideation")
    return {"corpus": corpus, "launch": launch, "docs": corpus["corpus_doc_ids"][:1]}


def _record(**overrides):
    record = {
        "statement": "A prior round's candidate, written in as an archive row.",
        "provenance": {"docs": []},
    }
    record.update(overrides)
    return record


def test_an_archived_row_may_omit_its_probe(store, intake_program):
    assert ARCHIVED_OPTIONAL_RECORD_FIELDS == ("probe",)
    rows = intake_records(
        store, round_id="round-archive", author_launch=intake_program["launch"],
        records=[_record(provenance={"docs": intake_program["docs"]}, probe=None)],
        status=ARCHIVED_STATUS,
    )
    assert len(rows) == 1
    idea = read_idea(store, idea_id=rows[0]["idea_id"])
    assert idea["status"] == ARCHIVED_STATUS
    assert idea["probe"] is None


def test_an_archived_row_may_leave_the_key_out_entirely(store, intake_program):
    rows = intake_records(
        store, round_id="round-archive", author_launch=intake_program["launch"],
        records=[_record(provenance={"docs": intake_program["docs"]})],
        status=ARCHIVED_STATUS,
    )
    assert read_idea(store, idea_id=rows[0]["idea_id"])["probe"] is None


def test_a_record_that_declares_its_own_archived_status_counts_too(store, intake_program):
    rows = intake_records(
        store, round_id="round-archive", author_launch=intake_program["launch"],
        records=[_record(provenance={"docs": intake_program["docs"]}, status=ARCHIVED_STATUS)],
    )
    assert read_idea(store, idea_id=rows[0]["idea_id"])["status"] == ARCHIVED_STATUS


def test_a_non_archived_record_without_a_probe_is_still_refused(store, intake_program):
    with pytest.raises(ValueError) as exc:
        intake_records(
            store, round_id="round-live", author_launch=intake_program["launch"],
            records=[_record(provenance={"docs": intake_program["docs"]})],
        )
    assert "'probe'" in str(exc.value)
    assert ARCHIVED_STATUS in str(exc.value), "the refusal says which rows may omit it"


def test_an_archived_row_still_needs_its_statement_and_provenance(store, intake_program):
    for missing in ("statement", "provenance"):
        record = _record(provenance={"docs": intake_program["docs"]})
        record.pop(missing)
        with pytest.raises(ValueError) as exc:
            intake_records(
                store, round_id="round-archive", author_launch=intake_program["launch"],
                records=[record], status=ARCHIVED_STATUS,
            )
        assert missing in str(exc.value)


def test_the_whole_file_is_still_refused_as_one(store, intake_program):
    """Record 1 has no probe and is not archived: nothing lands, including
    record 0."""
    before = store.knowledge.execute("SELECT COUNT(*) AS n FROM idea").fetchone()["n"]
    with pytest.raises(ValueError):
        intake_records(
            store, round_id="round-live", author_launch=intake_program["launch"],
            records=[
                _record(provenance={"docs": intake_program["docs"]}, probe="A probe."),
                _record(provenance={"docs": intake_program["docs"]}),
            ],
        )
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM idea").fetchone()["n"] == before
