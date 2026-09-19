"""The three subagent prompt files carry the ideation round's own
instructions — and carry nothing else.

These are prompts, not documentation: what is in the file IS what the agent
is told. So this suite pins the load-bearing instructions (the ones whose
absence would silently change what a lens generates or what a judge sees)
and, just as importantly, pins the rule that these files contain no
developer commentary — a note explaining the code to a human reader is
context the model also reads, and it is not an instruction.

Substrings, not whole paragraphs: the prose is meant to stay editable; the
instruction is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "agents"


def _text(stem: str) -> str:
    return (AGENTS_DIR / f"{stem}.md").read_text(encoding="utf-8")


def _flat(stem: str) -> str:
    """The same file with its line wrapping collapsed to single spaces.
    These are prose prompts that get re-wrapped whenever anyone edits them,
    so a phrase assertion written against the raw text would fail on a
    reflow rather than on a meaning change. Structure (headings) is matched
    against :func:`_text`; phrases against this."""
    return " ".join(_text(stem).split())


@pytest.mark.parametrize("stem", ["critic", "verifier", "lens"])
def test_prompt_files_carry_no_developer_notes(stem):
    text = _text(stem)
    for marker in ("TRIALERROR-DEV-NOTE", "DEV-NOTE", "TODO", "FIXME"):
        assert marker not in text, f"{stem}.md: prompt files carry instructions only, found {marker!r}"


@pytest.mark.parametrize("stem", ["critic", "verifier", "lens"])
def test_every_agent_declares_a_top_class_model(stem):
    from trialerror.budget.policy import class_rank, classify_model

    header = _text(stem).split("\n---\n", 1)[0]
    model = next(
        line.partition(":")[2].strip().strip("\"'")
        for line in header.splitlines()
        if line.startswith("model:")
    )
    assert class_rank(classify_model(model)) == class_rank("top"), (
        f"{stem}.md pins model {model!r}, which is not a top-class model - "
        "no research judgment runs below top"
    )


# ---------------------------------------------------------------------------
# lens: the card block, the record schema, the banned default, the barrier
# ---------------------------------------------------------------------------


def test_lens_has_a_card_block_slot_and_seat_specific_behaviour():
    assert "## Your card block" in _text("lens")
    flat = _flat("lens")
    assert "assumption_buster" in flat and "NEGATE" in flat
    assert "`control`" in flat and "no card" in flat


def test_lens_names_every_record_field_the_generator_writes():
    flat = _flat("lens")
    for field in (
        "requirements", "statement", "home_mechanic", "assumed_circle",
        "provenance", "operation_declared", "probe", "surprise", "author_rationale",
    ):
        assert f"`{field}`" in flat, f"lens.md does not name the {field!r} record field"


def test_lens_orders_requirements_before_the_statement():
    """A record-format rule, not an exhortation: requirements are written
    BEFORE the statement, and the file has to say so."""
    flat = _flat("lens")
    assert "BEFORE the statement" in flat
    assert flat.index("`requirements`") < flat.index("`statement`")


def test_lens_carries_the_banned_default():
    flat = _flat("lens")
    assert "Banned default" in flat
    assert "bottleneck" in flat
    assert "unify" in flat or "combine" in flat


def test_lens_states_that_its_brief_never_contains_dossiers_or_verdicts():
    flat = _flat("lens")
    assert "never contains dossiers" in flat
    assert "verdicts" in flat and "rubrics" in flat
    # And says what to DO about it, or the barrier is decoration.
    assert "stop" in flat.lower()


def test_lens_still_forbids_reading_outside_its_own_slice():
    flat = _flat("lens")
    assert "never a slice you pick for yourself" in flat
    assert "never another lens's slice" in flat


def test_lens_states_the_record_schema_the_intake_verb_enforces():
    """The fields are not documentation here -- `trialerror lens intake`
    refuses a record that omits them, so the prompt has to state exactly
    what the writer will refuse."""
    flat = _flat("lens")
    assert "`operation_declared`" in flat
    assert "`docs`" in flat and "doc_ids" in flat
    assert "opportunity:" in flat and "method:" in flat
    assert "lens intake" in flat
    assert "provenance.docs" in flat


def test_lens_requires_a_probe_on_every_record():
    flat = _flat("lens")
    assert "A record with no probe is not finished" in flat


# ---------------------------------------------------------------------------
# verifier: pairwise labels, with assumed_circle withheld
# ---------------------------------------------------------------------------


def test_verifier_has_a_pairwise_novelty_labelling_job():
    flat = _flat("verifier")
    assert "Pairwise novelty labelling" in flat
    assert "three jobs" in flat
    assert "discrete label" in flat
    assert "Never a score" in flat


def test_verifier_withholds_assumed_circle_and_the_authors_own_framing():
    flat = _flat("verifier")
    assert "deliberately NOT given" in flat
    withheld = flat.split("deliberately NOT given", 1)[1]
    for field in ("author_rationale", "surprise", "assumed_circle", "recipe card", "seat"):
        assert field in withheld, f"verifier.md does not withhold {field!r} from the judge envelope"


def test_verifier_names_both_label_vocabularies_the_contract_fixes():
    """Finding V-7. Jobs 1 and 2 defer their output shape to the calling
    skill; job 3 does not, so the labels have to be IN the file. The
    downstream ordinal arithmetic is keyed to exactly these two sets, and a
    launch free to invent its own vocabulary is a launch whose output that
    arithmetic cannot count."""
    flat = _flat("verifier")
    for label in ("same", "variant", "recombination", "new-mechanism", "unscreenable"):
        assert f"`{label}`" in flat, f"verifier.md job 3 does not name the inventory label {label!r}"
    for label in ("stated", "implied", "adjacent", "absent"):
        assert f"`{label}`" in flat, f"verifier.md job 3 does not name the retrieval label {label!r}"
    assert "no others" in flat


def test_verifier_says_no_close_neighbour_is_not_a_novelty_verdict():
    flat = _flat("verifier")
    assert 'is not "new mechanism"' in flat
    assert "unscreenable" in flat


# ---------------------------------------------------------------------------
# critic: the three pre-mortem questions
# ---------------------------------------------------------------------------


def test_critic_carries_all_three_pre_mortem_questions():
    flat = _flat("critic")
    assert "pre-mortem" in flat
    assert "proxy itself gameable" in flat
    assert "harness escapable" in flat
    assert "judge be steered" in flat


def test_critic_says_what_a_yes_costs():
    assert "named mitigation" in _flat("critic")


def test_critic_is_still_told_it_may_only_read():
    assert "VALIDATION ONLY" in _flat("critic")
