"""Lane F-1b stage 3: concurrent starts of one sidecar produce one process.

``docs/reviews/VERIFY_f1b-sidecar.md`` V-5: ``start_sidecar`` was
read-state-then-spawn with nothing serialising the window, so four concurrent
calls spawned four processes and recorded one -- and ``stop``, which correctly
signals only the recorded pid, could never reach the other three. The realistic
trigger is this lane's own supervision model: a poll loop calling ``status`` on
a dead sidecar while an operator calls ``start``.

Real child processes (tagged so ``pgrep`` can count them), a fake health seam,
no socket, and nothing that starts a llama-server or loads a GGUF.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from trialerror.sidecar import supervisor
from trialerror.sidecar.supervisor import (
    SIDECARS_TABLE,
    SidecarStartBusy,
    read_state,
    sidecar_lock_path,
    sidecar_status,
    start_sidecar,
    stop_sidecar,
)

#: Every test here counts live copies of a tagged process with ``pgrep`` and
#: the autouse reaper clears them with ``pkill``; neither exists on Windows,
#: where the count raises FileNotFoundError and the reaper's failure leaves
#: the spawned sleepers running (holding the temp tree open for the rest of
#: the run). One test also takes the lock from outside with ``fcntl``.
#:
#: The lock ITSELF is not POSIX-only -- ``supervisor._lock_exclusive_
#: nonblocking`` has an ``msvcrt.locking`` arm for Windows -- so this skip is
#: about the harness, not the feature: exercising it on Windows needs a
#: process counter and an out-of-process lock holder that do not use these
#: tools.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the harness needs pgrep/pkill to count and reap the tagged sleepers, and fcntl "
    "to hold the lock from outside",
)

#: A sleeper whose argv carries a per-test tag, so the number of live copies
#: is countable without touching anything else on the machine.
SLEEPER = "import time\nwhile True:\n    time.sleep(0.05)  # {tag}\n"

_MINIMAL_CONFIG = '[program]\nid = "PROG-sidecar-lock"\n\n'


class FakeHealth:
    def get(self, url: str, timeout_s: float) -> int:
        return 200


@pytest.fixture
def program_root(tmp_path: Path) -> Path:
    (tmp_path / "trialerror.toml").write_text(_MINIMAL_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture
def tag() -> str:
    return f"SIDECARLOCK-{os.getpid()}-{time.monotonic_ns()}"


@pytest.fixture(autouse=True)
def _reap_everything(tag: str):
    yield
    subprocess.run(["pkill", "-9", "-f", tag], check=False)
    for pid in list(supervisor._OWN_CHILDREN):
        child = supervisor._OWN_CHILDREN.pop(pid, None)
        if child is None:
            continue
        try:
            child.kill()
            child.wait(timeout=10)
        except Exception:  # noqa: BLE001 - already gone
            pass


def _config(tag: str) -> dict:
    return {
        SIDECARS_TABLE: {
            "embed": {
                "command": [sys.executable, "-c", SLEEPER.format(tag=tag)],
                "restart": "always",
            }
        }
    }


def _running(tag: str) -> int:
    out = subprocess.run(["pgrep", "-fc", tag], capture_output=True, text=True, check=False).stdout
    return int(out.strip() or 0)


def test_four_concurrent_starts_spawn_one_process(program_root, tag):
    """The V-5 repro, as a test."""
    config = _config(tag)
    results: list[dict] = []
    errors: list[Exception] = []

    def _start() -> None:
        try:
            results.append(start_sidecar(program_root, "embed", config=config, _http=FakeHealth()))
        except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
            errors.append(exc)

    threads = [threading.Thread(target=_start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert len(results) == 4
    assert _running(tag) == 1, "more than one process was spawned"
    assert len({r["pid"] for r in results}) == 1, "the four calls disagree about the pid"
    assert sum(1 for r in results if r["started"]) == 1
    assert sum(1 for r in results if r["already_running"]) == 3
    assert read_state(program_root, "embed", config)["pid"] == results[0]["pid"]


def test_a_start_racing_a_restarting_status_also_spawns_once(program_root, tag):
    """The realistic trigger: a poll loop restarting a dead sidecar while an
    operator starts it."""
    config = _config(tag)
    first = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    os.kill(first["pid"], 9)
    supervisor._OWN_CHILDREN.pop(str(first["pid"]), None)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _running(tag):
        time.sleep(0.02)

    out: list[dict] = []
    threads = [
        threading.Thread(
            target=lambda: out.append(
                sidecar_status(program_root, "embed", config=config, _http=FakeHealth())
            )
        ),
        threading.Thread(
            target=lambda: out.append(
                start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
            )
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert _running(tag) == 1
    assert len({row["pid"] for row in out}) == 1
    assert read_state(program_root, "embed", config)["pid"] == out[0]["pid"]


def test_a_start_that_cannot_take_the_lock_refuses_instead_of_spawning(program_root, tag):
    """The refusal itself, with the lock held by something else."""
    import fcntl

    config = _config(tag)
    path = sidecar_lock_path(program_root, "embed", config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(SidecarStartBusy) as caught:
            start_sidecar(program_root, "embed", config=config, lock_wait_s=0.2, _http=FakeHealth())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert "in flight" in str(caught.value)
    assert _running(tag) == 0, "nothing was spawned"
    assert read_state(program_root, "embed", config) is None


def test_the_lock_is_released_for_the_next_start(program_root, tag):
    config = _config(tag)
    first = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    stop_sidecar(program_root, "embed", config=config, grace_s=2.0)
    second = start_sidecar(program_root, "embed", config=config, lock_wait_s=1.0, _http=FakeHealth())

    assert second["started"] is True and second["pid"] != first["pid"]
    assert _running(tag) == 1


def test_the_lock_file_lives_beside_the_state_file(program_root, tag):
    config = _config(tag)
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    path = sidecar_lock_path(program_root, "embed", config)

    assert path.parent == program_root / "run" / "sidecars"
    assert path.is_file()
