"""``docs/OPERATOR_GUIDE.md``'s doctor-checks headline figure, pinned to the
live registry.

Fix pass (verify of 2026-09-05), finding F-03. The guide advertises a check
count and a category count. That figure had drifted twice -- once silently
before the 2026-09 mining lane, and again the moment the lane's merge
brought in two more subsystems' checks, leaving the freshly-"recounted"
sentence contradicting the table printed directly beneath it. A number in
prose that nothing verifies is a number that is wrong; this test is the
verification.

Adding a doctor check is supposed to fail this test. The fix when it does
is to update the sentence in the guide, never to relax the assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from trialerror.util.doctor import discover_and_register_checks, registered_checks

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GUIDE = _REPO_ROOT / "docs" / "OPERATOR_GUIDE.md"

#: "53 checks across 21 categories", however it is emphasised or worded
#: around.
_FIGURE = re.compile(r"\*{0,2}(\d+)\s+checks\s+across\s+(\d+)\s+categories")


def _registry() -> dict[str, tuple[str, object]]:
    discover_and_register_checks()
    return registered_checks()


@pytest.mark.skipif(not _GUIDE.is_file(), reason="OPERATOR_GUIDE.md not present in this tree")
def test_the_guides_check_count_matches_the_live_registry():
    text = _GUIDE.read_text(encoding="utf-8")
    matches = _FIGURE.findall(text)
    assert len(matches) == 1, (
        "expected exactly one 'N checks across M categories' claim in OPERATOR_GUIDE.md, "
        f"found {len(matches)}: {matches}"
    )
    claimed_checks, claimed_categories = (int(x) for x in matches[0])

    registry = _registry()
    actual_checks = len(registry)
    actual_categories = len({category for category, _fn in registry.values()})

    assert claimed_checks == actual_checks, (
        f"docs/OPERATOR_GUIDE.md claims {claimed_checks} doctor checks; the registry has "
        f"{actual_checks}. Update the guide -- do not relax this test."
    )
    assert claimed_categories == actual_categories, (
        f"docs/OPERATOR_GUIDE.md claims {claimed_categories} check categories; the registry has "
        f"{actual_categories}. Update the guide -- do not relax this test."
    )


@pytest.mark.skipif(not _GUIDE.is_file(), reason="OPERATOR_GUIDE.md not present in this tree")
def test_every_check_the_guides_table_names_actually_exists():
    """The table is a partial map, so it need not be exhaustive -- but a
    name printed there must resolve, or an operator reading it will type a
    `--only` that does nothing."""
    text = _GUIDE.read_text(encoding="utf-8")
    table = text.split("## Doctor checks catalog", 1)[1].split("\n\n`--only", 1)[0]
    named = {
        name
        for line in table.splitlines()
        if line.startswith("| `")
        for name in re.findall(r"`([a-z0-9_]+)`", line.split("|")[2])
    }
    assert named, "no check names parsed out of the catalog table -- has its shape changed?"

    registry = _registry()
    missing = sorted(named - set(registry))
    assert not missing, f"OPERATOR_GUIDE.md's catalog table names checks that are not registered: {missing}"
