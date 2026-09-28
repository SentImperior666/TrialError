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


def _section(stem: str, heading: str) -> str:
    """One ``##`` section of a prompt file, flattened like :func:`_flat`.

    Some instructions are only doing their job WHERE they sit -- an
    exception stated three sections away from the rule it excepts is an
    exception a reader of the rule never finds. Those assertions are made
    against the section, not the file."""
    text = _text(stem)
    assert heading in text, f"{stem}.md has no {heading!r} section"
    return " ".join(text.split(heading, 1)[1].split("\n## ", 1)[0].split())


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


#: The three record-field bullets whose format a round's brief supplies. The
#: agent definition is every round's system prompt, so it names the fields
#: (they are the intake schema) and leaves their format to the brief: no
#: reference set, no cell format and no domain word of any one round.
_BRIEF_FORMATTED_BULLETS = {
    "home_mechanic": (
        "- `home_mechanic` — where the idea lands, written in the schema and format "
        "your brief gives for this field."
    ),
    "assumed_circle": (
        "- `assumed_circle` — what you are taking as given for the idea to make sense, "
        "written in the schema and format your brief gives for this field."
    ),
    "probe": (
        "- `probe` — 2 to 6 lines: what one would implement or simulate to test this, "
        "against the target your brief names for it, and which check would show "
        "whether the idea holds. A record with no probe is not finished."
    ),
}


def _bullet(field: str) -> str:
    """The flattened bullet that starts with ``field``, up to the next one."""
    section = _section("lens", "## What one record contains")
    start = section.index(f"- `{field}` —")
    end = section.find(" - `", start + 1)
    return section[start:] if end < 0 else section[start:end]


@pytest.mark.parametrize("field", sorted(_BRIEF_FORMATTED_BULLETS))
def test_lens_leaves_the_format_of_three_fields_to_the_brief(field):
    assert _bullet(field) == _BRIEF_FORMATTED_BULLETS[field]


@pytest.mark.parametrize("field", sorted(_BRIEF_FORMATTED_BULLETS))
def test_the_brief_formatted_bullets_name_no_reference_set(field):
    bullet = _bullet(field).lower()
    assert "inventory row" not in bullet
    for word in ("inventory", "register", "cell", "row"):
        assert word not in bullet, f"lens.md's {field} bullet names {word!r}"


# ---------------------------------------------------------------------------
# lens: the derivation phase, and the one hole it opens in the barrier
# ---------------------------------------------------------------------------
#
# A round that runs a derivation phase hands every seat a few records written
# by OTHER lenses. The barrier section forbids exactly that, so the phase and
# the barrier have to be written as one thing: the exception is declared, it
# is named AT the rule, and it lets through records only -- never a label, a
# verdict or a distance. A control seat that reads the barrier and refuses
# the phase is reading the file correctly, and the comparison the round
# exists to make is what that costs.

_DERIVATION_HEADING = "## The derivation phase"
_BARRIER_HEADING = "## What your brief never contains"
_ENVELOPE_MARKER = "DERIVATION ENVELOPE"


def test_lens_has_a_derivation_phase_section_before_the_barrier():
    text = _text("lens")
    assert _DERIVATION_HEADING in text
    assert text.index(_DERIVATION_HEADING) < text.index(_BARRIER_HEADING), (
        "the derivation phase has to be described before the rule it excepts"
    )


def test_lens_names_the_envelope_marker_in_the_phase_and_at_the_rule():
    """The marker is the contract with the orchestrator's renderer, and it
    is also the exception to the barrier -- so it has to appear in BOTH
    places. A lens that reads only the barrier section must still find it."""
    assert _ENVELOPE_MARKER in _section("lens", _DERIVATION_HEADING)
    assert _ENVELOPE_MARKER in _section("lens", _BARRIER_HEADING)


def test_lens_barrier_survives_the_derivation_exception():
    barrier = _section("lens", _BARRIER_HEADING)
    for banned in ("dossiers", "novelty labels", "verdicts", "rubrics"):
        assert banned in barrier, f"lens.md barrier no longer lists {banned!r}"
    assert "stop, write no records" in _flat("lens")


def test_lens_says_judgment_inside_the_envelope_still_trips_the_barrier():
    """The envelope is an exception for other lenses' RECORDS, and for
    nothing else. Without this clause the exception is a hole a dossier
    could be posted through."""
    assert "labels, verdicts, dossiers, distances or rubrics" in _section("lens", _DERIVATION_HEADING)


def test_lens_tells_the_control_seat_it_is_in_the_derivation_phase_too():
    """The control seat's whole value is the matched comparison; a control
    that sits the phase out makes the phase not evaluable."""
    derivation = _section("lens", _DERIVATION_HEADING)
    assert "control" in derivation
    assert "same envelope" in derivation
    assert "no card" in derivation


def test_lens_names_the_two_fields_a_derivative_record_carries():
    flat = _flat("lens")
    assert "`parent_ids`" in flat
    assert '"phase": "derivation"' in flat


# ---------------------------------------------------------------------------
# lens: the plain brief, keyed on a declared marker
# ---------------------------------------------------------------------------
#
# A paired round books every lens twice -- one launch under its card block,
# one under the plain brief -- and the plain launch sits a STANDARD seat. So
# the plain brief cannot key on the seat: it keys on a marker the round's
# renderer emits on the plain launches only, and the file has to say what a
# plain launch leaves out, or the "plain" half is written under most of the
# card block anyway and the comparison measures nothing.

_PLAIN_HEADING = "## The plain brief — only when your prompt declares it"
_PLAIN_MARKER = "BRIEF: PLAIN"


def _paragraph(stem: str, opening: str) -> str:
    """The blank-line-delimited paragraph (or list item) of a prompt file
    that starts with ``opening``, flattened like :func:`_flat`."""
    text = _text(stem)
    assert opening in text, f"{stem}.md has no paragraph opening {opening!r}"
    tail = text.split(opening, 1)[1]
    ends = [i for i in (tail.find("\n\n"), tail.find("\n- ")) if i >= 0]
    return " ".join((opening + tail[: min(ends) if ends else len(tail)]).split())


def test_lens_has_a_plain_brief_section_before_the_barrier():
    text = _text("lens")
    assert _PLAIN_HEADING in text
    assert text.index(_DERIVATION_HEADING) < text.index(_PLAIN_HEADING) < text.index(_BARRIER_HEADING)


def test_lens_names_the_plain_marker_exactly_and_never_misspelt():
    """The marker is the contract with the round's renderer: the section
    states it as a line read exactly, and no other spelling of it appears
    anywhere in the file for a renderer author to copy."""
    import re

    plain = _section("lens", _PLAIN_HEADING)
    assert f"`{_PLAIN_MARKER}`" in plain
    assert "reads exactly" in plain
    spellings = {m.group(0) for m in re.finditer(r"brief\s*:\s*plain", _text("lens"), re.IGNORECASE)}
    assert spellings == {_PLAIN_MARKER}


def test_the_plain_brief_names_each_thing_a_plain_launch_leaves_out():
    plain = _section("lens", _PLAIN_HEADING)
    assert "no card block" in plain
    assert "no record carries a card tag" in plain
    assert "`requirements` field" in plain
    assert "`operation_declared` field" in plain
    assert "the `card` key of `provenance`" in plain
    assert "whatever your seat" in plain


def test_every_field_the_plain_brief_leaves_out_is_one_intake_knows():
    """A field named here is a field a lens learns exists. Naming one the
    record schema does not have would teach a card-side lens to write it,
    and `lens intake` refuses a record carrying an unknown field."""
    import re

    from trialerror.lens.ideas import RECORD_FIELD_ALIASES

    plain = _section("lens", _PLAIN_HEADING)
    assert "Leave out of every record" in plain, "the plain brief no longer says what a plain record leaves out"
    sentence = plain.split("Leave out of every record", 1)[1].split(". ", 1)[0]
    named = [t for t in re.findall(r"`([^`]+)`", sentence) if re.fullmatch(r"[a-z_]+", t)]
    assert {"requirements", "operation_declared", "card", "provenance"} <= set(named)
    provenance_keys = {"docs", "card"}
    assert [t for t in named if t not in RECORD_FIELD_ALIASES and t not in provenance_keys] == []


def test_the_banned_default_names_the_plain_exception_where_it_is_stated():
    banned = _paragraph("lens", "**Banned default.**")
    assert f"`{_PLAIN_MARKER}`" in banned
    assert "does not apply" in banned
    assert "The banned default does not apply to you" in _section("lens", _PLAIN_HEADING)


def test_the_plain_brief_keeps_slice_discipline_every_other_field_and_the_stop_rule():
    plain = _section("lens", _PLAIN_HEADING)
    assert "Read only your own slice" in plain and "doc_id" in plain
    assert "Every other field of a record is still required" in plain
    for field in ("statement", "home_mechanic", "assumed_circle", "probe", "surprise", "author_rationale"):
        assert f"`{field}`" in plain, f"the plain brief no longer keeps {field!r}"
    assert "stop and write no records" in plain


def test_the_plain_brief_keeps_the_derivation_phase():
    plain = _section("lens", _PLAIN_HEADING)
    assert f"`{_ENVELOPE_MARKER}`" in plain
    assert "derivatives under the plain brief" in plain
    assert "A plain launch takes part in every phase its prompt declares" in _flat_text(plain)
    derivation = _section("lens", _DERIVATION_HEADING)
    assert f"`{_PLAIN_MARKER}`" in derivation and "same envelope" in derivation


def test_each_rule_keyed_on_the_control_seat_names_the_plain_marker_too():
    card_block = _section("lens", "## Your card block")
    assert "`control`" in card_block and f"`{_PLAIN_MARKER}`" in card_block
    requirements = _paragraph("lens", "- `requirements`")
    assert "`control`" in requirements and f"`{_PLAIN_MARKER}`" in requirements
    assert "Omit it" in requirements


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


def _flat_text(text: str) -> str:
    return " ".join(text.split())


def test_the_lens_is_never_told_that_records_are_compared_or_which_side_it_is_on():
    """A round that contrasts card-brief and plain-brief launches measures the
    card block only if neither launch knows a contrast is running or which
    side it is on: the agent definition is every launch's system prompt, so
    it names the plain brief as a mode and never as one side of a comparison
    (the same rule that keeps the word "control" out of a plain prompt)."""
    flat = _flat("lens").lower()
    for phrase in ("comparison", "plain side", "card side", "control arm", "treatment arm", "the round exists to make"):
        assert phrase not in flat, f"lens.md tells a launch about the contrast: {phrase!r}"


def test_every_seat_is_told_it_takes_part_in_every_declared_phase():
    """What the old 'sits the phase out breaks the comparison' sentence was
    for -- a control or plain launch that refuses the derivation phase -- is
    kept as a plain obligation, without naming a comparison."""
    flat = _flat("lens")
    assert "A control seat takes part in every phase its prompt declares" in flat
    assert "A plain launch takes part in every phase its prompt declares" in flat
