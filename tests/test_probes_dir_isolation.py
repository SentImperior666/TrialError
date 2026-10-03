"""Every test must be isolated from the real machine's shared
``~/.trialerror/probes/hook_events.jsonl`` -- the same file
``trialerror probes run``'s ``hook_payload_keys`` check reads as evidence.
Before ``tests/conftest.py``'s ``_isolated_probes_dir`` autouse fixture
existed, nothing set ``TRIALERROR_PROBES_DIR`` for the pre-existing hook
suites (``test_spawn_gate_hook.py``, ``test_session_hooks.py``), so their
subprocesses inherited ``os.environ`` verbatim and appended real-looking
records to the real file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_probes_dir_is_isolated_from_the_real_machine_by_default(real_home):
    real_probes_dir = (real_home / ".trialerror" / "probes").resolve()
    configured = os.environ.get("TRIALERROR_PROBES_DIR")
    assert configured is not None, "TRIALERROR_PROBES_DIR must be set by an autouse fixture for every test"
    configured_path = Path(configured).resolve()
    assert configured_path != real_probes_dir
    assert real_probes_dir not in (configured_path, *configured_path.parents)


def test_a_subprocess_inheriting_os_environ_never_reaches_the_real_probes_dir(real_home):
    """The exact shape test_spawn_gate_hook.py/test_session_hooks.py use:
    `env = dict(os.environ)` handed to a subprocess. Proves the isolation
    survives that inheritance, not just the parent process's own env."""
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('TRIALERROR_PROBES_DIR', ''))"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    child_value = proc.stdout.strip()
    real_probes_dir = str((real_home / ".trialerror" / "probes").resolve())
    assert child_value, "the child process saw no TRIALERROR_PROBES_DIR at all"
    assert str(Path(child_value).resolve()) != real_probes_dir
