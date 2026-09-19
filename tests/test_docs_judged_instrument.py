"""Lane FB-5 item 7: the instrument claims an operator has to be able to
find.

Deliberately narrow, like ``tests/test_docs_round_mechanics.py``: each
assertion pins that a claim is present at the place the brief names, not its
prose. Every flag and config row the lane shipped is named in a doc here,
because a knob nobody can find is a knob that does not exist -- and the
distinctions these sections carry (absent is not empty, total mappings,
reported-not-refused, calibration consolidates nothing) are exactly the ones
a round gets wrong when nothing written down says otherwise.
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
    after = text.split(heading, 1)[1]
    return after.split("\n### ", 1)[0].split("\n## ", 1)[0]


def _setup_section(heading: str) -> str:
    text = _read("USER_SETUP.md")
    assert heading in text, f"USER_SETUP.md has no {heading!r} section"
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


# ---------------------------------------------------------------------------
# OPERATOR_GUIDE
# ---------------------------------------------------------------------------


def test_the_instrument_section_names_every_declaration_and_its_flag():
    block = _guide_section("### The instrument — what a round declares")
    for flag in ("--judged-sets", "--labels-file", "--plants-file", "--batch-fail-on"):
        assert flag in block, f"the instrument section does not name {flag}"
    assert "[lens.novelty]" in block
    assert "the flag wins" in block


def test_the_declared_sets_say_that_absent_is_not_empty():
    block = _guide_section("### The instrument — what a round declares")
    assert "absent from the envelope" in block
    assert "not empty" in block
    assert "archive_rows" in block
    assert "judged_sets_disagree" in block
    assert "R5 is evidence for the R4 label" in block


def test_the_vocabulary_claims_are_written_down():
    block = _guide_section("### The instrument — what a round declares")
    assert "label_canonical" in block
    assert "total over the" in block
    assert "hashed onto the\nbatch" in block or "hashed onto the batch" in block


def test_the_plants_file_fields_are_all_named():
    block = _guide_section("### The instrument — what a round declares")
    for field in ("`plant_id`", "`kind`", "`statement`", "`expected_labels`", "`donor_ref`, `source_ref`"):
        assert field in block, f"the plants-file table does not name {field}"
    assert "masked ids" in block
    assert "--plants 0" in block


def test_the_failing_kinds_distinguish_failures_from_inventory_failures():
    block = _guide_section("### The instrument — what a round declares")
    assert "`failures`" in block and "`inventory_failures`" in block
    assert "`by_kind`" in block
    assert "unauditable" in block


def test_the_archive_round_names_its_three_rules():
    block = _guide_section("### The archive round")
    assert "--status archived" in block
    assert "never consolidated" in block
    assert "never folds across it" in block
    assert "archive_hit" in block
    assert "archive_hits" in block


def test_calibration_names_the_cards_fields_and_what_it_never_does():
    block = _guide_section("### Calibration — measuring the instrument before the round")
    for field in (
        "catch_by_kind", "kappa_by_set", "r_embedding_human", "baseline_cosine_distribution",
        "misses_by_id",
    ):
        assert field in block, f"the calibration card table does not name {field}"
    assert "--calibration" in block and "--record-calibration" in block
    assert "--judge-sheet-a" in block and "--pair-ratings" in block
    assert "novelty-v2-calibration" in block
    assert "n_records" in block and "n_with_neighbour" in block


def test_the_seed_section_says_reported_never_refused():
    block = _guide_section("### Seed work behind an `unscreenable`")
    assert "seeds" in block
    assert "never a reference set" in block
    assert "unscreenable_below_bar" in block
    assert "never refused" in block


def test_the_record_schema_still_names_the_intake_status_flag():
    block = _guide_section("### The record schema, enforced at intake")
    assert "--status" in block


# ---------------------------------------------------------------------------
# USER_SETUP
# ---------------------------------------------------------------------------


def test_user_setup_carries_a_lens_novelty_config_table():
    block = _setup_section("## 3g. Optional: the judged novelty screen as a configurable instrument")
    assert "[lens.novelty]" in block
    for key in ("judged_sets", "labels_file", "plants_file", "batch_fail_on"):
        assert key in block, f"the [lens.novelty] table does not name {key}"


def test_user_setup_states_every_default_and_where_a_relative_path_resolves():
    block = _setup_section("## 3g. Optional: the judged novelty screen as a configurable instrument")
    assert '["R3", "R4"]' in block
    assert '["inventory"]' in block
    assert "against the program root" in block
    assert "labels_file_refused" in block and "plants_file_refused" in block


def test_user_setup_shows_the_labels_file_in_full():
    block = _setup_section("## 3g. Optional: the judged novelty screen as a configurable instrument")
    assert '"canonical"' in block
    assert '"extra"' in block and '"seed"' in block
    assert '"unscreenable"' in block
    assert "first" in block, "the seed vocabulary's first-is-on-topic convention"
