"""Lane FB-7 item 5: in-round plants, per batch.

``--plants-file`` takes one file, so a round that seeds different plants
into each judged batch had to split the file by hand and keep the splits in
step with the batch ids -- which is exactly the bookkeeping a batch id
exists to do.

A plant may now declare ``batch``. One that does rides in THAT batch only;
one that does not rides in every batch, which is what every plants file did
before the key existed. The second half matters as much as the first: a
``--batch-id`` that matches nothing must not quietly build a battery out of
the unbatched plants alone, so it warns and the warning reaches the
envelope.
"""

from __future__ import annotations

import json

import pytest

from tests._novelty_fixtures import build_round
from trialerror.lens.novelty import (
    build_calibration_batch,
    build_judged_batch,
    load_external_plants,
    plants_for_batch,
    run_mechanical_screen,
    score_plants,
)

SEED = "seed-per-batch"


def _plant(pid, batch=None, kind="area", **extra):
    out = {
        "plant_id": pid,
        "kind": kind,
        "statement": f"A planted statement for {pid}.",
        "expected_labels": {"R3": ["same", "variant"]},
        **extra,
    }
    if batch is not None:
        out["batch"] = batch
    return out


TWO_BATCHES = [
    _plant("P-A1", batch="judged-0"),
    _plant("P-A2", batch="judged-0"),
    _plant("P-B1", batch="judged-1"),
    _plant("P-B2", batch="judged-1"),
    _plant("P-EVERY"),
]


# ---------------------------------------------------------------------------
# the filter, on its own
# ---------------------------------------------------------------------------


def test_two_batches_get_disjoint_plants_plus_the_unbatched_one():
    declared = load_external_plants(TWO_BATCHES, judged_sets=["R3"])
    first, warn_first = plants_for_batch(declared, batch_id="judged-0")
    second, warn_second = plants_for_batch(declared, batch_id="judged-1")

    assert [p["plant_id"] for p in first] == ["P-A1", "P-A2", "P-EVERY"]
    assert [p["plant_id"] for p in second] == ["P-B1", "P-B2", "P-EVERY"]
    assert warn_first == warn_second == []

    batched_first = {p["plant_id"] for p in first} - {"P-EVERY"}
    batched_second = {p["plant_id"] for p in second} - {"P-EVERY"}
    assert not (batched_first & batched_second)


def test_an_unbatched_file_is_unchanged():
    """What every plants file did before this key existed."""
    declared = load_external_plants([_plant("P-1"), _plant("P-2")], judged_sets=["R3"])
    for batch_id in ("judged-0", "judged-7", "anything"):
        kept, warnings = plants_for_batch(declared, batch_id=batch_id)
        assert [p["plant_id"] for p in kept] == ["P-1", "P-2"]
        assert warnings == []


def test_an_unknown_batch_id_injects_none_of_them_and_says_so():
    declared = load_external_plants(TWO_BATCHES, judged_sets=["R3"])
    kept, warnings = plants_for_batch(declared, batch_id="judged-9")
    assert [p["plant_id"] for p in kept] == ["P-EVERY"]
    assert len(warnings) == 1
    message = warnings[0]
    assert "judged-9" in message
    assert "judged-0" in message and "judged-1" in message
    assert "4 plant(s) are batched" in message


def test_a_batch_key_is_compared_as_a_string():
    """A JSON file written by hand must not turn on 2 versus "2"."""
    declared = load_external_plants([_plant("P-1", batch=2), _plant("P-2", batch="2")], judged_sets=["R3"])
    kept, warnings = plants_for_batch(declared, batch_id="2")
    assert [p["plant_id"] for p in kept] == ["P-1", "P-2"]
    assert warnings == []
    kept_int, _ = plants_for_batch(declared, batch_id=2)
    assert [p["plant_id"] for p in kept_int] == ["P-1", "P-2"]


def test_a_blank_batch_reads_as_unbatched():
    declared = load_external_plants([_plant("P-1", batch="   ")], judged_sets=["R3"])
    kept, warnings = plants_for_batch(declared, batch_id="judged-3")
    assert [p["plant_id"] for p in kept] == ["P-1"]
    assert warnings == []


def test_surrounding_whitespace_is_stripped_on_both_sides(caplog):
    """Fix pass V-11. The first build stripped a batch value to decide
    whether it counted as "unbatched" and then compared it UNSTRIPPED, so a
    plant declaring `" judged-1 "` was counted as batched -- and therefore
    excluded from every other batch -- while matching no ``--batch-id`` at
    all. It landed nowhere. It did warn, which is why this was non-blocking
    and not why it was acceptable: a plants file written by hand must not
    turn on a trailing space in either direction."""
    declared = load_external_plants(
        [_plant("P-1", batch=" judged-1 "), _plant("P-2", batch="judged-2")],
        judged_sets=["R3"],
    )
    kept, warnings = plants_for_batch(declared, batch_id="judged-1")
    assert [p["plant_id"] for p in kept] == ["P-1"]
    assert warnings == []

    # ...and the same value on the --batch-id side.
    kept, warnings = plants_for_batch(declared, batch_id="  judged-1  ")
    assert [p["plant_id"] for p in kept] == ["P-1"]
    assert warnings == []

    # The warning, when it fires, names the stripped batches rather than
    # the file's incidental whitespace.
    _kept, warnings = plants_for_batch(declared, batch_id="judged-9")
    assert warnings and "'judged-1'" in warnings[0] and "'judged-2'" in warnings[0]


# ---------------------------------------------------------------------------
# on a real judged batch
# ---------------------------------------------------------------------------


@pytest.fixture()
def screened(store, tmp_path):
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"], out_dir=tmp_path
    )
    return fixture, mechanical, tmp_path


def _judged(store, screened, batch_id, plants=TWO_BATCHES, **kwargs):
    fixture, mechanical, tmp_path = screened
    return build_judged_batch(
        store, round_id=fixture["round_id"], dossiers=mechanical["dossiers"], seed=SEED,
        batch_id=batch_id, external_plants=plants, out_dir=tmp_path, **kwargs
    )


def test_two_judged_batches_carry_disjoint_round_plants(store, screened):
    first = _judged(store, screened, "judged-0")
    second = _judged(store, screened, "judged-1")

    assert first["plants_injected"] == ["P-A1", "P-A2", "P-EVERY"]
    assert second["plants_injected"] == ["P-B1", "P-B2", "P-EVERY"]
    assert first["warnings"] == second["warnings"] == []

    ids_first = {p["plant_id"] for p in first["plants"]}
    ids_second = {p["plant_id"] for p in second["plants"]}
    assert "P-A1" in ids_first and "P-A1" not in ids_second
    assert "P-B1" in ids_second and "P-B1" not in ids_first
    assert "P-EVERY" in ids_first and "P-EVERY" in ids_second

    # And no envelope in either batch is built for a plant it did not get.
    subjects_first = {e["subject_id"] for e in first["envelopes"]}
    assert not ({"P-B1", "P-B2"} & subjects_first)


def test_an_unknown_batch_id_says_so_on_the_batch(store, screened):
    batch = _judged(store, screened, "judged-9")
    assert batch["plants_injected"] == ["P-EVERY"]
    assert len(batch["warnings"]) == 1
    assert "judged-9" in batch["warnings"][0]


def test_plants_injected_survives_an_edit_to_the_plants_file(store, screened):
    """The record is on the batch rather than re-derivable from the file,
    because the file can be edited between two batches."""
    _fixture, _mechanical, tmp_path = screened
    batch = _judged(store, screened, "judged-0")
    on_disk = json.loads((tmp_path / "judged" / "judged-0.json").read_text(encoding="utf-8"))
    assert on_disk["plants_injected"] == batch["plants_injected"] == ["P-A1", "P-A2", "P-EVERY"]


def test_batch_fail_on_is_evaluated_over_this_batchs_plants(store, screened):
    """A miss is scored against the plants THIS batch actually carried, so
    a plant seeded into the other batch can neither be missed nor caught
    here."""
    first = _judged(store, screened, "judged-0", batch_fail_on="area")
    sheet = {p["plant_id"]: {"label_inventory": "same"} for p in first["plants"]}
    scored = score_plants(first, sheet, batch_fail_on="area")
    scored_ids = set(scored["caught"]) | {m["plant_id"] for m in scored["missed"]} | set(scored["unlabelled"])
    assert "P-B1" not in scored_ids and "P-B2" not in scored_ids
    assert {"P-A1", "P-A2", "P-EVERY"} <= scored_ids


# ---------------------------------------------------------------------------
# a calibration reads the same file the same way
# ---------------------------------------------------------------------------


def test_a_calibration_filters_by_batch_too(store, tmp_path):
    """One plants file must not mean two different things depending on
    which verb read it."""
    fixture = build_round(store)
    batch = build_calibration_batch(
        store, round_id=fixture["round_id"], external_plants=TWO_BATCHES, seed=SEED,
        dossiers={}, batch_fail_on="area", batch_id="judged-1", out_dir=tmp_path,
    )
    assert batch["plants_injected"] == ["P-B1", "P-B2", "P-EVERY"]
    assert {p["plant_id"] for p in batch["plants"]} == {"P-B1", "P-B2", "P-EVERY"}
    assert batch["warnings"] == []


def test_a_calibration_with_no_batch_id_still_builds(store, tmp_path):
    fixture = build_round(store)
    batch = build_calibration_batch(
        store, round_id=fixture["round_id"], external_plants=[_plant("C-1")], seed=SEED,
        dossiers={}, batch_fail_on="area", out_dir=tmp_path,
    )
    assert batch["plants_injected"] == ["C-1"]
    assert batch["warnings"] == []
    assert batch["batch_id"]


def test_no_judge_view_carries_the_batch_key(store, screened):
    batch = _judged(store, screened, "judged-0")
    for view in batch["judge_views"]:
        assert "batch" not in view
        assert "batch" not in view["record"]
