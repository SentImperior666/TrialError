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


def test_the_record_schema_is_written_down_with_its_required_fields():
    block = _guide_section("### The record schema, enforced at intake")
    assert "lens intake" in block
    for field in ("`statement`", "`probe`", "`provenance`", "`requirements`", "`operation_declared`"):
        assert field in block, f"the record schema table does not name {field}"
    assert "provenance.docs" in block
    assert "validated before any of it is written" in block


def test_the_lens_launch_link_names_both_ways_to_make_one():
    block = _guide_section("### Linking a lens's launch to its slice")
    assert "budget book --assign-id" in block
    assert "lens_assignment.lens_launch_id" in block
    assert "lens export" in block
    assert "lens_log_reconciled" in block
    assert "log shape unrecognised" in block


def test_the_procedure_file_flag_says_why_the_shell_form_is_wrong():
    block = _guide_section("### `prereg_compliant` — pass the procedure byte-exact")
    assert "--executed-procedure-file" in block
    assert "trailing newline" in block
    assert "mutually exclusive" in block


def test_user_setup_documents_the_handoffs_dir_opt_in():
    text = _read("USER_SETUP.md")
    assert "handoffs_dir_outside_root" in text
    assert "[session]" in text
