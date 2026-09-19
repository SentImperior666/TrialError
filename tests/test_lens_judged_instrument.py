"""Lane FB-5: the judged screen as a configurable INSTRUMENT.

The screen used to be one instrument with no knobs -- the judge saw the
nearest R3 inventory rows and the R4/R5 corpus passages, labelled them from
two fixed vocabularies, and the plants were built by the harness out of R3
rows and the batch's own records. This module is about everything that is
now declared per round instead: which reference sets a judge is shown
(item 1), what the round calls its labels and what they map onto (item 2),
which plants the round seeds (item 3), what an archive round is (item 4),
the seed work behind an ``unscreenable`` (item 5) and the calibration a
round runs before it has a single record (item 6).

Every test here holds the same line the brief's first sentence does: the
DEFAULT -- no declared sets, no label file, no plants file -- is the
instrument as it was, and the module's own existing suite
(``tests/test_lens_novelty_judged.py``) is what proves that half.
"""

from __future__ import annotations

import json

import pytest

from trialerror.lens.ideas import ARCHIVED_STATUS, IDEA_STATUSES, intake_records, read_idea
from trialerror.lens.novelty import (
    DEFAULT_JUDGED_SETS,
    DEFAULT_SEED_LABELS,
    INVENTORY_LABELS,
    MIN_ON_TOPIC_SEEDS,
    LITERATURE_LABELS,
    REFERENCE_SETS,
    REFERENCE_SET_ROWS,
    CALIBRATION_PROCEDURE_VERSION,
    NoveltyError,
    baseline_cosine_distribution,
    build_calibration_batch,
    build_judged_batch,
    load_external_plants,
    load_label_vocabularies,
    normalize_fail_kinds,
    normalize_judged_sets,
    normalize_seeds,
    on_topic_seed_label,
    pearson_r,
    record_calibration,
    record_novelty_verdicts,
    run_mechanical_screen,
    score_plants,
)

from tests._novelty_fixtures import build_round

SEED = "seed-instrument"


@pytest.fixture()
def screened(store):
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    return {**fixture, "mechanical": mechanical, "dossiers": mechanical["dossiers"]}


def _batch(store, screened, **kwargs):
    kwargs.setdefault("plants_per_kind", 0)
    return build_judged_batch(
        store, round_id=screened["round_id"], dossiers=screened["dossiers"], seed=SEED, **kwargs
    )


# ---------------------------------------------------------------------------
# item 1 -- declared reference sets
# ---------------------------------------------------------------------------


def test_the_default_declaration_is_the_instrument_as_it_was():
    assert DEFAULT_JUDGED_SETS == ("R3", "R4")
    assert normalize_judged_sets(None) == ("R3", "R4")


def test_declared_sets_are_put_in_canonical_order_however_they_were_typed():
    assert normalize_judged_sets("R4,R2") == ("R2", "R4")
    assert normalize_judged_sets(["r4", "r2"]) == ("R2", "R4")
    assert normalize_judged_sets(("R2", "R3", "R4")) == ("R2", "R3", "R4")


def test_a_set_nobody_declared_is_refused_by_name():
    with pytest.raises(NoveltyError) as exc:
        normalize_judged_sets("R3,R9")
    assert "R9" in str(exc.value)
    with pytest.raises(NoveltyError) as empty:
        normalize_judged_sets([])
    assert "at least one" in str(empty.value)


def test_r5_is_not_declarable_because_it_is_evidence_for_the_r4_label():
    assert "R5" not in REFERENCE_SETS
    with pytest.raises(NoveltyError):
        normalize_judged_sets("R5")


def test_a_default_batch_carries_the_two_sets_it_always_carried(store, screened):
    batch = _batch(store, screened)
    assert batch["judged_sets"] == ["R3", "R4"]
    for envelope in batch["envelopes"]:
        assert set(envelope) >= {"inventory_rows", "retrieved"}
        assert "archive_rows" not in envelope
        assert set(envelope["labels"]) == {"inventory", "literature"}


def test_a_round_that_declares_r2_and_r4_gets_archive_rows_and_no_inventory_rows(store, screened):
    batch = _batch(store, screened, judged_sets="R2,R4")
    assert batch["judged_sets"] == ["R2", "R4"]
    assert batch["envelopes"], "the fixture round has a judged scope"
    for envelope in batch["envelopes"]:
        assert "archive_rows" in envelope
        assert "retrieved" in envelope
        # An undeclared set is ABSENT, not empty: an empty list reads as
        # "consulted and held nothing", which is a different sentence.
        assert "inventory_rows" not in envelope
        assert set(envelope["labels"]) == {"archive", "literature"}


def test_an_archive_row_carries_the_four_fields_a_judge_needs_and_its_similarity(store, screened):
    batch = _batch(store, screened, judged_sets="R2")
    rows = [row for envelope in batch["envelopes"] for row in envelope["archive_rows"]]
    assert rows, "the fixture's own records are the archive this round is judged against"
    for row in rows:
        assert set(row) == {"idea_id", "round_id", "status", "statement", "similarity"}
        assert isinstance(row["similarity"], float)


def test_no_envelope_is_handed_its_own_record_as_an_archive_row(store, screened):
    batch = _batch(store, screened, judged_sets="R2,R4")
    for envelope in batch["envelopes"]:
        subject = envelope["subject_id"]
        assert subject not in {row["idea_id"] for row in envelope["archive_rows"]}


def test_verdict_rows_are_written_only_for_the_declared_sets(store, screened):
    batch = _batch(store, screened, judged_sets="R2,R4")
    labels = {
        idea_id: {"label_archive": "variant", "label_corpus": "adjacent"}
        for idea_id in batch["scope"]["scope"]
    }
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["judged_sets"] == ["R2", "R4"]
    sets_written = {row["label"].split(":", 1)[0] for row in recorded["verdicts"]}
    assert sets_written <= {"R2", "R4"}
    assert "R3" not in sets_written
    judged = {
        row["label"].split(":", 1)[0]
        for row in recorded["verdicts"]
        if row["subject_id"] in set(batch["scope"]["scope"])
    }
    assert judged == {"R2", "R4"}


def test_a_label_for_an_undeclared_set_is_refused_by_name(store, screened):
    batch = _batch(store, screened, judged_sets="R2,R4")
    idea_id = batch["scope"]["scope"][0]
    with pytest.raises(NoveltyError) as exc:
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch,
            labels={idea_id: {"label_inventory": "variant"}},
            issued_by_launch=screened["launches"]["lens-1"],
        )
    assert "label_inventory" in str(exc.value)
    assert "R3" in str(exc.value)


def test_an_unjudged_record_files_its_row_under_a_declared_set(store, screened):
    batch = _batch(store, screened, judged_sets="R2,R4")
    if not batch["scope"]["unjudged"]:
        pytest.skip("this fixture round left nothing outside the judged scope")
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch,
        labels={i: {"label_archive": "variant"} for i in batch["scope"]["scope"]},
        issued_by_launch=screened["launches"]["lens-1"],
    )
    unjudged = set(batch["scope"]["unjudged"])
    rows = [row for row in recorded["verdicts"] if row["subject_id"] in unjudged]
    assert rows
    for row in rows:
        assert row["label"].startswith("R2:")


def test_the_reference_set_tables_agree_with_each_other():
    assert set(REFERENCE_SETS) == set(REFERENCE_SET_ROWS)
    assert REFERENCE_SET_ROWS["R3"] == "inventory_rows"
    assert REFERENCE_SET_ROWS["R4"] == "retrieved"
    assert REFERENCE_SETS["R3"] == "label_inventory"


def test_an_r2_verdict_cites_archive_rows_and_never_a_chunk_id(store, screened):
    batch = _batch(store, screened, judged_sets="R2")
    idea_id = batch["scope"]["scope"][0]
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch,
        labels={i: {"label_archive": "same"} for i in batch["scope"]["scope"]},
        issued_by_launch=screened["launches"]["lens-1"],
    )
    row = next(r for r in recorded["verdicts"] if r["subject_id"] == idea_id)
    evidence = json.loads(row["evidence"])
    cited = [e for e in evidence if "R2 archive row" in str(e.get("note"))]
    assert cited, "an R2 verdict names the archive rows its judge was shown"
    assert not any(e.get("chunk_id") for e in evidence)


def test_the_record_still_reads_the_same_whatever_the_round_declared(store, screened):
    idea_id = sorted(screened["dossiers"])[0]
    idea = read_idea(store, idea_id=idea_id)
    default = _batch(store, screened)
    archive = _batch(store, screened, judged_sets="R2")
    by_id = {e["subject_id"]: e for e in default["envelopes"]}
    other = {e["subject_id"]: e for e in archive["envelopes"]}
    shared = set(by_id) & set(other)
    assert shared
    for subject in shared:
        assert by_id[subject]["record"] == other[subject]["record"]
    assert idea is not None


def test_the_design_vocabularies_are_unchanged():
    assert INVENTORY_LABELS == ("same", "variant", "recombination", "new-mechanism", "unscreenable")
    assert LITERATURE_LABELS == ("stated", "implied", "adjacent", "absent")


# ---------------------------------------------------------------------------
# item 2 -- per-round label vocabularies with a canonical mapping
# ---------------------------------------------------------------------------


ROUND_VOCABULARY = {
    "R2": {
        "labels": ["requested", "variant", "new"],
        "canonical": {"requested": "same", "variant": "variant", "new": "new-mechanism"},
    },
    "R4": {
        "labels": ["present", "adjacent", "absent"],
        "canonical": {"present": "stated", "adjacent": "adjacent", "absent": "absent"},
    },
    "extra": {"seed": ["on-topic", "off-topic"]},
    "unscreenable": "unscreenable",
}


def _vocabulary(*sets):
    """ROUND_VOCABULARY cut down to the sets a round DECLARES -- a block for
    an undeclared set is a refusal by name (stage 3, N4)."""
    return {
        key: value for key, value in ROUND_VOCABULARY.items()
        if key not in REFERENCE_SETS or key in sets
    }


def test_a_round_that_declares_nothing_keeps_the_design_vocabulary():
    resolved = load_label_vocabularies(None)
    assert resolved["sets"]["R3"]["labels"] == list(INVENTORY_LABELS)
    assert resolved["sets"]["R4"]["canonical"] == {label: label for label in LITERATURE_LABELS}
    assert resolved["seeds"] == list(DEFAULT_SEED_LABELS)
    assert resolved["unscreenable"] == "unscreenable"


def test_a_declared_set_the_file_says_nothing_about_keeps_its_own_vocabulary():
    resolved = load_label_vocabularies({"R4": ROUND_VOCABULARY["R4"]}, judged_sets="R3,R4")
    assert resolved["sets"]["R3"]["labels"] == list(INVENTORY_LABELS)
    assert resolved["sets"]["R4"]["labels"] == ["present", "adjacent", "absent"]


def test_the_vocabulary_hash_moves_with_the_vocabulary_and_not_with_the_typing():
    a = load_label_vocabularies(ROUND_VOCABULARY, judged_sets="R2,R4")
    b = load_label_vocabularies(dict(reversed(list(ROUND_VOCABULARY.items()))), judged_sets="R4,R2")
    assert a["sha256"] == b["sha256"]
    # and the unscreenable word alone moves it. A round that re-spells that
    # word has to offer the judge the spelling (stage 3, N3), so the two
    # vocabularies compared here differ in nothing else.
    offering = {
        **ROUND_VOCABULARY,
        "R2": {
            "labels": ["requested", "variant", "new", "vague"],
            "canonical": {**ROUND_VOCABULARY["R2"]["canonical"], "vague": "unscreenable"},
        },
    }
    c = load_label_vocabularies(offering, judged_sets="R2,R4")
    d = load_label_vocabularies({**offering, "unscreenable": "vague"}, judged_sets="R2,R4")
    assert c["sha256"] != d["sha256"]


def test_a_partial_canonical_mapping_is_refused_and_names_the_unmapped_label():
    partial = {
        "R4": {
            "labels": ["present", "adjacent", "absent"],
            "canonical": {"present": "stated", "adjacent": "adjacent"},
        }
    }
    with pytest.raises(NoveltyError) as exc:
        load_label_vocabularies(partial, judged_sets="R4")
    assert "absent" in str(exc.value)
    assert "TOTAL" in str(exc.value)


def test_a_canonical_value_outside_the_design_vocabulary_is_refused():
    bad = {"R4": {"labels": ["present"], "canonical": {"present": "obviously-known"}}}
    with pytest.raises(NoveltyError) as exc:
        load_label_vocabularies(bad, judged_sets="R4")
    assert "obviously-known" in str(exc.value)


def test_a_mapping_for_a_label_the_judge_is_never_offered_is_refused():
    bad = {"R4": {"labels": ["present"], "canonical": {"present": "stated", "ghost": "absent"}}}
    with pytest.raises(NoveltyError) as exc:
        load_label_vocabularies(bad, judged_sets="R4")
    assert "ghost" in str(exc.value)


def test_a_labels_file_key_nothing_reads_is_refused():
    with pytest.raises(NoveltyError) as exc:
        load_label_vocabularies({"R9": {"labels": ["x"], "canonical": {"x": "same"}}}, judged_sets="R4")
    assert "R9" in str(exc.value)
    with pytest.raises(NoveltyError):
        load_label_vocabularies({"extra": {"mood": ["good"]}}, judged_sets="R4")


def test_a_block_for_an_undeclared_reference_set_is_refused_by_name():
    """Stage 3, N4: dropped silently, a set-name typo left the judge on the
    design's words for the set it WAS shown and only surfaced when
    --record-verdicts refused the answers. The plants loader already refuses
    the same mistake by name; the two loaders now agree."""
    with pytest.raises(NoveltyError) as exc:
        load_label_vocabularies(
            {"R3": {"labels": ["x"], "canonical": {"x": "same"}}}, judged_sets="R2,R4"
        )
    assert "R3" in str(exc.value)
    assert "R2" in str(exc.value) and "R4" in str(exc.value)
    # declaring the set is what makes the block a vocabulary for somebody
    resolved = load_label_vocabularies(
        {"R3": {"labels": ["x"], "canonical": {"x": "same"}}}, judged_sets="R3,R4"
    )
    assert resolved["sets"]["R3"]["labels"] == ["x"]


def test_a_repeated_label_name_is_refused():
    with pytest.raises(NoveltyError) as exc:
        load_label_vocabularies(
            {"R4": {"labels": ["absent", "absent"], "canonical": {"absent": "absent"}}},
            judged_sets="R4",
        )
    assert "repeats" in str(exc.value)


def test_the_judge_is_shown_the_rounds_spellings_and_no_others(store, screened):
    batch = _batch(store, screened, judged_sets="R2,R4", label_vocabularies=ROUND_VOCABULARY)
    assert batch["labels_sha256"] == batch["label_vocabularies"]["sha256"]
    for envelope in batch["envelopes"]:
        assert envelope["labels"] == {
            "archive": ["requested", "variant", "new"],
            "literature": ["present", "adjacent", "absent"],
        }
    for view in batch["judge_views"]:
        assert view["labels"]["archive"] == ["requested", "variant", "new"]


def test_a_default_batch_declares_no_vocabulary_at_all(store, screened):
    batch = _batch(store, screened)
    assert "label_vocabularies" not in batch
    assert "labels_sha256" not in batch


def test_a_round_label_is_accepted_and_its_canonical_label_stored_beside_it(store, screened):
    batch = _batch(store, screened, judged_sets="R2,R4", label_vocabularies=ROUND_VOCABULARY)
    labels = {
        idea_id: {"label_archive": "requested", "label_corpus": "present"}
        for idea_id in batch["scope"]["scope"]
    }
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    judged = [r for r in recorded["verdicts"] if r["subject_id"] in set(batch["scope"]["scope"])]
    assert judged
    # This batch carries no plants (plants_per_kind=0), so it is unauditable
    # and every label is written with the batch's caveat on it -- which is
    # exactly the composite shape both columns have to keep.
    for row in judged:
        if row["label"].startswith("R2:"):
            assert row["label"].split(":")[:2] == ["R2", "requested"]
            assert row["label_canonical"].split(":")[:2] == ["R2", "same"]
        else:
            assert row["label"].split(":")[:2] == ["R4", "present"]
            assert row["label_canonical"].split(":")[:2] == ["R4", "stated"]
        assert row["label"].split(":")[2:] == row["label_canonical"].split(":")[2:]
    assert recorded["labels_sha256"] == batch["labels_sha256"]


def test_a_label_outside_the_rounds_vocabulary_is_refused_by_name(store, screened):
    batch = _batch(store, screened, judged_sets="R4", label_vocabularies=_vocabulary("R4"))
    idea_id = batch["scope"]["scope"][0]
    with pytest.raises(NoveltyError) as exc:
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch,
            labels={idea_id: {"label_corpus": "stated"}},
            issued_by_launch=screened["launches"]["lens-1"],
        )
    # 'stated' is the DESIGN's word and a perfectly good canonical value --
    # it is simply not one this round's judge was offered.
    assert "stated" in str(exc.value)
    assert "present" in str(exc.value)


def test_recording_under_a_different_vocabulary_than_the_judge_saw_is_refused(store, screened):
    batch = _batch(store, screened, judged_sets="R4", label_vocabularies=_vocabulary("R4"))
    other = {
        "R4": {
            "labels": ["here", "nearby", "gone"],
            "canonical": {"here": "stated", "nearby": "adjacent", "gone": "absent"},
        }
    }
    with pytest.raises(NoveltyError) as exc:
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch,
            labels={batch["scope"]["scope"][0]: {"label_corpus": "here"}},
            issued_by_launch=screened["launches"]["lens-1"],
            label_vocabularies=other,
        )
    assert "hashes" in str(exc.value)


def test_a_default_round_writes_label_and_canonical_equal(store, screened):
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch,
        labels={i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]},
        issued_by_launch=screened["launches"]["lens-1"],
    )
    rows = [r for r in recorded["verdicts"] if r["subject_id"] in set(batch["scope"]["scope"])]
    assert rows
    for row in rows:
        assert row["label_canonical"] == row["label"]


# ---------------------------------------------------------------------------
# item 3 -- external plants
# ---------------------------------------------------------------------------


def _external(plant_id, kind, statement, expected, **rest):
    return {"plant_id": plant_id, "kind": kind, "statement": statement, "expected_labels": expected, **rest}


def _two_external_plants(screened):
    donor = sorted(screened["dossiers"])[0]
    return [
        _external(
            "P-area-1", "area",
            "A request row is restated as a mechanism: the shared pool refills on a fixed cadence.",
            {"R3": ["same", "variant"]}, source_ref=donor,
        ),
        _external(
            "P-custom-1", "custom",
            "An unrelated mechanism nothing in the reference sets states.",
            {"R3": ["new-mechanism"]},
        ),
    ]


def test_an_external_plant_rides_among_the_records_with_a_masked_id(store, screened):
    plants = _two_external_plants(screened)
    batch = _batch(store, screened, external_plants=plants, plants_per_kind=0, batch_fail_on="area")
    ids = {p["plant_id"] for p in batch["plants"]}
    assert ids == {"P-area-1", "P-custom-1"}
    # Masked exactly like a harness plant: no view carries a plant id.
    flat = json.dumps(batch["judge_views"])
    assert "P-area-1" not in flat and "P-custom-1" not in flat
    assert set(batch["mask"].values()) >= ids
    # And shuffled in: the plants are not a trailing block.
    order = [e["subject_id"] for e in batch["envelopes"]]
    assert order != sorted(order, key=lambda s: s in ids)


def test_an_external_plants_envelope_is_shaped_exactly_like_a_records(store, screened):
    plants = _two_external_plants(screened)
    batch = _batch(store, screened, external_plants=plants, plants_per_kind=0, batch_fail_on="area")
    by_id = {e["subject_id"]: e for e in batch["envelopes"]}
    record_keys = {k for s, e in by_id.items() if s not in {"P-area-1", "P-custom-1"} for k in e}
    plant_keys = set(by_id["P-area-1"])
    assert plant_keys == record_keys
    assert set(by_id["P-area-1"]["record"]) == {
        "requirements", "statement", "home_mechanic", "probe", "provenance_docs", "extra_text",
    }
    # Lane FB-6 item 6: the key is part of the shape for every subject, so a
    # plant that carries extra text is not pickable on its key set.
    assert by_id["P-area-1"]["record"]["extra_text"] is None


def test_an_external_plant_wears_a_donor_records_missing_fields(store, screened):
    plants = [
        _external("P-bare", "custom", "A statement with nothing declared beside it.", {"R3": ["new-mechanism"]})
    ]
    batch = _batch(store, screened, external_plants=plants, plants_per_kind=0, batch_fail_on="custom")
    plant = next(p for p in batch["plants"] if p["plant_id"] == "P-bare")
    assert plant["record"]["requirements"], "a plant with no requirements is a plant a judge picks out"
    assert plant["record"]["probe"]
    assert plant["record"]["home_mechanic"] and "/" in plant["record"]["home_mechanic"]
    assert plant["record"]["provenance_docs"]


def test_a_declared_field_wins_over_the_donors(store, screened):
    plants = [
        _external(
            "P-own", "custom", "A statement of its own.", {"R3": ["new-mechanism"]},
            requirements="One line only.", probe="Check it against row one.", home_mechanic="family-a/row-9",
        )
    ]
    batch = _batch(store, screened, external_plants=plants, plants_per_kind=0, batch_fail_on="custom")
    plant = next(p for p in batch["plants"] if p["plant_id"] == "P-own")
    assert plant["record"]["requirements"] == "One line only."
    assert plant["record"]["home_mechanic"] == "family-a/row-9"


def test_plants_zero_with_a_plants_file_builds_only_the_rounds_own(store, screened):
    batch = _batch(
        store, screened, external_plants=_two_external_plants(screened), plants_per_kind=0,
        batch_fail_on="area",
    )
    assert batch["n_inventory_plants"] == 0
    assert batch["n_paraphrase_plants"] == 0
    assert len(batch["plants"]) == 2
    assert all(p["external"] for p in batch["plants"])


def test_the_harness_battery_and_the_rounds_plants_ride_together(store, screened):
    batch = _batch(
        store, screened, external_plants=_two_external_plants(screened), plants_per_kind=2,
    )
    assert batch["n_inventory_plants"] == 2
    assert len(batch["plants"]) == 6


def test_a_plant_id_that_collides_with_the_harnesss_is_refused(store, screened):
    plants = [_external("PLANT-inventory-0", "custom", "A colliding plant.", {"R3": ["new-mechanism"]})]
    with pytest.raises(NoveltyError) as exc:
        _batch(store, screened, external_plants=plants, plants_per_kind=2)
    assert "collide" in str(exc.value)


def test_a_miss_on_a_failing_kind_fails_the_batch_and_another_kind_is_only_reported(store, screened):
    plants = _two_external_plants(screened)
    batch = _batch(store, screened, external_plants=plants, plants_per_kind=0, batch_fail_on="area")
    labels = {
        "P-area-1": {"label_inventory": "new-mechanism"},   # a MISS on a failing kind
        "P-custom-1": {"label_inventory": "same"},          # a MISS on a reported kind
    }
    scored = score_plants(batch, labels)
    assert scored["batch_fail_on"] == ["area"]
    assert scored["failures"] == ["P-area-1"]
    assert scored["batch_failed"] is True
    assert sorted(m["plant_id"] for m in scored["missed"]) == ["P-area-1", "P-custom-1"]

    other_only = {
        "P-area-1": {"label_inventory": "same"},
        "P-custom-1": {"label_inventory": "same"},
    }
    reported = score_plants(batch, other_only)
    assert reported["failures"] == []
    assert reported["batch_failed"] is False
    assert [m["plant_id"] for m in reported["missed"]] == ["P-custom-1"]


def test_the_per_kind_catch_rates_are_correct_on_a_fixture(store, screened):
    plants = [
        _external("P-a1", "area", "Area plant one.", {"R3": ["same", "variant"]}),
        _external("P-a2", "area", "Area plant two.", {"R3": ["same", "variant"]}),
        _external("P-c1", "custom", "Custom plant one.", {"R3": ["new-mechanism"]}),
    ]
    batch = _batch(store, screened, external_plants=plants, plants_per_kind=0, batch_fail_on="area")
    scored = score_plants(
        batch,
        {
            "P-a1": {"label_inventory": "same"},           # caught
            "P-a2": {"label_inventory": "new-mechanism"},  # missed
            "P-c1": {"label_inventory": "new-mechanism"},  # caught
        },
    )
    assert scored["by_kind"]["area"] == {
        "n": 2, "caught": ["P-a1"], "missed": ["P-a2"], "unlabelled": [],
        "catch_rate": 0.5, "fails_batch": True,
    }
    assert scored["by_kind"]["custom"]["catch_rate"] == 1.0
    assert scored["by_kind"]["custom"]["fails_batch"] is False
    assert scored["catch_rate"] == round(2 / 3, 6)


def test_a_plant_is_scored_against_every_declared_set_it_expects(store, screened):
    plants = [
        _external(
            "P-both", "area", "A plant with an expectation on both sets.",
            {"R2": ["same", "variant"], "R4": ["absent"]},
        )
    ]
    batch = _batch(
        store, screened, judged_sets="R2,R4", external_plants=plants, plants_per_kind=0,
        batch_fail_on="area",
    )
    caught = score_plants(batch, {"P-both": {"label_archive": "same", "label_corpus": "absent"}})
    assert caught["by_set"]["R2"]["caught"] == ["P-both"]
    assert caught["by_set"]["R4"]["caught"] == ["P-both"]
    assert caught["caught"] == ["P-both"]

    half = score_plants(batch, {"P-both": {"label_archive": "same", "label_corpus": "stated"}})
    assert half["by_set"]["R2"]["caught"] == ["P-both"]
    assert half["by_set"]["R4"]["missed"] == ["P-both"]
    # Caught on one set and missed on another is a MISS: the plant is one
    # subject and the judge got it wrong about something it was shown.
    assert half["caught"] == []
    assert [m["plant_id"] for m in half["missed"]] == ["P-both"]
    assert half["missed"][0]["reference_set"] == "R4"


def test_an_expectation_for_an_undeclared_set_is_refused():
    with pytest.raises(NoveltyError) as exc:
        load_external_plants(
            [_external("P-x", "area", "text", {"R2": ["same"]})], judged_sets="R3,R4"
        )
    assert "R2" in str(exc.value)


def test_an_expectation_outside_the_rounds_vocabulary_is_refused():
    with pytest.raises(NoveltyError) as exc:
        load_external_plants(
            [_external("P-x", "area", "text", {"R4": ["stated"]})],
            judged_sets="R4",
            label_vocabularies=load_label_vocabularies(_vocabulary("R4"), judged_sets="R4"),
        )
    assert "stated" in str(exc.value)


def test_a_plant_declaration_is_validated_field_by_field():
    for bad, needle in (
        ([{"kind": "area", "statement": "x", "expected_labels": {"R4": ["absent"]}}], "plant_id"),
        ([_external("P", "sideways", "x", {"R4": ["absent"]})], "sideways"),
        ([_external("P", "area", "   ", {"R4": ["absent"]})], "statement"),
        ([_external("P", "area", "x", {})], "expected_labels"),
        ([_external("P", "area", "x", {"R4": ["absent"]}, mood="cheerful")], "mood"),
        ([_external("P", "area", "x", {"R4": ["absent"]}), _external("P", "area", "y", {"R4": ["absent"]})], "repeats"),
        ({"plants": []}, "no plants"),
    ):
        with pytest.raises(NoveltyError) as exc:
            load_external_plants(bad, judged_sets="R4")
        assert needle in str(exc.value), (bad, str(exc.value))


def test_batch_fail_on_is_validated_and_never_empty():
    assert normalize_fail_kinds(None) == ("inventory",)
    assert normalize_fail_kinds("custom,area") == ("area", "custom")
    with pytest.raises(NoveltyError):
        normalize_fail_kinds("sideways")
    with pytest.raises(NoveltyError):
        normalize_fail_kinds([])


def test_a_batch_that_seeds_no_failing_kind_is_refused(store, screened):
    plants = [_external("P-c", "custom", "A custom plant only.", {"R3": ["new-mechanism"]})]
    with pytest.raises(NoveltyError) as exc:
        _batch(store, screened, external_plants=plants, plants_per_kind=0)
    assert "failing kind" in str(exc.value)


# ---------------------------------------------------------------------------
# item 4 -- archive intake
# ---------------------------------------------------------------------------


ARCHIVE_ROUND = "round-archive"


def _archive_rows_in(store, screened, statements):
    """Write prior-round rows as an ARCHIVE round, the way `lens intake
    --status archived` does."""
    return intake_records(
        store,
        round_id=ARCHIVE_ROUND,
        records=[
            {
                "statement": statement,
                "home_mechanic": home,
                "probe": "Check the transition against one inventory row.",
                "provenance": {"docs": screened["corpus_doc_ids"][:1]},
            }
            for home, statement in statements
        ],
        author_launch=screened["launches"]["lens-1"],
        status="archived",
    )


def test_archived_is_a_status_a_record_may_take():
    assert ARCHIVED_STATUS == "archived"
    assert ARCHIVED_STATUS in IDEA_STATUSES


def test_an_intake_status_is_a_default_the_file_can_override(store, screened):
    rows = intake_records(
        store,
        round_id=ARCHIVE_ROUND,
        records=[
            {"statement": "An archived row.", "probe": "p", "provenance": {"docs": []}},
            {"statement": "A live row.", "probe": "p", "provenance": {"docs": []}, "status": "raw"},
        ],
        author_launch=screened["launches"]["lens-1"],
        status="archived",
    )
    assert [read_idea(store, idea_id=r["idea_id"])["status"] for r in rows] == ["archived", "raw"]


def test_an_unknown_intake_status_is_refused(store, screened):
    with pytest.raises(ValueError) as exc:
        intake_records(
            store, round_id=ARCHIVE_ROUND,
            records=[{"statement": "x", "probe": "p", "provenance": {"docs": []}}],
            author_launch=screened["launches"]["lens-1"], status="shelved",
        )
    assert "shelved" in str(exc.value)


def test_archived_rows_are_retrieved_as_archive_rows(store, screened):
    # An exact restatement of a live record, because the zero-setup embed
    # backend is hash-derived: identical text is the one case where the
    # cosine is known (1.0) and the row is certain to rank into the bundle.
    # A record the judge actually sees: the archive bundle rides in an
    # envelope, and only scoped records get one.
    in_scope = _batch(store, screened, judged_sets="R2")["scope"]["scope"][0]
    live = read_idea(store, idea_id=in_scope)
    written = _archive_rows_in(store, screened, [(live["home"], live["body"])])
    batch = _batch(store, screened, judged_sets="R2")
    archived_id = written[0]["idea_id"]
    seen = {
        row["idea_id"]: row
        for envelope in batch["envelopes"]
        for row in envelope["archive_rows"]
    }
    assert archived_id in seen, "an archived row is R2 content and is retrieved as one"
    assert seen[archived_id]["status"] == "archived"
    assert seen[archived_id]["round_id"] == ARCHIVE_ROUND
    # And it is the NEAREST row for the record it restates, which is the
    # comparison an R2 judge is being asked to make.
    subject = next(e for e in batch["envelopes"] if e["subject_id"] == live["idea_id"])
    assert subject["archive_rows"][0]["idea_id"] == archived_id
    assert subject["archive_rows"][0]["similarity"] >= 0.99


def test_a_live_record_near_an_archived_row_is_flagged_not_merged(store):
    """The whole point of item 4: the archive is the fixed background a round
    is judged against, not a place to retire this round's records into."""
    fixture = build_round(store)
    live = read_idea(store, idea_id=fixture["idea_ids"][0])
    intake_records(
        store,
        round_id=ARCHIVE_ROUND,
        records=[
            {
                "statement": live["body"],       # an exact restatement: cosine 1.0
                "home_mechanic": live["home"],   # and the same home cell
                "probe": "p",
                "provenance": {"docs": []},
            }
        ],
        author_launch=fixture["launches"]["lens-1"],
        status="archived",
    )
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"], rescreen=True
    )
    assert read_idea(store, idea_id=live["idea_id"])["status"] == "raw"
    assert live["idea_id"] not in {m["idea_id"] for m in mechanical["merged"]}
    hits = mechanical["archive_hits"].get(live["idea_id"])
    assert hits, "the live record carries the archived row it sits on"
    assert hits[0]["round_id"] == ARCHIVE_ROUND
    assert hits[0]["similarity"] >= 0.99
    assert mechanical["dossiers"][live["idea_id"]]["archive_hit"] == hits


def test_an_archived_row_is_never_the_survivor_of_a_merge(store):
    fixture = build_round(store)
    live = read_idea(store, idea_id=fixture["idea_ids"][0])
    written = intake_records(
        store, round_id=ARCHIVE_ROUND,
        records=[{"statement": live["body"], "home_mechanic": live["home"], "probe": "p",
                  "provenance": {"docs": []}}],
        author_launch=fixture["launches"]["lens-1"], status="archived",
    )
    run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"], rescreen=True
    )
    archived = read_idea(store, idea_id=written[0]["idea_id"])
    assert archived["status"] == "archived"
    assert not archived["parent_ids"]
    assert "merged_from" not in json.loads(archived["provenance"] or "{}")


def test_a_dossier_with_no_archive_hit_says_so_rather_than_saying_nothing(store, screened):
    for dossier in screened["dossiers"].values():
        assert dossier["archive_hit"] == []


def test_an_archived_row_is_never_consolidated(store, screened):
    written = _archive_rows_in(
        store, screened, [("family-a/row-21", "A prior round's request row, kept as reference.")]
    )
    archived_id = written[0]["idea_id"]
    batch = _batch(store, screened)
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch,
        labels={i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]},
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert read_idea(store, idea_id=archived_id)["status"] == "archived"


def test_an_archived_row_is_never_batched_by_the_mechanical_screen(store, screened):
    written = _archive_rows_in(
        store, screened, [("family-a/row-22", "Another prior row, in this round's own id.")]
    )
    mechanical = run_mechanical_screen(
        store, round_id=ARCHIVE_ROUND, launch_id=screened["launches"]["lens-1"]
    )
    assert written[0]["idea_id"] not in mechanical["dossiers"]
    assert mechanical["n_screened"] == 0


# ---------------------------------------------------------------------------
# item 5 -- seed-work labels
# ---------------------------------------------------------------------------


def test_seeds_round_trip_into_the_verdict_rows_evidence(store, screened):
    batch = _batch(store, screened)
    idea_id = batch["scope"]["scope"][0]
    labels = {i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]}
    labels[idea_id] = {
        "label_inventory": "unscreenable",
        "seeds": [
            {"ref": "DOC-a#3", "label": "on-topic"},
            {"ref": "DOC-b#7", "label": "on-topic"},
            {"ref": "DOC-c#1", "label": "off-topic"},
        ],
    }
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    row = next(r for r in recorded["verdicts"] if r["subject_id"] == idea_id)
    notes = [e.get("note") for e in json.loads(row["evidence"])]
    assert "seed DOC-a#3: on-topic" in notes
    assert "seed DOC-c#1: off-topic" in notes
    assert "seed work: 2 on-topic of 3 resolved" in notes
    # Seeds are evidence, never a reference set: no row names one.
    assert all(not r["label"].startswith("SEED") for r in recorded["verdicts"])


def test_the_seed_counts_are_reported_in_the_result(store, screened):
    batch = _batch(store, screened)
    first, second = batch["scope"]["scope"][0], batch["scope"]["scope"][1]
    labels = {i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]}
    labels[first] = {
        "label_inventory": "unscreenable",
        "seeds": [{"ref": "DOC-a#1", "label": "on-topic"}],
    }
    labels[second] = {
        "label_inventory": "variant",
        "seeds": [
            {"ref": "DOC-b#1", "label": "on-topic"},
            {"ref": "DOC-b#2", "label": "on-topic"},
        ],
    }
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    seeds = recorded["seeds"]
    assert seeds["n_subjects"] == 2
    assert seeds["n_seeds"] == 3
    assert seeds["n_on_topic"] == 3
    assert seeds["by_subject"][first]["n_on_topic"] == 1
    assert seeds["by_subject"][second]["n"] == 2
    assert seeds["min_on_topic"] == MIN_ON_TOPIC_SEEDS
    assert seeds["on_topic_label"] == "on-topic"


def test_an_unscreenable_with_too_little_on_topic_work_is_reported_not_refused(store, screened):
    """The RULE is the round's own text. The harness records the seeds and
    the count and says which subjects sit below the bar."""
    batch = _batch(store, screened)
    thin, none_at_all = batch["scope"]["scope"][0], batch["scope"]["scope"][1]
    labels = {i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]}
    labels[thin] = {
        "label_inventory": "unscreenable", "seeds": [{"ref": "DOC-a#1", "label": "on-topic"}],
    }
    labels[none_at_all] = {"label_inventory": "unscreenable"}
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["seeds"]["unscreenable_below_bar"] == sorted([thin, none_at_all])
    # Reported, never refused: the rows are written.
    assert any(r["subject_id"] == thin for r in recorded["verdicts"])


def test_an_unscreenable_over_the_bar_is_not_listed(store, screened):
    batch = _batch(store, screened)
    idea_id = batch["scope"]["scope"][0]
    labels = {i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]}
    labels[idea_id] = {
        "label_inventory": "unscreenable",
        "seeds": [
            {"ref": "DOC-a#1", "label": "on-topic"},
            {"ref": "DOC-a#2", "label": "on-topic"},
            {"ref": "DOC-a#3", "label": "off-topic"},
        ],
    }
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["seeds"]["unscreenable_below_bar"] == []


def test_a_round_may_respell_its_seed_labels_and_the_first_is_the_on_topic_one(store, screened):
    vocabulary = {
        "R3": {
            "labels": ["known", "vague"],
            "canonical": {"known": "same", "vague": "unscreenable"},
        },
        "extra": {"seed": ["relevant", "irrelevant"]},
        "unscreenable": "vague",
    }
    resolved = load_label_vocabularies(vocabulary, judged_sets="R3")
    assert on_topic_seed_label(resolved) == "relevant"
    batch = _batch(store, screened, judged_sets="R3", label_vocabularies=vocabulary)
    idea_id = batch["scope"]["scope"][0]
    labels = {i: {"label_inventory": "known"} for i in batch["scope"]["scope"]}
    labels[idea_id] = {
        "label_inventory": "vague",
        "seeds": [{"ref": "DOC-a#1", "label": "relevant"}, {"ref": "DOC-a#2", "label": "irrelevant"}],
    }
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["seeds"]["on_topic_label"] == "relevant"
    assert recorded["seeds"]["by_subject"][idea_id]["n_on_topic"] == 1
    # The round's own word for unscreenable is what the bar looks for.
    assert recorded["seeds"]["unscreenable_below_bar"] == [idea_id]


def test_a_respelled_unscreenable_no_declared_set_offers_is_refused(store, screened):
    """Stage 3, N3: accepted, the word was unusable -- a judge answering it
    was refused at --record-verdicts by name, so `unscreenable_below_bar`
    could never fire for that round."""
    unoffered = {
        "R3": {
            "labels": ["requested", "variant2", "brand-new"],
            "canonical": {"requested": "same", "variant2": "variant", "brand-new": "new-mechanism"},
        },
        "unscreenable": "no-mechanism",
    }
    with pytest.raises(NoveltyError) as exc:
        load_label_vocabularies(unoffered, judged_sets="R3,R4")
    assert "no-mechanism" in str(exc.value)
    assert "brand-new" in str(exc.value)
    # offered, it loads and the bar fires on it
    offered = {
        "R3": {
            "labels": ["requested", "variant2", "brand-new", "no-mechanism"],
            "canonical": {
                "requested": "same", "variant2": "variant", "brand-new": "new-mechanism",
                "no-mechanism": "unscreenable",
            },
        },
        "unscreenable": "no-mechanism",
    }
    resolved = load_label_vocabularies(offered, judged_sets="R3")
    assert resolved["unscreenable"] == "no-mechanism"
    batch = _batch(store, screened, judged_sets="R3", label_vocabularies=offered)
    idea_id = batch["scope"]["scope"][0]
    labels = {i: {"label_inventory": "requested"} for i in batch["scope"]["scope"]}
    labels[idea_id] = {"label_inventory": "no-mechanism"}
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["seeds"]["unscreenable_below_bar"] == [idea_id]


def test_the_designs_own_unscreenable_word_is_accepted_whatever_a_set_offers():
    """The refusal above is about a RE-SPELLING. A round that declares the
    design's own word keeps working even where no declared set offers it --
    the corpus vocabulary never has."""
    resolved = load_label_vocabularies({**ROUND_VOCABULARY}, judged_sets="R2,R4")
    assert resolved["unscreenable"] == "unscreenable"
    assert all(
        "unscreenable" not in block["labels"] for block in resolved["sets"].values()
    )


def test_a_seed_outside_the_rounds_vocabulary_is_refused_before_anything_is_written(store, screened):
    batch = _batch(store, screened)
    idea_id = batch["scope"]["scope"][0]
    labels = {i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]}
    labels[idea_id] = {"label_inventory": "variant", "seeds": [{"ref": "DOC-a#1", "label": "maybe"}]}
    with pytest.raises(NoveltyError) as exc:
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=labels,
            issued_by_launch=screened["launches"]["lens-1"],
        )
    assert "maybe" in str(exc.value)
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"] == 0


def test_a_seed_is_validated_field_by_field():
    for bad, needle in (
        ("not a list", "list of"),
        ([{"label": "on-topic"}], "no ref"),
        ([{"ref": "x", "label": "on-topic", "mood": "keen"}], "mood"),
        (["x"], "not an object"),
    ):
        with pytest.raises(NoveltyError) as exc:
            normalize_seeds(bad, subject_id="IDEA-1")
        assert needle in str(exc.value), (bad, str(exc.value))
    assert normalize_seeds(None, subject_id="IDEA-1") == []


def test_a_subject_with_seeds_and_no_label_is_unlabelled_not_consolidated(store, screened):
    """A sheet entry can now carry seed work and no label. A non-empty dict
    with nothing but seeds in it is a subject the judge said nothing about."""
    batch = _batch(store, screened)
    silent = batch["scope"]["scope"][0]
    labels = {i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]}
    labels[silent] = {"seeds": [{"ref": "DOC-a#1", "label": "on-topic"}]}
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert silent in recorded["unlabelled_scope"]
    assert silent not in [v["subject_id"] for v in recorded["verdicts"]]
    assert read_idea(store, idea_id=silent)["status"] == "raw"


# ---------------------------------------------------------------------------
# item 6 -- calibration mode
# ---------------------------------------------------------------------------


CALIBRATION_PLANTS = [
    {"plant_id": "C-1", "kind": "area", "statement": "Calibration plant one, a rewritten reference row.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-2", "kind": "area", "statement": "Calibration plant two, another rewritten row.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-3", "kind": "custom", "statement": "Calibration plant three, unrelated to everything.",
     "expected_labels": {"R3": ["new-mechanism"]}},
    {"plant_id": "C-4", "kind": "custom", "statement": "Calibration plant four, also unrelated.",
     "expected_labels": {"R3": ["new-mechanism"]}},
]


def _calibration(store, screened, **kwargs):
    kwargs.setdefault("external_plants", CALIBRATION_PLANTS)
    kwargs.setdefault("batch_fail_on", "area")
    return build_calibration_batch(
        store, round_id=screened["round_id"], seed=SEED, dossiers=screened["dossiers"], **kwargs
    )


def test_a_calibration_batch_is_plants_only_and_judges_no_record(store, screened):
    batch = _calibration(store, screened)
    assert batch["calibration"] is True
    assert batch["procedure_version"] == CALIBRATION_PROCEDURE_VERSION
    assert batch["scope"]["scope"] == []
    assert batch["scope"]["unjudged"] == []
    assert len(batch["plants"]) == 4
    assert len(batch["envelopes"]) == 4
    assert batch["batch_id"] == "calibration-0"


def test_a_calibration_needs_no_records_at_all(store):
    """The chicken-and-egg this closes: the battery is what makes a round's
    labels trustworthy, and until now the only way to run it was to run a
    round."""
    from tests._inventory_fixtures import build_corpus_with_inventory

    build_corpus_with_inventory(store)
    batch = build_calibration_batch(
        store, round_id="round-empty", external_plants=CALIBRATION_PLANTS, seed=SEED,
        batch_fail_on="area",
    )
    assert len(batch["plants"]) == 4
    # Lane FB-6 item 5: the baseline is over the PLANTS here -- there are no
    # records, which is the whole point of the mode.
    assert batch["baseline_cosine_distribution"]["over"] == "plants"
    assert batch["baseline_cosine_distribution"]["n"] == 4


def test_a_calibration_writes_its_own_file_with_masked_views(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    path = tmp_path / "judged" / "calibration-0.json"
    assert path.is_file()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["batch_id"] == "calibration-0"
    flat = json.dumps(written["judge_views"])
    for plant_id in ("C-1", "C-2", "C-3", "C-4"):
        assert plant_id not in flat
    assert set(written["mask"].values()) == {"C-1", "C-2", "C-3", "C-4"}


def test_a_second_calibration_takes_the_next_number(store, screened, tmp_path):
    _calibration(store, screened, out_dir=tmp_path)
    second = _calibration(store, screened, out_dir=tmp_path)
    assert second["batch_id"] == "calibration-1"


def test_two_label_files_give_kappa_per_declared_set_with_n(store, screened, tmp_path):
    batch = _calibration(store, screened, judged_sets="R3,R4", out_dir=tmp_path)
    a = {
        "C-1": {"label_inventory": "same", "label_corpus": "stated"},
        "C-2": {"label_inventory": "variant", "label_corpus": "absent"},
        "C-3": {"label_inventory": "new-mechanism", "label_corpus": "absent"},
        "C-4": {"label_inventory": "new-mechanism", "label_corpus": "absent"},
    }
    b = {
        "C-1": {"label_inventory": "same", "label_corpus": "stated"},
        "C-2": {"label_inventory": "variant", "label_corpus": "absent"},
        "C-3": {"label_inventory": "new-mechanism", "label_corpus": "absent"},
        "C-4": {"label_inventory": "variant", "label_corpus": "adjacent"},
    }
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=a, labels_b=b,
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert set(card["kappa_by_set"]) == {"R3", "R4"}
    for block in card["kappa_by_set"].values():
        assert block["n"] == 4
        assert block["observed_agreement"] is not None
    # Hand-computed: R3 has three agreements of four (po = 0.75); the marginals
    # are A {same 1, variant 1, new-mechanism 2} and B {same 1, variant 2,
    # new-mechanism 1}, so pe = (1/4)(1/4) + (1/4)(2/4) + (2/4)(1/4) = 0.3125
    # and kappa = (0.75 - 0.3125) / (1 - 0.3125) = 0.636364.
    assert card["kappa_by_set"]["R3"]["observed_agreement"] == 0.75
    assert card["kappa_by_set"]["R3"]["expected_agreement"] == 0.3125
    assert card["kappa_by_set"]["R3"]["kappa"] == 0.636364


def test_perfect_agreement_reads_as_one_by_the_documented_convention(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    sheet = {
        "C-1": {"label_inventory": "same"},
        "C-2": {"label_inventory": "variant"},
        "C-3": {"label_inventory": "new-mechanism"},
        "C-4": {"label_inventory": "new-mechanism"},
    }
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["kappa_by_set"]["R3"]["kappa"] == 1.0
    assert card["kappa_by_set"]["R3"]["observed_agreement"] == 1.0


def test_one_category_throughout_reads_as_agreement_not_as_zero(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    sheet = {pid: {"label_inventory": "variant"} for pid in ("C-1", "C-2", "C-3", "C-4")}
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["kappa_by_set"]["R3"]["kappa"] == 1.0


def test_the_card_reports_catch_by_kind_per_judge_and_the_misses_by_id(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    a = {
        "C-1": {"label_inventory": "same"},           # caught
        "C-2": {"label_inventory": "new-mechanism"},  # missed (area -> fails)
        "C-3": {"label_inventory": "new-mechanism"},  # caught
        "C-4": {"label_inventory": "new-mechanism"},  # caught
    }
    b = {
        "C-1": {"label_inventory": "same"},
        "C-2": {"label_inventory": "variant"},
        "C-3": {"label_inventory": "new-mechanism"},
        # C-4 unlabelled
    }
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=a, labels_b=b,
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["catch_by_kind"]["a"]["area"]["catch_rate"] == 0.5
    assert card["catch_by_kind"]["b"]["area"]["catch_rate"] == 1.0
    assert card["catch_by_kind"]["b"]["custom"]["unlabelled"] == ["C-4"]
    assert card["catch_rate"] == {"a": 0.75, "b": 0.75}
    assert set(card["misses_by_id"]) == {"C-2", "C-4"}
    assert card["misses_by_id"]["C-2"]["a"]["label"] == "new-mechanism"
    assert "b" not in card["misses_by_id"]["C-2"]
    assert card["misses_by_id"]["C-4"]["b"]["label"] is None


def test_pair_ratings_give_pearson_r(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    sheet = {pid: {"label_inventory": "variant"} for pid in ("C-1", "C-2", "C-3", "C-4")}
    ratings = [
        {"a": "C-1", "b": "C-2", "human": 0.9},
        {"a": "C-1", "b": "C-3", "human": 0.2},
        {"a": "C-2", "b": "C-4", "human": 0.5},
        {"a": "C-3", "b": "C-4", "human": 0.7},
    ]
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], pair_ratings=ratings, out_dir=tmp_path,
    )
    assert card["r_embedding_human"]["n"] == 4
    assert -1.0 <= card["r_embedding_human"]["r"] <= 1.0
    assert len(card["rated_pairs"]) == 4
    # The r is over the cosines the harness computed, not over anything the
    # rater supplied: recompute it from the card's own rows.
    expected = pearson_r([{"x": p["cosine"], "y": p["human"]} for p in card["rated_pairs"]])
    assert card["r_embedding_human"] == expected


def test_no_pair_ratings_means_r_is_absent_rather_than_assumed(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    sheet = {pid: {"label_inventory": "variant"} for pid in ("C-1", "C-2")}
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["r_embedding_human"] is None
    assert card["rated_pairs"] == []


def test_pearson_r_on_a_hand_computed_series():
    # y = 2x + 1 exactly: r = 1.0. And the mirror image: r = -1.0.
    rising = [{"x": 0.1, "y": 1.2}, {"x": 0.2, "y": 1.4}, {"x": 0.3, "y": 1.6}, {"x": 0.4, "y": 1.8}]
    assert pearson_r(rising)["r"] == 1.0
    assert pearson_r([{"x": p["x"], "y": -p["y"]} for p in rising])["r"] == -1.0
    # A known mixed series: x = [1,2,3,4,5], y = [2,4,5,4,5] -> r = 0.774597.
    mixed = [{"x": x, "y": y} for x, y in zip([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])]
    assert pearson_r(mixed)["r"] == 0.774597


def test_pearson_r_refuses_to_report_a_number_it_would_not_mean():
    assert pearson_r([{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.4}])["r"] is None
    assert "two points" in pearson_r([{"x": 0.1, "y": 0.2}])["note"]
    flat = [{"x": 0.5, "y": y} for y in (0.1, 0.2, 0.3)]
    assert pearson_r(flat)["r"] is None
    assert "cosines" in pearson_r(flat)["note"]
    flat_human = [{"x": x, "y": 0.5} for x in (0.1, 0.2, 0.3)]
    assert "human ratings" in pearson_r(flat_human)["note"]


def test_a_pair_naming_nothing_resolvable_is_refused(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    sheet = {"C-1": {"label_inventory": "same"}}
    with pytest.raises(NoveltyError) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
            pair_ratings=[{"a": "C-1", "b": "NOT-A-THING", "human": 0.5}],
        )
    assert "NOT-A-THING" in str(exc.value)


def test_the_baseline_percentiles_are_cut_on_the_fixtures_own_cosines(store, screened, tmp_path):
    """Lane FB-6 item 5: a calibration's baseline is over its PLANTS, cut on
    the same numbers the envelopes' ``retrieved[].similarity`` carry."""
    batch = _calibration(store, screened, out_dir=tmp_path)
    distribution = batch["baseline_cosine_distribution"]
    nearest = sorted(
        max(float(h["similarity"]) for h in p["retrieved"])
        for p in batch["plants"]
        if p.get("retrieved")
    )
    assert distribution["over"] == "plants"
    assert distribution["n"] == len(batch["plants"])
    assert distribution["n_with_neighbour"] == len(nearest)
    if nearest:
        assert distribution["min"] == round(nearest[0], 6)
        assert distribution["max"] == round(nearest[-1], 6)
        for key, p in (("p50", 0.50), ("p90", 0.90), ("p95", 0.95)):
            index = min(len(nearest) - 1, int(round(p * (len(nearest) - 1))))
            assert distribution[key] == round(nearest[index], 6)
    else:
        # This module's calibration plants state things the fixture corpus
        # does not, and the R4 bundle is thresholded -- so the honest reading
        # is "two counts, no percentiles", which is exactly what the two
        # counts exist to make visible. The populated case is cut in
        # tests/test_lens_baseline_over_plants.py.
        assert distribution["min"] is None and distribution["max"] is None
        assert all(distribution[key] is None for key in ("p50", "p90", "p95"))


def test_the_baseline_says_how_much_of_the_round_it_is_about():
    empty = baseline_cosine_distribution({})
    assert empty == {
        "over": "records", "n": 0, "n_records": 0, "n_with_neighbour": 0,
        "hit_similarity_floor": 0.5,
        "min": None, "p50": None, "p90": None, "p95": None, "max": None,
        # Lane FB-7 item 2: which convention cut these percentiles, on the
        # card as well as in `lens screen --baseline`. The identical
        # threshold under the linear convention is a different number.
        "percentile_method": "index-round(p*(n-1))",
    }
    one = baseline_cosine_distribution(
        {
            "IDEA-1": {"candidate_hits": {"R4": [{"similarity": 0.61}, {"similarity": 0.72}]}},
            "IDEA-2": {"candidate_hits": {"R4": []}},
        }
    )
    assert one["n_records"] == 2
    assert one["n_with_neighbour"] == 1
    assert one["p50"] == one["max"] == 0.72


def test_calibration_verdict_rows_carry_their_own_procedure_version(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    sheet = {pid: {"label_inventory": "variant"} for pid in ("C-1", "C-2")}
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["n_verdicts"] == 4  # two plants x two judges, one declared set
    rows = store.knowledge.execute(
        "SELECT subject_id, label, label_canonical, procedure_version, evidence FROM verdict"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["procedure_version"] == CALIBRATION_PROCEDURE_VERSION
        assert row["label"] == "R3:variant"
        assert row["label_canonical"] == "R3:variant"
    judges = sorted(
        note for row in rows
        for note in [n["note"] for n in json.loads(row["evidence"]) if "calibration judge" in str(n.get("note"))]
    )
    assert any("judge A" in note for note in judges)
    assert any("judge B" in note for note in judges)


def test_a_calibration_consolidates_nothing(store, screened, tmp_path):
    before = {
        i: read_idea(store, idea_id=i)["status"] for i in screened["idea_ids"]
    }
    batch = _calibration(store, screened, out_dir=tmp_path)
    record_calibration(
        store, round_id=screened["round_id"], batch=batch,
        labels_a={"C-1": {"label_inventory": "same"}}, labels_b={"C-1": {"label_inventory": "same"}},
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    after = {i: read_idea(store, idea_id=i)["status"] for i in screened["idea_ids"]}
    assert after == before


def test_a_calibration_does_not_block_the_rounds_own_first_submission(store, screened, tmp_path):
    """The separate procedure_version is what makes this true: the
    one-submission rule filters on novelty-v2, so a calibration is not read
    as a round's first recording."""
    calibration = _calibration(store, screened, out_dir=tmp_path)
    record_calibration(
        store, round_id=screened["round_id"], batch=calibration,
        labels_a={"C-1": {"label_inventory": "same"}}, labels_b={"C-1": {"label_inventory": "same"}},
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch,
        labels={i: {"label_inventory": "variant"} for i in batch["scope"]["scope"]},
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert recorded["n_verdicts"] > 0


def test_a_calibration_sheet_naming_a_non_plant_is_refused(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    with pytest.raises(NoveltyError) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch,
            labels_a={"IDEA-nope": {"label_inventory": "same"}},
            labels_b={"C-1": {"label_inventory": "same"}},
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
        )
    assert "IDEA-nope" in str(exc.value)


def test_a_calibration_label_outside_the_vocabulary_is_refused(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    with pytest.raises(NoveltyError) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch,
            labels_a={"C-1": {"label_inventory": "quite-new"}},
            labels_b={"C-1": {"label_inventory": "same"}},
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
        )
    assert "quite-new" in str(exc.value)


def test_masked_calibration_sheets_are_mapped_back(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    masked = {mask_id: {"label_inventory": "same"} for mask_id in batch["mask"]}
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=masked, labels_b=dict(masked),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["kappa_by_set"]["R3"]["n"] == 4


def test_a_calibration_cannot_be_recorded_twice_without_supersede(store, screened, tmp_path):
    """Stage 3, N2: a re-run used to append a second full set of rows beside
    the first -- two rows per plant per judge with nothing joining them, and
    the card rewritten in place, so only a later count of the calibration
    rows was wrong."""
    batch = _calibration(store, screened, out_dir=tmp_path)
    sheet = {p["plant_id"]: {"label_inventory": "same"} for p in batch["plants"]}
    first = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert first["superseded"] == []
    n_rows = store.knowledge.execute(
        "SELECT COUNT(*) AS n FROM verdict WHERE procedure_version = ?",
        (CALIBRATION_PROCEDURE_VERSION,),
    ).fetchone()["n"]
    assert n_rows == first["n_verdicts"] > 0

    with pytest.raises(NoveltyError) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
        )
    assert "supersede" in str(exc.value)
    assert store.knowledge.execute(
        "SELECT COUNT(*) AS n FROM verdict WHERE procedure_version = ?",
        (CALIBRATION_PROCEDURE_VERSION,),
    ).fetchone()["n"] == n_rows

    again = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path, supersede=True,
    )
    assert again["superseded"] == sorted(v["verdict_id"] for v in first["verdicts"])
    # and every new row names the rows it replaced
    for row in again["verdicts"]:
        assert "supersedes " in json.dumps(row["evidence"])


def test_a_calibration_over_no_plants_is_refused(store, screened, tmp_path):
    batch = _calibration(store, screened, out_dir=tmp_path)
    batch = {**batch, "plants": []}
    with pytest.raises(NoveltyError) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch, labels_a={}, labels_b={},
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
        )
    assert "nothing to" in str(exc.value)


# ---------------------------------------------------------------------------
# stage 3, B1 -- the harness battery under a round's own vocabulary
#
# Item 2 and item 3 interact, and the interaction is the whole audit: the
# harness battery's expectation used to be stamped in the DESIGN's words
# while the judge was shown the ROUND's, so a perfect judge caught nothing,
# the batch failed its own audit, and every verdict row carried
# `plants_failed` about a judge that did the task. Every test below builds a
# real harness battery -- which no other test in this module does, because
# `_batch` defaults `plants_per_kind=0`, and that default is exactly what hid
# this.
# ---------------------------------------------------------------------------


#: A round that re-spells the INVENTORY set -- the set the harness battery is
#: scored against under the default declaration.
R3_VOCABULARY = {
    "R3": {
        "labels": ["requested", "variant2", "brand-new", "no-mechanism"],
        "canonical": {
            "requested": "same",
            "variant2": "variant",
            "brand-new": "new-mechanism",
            "no-mechanism": "unscreenable",
        },
    },
    "unscreenable": "no-mechanism",
}


def _battery(store, screened, **kwargs):
    """A batch with the REAL harness battery in it."""
    return build_judged_batch(
        store, round_id=screened["round_id"], dossiers=screened["dossiers"], seed=SEED, **kwargs
    )


def test_the_harness_battery_expects_the_rounds_own_words(store, screened):
    batch = _battery(store, screened, label_vocabularies=R3_VOCABULARY)
    assert len(batch["plants"]) == 10
    for plant in batch["plants"]:
        # "requested" and "variant2" are this round's words for "same" and
        # "variant"; "brand-new" and "no-mechanism" are not, and must not be
        # smuggled in by the translation.
        assert plant["expected_labels"] == ["requested", "variant2"]


def test_the_default_battery_still_expects_the_designs_words(store, screened):
    batch = _battery(store, screened)
    assert all(p["expected_labels"] == ["same", "variant"] for p in batch["plants"])


def test_a_perfect_judge_in_the_rounds_words_catches_every_harness_plant(store, screened):
    batch = _battery(store, screened, label_vocabularies=R3_VOCABULARY)
    labels = {p["plant_id"]: {"label_inventory": "requested"} for p in batch["plants"]}
    labels |= {i: {"label_inventory": "brand-new"} for i in batch["scope"]["scope"]}
    scored = score_plants(batch, labels)
    assert scored["catch_rate"] == 1.0
    assert scored["failures"] == []
    assert scored["batch_failed"] is False
    # and the round that answered perfectly is recorded as one that did
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["status"] == "screened"
    assert recorded["caveats"] == []
    # the judged scope and the unjudged remainder both consolidate, which is
    # what a batch whose audit PASSED does
    assert recorded["n_consolidated"] >= len(batch["scope"]["scope"]) > 0
    assert all(":plants_failed" not in r["label"] for r in recorded["verdicts"])


def test_the_rounds_word_for_new_mechanism_still_misses_a_harness_plant(store, screened):
    """The translation must not turn the audit off: a judge that calls a
    planted register row brand-new misses it, in the round's words exactly as
    it did in the design's."""
    batch = _battery(store, screened, label_vocabularies=R3_VOCABULARY)
    labels = {p["plant_id"]: {"label_inventory": "brand-new"} for p in batch["plants"]}
    labels |= {i: {"label_inventory": "brand-new"} for i in batch["scope"]["scope"]}
    scored = score_plants(batch, labels)
    assert scored["catch_rate"] == 0.0
    assert scored["batch_failed"] is True
    assert scored["inventory_failures"] == sorted(
        p["plant_id"] for p in batch["plants"] if p["kind"] == "inventory"
    )


def test_a_batch_stamped_in_the_designs_words_is_scored_canonically(store, screened):
    """The other half of the fix, for a batch built before it: its plants
    expect ``same``/``variant`` while its judge answers the round's words.
    One label with two spellings is one answer."""
    batch = _battery(store, screened, label_vocabularies=R3_VOCABULARY)
    stale = {
        **batch,
        "plants": [{**p, "expected_labels": ["same", "variant"]} for p in batch["plants"]],
    }
    labels = {p["plant_id"]: {"label_inventory": "requested"} for p in stale["plants"]}
    assert score_plants(stale, labels)["catch_rate"] == 1.0
    # and the reverse: a judge answering the design's word for a label this
    # round re-spelled is not scored as a miss either
    fresh = {p["plant_id"]: {"label_inventory": "same"} for p in batch["plants"]}
    assert score_plants(batch, fresh)["catch_rate"] == 1.0


def test_a_round_whose_vocabulary_cannot_express_same_refuses_the_battery(store, screened):
    """A battery no answer can catch is a batch that fails its own audit
    whatever the judge does, so it is refused at prep rather than shipped."""
    vocabulary = {
        "R3": {
            "labels": ["recombined", "brand-new"],
            "canonical": {"recombined": "recombination", "brand-new": "new-mechanism"},
        }
    }
    with pytest.raises(NoveltyError) as exc:
        _battery(store, screened, label_vocabularies=vocabulary)
    assert getattr(exc.value, "code", None) == "plant_labels_unexpressible"
    assert "same" in str(exc.value) and "--plants-file" in str(exc.value)
    # the same round with its own plants and no harness battery is fine
    batch = _battery(
        store, screened, plants_per_kind=0, label_vocabularies=vocabulary,
        external_plants=[
            _external("P-own-1", "inventory", "A token moves one step per phase.",
                      {"R3": ["recombined"]}),
        ],
    )
    assert [p["plant_id"] for p in batch["plants"]] == ["P-own-1"]


def test_the_primary_set_is_the_one_the_battery_is_scored_against(store, screened):
    """With R3 undeclared the battery is scored against the first declared
    set, so that is the vocabulary its expectation is translated through."""
    batch = _battery(store, screened, judged_sets="R2,R4", label_vocabularies=ROUND_VOCABULARY)
    assert all(p["expected_labels"] == ["requested", "variant"] for p in batch["plants"])
    labels = {p["plant_id"]: {"label_archive": "requested"} for p in batch["plants"]}
    assert score_plants(batch, labels)["catch_rate"] == 1.0
