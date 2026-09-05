"""POSIX detachment tests for the three ``DETACHED_PROCESS``/``start_new_
session`` sites (LANE0_SANDBOX_RELOCATION_DESIGN.md Sec 6 item 1;
INTEGRATION_NOTES.md / FACTS_harness_runtime.md Sec 2 + Sec 8, FACTS_sandbox_
host.md Sec 4): ``trialerror/jobs/worker.py::spawn_worker``,
``trialerror/cli/dashboard.py``'s ``dashboard serve`` detached branch, and
``trialerror/cli/obs.py``'s ``obs start-phoenix``. All three share the
identical shape::

    if sys.platform == "win32":
        popen_kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:  # pragma: no cover
        popen_kwargs["start_new_session"] = True

The ``else`` arm (``setsid`` via ``start_new_session=True``) was never
exercised by any test in this build -- no POSIX CI lane existed
(FACTS_harness_runtime.md Sec 8: "untested but present"). This file
exercises it for real, on real POSIX process groups: a real GRANDCHILD
process, spawned by a real DRIVER "parent" subprocess (itself its own
session/process-group leader, so signalling it can never touch pytest's own
process group), must (a) survive the driver's own normal exit, and (b)
survive a SIGINT delivered to the driver's entire process group -- the two
properties design Sec 6 item 1 names, and the exact guarantee the "long
jobs are detached CLI workers" contract (design Sec 4.4) depends on.

Every driver is a real, standalone ``.py`` file run via ``sys.executable``
(the same interpreter pytest itself runs under -- correct venv on either
DEV or the sandbox) with ``start_new_session=True`` at Popen time, so the
driver is ALWAYS its own process-group leader (``driver.pid == its own
pgid``) regardless of what pytest's own group looks like -- ``os.killpg
(driver.pid, ...)`` below is therefore scoped to exactly the driver (and
whatever it forks WITHOUT its own ``setsid``), never anything else.

Skips cleanly on win32: this whole ``else`` branch never runs there
(``DETACHED_PROCESS`` is the win32 arm, already covered by this build's
pre-existing Windows-only tests -- ``tests/test_jobs_worker.py``,
``tests/test_obs_cli.py`` -- which this file does not duplicate).

TRIALERROR-DEV-NOTE (review L0D-2 / L0D-11 fix): the three
``*_survives_parent_process_exit`` tests originally only asserted the
detached child was still alive after its driver exited normally -- but on
POSIX a child always outlives its parent's ORDINARY exit whether or not
``start_new_session`` ran (there is no orphan-killing on POSIX the way
Windows job objects can do it), so that alone discriminates nothing about
``start_new_session`` specifically. Every test below now additionally
asserts ``os.getpgid(pid) != driver.pid`` right after the pidfile appears
(the driver is its own session/group leader, so its pgid IS its pid --
see the docstring above): with ``start_new_session=True`` the child starts
its OWN session and its pgid is its own pid, always different from the
driver's; flipping that kwarg to ``False`` at any of the three sites makes
this assertion fail immediately (mutation-tested on a real Linux host),
independent of
any signal or exit timing. This also makes the SIGINT tests' "still alive"
assertion timing-free (L0D-11): the pgid check does not depend on how far
the child got unwinding a signal it should never have received.

TRIALERROR-DEV-NOTE (obs site only): the obs driver monkeypatches
``subprocess.Popen`` INSIDE its own throwaway process before calling
``trialerror.cli.obs._cmd_start_phoenix`` -- substituting the argv
(``[python, "-c", "import time; time.sleep(...)"]`` in place of ``["-m",
"phoenix.server.main", "serve"]``) while forwarding every kwarg (``cwd``,
``stdin``, ``stdout``, ``stderr``, and -- the one this file actually cares
about -- ``start_new_session``) unchanged to the REAL ``subprocess.Popen``.
This exercises the exact ``sys.platform``-gated kwargs and the exact
``subprocess.Popen(...)`` call ``trialerror/cli/obs.py`` makes, without
depending on the optional ``obs`` extra being installed (the ubuntu CI lane
this design adds runs ``[dev]`` + tantivy, not ``[obs]`` -- FACTS confirms
``arize-phoenix``'s own dependency tree is ~140 packages, deliberately kept
out of ``dev``) and without risking a real ``phoenix serve`` colliding with
one already running on a shared sandbox host -- the same "never launches a
real phoenix serve" discipline ``tests/test_obs_cli.py`` already documents
for its own (fully-mocked) coverage, just with a REAL child process this
time so real OS-level detachment can be proven.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: start_new_session=True never runs on win32 (the DETACHED_PROCESS "
    "arm is covered by tests/test_jobs_worker.py + tests/test_obs_cli.py instead)",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PIDFILE_TIMEOUT_S = 10.0
_POLL_S = 0.05


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _communicate(driver: subprocess.Popen, timeout: float = _PIDFILE_TIMEOUT_S) -> str:
    """Drain the driver's stdout/stderr (redirected together) and wait for
    it to exit, exactly once.

    L0D-9: a bare ``driver.wait(timeout=...)`` with ``stdout=PIPE`` deadlocks
    the test (rather than failing cleanly) once the driver writes more than
    one pipe buffer (~64KB) of output before exiting, because nothing is
    draining the pipe concurrently with the wait. ``communicate()`` drains
    and waits together. If the driver is still running past ``timeout`` (the
    SIGINT tests expect it to have died; a hang would otherwise be a second,
    unrelated deadlock) it is force-killed so this helper always returns.
    """
    try:
        out, _ = driver.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        driver.kill()
        out, _ = driver.communicate()
    return (out or b"").decode("utf-8", "replace")


def _wait_for_pidfile(path: Path, driver: subprocess.Popen, timeout: float = _PIDFILE_TIMEOUT_S) -> int:
    """Poll for ``path`` to appear, but fail fast (and loudly) if ``driver``
    has already exited without ever writing it.

    L0D-8: previously this raised a bare ``TimeoutError`` with no driver
    diagnostics attached -- on a real import error (hit once while
    reproducing D-posix by hand) that turned an instant, informative crash
    into an opaque 10-second timeout with the actual traceback thrown away.
    Now the driver's own captured output and returncode ride along on
    either failure path.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        if driver.poll() is not None:
            break  # driver already exited -- no point polling further
        time.sleep(_POLL_S)
    output = _communicate(driver, timeout=1.0)
    raise TimeoutError(
        f"{path} never appeared/populated within {timeout}s "
        f"(driver returncode={driver.returncode}):\n{output}"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal -- still "alive"
    return True


def _assert_different_session(pid: int, driver: subprocess.Popen) -> None:
    """L0D-2 / L0D-11: the discriminating assertion. ``driver`` was launched
    with ``start_new_session=True`` itself (see module docstring), so
    ``driver.pid`` IS the driver's own pgid. If the site under test also
    used ``start_new_session=True`` for the grandchild, the grandchild
    became its own session/group leader and its pgid is its own pid --
    always different from the driver's. If that kwarg were ever removed,
    the grandchild would inherit the driver's process group and this
    assertion would fail immediately, independent of any signal or exit
    timing (mutation-tested on a real Linux host: flipping
    ``start_new_session`` to
    ``False`` at any of the three sites fails exactly this assertion)."""
    child_pgid = os.getpgid(pid)
    assert child_pgid != driver.pid, (
        f"child pid {pid} shares the driver's process group (pgid={child_pgid} == "
        f"driver pid {driver.pid}) -- it was not actually placed in its own session "
        "(start_new_session=True not honoured at this site)"
    )


def _wait_until(predicate, timeout: float = 5.0, interval: float = _POLL_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _kill_if_alive(pid: int | None) -> None:
    if pid is None or not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _wait_until(lambda: not _pid_alive(pid), timeout=3.0)


def _kill_driver(driver: subprocess.Popen) -> None:
    """Best-effort cleanup for the driver itself and anything still sitting
    in its process group (L0D-9): every test above kills the known
    grandchild ``pid`` by hand, but if ``_wait_for_pidfile`` raised before a
    pid was ever recorded, ``pid`` stays ``None`` and this is the only
    remaining handle on whatever the driver may have started. Killing the
    driver's whole process group also reaps the driver if a prior
    ``communicate``/``wait`` timed out without one. This cannot reach a
    grandchild that DID complete its own ``setsid`` (by design it is no
    longer in this group) -- an already-detached leak is a known residual
    risk the design's containment layer (snapshots/doctor), not this test
    file, is responsible for catching."""
    try:
        os.killpg(driver.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        driver.wait(timeout=3.0)
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


def _run_driver(tmp_path: Path, body: str, name: str) -> subprocess.Popen:
    """Write ``body`` to a standalone ``.py`` file and launch it as its own
    session/process-group leader -- the "parent" whose exit / SIGINT-to-its-
    group each test below exercises. Never pytest's own process group."""
    script = tmp_path / f"{name}_driver.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(_REPO_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# trialerror/jobs/worker.py :: spawn_worker  (worker.py:357-360)
# ---------------------------------------------------------------------------

_WORKER_DRIVER = """
import sys
sys.path.insert(0, {repo_root!r})
from trialerror.jobs.worker import spawn_worker

handle = spawn_worker(
    program_root={program_root!r},
    platform_root={platform_root!r},
    mode="loop",
    poll_interval_s=1.0,
    max_idle_polls=60,  # long enough that nothing in this test races its own exit
)
with open({pidfile!r}, "w", encoding="utf-8") as f:
    f.write(str(handle.pid))
{tail}
"""


def _worker_driver_body(program_root: Path, platform_root: Path, pidfile: Path, tail: str) -> str:
    return _WORKER_DRIVER.format(
        repo_root=str(_REPO_ROOT),
        program_root=str(program_root),
        platform_root=str(platform_root),
        pidfile=str(pidfile),
        tail=tail,
    )


def test_spawn_worker_child_survives_parent_process_exit(tmp_path):
    """No signal at all -- the driver just spawns and returns/exits normally
    (the real ``trialerror jobs start-worker`` caller shape: fire-and-forget).
    Before this file, this branch was ``pragma: no cover`` -- nothing proved
    the child didn't die as a side effect of its parent's own exit, and (see
    module docstring, L0D-2) "still alive after a normal exit" alone proves
    nothing POSIX-specific -- the pgid check below is what actually pins
    ``start_new_session=True`` to this branch."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    platform_root = tmp_path / "platform"
    pidfile = tmp_path / "worker_pid_exit.txt"
    body = _worker_driver_body(program_root, platform_root, pidfile, tail="")

    driver = _run_driver(tmp_path, body, "worker_exit")
    pid = None
    try:
        pid = _wait_for_pidfile(pidfile, driver)
        _assert_different_session(pid, driver)

        output = _communicate(driver, timeout=_PIDFILE_TIMEOUT_S)
        assert driver.returncode == 0, output
        assert _pid_alive(pid), "detached worker child died along with its already-exited parent"
    finally:
        _kill_if_alive(pid)
        _kill_driver(driver)


def test_spawn_worker_child_survives_sigint_to_parent_process_group(tmp_path):
    """The driver stays alive (``time.sleep``) so a real SIGINT can be
    delivered to its process group while it's still running -- the
    ``os.setsid()``-equivalent (``start_new_session=True``) the child was
    spawned with must mean it is in a DIFFERENT session/group and therefore
    never receives that signal."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    platform_root = tmp_path / "platform"
    pidfile = tmp_path / "worker_pid_sigint.txt"
    body = _worker_driver_body(program_root, platform_root, pidfile, tail="import time\ntime.sleep(30)\n")

    driver = _run_driver(tmp_path, body, "worker_sigint")
    pid = None
    try:
        pid = _wait_for_pidfile(pidfile, driver)
        assert _pid_alive(pid)
        _assert_different_session(pid, driver)

        os.killpg(driver.pid, signal.SIGINT)
        output = _communicate(driver, timeout=_PIDFILE_TIMEOUT_S)
        assert driver.returncode != 0, f"driver should have died to the SIGINT it was sent:\n{output}"

        assert _pid_alive(pid), "detached worker child received (or died from) a SIGINT meant only for its parent's group"
    finally:
        _kill_if_alive(pid)
        _kill_driver(driver)


# ---------------------------------------------------------------------------
# trialerror/cli/dashboard.py :: `dashboard serve` detached branch (dashboard.py:148-150)
# ---------------------------------------------------------------------------

_DASHBOARD_DRIVER = """
import sys, argparse
sys.path.insert(0, {repo_root!r})
from trialerror.cli import dashboard as dash_cli

args = argparse.Namespace(
    port={port!r}, host="127.0.0.1", poll_interval=0.5, debounce=0.5,
    no_watch=True, repo_root=None, log_dir={log_dir!r}, foreground=False,
    program_root={program_root!r}, platform_root={platform_root!r},
)
env = dash_cli._cmd_serve(args)
assert env.get("ok") is True, env
pid = env["result"]["pid"]
with open({pidfile!r}, "w", encoding="utf-8") as f:
    f.write(str(pid))
{tail}
"""


def _dashboard_driver_body(*, port: int, program_root: Path, platform_root: Path, log_dir: Path, pidfile: Path, tail: str) -> str:
    return _DASHBOARD_DRIVER.format(
        repo_root=str(_REPO_ROOT),
        port=port,
        program_root=str(program_root),
        platform_root=str(platform_root),
        log_dir=str(log_dir),
        pidfile=str(pidfile),
        tail=tail,
    )


def _seeded_dashboard_program(tmp_path: Path) -> tuple[Path, Path]:
    from trialerror.stores.store import open_store

    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-posix-detach"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"
    store = open_store(program_root, platform_root=platform_root)
    store.close()
    return program_root, platform_root


def test_dashboard_serve_child_survives_parent_process_exit(tmp_path):
    program_root, platform_root = _seeded_dashboard_program(tmp_path)
    port = _free_port()
    log_dir = tmp_path / "dashboard_logs"
    pidfile = tmp_path / "dashboard_pid_exit.txt"
    body = _dashboard_driver_body(
        port=port, program_root=program_root, platform_root=platform_root, log_dir=log_dir, pidfile=pidfile, tail=""
    )

    driver = _run_driver(tmp_path, body, "dashboard_exit")
    pid = None
    try:
        pid = _wait_for_pidfile(pidfile, driver)
        _assert_different_session(pid, driver)

        output = _communicate(driver, timeout=_PIDFILE_TIMEOUT_S)
        assert driver.returncode == 0, output
        assert _pid_alive(pid), "detached dashboard server died along with its already-exited parent"
    finally:
        _kill_if_alive(pid)
        _kill_driver(driver)


def test_dashboard_serve_child_survives_sigint_to_parent_process_group(tmp_path):
    program_root, platform_root = _seeded_dashboard_program(tmp_path)
    port = _free_port()
    log_dir = tmp_path / "dashboard_logs"
    pidfile = tmp_path / "dashboard_pid_sigint.txt"
    body = _dashboard_driver_body(
        port=port,
        program_root=program_root,
        platform_root=platform_root,
        log_dir=log_dir,
        pidfile=pidfile,
        tail="import time\ntime.sleep(30)\n",
    )

    driver = _run_driver(tmp_path, body, "dashboard_sigint")
    pid = None
    try:
        pid = _wait_for_pidfile(pidfile, driver)
        assert _pid_alive(pid)
        _assert_different_session(pid, driver)

        os.killpg(driver.pid, signal.SIGINT)
        output = _communicate(driver, timeout=_PIDFILE_TIMEOUT_S)
        assert driver.returncode != 0, f"driver should have died to the SIGINT it was sent:\n{output}"

        assert _pid_alive(pid), "detached dashboard server received (or died from) a SIGINT meant only for its parent's group"
    finally:
        _kill_if_alive(pid)
        _kill_driver(driver)


# ---------------------------------------------------------------------------
# trialerror/cli/obs.py :: `obs start-phoenix` (obs.py:138-140)
# ---------------------------------------------------------------------------

_OBS_DRIVER = """
import os, sys, argparse, subprocess
sys.path.insert(0, {repo_root!r})

# See this file's module docstring (TRIALERROR-DEV-NOTE, obs site) for why
# this substitutes the spawned argv rather than really launching `phoenix
# serve`: every kwarg (including start_new_session -- the one under test)
# still reaches the REAL subprocess.Popen unchanged.
_real_popen = subprocess.Popen
def _patched_popen(argv, *a, **kw):
    new_argv = [argv[0], "-c", "import time; time.sleep(30)"]
    return _real_popen(new_argv, *a, **kw)
subprocess.Popen = _patched_popen

# Force `probe_reachable` to see nothing listening, regardless of anything
# real that may already be running on this host -- port 1 is refused.
os.environ["TRIALERROR_OBS_OTLP_ENDPOINT"] = "http://127.0.0.1:1/v1/traces"

from trialerror.cli import obs as obs_cli

args = argparse.Namespace(python_exe=None, platform_root={platform_root!r})
env = obs_cli._cmd_start_phoenix(args)
assert env.get("ok") is True, env
assert env["result"]["already_running"] is False, env
pid = env["result"]["pid"]
with open({pidfile!r}, "w", encoding="utf-8") as f:
    f.write(str(pid))
{tail}
"""


def _obs_driver_body(*, platform_root: Path, pidfile: Path, tail: str) -> str:
    return _OBS_DRIVER.format(repo_root=str(_REPO_ROOT), platform_root=str(platform_root), pidfile=str(pidfile), tail=tail)


def test_obs_start_phoenix_child_survives_parent_process_exit(tmp_path):
    platform_root = tmp_path / "platform"
    pidfile = tmp_path / "obs_pid_exit.txt"
    body = _obs_driver_body(platform_root=platform_root, pidfile=pidfile, tail="")

    driver = _run_driver(tmp_path, body, "obs_exit")
    pid = None
    try:
        pid = _wait_for_pidfile(pidfile, driver)
        _assert_different_session(pid, driver)

        output = _communicate(driver, timeout=_PIDFILE_TIMEOUT_S)
        assert driver.returncode == 0, output
        assert _pid_alive(pid), "detached obs/phoenix child died along with its already-exited parent"
    finally:
        _kill_if_alive(pid)
        _kill_driver(driver)


def test_obs_start_phoenix_child_survives_sigint_to_parent_process_group(tmp_path):
    platform_root = tmp_path / "platform"
    pidfile = tmp_path / "obs_pid_sigint.txt"
    body = _obs_driver_body(platform_root=platform_root, pidfile=pidfile, tail="import time\ntime.sleep(30)\n")

    driver = _run_driver(tmp_path, body, "obs_sigint")
    pid = None
    try:
        pid = _wait_for_pidfile(pidfile, driver)
        assert _pid_alive(pid)
        _assert_different_session(pid, driver)

        os.killpg(driver.pid, signal.SIGINT)
        output = _communicate(driver, timeout=_PIDFILE_TIMEOUT_S)
        assert driver.returncode != 0, f"driver should have died to the SIGINT it was sent:\n{output}"

        assert _pid_alive(pid), "detached obs/phoenix child received (or died from) a SIGINT meant only for its parent's group"
    finally:
        _kill_if_alive(pid)
        _kill_driver(driver)
