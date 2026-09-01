"""Every runtime-read data file must be declared as package data.

This is the FX-9 class of bug, which has now bitten twice: a module reads a
non-``.py`` file from its own package directory at runtime, ``pyproject.toml``
does not declare it under ``[tool.setuptools.package-data]``, and the gap is
invisible for as long as everyone develops and CI-tests against an *editable*
install -- which reads straight from the source tree. It only surfaces when
someone builds a real wheel, at which point the file is simply absent.

FX-9 caught it for ``trialerror/artifacts/templates/*.md``. The dashboard's
``static/*`` had the same hole: ``trialerror dashboard serve`` and
``dashboard export`` both read ``dashboard.html``/``dashboard.css`` from
``STATIC_DIR``, and a built wheel contained neither.

Asserting against the declaration (rather than building a wheel, which is
slow and needs network for build deps) keeps this cheap enough to run every
time. The pairing it enforces is: if a package directory holds files the code
reads at runtime, that package needs a package-data line.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def package_data() -> dict:
    with PYPROJECT.open("rb") as fh:
        config = tomllib.load(fh)
    return config["tool"]["setuptools"]["package-data"]


def test_dashboard_static_assets_are_declared(package_data):
    assert "trialerror.dashboard" in package_data, (
        "trialerror/dashboard/static/* is read at runtime by serve.py and export.py "
        "but is not declared as package data -- a built wheel will not contain it"
    )
    patterns = package_data["trialerror.dashboard"]
    assert any(p.startswith("static/") for p in patterns), patterns


def test_artifact_templates_are_declared(package_data):
    """The original FX-9 guard, kept alongside its sibling."""
    assert "trialerror.artifacts" in package_data
    assert any(p.startswith("templates/") for p in package_data["trialerror.artifacts"])


def test_every_declared_pattern_actually_matches_something(package_data):
    """A stale pattern is as bad as a missing one -- it reads as coverage
    while shipping nothing."""
    for package, patterns in package_data.items():
        package_dir = REPO_ROOT / Path(*package.split("."))
        assert package_dir.is_dir(), f"{package} does not exist at {package_dir}"
        for pattern in patterns:
            assert list(package_dir.glob(pattern)), (
                f"{package} declares package-data {pattern!r} but nothing matches it"
            )


def test_dashboard_static_files_the_code_reads_are_present():
    """Names the two files by hand, so renaming one without updating the
    reader (or this list) fails loudly rather than at first HTTP request."""
    from trialerror.dashboard.serve import STATIC_DIR

    for name in ("dashboard.html", "dashboard.css"):
        assert (Path(STATIC_DIR) / name).is_file(), f"{name} missing from {STATIC_DIR}"
