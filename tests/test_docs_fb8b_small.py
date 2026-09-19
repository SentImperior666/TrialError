"""Lane FB-8b: the claims the small items put in the docs, pinned.

The shape ``tests/test_docs_e1e.py`` and ``tests/test_docs_small_items.py``
already use: each assertion pins that a claim is findable where the brief
says it should be, never its prose around it. Two things here are worth
more than that, though, and get a number rather than a phrase:

- **the measured OCR sizing figures**, because the whole point of item 1 is
  that the raster arithmetic is not the invocation's memory and an operator
  who cannot find a real measurement will size the knob from the arithmetic
  anyway. A figure nobody pinned is a figure that gets rounded off in the
  next edit;
- **the command catalog's `offload` row**, derived from the argparse tree
  rather than from a list written down here, so a subcommand added without a
  catalog entry fails this file instead of surprising somebody at `--help`.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

_DOCS = Path(__file__).resolve().parents[1] / "docs"

_CHUNKING_HEADING = "## OCR page-range chunking"


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


def _ocr_rows() -> str:
    """The ``[ingest.ocr]`` block of USER_SETUP.md's config table."""
    text = _read("USER_SETUP.md")
    assert "| `[ingest.ocr]` |" in text, "USER_SETUP.md has no [ingest.ocr] rows"
    after = text.split("| `[ingest.ocr]` |", 1)[1]
    return after.split("| `[ingest.embed]` |", 1)[0]


# ---------------------------------------------------------------------------
# item 1 -- the sizing paragraph says what was MEASURED
# ---------------------------------------------------------------------------


def test_the_guide_says_the_raster_arithmetic_is_a_lower_bound_on_one_component():
    """The defect this closes: `pixels x 3 bytes x safety` reads like the
    invocation's memory and is not within two orders of magnitude of it, so
    an operator sizing from it sizes from a number about page raster alone."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "lower bound" in block.lower()
    assert "two orders of magnitude" in block
    assert "page raster and nothing else" in block


def test_the_guide_carries_the_measured_figures_as_one_machines_measurement():
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "one machine's measurement" in block, (
        "the figures must be labelled as one measurement, not as constants"
    )
    assert "marker-pdf 1.10.2" in block
    assert "16 GB GPU" in block, "the hardware CLASS, which is what makes the figures readable"
    for figure in ("14.5–16 GB", "0.22–0.25 GB", "34–35 GB", "32.6 GB", "7–8 pages a minute"):
        assert figure in block, figure


def test_the_guide_carries_the_sizing_procedure():
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "peak_rss_bytes" in block and "range_wall_s" in block
    assert "`pages = (budget − fixed) / per-page`" in block
    assert "`max_range_pixels = pages × page_pixels_at_planning_dpi`" in block


def test_the_guide_says_the_fixed_cost_dominates_below_about_thirty_pages():
    """Which way to tune, and when tuning stops paying: this is the sentence
    that stops somebody halving the budget a fourth time."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "30 pages a range the fixed cost dominates" in block
    assert "doubles the number of model loads" in block


def test_the_sizing_table_says_its_column_is_raster_only():
    """The hotfix's table stays, and stays honest: its pages-per-range
    column is the raster arithmetic, which is not what the process costs."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    assert "pages per range (raster only)" in block
    # and the table itself is still there
    assert "| `32000000` | 1 | 300 invocations | 540 invocations |" in block


def test_user_setups_ocr_rows_carry_the_same_four_claims():
    rows = _ocr_rows()
    assert "LOWER bound on one component" in rows
    assert "two orders of magnitude" in rows
    assert "one machine's measurement" in rows
    for figure in ("14.5–16 GB", "0.22–0.25 GB", "34–35 GB", "32.6 GB"):
        assert figure in rows, figure
    assert "(budget − fixed) / per-page" in rows
    assert "30 pages a range the fixed cost dominates" in rows


# ---------------------------------------------------------------------------
# item 3 -- the command catalog has an `offload` row
# ---------------------------------------------------------------------------
#: The expected set is DERIVED, never written down here: a subcommand added
#: to the parser without a catalog entry has to fail this file, and a list
#: maintained beside the table would only move the drift one file over.


def _group_row(group: str) -> str:
    text = _read("OPERATOR_GUIDE.md")
    rows = [line for line in text.splitlines() if line.startswith(f"| `{group}` |")]
    assert rows, f"OPERATOR_GUIDE.md's command table has no `{group}` row"
    # The doctor-checks catalog has an `offload` row of its own further down
    # the file; the command table's is the one whose second cell is a verb
    # list, and it is the first.
    return rows[0]


def _group_parser(group: str):
    from trialerror.cli import build_parser

    parser = build_parser()
    groups = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]
    return groups.choices[group]


def _registered_verbs(group: str) -> set[str]:
    sub = _group_parser(group)
    verbs = [a for a in sub._actions if isinstance(a, argparse._SubParsersAction)][0]
    return set(verbs.choices)


def _catalogued_verbs(group: str) -> set[str]:
    cells = _group_row(group).split("|")
    return set(re.findall(r"`([a-z][a-z0-9-]*)`", cells[2]))


def test_the_command_catalog_has_an_offload_row_naming_every_registered_verb():
    registered = _registered_verbs("offload")
    catalogued = _catalogued_verbs("offload")
    assert not (registered - catalogued), (
        f"OPERATOR_GUIDE.md's `offload` row does not name: {sorted(registered - catalogued)}"
    )
    assert not (catalogued - registered), (
        f"OPERATOR_GUIDE.md's `offload` row names verbs that do not exist: "
        f"{sorted(catalogued - registered)}"
    )


def test_the_offload_row_counts_what_the_parser_registers():
    assert len(_catalogued_verbs("offload")) == len(_registered_verbs("offload"))


def test_every_flag_the_offload_row_names_is_a_real_flag_of_that_group():
    """The verbs are only half of it. A row that advertises `--queue-path`
    for a parser that spells it `--queue-root` sends an operator to a
    SystemExit, and nothing else in the suite reads this cell."""
    sub = _group_parser("offload")
    verbs = [a for a in sub._actions if isinstance(a, argparse._SubParsersAction)][0]
    real = {
        option
        for parser in verbs.choices.values()
        for action in parser._actions
        for option in action.option_strings
    }
    named = set(re.findall(r"(--[a-z][a-z0-9-]*)", _group_row("offload")))
    assert named, "the offload row names no flags at all -- has its shape changed?"
    assert not (named - real), f"flags the offload parsers do not have: {sorted(named - real)}"


# ---------------------------------------------------------------------------
# item 6 -- docs in lockstep with items 2, 4 and 5
# ---------------------------------------------------------------------------


def test_the_guide_documents_the_per_range_cost_fields():
    """Item 2's numbers are only useful if an operator knows they exist and
    knows what the POSIX one is a peak of. A cumulative high-water mark
    printed as "this range's peak" is a number somebody will subtract."""
    block = _section("OPERATOR_GUIDE.md", _CHUNKING_HEADING)
    for field in ("`range_wall_s`", "`cached`", "`peak_rss_bytes`", "`peak_rss_source`"):
        assert field in block, field
    assert "getrusage(RUSAGE_CHILDREN).ru_maxrss" in block
    assert "cumulative" in block
    assert "never subtract two of them" in block
    assert "psutil" in block and "no dependency is added" in block.lower()


def test_the_guide_says_both_recording_phases_stamp_prereg_compliant():
    """Item 5, where an operator meets it: the flags, the three answers, and
    that a mismatch is recorded rather than refused."""
    text = _read("OPERATOR_GUIDE.md")
    assert "### `prereg_compliant` — pass the procedure byte-exact" in text
    block = text.split("### `prereg_compliant` — pass the procedure byte-exact", 1)[1].split(
        "\n### ", 1
    )[0]
    assert "`--record-verdicts` and `--record-calibration` take the" in block
    assert "recorded, never refused" in block
    # the three answers, distinguished
    assert "no prereg_id given" in block
    assert "not stamped" in block
    assert "compliant by default" in block, "the middle state has to be named as NOT compliance"


def test_the_calibration_recipe_names_the_prereg_flags():
    text = _read("OPERATOR_GUIDE.md")
    recipe = text.split("# 2. two judges label it", 1)[1].split("```", 1)[0]
    for flag in ("--prereg-id", "--executed-procedure-file", "--executed-params"):
        assert flag in recipe, flag


def test_the_calibration_card_table_lists_the_prereg_fields():
    text = _read("OPERATOR_GUIDE.md")
    assert "| `prereg_id`, `prereg_compliant`, `prereg_compliance` |" in text


def test_release_readiness_says_the_export_default_is_per_worktree():
    """Item 4. The one doc that names the export's destination now names
    both: the mirror you publish from, and the gate directory a lane gets."""
    text = _read("RELEASE_READINESS.md")
    assert "scripts/export_public.py" in text
    assert ".export_public/" in text
    assert "--dest ../trialerror-public" in text
