"""Shared fixtures for the M1 (``trialerror.stores``) test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trialerror.stores.store import Store, open_store


@pytest.fixture()
def platform_root(tmp_path, monkeypatch) -> Path:
    """An isolated platform root for this test, via ``TRIALERROR_PLATFORM_ROOT``
    — never the real developer's ``~/.trialerror``."""
    root = tmp_path / "platform_root"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(root))
    return root


@pytest.fixture()
def program_root(tmp_path) -> Path:
    root = tmp_path / "program"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def store(platform_root, program_root) -> Store:
    s = open_store(program_root, platform_root=platform_root)
    yield s
    s.close()
