"""Test isolation holds at EVERY fixture scope, not only for function-scoped ones.
It covers the per-user data folders too: ``LOCALAPPDATA``, ``APPDATA`` and, off
Windows, the ``XDG_*`` roots, which decide where harness state (the offload
lock, the vast.ai ledger) and pip's cache land.

pytest sets up higher-scoped fixtures before lower-scoped ones. Isolation that
lives only in function-scoped autouse fixtures is therefore invisible to a
module- or class-scoped fixture, which runs first and sees the machine's real
home folder. ``tests/conftest.py``'s session-scoped ``_isolate_machine_state``
closes that gap; this file proves it from a session-, a module-, a class- and a
function-scoped fixture, and from a child process.

"Isolated" means two things, both checked: the path is NOT this machine's real
home (nor, for the state directories, anything under its real ``.trialerror``),
and it IS under pytest's own temporary base. "Not under the real home" alone
would be wrong on Windows and on most laptops: the temporary base itself lives
inside the user profile.

Each recording fixture snapshots the environment at the moment ITS scope is set
up, and the tests then assert on the snapshot -- an assertion made inside a test
body would only ever prove the function scope.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

#: The variables that decide where a test's machine-level state lands.
_HOME_VARS = (
    "HOME",
    "USERPROFILE",
    "TRIALERROR_PLATFORM_ROOT",
    "TRIALERROR_PROBES_DIR",
    "TRIALERROR_QUOTA_DIR",
    "LOCALAPPDATA",
    "APPDATA",
)
#: The per-user roots the XDG convention uses instead, off Windows.
_XDG_VARS = ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
_ROOT_VARS = _HOME_VARS + (_XDG_VARS if sys.platform != "win32" else ())
#: Variables that select WHOSE data the code under test handles; none may leak in.
#: ``CLAUDE_CONFIG_DIR`` is set per account by a deployment, and both the capture's
#: account hint and the default transcripts lookup read it.
_IDENTITY_VARS = ("TRIALERROR_ACCOUNT", "TRIALERROR_PROGRAM_ROOT", "TRIALERROR_HOST", "CLAUDE_CONFIG_DIR")


def _norm(path: str | Path) -> Path:
    return Path(os.path.normcase(os.path.realpath(str(path))))


def _is_or_under(path: str | Path, root: Path) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or r in p.parents


def _snapshot() -> dict:
    from trialerror.offload.lock import worker_state_dir

    snap = {name: os.environ.get(name) for name in _ROOT_VARS + _IDENTITY_VARS + ("HOMEDRIVE", "HOMEPATH")}
    snap["Path.home()"] = str(Path.home())
    snap["expanduser"] = os.path.expanduser("~")
    snap["worker_state_dir()"] = str(worker_state_dir())
    return snap


def _is_real_machine_state(path: str | Path, real_home: Path) -> bool:
    """The real home itself, or anything under the real ``.trialerror``."""
    return _norm(path) == _norm(real_home) or _is_or_under(path, real_home / ".trialerror")


def _assert_isolated(snap: dict, real_home: Path, basetemp: Path, scope: str) -> None:
    for name in _ROOT_VARS:
        assert snap[name], f"{name} is unset at {scope} scope"
    for key in _ROOT_VARS + ("Path.home()", "expanduser", "worker_state_dir()"):
        assert not _is_real_machine_state(snap[key], real_home), (
            f"{key}={snap[key]!r} is the real home or its .trialerror at {scope} scope"
        )
        assert _is_or_under(snap[key], basetemp), f"{key}={snap[key]!r} is not under pytest's temp base at {scope} scope"
    # The two AppData folders exist, under the temporary profile: a Windows
    # known-folder lookup expands them from ``%USERPROFILE%`` and fails when
    # they are missing.
    for var, leaf in (("LOCALAPPDATA", "Local"), ("APPDATA", "Roaming")):
        assert Path(snap[var]).is_dir(), f"{var}={snap[var]!r} does not exist at {scope} scope"
        assert _norm(snap[var]) == _norm(Path(snap["USERPROFILE"]) / "AppData" / leaf), (
            f"{var}={snap[var]!r} is not under USERPROFILE={snap['USERPROFILE']!r} at {scope} scope"
        )
    if sys.platform == "win32":
        # Python's fallback when USERPROFILE is missing.
        fallback = snap["HOMEDRIVE"] + snap["HOMEPATH"]
        assert _norm(fallback) == _norm(snap["USERPROFILE"]), (
            f"HOMEDRIVE+HOMEPATH={fallback!r} is not USERPROFILE={snap['USERPROFILE']!r} at {scope} scope"
        )
    for name in _IDENTITY_VARS:
        assert snap[name] is None, f"{name} leaked in at {scope} scope"


@pytest.fixture(scope="session")
def session_snapshot() -> dict:
    return _snapshot()


@pytest.fixture(scope="module")
def module_snapshot() -> dict:
    return _snapshot()


@pytest.fixture()
def function_snapshot() -> dict:
    return _snapshot()


@pytest.fixture(scope="class")
def class_snapshot() -> dict:
    return _snapshot()


def test_real_home_is_not_itself_a_temporary_home(real_home, tmp_path_factory):
    """The negative checks below only mean something if ``real_home`` really is
    the pre-isolation home, and not a temporary one."""
    assert not _is_or_under(real_home, tmp_path_factory.getbasetemp())
    assert _norm(os.environ["HOME"]) != _norm(real_home)


def test_session_scope_sees_a_temporary_home(session_snapshot, real_home, tmp_path_factory):
    _assert_isolated(session_snapshot, real_home, tmp_path_factory.getbasetemp(), "session")


def test_module_scope_sees_a_temporary_home(module_snapshot, real_home, tmp_path_factory):
    _assert_isolated(module_snapshot, real_home, tmp_path_factory.getbasetemp(), "module")


def test_function_scope_sees_a_temporary_home(function_snapshot, real_home, tmp_path_factory):
    _assert_isolated(function_snapshot, real_home, tmp_path_factory.getbasetemp(), "function")


class TestClassScope:
    def test_class_scope_sees_a_temporary_home(self, class_snapshot, real_home, tmp_path_factory):
        _assert_isolated(class_snapshot, real_home, tmp_path_factory.getbasetemp(), "class")

    def test_a_second_test_in_the_class_reuses_the_snapshot(self, class_snapshot, real_home, tmp_path_factory):
        _assert_isolated(class_snapshot, real_home, tmp_path_factory.getbasetemp(), "class")


def test_a_subprocess_sees_a_temporary_home(real_home, tmp_path_factory):
    """A child process inherits ``os.environ`` -- the shape every hook and CLI
    subprocess test uses -- so its own idea of the home must be temporary too."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from pathlib import Path; print(Path.home()); print(os.path.expanduser('~'))",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 2, proc.stdout
    for line in lines:
        assert line.strip()
        assert not _is_real_machine_state(line.strip(), real_home), f"the child's home is the real home: {line!r}"
        assert _is_or_under(line.strip(), tmp_path_factory.getbasetemp()), f"the child's home is not temporary: {line!r}"


def test_pip_keeps_its_cache_under_the_temporary_profile(tmp_path_factory):
    """pip's cache dir comes from a known-folder lookup of ``%USERPROFILE%``. With
    the AppData folder missing it fell back to ``./pip/cache``, relative to the
    working directory, and left a ``pip/`` folder in the worktree."""
    proc = subprocess.run(
        [sys.executable, "-c", "from pip._internal.locations import USER_CACHE_DIR; print(USER_CACHE_DIR)"],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"pip is not importable here: {proc.stderr[-300:]}")
    cache_dir = proc.stdout.strip()
    assert os.path.isabs(cache_dir), f"pip's cache dir is relative to the working directory: {cache_dir!r}"
    assert _is_or_under(cache_dir, tmp_path_factory.getbasetemp()), f"pip's cache dir is not temporary: {cache_dir!r}"


@pytest.mark.skipif(sys.platform != "win32", reason="HOMEDRIVE/HOMEPATH are the Windows home fallback")
def test_a_child_without_userprofile_still_gets_a_temporary_home(real_home, tmp_path_factory):
    """Python falls back to HOMEDRIVE + HOMEPATH when USERPROFILE is missing (a
    test that deletes it, or a child environment copied without it)."""
    env = {k: v for k, v in os.environ.items() if k not in ("USERPROFILE", "HOME")}
    proc = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    home = proc.stdout.strip()
    assert not _is_real_machine_state(home, real_home), f"the fallback home is the real home: {home!r}"
    assert _is_or_under(home, tmp_path_factory.getbasetemp()), f"the fallback home is not temporary: {home!r}"
