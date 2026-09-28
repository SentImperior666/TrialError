"""The kept skill file the gate review's procedure lives in carries its
instructions — and carries nothing else. (The other round skills were retired
in Phase 0.)

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
SKILLS = ("gate-critic",)


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


def test_the_critic_assembles_the_admission_escrow_into_the_subject():
    flat = _flat("gate-critic")
    assert "`admission_escrow` that order was committed under" in flat


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
