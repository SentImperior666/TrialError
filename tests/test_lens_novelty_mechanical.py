"""Phase 3a — the mechanical half of the novelty screen.

The fixture round is three lenses of six records each, with the two shapes
the screen exists to catch planted in it: one record restated verbatim by a
second lens in the same home cell (the merge), and one record that IS an
inventory row (the KNOWN-MECHANIC flag).

The bar these tests hold the module to is the one the design states: every
number here is a distance or a count, and none of them may become the word
"novel".
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest.pipeline import INVENTORY_SOURCE_KIND
from trialerror.lens.ideas import read_idea, write_idea
from trialerror.lens.novelty import (
    KNOWN_MECHANIC_THRESHOLD,
    NEAR_DUPLICATE_THRESHOLD,
    SCREEN_VERSION,
    UNJUDGED_LABEL,
    NoveltyError,
    StaticExternalProvider,
    home_family,
    neutral_abstract,
    reference_snapshot,
    round_dir,
    run_mechanical_screen,
)

from trialerror.retrieve import engine
from trialerror.stores.vecindex import vec_table_name

from tests._novelty_fixtures import DEGENERATE_IDEAS, DEGENERATE_ROUND_ID, build_round


@pytest.fixture()
def round_fixture(store):
    return build_round(store)


def _screen(store, round_fixture, **kwargs):
    return run_mechanical_screen(
        store, round_id=round_fixture["round_id"], launch_id=round_fixture["launches"]["lens-1"], **kwargs
    )


# ---------------------------------------------------------------------------
# near-duplicate merge
# ---------------------------------------------------------------------------


def test_a_restated_record_in_the_same_home_cell_is_merged_into_the_original(store, round_fixture):
    result = _screen(store, round_fixture)
    assert result["n_merged"] == 1
    merged = result["merged"][0]

    folded = read_idea(store, idea_id=merged["idea_id"])
    survivor = read_idea(store, idea_id=merged["merged_into"])
    assert folded["status"] == "merged"
    assert survivor["status"] == "raw"
    # The survivor is the OLDER record: a later re-proposal folds into the
    # original, never the other way round.
    assert survivor["created_ts"] <= folded["created_ts"]


def test_the_folded_record_points_at_its_survivor_and_stays_in_the_archive(store, round_fixture):
    result = _screen(store, round_fixture)
    merged = result["merged"][0]
    folded = read_idea(store, idea_id=merged["idea_id"])
    assert folded["parent_ids"] == [merged["merged_into"]]
    # Not a deletion: the row is still readable and still has its body.
    assert folded["body"]
    assert store.knowledge.execute(
        "SELECT COUNT(*) AS n FROM idea WHERE idea_id = ?", (merged["idea_id"],)
    ).fetchone()["n"] == 1


def test_the_survivors_provenance_gains_the_union_and_keeps_what_it_had(store, round_fixture):
    result = _screen(store, round_fixture)
    merged = result["merged"][0]
    survivor = read_idea(store, idea_id=merged["merged_into"])
    provenance = json.loads(survivor["provenance"])
    assert provenance["merged_from"] == [merged["idea_id"]]
    assert provenance["docs"] == round_fixture["corpus_doc_ids"][:2]
    # every other key the blob carried is untouched -- that blob is also
    # where interim-convention record fields live
    assert provenance["set_id"]


def test_two_near_identical_records_in_DIFFERENT_home_cells_are_not_merged(store, round_fixture):
    """Similarity alone is not identity. The home cell is the author's own
    claim about what the idea is FOR, and two records that make different
    claims are two records however alike the prose is."""
    original = round_fixture["ideas"][0]
    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-2"],
        body=original["body"], home="family-b/row-9", tier="near",
        provenance={"docs": round_fixture["corpus_doc_ids"][:1]}, operation_declared="bridge/formalize",
    )
    result = _screen(store, round_fixture)
    assert result["n_merged"] == 1  # the same-cell duplicate only


def test_the_merge_threshold_is_the_shared_proximity_constant(store):
    from trialerror.verify.independence import DEFAULT_PROXIMITY_THRESHOLD

    assert NEAR_DUPLICATE_THRESHOLD == KNOWN_MECHANIC_THRESHOLD == DEFAULT_PROXIMITY_THRESHOLD == 0.92


# ---------------------------------------------------------------------------
# KNOWN-MECHANIC
# ---------------------------------------------------------------------------


def test_a_record_that_is_an_inventory_row_is_flagged(store, round_fixture):
    result = _screen(store, round_fixture)
    flagged = {i: d for i, d in result["dossiers"].items() if d["known_mechanic"]}
    assert len(flagged) == 1
    dossier = next(iter(flagged.values()))
    assert dossier["known_mechanic"]["similarity"] >= KNOWN_MECHANIC_THRESHOLD
    assert dossier["known_mechanic"]["family"] == "family-a"
    assert dossier["known_mechanic"]["row_id"] in round_fixture["inventory_chunk_ids"]


def test_a_flag_is_not_a_label_and_the_unjudged_ones_are_not_called_new(store, round_fixture):
    """The whole discipline in one assertion: after the mechanical half,
    every record carries the SAME label, and it is not "new-mechanism"."""
    result = _screen(store, round_fixture)
    assert {d["label_inventory"] for d in result["dossiers"].values()} == {UNJUDGED_LABEL}
    assert all(d["judged"] is False for d in result["dossiers"].values())


# ---------------------------------------------------------------------------
# distances and terciles
# ---------------------------------------------------------------------------


def test_every_dossier_carries_the_four_distance_statistics(store, round_fixture):
    result = _screen(store, round_fixture)
    for dossier in result["dossiers"].values():
        distances = dossier["distances"]
        assert set(distances) == {"d_prov", "d_home", "leap", "H_prior", "tercile", "tercile_metric"}
        assert distances["d_prov"] is not None  # every fixture record names its provenance docs
        assert distances["d_home"] is not None  # every fixture home cell has inventory rows
        assert distances["leap"]["family"] != dossier["home_family"]


def test_terciles_are_cut_within_the_round_and_name_their_metric(store, round_fixture):
    result = _screen(store, round_fixture)
    terciles = [d["distances"]["tercile"] for d in result["dossiers"].values()]
    assert set(terciles) == {"near", "moderate", "far"}
    assert {d["distances"]["tercile_metric"] for d in result["dossiers"].values()} == {"d_prov"}
    # a tercile cut is a partition of THIS batch, so the three arms account
    # for all of it
    assert len(terciles) == len(result["dossiers"])


def test_the_tercile_metric_falls_back_and_says_so_when_provenance_is_absent(store, round_fixture):
    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-1"],
        body="A record written with no provenance documents at all.", home="family-b/row-10",
        operation_declared="bridge/formalize",
    )
    result = _screen(store, round_fixture)
    metrics = {d["distances"]["tercile_metric"] for d in result["dossiers"].values()}
    assert metrics == {"centroid"}


def test_home_family_is_the_documented_convention():
    assert home_family("family-a/row-3") == "family-a"
    assert home_family("family-a") == "family-a"
    assert home_family(None) == ""


# ---------------------------------------------------------------------------
# the round-level distribution audit
# ---------------------------------------------------------------------------


def test_declared_operations_are_counted_per_axis_with_entropy(store, round_fixture):
    result = _screen(store, round_fixture)
    declared = result["distribution"]["declared_operations"]
    for axis in ("opportunity", "method"):
        assert 0.0 <= declared[axis]["entropy"] <= 1.0
        assert sum(declared[axis]["counts"].values()) + declared[axis]["undeclared"] == result["distribution"]["n"]
        assert declared[axis]["off_taxonomy"] == {}


def test_an_off_taxonomy_declaration_is_counted_apart_never_dropped(store, round_fixture):
    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-1"],
        body="A record declaring a move nobody put in the taxonomy.", home="family-a/row-11",
        provenance={"docs": round_fixture["corpus_doc_ids"][:1]},
        operation_declared="invented-axis/invented-method",
    )
    result = _screen(store, round_fixture)
    declared = result["distribution"]["declared_operations"]
    assert declared["opportunity"]["off_taxonomy"] == {"invented-axis": 1}
    assert declared["method"]["off_taxonomy"] == {"invented-method": 1}


def test_the_pairwise_similarity_distribution_is_reported_not_reduced(store, round_fixture):
    result = _screen(store, round_fixture)
    pairwise = result["distribution"]["pairwise_similarity"]
    n = result["distribution"]["n"]
    assert pairwise["n_pairs"] == n * (n - 1) // 2
    assert pairwise["median"] <= pairwise["p90"] <= pairwise["max"]


def test_template_mass_reports_the_two_shares_and_their_intersection(store, round_fixture):
    result = _screen(store, round_fixture)
    mass = result["distribution"]["template_mass"]
    assert mass["template_share"] <= min(mass["bridge_share"], mass["synthesis_share"])


# ---------------------------------------------------------------------------
# the collapse flag
# ---------------------------------------------------------------------------


def test_with_no_pre_registered_alarms_the_monitor_is_descriptive_only(store, round_fixture):
    result = _screen(store, round_fixture)
    assert result["collapse"] == {"mode": "descriptive", "flag": False, "reasons": []}


def test_a_degenerate_batch_trips_every_pre_registered_alarm(store):
    """Six records saying the same thing in the same cell under the same
    declared move. The flag is raised against values fixed BEFORE the batch
    ran -- that is what makes it a monitor rather than a post-hoc opinion."""
    degenerate = build_round(store, round_id=DEGENERATE_ROUND_ID, ideas=DEGENERATE_IDEAS)
    alarms = {
        "median_pairwise_cosine_max": 0.7,
        "operation_entropy_min": {"opportunity": 0.3, "method": 0.3},
        "template_mass_max": 0.5,
    }
    result = run_mechanical_screen(
        store, round_id=DEGENERATE_ROUND_ID, launch_id=degenerate["launches"]["lens-1"], alarms=alarms
    )
    collapse = result["collapse"]
    assert collapse["mode"] == "armed"
    assert collapse["flag"] is True
    assert {r["alarm"] for r in collapse["reasons"]} == {
        "median_pairwise_cosine_max", "operation_entropy_min.opportunity",
        "operation_entropy_min.method", "template_mass_max",
    }


def test_the_same_alarms_leave_a_healthy_batch_unflagged(store, round_fixture):
    alarms = {
        "median_pairwise_cosine_max": 0.7,
        "operation_entropy_min": {"opportunity": 0.3, "method": 0.3},
        "template_mass_max": 0.5,
    }
    result = _screen(store, round_fixture, alarms=alarms)
    assert result["collapse"]["mode"] == "armed"
    assert result["collapse"]["flag"] is False


# ---------------------------------------------------------------------------
# incrementality, snapshots, and what lands on disk
# ---------------------------------------------------------------------------


def test_a_second_call_screens_only_what_arrived_since(store, round_fixture):
    """Phase 3a runs after each lens batch posts. The dossier on disk is the
    record of having been screened, so a re-run is a no-op and a new record
    is a batch of one."""
    first = _screen(store, round_fixture)
    assert first["n_screened"] == len(round_fixture["idea_ids"])

    again = _screen(store, round_fixture)
    assert again["n_screened"] == 0

    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-2"],
        body="A record posted in a later batch of the same round.", home="family-b/row-12",
        provenance={"docs": round_fixture["corpus_doc_ids"][:1]}, operation_declared="bridge/robustify",
    )
    third = _screen(store, round_fixture)
    assert third["n_screened"] == 1


def test_rescreen_re_runs_ideas_that_already_have_a_dossier(store, round_fixture):
    """Every record that is still ``raw`` -- which is one fewer than the
    round wrote, because the first pass folded the duplicate into ``merged``
    and a merged record is not re-screened as though it were live."""
    first = _screen(store, round_fixture)
    again = _screen(store, round_fixture, rescreen=True)
    assert again["n_screened"] == len(round_fixture["idea_ids"]) - first["n_merged"]
    assert again["n_screened"] == len(round_fixture["idea_ids"]) - 1


def test_the_reference_snapshot_is_hashed_on_the_round_and_on_every_dossier(store, round_fixture):
    result = _screen(store, round_fixture)
    snapshot = result["reference_snapshot"]
    assert set(snapshot) >= {"R1", "R2", "R3", "R4", "R5"}
    assert snapshot["R3"]["n"] == len(round_fixture["inventory_chunk_ids"])
    assert snapshot["R3"]["sha256"] != snapshot["R4"]["sha256"]
    for dossier in result["dossiers"].values():
        assert dossier["reference_snapshot"]["R3"]["sha256"] == snapshot["R3"]["sha256"]


def test_the_snapshot_moves_when_the_reference_set_does(store, round_fixture):
    before = reference_snapshot(store, round_id=round_fixture["round_id"])
    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-1"],
        body="One more record.", home="family-a/row-13",
    )
    after = reference_snapshot(store, round_id=round_fixture["round_id"])
    assert before["R1"]["sha256"] != after["R1"]["sha256"]
    assert before["R3"]["sha256"] == after["R3"]["sha256"]


def test_a_dossier_per_idea_and_an_adjudication_draft_land_under_the_round(store, round_fixture):
    result = _screen(store, round_fixture)
    base = round_dir(store.program_root, round_fixture["round_id"])
    for idea_id in result["dossiers"]:
        path = base / "novelty" / f"{idea_id}.json"
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["screen_version"] == SCREEN_VERSION
    draft = (base / "adjudication.md").read_text(encoding="utf-8")
    assert "Adjudication draft" in draft
    assert "never thresholded into" in draft
    assert (base / "batches" / f"{result['batch_id']}.json").is_file()


# ---------------------------------------------------------------------------
# R5, the external-query seam
# ---------------------------------------------------------------------------


def test_no_external_query_is_issued_under_mode_none(store, round_fixture):
    provider = StaticExternalProvider(default=[{"id": "X-1", "title": "Prior work", "score": 0.9}])
    result = _screen(store, round_fixture, external=provider, external_query_mode="none")
    assert provider.calls == []
    assert all(d["candidate_hits"]["R5"] == [] for d in result["dossiers"].values())


def test_neutral_abstract_mode_sends_the_template_never_the_statement(store, round_fixture):
    provider = StaticExternalProvider(default=[{"id": "X-1", "title": "Prior work", "score": 0.9}])
    result = _screen(store, round_fixture, external=provider, external_query_mode="neutral_abstract")

    statements = {i["body"] for i in round_fixture["ideas"]}
    issued = {call.text for call in provider.calls}
    assert provider.calls
    assert all(call.mode == "neutral_abstract" for call in provider.calls)
    assert not (issued & statements)
    # every issued query is exactly the template over that record's own
    # structured fields -- nothing the author wrote steered it
    expected = {
        neutral_abstract({"home": d["home"], "operation_declared": d["operation_declared"]})
        for d in result["dossiers"].values()
    }
    assert issued == expected
    sample = next(iter(result["dossiers"].values()))
    # The provider's own record, plus the reference-set tag that tells a
    # judged envelope (and the verdict evidence built from it) which set an
    # entry came from.
    assert sample["candidate_hits"]["R5"] == [
        {"id": "X-1", "title": "Prior work", "score": 0.9, "reference_set": "R5"}
    ]


def test_statement_mode_sends_the_statement_and_says_so(store, round_fixture):
    provider = StaticExternalProvider(default=[])
    _screen(store, round_fixture, external=provider, external_query_mode="statement")
    statements = {i["body"] for i in round_fixture["ideas"]}
    assert provider.calls
    assert all(call.mode == "statement" for call in provider.calls)
    assert {c.text for c in provider.calls} <= statements


def test_every_external_query_is_logged_with_its_launch_and_its_mode(store, round_fixture):
    provider = StaticExternalProvider(default=[{"id": "X-1", "score": 0.4}])
    result = _screen(store, round_fixture, external=provider, external_query_mode="neutral_abstract")
    rows = store.ops.execute(
        "SELECT launch_id, payload FROM event WHERE type = 'novelty_external_query'"
    ).fetchall()
    assert len(rows) == len(result["dossiers"])
    payloads = [json.loads(r["payload"]) for r in rows]
    assert {p["external_query_mode"] for p in payloads} == {"neutral_abstract"}
    assert {p["provider"] for p in payloads} == {"static"}
    assert {r["launch_id"] for r in rows} == {round_fixture["launches"]["lens-1"]}
    assert all(p["n_results"] == 1 for p in payloads)


def test_a_mode_that_needs_a_provider_and_has_none_is_refused(store, round_fixture):
    with pytest.raises(NoveltyError, match="no provider"):
        _screen(store, round_fixture, external_query_mode="neutral_abstract")


def test_an_unknown_query_mode_is_refused_before_anything_runs(store, round_fixture):
    with pytest.raises(NoveltyError, match="external_query_mode"):
        _screen(store, round_fixture, external_query_mode="everything")


def test_neutral_abstract_is_built_from_structured_fields_only():
    idea = {
        "home": "family-a/row-3",
        "operation_declared": "bridge/formalize",
        "body": "the statement that must not leave the machine",
        "probe": "the probe that must not either",
    }
    text = neutral_abstract(idea)
    assert "family-a" in text and "row-3" in text and "bridge/formalize" in text
    assert idea["body"] not in text and idea["probe"] not in text


def test_the_adjudication_draft_covers_the_ROUND_not_only_the_last_batch(store, round_fixture):
    """The mechanical half runs once per lens batch. A draft written from
    one batch would silently drop the round's earlier records every time a
    new batch landed."""
    from trialerror.lens.ideas import write_idea

    first = _screen(store, round_fixture)
    first_ids = sorted(first["dossiers"])

    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-2"],
        body="A record posted in the round's second batch.", home="family-b/row-14",
        provenance={"docs": round_fixture["corpus_doc_ids"][:1]}, operation_declared="bridge/robustify",
    )
    second = _screen(store, round_fixture)
    assert second["n_screened"] == 1

    draft = (round_dir(store.program_root, round_fixture["round_id"]) / "adjudication.md").read_text(encoding="utf-8")
    for idea_id in first_ids + sorted(second["dossiers"]):
        assert idea_id in draft
    assert f"dossiers on file for this round: {len(first_ids) + 1}" in draft


def test_batch_ids_count_batches_not_records(store, round_fixture):
    from trialerror.lens.ideas import write_idea

    assert _screen(store, round_fixture)["batch_id"] == "batch-0"
    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-2"],
        body="One more record in a later batch.", home="family-b/row-15",
        provenance={"docs": round_fixture["corpus_doc_ids"][:1]},
    )
    assert _screen(store, round_fixture)["batch_id"] == "batch-1"


def test_only_records_sharing_a_home_cell_with_the_batch_are_embedded(store, round_fixture):
    """The archive is never reset, so embedding all of it on every batch is
    the one line here that would get slower every round for the rest of the
    program's life. A record in a home cell the batch does not touch cannot
    merge with anything in it, so it is not embedded."""
    from unittest.mock import patch

    from trialerror.ingest.backends import FakeEmbedBackend

    _screen(store, round_fixture)  # first pass screens and embeds everything

    from trialerror.lens.ideas import write_idea

    write_idea(
        store, round_id=round_fixture["round_id"], author_launch=round_fixture["launches"]["lens-2"],
        body="A second-batch record in a cell of its own.", home="family-c/row-1",
        provenance={"docs": round_fixture["corpus_doc_ids"][:1]},
    )

    seen: list[int] = []
    real = FakeEmbedBackend.embed_batch

    def counting(self, texts, *, kind="document"):
        seen.append(len(texts))
        return real(self, texts, kind=kind)

    with patch.object(FakeEmbedBackend, "embed_batch", counting):
        _screen(store, round_fixture)
    # the first embed_batch call is the idea set; it is the new record alone,
    # not the eighteen already on file in other cells
    assert seen[0] == 1


# ---------------------------------------------------------------------------
# R3: what was compared, and what happens when nothing could be
# ---------------------------------------------------------------------------


def _drop_inventory_vectors(store, corpus) -> None:
    """Delete the inventory chunks' embeddings, leaving the rows themselves
    in place -- an inventory that is ingested but not yet embedded."""
    ids = corpus["inventory_chunk_ids"]
    ph = ",".join("?" for _ in ids)
    with store.knowledge:
        store.knowledge.execute(
            f"DELETE FROM emb WHERE chunk_sha256 IN (SELECT sha256 FROM chunk WHERE chunk_id IN ({ph}))", ids
        )
        store.knowledge.execute(
            f"DELETE FROM {vec_table_name(corpus['model_key'])} WHERE chunk_id IN ({ph})", ids
        )


def test_an_unembedded_inventory_refuses_the_screen_rather_than_quietly_disabling_it(store, round_fixture):
    """The failure this refusal exists for: with R3 present but unembedded,
    a record that IS an inventory row was not flagged, no dossier or batch
    field said so, and the snapshot still reported R3 n=8. The flag, the
    mandatory routing and the plant battery stop at once, and nothing in the
    artifacts says the measurement did not happen."""
    _drop_inventory_vectors(store, round_fixture)
    with pytest.raises(NoveltyError) as excinfo:
        _screen(store, round_fixture)
    assert "R3" in str(excinfo.value)
    assert "none is embedded" in str(excinfo.value)


def test_the_snapshot_and_every_dossier_report_how_much_of_r3_was_usable(store, round_fixture):
    result = _screen(store, round_fixture)
    n_rows = len(round_fixture["inventory_chunk_ids"])

    assert result["reference_snapshot"]["R3"]["n"] == n_rows
    assert result["reference_snapshot"]["R3"]["n_vectorized"] == n_rows
    assert result["inventory"] == {"n_rows": n_rows, "n_vectorized": n_rows}
    for dossier in result["dossiers"].values():
        assert dossier["inventory_compared"] == {
            "n_compared": n_rows, "n_rows": n_rows, "n_vectorized": n_rows,
        }


def test_r4_is_the_complement_of_the_whole_exclusion_tuple_not_of_one_kind(store, round_fixture):
    """R3 is identified by the ingest pipeline's own inventory kind, and R4
    counts everything the exclusion tuple does not hold -- so a second
    excluded kind cannot repoint R3 at itself nor be counted as prior art."""
    snapshot = reference_snapshot(store, round_id=round_fixture["round_id"], model_key=round_fixture["model_key"])
    assert snapshot["R3"]["kind"] == INVENTORY_SOURCE_KIND
    assert INVENTORY_SOURCE_KIND in snapshot["R4"]["excluded_kinds"]
    assert set(engine.DEFAULT_EXCLUDED_KINDS) <= set(snapshot["R4"]["excluded_kinds"])
    assert snapshot["R3"]["n"] + snapshot["R4"]["n"] == len(
        round_fixture["inventory_chunk_ids"] + round_fixture["corpus_chunk_ids"]
    )


# ---------------------------------------------------------------------------
# the rows a judge will be handed
# ---------------------------------------------------------------------------


def test_every_dossier_carries_the_nearest_inventory_rows_with_their_text(store, round_fixture):
    """The dossier is what the judged envelope is built from, so the rows the
    pairwise comparison needs have to be in it."""
    result = _screen(store, round_fixture)
    ids = round_fixture["inventory_chunk_ids"]
    row_text = {
        r["chunk_id"]: r["text"]
        for r in store.knowledge.execute(
            "SELECT chunk_id, text FROM chunk WHERE chunk_id IN ({})".format(",".join("?" for _ in ids)), ids
        )
    }
    for dossier in result["dossiers"].values():
        rows = dossier["inventory_rows"]
        assert 0 < len(rows) <= 5
        assert [r["similarity"] for r in rows] == sorted((r["similarity"] for r in rows), reverse=True)
        for row in rows:
            assert row["row_id"] in row_text
            assert row_text[row["row_id"]] in row["text"]

    # the flagged record's own dossier names the row that flagged it
    flagged = next(d for d in result["dossiers"].values() if d["known_mechanic"])
    assert flagged["known_mechanic"]["row_id"] in {r["row_id"] for r in flagged["inventory_rows"]}


def test_r4_hits_carry_the_engine_fenced_text_and_their_reference_set(store, round_fixture):
    result = _screen(store, round_fixture, candidate_hit_similarity=-1.0)
    hits = [h for d in result["dossiers"].values() for h in d["candidate_hits"]["R4"]]
    assert hits
    for hit in hits:
        assert hit["reference_set"] == "R4"
        assert hit["text"]


def test_a_dossier_names_two_labelled_reference_sets_not_three(store, round_fixture):
    """R5 is evidence for the literature label, not a labelled set of its own
    -- the dead `label_external` field asserted a verdict row that no code
    path ever wrote."""
    result = _screen(store, round_fixture)
    for dossier in result["dossiers"].values():
        assert {"label_inventory", "label_corpus"} <= set(dossier)
        assert "label_external" not in dossier


# ---------------------------------------------------------------------------
# R5 egress logging
# ---------------------------------------------------------------------------


class _RaisingProvider:
    name = "raising"

    def snapshot_id(self) -> str:
        return "raising-1"

    def search(self, query):
        raise RuntimeError("index unreachable")


def _external_query_events(store) -> list[dict]:
    return [
        json.loads(r["payload"])
        for r in store.ops.execute("SELECT payload FROM event WHERE type = ?", ("novelty_external_query",))
    ]


def test_a_provider_that_raises_still_leaves_the_egress_audit_line(store, round_fixture):
    """The text had already left the machine. Logging only on the happy path
    would drop the one record of what was sent exactly when sending it went
    wrong."""
    with pytest.raises(RuntimeError):
        _screen(store, round_fixture, external=_RaisingProvider(), external_query_mode="neutral_abstract")

    rows = _external_query_events(store)
    assert len(rows) == 1
    assert rows[0]["external_query_mode"] == "neutral_abstract"
    assert rows[0]["n_results"] == 0
    assert rows[0]["error"] == "RuntimeError: index unreachable"


def test_a_successful_query_logs_its_result_count_and_no_error(store, round_fixture):
    provider = StaticExternalProvider(default=[{"id": "X-1", "score": 0.9}])
    _screen(store, round_fixture, external=provider, external_query_mode="neutral_abstract")
    rows = _external_query_events(store)
    assert rows
    assert all(r["error"] is None and r["n_results"] == 1 for r in rows)
