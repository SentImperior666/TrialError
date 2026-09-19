"""Phase 3b — the judged half: scope, envelope, plants, verdicts, kappa.

This module never calls a model, and neither does the code under test. What
is exercised is everything AROUND the judgment: what a judge is allowed to
see, which ideas it sees, what rides in the batch alongside them, and what
happens to a batch whose judge fails the audit.
"""

from __future__ import annotations

import json

import pytest

from trialerror.lens.ideas import read_idea
from trialerror.lens.novelty import (
    INVENTORY_LABELS,
    _paraphrase_statement,
    UNJUDGED_LABEL,
    UNJUDGED_QUALIFIER,
    LITERATURE_LABELS,
    PROCEDURE,
    PROCEDURE_VERSION,
    WITHHELD_FROM_JUDGE,
    NoveltyError,
    build_judged_batch,
    build_judge_views,
    build_plants,
    mechanic_row_candidates,
    parse_mechanic_rows,
    build_verifier_envelope,
    cohens_kappa,
    record_novelty_verdicts,
    round_dir,
    run_mechanical_screen,
    score_plants,
    select_judged_scope,
    strip_self_assessment,
)

from trialerror.stores.vecindex import vec_table_name

from tests._novelty_fixtures import build_round

SEED = "seed-judged"


@pytest.fixture()
def screened(store):
    fixture = build_round(store)
    mechanical = run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    return {**fixture, "mechanical": mechanical, "dossiers": mechanical["dossiers"]}


def _batch(store, screened, **kwargs):
    return build_judged_batch(
        store, round_id=screened["round_id"], dossiers=screened["dossiers"], seed=SEED, **kwargs
    )


def _label_everything(batch, *, inventory="new-mechanism", literature="absent", plants="same"):
    """Label every subject in a batch: the real records as told, and every
    plant as caught. Tests that want a miss override one entry."""
    labels = {i: {"label_inventory": inventory, "label_corpus": literature} for i in batch["scope"]["scope"]}
    for plant in batch["plants"]:
        labels[plant["plant_id"]] = {"label_inventory": plants, "label_corpus": "stated"}
    return labels


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def test_the_scope_is_flagged_plus_hits_plus_a_seeded_sample(screened):
    scope = select_judged_scope(screened["dossiers"], seed=SEED)
    assert scope["flagged"]  # the fixture's inventory-row record
    assert set(scope["scope"]) == set(scope["flagged"]) | set(scope["retrieval_hits"]) | set(scope["sampled"])
    assert set(scope["unjudged"]) == set(screened["dossiers"]) - set(scope["scope"])
    assert not (set(scope["unjudged"]) & set(scope["scope"]))


def test_every_flagged_idea_is_in_scope_and_no_flagged_idea_can_be_sampled_out(screened):
    scope = select_judged_scope(screened["dossiers"], seed=SEED, sample_fraction=0.0)
    flagged = [i for i, d in screened["dossiers"].items() if d["known_mechanic"]]
    assert set(flagged) <= set(scope["scope"])
    assert scope["sampled"] == []


def test_the_sample_is_a_seeded_draw_that_reproduces(screened):
    a = select_judged_scope(screened["dossiers"], seed=SEED)
    b = select_judged_scope(screened["dossiers"], seed=SEED)
    c = select_judged_scope(screened["dossiers"], seed="a-different-seed")
    assert a["sampled"] == b["sampled"]
    assert a["sampled"] != c["sampled"] or len(a["sampled"]) <= 1


def test_the_sample_rounds_up_so_a_small_remainder_is_never_zero(screened):
    scope = select_judged_scope(screened["dossiers"], seed=SEED, sample_fraction=0.2)
    remainder = len(scope["sampled"]) + len(scope["unjudged"])
    assert len(scope["sampled"]) >= 1
    assert len(scope["sampled"]) == int(-(-remainder * 0.2 // 1))


# ---------------------------------------------------------------------------
# the envelope
# ---------------------------------------------------------------------------


def test_the_envelope_carries_raw_fields_and_retrieved_text_and_nothing_else(store, screened):
    idea_id = next(iter(screened["dossiers"]))
    idea = read_idea(store, idea_id=idea_id)
    envelope = build_verifier_envelope(idea, retrieved=[{"chunk_id": "CHK-x", "text": "prior work"}])

    assert set(envelope["record"]) == {
        "requirements", "statement", "home_mechanic", "probe", "provenance_docs", "extra_text",
    }
    assert envelope["record"]["extra_text"] is None
    flat = json.dumps(envelope)
    for withheld in ("author_rationale", "surprise", "assumed_circle", "seat", "recipe_card"):
        assert withheld in WITHHELD_FROM_JUDGE
        assert withheld not in envelope["record"]
    # and the VALUES the author wrote into those fields are absent too --
    # a key that is gone but whose text rides along in another field is not
    # a withheld field
    assert idea["author_rationale"] not in flat
    assert idea["surprise"] not in flat
    assert idea["recipe_card"] not in flat
    assert idea["author_launch"] not in flat


def test_the_envelope_names_both_label_vocabularies_and_no_others(store, screened):
    idea = read_idea(store, idea_id=next(iter(screened["dossiers"])))
    envelope = build_verifier_envelope(idea)
    assert envelope["labels"] == {"inventory": list(INVENTORY_LABELS), "literature": list(LITERATURE_LABELS)}


def test_self_assessment_sentences_are_stripped_from_the_envelope_only(store, screened):
    from trialerror.lens.ideas import write_idea

    row = write_idea(
        store, round_id=screened["round_id"], author_launch=screened["launches"]["lens-1"],
        body="This is a novel mechanism unlike any existing system. The token moves one step per phase.",
        home="family-a/row-20", operation_declared="bridge/formalize",
    )
    idea = read_idea(store, idea_id=row["idea_id"])
    envelope = build_verifier_envelope(idea)
    assert "novel" not in envelope["record"]["statement"]
    assert "The token moves one step per phase." in envelope["record"]["statement"]
    assert envelope["self_assessment_removed"]
    # the record itself is untouched: the feed post stays full text
    assert "novel" in read_idea(store, idea_id=row["idea_id"])["body"]


def test_a_statement_that_is_mostly_self_assessment_is_flagged_for_unscreenable():
    stripped = strip_self_assessment(
        "This is a novel and unprecedented approach. It is unlike anything in the field. It is a breakthrough."
    )
    assert stripped["mostly_self_assessment"] is True
    # and the flag is reported, not applied: nothing here assigns a label
    assert "label" not in stripped


def test_the_stripper_is_hygiene_and_a_paraphrase_walks_past_it():
    """Asserted deliberately, because the module says so: the stripper is
    trivially paraphrased and is NOT the defence. A test that only showed it
    working would misrepresent what it is for."""
    paraphrase = "No register row states this procedure. The token moves one step per phase."
    assert strip_self_assessment(paraphrase)["removed"] == []


# ---------------------------------------------------------------------------
# plants
# ---------------------------------------------------------------------------


def test_a_judged_batch_carries_five_plants_of_each_kind(store, screened):
    batch = _batch(store, screened)
    kinds = [p["kind"] for p in batch["plants"]]
    assert kinds.count("inventory") == 5
    assert kinds.count("paraphrase") == 5
    assert all(p["expected_labels"] == ["same", "variant"] for p in batch["plants"])


def test_an_inventory_plant_carries_the_row_it_was_built_from(store, screened):
    """The plant names both ids -- the CHUNK it was handed from and the
    M-row inside it -- and its statement is that row's DESCRIPTION, never
    the chunk (which carries the id and the name a judge would recognise as
    a register row rather than as a record)."""
    batch = _batch(store, screened)
    plant = next(p for p in batch["plants"] if p["kind"] == "inventory")
    assert plant["source_ref"] in screened["inventory_chunk_ids"]
    row_text = store.knowledge.execute(
        "SELECT text FROM chunk WHERE chunk_id = ?", (plant["source_ref"],)
    ).fetchone()["text"]
    mechanic = parse_mechanic_rows(row_text)[0]
    assert plant["source_row_id"] == mechanic["row_id"]
    assert mechanic["statement"] in plant["record"]["statement"]
    assert row_text not in plant["record"]["statement"]


def test_no_inventory_plant_statement_is_ever_the_chunk_it_came_from(store, screened):
    batch = _batch(store, screened)
    texts = {
        row["chunk_id"]: row["text"]
        for row in store.knowledge.execute("SELECT chunk_id, text FROM chunk")
    }
    inventory = [p for p in batch["plants"] if p["kind"] == "inventory"]
    assert inventory
    for plant in inventory:
        statement = plant["record"]["statement"]
        assert statement != texts[plant["source_ref"]]
        assert plant["source_row_id"] not in statement


# ---------------------------------------------------------------------------
# a paraphrase plant is never its donor's own bytes
# ---------------------------------------------------------------------------


_PARAPHRASE_DONORS = [
    "A tracked resource is converted into position, and the conversion rate falls as the track fills.",
    "Two seats share one action budget and must declare their split before either acts.",
    "The value of a held card C is a function of how many phases C has been held.",
    "A depleted common pool refills at the start of every third phase. The refill is public and is "
    "announced. Nobody may hold more than 3 at once.",
    "The model records why a state changed, not only that it changed.",
]

_NUMBER_RE = __import__("re").compile(r"\d+")
_WORD_RE = __import__("re").compile(r"[A-Za-z0-9]+")


def _seeded(seed):
    from trialerror.lens.quota import derive_rng

    return derive_rng(seed, salt="plants")


def test_a_paraphrase_plant_is_never_byte_identical_to_its_donor_over_200_seeds():
    """The reproduction: the frame pool carries the identity frame, nothing
    else touched the text, and two paraphrase plants of a live batch carried
    the donor statement verbatim -- asking a judge whether a record is the
    same as itself."""
    for donor in _PARAPHRASE_DONORS:
        for i in range(200):
            statement, method = _paraphrase_statement(_seeded(f"seed-{i}"), donor)
            assert statement.strip() != donor.strip(), (donor, i)
            assert method["frame"] != "{text}"
            assert method["transformations"], (donor, i, statement)


def test_the_paraphrase_method_names_the_frame_and_every_transformation_applied():
    statement, method = _paraphrase_statement(
        _seeded("seed-method"), "The value of a held card C is a function of how many phases C has been held."
    )
    assert set(method) == {"frame", "transformations", "symbol_map", "dropped_tokens"}
    assert "symbol_rename" in method["transformations"]
    assert method["symbol_map"] and "C" in method["symbol_map"]
    assert method["symbol_map"]["C"] in statement


def test_a_paraphrase_loses_no_number_and_no_word_of_its_donor():
    """Nothing is lost silently: every number survives, and every word
    survives except the single-letter symbols the method names as renamed
    and the coordinating conjunction it names as dropped -- both recorded,
    neither guessed at."""
    for donor in _PARAPHRASE_DONORS:
        for i in range(25):
            statement, method = _paraphrase_statement(_seeded(f"seed-{i}"), donor)
            for number in _NUMBER_RE.findall(donor):
                assert number in statement, (donor, number)
            renamed = set(method["symbol_map"])
            dropped = {w.lower() for w in method["dropped_tokens"]}
            for word in _WORD_RE.findall(donor):
                if word.lower() in dropped:
                    continue
                if word in renamed:
                    assert method["symbol_map"][word] in _WORD_RE.findall(statement)
                    continue
                assert word.lower() in [w.lower() for w in _WORD_RE.findall(statement)], (donor, word)


def test_a_paraphrase_reproduces_under_its_seed():
    a = _paraphrase_statement(_seeded("seed-x"), _PARAPHRASE_DONORS[0])
    b = _paraphrase_statement(_seeded("seed-x"), _PARAPHRASE_DONORS[0])
    assert a == b


def test_every_paraphrase_plant_in_a_batch_differs_from_the_record_it_paraphrases(store, screened):
    batch = _batch(store, screened)
    paraphrases = [p for p in batch["plants"] if p["kind"] == "paraphrase"]
    assert paraphrases
    for plant in paraphrases:
        donor = read_idea(store, idea_id=plant["source_ref"])
        donor_statement = strip_self_assessment(donor["body"])["text"].strip()
        assert plant["record"]["statement"].strip() != donor_statement
        assert plant["paraphrase_method"]["transformations"]


# ---------------------------------------------------------------------------
# plants come from MECHANIC ROWS, or the battery refuses to build
# ---------------------------------------------------------------------------


_MIXED_MECHANICS = [
    "| REGC-M001 | carried surplus | an unspent allowance is carried forward once and then expires |",
    "| REGC-M002 | paired reveal | two seats reveal together and the later reveal is discarded |",
    "| REGC-M003 | tapering refill | the refill rate falls by one for each completed phase |",
]
_MIXED_PROSE = (
    "Coverage note: every row in this register cites the page anchor of its source rather than the "
    "page number, and the rows below are not exhaustive for this family."
)


def _repoint_inventory_at(store, screened, paragraphs):
    """Make ``paragraphs`` the ONLY embedded register text in this store.

    The old rows stay ingested and lose their vectors, which is exactly what
    ``_inventory_rows`` treats as "not in R3" -- the same lever
    ``test_a_batch_with_no_inventory_plants...`` pulls, used here to control
    what the battery may draw from rather than to empty it."""
    from tests._inventory_fixtures import add_document
    from trialerror.stores.vecindex import ensure_vec_table

    ids = screened["inventory_chunk_ids"]
    ph = ",".join("?" for _ in ids)
    with store.knowledge:
        store.knowledge.execute(
            f"DELETE FROM emb WHERE chunk_sha256 IN (SELECT sha256 FROM chunk WHERE chunk_id IN ({ph}))", ids
        )
        store.knowledge.execute(
            f"DELETE FROM {vec_table_name(screened['model_key'])} WHERE chunk_id IN ({ph})", ids
        )
    backend = ensure_vec_table(store.knowledge, screened["model_key"], screened["dims"])
    return add_document(
        store, source_id=screened["inventory_source_id"], rel_path="archive/family-c.md",
        paragraphs=paragraphs, launch_id=screened["launch_id"], model_key=screened["model_key"],
        embed_backend=screened["embed_backend"], backend=backend, row_per_element=True,
    )


def test_plants_are_drawn_only_from_mechanic_rows_never_from_prose(store, screened):
    """The reproduction: a batch's two inventory plants were a
    citation-convention note and a coverage-gap list, neither of which
    states a mechanism -- so a judge doing the task returns `unscreenable`,
    the scorer counts a missed inventory plant and the batch fails on the
    battery's own sampling."""
    _repoint_inventory_at(store, screened, [*_MIXED_MECHANICS, _MIXED_PROSE])
    plants = build_plants(store, dossiers=screened["dossiers"], seed=SEED, k=3)
    inventory = [p for p in plants if p["kind"] == "inventory"]
    assert len(inventory) == 3
    descriptions = {parse_mechanic_rows(row)[0]["statement"] for row in _MIXED_MECHANICS}
    row_ids = {parse_mechanic_rows(row)[0]["row_id"] for row in _MIXED_MECHANICS}
    for plant in inventory:
        assert plant["source_row_id"] in row_ids
        assert any(d in plant["record"]["statement"] for d in descriptions)
        assert "Coverage note" not in plant["record"]["statement"]


def test_a_reference_set_with_too_few_mechanic_rows_refuses_rather_than_planting_prose(store, screened):
    _repoint_inventory_at(store, screened, [*_MIXED_MECHANICS, _MIXED_PROSE])
    with pytest.raises(NoveltyError) as excinfo:
        build_plants(store, dossiers=screened["dossiers"], seed=SEED, k=4)
    assert "mechanic row" in str(excinfo.value)
    assert excinfo.value.code == "no_mechanic_rows"


def test_a_register_of_pure_prose_refuses_outright(store, screened):
    _repoint_inventory_at(store, screened, [_MIXED_PROSE, "Another paragraph that states no mechanism."])
    with pytest.raises(NoveltyError) as excinfo:
        build_plants(store, dossiers=screened["dossiers"], seed=SEED, k=1)
    assert "0 mechanic row" in str(excinfo.value)


def test_parse_mechanic_rows_reads_ids_names_and_descriptions():
    rows = parse_mechanic_rows(
        "| id | name | description |\n| --- | --- | --- |\n"
        "| REGC-M001 | carried surplus | an unspent allowance is carried forward |\n"
        "| not-a-row | prose | this cell has no M-id |\n"
        "| REGC-M002 | empty | |\n"
    )
    assert [r["row_id"] for r in rows] == ["REGC-M001"]
    assert rows[0]["name"] == "carried surplus"
    assert rows[0]["statement"] == "an unspent allowance is carried forward"


def test_parse_mechanic_rows_takes_a_two_cell_row_as_id_and_description():
    rows = parse_mechanic_rows("| REG-M012 | a timer advances whenever a seat declines to act |")
    assert rows[0]["statement"] == "a timer advances whenever a seat declines to act"
    assert rows[0]["name"] is None


def test_parse_mechanic_rows_follows_a_named_description_column():
    rows = parse_mechanic_rows(
        "| id | what it does | notes |\n| REG-M003 | the pool refills every third phase | see p. 12 |"
    )
    assert rows[0]["statement"] == "the pool refills every third phase"


def test_a_foreign_table_ahead_of_the_register_does_not_steal_the_description_column():
    """Fix pass N-1. A coverage table printed above the register table has a
    ``description`` column of its own at another position; reading it as the
    register's turned the plant statement into the row's two-word NAME — the
    bare label the mechanic-row filter exists to prevent."""
    rows = parse_mechanic_rows(
        "| family | description | gaps |\n| --- | --- | --- |\n"
        "| family-a | four rows about resource tracks | 2 |\n"
        "\n"
        "| id | name | description |\n| --- | --- | --- |\n"
        "| REGA-M001 | track step | a spent resource advances a shared track by exactly one step |\n"
    )
    assert [r["row_id"] for r in rows] == ["REGA-M001"]
    assert rows[0]["name"] == "track step"
    assert rows[0]["statement"] == "a spent resource advances a shared track by exactly one step"


def test_each_table_block_reads_its_own_header():
    """Two register tables in one chunk, each with its description column in
    a different place: each block's rows follow their own header."""
    rows = parse_mechanic_rows(
        "| id | name | description |\n| --- | --- | --- |\n"
        "| REGA-M001 | first | the first block states a mechanism here |\n"
        "\n"
        "some prose between the two tables\n"
        "\n"
        "| id | what it does | page |\n| --- | --- | --- |\n"
        "| REGA-M002 | the second block states its mechanism in cell one | p. 4 |\n"
    )
    assert [r["statement"] for r in rows] == [
        "the first block states a mechanism here",
        "the second block states its mechanism in cell one",
    ]


def test_mechanic_row_candidates_carry_both_ids():
    candidates = mechanic_row_candidates(
        [{"row_id": "CHK-1", "doc_id": "DOC-1", "family": "family-c", "text": _MIXED_MECHANICS[0]},
         {"row_id": "CHK-2", "doc_id": "DOC-1", "family": "family-c", "text": _MIXED_PROSE}]
    )
    assert [c["row_id"] for c in candidates] == ["CHK-1"]
    assert candidates[0]["source_row_id"] == "REGC-M001"
    assert candidates[0]["family"] == "family-c"


def test_plants_ride_among_the_real_envelopes_not_in_a_trailing_block(store, screened):
    """A plant a judge can pick out by position tests nothing."""
    batch = _batch(store, screened)
    positions = [i for i, e in enumerate(batch["envelopes"]) if e["subject_id"].startswith("PLANT-")]
    assert len(positions) == 10
    assert positions != list(range(len(batch["envelopes"]) - 10, len(batch["envelopes"])))
    # and a plant envelope is shaped exactly like a real one -- same keys
    plant_env = batch["envelopes"][positions[0]]
    real_env = next(e for e in batch["envelopes"] if not e["subject_id"].startswith("PLANT-"))
    assert set(plant_env) == set(real_env)
    assert set(plant_env["record"]) == set(real_env["record"])


def test_the_batch_reproduces_from_its_seed(store, screened):
    a = _batch(store, screened, batch_id="a")
    b = _batch(store, screened, batch_id="b")
    assert [e["subject_id"] for e in a["envelopes"]] == [e["subject_id"] for e in b["envelopes"]]
    assert [p["source_ref"] for p in a["plants"]] == [p["source_ref"] for p in b["plants"]]


def test_score_plants_counts_catches_misses_and_unlabelled_apart(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    del labels[batch["plants"][0]["plant_id"]]
    scored = score_plants(batch, labels)
    assert scored["n_plants"] == 10
    assert len(scored["caught"]) == 9
    assert scored["unlabelled"] == [batch["plants"][0]["plant_id"]]


# ---------------------------------------------------------------------------
# recording the verdicts
# ---------------------------------------------------------------------------


def test_verdict_rows_carry_the_procedure_version_and_the_prereg(store, screened):
    from trialerror.verify.prereg import commit_prereg

    prereg = commit_prereg(store, title="round", procedure="aiif-round-v2", params={"seed": SEED})
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"], prereg_id=prereg["prereg_id"],
    )
    assert recorded["status"] == "screened"
    # two rows per judged idea, one mechanical row per unjudged survivor
    assert recorded["n_verdicts"] == 2 * len(batch["scope"]["scope"]) + len(batch["scope"]["unjudged"])
    for verdict in recorded["verdicts"]:
        assert verdict["procedure"] == PROCEDURE == "custom"
        assert verdict["procedure_version"] == PROCEDURE_VERSION == "novelty-v2"
        assert verdict["prereg_id"] == prereg["prereg_id"]
        assert verdict["subject_kind"] == "claim"
    labels_written = {v["label"] for v in recorded["verdicts"]}
    assert labels_written == {"R3:new-mechanism", "R4:absent", "R3:no-close-neighbour:unjudged"}


def test_prereg_compliant_is_stamped_when_the_round_names_what_it_ran(store, screened):
    """The column used to be NULL on every novelty verdict, and the omission
    was not listed anywhere. The screen does not hold the round's charter,
    so the caller passes it -- and when it does not, the result says why
    rather than the gap being invisible."""
    from trialerror.verify.prereg import commit_prereg

    prereg = commit_prereg(store, title="round", procedure="aiif-round-v2", params={"seed": SEED})
    batch = _batch(store, screened)

    unstamped = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"], prereg_id=prereg["prereg_id"],
    )
    assert unstamped["prereg_compliant"] is None
    assert "executed_procedure" in unstamped["prereg_compliance"]
    assert all(v["prereg_compliant"] is None for v in unstamped["verdicts"])

    stamped = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"], prereg_id=prereg["prereg_id"],
        executed_procedure="aiif-round-v2", executed_params={"seed": SEED}, supersede=True,
    )
    assert stamped["prereg_compliant"] is True
    assert all(v["prereg_compliant"] == 1 for v in stamped["verdicts"])

    diverged = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"], prereg_id=prereg["prereg_id"],
        executed_procedure="aiif-round-v2", executed_params={"seed": "a-different-seed"}, supersede=True,
    )
    assert diverged["prereg_compliant"] is False
    # and it says WHICH of the two hashes moved: the params did, the
    # procedure did not
    assert diverged["prereg_compliance_detail"]["mismatched"] == ["params"]
    assert "params hash disagrees" in diverged["prereg_compliance"]
    assert "procedure hash" not in diverged["prereg_compliance"]


def test_a_procedure_that_diverges_names_the_procedure_axis(store, screened):
    from trialerror.verify.prereg import commit_prereg

    prereg = commit_prereg(store, title="round", procedure="aiif-round-v2\n", params={"seed": SEED})
    batch = _batch(store, screened)
    # exactly the observed failure: the committed procedure ends in a
    # newline, the caller's shell stripped it
    stripped = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"], prereg_id=prereg["prereg_id"],
        executed_procedure="aiif-round-v2", executed_params={"seed": SEED},
    )
    assert stripped["prereg_compliant"] is False
    assert stripped["prereg_compliance_detail"]["mismatched"] == ["procedure"]
    assert "procedure hash disagrees" in stripped["prereg_compliance"]
    assert "--executed-procedure-file" in stripped["prereg_compliance"]

    byte_exact = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"], prereg_id=prereg["prereg_id"],
        executed_procedure="aiif-round-v2\n", executed_params={"seed": SEED}, supersede=True,
    )
    assert byte_exact["prereg_compliant"] is True


def test_a_clean_batch_consolidates_every_survivor_not_only_the_judged_ones(store, screened):
    """The pruning this closes: on the fixture round, 18 records became 1
    merged, 6 judged and 5 consolidated -- 12 left `raw` and silently out of
    the round, behind a retrieval threshold and a 20% sample. Phase 5 rooms
    every consolidated idea, and nothing may be pruned on a proxy."""
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    for idea_id in batch["scope"]["scope"]:
        assert read_idea(store, idea_id=idea_id)["status"] == "consolidated"
    for idea_id in batch["scope"]["unjudged"]:
        assert read_idea(store, idea_id=idea_id)["status"] == "consolidated"

    assert recorded["n_consolidated"] == len(screened["dossiers"])
    assert recorded["n_consolidated_unjudged"] == len(batch["scope"]["unjudged"])
    assert sorted(recorded["consolidated_unjudged"]) == sorted(batch["scope"]["unjudged"])


def test_an_unjudged_survivor_carries_the_mechanical_pair_as_its_own_row(store, screened):
    """`no-close-neighbour . unjudged` is recorded, not assumed -- and it is
    never written as, or mistakable for, `new-mechanism`."""
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    unjudged = set(batch["scope"]["unjudged"])
    assert unjudged
    rows = [v for v in recorded["verdicts"] if v["subject_id"] in unjudged]
    assert {v["subject_id"] for v in rows} == unjudged
    for row in rows:
        assert row["label"] == f"R3:{UNJUDGED_LABEL}:{UNJUDGED_QUALIFIER}"
        assert "new-mechanism" not in row["label"]
        evidence = json.loads(row["evidence"])
        assert any("outside the judged scope" in i.get("note", "") for i in evidence)


def test_an_idea_in_scope_the_judge_never_labelled_is_reported_not_consolidated(store, screened):
    """A judge that simply omits a scoped idea used to be silent: the subject
    was skipped, the batch still read 'screened', and the count appeared in
    no field."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    omitted = batch["scope"]["scope"][0]
    del labels[omitted]

    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["unlabelled_scope"] == [omitted]
    assert recorded["unlabelled_scope_share"] > 0
    assert omitted not in recorded["consolidated_unjudged"]
    assert read_idea(store, idea_id=omitted)["status"] == "raw"


def test_a_non_trivial_share_of_unlabelled_scope_caveats_the_batch(store, screened):
    batch = _batch(store, screened)
    labels = {p["plant_id"]: {"label_inventory": "same"} for p in batch["plants"]}

    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["status"] == "reopened_with_caveat"
    assert "unlabelled_scope" in recorded["caveats"]
    assert recorded["unlabelled_scope"] == sorted(batch["scope"]["scope"])
    assert recorded["n_consolidated"] == 0


def test_a_second_recording_is_refused_unless_it_says_it_is_superseding(store, screened):
    """Design 5.2(3): one submission per idea per judge. A second call used
    to write a contradicting row beside the first -- ['R3:same',
    'R3:new-mechanism:reopened_with_caveat'] for one subject -- and re-stamp
    the record 'consolidated'."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    first = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    with pytest.raises(NoveltyError, match="one submission per idea per judge"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=labels,
            issued_by_launch=screened["launches"]["lens-1"],
        )
    again = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], supersede=True,
    )
    assert again["n_verdicts"] > 0
    # the verdict table is append-only, so a superseding row has to name the
    # rows it replaces -- two rows for one subject and reference set with
    # nothing joining them is the contradiction the rule exists to prevent
    assert set(again["superseded"]) == {v["verdict_id"] for v in first["verdicts"]}
    for verdict in again["verdicts"]:
        notes = [i.get("note", "") for i in json.loads(verdict["evidence"])]
        assert any(n.startswith("supersedes ") for n in notes), verdict["label"]


def test_a_merged_record_is_never_revived_to_consolidated_by_a_rescreen(store, screened):
    """A folded record back in the round without the Phase 7 ruling that
    alone reopens one."""
    merged_id = screened["mechanical"]["merged"][0]["idea_id"]
    assert read_idea(store, idea_id=merged_id)["status"] == "merged"

    dossiers = dict(screened["dossiers"])
    dossiers[merged_id] = {**next(iter(dossiers.values())), "idea_id": merged_id, "known_mechanic": None}
    batch = build_judged_batch(store, round_id=screened["round_id"], dossiers=dossiers, seed=SEED)

    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert read_idea(store, idea_id=merged_id)["status"] == "merged"
    assert {s["idea_id"] for s in recorded["consolidation_skipped"]} == {merged_id}
    assert recorded["consolidation_skipped"][0]["status"] == "merged"


def test_a_missed_inventory_plant_fails_the_batch_and_nothing_is_silent(store, screened):
    """The audit's whole point. The labels are still written -- hiding them
    would be the silence the design forbids -- but they carry the caveat and
    no idea advances on that judge's word."""
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    missed = next(p for p in batch["plants"] if p["kind"] == "inventory")
    labels[missed["plant_id"]] = {"label_inventory": "new-mechanism", "label_corpus": "absent"}

    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["status"] == "reopened_with_caveat"
    assert recorded["plants"]["batch_failed"] is True
    assert recorded["plants"]["inventory_failures"] == [missed["plant_id"]]
    assert recorded["n_verdicts"] > 0
    assert recorded["caveats"] == ["plants_failed"]
    assert all(v["label"].endswith(":plants_failed") for v in recorded["verdicts"])
    for idea_id in [*batch["scope"]["scope"], *batch["scope"]["unjudged"]]:
        assert read_idea(store, idea_id=idea_id)["status"] == "raw"


def test_a_missed_paraphrase_plant_is_reported_but_does_not_fail_the_batch(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    missed = next(p for p in batch["plants"] if p["kind"] == "paraphrase")
    labels[missed["plant_id"]] = {"label_inventory": "new-mechanism", "label_corpus": "absent"}

    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    assert recorded["status"] == "screened"
    assert [m["plant_id"] for m in recorded["plants"]["missed"]] == [missed["plant_id"]]
    assert recorded["plants"]["batch_failed"] is False


def test_a_label_outside_its_vocabulary_is_refused_before_anything_is_written(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    labels[batch["scope"]["scope"][0]]["label_inventory"] = "quite-new"
    with pytest.raises(NoveltyError, match="label_inventory"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=labels,
            issued_by_launch=screened["launches"]["lens-1"],
        )
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"] == 0


def test_a_label_for_a_subject_outside_the_batch_is_refused(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    labels["IDEA-not-in-this-batch"] = {"label_inventory": "same"}
    with pytest.raises(NoveltyError, match="judged scope"):
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=labels,
            issued_by_launch=screened["launches"]["lens-1"],
        )


# ---------------------------------------------------------------------------
# the second judge and kappa
# ---------------------------------------------------------------------------


def test_a_tenth_of_the_scope_is_selected_for_a_second_judge(store, screened):
    batch = _batch(store, screened)
    assert set(batch["second_judge"]) <= set(batch["scope"]["scope"])
    assert len(batch["second_judge"]) == int(-(-len(batch["scope"]["scope"]) * 0.1 // 1))
    assert _batch(store, screened, batch_id="again")["second_judge"] == batch["second_judge"]


def test_kappa_is_computed_per_label_set(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    second = {
        s: dict(labels[s]) for s in batch["second_judge"]
    }
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=labels,
        issued_by_launch=screened["launches"]["lens-1"], second_judge_labels=second,
    )
    assert set(recorded["kappa"]) == {"label_inventory", "label_corpus"}
    for stats in recorded["kappa"].values():
        assert stats["n"] == len(batch["second_judge"])


def test_cohens_kappa_reports_n_and_observed_agreement_beside_the_number():
    first = {"a": "same", "b": "variant", "c": "same", "d": "new-mechanism"}
    second = {"a": "same", "b": "variant", "c": "variant", "d": "new-mechanism"}
    stats = cohens_kappa(first, second, categories=INVENTORY_LABELS)
    assert stats["n"] == 4
    assert stats["observed_agreement"] == 0.75
    assert -1.0 <= stats["kappa"] <= 1.0


def test_kappa_on_perfect_disagreement_and_perfect_agreement():
    perfect = {"a": "same", "b": "variant", "c": "same", "d": "variant"}
    assert cohens_kappa(perfect, perfect, categories=INVENTORY_LABELS)["kappa"] == 1.0
    flipped = {"a": "variant", "b": "same", "c": "variant", "d": "same"}
    assert cohens_kappa(perfect, flipped, categories=INVENTORY_LABELS)["kappa"] < 0


def test_kappa_refuses_to_report_a_number_over_fewer_than_two_subjects():
    stats = cohens_kappa({"a": "same"}, {"a": "same"}, categories=INVENTORY_LABELS)
    assert stats["kappa"] is None
    assert stats["n"] == 1
    assert "fewer than two" in stats["note"]


def test_one_category_used_throughout_reads_as_agreement_not_as_zero():
    """The degenerate 0/0 case, decided explicitly rather than left to
    whatever the formula does with it."""
    same = {"a": "same", "b": "same", "c": "same"}
    assert cohens_kappa(same, same, categories=INVENTORY_LABELS)["kappa"] == 1.0


# ---------------------------------------------------------------------------
# what lands on disk
# ---------------------------------------------------------------------------


def test_the_judged_batch_and_its_verdict_summary_land_under_the_round(store, screened):
    batch = _batch(store, screened)
    base = round_dir(store.program_root, screened["round_id"])
    assert (base / "judged" / f"{batch['batch_id']}.json").is_file()

    record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    summary = json.loads((base / "judged" / f"{batch['batch_id']}-verdicts.json").read_text(encoding="utf-8"))
    assert summary["status"] == "screened"
    assert summary["plants"]["catch_rate"] == 1.0


def test_a_verdict_cites_the_evidence_its_judge_was_shown(store, screened):
    """A verdict whose evidence array says only "a judge said so" is not
    evidence-anchored. The rows carry the ids that actually rode in that
    idea's envelope, plus the reference-snapshot note."""
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    evidence = json.loads(recorded["verdicts"][0]["evidence"])
    assert evidence, "a verdict with an empty evidence array is not evidence-anchored"
    assert any(item.get("note", "").startswith("reference_snapshot R1=") for item in evidence)

    with_hits = next(
        (v for v in recorded["verdicts"] if any("chunk_id" in i for i in json.loads(v["evidence"]))),
        None,
    )
    assert with_hits is not None, "the fixture round has at least one record with a retrieval hit"
    cited = [i for i in json.loads(with_hits["evidence"]) if "chunk_id" in i]
    # each cited id carries THIS verdict's own label as its stance
    assert {i["stance"] for i in cited} == {with_hits["label"].split(":", 1)[1]}


def test_each_verdict_cites_its_own_reference_set_and_not_the_other_one(store, screened):
    """The R3 verdict used to cite the R4 chunks of the same idea, each
    stamped with the R3 label -- a claim about the inventory anchored in the
    corpus, and for a flagged record whose R4 bundle was empty, a
    "this is `same` as a register row" citing no row at all."""
    batch = _batch(store, screened)
    recorded = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=_label_everything(batch),
        issued_by_launch=screened["launches"]["lens-1"],
    )
    by_subject: dict[str, dict[str, list[dict]]] = {}
    for verdict in recorded["verdicts"]:
        reference_set = verdict["label"].split(":", 1)[0]
        by_subject.setdefault(verdict["subject_id"], {})[reference_set] = json.loads(verdict["evidence"])

    checked = 0
    for idea_id in batch["scope"]["scope"]:
        envelope = next(e for e in batch["envelopes"] if e["subject_id"] == idea_id)
        rows = by_subject[idea_id]
        r3_cited = {i["chunk_id"] for i in rows["R3"] if "chunk_id" in i}
        r4_cited = {i["chunk_id"] for i in rows["R4"] if "chunk_id" in i}
        assert r3_cited == {r["row_id"] for r in envelope["inventory_rows"]}
        assert r4_cited == {h["chunk_id"] for h in envelope["retrieved"] if h.get("chunk_id")}
        assert r3_cited, "an inventory verdict citing no inventory row is the bug this closes"
        assert not (r3_cited & r4_cited)
        checked += 1
    assert checked


def test_the_batch_carries_the_reference_snapshot_the_dossiers_were_built_against(store, screened):
    batch = _batch(store, screened)
    assert batch["reference_snapshot"]["R3"]["sha256"] == (
        screened["mechanical"]["reference_snapshot"]["R3"]["sha256"]
    )


# ---------------------------------------------------------------------------
# what the judge is actually handed
# ---------------------------------------------------------------------------


def test_every_scoped_envelope_carries_the_inventory_rows_and_the_retrieved_text(store, screened):
    """The pairwise comparison against retrieved rows is the load-bearing
    defence. An envelope carrying ids and similarities but no rows asks the
    judge to answer "does a register row already state this" from memory."""
    batch = _batch(store, screened)
    scoped = {e["subject_id"] for e in batch["envelopes"]} & set(batch["scope"]["scope"])
    assert scoped

    for envelope in batch["envelopes"]:
        if envelope["subject_id"] not in scoped:
            continue
        rows = envelope["inventory_rows"]
        assert rows, envelope["subject_id"]
        for row in rows:
            assert row["row_id"] and row["text"].strip()
        for hit in envelope["retrieved"]:
            assert hit["reference_set"] in ("R4", "R5")
            if hit["reference_set"] == "R4":
                assert hit["text"]


def test_the_flagged_record_is_handed_the_row_that_made_its_judgment_mandatory(store, screened):
    """A KNOWN-MECHANIC flag routes an idea to a mandatory pairwise
    judgment; the row that caused it used to be dropped on the way, leaving
    the judge to decide "is this a known mechanic" without the mechanic."""
    flagged_id = next(i for i, d in screened["dossiers"].items() if d["known_mechanic"])
    row_id = screened["dossiers"][flagged_id]["known_mechanic"]["row_id"]

    batch = _batch(store, screened)
    assert flagged_id in batch["scope"]["flagged"]
    envelope = next(e for e in batch["envelopes"] if e["subject_id"] == flagged_id)
    assert row_id in {r["row_id"] for r in envelope["inventory_rows"]}

    # ...and the fact that it was FLAGGED is not in the envelope: a judge
    # told the answer is not being asked the question.
    blob = json.dumps(envelope)
    assert "known_mechanic" not in blob
    assert "flagged" not in blob


def test_the_envelope_still_withholds_everything_it_withheld_before(store, screened):
    batch = _batch(store, screened)
    for envelope in batch["envelopes"]:
        blob = json.dumps(envelope)
        for field in WITHHELD_FROM_JUDGE:
            assert f'"{field}"' not in blob


# ---------------------------------------------------------------------------
# the plants: countable, mandatory, and not identifiable
# ---------------------------------------------------------------------------


def _plant_ids(batch) -> set[str]:
    return {p["plant_id"] for p in batch["plants"]}


def test_a_batch_reports_how_many_inventory_plants_it_carries(store, screened):
    batch = _batch(store, screened)
    assert batch["n_inventory_plants"] == 5
    assert batch["n_paraphrase_plants"] == 5

    labels = _label_everything(batch)
    scored = score_plants(batch, labels)
    assert scored["n_inventory_plants"] == 5
    assert scored["unauditable"] is False


def test_a_batch_with_no_inventory_plants_is_refused_rather_than_shipped_unauditable(store, screened):
    """Without an inventory, every plant is a paraphrase plant, no miss can
    fail the batch, and the ideas consolidate on the word of a judge nothing
    checked: catch_rate 0.0, inventory_failures [], batch_failed False."""
    ids = screened["inventory_chunk_ids"]
    ph = ",".join("?" for _ in ids)
    with store.knowledge:  # ingested, but not embedded by the time the judge runs
        store.knowledge.execute(
            f"DELETE FROM emb WHERE chunk_sha256 IN (SELECT sha256 FROM chunk WHERE chunk_id IN ({ph}))", ids
        )
        store.knowledge.execute(
            f"DELETE FROM {vec_table_name(screened['model_key'])} WHERE chunk_id IN ({ph})", ids
        )

    with pytest.raises(NoveltyError) as excinfo:
        _batch(store, screened)
    assert "no inventory plants" in str(excinfo.value)


def test_a_hand_assembled_batch_with_no_inventory_plants_scores_as_unauditable(store, screened):
    """build_judged_batch refuses to build one, so this only fires for a
    batch assembled elsewhere — but the shape that cannot fail the audit
    must not be the shape that passes it silently."""
    batch = _batch(store, screened)
    batch["plants"] = [p for p in batch["plants"] if p["kind"] != "inventory"]
    scored = score_plants(batch, _label_everything(batch))
    assert scored["n_inventory_plants"] == 0
    assert scored["unauditable"] is True
    assert scored["batch_failed"] is True


def test_no_surface_rule_recovers_the_plants_from_a_shuffled_batch(store, screened):
    """The reproduction: inventory plants always opened "Proposal: " and
    paraphrase plants "Restated: ", so a two-prefix rule recovered 10/10.
    Three further tells went with it — a plant's home was a bare family
    where a real record's is family/cell, and its `retrieved` and
    `self_assessment_removed` were always empty."""
    batch = _batch(store, screened)
    plants = _plant_ids(batch)
    envelopes = batch["envelopes"]

    statements = [e["record"]["statement"] for e in envelopes]
    prefixes = {s.split(":")[0] for s in statements if ":" in s.split(" ", 2)[0]}
    assert not prefixes, prefixes

    real = [e for e in envelopes if e["subject_id"] not in plants]
    planted = [e for e in envelopes if e["subject_id"] in plants]
    assert real and planted

    # every field a judge could key on takes the same shape in both groups
    # a plant wears a real record's home cell -- from anywhere in the batch,
    # not only from the judged scope
    real_homes = {d["home"] for d in screened["dossiers"].values()}
    assert {e["record"]["home_mechanic"] for e in planted} <= real_homes
    assert all("/" in (e["record"]["home_mechanic"] or "") for e in planted)
    assert all(e["inventory_rows"] for e in planted)
    assert all(e["record"]["provenance_docs"] for e in planted)
    assert all(e["record"]["requirements"] for e in planted)
    assert all(e["record"]["probe"] for e in planted)

    # Both groups go through one envelope builder, so there is no key a rule
    # could key on either.
    assert {frozenset(e) for e in planted} == {frozenset(e) for e in real}
    assert {frozenset(e["record"]) for e in planted} == {frozenset(e["record"]) for e in real}


# ---------------------------------------------------------------------------
# the judge never sees a plant id
# ---------------------------------------------------------------------------


def test_no_judge_view_carries_a_plant_id_or_a_record_id(store, screened):
    """The leak: an envelope's subject_id is PLANT-inventory-0, so any
    prompt builder that copies the envelope hands the judge the answer
    key."""
    batch = _batch(store, screened)
    blob = json.dumps(batch["judge_views"])
    assert "PLANT-" not in blob
    assert all(v["subject_id"].startswith("J-") for v in batch["judge_views"])
    assert len(batch["judge_views"]) == len(batch["envelopes"])
    assert len({v["subject_id"] for v in batch["judge_views"]}) == len(batch["envelopes"])


def test_a_judge_view_differs_from_its_envelope_in_exactly_one_field(store, screened):
    batch = _batch(store, screened)
    by_real = {e["subject_id"]: e for e in batch["envelopes"]}
    for view in batch["judge_views"]:
        real = by_real[batch["mask"][view["subject_id"]]]
        assert set(view) == set(real)
        assert {k: v for k, v in view.items() if k != "subject_id"} == {
            k: v for k, v in real.items() if k != "subject_id"
        }


def test_the_mask_covers_every_envelope_and_is_seeded(store, screened):
    first = _batch(store, screened)
    second = _batch(store, screened)
    assert set(first["mask"].values()) == {e["subject_id"] for e in first["envelopes"]}
    assert first["mask"] == second["mask"]
    # not merely positional: the numbering is a seeded permutation
    assert [v["subject_id"] for v in first["judge_views"]] != [
        f"J-{i}" for i in range(len(first["envelopes"]))
    ]


def test_masked_labels_are_mapped_back_and_score_identically(store, screened):
    batch = _batch(store, screened)
    real_labels = _label_everything(batch)
    inverse = {real: masked for masked, real in batch["mask"].items()}
    masked_labels = {inverse[subject]: value for subject, value in real_labels.items()}

    from_masked = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=masked_labels,
        issued_by_launch=screened["launches"]["lens-1"],
    )
    from_real = record_novelty_verdicts(
        store, round_id=screened["round_id"], batch=batch, labels=real_labels,
        issued_by_launch=screened["launches"]["lens-1"], supersede=True,
    )
    assert from_masked["plants"] == from_real["plants"]
    assert from_masked["n_verdicts"] == from_real["n_verdicts"]
    assert from_masked["status"] == from_real["status"] == "screened"


def test_a_masked_label_for_a_subject_outside_the_batch_is_still_refused(store, screened):
    batch = _batch(store, screened)
    labels = _label_everything(batch)
    labels["J-99999"] = {"label_inventory": "same"}
    with pytest.raises(NoveltyError) as excinfo:
        record_novelty_verdicts(
            store, round_id=screened["round_id"], batch=batch, labels=labels,
            issued_by_launch=screened["launches"]["lens-1"],
        )
    assert "J-99999" in str(excinfo.value)


def test_build_judge_views_masks_a_bare_envelope_list():
    views, mask = build_judge_views(
        [{"subject_id": "PLANT-inventory-0", "record": {}}, {"subject_id": "IDEA-1", "record": {}}],
        seed="seed-mask",
    )
    assert sorted(mask.values()) == ["IDEA-1", "PLANT-inventory-0"]
    assert all(v["subject_id"].startswith("J-") for v in views)


def test_a_plant_bundle_is_retrieved_the_same_way_a_records_is(store, screened):
    """`retrieved` was always [] for a plant and populated for a flagged
    record — on its own enough to sort the batch."""
    plants = build_plants(
        store, dossiers=screened["dossiers"], seed=SEED, candidate_hit_similarity=-1.0
    )
    assert plants
    for plant in plants:
        assert plant["retrieved"]
        assert all(h["reference_set"] == "R4" and h["text"] for h in plant["retrieved"])
        assert plant["inventory_rows"]


def test_plants_stay_reproducible_under_the_seed_and_stay_shuffled(store, screened):
    first = _batch(store, screened)
    second = _batch(store, screened)
    assert [p["record"]["statement"] for p in first["plants"]] == [
        p["record"]["statement"] for p in second["plants"]
    ]
    assert [e["subject_id"] for e in first["envelopes"]] == [e["subject_id"] for e in second["envelopes"]]

    order = [e["subject_id"] for e in first["envelopes"]]
    plants = _plant_ids(first)
    positions = [i for i, sid in enumerate(order) if sid in plants]
    assert positions != list(range(len(order) - len(plants), len(order)))


def test_an_inventory_plant_is_handed_the_row_it_was_cut_from(store, screened):
    """The plant's own bundle contains its source row, exactly as a flagged
    record's does — which is what makes `same`/`variant` the answer a judge
    doing the task returns."""
    batch = _batch(store, screened)
    for plant in batch["plants"]:
        if plant["kind"] != "inventory":
            continue
        envelope = next(e for e in batch["envelopes"] if e["subject_id"] == plant["plant_id"])
        assert plant["source_ref"] in {r["row_id"] for r in envelope["inventory_rows"]}
