"""Shared fixtures for the M1 (``trialerror.stores``) test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trialerror.stores.store import Store, open_store


@pytest.fixture(autouse=True)
def _neutral_numpy_fastpath(monkeypatch) -> None:
    """``TRIALERROR_NUMPY_FASTPATH`` unset for EVERY test.

    Lane FB-7 fix pass, V-10. The knob's own process-wide lever is
    documented in ``USER_SETUP`` §3i, and setting the documented lever
    turned the lane's targeted suite red: one test asserted
    ``numpy_fastpath_mode(None) == "auto"`` without clearing the
    environment, and several others take the fast path as a given because
    numpy is installed here. A suite whose answer depends on an ambient
    environment variable is answering about the shell it was run from --
    the same argument ``_isolated_quota_dir`` below makes about a capture
    file, and ``platform_root`` makes about a developer's home directory.

    A test that is ABOUT the lever sets it itself with ``monkeypatch``,
    which wins over this one."""
    monkeypatch.delenv("TRIALERROR_NUMPY_FASTPATH", raising=False)


@pytest.fixture(autouse=True)
def _isolated_quota_dir(tmp_path_factory, monkeypatch) -> Path:
    """An isolated plan-quota capture dir for EVERY test, via
    ``TRIALERROR_QUOTA_DIR`` — never the machine's own statusLine capture.

    Lane FB-3 fix pass, V-1. The booking surfaces grew a staleness gate
    (``trialerror budget book`` / the ``book_launch`` MCP tool), and that
    gate reads a file on disk. With nothing isolating the capture dir, the
    booking tests answered about whatever the machine running them last
    captured: green on a laptop whose statusLine is live, red on the same
    commit fifteen minutes later. Autouse and unconditional, exactly like
    ``platform_root``'s reason for existing — a test must never read a
    developer's ``~/.trialerror``.

    The directory is left EMPTY: an absent capture is not stale (see
    :func:`trialerror.budget.quota.staleness`), so the default for every
    test is "no reading", which is the only state that cannot age. A test
    that wants a capture writes one into a dir of its own and points the
    variable at it (``tests/test_budget_quota_staleness.py`` does), and its
    own ``monkeypatch.setenv`` wins over this one."""
    # 2026-09-16: a separate factory dir, never the test's own tmp_path -- a
    # test that asserts tmp_path is otherwise empty (tests/test_atomic.py) saw
    # this directory as a leftover after the lane landed.
    quota_dir = tmp_path_factory.mktemp("quota_capture")
    quota_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))
    return quota_dir


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
