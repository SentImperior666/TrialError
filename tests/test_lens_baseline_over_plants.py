"""Lane FB-6 item 5: a calibration's baseline is over its PLANTS.

``baseline_cosine_distribution`` read the round's dossiers, so a calibration
-- which exists precisely for the moment before a round has a single record
-- reported ``n 0`` on the one card whose job is to say what a cosine means
in this corpus. Every other number on that card (a kappa, a catch rate, a
Pearson r) is read against it.

The plants have exactly the number that was wanted: each carries its own
retrieved bundle, and ``retrieved[].similarity`` is the same
statement-to-corpus cosine a record's ``candidate_hits`` carries. ``over``
now says which population the percentiles are about, because the artifact
would otherwise not distinguish them.
"""

from __future__ import annotations

import pytest

from trialerror.lens.novelty import (
    DEFAULT_CANDIDATE_HIT_SIMILARITY,
    baseline_cosine_distribution,
    build_calibration_batch,
    record_calibration,
    run_mechanical_screen,
)

from tests._inventory_fixtures import build_corpus_with_inventory
from tests._novelty_fixtures import build_round

SEED = "seed-baseline"


def _chunk_texts(store, n=2):
    return [
        str(r["text"])
        for r in store.knowledge.execute(
            "SELECT c.text AS text FROM chunk c JOIN document d ON d.doc_id = c.doc_id "
            "JOIN source s ON s.source_id = d.source_id WHERE s.kind = 'paper' "
            "ORDER BY d.rel_path, c.seq LIMIT ?",
            (n,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# the function
# ---------------------------------------------------------------------------


def test_the_records_arm_is_unchanged_and_says_so():
    over_records = baseline_cosine_distribution(
        {
            "IDEA-1": {"candidate_hits": {"R4": [{"similarity": 0.61}, {"similarity": 0.72}]}},
            "IDEA-2": {"candidate_hits": {"R4": []}},
        }
    )
    assert over_records["over"] == "records"
    assert over_records["n"] == over_records["n_records"] == 2
    assert over_records["n_with_neighbour"] == 1
    assert over_records["p50"] == over_records["max"] == 0.72


def test_the_plants_arm_cuts_the_same_percentiles_on_the_plants_own_cosines():
    plants = [
        {"plant_id": "C-1", "retrieved": [{"similarity": 0.50}, {"similarity": 0.90}]},
        {"plant_id": "C-2", "retrieved": [{"similarity": 0.60}]},
        {"plant_id": "C-3", "retrieved": [{"similarity": 0.70}]},
        {"plant_id": "C-4", "retrieved": []},
    ]
    block = baseline_cosine_distribution(plants=plants)
    assert block["over"] == "plants"
    assert block["n"] == 4
    assert block["n_with_neighbour"] == 3
    # nearest per plant, sorted: [0.6, 0.7, 0.9] -- the index-round cuts
    assert block["min"] == 0.6
    assert block["p50"] == 0.7
    assert block["p90"] == 0.9
    assert block["max"] == 0.9
    assert block["hit_similarity_floor"] == DEFAULT_CANDIDATE_HIT_SIMILARITY


def test_the_two_arms_do_not_mix():
    """``plants`` wins when both are handed in: a caller that named the
    population meant it."""
    block = baseline_cosine_distribution(
        {"IDEA-1": {"candidate_hits": {"R4": [{"similarity": 0.99}]}}},
        plants=[{"plant_id": "C-1", "retrieved": [{"similarity": 0.55}]}],
    )
    assert block["over"] == "plants"
    assert block["max"] == 0.55


# ---------------------------------------------------------------------------
# through a calibration
# ---------------------------------------------------------------------------


def test_a_two_plant_fixture_yields_n_two_and_the_right_percentiles(store, tmp_path):
    """One plant cut verbatim from a corpus chunk (cosine 1.0 under the
    fixture's hash-derived backend) and one that matches nothing."""
    build_corpus_with_inventory(store)
    verbatim = _chunk_texts(store, 1)[0]
    plants = [
        {"plant_id": "C-1", "kind": "area", "statement": verbatim,
         "expected_labels": {"R3": ["same", "variant"]}},
        {"plant_id": "C-2", "kind": "area",
         "statement": "A plant that restates nothing anybody has written down.",
         "expected_labels": {"R3": ["new-mechanism"]}},
    ]
    batch = build_calibration_batch(
        store, round_id="round-empty", external_plants=plants, seed=SEED,
        batch_fail_on="area", out_dir=tmp_path,
    )
    block = batch["baseline_cosine_distribution"]
    assert block["over"] == "plants"
    assert block["n"] == 2
    assert block["n_with_neighbour"] == 1
    assert block["min"] == block["p50"] == block["max"] == 1.0

    nearest = {
        p["plant_id"]: max((float(h["similarity"]) for h in p.get("retrieved") or ()), default=None)
        for p in batch["plants"]
    }
    assert nearest == {"C-1": 1.0, "C-2": None}


def test_the_card_reports_the_same_population_as_the_batch(store, tmp_path):
    build_corpus_with_inventory(store)
    from tests._inventory_fixtures import bootstrap_launch

    launch = bootstrap_launch(store)
    verbatim = _chunk_texts(store, 1)[0]
    plants = [
        {"plant_id": "C-1", "kind": "area", "statement": verbatim,
         "expected_labels": {"R3": ["same", "variant"]}},
        {"plant_id": "C-2", "kind": "area", "statement": "Another plant, matching nothing.",
         "expected_labels": {"R3": ["new-mechanism"]}},
    ]
    batch = build_calibration_batch(
        store, round_id="round-empty", external_plants=plants, seed=SEED,
        batch_fail_on="area", out_dir=tmp_path,
    )
    sheet = {
        "C-1": {"label_inventory": "same"},
        "C-2": {"label_inventory": "new-mechanism"},
    }
    card = record_calibration(
        store, round_id="round-empty", batch=batch, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=launch, out_dir=tmp_path,
    )
    assert card["baseline_cosine_distribution"] == batch["baseline_cosine_distribution"]


def test_a_round_keeps_its_record_baseline(store, tmp_path):
    """``dossiers`` still names the round's records, for a caller that wants
    that population on the card."""
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    plants = [
        {"plant_id": "C-1", "kind": "area", "statement": "A plant.",
         "expected_labels": {"R3": ["same", "variant"]}},
    ]
    batch = build_calibration_batch(
        store, round_id=fixture["round_id"], external_plants=plants, seed=SEED,
        dossiers=mechanical["dossiers"], batch_fail_on="area", out_dir=tmp_path,
    )
    card = record_calibration(
        store, round_id=fixture["round_id"], batch=batch,
        labels_a={"C-1": {"label_inventory": "same"}},
        labels_b={"C-1": {"label_inventory": "same"}},
        issued_by_launch=fixture["launches"]["lens-1"],
        dossiers=mechanical["dossiers"], out_dir=tmp_path,
    )
    assert batch["baseline_cosine_distribution"]["over"] == "plants"
    assert card["baseline_cosine_distribution"]["over"] == "records"
    assert card["baseline_cosine_distribution"]["n"] == len(mechanical["dossiers"])
