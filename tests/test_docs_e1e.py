"""Lane e1e: the claims an operator has to be able to find.

Deliberately narrow, the shape ``tests/test_docs_small_items.py`` already
uses: each assertion pins that a claim is present at the place the brief
names, never its prose. A knob nobody can find is a knob that surprises
somebody later -- and for a rule that TIGHTENS an existing gate the two
claims that have to be findable are the direction (fewer candidates, never
more) and the way back (the keys that reach the old behaviour, and the verb
that re-asks a queue opened before the change).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trialerror.ingest.backends import DEFAULT_BOUNDED_DPI, plan_page_ranges

_DOCS = Path(__file__).resolve().parents[1] / "docs"


def _read(name: str) -> str:
    path = _DOCS / name
    if not path.is_file():
        pytest.skip(f"{name} not present in this tree")
    return path.read_text(encoding="utf-8")


def _section(name: str, heading: str) -> str:
    text = _read(name)
    assert heading in text, f"{name} has no {heading!r} section"
    after = text.split(heading, 1)[1]
    return after.split("\n### ", 1)[0].split("\n## ", 1)[0]


# ---------------------------------------------------------------------------
# Part A -- the duplicate gate's coverage rule
# ---------------------------------------------------------------------------

_GATE_HEADING = "## The duplicate-candidate gate"
_LEXICON_HEADING = "## 3h. Optional: the duplicate-candidate gate"


def test_the_guide_names_both_rule_1e_keys_and_their_defaults():
    block = _section("OPERATOR_GUIDE.md", _GATE_HEADING)
    assert "duplicate_coverage_min" in block
    assert "name_in_text_requires_informative" in block


def test_the_guide_says_coverage_is_applied_after_the_frequency_test():
    """The one thing about the rule that is easy to get backwards, and the
    thing that decides whether a pair sharing no rare word is affected by
    it at all (it is not)."""
    block = _section("OPERATOR_GUIDE.md", _GATE_HEADING)
    assert "AFTER the frequency test, never instead of it" in block
    assert "shorter" in block


def test_the_guide_says_the_rule_is_a_tightening_and_how_to_turn_it_off():
    block = _section("OPERATOR_GUIDE.md", _GATE_HEADING)
    assert "tightening" in block
    assert "subset" in block
    assert "duplicate_coverage_min = 0.0" in block


def test_the_guide_carries_the_rescan_procedure():
    block = _section("OPERATOR_GUIDE.md", _GATE_HEADING)
    assert "term scan --rescan --dry-run" in block
    assert "withdrawn_by_coverage" in block
    assert "human-touched" in block
    assert "--by-launch" in block


def test_user_setup_has_the_lexicon_config_rows():
    block = _section("USER_SETUP.md", _LEXICON_HEADING)
    for key in (
        "duplicate_coverage_min",
        "name_in_text_requires_informative",
        "duplicate_informative_token_fraction",
        "duplicate_similarity_floor",
    ):
        assert key in block, key
    assert "`0.5`" in block
    assert "`true`" in block
    assert "term scan --rescan" in block


# ---------------------------------------------------------------------------
# Part B -- OCR page-range chunking
# ---------------------------------------------------------------------------

_CHUNKING_HEADING = "## OCR page-range chunking"


def test_the_guide_names_the_four_ocr_keys():
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    for key in (
        "max_range_pixels",
        "bounded_dpi",
        "page_range_flag",
        "page_range_numbering",
    ):
        assert key in block, key


def test_the_guide_carries_the_measured_memory_condition_and_the_arithmetic():
    """A bound whose number nobody can re-derive is a number that gets
    changed by feel. The guide states what was measured and how the default
    follows from it."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "58 GB" in block
    assert "3,562,596" in block
    assert "16 pages per range" in block
    # FIX V-4. The worked example is the part an operator checks the knob
    # against, so it is the part that must not be arithmetic nobody did:
    # 540 A4 pages at the default budget and the default planning DPI is 33
    # full ranges (528) and a 34th of 12. Pinned to the planner's own answer.
    plan = plan_page_ranges(
        [(595.0, 842.0)] * 540, dpi=DEFAULT_BOUNDED_DPI, max_range_pixels=64_000_000
    )
    assert [r.count for r in plan] == [16] * 33 + [12], "the guide's example, as the planner plans it"
    assert "**34\nranges**" in block or "**34 ranges**" in block
    assert "33 of 16 pages (528) and a 34th of 12" in block
    assert "one of 45" not in block
    assert "65 pages per range" not in block, "the 96-DPI arithmetic is gone, not merely amended"


def test_the_guide_explains_why_the_planning_dpi_is_markers_high_res_pass():
    """The knob's whole worth is that it is at least what the stack really
    renders at, and the number was wrong in the shipped default until a scan
    died proving it. An operator who does not know marker holds the whole
    range as high-res images cannot size this."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "highres_image_dpi" in block
    assert "MemoryError" in block
    assert "every page of the range" in block or "every page of the range" in block.lower()
    assert "DPI *squared*" in block or "DPI squared" in block
    assert "four times too generous" in block
    assert "`max(bounded_dpi, N)`" in block
    assert "raised, never lowered" in block.replace("**", "")


def test_the_guide_prices_a_range_in_invocations_as_well_as_memory():
    """Each range is another marker process and another model load, and the
    two costs move in opposite directions. The guide has to show both, with
    arithmetic an operator can redo, or the budget gets set by feel."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "another model load" in block
    plate = (23 * 72.0, 33 * 72.0)
    for budget, per_range in ((32_000_000, 1), (64_000_000, 2), (256_000_000, 8)):
        plan = plan_page_ranges([plate] * 540, dpi=DEFAULT_BOUNDED_DPI, max_range_pixels=budget)
        assert plan[0].count == per_range, (budget, per_range)
    # The table's own numbers: 540 pages at 1, 2 and 8 pages a range.
    for cell in ("540 invocations", "270 invocations", "68 invocations"):
        assert cell in block, cell
    assert "27,979,776" in block
    assert "6,994,944" in block, "and what the same page counted at the wrong DPI"


def test_the_guide_carries_the_numbering_rule_and_the_key():
    """The defect this hotfix repairs, written where an operator meets it:
    which convention the installed release uses, that `auto` decides from
    evidence, and that every un-decidable case is a refusal rather than a
    guess."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "page_range_numbering" in block
    assert "marker-pdf 1.10.x numbers by the page's own index" in block
    assert "never by\nguessing" in block or "never by guessing" in block
    assert "relative-consistent" in block and "absolute-consistent" in block
    assert "The first range never decides" in block
    assert "raw marker text" in block, "and why a resume cannot mix two conventions"


def test_the_guide_says_why_a_bound_rather_than_a_kill():
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "kill" in block
    assert "never recomputed" in block


def test_the_guide_carries_the_refusals_and_the_resume():
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "--help" in block
    assert "strictly increasing" in block
    assert "expect.page_count" in block
    assert "_ranges" in block


def test_the_guide_says_how_far_the_page_count_check_reaches():
    """FIX V-7. The guide described `expect.page_count` as the sandbox's
    registration count, and nothing counts pages at registration: the column
    is written by one route in the tree, so for a PDF acquired directly the
    check does not fire at all. A check advertised wider than it reaches is
    one an operator stops looking behind."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "when that\n  column is populated" in block or "when that column is populated" in block
    assert "this check does not fire" in block
    assert "Absence is not disagreement" in block


def test_the_guide_says_a_damaged_cached_range_is_re_run():
    """FIX V-1's operator-facing half: the resume cache is inspectable, so
    the rule for anyone looking inside it has to be written down. A range
    file that does not hash to the digest beside it is not in the cache."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert ".sha256" in block
    assert "not in the cache" in block
    assert "the write\nis atomic" in block or "the write is atomic" in block


def test_the_guide_carries_the_two_machine_rollout_note():
    """What the operator has to DO after the merge, and -- just as load
    bearing -- what they do not: the queue host is untouched."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "queue-host wrapper is **unchanged**" in block
    assert "restart `trialerror offload worker`" in block
    assert "jobs resume" in block


def test_the_control_section_no_longer_says_ocr_is_always_one_unit():
    """The bullet that documented the limitation this lane removes. It still
    has to describe the unchunked case, and it must not still say the
    chunked one is a future fix."""
    block = _section("OPERATOR_GUIDE.md", "## Detached jobs")
    assert "OCR's unit is the RANGE when the document is chunked" in block
    assert "The fix is\n    page-range chunking" not in block


def test_user_setup_has_the_four_ocr_rows():
    block = _section("USER_SETUP.md", "## 1. Local models")
    assert "`max_range_pixels` (optional)" in block
    assert "`bounded_dpi` (optional)" in block
    assert "`page_range_flag` (optional)" in block
    assert "`page_range_numbering` (optional)" in block
    assert "64000000" in block
    assert "16 pages per range" in block
    assert "Default `192`" in block, "the planning DPI row states the corrected default"
    assert "highres_image_dpi" in block
    assert "another model load" in block, "and that a range costs an invocation, not only memory"
    assert "marker-pdf 1.10.x does" in block, "which convention the supported release uses"
