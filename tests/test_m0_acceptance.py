"""M0 acceptance criteria, design Section 12 row, gathered in one place so
`pytest -m acceptance` (the eventual M15 harness convention) has a single
module to point at for this build's sign-off. Each test below duplicates a
narrower, already-covered assertion from its dedicated test module -- that
duplication is deliberate: this file IS the acceptance-criteria mapping.

    | Acceptance criterion                                            | Test |
    |------------------------------------------------------------------|------|
    | pip install -e . on Win                                          | test_editable_install_is_importable |
    | trialerror --version emits a valid envelope                           | test_trialerror_dash_dash_version_emits_valid_envelope |
    | atomic-write survives kill-mid-write test                        | test_atomic_write_survives_kill_mid_write (see tests/test_atomic.py) |
    | trialerror doctor --license-audit fails on a headerless vendored fixture | test_doctor_license_audit_fails_on_headerless_fixture |
    | a fixture CLI group auto-registers without touching shared files | test_fixture_cli_group_autoregisters (see tests/test_cli_group_autodiscovery.py) |
"""

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance


def test_editable_install_is_importable():
    version = importlib.metadata.version("trialerror")
    assert version == "0.1.0"
    import trialerror  # noqa: F401 - the assertion is that this import succeeds at all

    assert trialerror.__version__ == version


def test_trialerror_dash_dash_version_emits_valid_envelope():
    scripts_dir = Path(sys.executable).parent
    exe = next(
        (c for c in (scripts_dir / "trialerror.exe", scripts_dir / "trialerror") if c.exists()),
        None,
    ) or shutil.which("trialerror")
    if not exe:
        pytest.skip("trialerror console script not found; run `pip install -e .` first")

    result = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    env = json.loads(result.stdout.strip().splitlines()[-1])
    assert env["ok"] is True
    assert env["command"] == "version"
    assert set(env) == {"ok", "command", "protocolVersion", "result", "nextActions", "meta"}


def test_doctor_license_audit_fails_on_headerless_fixture(tmp_path):
    from trialerror.cli import main

    vroot = tmp_path / "vendored"
    item = vroot / "some-lib"
    item.mkdir(parents=True)
    (item / "adapted.py").write_text("print('no header')\n", encoding="utf-8")
    (vroot / "VENDORED.md").write_text("# manifest\n", encoding="utf-8")

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["doctor", "--license-audit", "--vendored-root", str(vroot)])

    assert rc != 0
    env = json.loads(buf.getvalue().strip())
    assert env["ok"] is False
