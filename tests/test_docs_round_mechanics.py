"""Lane FB-4 item 11: the round-mechanics claims an operator has to be able
to find.

Deliberately narrow, like ``tests/test_docs_actual_tokens.py``: each
assertion pins that a claim is present at the place the brief names, not its
prose. Every one of these is something a live round got wrong precisely
because nothing written down said otherwise -- a plant cut from a prose
chunk, a prompt built from an envelope carrying the plant id, a record with
no ``provenance.docs``, a lens launch nothing linked to its slice, a
procedure hash over a string the shell had stripped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DOCS = Path(__file__).resolve().parents[1] / "docs"


def _read(name: str) -> str:
    path = _DOCS / name
    if not path.is_file():
        pytest.skip(f"{name} not present in this tree")
    return path.read_text(encoding="utf-8")


def _guide_section(heading: str) -> str:
    text = _read("OPERATOR_GUIDE.md")
    assert heading in text, f"OPERATOR_GUIDE.md has no {heading!r} section"
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_the_guide_has_an_ideation_rounds_section():
    assert "## Ideation rounds" in _read("OPERATOR_GUIDE.md")


def test_the_plant_model_says_what_a_plant_is_and_what_a_miss_costs():
    block = _guide_section("### The plant model")
    assert "inventory plant" in block and "paraphrase plant" in block
    assert "fails the batch" in block
    assert "MECHANIC ROW" in block
    assert "<source>-M<nnn>" in block
    assert "never byte-identical" in block
    assert "paraphrase_method" in block
    assert "--paraphrase-backend llm" in block


def test_the_judged_batch_flow_names_the_masked_ids():
    block = _guide_section("### The judged batch, and the ids a judge sees")
    assert "judge_views" in block and "mask" in block
    assert "J-<n>" in block
    assert "--judge-envelopes-out" in block
    assert "masked id, the real id or a mix" in block
    assert "not retrievable" in block


def test_the_re_judge_section_says_where_each_label_is_stored_and_names_both_verbs():
    """Lane R0-C. A round had to rebuild its own kappa's disagreements from
    the judges' sheets by hand because nothing written down said where a
    second opinion lives -- it lived nowhere."""
    block = _guide_section("### The re-judge is kept in the store, with its judges")
    assert "`verdict`" in block and "`verdict_rejudge`" in block
    assert "--judge-launch" in block and "--second-judge-launch" in block
    assert "--rejudge-report" in block and "--record-rejudge" in block
    assert "rejudge_rows_match_recorded_kappa" in block


def test_the_slice_salt_section_names_both_schemes_and_what_reproducing_a_round_takes():
    """Lane R0-D. A round pre-registered the hash of a draw that depended on
    minted roster ids, and kept the hash true with a standing prohibition
    nothing written down explained. An operator has to be able to read which
    scheme drew a stored round, and that reproducing it means using that one."""
    block = _guide_section("### What the slice draw depends on")
    assert "--slice-salt" in block
    assert "`roster-id`" in block and "`lens-name`" in block
    assert "slice_spec.salt_scheme" in block
    assert "pre-registered parameters" in block
    assert "A row with no recorded" in block and "scheme was drawn under `roster-id`" in block


def test_the_record_schema_is_written_down_with_its_required_fields():
    block = _guide_section("### The record schema, enforced at intake")
    assert "lens intake" in block
    for field in ("`statement`", "`probe`", "`provenance`", "`requirements`", "`operation_declared`"):
        assert field in block, f"the record schema table does not name {field}"
    assert "provenance.docs" in block
    assert "validated before any of it is written" in block


def test_the_derivation_phase_names_the_marker_the_renderer_must_emit():
    """The marker is a contract between two files -- the lens prompt admits
    the envelope only under it, and whatever renders the phase's prompts has
    to emit it. An operator reading only the guide has to learn the exact
    string, or the phase fails as a lens refusing a leak."""
    block = _guide_section("### The derivation phase")
    assert "DERIVATION ENVELOPE" in block
    assert "`parent_ids`" in block and '"phase": "derivation"' in block
    assert "control" in block
    for banned in ("label", "verdict", "dossier", "distance", "rubric"):
        assert banned in block, f"the derivation phase does not say the envelope carries no {banned}"


def test_the_derivation_phase_says_how_a_lenss_second_launch_is_booked():
    """The pattern is not guessable from the flag list: the SAME assign ids
    plus a phase label, and the first binding standing. An operator who
    books the second launch without assign ids loses the phase's slice."""
    block = _guide_section("### The derivation phase")
    assert "--phase derivation" in block
    assert "never overwritten" in block
    assert "lens_assignment_launch" in block


def test_the_lens_launch_link_names_both_ways_to_make_one():
    block = _guide_section("### Linking a lens's launch to its slice")
    assert "budget book --assign-id" in block
    assert "lens_assignment.lens_launch_id" in block
    assert "lens export" in block
    assert "lens_log_reconciled" in block
    assert "log shape unrecognised" in block


def test_the_round_gate_section_names_the_declarations_the_status_and_the_marker():
    """Lane R1-H. The round gate failed every round of another design by
    construction; a declaration in the prereg params is the way through, the
    marker is the renderer's contract with the lens, and an operator has to
    be able to find both."""
    block = " ".join(_guide_section("### The round gate reads the round's design").split())
    for key in ("**`design`**", "`control_count: N`", "**`rooms`**", "**`report_p_values`**"):
        assert key in block
    for design in ('"control_seats"', '"paired"', '"none"'):
        assert design in block
    assert "judged exactly as before" in block
    assert "**`not_applicable`**" in block and "never shown as a pass" in block
    assert "`reproduction_status` is `match` iff every check is a pass or `not_applicable`" in block
    assert "`lens_launches`" in block and "lens_assignment_launch" in block
    assert "--phase card" in block and "--phase plain" in block
    assert "`BRIEF: PLAIN`" in block and "on the plain launches only" in block


def test_the_procedure_file_flag_says_why_the_shell_form_is_wrong():
    block = _guide_section("### `prereg_compliant` — pass the procedure byte-exact")
    assert "--executed-procedure-file" in block
    assert "trailing newline" in block
    assert "mutually exclusive" in block


def test_user_setup_documents_the_handoffs_dir_opt_in():
    text = _read("USER_SETUP.md")
    assert "handoffs_dir_outside_root" in text
    assert "[session]" in text
