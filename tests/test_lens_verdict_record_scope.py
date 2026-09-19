"""Fix pass V-3: the round is a record's grain; the batch is a plant's.

Item 9 of this lane scoped the duplicate-verdict guard by ``(round_id,
batch_id)`` for BOTH recorders. That is right for ``record_calibration``,
whose subjects are ``plant_id``s and whose two batteries of one round
legitimately re-use them. It is a regression for ``record_novelty_verdicts``,
whose subjects are ``idea_id``s: a round that preps two judged batches --
which is the shape item 5 of this very lane exists to support -- could record
the same idea twice, with no refusal, no ``--supersede`` and nothing joining
the two rows. Design 5.2(3), "one submission per idea per judge", undone by a
key.

What the guard must do, and what is asserted here:

* a second judged batch of the SAME round re-offering an already-recorded
  idea is refused, and the refusal names the batch the prior rows came from
  (so "this idea already answered in judged-0" is readable without SQL);
* ``--supersede`` gets the second recording through and names the rows it
  replaces;
* another ROUND is still invisible -- the thing item 9 actually scoped;
* a plant id in a calibration is still keyed by batch, so item 9's own case
  has not been reverted along with the regression.
"""

from __future__ import annotations

import json

import pytest

from tests._novelty_fixtures import build_round
from trialerror.lens.novelty import (
    PROCEDURE,
    PROCEDURE_VERSION,
    NoveltyError,
    build_judged_batch,
    record_novelty_verdicts,
    run_mechanical_screen,
)

SEED = "seed-record-scope"


@pytest.fixture()
def screened(store):
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    return {**fixture, "mechanical": mechanical, "dossiers": mechanical["dossiers"]}


def _batch(store, screened, *, round_id=None, batch_id=None):
    return build_judged_batch(
        store,
        round_id=round_id or screened["round_id"],
        dossiers=screened["dossiers"],
        seed=SEED,
        batch_id=batch_id,
    )


def _label_everything(batch, *, inventory="new-mechanism", literature="absent"):
    labels = {
        i: {"label_inventory": inventory, "label_corpus": literature}
        for i in batch["scope"]["scope"]
    }
    for plant in batch["plants"]:
        labels[plant["plant_id"]] = {"label_inventory": "same", "label_corpus": "stated"}
    return labels


def _rows_for(store, subject_id):
    return [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT verdict_id, label, round_id, batch_id FROM verdict "
            "WHERE procedure = ? AND procedure_version = ? AND subject_id = ? ORDER BY ts, verdict_id",
            (PROCEDURE, PROCEDURE_VERSION, subject_id),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# a second batch of the same round is a second submission
# ---------------------------------------------------------------------------


def test_a_second_judged_batch_of_the_same_round_is_refused(store, screened):
    """The regression, exactly: two batches, the same scope, both recorded
    with no complaint and two contradicting R3 rows for one idea."""
    first = _batch(store, screened, batch_id="judged-0")
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=first,
        labels=_label_everything(first, inventory="same"),
        issued_by_launch=screened["launches"]["lens-1"],
    )

    second = _batch(store, screened, batch_id="judged-1")
    assert set(second["scope"]["scope"]) & set(first["scope"]["scope"])
    with pytest.raises(NoveltyError) as excinfo:
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=second,
            labels=_label_everything(second, inventory="new-mechanism"),
            issued_by_launch=screened["launches"]["lens-1"],
        )
    message = str(excinfo.value)
    assert "one submission per idea per judge" in message
    # The batch the prior rows came from is named: "already answered in
    # judged-0" has to be readable without a SQL query.
    assert "judged-0" in message

    subject = sorted(first["scope"]["scope"])[0]
    labels = [r["label"] for r in _rows_for(store, subject)]
    assert labels.count("R3:same") == 1


def test_supersede_gets_the_second_batch_through_and_names_the_rows(store, screened):
    first = _batch(store, screened, batch_id="judged-0")
    before = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=first,
        labels=_label_everything(first), issued_by_launch=screened["launches"]["lens-1"],
    )
    second = _batch(store, screened, batch_id="judged-1")
    after = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=second,
        labels=_label_everything(second), issued_by_launch=screened["launches"]["lens-1"],
        supersede=True,
    )
    assert set(after["superseded"]) >= {v["verdict_id"] for v in before["verdicts"]}
    for verdict in after["verdicts"]:
        notes = [i.get("note", "") for i in json.loads(verdict["evidence"])]
        assert any(n.startswith("supersedes ") for n in notes), verdict["label"]


def test_another_round_is_still_invisible(store, screened):
    """What item 9 actually scoped, unchanged: a verdict of a DIFFERENT
    round neither blocks nor is superseded."""
    first = _batch(store, screened, batch_id="judged-0")
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=first,
        labels=_label_everything(first), issued_by_launch=screened["launches"]["lens-1"],
    )
    other = _batch(store, screened, round_id="round-other", batch_id="judged-0")
    recorded = record_novelty_verdicts(
        store, round_id="round-other", batch=other,
        labels=_label_everything(other), issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["n_verdicts"] > 0
    assert recorded["superseded"] == []

    subject = sorted(first["scope"]["scope"])[0]
    rounds = {r["round_id"] for r in _rows_for(store, subject)}
    assert rounds == {screened["round_id"], "round-other"}


def test_the_guard_is_keyed_by_round_alone_for_records(store, screened):
    """The key itself, asserted rather than inferred from behaviour: the
    record recorder looks the guard up with a round and no batch."""
    from trialerror.lens import novelty

    seen: list[dict] = []
    real = novelty._existing_novelty_verdicts

    def _spy(store_, subject_ids, **kwargs):
        seen.append(dict(kwargs))
        return real(store_, subject_ids, **kwargs)

    batch = _batch(store, screened, batch_id="judged-0")
    novelty._existing_novelty_verdicts = _spy
    try:
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch,
            labels=_label_everything(batch), issued_by_launch=screened["launches"]["lens-1"],
        )
    finally:
        novelty._existing_novelty_verdicts = real

    assert seen, "the guard was never consulted"
    assert seen[0]["round_id"] == screened["round_id"]
    assert "batch_id" not in seen[0] or seen[0]["batch_id"] is None


# ---------------------------------------------------------------------------
# fix pass V-9: the launch is not part of the key, and must not become one
# ---------------------------------------------------------------------------


def test_a_second_launch_re_recording_the_same_round_is_still_refused(store, screened):
    """The behaviour the documented-but-unimplemented ``issued_by`` half of
    the key would have changed. Design 5.2(3) is one submission per idea per
    JUDGE; a round that books a second launch has not thereby acquired a
    second judge's answer to record."""
    batch = _batch(store, screened, batch_id="judged-0")
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch,
        labels=_label_everything(batch), issued_by_launch=screened["launches"]["lens-1"],
    )
    with pytest.raises(NoveltyError, match="one submission per idea per judge"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch,
            labels=_label_everything(batch),
            issued_by_launch=screened["launches"]["lens-2"],  # a DIFFERENT launch
        )
