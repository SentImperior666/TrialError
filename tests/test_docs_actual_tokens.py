"""Lane FB-1 item 11 (F9 + F13 docs half): the two sentences that had to be
written down somewhere a reader will find them.

Docs tests, and deliberately narrow: they pin that each claim is present at
the place the brief names, not its prose. The F13 definition is the one an
operator gets wrong at cost -- a reconcile taken from a cache-write proxy
mis-sizes every pool projection after it -- and the F9 line is a Claude Code
tool check nothing in this harness can enforce, so the docs are the only
place it can live at all.
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


def test_the_operator_guide_carries_the_actual_tokens_block():
    text = _read("OPERATOR_GUIDE.md")
    assert "### What `--actual-tokens` counts" in text
    block = text.split("### What `--actual-tokens` counts", 1)[1].split("\n### ", 1)[0]
    assert "the total the HOST reports" in block
    assert "unverified" in block
    assert "billed_multiplier" in block and "learned" in block
    assert "budget calibrate" in block
    assert "caller-asserted label" in block
    assert "never" in block and "cache-write proxy" in block


def test_getting_started_carries_it_beside_the_reconcile_example():
    text = _read("GETTING_STARTED.md")
    example = "trialerror budget reconcile --launch-id <launch_id> --actual-tokens 4200"
    assert example in text
    after = text.split(example, 1)[1][:1200]
    assert "What `--actual-tokens` counts" in after
    assert "cache-write proxy" in after
    assert "caller-asserted" in after


def test_the_design_schema_row_names_the_unit():
    text = _read("DESIGN_v0.md")
    row = [line for line in text.splitlines() if "actual_tokens?" in line]
    assert row, "DESIGN_v0.md has no launch-schema row naming actual_tokens"
    assert any("VISIBLE tokens" in line for line in row)
    assert any("caller-asserted" in line for line in row)


def test_the_operator_guide_says_to_author_workflow_scripts_with_lf():
    text = _read("OPERATOR_GUIDE.md")
    assert "LF line endings" in text
    sentence = text.split("LF line endings", 1)[0].rsplit("\n\n", 1)[-1] + "LF line endings"
    assert "workflow script" in sentence
    # ...and says whose check it is, so nobody goes looking for a doctor check
    after = text.split("LF line endings", 1)[1][:300]
    assert "Claude Code tool check" in after
    assert "not a harness check" in after
