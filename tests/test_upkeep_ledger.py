"""The upkeep ledger script runs and emits every row with an integer."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upkeep_ledger.py"

REQUIRED_ROWS = (
    "py files: total",
    "py lines: total",
    "hooks",
    "plugin skills",
    "plugin agents",
    "tables: ops",
    "migration version: ops",
    "cli groups",
    "cli actions",
    "mcp tools: ops",
    "mcp tools: knowledge",
    "doctor checks",
    "doctor categories",
    "job handlers",
    "job kinds (CHECK)",
    "test files",
    "test functions",
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=300)


def test_json_output_has_every_row_as_an_integer():
    proc = _run("--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["date"] and data["commit"]
    rows = data["rows"]
    for label in REQUIRED_ROWS:
        assert isinstance(rows.get(label), int), label
    assert all(isinstance(v, int) for v in rows.values())
    assert rows["mcp tools: ops"] > 0 and rows["mcp tools: knowledge"] > 0


def test_markdown_output_is_a_table():
    proc = _run()
    assert proc.returncode == 0, proc.stderr
    assert "| Row | Count |" in proc.stdout
    assert "| doctor checks |" in proc.stdout
