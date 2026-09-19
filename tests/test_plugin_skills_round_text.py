"""The four skill files the ideation round's procedure lives in carry its
instructions — and carry nothing else.

Same posture as ``tests/test_plugin_agents_ideation_text.py``: these are
prompts, not documentation, so what is in the file IS what the agent is told.
This suite pins the load-bearing instructions (the ones whose absence would
silently change what a round runs, what a judge sees, or what a number means)
and pins the rule that these files carry no developer commentary.

Substrings, not whole paragraphs: the prose stays editable, the instruction
does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "skills"
SKILLS = ("ideation-round", "gate-critic", "lit-review", "verify-hypothesis")


def _text(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _flat(name: str) -> str:
    """The file with its line wrapping collapsed, so a phrase assertion fails
    on a meaning change rather than on a reflow."""
    return " ".join(_text(name).split())


# ---------------------------------------------------------------------------
# instructions only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SKILLS)
def test_skill_files_carry_no_developer_notes(name):
    text = _text(name)
    for marker in ("TRIALERROR-DEV-NOTE", "DEV-NOTE", "TODO", "FIXME", "stage A", "stage B", "stage C"):
        assert marker not in text, f"{name}: skill files carry instructions only, found {marker!r}"


@pytest.mark.parametrize("name", SKILLS)
def test_every_skill_declares_a_name_and_a_description(name):
    header = _text(name).split("\n---\n", 1)[0]
    assert header.lstrip().startswith("---"), f"{name}: no frontmatter block"
    assert f"name: {name}" in header
    assert "description:" in header


# ---------------------------------------------------------------------------
# ideation-round: the nine phases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "heading",
    [
        "## Phase 0 — FRAME",
        "## Phase 1 — SLICE",
        "## Phase 2 — DIVERGE",
        "## Phase 3 — NOVELTY SCREEN",
        "## Phase 4 — ROTATE",
        "## Phase 5 — CONVERGE",
        "## Phase 6 — CRITIC GATE",
        "## Phase 7 — ADOPT / KILL / LOG",
        "## Phase 8 — RETRO",
    ],
)
def test_the_round_runs_in_named_phases(heading):
    assert heading in _text("ideation-round"), f"ideation-round has no {heading!r} section"


def test_phase_zero_frames_escrows_and_floors_the_round():
    flat = _flat("ideation-round")
    assert "trialerror prereg commit" in flat
    assert "Never back-fill a prereg" in flat
    assert "`[models]` table" in flat and "`consolidation` and `screen` at `mid`" in flat
    assert "`DEFERRED`" in flat and "idle beats shallow" in flat


def test_phase_zero_escrows_the_admission_rule_and_seed_but_not_the_order():
    """The order is a draw over the CONSOLIDATED pool, which does not exist
    until Phase 3b has run -- so a hash in the Phase 0 params is either
    invented or back-filled, and back-filling is the one thing the procedure
    forbids."""
    flat = _flat("ideation-round")
    assert '"room_admission":"seeded-stratified-all"' in flat
    assert '"admission_seed":"<seed>"' in flat
    assert '"admission_order_hash":"<hash>"' not in flat
    assert "never the admission order's `hash`" in flat


def test_phase_five_escrows_the_order_in_its_own_commit():
    flat = _flat("ideation-round")
    assert "Escrow the order in its own second commit" in flat
    assert "after Phase 3b, before the first room opens" in flat
    assert "`admission_escrow`" in flat
    assert "derivative closes the pool before the order is escrowed" in flat


def test_the_critic_assembles_the_admission_escrow_into_the_subject():
    flat = _flat("gate-critic")
    assert "`admission_escrow` that order was committed under" in flat


def test_the_per_lens_mix_sentence_is_corrected():
    """The old text called the per-lens 40/40/20 split "the AMENDMENT-3
    defaults". The roster-level split is what the amendment specifies, and
    `--arm-per-lens` is what implements it; the per-lens mix is a declarable
    interim, not the default."""
    flat = _flat("ideation-round")
    assert "the AMENDMENT-3 defaults" not in flat
    assert "split the **roster** across the arms" in flat
    assert "roster 6 → 3 near / 1 moderate / 2 far" in flat
    assert "not** this protocol's" in flat
    assert "`tier=mixed`" in flat


def test_the_control_seat_is_excluded_from_the_arm_mix_and_the_far_floor():
    flat = _flat("ideation-round")
    assert "excluded from the arm mix and from the far floor" in flat
    assert "buster is pre-placed in the far arm and counts toward that floor" in flat
    assert "control lands in the modal arm and counts toward neither" in flat
    assert "no card and no `requirements` field" in flat


def test_card_blocks_the_slice_cap_and_the_exclusion_list_are_instructed():
    flat = _flat("ideation-round")
    assert "--recipe-card" in flat and "seeded order" in flat
    assert "5 documents, cap 6" in flat
    assert "abstract exclusion list" in flat
    assert "covered cluster ids, home cells and register families" in flat


def test_the_screen_is_split_into_its_two_halves():
    flat = _flat("ideation-round")
    assert "3a, mechanical" in flat and "3b, judged" in flat
    assert "seeded 20% sample" in flat
    assert "`no-close-neighbour`" in flat and "is **not** `new-mechanism`" in flat
    assert "no dossier field gates or orders room admission" in flat


def test_the_judged_half_masks_the_ids_and_hashes_the_procedure_from_a_file():
    """Fix pass N-6. The skill is the procedure an orchestrator actually runs
    a round from, and its Phase 3b block still printed the shell-stripped
    ``--executed-procedure "<what ran>"`` form that stamps
    ``prereg_compliant=false`` for a procedure that was followed, with no
    mention of the masked judge views."""
    flat = _flat("ideation-round")
    assert "--executed-procedure-file" in flat
    assert '--executed-procedure "<what ran>"' not in flat
    assert "--judge-envelopes-out" in flat
    assert "J-<n>" in flat


def test_phase_two_teaches_the_intake_verb_and_the_lens_launch_link():
    flat = _flat("ideation-round")
    assert "trialerror lens intake" in flat
    assert "provenance" in flat and "docs: [doc_id" in flat
    assert "--assign-id" in flat
    assert "lens_log_reconciled" in flat


def test_phase_four_names_its_three_triggers_and_nothing_else():
    flat = _flat("ideation-round")
    assert "Three triggers, and no others" in flat
    assert "k=3 consolidated records" in flat and "ONE envelope" in flat
    assert "`collapse_rerun`" in flat
    assert "At most one re-run per round" in flat


def test_phase_five_carries_the_admission_order_and_every_room_flag():
    flat = _flat("ideation-round")
    assert "trialerror room admission-order" in flat
    assert "seeded draw stratified on (arm, card)" in flat
    assert "never gate or order admission" in flat
    for flag in ("--rank-all", "--blind-first-turn", "--buster"):
        assert flag in flat, f"ideation-round does not instruct {flag}"
    assert "`position`, `question`, `closure`" in flat
    assert "own first round on a point is refused" in flat
    assert "re-injected verbatim" in flat
    assert "dossier's **labels** only" in flat
    assert "`--require-extracts`" in flat
    assert "computed from the structured stances" in flat
    assert ">90% bar" in flat
    assert "checked by LENS NAME" in flat


def test_phase_five_says_what_a_pool_smaller_than_one_room_does():
    """Round 0's two-idea dry run seats nobody at the charter room size, and
    neither the skill nor the guide said which flag makes it a sitting."""
    flat = _flat("ideation-round")
    assert "smaller than one room seats nobody" in flat
    assert "`--ideas-per-room 2`" in flat


def test_phase_six_reveals_and_refuses_to_re_describe():
    flat = _flat("ideation-round")
    assert "`aiif_round` suite" in flat
    assert "reveal the pre-registration" in flat
    assert "reported as non-compliant" in flat and "never re-described" in flat
    assert "regularities note" in flat and "never become a selection rule" in flat


def test_the_stop_rules_and_the_collapse_contingency_are_stated():
    flat = _flat("ideation-round")
    assert "400 pre-dedupe records" in flat
    assert "under 20% **mechanically-new**" in flat
    assert "judged \"new-mechanism\" rate is **not** a stopping rule" in flat
    assert "Floor of 100 post-dedupe records before rooms" in flat
    assert "descriptive only" in flat
    assert "round_collapse_acknowledged" in flat
    assert "closes as PARTIAL" in flat and "in their pre-registered order" in flat


def test_the_bounded_planner_block_and_the_pre_mortem_survive():
    text = _text("ideation-round")
    flat = _flat("ideation-round")
    assert "## Round plan: bounded but locally complete" in text
    assert "Priorities (at most three)" in flat
    assert "Preservation gate" in flat and "Acceptance gate" in flat
    assert "## Pre-mortem for every judged step of a round — REQUIRED" in text
    for question in ("Is the proxy itself gameable?", "Is the harness escapable?", "Can the judge be steered"):
        assert question in flat
    assert "round_premortem" in flat


def test_per_cell_n_is_disclosed_and_significance_language_is_refused():
    flat = _flat("ideation-round")
    assert "per-cell n disclosed and no significance language" in flat
    assert "every comparison is directional" in flat


# ---------------------------------------------------------------------------
# gate-critic: the suite in tier 1, the pre-mortem in tier 2
# ---------------------------------------------------------------------------


def test_tier_one_runs_the_round_suite():
    flat = _flat("gate-critic")
    assert "trialerror eval gate" in flat and "--suite aiif_round" in flat
    assert "fails closed on a missing section" in flat
    assert "reproduction_status = mismatch" in flat
    assert "Fix the round, not the subject file" in flat


def test_tier_two_carries_the_pre_mortem_into_the_critics_brief():
    flat = _flat("gate-critic")
    assert "critic's brief carries this gate's own pre-mortem" in flat
    assert "reading for polish" in flat
    assert "`round_premortem` answers travel into the brief" in flat


def test_the_critic_stays_read_only():
    flat = _flat("gate-critic")
    assert "VALIDATION ONLY" in flat
    assert "tool-locked to `[Read]` only" in flat


# ---------------------------------------------------------------------------
# lit-review: two new modes
# ---------------------------------------------------------------------------


def test_the_prior_art_bundle_mode_drafts_nothing():
    text = _text("lit-review")
    flat = _flat("lit-review")
    assert "## Mode: prior-art bundle" in text
    assert "Draft no answer" in flat
    assert "40/40/20 with a far floor of 2" in flat
    assert "Keep the arms" in flat and "Nothing in the far arm" in flat
    assert "never substitutes for one" in flat or "never substitute for one" in flat


def test_the_bounded_hunt_mode_fixes_and_logs_its_query_list():
    text = _text("lit-review")
    flat = _flat("lit-review")
    assert "## Mode: bounded hunt" in text
    assert "The list is fixed before the first query runs" in flat
    assert "including the ones that returned nothing" in flat
    assert "literature topic, never content from a record" in flat
    assert "corroborated" in flat and "still a probe" in flat
    assert "trialerror lit search --query" in flat


def test_the_fence_still_binds_in_both_modes():
    flat = _flat("lit-review")
    assert "≤20 words for a `commercial_restricted` source" in flat
    assert "never invoke it on the user's behalf" in flat


# ---------------------------------------------------------------------------
# verify-hypothesis: the pairwise envelope and its procedure version
# ---------------------------------------------------------------------------


def test_the_pairwise_envelope_and_its_procedure_version_are_named():
    flat = _flat("verify-hypothesis")
    assert "PAIRWISE-LABEL envelope" in flat
    assert "`procedure=custom`, `procedure_version=novelty-v2`" in flat
    assert "retrieved rows themselves" in flat


def test_both_label_vocabularies_are_enumerated_with_no_others():
    flat = _flat("verify-hypothesis")
    for label in ("same", "variant", "recombination", "new-mechanism", "unscreenable"):
        assert f"`{label}`" in flat, f"verify-hypothesis does not name the {label!r} label"
    for label in ("stated", "implied", "adjacent", "absent"):
        assert f"`{label}`" in flat, f"verify-hypothesis does not name the {label!r} label"
    assert "No others, and no number" in flat


def test_the_withheld_fields_and_the_one_submission_rule_are_stated():
    flat = _flat("verify-hypothesis")
    for field in ("author_rationale", "surprise", "assumed_circle"):
        assert f"`{field}`" in flat
    assert "Self-assessment sentences are stripped" in flat
    assert "One submission per record per judge" in flat
    assert "`parent_ids`" in flat
    assert "prereg_compliant` recomputed" in flat
