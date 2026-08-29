import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror import __version__
from trialerror.cli import main
from trialerror.util.envelope import PROTOCOL_VERSION


def _trialerror_console_script() -> Path:
    """Locate the installed 'trialerror' console script next to the current
    interpreter (works for a venv without relying on PATH), falling back to
    a PATH lookup."""
    scripts_dir = Path(sys.executable).parent
    for candidate in (scripts_dir / "trialerror.exe", scripts_dir / "trialerror"):
        if candidate.exists():
            return candidate
    found = shutil.which("trialerror")
    if found:
        return Path(found)
    pytest.skip("trialerror console script not found; run `pip install -e .` first")


def test_cli_version_in_process_emits_valid_envelope(capsys):
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    env = json.loads(out)
    assert env["ok"] is True
    assert env["command"] == "version"
    assert env["protocolVersion"] == PROTOCOL_VERSION
    assert env["result"]["version"] == __version__
    assert isinstance(env["nextActions"], list)
    assert env["meta"] == {}


def test_cli_version_text_format(capsys):
    rc = main(["--version", "--format", "text"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[OK] version" in out


def test_cli_no_args_prints_help_and_exits_zero(capsys):
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: trialerror" in out


def test_trialerror_dash_dash_version_via_installed_console_script():
    """The literal M0 acceptance criterion: `trialerror --version` emits a valid
    envelope -- exercised via the actual installed console script, not the
    in-process function call above."""
    exe = _trialerror_console_script()
    result = subprocess.run(
        [str(exe), "--version"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr

    line = result.stdout.strip().splitlines()[-1]
    env = json.loads(line)
    assert env["ok"] is True
    assert env["command"] == "version"
    assert env["protocolVersion"] == PROTOCOL_VERSION
    assert env["result"]["package"] == "trialerror"
    assert env["result"]["version"]
    assert "nextActions" in env
    assert "meta" in env
