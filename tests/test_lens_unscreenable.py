"""Lane FB-6 item 4: ``unscreenable`` is an answer for every declared set.

Observed on a live calibration: two judges answered ``unscreenable`` against
R2 -- the design's own word, for a record that states no mechanism at all --
and the recorder refused it, because the round had re-spelled R2's labels and
its list did not repeat the word. The two answers had to be passed as
UNLABELLED, which says something different and false: a judge that says
"there is nothing here to compare" has judged; a judge that says nothing has
not, and the two are counted differently everywhere downstream.

The word is a statement about the RECORD, not about a reference set. So it is
accepted for every set a round declares, it is a non-catch for a plant (a
plant is a row that exists, so ``unscreenable`` is wrong about it), it is its
own category in the kappa, and it is written with
``label_canonical = unscreenable`` however the round spells it.

What does NOT change is what the judge is SHOWN: ``label_block_for_judge``
still lists each set's own vocabulary, so no round's envelopes move.
"""

from __future__ import annotations

import pytest

from trialerror.lens.novelty import (
    LITERATURE_LABELS,
    UNSCREENABLE_LABEL,
    accepted_labels_for_set,
    build_calibration_batch,
    canonical_label,
    label_block_for_judge,
    load_label_vocabularies,
    record_calibration,
    record_novelty_verdicts,
    run_mechanical_screen,
    score_plants,
    unscreenable_word,
)

from tests._novelty_fixtures import build_round

SEED = "seed-unscreenable"

#: R2 re-spelled with no word for "no mechanism here" -- the live shape.
ROUND_VOCABULARY = {
    "R2": {
        "labels": ["requested", "variant", "new"],
        "canonical": {"requested": "same", "variant": "variant", "new": "new-mechanism"},
    },
    "R4": {
        "labels": ["present", "adjacent", "absent"],
        "canonical": {"present": "stated", "adjacent": "adjacent", "absent": "absent"},
    },
}

PLANTS = [
    {"plant_id": "C-1", "kind": "area", "statement": "A plant cut from an archive row.",
     "expected_labels": {"R2": ["requested", "variant"]}},
    {"plant_id": "C-2", "kind": "area", "statement": "A second plant cut from an archive row.",
     "expected_labels": {"R2": ["requested", "variant"]}},
]


@pytest.fixture()
def screened(store):
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    return {**fixture, "dossiers": mechanical["dossiers"]}


@pytest.fixture()
def vocabularies():
    return load_label_vocabularies(ROUND_VOCABULARY, judged_sets="R2,R4")


# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------


def test_the_word_is_accepted_for_a_set_whose_list_never_offers_it(vocabularies):
    assert "unscreenable" not in ROUND_VOCABULARY["R2"]["labels"]
    assert accepted_labels_for_set(vocabularies, reference_set="R2") == [
        "requested", "variant", "new", UNSCREENABLE_LABEL,
    ]
    assert accepted_labels_for_set(vocabularies, reference_set="R4") == [
        "present", "adjacent", "absent", UNSCREENABLE_LABEL,
    ]


def test_a_round_that_re_spells_the_word_is_accepted_under_its_own_spelling():
    vocabularies = load_label_vocabularies(
        {
            "R3": {
                "labels": ["same", "variant", "recombination", "new-mechanism", "no-mechanism-stated"],
                "canonical": {
                    "same": "same", "variant": "variant", "recombination": "recombination",
                    "new-mechanism": "new-mechanism", "no-mechanism-stated": "unscreenable",
                },
            }
        },
        judged_sets="R3,R4",
    )
    assert unscreenable_word(vocabularies) == UNSCREENABLE_LABEL
    # R3 offers it, so it is not appended twice; R4's list gains it.
    assert accepted_labels_for_set(vocabularies, reference_set="R4")[-1] == UNSCREENABLE_LABEL


def test_the_default_round_gains_the_word_on_the_literature_set_only():
    assert accepted_labels_for_set(None, reference_set="R3") == [
        "same", "variant", "recombination", "new-mechanism", "unscreenable",
    ]
    assert accepted_labels_for_set(None, reference_set="R4") == [
        *LITERATURE_LABELS, UNSCREENABLE_LABEL,
    ]


def test_it_canonicalises_to_the_designs_word_for_every_set(vocabularies):
    assert canonical_label(vocabularies, reference_set="R2", label=UNSCREENABLE_LABEL) == (
        UNSCREENABLE_LABEL
    )
    assert canonical_label(vocabularies, reference_set="R4", label=UNSCREENABLE_LABEL) == (
        UNSCREENABLE_LABEL
    )
    # nothing else moved
    assert canonical_label(vocabularies, reference_set="R2", label="requested") == "same"


def test_what_the_judge_is_shown_does_not_move(vocabularies):
    block = label_block_for_judge(vocabularies)
    assert block["R2"] == ["requested", "variant", "new"]
    assert block["R4"] == ["present", "adjacent", "absent"]


# ---------------------------------------------------------------------------
# recording a calibration
# ---------------------------------------------------------------------------


def _calibration(store, screened, tmp_path, **kwargs):
    return build_calibration_batch(
        store, round_id=screened["round_id"], external_plants=PLANTS, seed=SEED,
        dossiers=screened["dossiers"], judged_sets="R2,R4", batch_fail_on="area",
        label_vocabularies=ROUND_VOCABULARY, out_dir=tmp_path, **kwargs,
    )


def test_a_sheet_carrying_unscreenable_records(store, screened, tmp_path):
    batch = _calibration(store, screened, tmp_path)
    a = {
        "C-1": {"label_archive": UNSCREENABLE_LABEL, "label_corpus": UNSCREENABLE_LABEL},
        "C-2": {"label_archive": "requested", "label_corpus": "absent"},
    }
    b = {
        "C-1": {"label_archive": UNSCREENABLE_LABEL, "label_corpus": "absent"},
        "C-2": {"label_archive": "variant", "label_corpus": "absent"},
    }
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=a, labels_b=b,
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert card["n_verdicts"] == 8

    rows = store.knowledge.execute(
        "SELECT subject_id, label, label_canonical FROM verdict WHERE label LIKE 'R2:%'"
    ).fetchall()
    written = {(r["label"], r["label_canonical"]) for r in rows}
    assert (f"R2:{UNSCREENABLE_LABEL}", f"R2:{UNSCREENABLE_LABEL}") in written


def test_the_plant_that_drew_it_is_a_non_catch_not_an_unlabelled(store, screened, tmp_path):
    batch = _calibration(store, screened, tmp_path)
    sheet = {
        "C-1": {"label_archive": UNSCREENABLE_LABEL},
        "C-2": {"label_archive": "requested"},
    }
    scored = score_plants(batch, sheet, judged_sets="R2,R4", batch_fail_on="area")
    assert scored["caught"] == ["C-2"]
    assert [m["plant_id"] for m in scored["missed"]] == ["C-1"]
    assert scored["unlabelled"] == [], "a judge that answered unscreenable answered"
    assert scored["failures"] == ["C-1"]
    assert scored["by_set"]["R2"]["missed"] == ["C-1"]


def test_kappa_counts_it_as_its_own_category(store, screened, tmp_path):
    batch = _calibration(store, screened, tmp_path)
    sheet = {
        "C-1": {"label_archive": UNSCREENABLE_LABEL},
        "C-2": {"label_archive": UNSCREENABLE_LABEL},
    }
    card = record_calibration(
        store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    block = card["kappa_by_set"]["R2"]
    # Both judges used one category throughout: observed 1.0, expected 1.0,
    # and the documented convention reads that as agreement rather than 0.
    assert block["n"] == 2
    assert block["observed_agreement"] == 1.0
    assert block["kappa"] == 1.0


def test_a_word_that_is_neither_in_the_vocabulary_nor_unscreenable_is_still_refused(
    store, screened, tmp_path
):
    batch = _calibration(store, screened, tmp_path)
    sheet = {"C-1": {"label_archive": "made-up"}}
    with pytest.raises(Exception) as exc:
        record_calibration(
            store, round_id=screened["round_id"], batch=batch, labels_a=sheet, labels_b=dict(sheet),
            issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
        )
    assert "made-up" in str(exc.value)
    assert UNSCREENABLE_LABEL in str(exc.value), "the refusal lists what IS accepted"


# ---------------------------------------------------------------------------
# recording a round's own verdicts, the same path
# ---------------------------------------------------------------------------


def test_a_round_records_it_against_a_set_whose_list_never_offered_it(store, screened, tmp_path):
    from trialerror.lens.novelty import build_judged_batch

    batch = build_judged_batch(
        store, round_id=screened["round_id"], dossiers=screened["dossiers"], seed=SEED,
        plants_per_kind=0, judged_sets="R2,R4", label_vocabularies=ROUND_VOCABULARY,
        out_dir=tmp_path,
    )
    subject = batch["scope"]["scope"][0]
    labels = {
        i: {"label_archive": "variant", "label_corpus": "present"} for i in batch["scope"]["scope"]
    }
    labels[subject] = {
        "label_archive": UNSCREENABLE_LABEL,
        "label_corpus": UNSCREENABLE_LABEL,
        "seeds": [{"ref": "DOC-1", "label": "on-topic"}],
    }
    result = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], out_dir=tmp_path,
    )
    assert subject in result["seeds"]["unscreenable_below_bar"]
    rows = store.knowledge.execute(
        "SELECT label, label_canonical FROM verdict WHERE subject_id = ? AND label LIKE 'R2:%'",
        (subject,),
    ).fetchall()
    # The batch carries no plants, so every label is stamped with the
    # plants_failed caveat -- the word itself is what this asserts.
    assert len(rows) == 1
    assert rows[0]["label"].startswith(f"R2:{UNSCREENABLE_LABEL}")
    assert rows[0]["label_canonical"].startswith(f"R2:{UNSCREENABLE_LABEL}")
