"""Lane FB-7 item 9: the duplicate-verdict check is scoped to the round.

``--record-calibration`` refused a live supplement battery because twelve of
its plants re-used the ``plant_id`` of an earlier battery's plants, and the
only offered way through -- ``--supersede`` -- would have marked the EARLIER
round's verdict rows as replaced. That is rewriting settled history to
record a new battery.

The rule is right for a RECORD, whose ``idea_id`` is unique across the
programme, and wrong for a PLANT, whose id is whatever the round's plants
file called it. The guard is now keyed by round and batch as well, and the
three claims tested here are the three the brief names: two rounds sharing a
plant id both record; ``--supersede`` in round B leaves round A's rows
untouched; a same-round re-record is still refused.

A NULL round is treated as UNKNOWN rather than "no round" -- a row written
before the migration may be this round's, so it still blocks, which is
exactly what it did before. That is tested too, because it is the one place
the change could have quietly loosened a rule it was only meant to scope.
"""

from __future__ import annotations

import pytest

from tests._novelty_fixtures import build_round
from trialerror.lens.novelty import (
    CALIBRATION_PROCEDURE_VERSION,
    PROCEDURE,
    NoveltyError,
    build_calibration_batch,
    record_calibration,
)

SEED = "seed-round-scope"


def _plant(pid):
    return {
        "plant_id": pid,
        "kind": "area",
        "statement": f"A planted statement for {pid}.",
        "expected_labels": {"R3": ["same", "variant"]},
    }


def _sheet(ids, label="same"):
    return {pid: {"label_inventory": label} for pid in ids}


def _verdict_rows(store, subject_id):
    return [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT verdict_id, subject_id, label, round_id, batch_id, evidence FROM verdict "
            "WHERE procedure = ? AND procedure_version = ? AND subject_id = ? ORDER BY ts, verdict_id",
            (PROCEDURE, CALIBRATION_PROCEDURE_VERSION, subject_id),
        ).fetchall()
    ]


@pytest.fixture()
def rounds(store, tmp_path):
    """Two rounds in one store, each with a battery that reuses "C-1"."""
    fixture = build_round(store)
    launch = fixture["launches"]["lens-1"]
    batches = {}
    for name in ("round-a", "round-b"):
        batches[name] = build_calibration_batch(
            store, round_id=name, external_plants=[_plant("C-1"), _plant("C-2")], seed=SEED,
            dossiers={}, batch_fail_on="area", batch_id=f"cal-{name}", out_dir=tmp_path / name,
        )
    return fixture, launch, batches, tmp_path


# ---------------------------------------------------------------------------
# the three claims
# ---------------------------------------------------------------------------


def test_two_rounds_sharing_a_plant_id_both_record(store, rounds):
    _fixture, launch, batches, tmp_path = rounds
    sheet = _sheet(["C-1", "C-2"])

    card_a = record_calibration(
        store, round_id="round-a", batch=batches["round-a"],
        labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-a",
    )
    # The second round is not blocked by the first, and supersedes nothing.
    card_b = record_calibration(
        store, round_id="round-b", batch=batches["round-b"],
        labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-b",
    )
    assert card_a["n_verdicts"] and card_b["n_verdicts"]
    assert card_b["superseded"] == []

    rows = _verdict_rows(store, "C-1")
    assert {r["round_id"] for r in rows} == {"round-a", "round-b"}
    assert {r["batch_id"] for r in rows} == {"cal-round-a", "cal-round-b"}


def test_supersede_in_round_b_leaves_round_a_untouched(store, rounds):
    _fixture, launch, batches, tmp_path = rounds
    sheet = _sheet(["C-1", "C-2"])
    record_calibration(
        store, round_id="round-a", batch=batches["round-a"],
        labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-a",
    )
    before_a = [r for r in _verdict_rows(store, "C-1") if r["round_id"] == "round-a"]
    assert before_a

    record_calibration(
        store, round_id="round-b", batch=batches["round-b"],
        labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-b",
    )
    card = record_calibration(
        store, round_id="round-b", batch=batches["round-b"],
        labels_a=_sheet(["C-1", "C-2"], label="variant"),
        labels_b=_sheet(["C-1", "C-2"], label="variant"),
        issued_by_launch=launch, supersede=True, out_dir=tmp_path / "round-b",
    )
    assert card["superseded"]

    after_a = [r for r in _verdict_rows(store, "C-1") if r["round_id"] == "round-a"]
    assert after_a == before_a

    # ...and not one of round A's ids is named as superseded, anywhere.
    round_a_ids = {r["verdict_id"] for r in before_a}
    assert not (round_a_ids & set(card["superseded"]))
    for row in _verdict_rows(store, "C-1"):
        for a_id in round_a_ids:
            assert a_id not in str(row["evidence"]) or row["verdict_id"] == a_id


def test_a_same_round_re_record_is_still_refused(store, rounds):
    _fixture, launch, batches, tmp_path = rounds
    sheet = _sheet(["C-1", "C-2"])
    record_calibration(
        store, round_id="round-a", batch=batches["round-a"],
        labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-a",
    )
    with pytest.raises(NoveltyError) as excinfo:
        record_calibration(
            store, round_id="round-a", batch=batches["round-a"],
            labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-a",
        )
    message = str(excinfo.value)
    assert "C-1" in message
    # The refusal names the round of the rows it found -- item 9's own
    # requirement, and the thing the old message could not say.
    assert "round-a" in message
    assert "cal-round-a" in message


def test_a_second_batch_in_the_same_round_is_not_blocked(store, rounds):
    """Batch is part of the key too: a round that seeds two batteries is not
    re-recording when it records the second."""
    fixture, launch, _batches, tmp_path = rounds
    first = build_calibration_batch(
        store, round_id="round-c", external_plants=[_plant("C-1")], seed=SEED,
        dossiers={}, batch_fail_on="area", batch_id="cal-1", out_dir=tmp_path / "c1",
    )
    second = build_calibration_batch(
        store, round_id="round-c", external_plants=[_plant("C-1")], seed=SEED,
        dossiers={}, batch_fail_on="area", batch_id="cal-2", out_dir=tmp_path / "c2",
    )
    sheet = _sheet(["C-1"])
    record_calibration(
        store, round_id="round-c", batch=first, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=launch, out_dir=tmp_path / "c1",
    )
    card = record_calibration(
        store, round_id="round-c", batch=second, labels_a=sheet, labels_b=dict(sheet),
        issued_by_launch=launch, out_dir=tmp_path / "c2",
    )
    assert card["superseded"] == []
    assert {r["batch_id"] for r in _verdict_rows(store, "C-1")} == {"cal-1", "cal-2"}


# ---------------------------------------------------------------------------
# a NULL round is UNKNOWN, not "no round"
# ---------------------------------------------------------------------------


def test_a_row_with_no_round_still_blocks(store, rounds):
    """A verdict written before the migration cannot say which round it
    belongs to, so it may be this one's and it still blocks -- which is
    exactly what it did before. Scoping a guard must not loosen it."""
    _fixture, launch, batches, tmp_path = rounds
    sheet = _sheet(["C-1", "C-2"])
    record_calibration(
        store, round_id="round-a", batch=batches["round-a"],
        labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-a",
    )
    with store.knowledge as conn:
        conn.execute(
            "UPDATE verdict SET round_id = NULL, batch_id = NULL WHERE procedure_version = ?",
            (CALIBRATION_PROCEDURE_VERSION,),
        )
    # Another round is now blocked by it, because nothing says it is not
    # that round's.
    with pytest.raises(NoveltyError, match=r"\(unrecorded\)"):
        record_calibration(
            store, round_id="round-b", batch=batches["round-b"],
            labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-b",
        )


def test_the_lookup_without_a_round_is_the_whole_store(store, rounds):
    """The pre-item-9 behaviour is still reachable, and is the default for a
    caller with no round in hand."""
    from trialerror.lens.novelty import _existing_novelty_verdicts

    _fixture, launch, batches, tmp_path = rounds
    sheet = _sheet(["C-1", "C-2"])
    record_calibration(
        store, round_id="round-a", batch=batches["round-a"],
        labels_a=sheet, labels_b=dict(sheet), issued_by_launch=launch, out_dir=tmp_path / "round-a",
    )
    whole = _existing_novelty_verdicts(
        store, ["C-1"], procedure_version=CALIBRATION_PROCEDURE_VERSION
    )
    assert whole
    scoped_elsewhere = _existing_novelty_verdicts(
        store, ["C-1"], procedure_version=CALIBRATION_PROCEDURE_VERSION, round_id="round-b",
    )
    assert scoped_elsewhere == {}
