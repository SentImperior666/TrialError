"""Lane FB-1 item F12: the CLI emits UTF-8 whatever the host console says.

These tests spawn the INSTALLED CONSOLE SCRIPT (``trialerror``) as a real
subprocess with ``PYTHONIOENCODING`` naming a legacy codepage, because that
is the only place the bug lived: ``trialerror.cli.main`` called in-process
writes to whatever stream pytest's capture installed, and every in-process
test therefore passed while the shipped command failed.

They deliberately do NOT use ``trialerror.accept.e2e``'s ``_subprocess_env``
helper: it sets ``PYTHONIOENCODING=utf-8``, which would make this whole
module pass vacuously.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._ingest_fixtures import bootstrap_launch

#: Greek + CJK: representable in UTF-8, representable in NO cp1252 byte.
NON_CP1252_AUTHORS = "Ωμέγα Λαμβδα, 京子 田中"

_PACKAGE_PARENT = Path(__file__).resolve().parents[1]


def _console_script() -> Path:
    """The installed ``trialerror`` console script beside this interpreter.

    ``sys.executable`` is used UNRESOLVED: in a virtualenv the interpreter is
    a symlink into the base Python, and resolving it would look for the
    script in the base installation's ``bin`` instead of the venv's.
    """
    candidates = [Path(sys.executable).parent, Path(sys.executable).resolve().parent]
    for bindir in candidates:
        for name in ("trialerror", "trialerror.exe"):
            candidate = bindir / name
            if candidate.exists():
                return candidate
    found = shutil.which("trialerror")
    if found:
        return Path(found)
    pytest.skip("no installed `trialerror` console script beside this interpreter")


def _legacy_console_env(platform_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_PACKAGE_PARENT}{os.pathsep}{existing}" if existing else str(_PACKAGE_PARENT)
    )
    # the whole point: a console Python believes cannot encode the envelope.
    env["PYTHONIOENCODING"] = "cp1252"
    env.pop("PYTHONUTF8", None)
    return env


def _add_source_argv(script: Path, program_root: Path, platform_root: Path, launch_id: str) -> list[str]:
    return [
        str(script),
        "ingest",
        "add-source",
        "--program-root", str(program_root),
        "--platform-root", str(platform_root),
        "--kind", "web",
        "--title", "A title with a Λ in it",
        "--license-tier", "open",
        "--acquisition-route", "web",
        "--launch-id", launch_id,
        "--authors", NON_CP1252_AUTHORS,
    ]


def test_console_script_emits_one_parseable_line_under_a_legacy_codepage(
    store, program_root, platform_root
):
    launch_id = bootstrap_launch(store)
    store.close()
    script = _console_script()

    proc = subprocess.run(
        _add_source_argv(script, program_root, platform_root, launch_id),
        env=_legacy_console_env(platform_root),
        capture_output=True,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    lines = [ln for ln in proc.stdout.decode("utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    envelope = json.loads(lines[0])
    assert envelope["ok"] is True
    assert envelope["result"]["source"]["authors"] == NON_CP1252_AUTHORS


def test_the_legacy_codepage_env_really_reaches_the_child(store, program_root, platform_root):
    """Guard on the guard: if this environment stopped producing a cp1252
    stdout in the child, the test above would pass for the wrong reason."""
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
        env=_legacy_console_env(platform_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.stdout.strip().lower().replace("-", "") == "cp1252"


def test_without_the_reconfigure_the_same_write_would_have_failed(platform_root):
    """The counterfactual, so the fix is shown to be load-bearing: the same
    line written to the same kind of stream WITHOUT the reconfigure raises,
    and raises before any byte reaches the buffer (the write is discarded
    whole -- there is no partial line to find)."""
    program = (
        "import sys\n"
        "try:\n"
        f"    print({NON_CP1252_AUTHORS!r})\n"
        "except UnicodeEncodeError as exc:\n"
        "    sys.stderr.write('raised\\n')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        env=_legacy_console_env(platform_root),
        capture_output=True,
        timeout=60,
    )
    assert proc.stderr.decode("utf-8", "replace").strip() == "raised"
    assert proc.stdout == b""


def test_reconfigure_guard_tolerates_a_stream_without_reconfigure(monkeypatch):
    """A captured stream (``io.StringIO`` under pytest, a custom host
    wrapper) has no ``.reconfigure``; the guard must be a no-op, never an
    exception on the way into every single command."""
    import io

    from trialerror.cli import _force_utf8_stdio

    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    _force_utf8_stdio()  # must not raise
