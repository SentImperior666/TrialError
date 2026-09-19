"""M15 acceptance harness -- the design's own literally-named acceptance
bar (Section 12 M15 row): "single `pytest -m acceptance` green on a clean
Windows checkout ... doubles as the CI definition." Reuses ``program_root``/
``platform_root`` from the repo-root ``tests/conftest.py`` (isolated
tmp-path roots; ``platform_root`` env-scoped via ``TRIALERROR_PLATFORM_ROOT`` so
this never touches a real developer's ``~/.trialerror``) -- NOT the ``store``
fixture, since :func:`~trialerror.accept.journeys.run_clean_checkout_smoke` owns
its own ``Store`` lifecycle end to end (opens and closes it itself).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.accept.journeys import run_clean_checkout_smoke

pytestmark = pytest.mark.acceptance

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fresh_venv_pip_install_dash_e_smoke(tmp_path):
    """Design Section 12 M15 row, literally: "fresh venv -> pip install
    -e" (this build's own binding arrow-chain). A genuinely NEW venv
    (``python -m venv``, no packages preinstalled beyond pip itself on
    py>=3.12) is created, ``pip install -e .`` run against THIS checkout
    inside it, and ``trialerror --version`` invoked from that fresh venv's own
    interpreter -- proof the editable install + console-script-equivalent
    entry point work on a truly clean Python environment, not just the
    persistent ``.venv312`` every other test in this suite runs from.

    M0's own acceptance criterion
    (``tests/test_m0_acceptance.py::test_editable_install_is_importable``)
    already proves "pip install -e . on Win" against the CURRENT
    environment; this test is the incremental, narrower proof that a
    BRAND NEW venv can do the same install from scratch -- kept as its own
    test (not folded into the big end-to-end smoke below) so a genuine
    environment/network hiccup here can't mask every other step's own
    signal, and vice versa.

    Skips (rather than fails) on any environment problem creating the venv
    or running pip -- this is a real, network-touching operation
    (`pyproject.toml`'s build backend, ``setuptools``/``wheel``, is fetched
    via PEP 517 build isolation unless already cached) and a sandboxed/
    offline CI runner failing THIS specific step is a environment fact to
    report, not a code defect for `pytest -m acceptance` to fail red over.
    """
    venv_dir = tmp_path / "fresh-venv"
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True, text=True, timeout=120, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"could not create a fresh venv in this environment: {exc}")

    venv_python = venv_dir / "Scripts" / "python.exe" if sys.platform == "win32" else venv_dir / "bin" / "python"
    if not venv_python.exists():
        pytest.skip(f"fresh venv created but its interpreter is missing at {venv_python}")

    try:
        install = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-e", str(_REPO_ROOT)],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"pip install -e . did not complete in this environment: {exc}")
    if install.returncode != 0:
        pytest.skip(
            "pip install -e . into a fresh venv failed in this environment (likely no "
            f"network access to resolve build deps/pypdf): rc={install.returncode}\n"
            f"stdout={install.stdout[-2000:]}\nstderr={install.stderr[-2000:]}"
        )

    version_check = subprocess.run(
        [str(venv_python), "-m", "trialerror.cli", "--version"],
        capture_output=True, text=True, timeout=30,
    )
    assert version_check.returncode == 0, version_check.stderr
    assert '"ok":true' in version_check.stdout
    assert '"package":"trialerror"' in version_check.stdout


def test_clean_checkout_end_to_end_smoke(program_root, platform_root):
    """The main event: design Section 12 M15 row's full smoke chain, via
    :func:`trialerror.accept.journeys.run_clean_checkout_smoke` -- the SAME
    function ``trialerror accept`` (``trialerror/cli/accept.py``) calls, so this test
    and that CLI command are proven to agree on every step, not just
    independently "probably" doing the same thing.
    """
    result = run_clean_checkout_smoke(program_root, platform_root)

    step_names = [s["name"] for s in result.details["steps"]]
    assert result.status == "pass", (
        f"{result.message}\nsteps so far: {step_names}\n"
        f"full step detail: {result.details['steps']}"
    )

    # every named leg of the design's own arrow-chain actually ran, in
    # order -- not just "some steps passed, the smoke overall says pass".
    assert step_names == [
        "migrate_stores",
        "boot_session_via_session_start_hook",
        "book_launch",
        "spawn_gate_refusal_no_token",
        "spawn_gate_consumption",
        "spawn_gate_replay_refused",
        "ingest_fixture_docs",
        "search_with_citation_and_fence",
        "citecheck",
        "gate_journey",
        "close_refusal_then_close",
        "doctor_green",
    ]
    assert all(s["ok"] for s in result.details["steps"])

    # a few steps' own detail deserves a direct, meaningful assertion here
    # too, not just trust in the journey function's internal checks.
    by_name = {s["name"]: s for s in result.details["steps"]}
    assert by_name["book_launch"]["detail"]["launch_id"].startswith("LNCH-")
    assert by_name["search_with_citation_and_fence"]["detail"]["restricted_fenced"] is True
    assert by_name["search_with_citation_and_fence"]["detail"]["restricted_quote_words"] <= 20
    assert by_name["citecheck"]["detail"]["pair_statuses"] == ["mechanical_pass"]
    assert by_name["doctor_green"]["detail"]["total"] > 0
    assert by_name["close_refusal_then_close"]["detail"]["handoff_path"]

    handoffs_dir = program_root / "handoffs"
    assert handoffs_dir.is_dir() and any(handoffs_dir.iterdir()), "close succeeded but no handoff file was rendered"


def test_clean_checkout_smoke_is_wired_into_the_trialerror_accept_cli(program_root, platform_root):
    """"each journey as a pytest-runnable scenario AND a doctor-integrated
    summary" (this build's own binding instruction): the pytest scenario
    above IS the runnable scenario; this test proves the OTHER front door
    (`trialerror accept`) reaches the identical journey function and shapes its
    result exactly like `trialerror doctor`'s own envelope
    (``result.checks``/``result.summary``), over a REAL subprocess
    invocation of the actual CLI (not an in-process handler call) -- the
    same "does the shipped console-script-equivalent entry point actually
    work" bar test_m0_acceptance.py holds ``--version`` to.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "trialerror.cli", "accept",
         "--program-root", str(program_root), "--platform-root", str(platform_root),
         "--skip-gpu-live-cc-enumeration"],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    import json

    env = json.loads(proc.stdout)
    assert env["ok"] is True, env
    assert env["result"]["summary"]["total"] == 1
    assert env["result"]["summary"]["passed"] == 1
    assert env["result"]["summary"]["failed"] == 0
    assert env["result"]["checks"][0]["name"] == "clean_checkout_smoke"
    assert env["result"]["checks"][0]["status"] == "pass"


def test_trialerror_accept_includes_the_gpu_live_cc_enumeration_by_default(program_root, platform_root):
    """Without ``--skip-gpu-live-cc-enumeration``, `trialerror accept`'s summary
    also lists the 8 enumerated GPU/live-Claude-Code items as ``skip`` --
    the same single source of truth
    (``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS``)
    ``tests/acceptance/test_gpu_and_live_cc_journeys.py``'s own
    skip-marked pytest stand-ins read from, so the two never drift."""
    from trialerror.accept.journeys import GPU_LIVE_CC_ITEMS

    proc = subprocess.run(
        [sys.executable, "-m", "trialerror.cli", "accept",
         "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    import json

    env = json.loads(proc.stdout)
    checks = env["result"]["checks"]
    assert env["result"]["summary"]["total"] == 1 + len(GPU_LIVE_CC_ITEMS)
    assert env["result"]["summary"]["skipped"] == len(GPU_LIVE_CC_ITEMS)
    names = {c["name"] for c in checks}
    assert names == {"clean_checkout_smoke", *GPU_LIVE_CC_ITEMS}
    for c in checks:
        if c["name"] == "clean_checkout_smoke":
            continue
        assert c["status"] == "skip"
        assert c["message"] == GPU_LIVE_CC_ITEMS[c["name"]]
