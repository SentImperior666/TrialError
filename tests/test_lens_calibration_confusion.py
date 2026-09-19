"""Lane FB-7 item 3: what a kappa reader needs BESIDE the kappa.

``kappa_by_set`` gives ``n``, the observed and the expected agreement. A
kappa of 0.4 can be two judges disagreeing everywhere or two judges agreeing
on everything except one label pair they read differently, and only the
matrix says which. A balanced battery asks a further question the card also
did not answer -- "when the right answer was ``absent``, what did this judge
actually say" -- so a round reading its own calibration joined two sheets
against the plants file by hand.

The load-bearing test here is
:func:`test_the_matrix_is_the_one_a_reader_would_count_by_hand`: a six-item
fixture with a matrix written out literally, so the code is checked against
an answer that was worked out independently of it.
"""

from __future__ import annotations

import pytest

from tests._novelty_fixtures import build_round
from trialerror.lens.novelty import (
    build_calibration_batch,
    catch_by_expected,
    confusion_table,
    record_calibration,
)

SEED = "seed-confusion"
CATEGORIES = ["same", "variant", "new-mechanism", "unscreenable"]


# ---------------------------------------------------------------------------
# the matrix, on its own
# ---------------------------------------------------------------------------


def test_the_matrix_is_the_one_a_reader_would_count_by_hand():
    """Six items, worked out by hand:

    ====== ============= =============
    item   judge a       judge b
    ====== ============= =============
    i1     same          same
    i2     same          variant
    i3     variant       variant
    i4     new-mechanism same
    i5     unscreenable  unscreenable
    i6     variant       (nothing)
    ====== ============= =============

    Five pairs, one unpaired. Three cells on the diagonal hold one each
    (same/same, variant/variant, unscreenable/unscreenable) and two off it
    (same->variant, new-mechanism->same). Two disagreements.
    """
    first = {"i1": "same", "i2": "same", "i3": "variant", "i4": "new-mechanism",
             "i5": "unscreenable", "i6": "variant"}
    second = {"i1": "same", "i2": "variant", "i3": "variant", "i4": "same",
              "i5": "unscreenable"}
    confusion, disagreements = confusion_table(first, second, categories=CATEGORIES)

    assert confusion["labels"] == CATEGORIES
    assert confusion["n_paired"] == 5
    assert confusion["n_unpaired"] == 1
    assert confusion["unpaired"] == ["i6"]

    matrix = confusion["matrix"]
    assert matrix["same"]["same"] == 1
    assert matrix["same"]["variant"] == 1
    assert matrix["variant"]["variant"] == 1
    assert matrix["new-mechanism"]["same"] == 1
    assert matrix["unscreenable"]["unscreenable"] == 1
    assert sum(sum(row.values()) for row in matrix.values()) == 5

    assert [(d["idea_id"], d["a"], d["b"]) for d in disagreements] == [
        ("i2", "same", "variant"),
        ("i4", "new-mechanism", "same"),
    ]


def test_the_matrix_is_dense_and_in_the_labels_files_order():
    """Every cell present, zeros included, in the vocabulary's own order --
    a matrix with holes in it cannot be read as a matrix, and a reader
    comparing two cards needs the rows in the same places."""
    confusion, _ = confusion_table({"i1": "same"}, {"i1": "same"}, categories=CATEGORIES)
    assert list(confusion["matrix"]) == CATEGORIES
    for row in confusion["matrix"].values():
        assert list(row) == CATEGORIES
    assert confusion["matrix"]["new-mechanism"]["unscreenable"] == 0


def test_a_label_outside_the_vocabulary_lands_on_the_axes_too():
    """Fix pass V-12. ``confusion_table`` added a ROW for an
    out-of-vocabulary label and left ``labels`` at the declared vocabulary,
    so a reader iterating ``labels x labels`` -- which the docstring's own
    "dense over categories" invites -- silently dropped those counts. A
    matrix whose axes do not list its own rows is the same failure as a
    matrix with holes, one level up.

    ``record_calibration`` refuses such a label before it gets here, so
    this is only reachable by a direct caller -- which is why it was
    non-blocking, and not a reason to leave a helper that answers wrongly
    when it is used."""
    confusion, disagreements = confusion_table(
        {"i1": "made-up", "i2": "same"},
        {"i1": "same", "i2": "same"},
        categories=CATEGORIES,
    )
    labels = confusion["labels"]
    assert "made-up" in labels
    # The declared vocabulary keeps its own order and its own places; the
    # stray label is appended, not interleaved.
    assert labels[: len(CATEGORIES)] == CATEGORIES
    # Dense over the axes it publishes, still.
    assert list(confusion["matrix"]) == labels
    for row in confusion["matrix"].values():
        assert list(row) == labels
    # And no count is lost to a reader who iterates labels x labels.
    total = sum(
        confusion["matrix"][a][b] for a in labels for b in labels
    )
    assert total == confusion["n_paired"] == 2
    assert [d["a"] for d in disagreements] == ["made-up"]


def test_unscreenable_is_a_category_and_two_of_them_agree():
    """FB-6 item 4's word is an ANSWER, not a gap: it is its own cell and it
    sits on the diagonal, exactly as the kappa treats it."""
    confusion, disagreements = confusion_table(
        {"i1": "unscreenable", "i2": "unscreenable"},
        {"i1": "unscreenable", "i2": "same"},
        categories=CATEGORIES,
    )
    assert "unscreenable" in confusion["labels"]
    assert confusion["matrix"]["unscreenable"]["unscreenable"] == 1
    assert confusion["matrix"]["unscreenable"]["same"] == 1
    assert [d["idea_id"] for d in disagreements] == ["i2"]


def test_an_item_neither_judge_labelled_is_absent_not_unpaired():
    confusion, _ = confusion_table({"i1": "same"}, {"i1": "same"}, categories=CATEGORIES)
    assert confusion["n_paired"] == 1
    assert confusion["n_unpaired"] == 0
    assert confusion["unpaired"] == []


def test_disagreements_are_reported_under_the_id_the_judge_saw():
    _confusion, disagreements = confusion_table(
        {"PLANT-area-0": "same"},
        {"PLANT-area-0": "variant"},
        categories=CATEGORIES,
        masked_as={"PLANT-area-0": "J-3"},
    )
    assert disagreements == [{"subject_id": "J-3", "idea_id": "PLANT-area-0", "a": "same", "b": "variant"}]


def test_an_empty_pair_of_sheets_still_returns_a_readable_table():
    confusion, disagreements = confusion_table({}, {}, categories=CATEGORIES)
    assert confusion["n_paired"] == confusion["n_unpaired"] == 0
    assert disagreements == []
    assert sum(sum(row.values()) for row in confusion["matrix"].values()) == 0


# ---------------------------------------------------------------------------
# by_expected
# ---------------------------------------------------------------------------


def _batch(plants, declared=("R3",)):
    return {"plants": list(plants), "judged_sets": list(declared)}


def test_by_expected_groups_on_the_expectation_and_histograms_what_was_said():
    plants = [
        {"plant_id": "C-1", "kind": "area", "expected_labels": {"R3": ["same", "variant"]}},
        {"plant_id": "C-2", "kind": "area", "expected_labels": {"R3": ["variant", "same"]}},
        {"plant_id": "C-3", "kind": "area", "expected_labels": {"R3": ["new-mechanism"]}},
    ]
    sheet = {
        "C-1": {"label_inventory": "same"},
        "C-2": {"label_inventory": "new-mechanism"},
        "C-3": {"label_inventory": None},
    }
    by_expected = catch_by_expected(
        _batch(plants), sheet, {"caught": ["C-1"]}, declared=["R3"]
    )
    # The two plants that expect the same pair group together however the
    # list was typed.
    assert set(by_expected) == {"R3=same,variant", "R3=new-mechanism"}
    pair = by_expected["R3=same,variant"]
    assert pair["n"] == 2
    assert pair["caught"] == 1
    assert pair["given"]["R3"] == {"same": 1, "new-mechanism": 1}
    lone = by_expected["R3=new-mechanism"]
    assert lone["n"] == 1
    assert lone["caught"] == 0
    assert lone["given"]["R3"] == {"(unlabelled)": 1}


def test_by_expected_spans_every_declared_set():
    plants = [
        {"plant_id": "C-1", "kind": "area",
         "expected_labels": {"R3": ["same"], "R4": ["absent"]}},
    ]
    sheet = {"C-1": {"label_inventory": "same", "label_corpus": "adjacent"}}
    by_expected = catch_by_expected(_batch(plants, ("R3", "R4")), sheet, {"caught": []}, declared=["R3", "R4"])
    assert list(by_expected) == ["R3=same|R4=absent"]
    assert by_expected["R3=same|R4=absent"]["given"] == {
        "R3": {"same": 1}, "R4": {"adjacent": 1}
    }


def test_by_expected_is_empty_when_there_is_nothing_to_group():
    assert catch_by_expected(_batch([]), {}, {"caught": []}, declared=["R3"]) == {}


# ---------------------------------------------------------------------------
# on a real card
# ---------------------------------------------------------------------------


@pytest.fixture()
def calibrated(store, tmp_path):
    fixture = build_round(store)
    plants = [
        {"plant_id": f"C-{i}", "kind": "area", "statement": f"A planted statement number {i}.",
         "expected_labels": {"R3": ["same", "variant"]}}
        for i in range(1, 5)
    ] + [
        {"plant_id": "C-5", "kind": "area", "statement": "A fifth planted statement.",
         "expected_labels": {"R3": ["new-mechanism"]}},
        {"plant_id": "C-6", "kind": "area", "statement": "A sixth planted statement.",
         "expected_labels": {"R3": ["new-mechanism"]}},
    ]
    batch = build_calibration_batch(
        store, round_id=fixture["round_id"], external_plants=plants, seed=SEED,
        dossiers={}, batch_fail_on="area", out_dir=tmp_path,
    )
    return fixture, batch, tmp_path


def test_a_real_card_carries_the_matrix_the_disagreements_and_by_expected(store, calibrated):
    fixture, batch, tmp_path = calibrated
    labels_a = {
        "C-1": {"label_inventory": "same"},
        "C-2": {"label_inventory": "same"},
        "C-3": {"label_inventory": "variant"},
        "C-4": {"label_inventory": "new-mechanism"},
        "C-5": {"label_inventory": "unscreenable"},
        "C-6": {"label_inventory": "variant"},
    }
    labels_b = dict(labels_a)
    labels_b["C-2"] = {"label_inventory": "variant"}
    labels_b["C-4"] = {"label_inventory": "same"}
    labels_b["C-6"] = {"label_inventory": None}

    card = record_calibration(
        store, round_id=fixture["round_id"], batch=batch,
        labels_a=labels_a, labels_b=labels_b,
        issued_by_launch=fixture["launches"]["lens-1"], out_dir=tmp_path,
    )

    confusion = card["confusion"]["R3"]
    assert confusion["n_paired"] == 5
    assert confusion["n_unpaired"] == 1
    assert confusion["matrix"]["same"]["same"] == 1
    assert confusion["matrix"]["same"]["variant"] == 1
    assert confusion["matrix"]["new-mechanism"]["same"] == 1
    assert confusion["matrix"]["unscreenable"]["unscreenable"] == 1
    # The unpaired one is excluded from the matrix, so the cells total the
    # pairs and not the items.
    assert sum(sum(row.values()) for row in confusion["matrix"].values()) == 5
    # ...and it is the same n the kappa was computed over.
    assert card["kappa_by_set"]["R3"]["n"] == confusion["n_paired"]

    disagreements = {d["idea_id"]: (d["a"], d["b"]) for d in card["disagreements"]["R3"]}
    assert disagreements == {"C-2": ("same", "variant"), "C-4": ("new-mechanism", "same")}
    # Under the id the JUDGE saw, which is the envelope somebody will open.
    masked_as = {real: masked for masked, real in (batch.get("mask") or {}).items()}
    assert masked_as, "this battery is masked; the test would prove nothing otherwise"
    for entry in card["disagreements"]["R3"]:
        assert entry["subject_id"] == masked_as[entry["idea_id"]]
        assert entry["subject_id"] != entry["idea_id"]

    by_expected_a = card["by_expected"]["a"]
    assert set(by_expected_a) == {"R3=same,variant", "R3=new-mechanism"}
    assert by_expected_a["R3=same,variant"]["n"] == 4
    assert by_expected_a["R3=new-mechanism"]["n"] == 2
    assert by_expected_a["R3=new-mechanism"]["given"]["R3"] == {"unscreenable": 1, "variant": 1}
    assert by_expected_a["R3=new-mechanism"]["caught"] == 0

    by_expected_b = card["by_expected"]["b"]
    assert by_expected_b["R3=new-mechanism"]["given"]["R3"] == {"unscreenable": 1, "(unlabelled)": 1}


def test_the_additions_did_not_move_anything_that_was_already_on_the_card(store, calibrated):
    fixture, batch, tmp_path = calibrated
    sheet = {p["plant_id"]: {"label_inventory": "same"} for p in batch["plants"]}
    card = record_calibration(
        store, round_id=fixture["round_id"], batch=batch,
        labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=fixture["launches"]["lens-1"], out_dir=tmp_path,
    )
    for key in (
        "catch_by_kind", "catch_by_set", "catch_rate", "kappa_by_set",
        "r_embedding_human", "rated_pairs", "warnings", "misses_by_id",
        "baseline_cosine_distribution", "labels_sha256",
    ):
        assert key in card
    assert set(card["confusion"]) == set(card["disagreements"]) == set(card["kappa_by_set"])
    assert set(card["by_expected"]) == {"a", "b"}
