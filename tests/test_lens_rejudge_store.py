"""Lane R0-C — the re-judge is kept in the store, with its judges.

A judged screen may re-judge a share of its subjects with a SECOND judge,
and the recording published Cohen's kappa per reference set off that
judge's sheet and then dropped the sheet. This module pins the other half:
every one of those labels lands in ``verdict_rejudge``, the number is
reproducible from ``verdict`` + ``verdict_rejudge`` alone, a batch recorded
before the table existed can be backfilled and the backfill checks itself,
and no reader of ``verdict`` sees anything new.

Nothing here calls a model, and nothing here touches a live store.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from trialerror.cli import main
from trialerror.lens.novelty import (
    KAPPA_DECIMALS,
    REFERENCE_SETS,
    REJUDGE_ROLE,
    REJUDGE_TABLE,
    NoveltyError,
    build_judged_batch,
    load_label_vocabularies,
    record_novelty_verdicts,
    record_rejudge,
    recorded_vocabularies,
    rejudge_report,
    round_dir,
    run_mechanical_screen,
)
from trialerror.stores.migrate import apply_migrations, latest_version
from trialerror.stores.schema import knowledge as knowledge_schema
from trialerror.stores.store import open_store

from tests._inventory_fixtures import bootstrap_launch
from tests._novelty_fixtures import build_round

SEED = "seed-rejudge"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def screened(store):
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    return {**fixture, "mechanical": mechanical, "dossiers": mechanical["dossiers"]}


#: The fixture round is small, and the screen's own 10% re-judge sample over
#: it is ONE subject -- a kappa over one subject is ``None`` by the
#: recorder's own rule, which is the wrong instrument to prove a kappa
#: reproduces with. Every batch here re-judges most of its scope instead.
SECOND_JUDGE_FRACTION = 0.8


def _batch(store, screened, **kwargs):
    kwargs.setdefault("second_judge_fraction", SECOND_JUDGE_FRACTION)
    return build_judged_batch(
        store, round_id=screened["round_id"], dossiers=screened["dossiers"], seed=SEED, **kwargs
    )


def _label_everything(batch, *, inventory="new-mechanism", literature="absent", plants="same"):
    labels = {i: {"label_inventory": inventory, "label_corpus": literature} for i in batch["scope"]["scope"]}
    for plant in batch["plants"]:
        labels[plant["plant_id"]] = {"label_inventory": plants, "label_corpus": "stated"}
    return labels


def _second_sheet(batch, labels, *, disagree_on=1):
    """The second judge's sheet over the batch's own re-judge sample, with
    ``disagree_on`` of its subjects reading the inventory differently."""
    sheet = {s: dict(labels[s]) for s in batch["second_judge"]}
    for subject in sorted(sheet)[:disagree_on]:
        sheet[subject]["label_inventory"] = "variant"
    return sheet


def _rows(store, *, round_id=None):
    where, params = ("", ())
    if round_id is not None:
        where, params = (" WHERE round_id = ?", (round_id,))
    return [
        dict(r)
        for r in store.knowledge.execute(
            f"SELECT * FROM {REJUDGE_TABLE}{where} ORDER BY subject_id, reference_set", params
        )
    ]


def _n_verdicts(store) -> int:
    return store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"]


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


def test_the_migration_adds_the_table_and_its_indexes(tmp_path):
    """v13 applied on its own, on a file that stops at v12 -- the shape the
    live store is in before the orchestrator migrates it."""
    path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        apply_migrations(conn, knowledge_schema.MIGRATIONS[:12])
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (REJUDGE_TABLE,)
        ).fetchall()

        apply_migrations(conn, knowledge_schema.MIGRATIONS)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version(
            knowledge_schema.MIGRATIONS
        )
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({REJUDGE_TABLE})")}
        assert columns == {
            "rejudge_id", "round_id", "batch_id", "subject_kind", "subject_id", "reference_set",
            "label", "label_canonical", "first_label", "agrees", "judge_role", "judge_launches",
            "recorded_by_launch", "prereg_id", "ts",
        }
        indexes = {r["name"] for r in conn.execute(f"PRAGMA index_list({REJUDGE_TABLE})")}
        assert {"idx_verdict_rejudge_batch", "idx_verdict_rejudge_subject"} <= indexes
    finally:
        conn.close()


def test_the_unique_key_is_one_second_opinion_per_subject_and_set(store, screened):
    """The table's own key, proved at the SQL level: the guard in the
    recorder is a better error, not the only lock."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
        second_judge_labels=_second_sheet(batch, labels),
    )
    row = _rows(store)[0]
    with pytest.raises(sqlite3.IntegrityError):
        with store.knowledge:
            store.knowledge.execute(
                f"INSERT INTO {REJUDGE_TABLE} (rejudge_id, round_id, batch_id, subject_kind, subject_id, "
                "reference_set, label, judge_role, recorded_by_launch, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "RJDG-duplicate", row["round_id"], row["batch_id"], row["subject_kind"],
                    row["subject_id"], row["reference_set"], row["label"], row["judge_role"],
                    row["recorded_by_launch"], row["ts"],
                ),
            )


# ---------------------------------------------------------------------------
# the recording writes it
# ---------------------------------------------------------------------------


def test_the_second_judges_labels_land_as_rows_with_the_first_label_beside_them(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    second = _second_sheet(batch, labels)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
    )
    rows = _rows(store)
    assert rows, "a second judge's sheet must leave rows behind"
    assert {r["subject_id"] for r in rows} == set(second)
    assert {r["judge_role"] for r in rows} == {REJUDGE_ROLE}
    assert {r["round_id"] for r in rows} == {screened["round_id"]}
    assert {r["batch_id"] for r in rows} == {batch["batch_id"]}
    assert {r["recorded_by_launch"] for r in rows} == {screened["launches"]["lens-1"]}
    for row in rows:
        key = REFERENCE_SETS[row["reference_set"]]
        assert row["label"] == second[row["subject_id"]][key]
        assert row["first_label"] == labels[row["subject_id"]][key]
        assert row["agrees"] == (1 if row["label"] == row["first_label"] else 0)
    assert recorded["rejudge"]["n_rows"] == len(rows)
    assert recorded["rejudge"]["n_subjects"] == len(second)


def test_the_disagreements_are_reported_raw_and_in_a_stable_order(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    second = _second_sheet(batch, labels, disagree_on=1)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
    )
    disagreements = recorded["rejudge"]["disagreements"]
    assert disagreements == [
        {
            "subject_id": sorted(second)[0], "reference_set": "R3",
            "first": "new-mechanism", "second": "variant",
        }
    ]
    assert disagreements == sorted(
        disagreements, key=lambda d: (d["subject_id"], d["reference_set"])
    )


def test_a_re_judged_plant_gets_its_rows_like_any_other_subject(store, screened):
    """A plant is a subject of the re-judge too, and its rows say so -- the
    kind is on the row so a reader counting second opinions on the round's
    OWN records need not know the plants file."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    plant_id = batch["plants"][0]["plant_id"]
    second = {**_second_sheet(batch, labels), plant_id: {"label_inventory": "variant"}}
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
    )
    kinds = {r["subject_id"]: r["subject_kind"] for r in _rows(store)}
    assert kinds[plant_id] == "plant"
    assert set(kinds.values()) == {"plant", "record"}


def test_the_recording_writes_no_first_judge_row(store, screened):
    """The primary labels are the ``verdict`` rows and stay the only copy of
    themselves."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
        second_judge_labels=_second_sheet(batch, labels),
    )
    assert {r["judge_role"] for r in _rows(store)} == {REJUDGE_ROLE}
    assert len(_rows(store)) < _n_verdicts(store)


def test_a_recording_with_no_second_judge_writes_no_rejudge_row(store, screened):
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert _rows(store) == []
    assert recorded["rejudge"] == {"n_subjects": 0, "n_rows": 0, "disagreements": []}


# ---------------------------------------------------------------------------
# who judged
# ---------------------------------------------------------------------------


def test_the_two_launch_lists_ride_where_the_recording_already_keeps_them(store, screened, tmp_path):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    first_judge = bootstrap_launch(store, purpose="judge-a")
    second_judge = bootstrap_launch(store, purpose="judge-b")
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
        second_judge_labels=_second_sheet(batch, labels),
        judge_launches=[first_judge],
        second_judge_launches=[second_judge],
    )
    assert recorded["judges"] == {"first": [first_judge], "second": [second_judge]}
    assert {r["judge_launches"] for r in _rows(store)} == {json.dumps([second_judge])}

    base = round_dir(store.program_root, screened["round_id"])
    on_disk = json.loads(
        (base / "judged" / f"{batch['batch_id']}-verdicts.json").read_text(encoding="utf-8")
    )
    assert on_disk["judges"] == {"first": [first_judge], "second": [second_judge]}
    assert on_disk["rejudge"]["n_rows"] == len(_rows(store))


def test_an_unknown_second_judge_launch_is_refused_before_any_write(store, screened):
    """PROBE (e). The refusal lands before the first verdict row, not after
    half of them."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    before = _n_verdicts(store)
    with pytest.raises(NoveltyError, match="platform.launch"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=labels,
            issued_by_launch=screened["launches"]["lens-1"],
            second_judge_labels=_second_sheet(batch, labels),
            second_judge_launches=["LNCH-does-not-exist"],
        )
    assert _n_verdicts(store) == before
    assert _rows(store) == []


def test_an_unknown_primary_judge_launch_is_refused_the_same_way(store, screened):
    batch = _batch(store, screened)
    before = _n_verdicts(store)
    with pytest.raises(NoveltyError, match="judge_launches"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
            issued_by_launch=screened["launches"]["lens-1"],
            judge_launches=["LNCH-does-not-exist"],
        )
    assert _n_verdicts(store) == before


def test_second_judge_launches_without_a_second_judge_sheet_is_refused(store, screened):
    batch = _batch(store, screened)
    judge = bootstrap_launch(store, purpose="judge-b")
    with pytest.raises(NoveltyError, match="nothing for those launches"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
            issued_by_launch=screened["launches"]["lens-1"], second_judge_launches=[judge],
        )


# ---------------------------------------------------------------------------
# the read side reproduces the recorded number
# ---------------------------------------------------------------------------


def test_the_report_reproduces_the_recordings_kappa_n_and_observed_agreement(store, screened):
    """PROBE (a), per reference set and from the two tables only."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
        second_judge_labels=_second_sheet(batch, labels),
    )
    report = rejudge_report(store, round_id=screened["round_id"], batch_id=batch["batch_id"])
    sets = report["batches"][0]["sets"]
    assert set(sets) == {"R3", "R4"}
    for reference_set, block in sets.items():
        was = recorded["kappa"][REFERENCE_SETS[reference_set]]
        assert block["n"] == was["n"]
        assert block["kappa"] == was["kappa"]
        assert block["observed_agreement"] == was["observed_agreement"]
        assert block["expected_agreement"] == was["expected_agreement"]
    assert report["pooled"]["sets"]["R3"]["kappa"] == recorded["kappa"]["label_inventory"]["kappa"]
    assert report["no_verdict_row"] == []
    assert report["first_label_mismatch"] == []


def test_the_report_carries_the_raw_disagreements_and_the_judging_launches(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    judge = bootstrap_launch(store, purpose="judge-b")
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
        second_judge_labels=_second_sheet(batch, labels), second_judge_launches=[judge],
    )
    report = rejudge_report(store, round_id=screened["round_id"])
    assert report["pooled"]["judge_launches"] == [judge]
    assert report["batches"][0]["judge_launches"] == [judge]
    assert report["pooled"]["sets"]["R3"]["disagreements"] == [
        {
            "subject_id": sorted(batch["second_judge"])[0], "reference_set": "R3",
            "first": "new-mechanism", "second": "variant",
        }
    ]


def test_a_plants_second_opinion_is_still_in_the_kappa_and_is_reported_as_having_no_verdict_row(
    store, screened
):
    """The one place ``first_label`` earns its column: a plant carries no
    ``verdict`` row, so a report reading ``verdict`` alone could not
    reproduce a kappa taken over a sheet that included one."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    plant_id = batch["plants"][0]["plant_id"]
    second = {**_second_sheet(batch, labels), plant_id: {"label_inventory": "variant"}}
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
    )
    report = rejudge_report(store, round_id=screened["round_id"])
    assert report["pooled"]["sets"]["R3"]["n"] == recorded["kappa"]["label_inventory"]["n"]
    assert report["pooled"]["sets"]["R3"]["kappa"] == recorded["kappa"]["label_inventory"]["kappa"]
    assert [entry["subject_id"] for entry in report["no_verdict_row"]] == [plant_id]


def test_a_round_with_no_re_judge_reports_zero_rather_than_raising(store, screened):
    batch = _batch(store, screened)
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    report = rejudge_report(store, round_id=screened["round_id"])
    assert report["n_rows"] == 0
    assert report["batches"] == []
    assert report["pooled"]["sets"] == {}


# ---------------------------------------------------------------------------
# supersede
# ---------------------------------------------------------------------------


def test_re_recording_with_supersede_replaces_the_rows_rather_than_doubling_them(store, screened):
    """PROBE (d)."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    second = _second_sheet(batch, labels)
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
    )
    first_ids = {r["rejudge_id"] for r in _rows(store)}
    n_before = len(first_ids)

    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
        supersede=True,
    )
    after = _rows(store)
    assert len(after) == n_before
    assert not ({r["rejudge_id"] for r in after} & first_ids), "the rows were replaced, not kept"


def test_a_second_recording_without_supersede_is_refused_and_writes_nothing(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    second = _second_sheet(batch, labels)
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
    )
    n_rejudge, n_verdict = len(_rows(store)), _n_verdicts(store)
    with pytest.raises(NoveltyError, match="judge row"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=labels,
            issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
        )
    assert len(_rows(store)) == n_rejudge
    assert _n_verdicts(store) == n_verdict


# ---------------------------------------------------------------------------
# no reader of `verdict` changes
# ---------------------------------------------------------------------------


def test_the_verdict_rows_a_recording_writes_do_not_depend_on_the_second_judge(store, screened, tmp_path):
    """PROBE (c), at the row level: the same batch recorded with and without
    a second judge writes the same verdict rows, and the same ideas
    consolidate."""

    def _record(program_root, *, with_second):
        s = open_store(program_root, platform_root=tmp_path / "platform_root")
        fixture = build_round(s)
        mechanical = run_mechanical_screen(
            s, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
        )
        batch = build_judged_batch(
            s, round_id=fixture["round_id"], dossiers=mechanical["dossiers"], seed=SEED,
            second_judge_fraction=SECOND_JUDGE_FRACTION,
        )
        labels = _label_everything(batch)
        recorded = record_novelty_verdicts(
            s, round_id=fixture["round_id"], batch=batch, labels=labels,
            issued_by_launch=fixture["launches"]["lens-1"],
            second_judge_labels=_second_sheet(batch, labels) if with_second else None,
        )
        # The ids are freshly minted per program root, so the comparison is
        # over the SHAPE the verdict rows have: how many, which labels, how
        # many consolidated, and the batch's own status.
        shape = (
            sorted((v["label"], v["label_canonical"]) for v in recorded["verdicts"]),
            recorded["n_verdicts"],
            recorded["n_consolidated"],
            recorded["n_consolidated_unjudged"],
            recorded["status"],
            recorded["caveats"],
        )
        s.close()
        return shape

    plain = tmp_path / "plain"
    plain.mkdir()
    judged = tmp_path / "judged"
    judged.mkdir()
    assert _record(plain, with_second=False) == _record(judged, with_second=True)


# ---------------------------------------------------------------------------
# the backfill, and its self-verification
# ---------------------------------------------------------------------------


def _recorded_then_wiped(store, screened, **kwargs):
    """A batch recorded WITH a second judge, whose re-judge rows are then
    deleted -- byte for byte the state every batch recorded before
    knowledge-v13 is in: a published kappa, no rows behind it."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    second = _second_sheet(batch, labels)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second, **kwargs
    )
    with store.knowledge:
        store.knowledge.execute(f"DELETE FROM {REJUDGE_TABLE}")
    assert _rows(store) == []
    return batch, labels, second, recorded


def test_the_backfill_writes_the_rows_and_reproduces_the_published_number(store, screened):
    batch, _labels, second, recorded = _recorded_then_wiped(store, screened)
    judge = bootstrap_launch(store, purpose="judge-b")
    n_verdict = _n_verdicts(store)

    out = record_rejudge(
        store,
        round_id=screened["round_id"],
        batch_id=batch["batch_id"],
        batch=batch,
        second_judge_labels=second,
        recorded_by_launch=screened["launches"]["lens-1"],
        second_judge_launches=[judge],
    )
    rows = _rows(store)
    assert rows and out["rejudge"]["n_rows"] == len(rows)
    assert out["judge_launches"] == [judge]
    # It wrote ONLY re-judge rows: no verdict row appeared or moved.
    assert _n_verdicts(store) == n_verdict
    for reference_set in ("R3", "R4"):
        block = out["report"]["pooled"]["sets"][reference_set]
        was = recorded["kappa"][REFERENCE_SETS[reference_set]]
        assert (block["kappa"], block["n"], block["observed_agreement"]) == (
            was["kappa"], was["n"], was["observed_agreement"]
        )


def test_the_backfill_refuses_a_corrupted_sheet_with_both_numbers_and_writes_nothing(store, screened):
    """PROBE (b). One label changed in a copy of the second-judge file; the
    backfill rolls back and names both kappas."""
    batch, _labels, second, recorded = _recorded_then_wiped(store, screened)
    corrupted = {s: dict(v) for s, v in second.items()}
    subject = sorted(corrupted)[-1]
    corrupted[subject]["label_inventory"] = (
        "recombination" if corrupted[subject]["label_inventory"] != "recombination" else "same"
    )
    n_verdict = _n_verdicts(store)

    with pytest.raises(NoveltyError) as excinfo:
        record_rejudge(
            store,
            round_id=screened["round_id"],
            batch_id=batch["batch_id"],
            batch=batch,
            second_judge_labels=corrupted,
            recorded_by_launch=screened["launches"]["lens-1"],
        )
    message = str(excinfo.value)
    assert "R3" in message and "recorded" in message and "recomputed" in message
    # BOTH numbers, side by side: the published one and the one this sheet
    # would have produced, so the operator can see which is which.
    was = recorded["kappa"]["label_inventory"]
    assert any(
        f"recorded {was[statistic]!r}" in message for statistic in ("kappa", "observed_agreement")
    ), message
    assert _rows(store) == [], "a refused backfill leaves no row"
    assert _n_verdicts(store) == n_verdict


def test_the_self_check_compares_at_the_precision_the_recorder_writes():
    """The comparison is exact at the six decimals :func:`cohens_kappa`
    rounds to -- not at the RECORDED value's own printed decimals, which
    looks like the same rule and is far looser: a stored ``1.0`` prints one
    decimal, and one decimal would call a recomputed 0.96 the same number."""
    from trialerror.lens.novelty import KAPPA_DECIMALS, _matches_recorded

    assert KAPPA_DECIMALS == 6
    assert _matches_recorded(1.0, 1.0)
    assert not _matches_recorded(0.96, 1.0)
    assert not _matches_recorded(0.04, 0.0)
    assert _matches_recorded(0.958333, 0.958333)
    assert not _matches_recorded(0.958334, 0.958333)
    # n is a count, and None is its own finding
    assert _matches_recorded(4, 4) and not _matches_recorded(3, 4)
    assert _matches_recorded(None, None)
    assert not _matches_recorded(0.0, None) and not _matches_recorded(None, 0.0)


def test_the_backfill_refuses_a_batch_with_no_recording_on_file(store, screened):
    batch = _batch(store, screened)
    with pytest.raises(NoveltyError, match="no recording on file"):
        record_rejudge(
            store,
            round_id=screened["round_id"],
            batch_id=batch["batch_id"],
            batch=batch,
            second_judge_labels={},
            recorded_by_launch=screened["launches"]["lens-1"],
        )


def test_the_backfill_refuses_rows_already_on_file_unless_superseded(store, screened):
    batch, _labels, second, _recorded = _recorded_then_wiped(store, screened)
    kwargs = dict(
        round_id=screened["round_id"], batch_id=batch["batch_id"], batch=batch,
        second_judge_labels=second, recorded_by_launch=screened["launches"]["lens-1"],
    )
    record_rejudge(store, **kwargs)
    n_rows = len(_rows(store))
    with pytest.raises(NoveltyError, match="already carries"):
        record_rejudge(store, **kwargs)
    record_rejudge(store, supersede=True, **kwargs)
    assert len(_rows(store)) == n_rows


def test_a_batch_recorded_with_no_second_judge_has_no_number_to_verify_against(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    with pytest.raises(NoveltyError, match="reports no kappa"):
        record_rejudge(
            store,
            round_id=screened["round_id"],
            batch_id=batch["batch_id"],
            batch=batch,
            second_judge_labels=_second_sheet(batch, labels),
            recorded_by_launch=screened["launches"]["lens-1"],
        )


def test_the_backfill_unmasks_the_sheet_through_the_batchs_own_mask(store, screened):
    """A sheet keyed by the ids the judge was actually shown backfills
    identically to one keyed by the real ids -- the mask is a barrier, not a
    trap, on this verb too."""
    batch, _labels, second, _recorded = _recorded_then_wiped(store, screened)
    to_masked = {real: masked for masked, real in (batch.get("mask") or {}).items()}
    masked_sheet = {to_masked[s]: v for s, v in second.items()}
    assert masked_sheet and set(masked_sheet) != set(second)

    record_rejudge(
        store, round_id=screened["round_id"], batch_id=batch["batch_id"], batch=batch,
        second_judge_labels=masked_sheet, recorded_by_launch=screened["launches"]["lens-1"],
    )
    assert {r["subject_id"] for r in _rows(store)} == set(second)


def test_an_unknown_launch_refuses_the_backfill_before_it_writes(store, screened):
    batch, _labels, second, _recorded = _recorded_then_wiped(store, screened)
    with pytest.raises(NoveltyError, match="platform.launch"):
        record_rejudge(
            store, round_id=screened["round_id"], batch_id=batch["batch_id"], batch=batch,
            second_judge_labels=second, recorded_by_launch=screened["launches"]["lens-1"],
            second_judge_launches=["LNCH-nope"],
        )
    assert _rows(store) == []


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(tmp_path / "platform_root"))
    root = tmp_path / "program"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run(capsys, argv):
    code = main(argv)
    envelope = json.loads(capsys.readouterr().out.strip())
    envelope["_exit_code"] = code
    return envelope


def _screen(root, round_id, *rest):
    return ["lens", "--program-root", str(root), "screen", "--round-id", round_id, *rest]


@pytest.fixture()
def cli_recorded(cli_root, tmp_path, capsys):
    """A round screened, prepped and RECORDED with a second judge, through
    the CLI, with the two sheets left on disk."""
    store = open_store(cli_root, platform_root=tmp_path / "platform_root")
    fixture = build_round(store)
    judge_launch = bootstrap_launch(store, purpose="judge-b")
    store.close()

    _run(capsys, _screen(cli_root, fixture["round_id"], "--mechanical",
                         "--launch-id", fixture["launches"]["lens-1"]))
    prep = _run(capsys, _screen(
        cli_root, fixture["round_id"], "--judged-prep", "--seed", SEED,
        "--second-judge-fraction", str(SECOND_JUDGE_FRACTION),
    ))
    batch = json.loads(
        (round_dir(cli_root, fixture["round_id"]) / "judged" / "judged-0.json").read_text(encoding="utf-8")
    )
    assert prep["ok"] is True
    labels = _label_everything(batch)
    second = _second_sheet(batch, labels)
    first_file = tmp_path / "first.json"
    second_file = tmp_path / "second.json"
    first_file.write_text(json.dumps(labels), encoding="utf-8")
    second_file.write_text(json.dumps(second), encoding="utf-8")
    return {
        "round_id": fixture["round_id"], "batch": batch, "labels": labels, "second": second,
        "first_file": first_file, "second_file": second_file,
        "launch": fixture["launches"]["lens-1"], "judge_launch": judge_launch,
        "platform_root": tmp_path / "platform_root",
    }


def test_the_cli_records_the_re_judge_and_reads_it_back(cli_root, cli_recorded, capsys):
    env = _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-verdicts", str(cli_recorded["first_file"]),
        "--second-judge-file", str(cli_recorded["second_file"]),
        "--launch-id", cli_recorded["launch"],
        "--judge-launch", cli_recorded["launch"],
        "--second-judge-launch", cli_recorded["judge_launch"],
    ))
    assert env["ok"] is True, env
    recorded = env["result"]["record_verdicts"]
    assert recorded["judges"] == {
        "first": [cli_recorded["launch"]], "second": [cli_recorded["judge_launch"]]
    }
    assert recorded["rejudge"]["n_rows"] > 0

    report = _run(capsys, _screen(cli_root, cli_recorded["round_id"], "--rejudge-report"))
    assert report["ok"] is True
    block = report["result"]["rejudge_report"]
    assert block["n_rows"] == recorded["rejudge"]["n_rows"]
    assert block["pooled"]["judge_launches"] == [cli_recorded["judge_launch"]]
    assert block["pooled"]["sets"]["R3"]["kappa"] == recorded["kappa"]["label_inventory"]["kappa"]


def test_the_cli_backfill_verifies_itself_and_refuses_a_corrupted_sheet(
    cli_root, cli_recorded, capsys, tmp_path
):
    _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-verdicts", str(cli_recorded["first_file"]),
        "--second-judge-file", str(cli_recorded["second_file"]), "--launch-id", cli_recorded["launch"],
    ))
    store = open_store(cli_root, platform_root=cli_recorded["platform_root"])
    with store.knowledge:
        store.knowledge.execute(f"DELETE FROM {REJUDGE_TABLE}")
    n_verdict = _n_verdicts(store)
    store.close()

    corrupted = {s: dict(v) for s, v in cli_recorded["second"].items()}
    corrupted[sorted(corrupted)[-1]]["label_inventory"] = "recombination"
    corrupt_file = tmp_path / "second-corrupt.json"
    corrupt_file.write_text(json.dumps(corrupted), encoding="utf-8")

    refused = _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-rejudge", "--batch-id", "judged-0",
        "--second-judge-file", str(corrupt_file), "--launch-id", cli_recorded["launch"],
    ))
    assert refused["ok"] is False
    assert "recomputed" in refused["error"]["message"]

    store = open_store(cli_root, platform_root=cli_recorded["platform_root"])
    assert _rows(store) == []
    assert _n_verdicts(store) == n_verdict
    store.close()

    ok = _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-rejudge", "--batch-id", "judged-0",
        "--second-judge-file", str(cli_recorded["second_file"]), "--launch-id", cli_recorded["launch"],
        "--second-judge-launch", cli_recorded["judge_launch"],
    ))
    assert ok["ok"] is True, ok
    assert ok["result"]["record_rejudge"]["rejudge"]["n_rows"] > 0

    store = open_store(cli_root, platform_root=cli_recorded["platform_root"])
    assert _n_verdicts(store) == n_verdict
    store.close()


def test_the_backfill_needs_a_sheet_a_batch_id_and_a_launch(cli_root, cli_recorded, capsys):
    env = _run(capsys, _screen(cli_root, cli_recorded["round_id"], "--record-rejudge"))
    assert env["error"]["code"] == "second_judge_file_required"

    env = _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-rejudge",
        "--second-judge-file", str(cli_recorded["second_file"]),
    ))
    assert env["error"]["code"] == "batch_id_required"

    env = _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-rejudge", "--batch-id", "judged-0",
        "--second-judge-file", str(cli_recorded["second_file"]),
    ))
    assert env["error"]["code"] == "launch_id_required"


def test_the_no_phase_refusal_names_the_two_new_verbs(cli_root, capsys):
    env = _run(capsys, _screen(cli_root, "round-1"))
    assert env["error"]["code"] == "no_phase"
    assert "--rejudge-report" in env["error"]["message"]
    assert "--record-rejudge" in env["error"]["message"]


# ---------------------------------------------------------------------------
# the doctor check
# ---------------------------------------------------------------------------


def _check(program_root):
    from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    return {r.name: r for r in run_checks(ctx, only=["rejudge_rows_match_recorded_kappa"])}[
        "rejudge_rows_match_recorded_kappa"
    ]


def test_the_doctor_passes_a_backfilled_batch_and_warns_about_one_that_is_not(
    cli_root, cli_recorded, capsys
):
    _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-verdicts", str(cli_recorded["first_file"]),
        "--second-judge-file", str(cli_recorded["second_file"]), "--launch-id", cli_recorded["launch"],
    ))
    passing = _check(cli_root)
    assert passing.status == "pass", passing.message
    assert passing.details["batches_checked"] == 1

    store = open_store(cli_root, platform_root=cli_recorded["platform_root"])
    with store.knowledge:
        store.knowledge.execute(f"DELETE FROM {REJUDGE_TABLE}")
    store.close()

    warned = _check(cli_root)
    assert warned.status == "warn"
    assert "--record-rejudge" in warned.message
    assert warned.details["offenders"][0]["batch_id"] == "judged-0"


def test_the_doctor_skips_a_program_whose_batches_published_no_kappa(cli_root, cli_recorded, capsys):
    _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-verdicts", str(cli_recorded["first_file"]),
        "--launch-id", cli_recorded["launch"],
    ))
    result = _check(cli_root)
    assert result.status == "skip"
    assert "re-judged" in result.message


def test_the_doctor_skips_a_program_with_no_round_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(tmp_path / "platform_root"))
    root = tmp_path / "empty-program"
    root.mkdir()
    open_store(root, platform_root=tmp_path / "platform_root").close()
    assert _check(root).status == "skip"


# ---------------------------------------------------------------------------
# lane FB-acq item 7: the report resolves each batch's OWN label vocabulary
# ---------------------------------------------------------------------------
#
# A kappa's chance term is computed over the categories a judge COULD have
# used. Those are the ROUND's whenever it declared a labels file, and the
# report used to take them from its CALLER -- the design's default unless
# someone remembered --labels-file. So a round that re-spelled its labels got a
# report whose kappa did not match the one its own recording published, with
# the same n and the same observed agreement printed beside it: only the chance
# term had moved, which is the hardest kind of discrepancy to notice.
#
# ``cohens_kappa`` uses ``categories`` in exactly one place -- the expected
# agreement, one product of the two judges' marginal shares per category -- so
# a label a judge actually returned that the given categories do not list
# contributes nothing and drops out of the marginals entirely.

#: A round vocabulary whose words are NONE of the design's, so that computing
#: its kappa under the default categories drops every marginal and the chance
#: term collapses to 0. That is what makes the fixture prove something.
ROUND_VOCABULARY = {
    "R3": {
        "labels": ["identical", "tweak", "mashup", "fresh", "nothing-to-compare"],
        "canonical": {
            "identical": "same", "tweak": "variant", "mashup": "recombination",
            "fresh": "new-mechanism", "nothing-to-compare": "unscreenable",
        },
    },
    "R4": {
        "labels": ["said", "hinted", "nearby", "missing"],
        "canonical": {"said": "stated", "hinted": "implied", "nearby": "adjacent", "missing": "absent"},
    },
    "unscreenable": "nothing-to-compare",
}

#: A SECOND, differently-spelled vocabulary -- two batches of one round judged
#: under these two have no common chance term, so they have no pooled kappa.
OTHER_VOCABULARY = {
    "R3": {
        "labels": ["duplicate", "twist", "blend", "novel", "uncomparable"],
        "canonical": {
            "duplicate": "same", "twist": "variant", "blend": "recombination",
            "novel": "new-mechanism", "uncomparable": "unscreenable",
        },
    },
    "R4": {
        "labels": ["explicit", "suggested", "close", "unseen"],
        "canonical": {
            "explicit": "stated", "suggested": "implied", "close": "adjacent", "unseen": "absent",
        },
    },
    "unscreenable": "uncomparable",
}


def _vocab(declared):
    return load_label_vocabularies(declared, judged_sets=("R3", "R4"))


def _round_labels(batch, words):
    """Labels in the ROUND's own spellings, varied across subjects so the
    chance term is not degenerate (a fixture whose expected agreement is 1
    would prove nothing about a vocabulary)."""
    inventory, alternative = words["inventory"]
    literature, literature_alt = words["literature"]
    labels = {}
    for n, subject in enumerate(sorted(batch["scope"]["scope"])):
        labels[subject] = {
            "label_inventory": inventory if n % 2 == 0 else alternative,
            "label_corpus": literature if n % 3 else literature_alt,
        }
    for plant in batch["plants"]:
        labels[plant["plant_id"]] = {"label_inventory": words["plant"], "label_corpus": literature}
    return labels


ROUND_WORDS = {
    "inventory": ("fresh", "mashup"), "literature": ("missing", "nearby"), "plant": "identical",
    "disagree_as": "tweak",
}
OTHER_WORDS = {
    "inventory": ("novel", "blend"), "literature": ("unseen", "close"), "plant": "duplicate",
    "disagree_as": "twist",
}


def _round_second_sheet(batch, labels, words, *, disagree_on=1):
    sheet = {s: dict(labels[s]) for s in batch["second_judge"]}
    for subject in sorted(sheet)[:disagree_on]:
        sheet[subject]["label_inventory"] = words["disagree_as"]
    return sheet


def _record_under(
    store, screened, *, declared, words, batch_id=None, out_dir=None, dossiers=None,
    sample_fraction=None,
):
    """One batch of the round, judged and recorded under ``declared``."""
    vocabularies = _vocab(declared)
    extra = {} if sample_fraction is None else {"sample_fraction": sample_fraction}
    batch = build_judged_batch(
        store, round_id=screened["round_id"],
        dossiers=screened["dossiers"] if dossiers is None else dossiers, seed=SEED,
        second_judge_fraction=SECOND_JUDGE_FRACTION, label_vocabularies=vocabularies,
        batch_id=batch_id, out_dir=out_dir, **extra,
    )
    labels = _round_labels(batch, words)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
        second_judge_labels=_round_second_sheet(batch, labels, words),
        label_vocabularies=vocabularies, out_dir=out_dir,
    )
    return {"batch": batch, "recorded": recorded, "vocabularies": vocabularies, "labels": labels}


def _assert_matches_recording(block, recorded, reference_set):
    was = recorded["kappa"][REFERENCE_SETS[reference_set]]
    for field in ("kappa", "n", "observed_agreement"):
        assert round(float(block[field]), KAPPA_DECIMALS) == round(
            float(was[field]), KAPPA_DECIMALS
        ), (reference_set, field, block[field], was[field])


def test_the_report_resolves_each_batchs_vocabulary_from_its_own_recording(store, screened):
    """PROBE: the recorded kappa, the report's kappa under the DEFAULT
    vocabulary, and the report's kappa after resolving the recording's."""
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    base = round_dir(store.program_root, screened["round_id"])

    # The fixture has to be one where the vocabulary MOVES the number, or
    # nothing below is evidence. Explicitly passing the design's own default is
    # what the report used to do when nobody passed --labels-file.
    default = rejudge_report(
        store, round_id=screened["round_id"], batch_id=under["batch"]["batch_id"],
        label_vocabularies=_vocab({}), out_dir=base,
    )["batches"][0]["sets"]["R3"]
    recorded_r3 = under["recorded"]["kappa"][REFERENCE_SETS["R3"]]
    assert default["kappa"] != recorded_r3["kappa"]
    # ... and it moves ONLY the chance term: same subjects, same agreement.
    assert default["n"] == recorded_r3["n"]
    assert default["observed_agreement"] == recorded_r3["observed_agreement"]

    report = rejudge_report(
        store, round_id=screened["round_id"], batch_id=under["batch"]["batch_id"], out_dir=base
    )
    block = report["batches"][0]
    assert block["vocabulary_source"] == "recording"
    assert block["labels_sha256"] == under["vocabularies"]["sha256"]
    assert "vocabulary_warning" not in block
    assert report["warnings"] == []
    for reference_set in ("R3", "R4"):
        _assert_matches_recording(block["sets"][reference_set], under["recorded"], reference_set)
    assert report["pooled"]["vocabulary_source"] == "recording"


def test_the_default_out_dir_is_the_rounds_own_directory(store, screened):
    """The recording wrote its file there, so a report with no ``out_dir`` at
    all resolves the same vocabulary."""
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    report = rejudge_report(store, round_id=screened["round_id"])
    assert report["batches"][0]["vocabulary_source"] == "recording"
    _assert_matches_recording(report["batches"][0]["sets"]["R3"], under["recorded"], "R3")


def test_with_the_recording_gone_the_batch_file_answers(store, screened):
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    base = round_dir(store.program_root, screened["round_id"])
    (base / "judged" / f"{under['batch']['batch_id']}-verdicts.json").unlink()

    report = rejudge_report(store, round_id=screened["round_id"], out_dir=base)
    block = report["batches"][0]
    assert block["vocabulary_source"] == "batch_file"
    assert block["labels_sha256"] == under["vocabularies"]["sha256"]
    assert report["warnings"] == []
    for reference_set in ("R3", "R4"):
        _assert_matches_recording(block["sets"][reference_set], under["recorded"], reference_set)


def test_with_neither_file_the_default_is_used_and_said_so_three_times_over(store, screened):
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    base = round_dir(store.program_root, screened["round_id"])
    batch_id = under["batch"]["batch_id"]
    (base / "judged" / f"{batch_id}-verdicts.json").unlink()
    (base / "judged" / f"{batch_id}.json").unlink()

    report = rejudge_report(store, round_id=screened["round_id"], out_dir=base)
    block = report["batches"][0]
    assert block["vocabulary_source"] == "default_unresolved"
    assert block["labels_sha256"] is None
    assert batch_id in block["vocabulary_warning"]
    assert "DEFAULT category vocabulary" in block["vocabulary_warning"]
    for set_block in block["sets"].values():
        assert set_block["kappa_vocabulary"] == "default_unresolved"
    assert report["warnings"] == [block["vocabulary_warning"]]
    # and the number really is the default one, not the recorded one
    assert block["sets"]["R3"]["kappa"] != under["recorded"]["kappa"][REFERENCE_SETS["R3"]]["kappa"]


def test_a_malformed_recording_file_does_not_raise_and_falls_through(store, screened):
    """A report is a READ: one truncated file must not take the whole report
    down with it."""
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    base = round_dir(store.program_root, screened["round_id"])
    (base / "judged" / f"{under['batch']['batch_id']}-verdicts.json").write_text(
        "{not json", encoding="utf-8"
    )

    report = rejudge_report(store, round_id=screened["round_id"], out_dir=base)
    assert report["batches"][0]["vocabulary_source"] == "batch_file"
    _assert_matches_recording(report["batches"][0]["sets"]["R3"], under["recorded"], "R3")


def test_a_round_that_declared_no_vocabulary_reports_the_recordings_own_default(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
        second_judge_labels=_second_sheet(batch, labels),
    )
    report = rejudge_report(store, round_id=screened["round_id"])
    block = report["batches"][0]
    assert block["vocabulary_source"] == "recording_default"
    assert block["labels_sha256"] is None
    assert "vocabulary_warning" not in block
    assert report["warnings"] == []
    for reference_set in ("R3", "R4"):
        _assert_matches_recording(block["sets"][reference_set], recorded, reference_set)
        assert "kappa_vocabulary" not in block["sets"][reference_set]


def test_an_explicit_vocabulary_wins_and_a_hash_that_differs_is_reported(store, screened):
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    base = round_dir(store.program_root, screened["round_id"])
    given = _vocab(OTHER_VOCABULARY)

    report = rejudge_report(
        store, round_id=screened["round_id"], label_vocabularies=given, out_dir=base
    )
    block = report["batches"][0]
    assert block["vocabulary_source"] == "argument"
    assert block["labels_sha256"] == given["sha256"]
    assert block["vocabulary_mismatch"] == {
        "given": given["sha256"], "recorded": under["vocabularies"]["sha256"]
    }
    # reported, not refused: the report still computed under what it was given
    assert report["labels_sha256"] == given["sha256"]


def test_an_explicit_vocabulary_that_matches_the_recording_reports_no_mismatch(store, screened):
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    base = round_dir(store.program_root, screened["round_id"])
    report = rejudge_report(
        store, round_id=screened["round_id"], label_vocabularies=under["vocabularies"], out_dir=base
    )
    block = report["batches"][0]
    assert block["vocabulary_source"] == "argument"
    assert "vocabulary_mismatch" not in block
    _assert_matches_recording(block["sets"]["R3"], under["recorded"], "R3")


def test_recorded_vocabularies_reports_where_it_got_the_answer(store, screened, tmp_path):
    under = _record_under(store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS)
    base = round_dir(store.program_root, screened["round_id"])
    batch_id = under["batch"]["batch_id"]

    vocab, source, sha = recorded_vocabularies(base, batch_id)
    assert source == "recording"
    assert sha == under["vocabularies"]["sha256"]
    assert vocab["sha256"] == sha

    (base / "judged" / f"{batch_id}-verdicts.json").unlink()
    assert recorded_vocabularies(base, batch_id)[1] == "batch_file"

    (base / "judged" / f"{batch_id}.json").unlink()
    assert recorded_vocabularies(base, batch_id) == (None, "default_unresolved", None)

    # nothing on disk at all -- not even a judged/ directory
    assert recorded_vocabularies(tmp_path / "nowhere", batch_id)[1] == "default_unresolved"


# ---------------------------------------------------------------------------
# two batches, two vocabularies: a pooled kappa is not defined
# ---------------------------------------------------------------------------


def _halves(screened):
    """Two DISJOINT dossier sets, so two batches of one round can be recorded
    without either one re-submitting the other's ideas (design 5.2(3): one
    submission per idea per judge)."""
    keys = sorted(screened["dossiers"])
    left, right = keys[::2], keys[1::2]
    return (
        {k: screened["dossiers"][k] for k in left},
        {k: screened["dossiers"][k] for k in right},
    )


def test_two_batches_under_different_vocabularies_pool_everything_but_the_kappa(store, screened):
    left, right = _halves(screened)
    first = _record_under(
        store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS, batch_id="judged-a",
        dossiers=left, sample_fraction=1.0,
    )
    second = _record_under(
        store, screened, declared=OTHER_VOCABULARY, words=OTHER_WORDS, batch_id="judged-b",
        dossiers=right, sample_fraction=1.0,
    )
    base = round_dir(store.program_root, screened["round_id"])

    report = rejudge_report(store, round_id=screened["round_id"], out_dir=base)
    blocks = {block["batch_id"]: block for block in report["batches"]}
    assert set(blocks) == {"judged-a", "judged-b"}
    for batch_id, under in (("judged-a", first), ("judged-b", second)):
        assert blocks[batch_id]["vocabulary_source"] == "recording"
        assert blocks[batch_id]["labels_sha256"] == under["vocabularies"]["sha256"]
        for reference_set in ("R3", "R4"):
            _assert_matches_recording(
                blocks[batch_id]["sets"][reference_set], under["recorded"], reference_set
            )

    pooled = report["pooled"]
    assert pooled["vocabulary_source"] == "mixed"
    assert pooled["n_rows"] == blocks["judged-a"]["n_rows"] + blocks["judged-b"]["n_rows"]
    for set_block in pooled["sets"].values():
        assert set_block["kappa"] is None
        assert set_block["expected_agreement"] is None
        # the vocabulary-free half is still real
        assert set_block["n"] >= 2
        assert set_block["observed_agreement"] is not None
    assert pooled["sets"]["R3"]["n"] == (
        blocks["judged-a"]["sets"]["R3"]["n"] + blocks["judged-b"]["sets"]["R3"]["n"]
    )
    assert any("POOLED kappa is not defined" in w for w in report["warnings"])


def test_two_batches_under_ONE_vocabulary_still_pool_a_kappa(store, screened):
    left, right = _halves(screened)
    _record_under(
        store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS, batch_id="judged-a",
        dossiers=left, sample_fraction=1.0,
    )
    _record_under(
        store, screened, declared=ROUND_VOCABULARY, words=ROUND_WORDS, batch_id="judged-b",
        dossiers=right, sample_fraction=1.0,
    )
    base = round_dir(store.program_root, screened["round_id"])

    report = rejudge_report(store, round_id=screened["round_id"], out_dir=base)
    assert report["pooled"]["vocabulary_source"] == "recording"
    assert report["pooled"]["sets"]["R3"]["kappa"] is not None
    assert report["warnings"] == []


# ---------------------------------------------------------------------------
# the CLI carries the warning
# ---------------------------------------------------------------------------


def test_the_cli_envelope_carries_the_unresolved_vocabulary_warning(cli_root, cli_recorded, capsys):
    _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-verdicts", str(cli_recorded["first_file"]),
        "--second-judge-file", str(cli_recorded["second_file"]), "--launch-id", cli_recorded["launch"],
    ))
    judged = round_dir(cli_root, cli_recorded["round_id"]) / "judged"
    (judged / "judged-0-verdicts.json").unlink()
    (judged / "judged-0.json").unlink()

    env = _run(capsys, _screen(cli_root, cli_recorded["round_id"], "--rejudge-report"))
    assert env["ok"] is True, env
    assert env["warnings"], env
    assert "DEFAULT category vocabulary" in env["warnings"][0]["message"]
    assert env["result"]["rejudge_report"]["batches"][0]["vocabulary_source"] == "default_unresolved"


def test_a_resolved_report_adds_no_warnings_block_to_the_envelope(cli_root, cli_recorded, capsys):
    """The envelope key is emitted only when there is something to say -- a
    healthy report is byte-identical to what it was."""
    _run(capsys, _screen(
        cli_root, cli_recorded["round_id"], "--record-verdicts", str(cli_recorded["first_file"]),
        "--second-judge-file", str(cli_recorded["second_file"]), "--launch-id", cli_recorded["launch"],
    ))
    env = _run(capsys, _screen(cli_root, cli_recorded["round_id"], "--rejudge-report"))
    assert env["ok"] is True, env
    assert "warnings" not in env
    assert env["result"]["rejudge_report"]["batches"][0]["vocabulary_source"] == "recording_default"
