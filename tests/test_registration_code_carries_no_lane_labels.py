"""The harness is exported publicly: the code of the plan-time check and of the
two registration end states says what an edge or a column is for, never which
lane or programme asked for it."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "trialerror/artifacts/state_machine.py",
    "trialerror/artifacts/gates.py",
    "trialerror/artifacts/checks.py",
    "trialerror/verify/plan_check.py",
    "trialerror/verify/prereg.py",
    "trialerror/cli/prereg.py",
    "trialerror/cli/artifact.py",
    "trialerror/stores/schema/ops.py",
    "tests/test_artifacts_state_machine.py",
    "tests/test_dashboard_data_v2.py",
]
# built from pieces so this file does not match itself
LABELS = ["te" + "-meta", "te" + "-audit", "te" + "-prereg", "round" + "-0", "round " + "0", "E0" + "-5", "V" + "-2:"]


@pytest.mark.parametrize("name", FILES)
def test_no_lane_or_programme_labels(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    found = [label for label in LABELS if label in text]
    assert not found, f"{name} carries {found}"
