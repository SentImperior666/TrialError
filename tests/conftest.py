"""Shared fixtures for the M1 (``trialerror.stores``) test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from trialerror.stores.store import Store, open_store

#: This machine's real home, captured when conftest is imported -- before any
#: fixture or patch has run -- for the negative checks that compare against it
#: (see the ``real_home`` fixture). Nothing else may use it.
REAL_HOME = Path.home()


def _home_fallback_env(home: Path) -> dict[str, str]:
    """``HOMEDRIVE`` and ``HOMEPATH`` for a temporary ``home``, on Windows only.

    Python's home lookup falls back to ``HOMEDRIVE`` + ``HOMEPATH`` when
    ``USERPROFILE`` is missing. Left real, a test that deletes ``USERPROFILE``
    (or hands a child an environment copied without it) would resolve the
    machine's real home. Off Windows the variables mean nothing."""
    if sys.platform != "win32":
        return {}
    drive, path = os.path.splitdrive(str(home))
    return {"HOMEDRIVE": drive, "HOMEPATH": path}


def _known_folder_env(home: Path) -> dict[str, str]:
    """The per-user data folders under a temporary ``home``, created, as the
    environment that points at them.

    ``LOCALAPPDATA`` and ``APPDATA`` are what harness code (the offload worker
    lock, the vast.ai ledger, the ingest slot lock) reads to decide where its
    machine-local state lives, so left alone they stay the REAL ones. And a
    Windows known-folder lookup (pip's cache dir, say) expands
    ``%USERPROFILE%/AppData/Local`` from the environment: with the folder
    missing under a temporary ``USERPROFILE`` the lookup fails, and pip falls
    back to a cache directory relative to the working directory (a stray
    ``pip/cache`` in the worktree). Creating both folders closes that too.
    Off Windows the equivalent state, cache, config and data roots are the four
    ``XDG_*`` variables."""
    local = home / "AppData" / "Local"
    roaming = home / "AppData" / "Roaming"
    local.mkdir(parents=True, exist_ok=True)
    roaming.mkdir(parents=True, exist_ok=True)
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    if sys.platform != "win32":
        env["XDG_STATE_HOME"] = str(home / ".local" / "state")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    return env


@pytest.fixture(scope="session", autouse=True)
def _isolate_machine_state(tmp_path_factory):
    """One session-wide temporary ``HOME``, ``USERPROFILE`` and platform root,
    set up before any module-, class- or function-scoped fixture runs.

    pytest sets up higher-scoped fixtures first, so a module-scoped fixture
    (the e2e journeys' ``corpus_run``, the demo-seed ``seeded``) runs before any
    function-scoped autouse fixture below has redirected anything. Such a
    fixture used to run with the machine's real home, and the real SessionStart
    hook it drove appended test records to the real ``~/.trialerror/probes/
    hook_events.jsonl``. Here every scope sees a temporary home, and subprocesses
    inherit it through ``os.environ``.

    The four function-scoped fixtures below stay: they give each test its own
    fresh directories on top of this session-wide floor. Variables the code
    under test reads to decide whose data it is handling are removed."""
    with pytest.MonkeyPatch.context() as mp:
        home = tmp_path_factory.mktemp("home")
        mp.setenv("HOME", str(home))
        mp.setenv("USERPROFILE", str(home))
        mp.setenv("TRIALERROR_PLATFORM_ROOT", str(home / ".trialerror"))
        mp.setenv("TRIALERROR_QUOTA_DIR", str(home / ".trialerror" / "quota"))
        mp.setenv("TRIALERROR_PROBES_DIR", str(home / ".trialerror" / "probes"))
        for name, value in {**_known_folder_env(home), **_home_fallback_env(home)}.items():
            mp.setenv(name, value)
        for var in ("TRIALERROR_ACCOUNT", "TRIALERROR_PROGRAM_ROOT", "TRIALERROR_HOST", "CLAUDE_CONFIG_DIR"):
            mp.delenv(var, raising=False)
        yield home


@pytest.fixture(scope="session")
def real_home() -> Path:
    """This machine's real home directory, for the tests that assert an
    isolated path is NOT under it. Never read or write anything below it."""
    return REAL_HOME


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


@pytest.fixture(autouse=True)
def _isolated_probes_dir(tmp_path_factory, monkeypatch) -> Path:
    """An isolated probe-log dir for EVERY test, via ``TRIALERROR_PROBES_DIR``
    — never the real machine's shared ``~/.trialerror/probes/
    hook_events.jsonl``.

    ``trialerror.hooks.probe_log.append_hook_record`` is called from
    ``spawn_gate.py``, ``post_task.py`` and ``session_start.py`` on every
    invocation, and none of those existing hook test suites set
    ``TRIALERROR_PROBES_DIR`` themselves (their subprocesses inherit
    ``os.environ`` verbatim). Without this fixture, running the suite
    appends real-looking synthetic records to the machine's actual,
    shared probe log — the same file ``trialerror probes run``'s
    ``hook_payload_keys`` check reads for evidence of Claude Code drift.
    Reproduced live: an unisolated run of the existing hook suites wrote
    exactly 18 ``spawn_gate`` + 2 ``post_task`` lines (none carrying
    ``session_id``) directly into that real file — a result later
    misread as a genuine ``hook_payload_keys`` finding, when it was this
    test-isolation gap. Same pattern as ``_isolated_quota_dir`` above; a
    test that is ABOUT the real value (there are none) would set it
    itself, which wins over this one."""
    probes_dir = tmp_path_factory.mktemp("probes_capture")
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(probes_dir))
    return probes_dir


@pytest.fixture(autouse=True)
def _isolated_default_platform_root(tmp_path_factory, monkeypatch) -> Path:
    """A temporary platform root AND home directory for EVERY test, via
    ``TRIALERROR_PLATFORM_ROOT`` and ``HOME``/``USERPROFILE`` -- never this
    machine's real ``~/.trialerror``.

    Incident, 2026-09-27: at 15:46Z a test in the full suite opened this
    laptop's REAL ``~/.trialerror/platform.db`` and applied a
    migration to it. The pre-existing ``platform_root`` fixture below is
    opt-in -- a test only gets it by naming it as an argument -- and several
    tests (e.g. ``test_lens_cli.py``, ``test_lens_slice_salt.py``) call
    ``open_store(some_root)`` with no ``platform_root=`` at all, which falls
    through to ``trialerror.stores.paths.platform_root()``'s own default:
    ``TRIALERROR_PLATFORM_ROOT`` if set, else ``Path.home() / ".trialerror"``.
    With nothing isolating either half of that fallback, such a test answers
    about THIS machine's real money store -- the same class of gap
    ``_isolated_quota_dir``/``_isolated_probes_dir`` above close for the
    quota capture and the hook probe log.

    Redirecting ``HOME``/``USERPROFILE`` too (not just the env var) means
    the fallback itself lands somewhere temporary even for a test that
    clears ``TRIALERROR_PLATFORM_ROOT`` on purpose -- see
    ``test_stores_checks.py::test_platform_root_precedence_explicit_over_
    env_over_default``, which does exactly that and compares against a
    freshly-read ``Path.home()``: both sides read the same (isolated) value,
    so that test's own behavior is unchanged.

    A test that wants a SPECIFIC platform root still uses the ``platform_root``
    fixture below (or its own ``tmp_path``), whose ``monkeypatch.setenv`` runs
    after this one and so wins, exactly like ``_isolated_quota_dir``'s own
    rule. A test that is ABOUT the default-root resolution itself sets its
    own ``HOME``/``USERPROFILE`` deliberately (there are none today; this
    fixture's isolated home already serves that role for every test)."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name, value in {**_known_folder_env(home), **_home_fallback_env(home)}.items():
        monkeypatch.setenv(name, value)
    default_root = tmp_path_factory.mktemp("platform_root_default")
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(default_root))
    return default_root


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
