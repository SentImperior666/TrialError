"""The ``aiif_round`` gate suite: each of its fourteen checks green and red
on its own, plus two real pytest-subprocess runs of the whole suite — one
over a complete round, one over a round missing its prereg and its models
table (the acceptance criterion the design names: "the suite fails on a
fixture round lacking a prereg or a `[models]` table").

No store is opened here: the suite reads an assembled subject dict, which is
the boundary ``trialerror.eval`` states for itself.
"""

from __future__ import annotations

import copy

import pytest

from trialerror.eval.gate_suites import (
    AIIF_MODEL_FLOORS,
    AIIF_ROUND_SUITE_ID,
    IDEA_DISPOSITIONS,
    MIN_LENS_CELLS_PER_CARD,
    admission_order_hash_matches,
    arm_mode_declared,
    card_cells_ge_2,
    consolidation_completeness_over_ideas,
    control_arm_present,
    distribution_card_present,
    get_suite,
    lens_log_reconciled,
    list_suites,
    models_table_present,
    no_significance_language,
    novelty_bundle_complete,
    per_arm_n_disclosed,
    plants_caught,
    prereg_present,
    run_gate_suite,
    self_assessment_absent,
)

_ORDER_HASH = "a" * 64


def _subject() -> dict:
    """A round that passes every check. Each test below breaks exactly one
    thing in a deep copy of it, so a test that fails says which bar it
    broke rather than which fixture it mis-assembled."""
    return {
        "prereg": {
            "prereg_id": "PREREG-0001",
            "status": "committed",
            "params": {
                "arm_mode": "per_lens",
                # The admission ORDER cannot be escrowed at Phase 0 (nothing
                # is consolidated yet), so Phase 0 escrows the rule and the
                # seed and the order goes in its own post-screen escrow below.
                "room_admission": "seeded-stratified-all",
                "admission_seed": "seed-1",
            },
        },
        "models_table": dict(AIIF_MODEL_FLOORS),
        "ideas": [
            {
                "idea_id": f"IDEA-{i}",
                "status": "consolidated",
                "dossier": {"label_inventory": "no-close-neighbour", "label_corpus": "absent", "judged": False},
            }
            for i in range(3)
        ]
        + [{"idea_id": "IDEA-merged", "status": "merged", "dossier": {}}],
        "reference_snapshot": {
            "R1": {"sha256": "1" * 64},
            "R2": {"sha256": "2" * 64},
            "R3": {"sha256": "3" * 64},
            "R4": {"sha256": "4" * 64},
            "R5": {"snapshot_id": "index-build-9"},
        },
        "judge_envelopes": [
            {"subject_id": "IDEA-0", "record": {"requirements": "two lines", "statement": "a state transition", "probe": "check row X"}}
        ],
        "plants": {
            "n_plants": 10, "n_inventory_plants": 5, "n_paraphrase_plants": 5,
            "caught": ["PL-1"], "missed": [], "unlabelled": [], "catch_rate": 1.0,
            "inventory_failures": [], "unauditable": False, "batch_failed": False,
        },
        "distribution": {
            "declared_operations": {
                "opportunity": {"entropy": 0.82, "counts": {}},
                "method": {"entropy": 0.71, "counts": {}},
            },
            "pairwise_similarity": {"median": 0.41, "p90": 0.66},
            "template_mass": {"template_share": 0.12},
        },
        "roster": [
            {"lens_name": "lens_1", "seat": "standard", "recipe_cards": ["MISMATCH", "TRANSFER"]},
            {"lens_name": "lens_2", "seat": "standard", "recipe_cards": ["TRANSFER", "MISMATCH"]},
            {"lens_name": "lens_3", "seat": "assumption_buster", "recipe_cards": ["NEGATE"]},
            {"lens_name": "lens_4", "seat": "control", "recipe_cards": []},
        ],
        "assignment": {"arm_mode": "per_lens", "far_lens_floor": 2},
        "admission_order": {"hash": _ORDER_HASH, "n_ideas": 3},
        "admission_escrow": {"prereg_id": "PREREG-0002", "status": "committed", "admission_order_hash": _ORDER_HASH},
        "lens_log": {"offenders": [], "n_lenses": 4},
        "outcomes": [
            {"cell": "arm=near", "arm": "near", "n": 3},
            {"cell": "arm=far", "arm": "far", "n": 2},
            {"cell": "control", "seat": "control", "n": 1},
        ],
        "report_text": "The far arm survived more rooms than the near arm, directionally, at n=2 and n=3.",
        "findings": [],
    }


def _broken(**sections) -> dict:
    subject = _subject()
    for key, value in sections.items():
        if value is _DELETE:
            subject.pop(key, None)
        else:
            subject[key] = value
    return subject


class _Delete:
    pass


_DELETE = _Delete()


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_the_suite_registers_the_fourteen_named_checks():
    checks = list_suites()[AIIF_ROUND_SUITE_ID]
    assert set(checks) == {
        "prereg_present", "models_table_present", "novelty_bundle_complete", "self_assessment_absent",
        "plants_caught", "distribution_card_present", "control_arm_present", "arm_mode_declared",
        "card_cells_ge_2", "admission_order_hash_matches", "lens_log_reconciled", "per_arm_n_disclosed",
        "no_significance_language", "consolidation_completeness",
    }
    assert len(checks) == 14


def test_every_check_passes_on_a_complete_round():
    suite = get_suite(AIIF_ROUND_SUITE_ID)
    subject = _subject()
    failures = {name: fn(subject).message for name, fn in suite.checks.items() if not fn(subject).passed}
    assert failures == {}


def test_every_check_fails_closed_on_an_empty_subject():
    """A round artifact carrying none of these sections has not met these
    bars; it is not exempt from them."""
    suite = get_suite(AIIF_ROUND_SUITE_ID)
    passing = [name for name, fn in suite.checks.items() if fn({}).passed]
    assert passing == []


# ---------------------------------------------------------------------------
# prereg_present
# ---------------------------------------------------------------------------


def test_prereg_present_green_and_red():
    assert prereg_present(_subject()).passed
    assert not prereg_present(_broken(prereg={})).passed
    assert "names no prereg_id" in prereg_present(_broken(prereg={})).message


def test_a_voided_prereg_is_not_a_prereg():
    result = prereg_present(_broken(prereg={"prereg_id": "PREREG-1", "status": "voided"}))
    assert not result.passed
    assert "voided escrow" in result.message


def test_a_revealed_prereg_still_gates():
    assert prereg_present(_broken(prereg={"prereg_id": "PREREG-1", "status": "revealed"})).passed


# ---------------------------------------------------------------------------
# models_table_present
# ---------------------------------------------------------------------------


def test_models_table_missing_entirely_is_refused():
    result = models_table_present(_broken(models_table={}))
    assert not result.passed
    assert "meets_minimum" in result.message


def test_a_purpose_below_its_floor_is_named():
    table = dict(AIIF_MODEL_FLOORS)
    table["ideation"] = "mid"
    result = models_table_present(_broken(models_table=table))
    assert not result.passed
    assert "ideation='mid'<top" in result.message


def test_a_missing_purpose_is_not_a_pass():
    table = dict(AIIF_MODEL_FLOORS)
    del table["novelty_judge"]
    result = models_table_present(_broken(models_table=table))
    assert not result.passed
    assert "novelty_judge" in result.message


def test_a_purpose_above_its_floor_passes():
    table = dict(AIIF_MODEL_FLOORS)
    table["screen"] = "top"
    assert models_table_present(_broken(models_table=table)).passed


# ---------------------------------------------------------------------------
# novelty_bundle_complete
# ---------------------------------------------------------------------------


def test_the_mechanical_label_pair_is_a_complete_bundle():
    """An unjudged idea's bundle is the mechanical pair. Requiring a judged
    label for every idea would require judging every idea, which the design
    explicitly does not."""
    assert novelty_bundle_complete(_subject()).passed


def test_a_consolidated_idea_with_no_dossier_fails_and_is_named():
    subject = _subject()
    subject["ideas"][1]["dossier"] = {}
    result = novelty_bundle_complete(subject)
    assert not result.passed
    assert "IDEA-1" in result.message
    assert result.score == pytest.approx(2 / 3)


def test_a_reference_set_with_no_snapshot_id_fails():
    subject = _subject()
    subject["reference_snapshot"]["R3"] = {}
    result = novelty_bundle_complete(subject)
    assert not result.passed
    assert "R3" in result.message


def test_a_round_with_nothing_consolidated_fails():
    subject = _subject()
    subject["ideas"] = [{"idea_id": "IDEA-raw", "status": "raw"}]
    assert not novelty_bundle_complete(subject).passed


# ---------------------------------------------------------------------------
# self_assessment_absent
# ---------------------------------------------------------------------------


def test_a_judge_envelope_grading_its_own_novelty_fails():
    subject = _subject()
    subject["judge_envelopes"][0]["record"]["statement"] = (
        "A state transition for upkeep. This is novel: unlike any existing system."
    )
    result = self_assessment_absent(subject)
    assert not result.passed
    assert "IDEA-0" in result.message


def test_the_check_runs_the_screens_own_stripper_not_a_restatement():
    """The paraphrase the stripper cannot catch passes here too — stated, so
    a green result is not read as more than it is."""
    subject = _subject()
    subject["judge_envelopes"][0]["record"]["statement"] = "No register row states this procedure."
    assert self_assessment_absent(subject).passed


def test_no_envelopes_at_all_fails_closed():
    assert not self_assessment_absent(_broken(judge_envelopes=[])).passed


# ---------------------------------------------------------------------------
# plants_caught
# ---------------------------------------------------------------------------


def test_a_missed_inventory_plant_fails_the_gate():
    subject = _subject()
    subject["plants"]["inventory_failures"] = ["PL-3"]
    subject["plants"]["batch_failed"] = True
    result = plants_caught(subject)
    assert not result.passed
    assert "PL-3" in result.message


def test_an_unauditable_batch_fails_on_that_ground_alone():
    subject = _subject()
    subject["plants"]["n_inventory_plants"] = 0
    subject["plants"]["unauditable"] = True
    result = plants_caught(subject)
    assert not result.passed
    assert "unauditable" in result.message


def test_a_missed_paraphrase_plant_is_reported_not_failed():
    """A miss on a kind the round does NOT fail on is counted in the message
    and passes. The message names the failing kinds rather than the word
    "inventory", because which kinds fail a batch is the round's own
    declaration (lane FB-5 item 3, `--batch-fail-on`)."""
    subject = _subject()
    subject["plants"]["missed"] = [{"plant_id": "PL-para", "label": "new-mechanism", "expected": ["variant"]}]
    result = plants_caught(subject)
    assert result.passed
    assert "1 plant(s) of other kinds missed" in result.message
    assert "inventory plant(s) was caught" in result.message


def test_the_check_turns_on_the_kinds_the_round_declared_it_fails_on():
    subject = _subject()
    subject["plants"] |= {
        "batch_fail_on": ["area"],
        "failures": ["PL-area-1"],
        "inventory_failures": [],
        "by_kind": {"area": {"n": 3, "fails_batch": True}, "inventory": {"n": 5, "fails_batch": False}},
    }
    result = plants_caught(subject)
    assert not result.passed
    assert "area plant(s) missed or unlabelled" in result.message


def test_a_score_written_before_batch_fail_on_existed_still_reads_correctly():
    """`failures` is absent from a pre-FB-5 score, and under the default rule
    `inventory_failures` IS that list -- so the fallback reads an older
    subject rather than passing it vacuously."""
    subject = _subject()
    subject["plants"]["inventory_failures"] = ["PL-inv-2"]
    subject["plants"].pop("failures", None)
    result = plants_caught(subject)
    assert not result.passed
    assert "PL-inv-2" in result.message


# ---------------------------------------------------------------------------
# distribution_card_present
# ---------------------------------------------------------------------------


def test_a_missing_axis_entropy_key_fails():
    subject = _subject()
    del subject["distribution"]["declared_operations"]["method"]
    result = distribution_card_present(subject)
    assert not result.passed
    assert "declared_operations.method.entropy" in result.message


def test_a_null_entropy_is_not_a_hole():
    """An entropy over one record has no value and the card says so; the key
    being absent is the hole, not the value being None."""
    subject = _subject()
    subject["distribution"]["declared_operations"]["method"]["entropy"] = None
    subject["distribution"]["pairwise_similarity"]["median"] = None
    assert distribution_card_present(subject).passed


def test_a_missing_template_mass_fails():
    subject = _subject()
    del subject["distribution"]["template_mass"]
    assert not distribution_card_present(subject).passed


# ---------------------------------------------------------------------------
# control_arm_present
# ---------------------------------------------------------------------------


def test_a_roster_with_no_control_seat_fails_and_says_where_to_look():
    subject = _subject()
    subject["roster"] = [r for r in subject["roster"] if r["seat"] != "control"]
    result = control_arm_present(subject)
    assert not result.passed
    assert "excluded from the arm mix and the far floor" in result.message


def test_the_passing_message_states_the_exclusion_too():
    """AMENDMENT-5 item 1: the suite's own message must say the same thing
    the assignment code and the skill text say — CONTROL never counts toward
    the arm mix or the far floor."""
    result = control_arm_present(_subject())
    assert result.passed
    assert "excluded from the arm mix" in result.message
    assert "from the far floor" in result.message


def test_a_control_lens_holding_a_card_is_not_a_control():
    subject = _subject()
    subject["roster"][3]["recipe_cards"] = ["MISMATCH"]
    result = control_arm_present(subject)
    assert not result.passed
    assert "is not a control" in result.message


def test_two_control_seats_are_refused():
    subject = _subject()
    subject["roster"].append({"lens_name": "lens_5", "seat": "control", "recipe_cards": []})
    assert not control_arm_present(subject).passed


# ---------------------------------------------------------------------------
# arm_mode_declared
# ---------------------------------------------------------------------------


def test_an_undeclared_arm_mode_fails():
    assert not arm_mode_declared(_broken(assignment={})).passed


def test_a_mode_run_but_not_escrowed_fails():
    subject = _subject()
    del subject["prereg"]["params"]["arm_mode"]
    result = arm_mode_declared(subject)
    assert not result.passed
    assert "escrowed, not chosen afterwards" in result.message


def test_a_mode_that_differs_from_the_escrow_fails():
    subject = _subject()
    subject["assignment"]["arm_mode"] = "per_slice"
    result = arm_mode_declared(subject)
    assert not result.passed
    assert "pre-registered 'per_lens'" in result.message


def test_the_interim_mode_passes_when_it_is_the_one_escrowed():
    subject = _subject()
    subject["assignment"]["arm_mode"] = "per_slice"
    subject["prereg"]["params"]["arm_mode"] = "per_slice"
    assert arm_mode_declared(subject).passed


# ---------------------------------------------------------------------------
# card_cells_ge_2
# ---------------------------------------------------------------------------


def test_a_one_lens_card_cell_fails():
    subject = _subject()
    subject["roster"][1]["recipe_cards"] = ["TRANSFER", "INVERT"]
    result = card_cells_ge_2(subject)
    assert not result.passed
    assert "INVERT=1" in result.message


def test_the_busters_own_card_does_not_have_to_be_held_twice():
    """NEGATE comes with the seat and is held by one lens by design;
    counting it would fail a correctly assembled round."""
    result = card_cells_ge_2(_subject())
    assert result.passed
    assert "NEGATE" not in result.message


def test_a_round_where_no_standard_lens_holds_a_card_fails():
    subject = _subject()
    for row in subject["roster"]:
        if row["seat"] == "standard":
            row["recipe_cards"] = []
    assert not card_cells_ge_2(subject).passed
    assert MIN_LENS_CELLS_PER_CARD == 2


# ---------------------------------------------------------------------------
# admission_order_hash_matches
# ---------------------------------------------------------------------------


def test_a_reordered_admission_fails_against_its_escrow():
    subject = _subject()
    subject["admission_order"]["hash"] = "b" * 64
    result = admission_order_hash_matches(subject)
    assert not result.passed
    assert "other than the escrowed seeded draw" in result.message


def test_the_post_screen_escrow_is_read_and_named():
    """The ordinary shape: the rule and the seed at Phase 0, the ORDER in its
    own escrow committed after the judged screen and before the first room
    opens -- because the pool the order is drawn over does not exist until
    records are consolidated."""
    result = admission_order_hash_matches(_subject())
    assert result.passed
    assert "post-screen admission escrow" in result.message
    assert "PREREG-0002" in result.message


def test_a_hash_in_the_prereg_params_is_read_and_named_as_that():
    """The other legal shape: a round re-ordering a pool that was already
    closed had its order at Phase 0 and may escrow it there. The message says
    which escrow it read, because that says when the order was fixed."""
    subject = _subject()
    subject["prereg"]["params"]["admission_order_hash"] = _ORDER_HASH
    del subject["admission_escrow"]
    result = admission_order_hash_matches(subject)
    assert result.passed
    assert "prereg params" in result.message


def test_an_order_with_nothing_escrowed_against_it_fails():
    subject = _subject()
    del subject["admission_escrow"]
    result = admission_order_hash_matches(subject)
    assert not result.passed
    assert "no escrow carries an admission_order_hash" in result.message
    assert "prereg params" in result.message and "admission_escrow" in result.message


def test_a_voided_admission_escrow_is_not_one_a_round_may_gate_on():
    subject = _subject()
    subject["admission_escrow"]["status"] = "voided"
    result = admission_order_hash_matches(subject)
    assert not result.passed
    assert "'voided'" in result.message


def test_an_unrecorded_order_fails():
    assert not admission_order_hash_matches(_broken(admission_order={})).passed


# ---------------------------------------------------------------------------
# lens_log_reconciled
# ---------------------------------------------------------------------------


def test_a_booked_lens_that_never_posted_fails():
    result = lens_log_reconciled(_broken(lens_log={"offenders": [{"lens_name": "lens_2"}], "n_lenses": 4}))
    assert not result.passed
    assert "lens_2" in result.message


def test_the_bare_row_shape_is_accepted_too():
    assert lens_log_reconciled(_broken(lens_log=[{"lens_name": "a", "posted": True}])).passed
    assert not lens_log_reconciled(_broken(lens_log=[{"lens_name": "a", "posted": False}])).passed


def test_an_absent_log_fails_closed():
    assert not lens_log_reconciled(_broken(lens_log=_DELETE)).passed


# ---------------------------------------------------------------------------
# per_arm_n_disclosed
# ---------------------------------------------------------------------------


def test_a_cell_without_its_n_fails():
    subject = _subject()
    del subject["outcomes"][0]["n"]
    result = per_arm_n_disclosed(subject)
    assert not result.passed
    assert "arm=near" in result.message


def test_a_control_cell_reported_inside_the_arm_mix_fails():
    """AMENDMENT-5 item 1 again, on the reporting side: the control's n is
    disclosed, but never as an arm."""
    subject = _subject()
    subject["outcomes"][2]["arm"] = "moderate"
    result = per_arm_n_disclosed(subject)
    assert not result.passed
    assert "inside the arm mix" in result.message


def test_a_zero_n_cell_is_disclosed_not_omitted():
    subject = _subject()
    subject["outcomes"][1]["n"] = 0
    assert per_arm_n_disclosed(subject).passed


def test_a_round_that_never_reports_the_control_cell_fails():
    """The adoption rule is "AIIF arm >= CONTROL on O1", so an analysis with
    no control cell has not reported the comparison it rests on. This passed
    before, against the check's own docstring."""
    subject = _subject()
    subject["outcomes"] = [row for row in subject["outcomes"] if row.get("seat") != "control"]
    result = per_arm_n_disclosed(subject)
    assert not result.passed
    assert "no cell carries seat='control'" in result.message


def test_a_cell_naming_the_control_with_no_seat_key_is_undisclosed_not_an_arm():
    """Naming the control in the cell TEXT is how a missing seat key slipped
    through: the row read as an ordinary arm cell, and the control counted as
    reported."""
    subject = _subject()
    subject["outcomes"] = [
        {"cell": "arm=near", "arm": "near", "n": 3},
        {"cell": "control (matched budget)", "n": 1},
    ]
    result = per_arm_n_disclosed(subject)
    assert not result.passed
    assert "no seat key" in result.message
    assert "control (matched budget)" in result.message


# ---------------------------------------------------------------------------
# no_significance_language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The far arm was significantly better.",
        "This result reaches statistical significance.",
        "The difference held (p < .05).",
        "We report a 95% confidence interval.",
        "We reject the null hypothesis.",
        "p=0.03 across the judged subset.",
    ],
)
def test_significance_language_fails(text):
    assert not no_significance_language(_broken(report_text=text)).passed


@pytest.mark.parametrize(
    "text",
    [
        "The far arm survived more rooms, directionally, at n=2.",
        "Per-cell n is single digits, so every comparison is a direction.",
        "A significand is not the word we are looking for.",
    ],
)
def test_directional_prose_passes(text):
    assert no_significance_language(_broken(report_text=text)).passed


def test_absent_prose_fails_closed():
    assert not no_significance_language(_broken(report_text=_DELETE)).passed


# ---------------------------------------------------------------------------
# consolidation_completeness, reused over idea rows
# ---------------------------------------------------------------------------


def test_every_raw_idea_needs_a_disposition():
    subject = _subject()
    subject["ideas"].append({"idea_id": "IDEA-raw", "status": "raw"})
    result = consolidation_completeness_over_ideas(subject)
    assert not result.passed
    assert "IDEA-raw" in result.message
    assert result.name == "consolidation_completeness"


def test_the_four_dispositions_are_the_idea_statuses_minus_raw():
    assert IDEA_DISPOSITIONS == {"CONSOLIDATED", "MERGED", "ELIMINATED", "PROMOTED"}
    subject = _subject()
    subject["ideas"] = [
        {"idea_id": "a", "status": "consolidated"}, {"idea_id": "b", "status": "merged"},
        {"idea_id": "c", "status": "eliminated"}, {"idea_id": "d", "status": "promoted"},
    ]
    assert consolidation_completeness_over_ideas(subject).passed


def test_a_round_with_no_ideas_at_all_fails_closed():
    assert not consolidation_completeness_over_ideas(_broken(ideas=[])).passed


# ---------------------------------------------------------------------------
# the suite as a real pytest subprocess
# ---------------------------------------------------------------------------


def test_the_whole_suite_runs_green_as_a_subprocess():
    result = run_gate_suite(AIIF_ROUND_SUITE_ID, _subject(), timeout=180.0)
    assert result["overall"] == "PASS", result["stdout_tail"]
    assert result["returncode"] == 0
    assert len(result["checks"]) == 14


def test_the_suite_fails_a_round_with_no_prereg_and_no_models_table():
    """The design's own acceptance criterion for this row, verbatim: "the
    suite fails on a fixture round lacking a prereg or a [models] table"."""
    subject = copy.deepcopy(_subject())
    subject["prereg"] = {}
    subject["models_table"] = {}
    result = run_gate_suite(AIIF_ROUND_SUITE_ID, subject, timeout=180.0)
    assert result["overall"] == "FAIL"
    assert result["returncode"] == 1
    failed = {c["name"] for c in result["checks"] if not c["passed"]}
    # the two named bars, plus the check that reads the prereg's own params
    assert {"prereg_present", "models_table_present"} <= failed
    assert "arm_mode_declared" in failed
    # admission_order_hash_matches reads its own post-screen escrow, so it
    # survives a wiped prereg -- and fails once that escrow goes too.
    assert "admission_order_hash_matches" not in failed
    subject.pop("admission_escrow")
    without_escrow = run_gate_suite(AIIF_ROUND_SUITE_ID, subject, timeout=180.0)
    assert "admission_order_hash_matches" in {c["name"] for c in without_escrow["checks"] if not c["passed"]}
