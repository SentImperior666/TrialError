"""The explicit "tested both ways" requirement from the M12 build brief:
"everything no-ops cleanly without [opentelemetry/arize-phoenix] (tested
both ways: with deps in .venv312, and a subprocess test with them
hidden)". Every other test in this suite (test_obs_tracer.py,
test_obs_spans.py, ...) covers the "with deps" side, against the REAL
installed packages. This file covers the "hidden" side, via a genuinely
fresh subprocess with ``opentelemetry.*`` blocked at the meta-path level
(see ``tests/_obs_noop_subprocess_script.py``'s own docstring for why that,
and not an in-process monkeypatch of ``tracer.OTEL_AVAILABLE``, is the
real proof)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "_obs_noop_subprocess_script.py"


def test_trialerror_obs_is_a_genuine_noop_in_a_fresh_interpreter_with_opentelemetry_blocked():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert json.loads(result.stdout.strip()) == {"ok": True}
