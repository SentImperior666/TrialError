"""Lane F-1b stage 3: a recorded pid is only ever trusted together with the
identity recorded beside it, and no wait in ``stop`` is unbounded.

The two blocking findings of ``docs/reviews/VERIFY_f1b-sidecar.md``:

- **V-1** -- ``_process_alive`` was ``os.kill(pid, 0)`` and nothing else, so a
  state file left behind by a previous boot whose pid had been recycled made
  ``status`` adopt a stranger, ``start`` a no-op, and ``stop`` send SIGTERM and
  SIGKILL to a process this program never started.
- **V-2** -- the post-SIGKILL wait had no deadline, and ``os.kill(pid, 0)``
  succeeds for a ZOMBIE, so a corpse made ``stop`` spin forever and
  ``restart = "always"`` never fire.

Everything here runs against real child processes (the one thing a mock cannot
tell the truth about is whether a pid exists) and a fake health seam. Nothing
starts a llama-server, loads a GGUF, or touches whatever sidecar this container
happens to be running.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from trialerror.sidecar import supervisor
from trialerror.sidecar.supervisor import (
    SIDECARS_TABLE,
    read_state,
    sidecar_state_path,
    sidecar_status,
    start_sidecar,
    stop_sidecar,
)

SLEEPER = "import time\nwhile True:\n    time.sleep(0.05)\n"

#: A parent that spawns a child, lets it die, and never reaps it -- the only
#: way to get a real zombie whose pid this test can plant in a state file.
ZOMBIE_MAKER = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'pass'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)

_MINIMAL_CONFIG = '[program]\nid = "PROG-sidecar-identity"\n\n'


def _config(*, code: str = SLEEPER, restart: str = "always", health_url: str | None = None) -> dict:
    table: dict = {"command": [sys.executable, "-c", code], "restart": restart}
    if health_url:
        table["health_url"] = health_url
    return {SIDECARS_TABLE: {"embed": table}}


class FakeHealth:
    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, timeout_s: float) -> int:
        self.calls.append((url, timeout_s))
        return self.status


@pytest.fixture
def program_root(tmp_path: Path) -> Path:
    (tmp_path / "trialerror.toml").write_text(_MINIMAL_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _reap_everything():
    started: list[subprocess.Popen] = []
    yield started
    for child in started:
        try:
            child.kill()
            child.wait(timeout=10)
        except Exception:  # noqa: BLE001 - already gone
            pass
    for pid in list(supervisor._OWN_CHILDREN):
        child = supervisor._OWN_CHILDREN.pop(pid, None)
        if child is None:
            continue
        try:
            child.kill()
            child.wait(timeout=10)
        except Exception:  # noqa: BLE001 - already gone
            pass


def _wait_until(predicate, *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _plant_state(program_root: Path, config: dict, **fields) -> Path:
    path = sidecar_state_path(program_root, "embed", config)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"name": "embed", "restart": "always", **fields}
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _foreign_process(reaper: list) -> subprocess.Popen:
    """A live process this program did NOT start, standing in for whatever
    inherited a recycled pid."""
    child = subprocess.Popen([sys.executable, "-c", SLEEPER])
    reaper.append(child)
    assert _wait_until(lambda: Path(f"/proc/{child.pid}/cmdline").is_file())
    return child


# ---------------------------------------------------------------------------
# V-1: the identity check
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="/proc is the identity source")
def test_start_records_the_kernels_own_facts_about_the_pid(program_root):
    result = start_sidecar(program_root, "embed", config=_config(), _http=FakeHealth())
    state = read_state(program_root, "embed", _config())

    identity = state["identity"]
    assert identity["start_ticks"] == supervisor._process_start_ticks(result["pid"])
    assert identity["cmdline"] == [sys.executable, "-c", SLEEPER]


@pytest.mark.skipif(sys.platform == "win32", reason="/proc is the identity source")
def test_a_recycled_pid_is_not_adopted_by_status(program_root, _reap_everything):
    """The V-1 repro, as a test: a state file whose recorded argv is a
    llama-server and whose pid now belongs to something else."""
    config = _config(restart="never")
    victim = _foreign_process(_reap_everything)
    _plant_state(
        program_root, config,
        pid=victim.pid,
        argv=["/opt/llama.cpp/llama-server", "-m", "/models/an-embedding-model.gguf"],
        restart="never",
    )

    status = sidecar_status(program_root, "embed", config=config, restart_if_dead=False)
    assert status["state"] == "dead" and status["running"] is False
    assert "reused" in status["stale_pid_note"]
    assert victim.poll() is None, "the foreign process was not touched"


@pytest.mark.skipif(sys.platform == "win32", reason="/proc is the identity source")
def test_a_recycled_pid_does_not_make_start_a_no_op(program_root, _reap_everything):
    config = _config()
    victim = _foreign_process(_reap_everything)
    _plant_state(program_root, config, pid=victim.pid, argv=["/opt/llama.cpp/llama-server"])

    result = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert result["started"] is True and result["already_running"] is False
    assert result["pid"] != victim.pid
    assert "reused" in result["stale_pid_note"]
    assert victim.poll() is None
    assert supervisor._process_alive(result["pid"])


@pytest.mark.skipif(sys.platform == "win32", reason="/proc is the identity source")
def test_stop_never_signals_a_pid_that_is_somebody_elses_now(program_root, _reap_everything):
    config = _config()
    victim = _foreign_process(_reap_everything)
    _plant_state(program_root, config, pid=victim.pid, argv=["/opt/llama.cpp/llama-server"])

    result = stop_sidecar(program_root, "embed", config=config, grace_s=1.0)
    assert result["state"] == "stale_pid"
    assert result["stopped"] is False and result["was_running"] is False
    assert "signals" not in result, "nothing may be sent to a pid we do not own"
    assert victim.poll() is None, "the foreign process survived the stop"
    assert read_state(program_root, "embed", config) is None, "the stale file was cleared"


@pytest.mark.skipif(sys.platform == "win32", reason="/proc is the identity source")
def test_a_start_time_that_does_not_match_counts_as_dead_even_for_our_own_child(program_root):
    """The strongest form of the check: the pid, the argv and the Popen object
    all still say "alive", and the recorded start time says otherwise."""
    config = _config(restart="never")
    result = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    path = sidecar_state_path(program_root, "embed", config)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["identity"]["start_ticks"] = int(state["identity"]["start_ticks"]) + 1
    path.write_text(json.dumps(state), encoding="utf-8")

    status = sidecar_status(program_root, "embed", config=config, restart_if_dead=False)
    assert status["state"] == "dead"
    assert supervisor._process_alive(result["pid"]), "the real process is untouched"


@pytest.mark.skipif(sys.platform == "win32", reason="/proc is the identity source")
def test_a_state_file_without_an_identity_falls_back_to_the_recorded_argv(program_root, _reap_everything):
    """A file written before this field existed still gets a check: the
    kernel's argv for the pid against the argv the file records."""
    config = _config()
    victim = _foreign_process(_reap_everything)
    _plant_state(program_root, config, pid=victim.pid, argv=["/opt/llama.cpp/llama-server"])
    state = read_state(program_root, "embed", config)
    assert "identity" not in state

    alive, note = supervisor._recorded_alive(state)
    assert alive is False and "llama-server" in note


def test_a_matching_argv_with_no_recorded_identity_is_still_alive(program_root):
    """The fallback must not turn a running sidecar into a dead one: same
    process, same argv, no identity block."""
    config = _config()
    result = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    path = sidecar_state_path(program_root, "embed", config)
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("identity")
    path.write_text(json.dumps(state), encoding="utf-8")

    assert supervisor._recorded_alive(read_state(program_root, "embed", config))[0] is True
    status = sidecar_status(program_root, "embed", config=config, restart_if_dead=False)
    assert status["running"] is True and status["pid"] == result["pid"]


def test_an_unverifiable_identity_is_never_read_as_a_mismatch():
    """Off a platform that exposes neither fact, the check reports
    ``unverified`` -- it does not invent a mismatch."""
    assert supervisor._identity_matches({"pid": None, "identity": {}}) == (True, "unverified")


# ---------------------------------------------------------------------------
# V-2: zombies, and every wait bounded
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="/proc reports the zombie state")
def test_a_zombie_is_not_alive(program_root, _reap_everything):
    parent = subprocess.Popen(
        [sys.executable, "-c", ZOMBIE_MAKER], stdout=subprocess.PIPE, text=True
    )
    _reap_everything.append(parent)
    zombie_pid = int(parent.stdout.readline())
    assert _wait_until(lambda: supervisor._process_state_char(zombie_pid) == "Z")

    assert supervisor._process_alive(zombie_pid) is False
    assert os.kill(zombie_pid, 0) is None, "os.kill(pid, 0) alone still says 'alive'"


@pytest.mark.skipif(sys.platform == "win32", reason="/proc reports the zombie state")
def test_stop_returns_promptly_for_a_zombie_instead_of_spinning(program_root, _reap_everything):
    config = _config(restart="never")
    parent = subprocess.Popen(
        [sys.executable, "-c", ZOMBIE_MAKER], stdout=subprocess.PIPE, text=True
    )
    _reap_everything.append(parent)
    zombie_pid = int(parent.stdout.readline())
    assert _wait_until(lambda: supervisor._process_state_char(zombie_pid) == "Z")
    _plant_state(program_root, config, pid=zombie_pid, argv=[sys.executable, "-c", SLEEPER])

    started = time.monotonic()
    result = stop_sidecar(program_root, "embed", config=config, grace_s=0.2)
    assert time.monotonic() - started < 5.0, "the post-SIGKILL wait was unbounded"
    assert result["state"] == "already_gone" and result["was_running"] is False
    assert read_state(program_root, "embed", config) is None


@pytest.mark.skipif(sys.platform == "win32", reason="/proc reports the zombie state")
def test_restart_always_fires_on_a_zombie_corpse(program_root, _reap_everything):
    config = _config()
    parent = subprocess.Popen(
        [sys.executable, "-c", ZOMBIE_MAKER], stdout=subprocess.PIPE, text=True
    )
    _reap_everything.append(parent)
    zombie_pid = int(parent.stdout.readline())
    assert _wait_until(lambda: supervisor._process_state_char(zombie_pid) == "Z")
    _plant_state(program_root, config, pid=zombie_pid, argv=[sys.executable, "-c", SLEEPER])

    status = sidecar_status(program_root, "embed", config=config, _http=FakeHealth())
    assert status["state"] == "restarted" and status["restarted"] is True
    assert status["previous_pid"] == zombie_pid and status["pid"] != zombie_pid


def test_a_pid_sigkill_cannot_clear_is_reported_not_waited_on(program_root, monkeypatch):
    """The bound itself: a pid that never disappears returns
    ``kill_timeout`` and keeps its state file, instead of hanging the verb."""
    config = _config(restart="never")
    sent: list[int] = []
    slept: list[float] = []
    _plant_state(program_root, config, pid=424_242, argv=["/opt/llama.cpp/llama-server"])

    monkeypatch.setattr(supervisor, "_process_alive", lambda pid: True)
    monkeypatch.setattr(supervisor, "_identity_matches", lambda state: (True, "start_time"))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: sent.append(sig))

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        time.sleep(seconds)

    started = time.monotonic()
    result = stop_sidecar(
        program_root, "embed", config=config, grace_s=0.2, kill_timeout_s=0.2, _sleep=_sleep,
    )
    elapsed = time.monotonic() - started
    assert result["state"] == "kill_timeout" and result["stopped"] is False
    assert result["signals"] == ["SIGTERM", "SIGKILL"]
    assert "retried" in result["error"]
    assert read_state(program_root, "embed", config) is not None, "the state file is kept"
    assert elapsed < 3.0, "both waits are bounded, so the verb returns"
    assert slept, "it did wait for the grace and for the kill"
    assert sent == [supervisor.signal.SIGTERM, supervisor.signal.SIGKILL]
