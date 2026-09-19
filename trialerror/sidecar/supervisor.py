"""Start, inspect and stop one configured sidecar process.

The lifecycle lives here rather than in the CLI group so that the doctor check
(:mod:`trialerror.sidecar.checks`) and any later caller ask the same questions
of the same files, and so every part of it can be tested with a fake command
and a fake health endpoint (see ``tests/test_sidecar_supervisor.py``).

**State on disk.** One JSON file per sidecar under the program's ``run/``
directory::

    run/sidecars/<name>.json      pid, argv, cwd, health_url, restart,
                                  started_at, heartbeat_at, log_path
    run/sidecars/<name>.log       the process's own stdout+stderr

The JSON file IS the heartbeat: :func:`sidecar_status` rewrites its
``heartbeat_at`` every time it confirms the process alive and healthy, so the
age of that field answers "when did anything last verify this?" -- the same
question ``webfetch``'s ``sidecar.heartbeat`` answers for the fetch loop, and
the reason it is a separate field from ``started_at`` (a process that has been
up for three days and unverified for three days are different states). That
age is read back as ``heartbeat_age_s`` (:func:`heartbeat_age_s`) in every
status row and in the doctor detail, and a caller that is not the supervisor
asks for it WITHOUT moving it (``refresh_heartbeat=False``) -- a reader that
refreshed the timestamp it reports would be the only thing keeping it fresh.

**What is deliberately NOT here.** No kill-by-name, no pkill, no port
scanning: a pid this program recorded is the only process any verb here will
signal. A stale state file whose pid now belongs to something else is the one
hazard that creates, and :func:`_recorded_alive` is the mitigation: the pid's
liveness is never trusted on its own, it is checked against the process
IDENTITY recorded beside it (:func:`_process_identity` -- the kernel's start
time for the pid, and the argv the kernel reports for it), and a mismatch
counts as NOT running. So a reused pid makes ``start`` spawn, ``status``
report ``dead``, and ``stop`` refuse to signal, which is what the three verbs
would do for a pid that had simply vanished. The state file is removed as soon
as a process is found gone or found to be somebody else's.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trialerror.stores.paths import program_run_dir
from trialerror.util.atomic import atomic_write_text
from trialerror.util.timeutil import now

__all__ = [
    "SIDECARS_TABLE",
    "SidecarConfigError",
    "SidecarStartBusy",
    "SidecarSpec",
    "DEFAULT_RESTART",
    "RESTART_CHOICES",
    "DEFAULT_HEALTH_TIMEOUT_S",
    "DEFAULT_STOP_GRACE_S",
    "DEFAULT_KILL_TIMEOUT_S",
    "sidecar_names",
    "load_sidecar_spec",
    "sidecar_dir",
    "sidecar_state_path",
    "sidecar_lock_path",
    "read_state",
    "health_probe",
    "heartbeat_age_s",
    "start_sidecar",
    "sidecar_status",
    "stop_sidecar",
]

#: The config table: ``[sidecars.<name>]``.
SIDECARS_TABLE = "sidecars"

#: ``restart = "always"`` -- the poll-time restart described in the package
#: docstring -- or ``"never"``, which reports a dead sidecar and leaves it
#: dead. Default ``"never"``: restarting a process is a side effect, and a
#: program that has not asked for it should not get it.
RESTART_ALWAYS = "always"
RESTART_NEVER = "never"
RESTART_CHOICES = (RESTART_NEVER, RESTART_ALWAYS)
DEFAULT_RESTART = RESTART_NEVER

#: How long a health probe waits. Short on purpose: this answers "is it
#: listening", not "how fast is it" -- a model still loading answers 503
#: immediately, and a process wedged mid-load answers nothing at all.
DEFAULT_HEALTH_TIMEOUT_S = 5.0

#: How long :func:`stop_sidecar` waits after SIGTERM before SIGKILL. A
#: llama-server with a 4 GB mmap exits in well under a second; the grace is
#: for the general case.
DEFAULT_STOP_GRACE_S = 10.0

#: How long :func:`stop_sidecar` waits for a pid to disappear AFTER SIGKILL
#: before it gives up and says so. SIGKILL is not refusable, so the only
#: states that outlast it are uninterruptible sleep (a wedged mount, a driver)
#: and a corpse nobody has reaped -- neither of which more waiting fixes. An
#: unbounded wait here is a verb that hangs with no output, which is worse
#: than a report saying the pid is still present.
DEFAULT_KILL_TIMEOUT_S = 5.0

#: How long :func:`start_sidecar` waits for another start of the SAME sidecar
#: to finish before refusing. Generous, because the thing it waits for is a
#: model load: the lock is held across read-state/spawn/record, and a start
#: that gave up early would be back to spawning the second copy the lock
#: exists to prevent.
DEFAULT_START_LOCK_WAIT_S = 60.0

#: Processes started by THIS process, so a start/stop pair inside one
#: interpreter (a test, or a foreground operator session) can reap the child
#: instead of leaving a zombie. A detached child whose parent has exited is
#: reparented and reaped by init; one whose parent is still alive is not.
_OWN_CHILDREN: dict[str, subprocess.Popen] = {}


class SidecarConfigError(ValueError):
    """``[sidecars.<name>]`` is absent, is not a table, or does not carry a
    usable ``command``.

    A ``ValueError`` so the CLI's existing config-refusal handling applies
    unchanged. The message always names the table and the key, because the
    only fix is an edit to ``trialerror.toml``."""


class SidecarStartBusy(RuntimeError):
    """Another ``start`` of this same sidecar is in flight and did not finish
    within the wait.

    Refusing is the conservative answer: the alternative is the race this lock
    exists to close (VERIFY_f1b-sidecar.md V-5 -- four concurrent starts
    spawned four processes and recorded one, leaving three this program could
    never stop), and a second model load on a memory-capped box is a worse
    outcome than a verb that says "try again"."""


@dataclass(frozen=True)
class SidecarSpec:
    """One configured sidecar, as the supervisor needs it."""

    name: str
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    health_url: str | None = None
    restart: str = DEFAULT_RESTART
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "cwd": self.cwd,
            "env_keys": sorted(self.env),  # keys only: a value can be a token
            "health_url": self.health_url,
            "restart": self.restart,
            "health_timeout_s": self.health_timeout_s,
        }


def _sidecars_config(config: dict[str, Any] | None) -> dict[str, Any]:
    table = (config or {}).get(SIDECARS_TABLE)
    return table if isinstance(table, dict) else {}


def sidecar_names(config: dict[str, Any] | None) -> list[str]:
    """Every configured sidecar name, sorted -- what ``status`` with no name
    reports on, and what the doctor check iterates."""
    return sorted(k for k, v in _sidecars_config(config).items() if isinstance(v, dict))


def load_sidecar_spec(config: dict[str, Any] | None, name: str) -> SidecarSpec:
    """``[sidecars.<name>]`` as a :class:`SidecarSpec`, or
    :class:`SidecarConfigError`.

    ``command`` must be a LIST of strings -- the argv, no shell. A string
    would have to be split by something, and every splitter is a quoting bug
    waiting for a path with a space in it; worse, a config that reaches a
    shell is a config that can reach a pipeline.
    """
    table = _sidecars_config(config).get(name)
    if table is None:
        configured = sidecar_names(config)
        known = f" (configured: {', '.join(configured)})" if configured else ""
        raise SidecarConfigError(
            f"no [{SIDECARS_TABLE}.{name}] table in trialerror.toml{known} -- a sidecar's command "
            f"comes from config, never from an argument, so this is the only place it can be named"
        )
    if not isinstance(table, dict):
        raise SidecarConfigError(
            f"[{SIDECARS_TABLE}.{name}] must be a table (got {type(table).__name__})"
        )
    command = table.get("command")
    if not isinstance(command, (list, tuple)) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise SidecarConfigError(
            f"[{SIDECARS_TABLE}.{name}] command must be a non-empty list of strings (the argv to "
            f'run, e.g. command = ["/path/to/server", "--port", "8871"]) -- a string would have to '
            f"be split by a shell, and a config that reaches a shell can reach a pipeline"
        )
    env_table = table.get("env") or {}
    if not isinstance(env_table, dict):
        raise SidecarConfigError(f"[{SIDECARS_TABLE}.{name}] env must be a table of strings")
    restart = str(table.get("restart", DEFAULT_RESTART)).strip().lower()
    if restart not in RESTART_CHOICES:
        raise SidecarConfigError(
            f"[{SIDECARS_TABLE}.{name}] restart = {table.get('restart')!r} is not one of "
            f"{list(RESTART_CHOICES)}"
        )
    health_url = table.get("health_url")
    return SidecarSpec(
        name=str(name),
        command=[str(part) for part in command],
        cwd=str(table["cwd"]) if table.get("cwd") else None,
        env={str(k): str(v) for k, v in env_table.items()},
        health_url=str(health_url) if health_url else None,
        restart=restart,
        health_timeout_s=float(table.get("health_timeout_s", DEFAULT_HEALTH_TIMEOUT_S)),
    )


# ---------------------------------------------------------------------------
# state files
# ---------------------------------------------------------------------------


def sidecar_dir(program_root: Path | str, config: dict[str, Any] | None = None) -> Path:
    return program_run_dir(program_root, config) / "sidecars"


def sidecar_state_path(program_root: Path | str, name: str, config: dict[str, Any] | None = None) -> Path:
    return sidecar_dir(program_root, config) / f"{name}.json"


def sidecar_log_path(program_root: Path | str, name: str, config: dict[str, Any] | None = None) -> Path:
    return sidecar_dir(program_root, config) / f"{name}.log"


def read_state(program_root: Path | str, name: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The recorded state, or ``None`` when there is none or it is unreadable.

    Unreadable is treated as absent on purpose: a truncated JSON file (a
    machine that lost power mid-write, which ``atomic_write_text`` makes
    unlikely but not impossible) must not stop an operator from starting the
    sidecar again."""
    path = sidecar_state_path(program_root, name, config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent, truncated, or not JSON: all "no state"
        return None
    return data if isinstance(data, dict) else None


def _write_state(program_root: Path | str, name: str, state: dict[str, Any], config: dict[str, Any] | None = None) -> None:
    path = sidecar_state_path(program_root, name, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _clear_state(program_root: Path | str, name: str, config: dict[str, Any] | None = None) -> None:
    try:
        sidecar_state_path(program_root, name, config).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# liveness
# ---------------------------------------------------------------------------


def _read_proc_stat(pid: int) -> str | None:
    """``/proc/<pid>/stat`` as text, or ``None`` off Linux and for a pid that
    is gone. Everything this module knows about a process it did not spawn
    comes from here and from ``cmdline`` next door."""
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - no /proc, or no such pid: "cannot say"
        return None


def _proc_stat_fields(pid: int) -> list[str] | None:
    """``/proc/<pid>/stat`` from the STATE field on, split on whitespace.

    Everything before the state is dropped by splitting on the last ``)``:
    field 2 is the executable name in parentheses and may itself contain
    spaces and parentheses, which is what makes a naive ``.split()`` of this
    file wrong for exactly the processes it matters for."""
    raw = _read_proc_stat(pid)
    if raw is None or ")" not in raw:
        return None
    return raw.rsplit(")", 1)[1].split()


def _process_state_char(pid: int | None) -> str | None:
    """The single-letter process state (``R``, ``S``, ``D``, ``Z`` ...), or
    ``None`` when it cannot be read."""
    if not pid or pid <= 0:
        return None
    fields = _proc_stat_fields(int(pid))
    return fields[0] if fields else None


def _process_start_ticks(pid: int | None) -> int | None:
    """When the kernel started this pid, in clock ticks since boot (field 22
    of ``/proc/<pid>/stat``).

    THE identity fact worth recording: pids are recycled, a start time is
    not -- two processes with the same pid always have different start
    times, so comparing it answers "is this still the process I started?"
    without reading any of the process's own content."""
    if not pid or pid <= 0:
        return None
    fields = _proc_stat_fields(int(pid))
    if not fields or len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:  # pragma: no cover - the kernel does not write this field wrong
        return None


def _process_cmdline(pid: int | None) -> list[str] | None:
    """The argv the kernel reports for ``pid``, or ``None``/``[]`` when it
    cannot be read (a zombie has none, and neither has a kernel thread)."""
    if not pid or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except Exception:  # noqa: BLE001 - no /proc, or no such pid
        return None
    parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00")]
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def _process_identity(pid: int) -> dict[str, Any]:
    """What is recorded beside the pid so a later reader can tell THIS
    process from whatever inherits its number.

    Empty off a platform that does not expose either fact, in which case
    :func:`_identity_matches` says so rather than guessing -- an absent
    identity is "unverified", never "mismatched"."""
    identity: dict[str, Any] = {}
    ticks = _process_start_ticks(pid)
    if ticks is not None:
        identity["start_ticks"] = ticks
    cmdline = _process_cmdline(pid)
    if cmdline:
        identity["cmdline"] = cmdline
    return identity


def _identity_matches(state: dict[str, Any]) -> tuple[bool, str]:
    """Whether the live pid in ``state`` is still the process this program
    started: ``(True, how_it_was_checked)`` or ``(False, why_not)``.

    Start time first (exact, and nothing a process can change about itself),
    the kernel's argv second (for a state file written before the start time
    was recorded), and ``"unverified"`` when the platform exposes neither --
    a check that cannot run must not turn a healthy sidecar into a dead one.
    """
    pid = state.get("pid")
    recorded = state.get("identity")
    recorded = recorded if isinstance(recorded, dict) else {}

    recorded_ticks = recorded.get("start_ticks")
    if isinstance(recorded_ticks, int):
        ticks_now = _process_start_ticks(pid)
        if ticks_now is not None:
            if ticks_now == recorded_ticks:
                return True, "start_time"
            return False, (
                f"pid {pid} was started at {ticks_now} ticks since boot, not the {recorded_ticks} "
                f"this program recorded: the pid has been reused by another process"
            )

    recorded_argv = recorded.get("cmdline") or state.get("argv")
    cmdline_now = _process_cmdline(pid)
    if cmdline_now and isinstance(recorded_argv, list) and recorded_argv:
        if list(cmdline_now) == [str(part) for part in recorded_argv]:
            return True, "cmdline"
        return False, (
            f"pid {pid} is running {cmdline_now[0]!r}, not the {str(recorded_argv[0])!r} this "
            f"program started: the pid has been reused by another process"
        )
    return True, "unverified"


def _recorded_alive(state: dict[str, Any] | None) -> tuple[bool, str | None]:
    """``(is the recorded process running, why not)`` for one state file.

    The only liveness question any verb here asks. ``False`` covers both "the
    pid is gone" (reason ``None``) and "the pid is somebody else's now"
    (reason = the mismatch, which every caller reports rather than swallows)."""
    if not state:
        return False, None
    pid = state.get("pid")
    if not _process_alive(pid):
        return False, None
    ok, note = _identity_matches(state)
    return (True, None) if ok else (False, note)


def _process_alive(pid: int | None) -> bool:
    """Whether ``pid`` names a live process THIS user can signal.

    ``os.kill(pid, 0)`` on POSIX; on Windows, a ``WaitForSingleObject``-free
    approximation via ``OpenProcess`` is not available from the stdlib, so the
    Popen object is consulted where we own it and the pid is otherwise
    reported as unknown rather than guessed.

    A ZOMBIE is not alive. ``os.kill(pid, 0)`` succeeds for one -- the process
    table entry is still there, waiting for a parent that may never call
    ``wait()`` -- and reading that as "running" is what would make ``stop``
    wait for an exit that has already happened and ``restart = "always"``
    never fire on a corpse. This does NOT apply to a child of this process:
    ``Popen.poll()`` reaps it, so it is gone rather than a zombie.

    Answers only about the pid. Whether the pid is still the process this
    program started is :func:`_identity_matches`, and callers ask both through
    :func:`_recorded_alive`."""
    if not pid or pid <= 0:
        return False
    child = _OWN_CHILDREN.get(str(pid))
    if child is not None:
        return child.poll() is None
    if _process_state_char(pid) == "Z":
        return False
    if sys.platform == "win32":  # pragma: no cover - POSIX container is the deployment
        try:
            output = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout
        except Exception:  # noqa: BLE001 - no tasklist: unknown, report not-alive
            return False
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, owned by someone else
        return True
    except OSError:
        return False
    return True


def _health_not_probed(url: str | None, reason: str) -> dict[str, Any]:
    """The health reading for a sidecar whose endpoint was NOT asked, because
    the process is not there to answer.

    A different statement from :func:`health_probe`'s ``configured: False``,
    which means "this sidecar declares no ``health_url``" -- reading a dead
    sidecar's detail as that made a configured endpoint look unconfigured
    (VERIFY_f1b-sidecar.md V-6). ``ok: None`` in both: neither says the
    endpoint answered."""
    if not url:
        return health_probe(None)
    return {"configured": True, "ok": None, "url": url, "skipped": reason}


def heartbeat_age_s(state: dict[str, Any] | None, *, field: str = "heartbeat_at") -> float | None:
    """Seconds since ``heartbeat_at`` was last refreshed, or ``None`` when the
    field is absent or unparseable.

    The consumer the field was missing (V-4): the supervisor's docstring says
    the age answers "when did anything last verify this?", and nothing read
    it. Reported, not judged -- see :mod:`trialerror.sidecar.checks` on why a
    threshold here would fail a program in which nothing polls."""
    stamp = (state or {}).get(field)
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        from trialerror.util.timeutil import now_dt, parse

        return max(0.0, round((now_dt() - parse(stamp)).total_seconds(), 1))
    except Exception:  # noqa: BLE001 - an unparseable stamp is "cannot say"
        return None


def _reap_own_child(pid: int | None, *, timeout_s: float = DEFAULT_KILL_TIMEOUT_S) -> None:
    """``wait()`` on a child THIS process started, bounded, and a no-op for a
    pid this process does not own.

    A child of a still-living parent stays in the process table as a zombie
    until somebody waits on it; this process is that parent for anything it
    spawned in the same interpreter (a test, a foreground operator session).
    Ownership is given back if the wait times out, because a process that is
    still running is still ours to report on."""
    child = _OWN_CHILDREN.pop(str(pid), None)
    if child is None:
        return
    try:
        child.wait(timeout=max(0.1, float(timeout_s)))
    except Exception:  # noqa: BLE001 - still running (or already reaped by poll())
        if child.poll() is None:
            _OWN_CHILDREN[str(pid)] = child


def health_probe(url: str | None, *, timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S, _http: Any = None) -> dict[str, Any]:
    """GET ``url``, reporting ``{configured, ok, status?, error?, latency_ms}``.

    ``{"configured": False, "ok": None}`` when no ``health_url`` is set: a
    sidecar without one can only ever be reported as "the process is alive",
    and saying ``ok: True`` for that would be claiming something nobody
    checked.

    ``_http`` is the test seam (an object with ``get(url, timeout_s)``
    returning a status int) -- no test in this repo opens a socket."""
    if not url:
        return {"configured": False, "ok": None}
    started = time.perf_counter()
    try:
        if _http is not None:
            status = int(_http.get(url, timeout_s))
        else:
            import urllib.error
            import urllib.request

            try:
                with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - config-pathed health url
                    status = int(getattr(response, "status", 0) or 0)
            except urllib.error.HTTPError as exc:  # a 4xx/5xx is an answer, not an outage
                status = int(exc.code)
    except Exception as exc:  # noqa: BLE001 - an unreachable health endpoint is a reading
        return {
            "configured": True,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "url": url,
        }
    return {
        "configured": True,
        "ok": 200 <= status < 300,
        "status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "url": url,
    }


# ---------------------------------------------------------------------------
# the three verbs
# ---------------------------------------------------------------------------


def _spawn(spec: SidecarSpec, *, program_root: Path, config: dict[str, Any] | None) -> tuple[int, Path, list[str]]:
    """Start the process detached, appending its output to the sidecar's log.

    Detachment is the same technique :mod:`trialerror.jobs.worker` and
    ``dashboard serve`` use (``start_new_session`` on POSIX,
    ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` on Windows): the child
    survives the CLI process that started it and does not take the operator's
    Ctrl-C."""
    log_path = sidecar_log_path(program_root, spec.name, config)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(spec.env)

    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover - POSIX container is the deployment
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    log_fh = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv from this program's own config, never a shell
            spec.command,
            cwd=spec.cwd or str(program_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            **popen_kwargs,
        )
    finally:
        log_fh.close()
    _OWN_CHILDREN[str(proc.pid)] = proc
    return proc.pid, log_path, list(spec.command)


def sidecar_lock_path(program_root: Path | str, name: str, config: dict[str, Any] | None = None) -> Path:
    """The file :func:`start_sidecar` locks while it decides whether to spawn.

    Beside the state file, and never deleted: an empty lock file costs
    nothing, and removing one while another process holds it is how a lock
    stops being one."""
    return sidecar_dir(program_root, config) / f"{name}.lock"


@contextmanager
def _start_lock(program_root: Path, name: str, config: dict[str, Any] | None, *, wait_s: float):
    """Hold an exclusive lock across ``start``'s read-then-spawn window.

    An OS-level lock on a file rather than a pid file (the precedent is
    :func:`trialerror.offload.lock.single_instance_lock`, for the same
    reason): the kernel releases it when the holder's handle closes or the
    holder dies, so there is no stale lock to age out and nothing to clean up
    after a process killed with the power button.

    Serialises starts of ONE sidecar in one program. It does not pretend to
    do more: two programs pointing at the same port is a config error no lock
    can catch, and the port bind is what catches it."""
    path = sidecar_lock_path(program_root, name, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, float(wait_s))
    try:
        while True:
            try:
                _lock_exclusive_nonblocking(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SidecarStartBusy(
                        f"another `sidecar start {name}` is in flight (it holds {path}) and has not "
                        f"finished within {wait_s:g}s -- nothing was spawned; "
                        f"`trialerror sidecar status {name}` says what it did"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _lock_exclusive_nonblocking(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - POSIX container is the deployment
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":  # pragma: no cover - POSIX container is the deployment
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 - closing the fd releases it anyway
        pass


def start_sidecar(
    program_root: Path | str,
    name: str,
    *,
    config: dict[str, Any] | None = None,
    lock_wait_s: float = DEFAULT_START_LOCK_WAIT_S,
    _http: Any = None,
) -> dict[str, Any]:
    """Start the configured sidecar, or report the one already running.

    Idempotent by design: a start against a live, recorded process is not an
    error and does not spawn a second copy -- two ``llama-server`` processes
    on one port is a 4 GB mistake, and the second one's bind failure would be
    visible only in a log file nobody reads.

    Idempotent UNDER CONCURRENCY too (VERIFY_f1b-sidecar.md V-5): the read of
    the state file, the spawn and the record happen under an exclusive lock on
    ``run/sidecars/<name>.lock``, so the realistic race -- a poll loop calling
    ``status`` on a dead sidecar at the moment an operator calls ``start`` --
    produces one process and one recorded pid rather than N processes of which
    ``stop`` can reach one. A start that cannot take the lock within
    ``lock_wait_s`` raises :class:`SidecarStartBusy` and spawns nothing."""
    program_root = Path(program_root)
    spec = load_sidecar_spec(config, name)
    with _start_lock(program_root, name, config, wait_s=lock_wait_s):
        return _start_locked(program_root, name, spec, config=config, _http=_http)


def _start_locked(
    program_root: Path,
    name: str,
    spec: SidecarSpec,
    *,
    config: dict[str, Any] | None,
    _http: Any,
) -> dict[str, Any]:
    """:func:`start_sidecar`'s body, with the lock already held."""
    state = read_state(program_root, name, config)
    alive, stale_note = _recorded_alive(state)
    if state is not None and alive:
        health = health_probe(spec.health_url, timeout_s=spec.health_timeout_s, _http=_http)
        state = {**state, "heartbeat_at": now(), "health": health}
        _write_state(program_root, name, state, config)
        return {"name": name, "started": False, "already_running": True, **_report(state, spec, health)}

    if state is not None:
        # A recorded pid that is gone -- or one that now belongs to somebody
        # else (``stale_note``) -- and leaving the file would make every later
        # `status` report a dead process forever, or worse, adopt a stranger.
        _clear_state(program_root, name, config)

    pid, log_path, argv = _spawn(spec, program_root=program_root, config=config)
    health = health_probe(spec.health_url, timeout_s=spec.health_timeout_s, _http=_http)
    state = {
        "name": name,
        "pid": pid,
        "argv": argv,
        #: The kernel's own facts about this pid, so a later reader can tell
        #: it from whatever inherits the number (see :func:`_process_identity`).
        "identity": _process_identity(pid),
        "cwd": spec.cwd or str(program_root),
        "env_keys": sorted(spec.env),
        "health_url": spec.health_url,
        "restart": spec.restart,
        "log_path": str(log_path),
        "started_at": now(),
        "heartbeat_at": now(),
        "health": health,
    }
    _write_state(program_root, name, state, config)
    return {
        "name": name,
        "started": True,
        "already_running": False,
        **_report(state, spec, health),
        **({"stale_pid_note": stale_note} if stale_note else {}),
    }


def _report(state: dict[str, Any], spec: SidecarSpec, health: dict[str, Any]) -> dict[str, Any]:
    """The fields every verb reports about one sidecar, from one place so
    ``start``, ``status`` and the doctor check cannot describe it differently."""
    return {
        "pid": state.get("pid"),
        "argv": state.get("argv"),
        "log_path": state.get("log_path"),
        "started_at": state.get("started_at"),
        "heartbeat_at": state.get("heartbeat_at"),
        "health": health,
        "health_url": spec.health_url,
        "restart": spec.restart,
    }


def sidecar_status(
    program_root: Path | str,
    name: str,
    *,
    config: dict[str, Any] | None = None,
    restart_if_dead: bool = True,
    refresh_heartbeat: bool = True,
    _http: Any = None,
) -> dict[str, Any]:
    """What this sidecar is doing now, refreshing its heartbeat when it is
    confirmed alive -- and restarting it when it is dead and configured
    ``restart = "always"``.

    THIS is where ``restart = "always"`` happens (see the package docstring):
    the caller that polls is the supervisor.

    Two independent switches, because they suppress two different side
    effects (VERIFY_f1b-sidecar.md V-4: ``restart_if_dead=False`` alone was
    described as read-only and still rewrote the state file on every call):

    - ``restart_if_dead=False`` -- report a dead sidecar, start nothing.
    - ``refresh_heartbeat=False`` -- touch no file at all. A caller that is
      not itself the supervisor must not move the timestamp whose age is the
      evidence of when something last verified this process.

    A genuinely read-only surface (the doctor check) passes both."""
    program_root = Path(program_root)
    spec = load_sidecar_spec(config, name)
    state = read_state(program_root, name, config)

    if state is None:
        return {
            "name": name,
            "running": False,
            "state": "never_started",
            "restarted": False,
            "spec": spec.to_dict(),
            # Not probed because there is no process to answer -- which is not
            # the same statement as "no health_url configured" (V-6).
            "health": _health_not_probed(spec.health_url, "never started"),
        }

    alive, stale_note = _recorded_alive(state)
    if not alive:
        dead_pid = state.get("pid")
        if restart_if_dead and spec.restart == RESTART_ALWAYS:
            result = start_sidecar(program_root, name, config=config, _http=_http)
            return {
                "name": name,
                "running": True,
                "state": "restarted",
                "restarted": True,
                "previous_pid": dead_pid,
                "spec": spec.to_dict(),
                **{k: v for k, v in result.items() if k not in {"name", "started", "already_running"}},
                **({"stale_pid_note": stale_note} if stale_note else {}),
            }
        return {
            "name": name,
            "running": False,
            "state": "dead",
            "restarted": False,
            "pid": dead_pid,
            "log_path": state.get("log_path"),
            "started_at": state.get("started_at"),
            "heartbeat_at": state.get("heartbeat_at"),
            "heartbeat_age_s": heartbeat_age_s(state),
            "spec": spec.to_dict(),
            "health": _health_not_probed(spec.health_url, "process not running"),
            **({"stale_pid_note": stale_note} if stale_note else {}),
        }

    health = health_probe(spec.health_url, timeout_s=spec.health_timeout_s, _http=_http)
    if not refresh_heartbeat:
        # Report the reading, record nothing: the heartbeat in the file stays
        # whatever the last real poll left, and its age stays meaningful.
        state = {**state, "health": health}
    elif health.get("ok") is not False:
        # The heartbeat means "verified alive and not unhealthy" -- a process
        # that is up but failing its health check must not refresh it, or the
        # age of the heartbeat would stop being evidence of anything.
        state = {**state, "heartbeat_at": now(), "health": health}
        _write_state(program_root, name, state, config)
    else:
        state = {**state, "health": health}
        _write_state(program_root, name, state, config)
    return {
        "name": name,
        "running": True,
        "state": "healthy" if health.get("ok") is not False else "unhealthy",
        "restarted": False,
        "spec": spec.to_dict(),
        "heartbeat_age_s": heartbeat_age_s(state),
        **_report(state, spec, health),
    }


def stop_sidecar(
    program_root: Path | str,
    name: str,
    *,
    config: dict[str, Any] | None = None,
    grace_s: float = DEFAULT_STOP_GRACE_S,
    kill_timeout_s: float = DEFAULT_KILL_TIMEOUT_S,
    _sleep: Any = None,
) -> dict[str, Any]:
    """Ask the recorded process to exit, escalate if it will not, and remove
    the state file.

    SIGTERM, then SIGKILL after ``grace_s`` -- a sidecar holding a model in
    mmap has nothing to flush, and a process that ignores SIGTERM is not
    going to be talked round. Only ever the pid this program recorded AND
    still owns (:func:`_recorded_alive`): a pid whose process is somebody
    else's now is reported as ``stale_pid`` and is never signalled. A pid that
    is already gone is a success, not an error: ``stop`` is idempotent because
    the state it is asked to reach is "not running".

    Every wait is bounded. Past ``kill_timeout_s`` after the SIGKILL the verb
    reports ``kill_timeout`` and KEEPS the state file (so a later ``stop`` can
    try again) rather than spinning on a pid that SIGKILL did not clear."""
    program_root = Path(program_root)
    sleeper = _sleep or time.sleep
    state = read_state(program_root, name, config)
    if state is None:
        return {"name": name, "stopped": False, "was_running": False, "state": "never_started"}

    pid = state.get("pid")
    alive, stale_note = _recorded_alive(state)
    if not alive:
        _reap_own_child(pid)
        _clear_state(program_root, name, config)
        if stale_note:
            return {
                "name": name, "stopped": False, "was_running": False, "state": "stale_pid",
                "pid": pid, "note": (
                    f"{stale_note} -- nothing was signalled, and the stale state file was removed"
                ),
            }
        return {"name": name, "stopped": False, "was_running": False, "state": "already_gone", "pid": pid}

    signals_sent: list[str] = []
    child = _OWN_CHILDREN.get(str(pid))
    try:
        if child is not None:
            child.terminate()
        else:
            os.kill(int(pid), signal.SIGTERM)
        signals_sent.append("SIGTERM")
    except ProcessLookupError:
        signals_sent.append("already_gone")
    except Exception as exc:  # noqa: BLE001 - report, never raise out of a stop
        _clear_state(program_root, name, config)
        return {
            "name": name, "stopped": False, "was_running": True, "state": "signal_failed",
            "pid": pid, "error": f"{type(exc).__name__}: {exc}",
        }

    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline and _process_alive(pid):
        sleeper(0.05)

    if _process_alive(pid):
        try:
            if child is not None:
                child.kill()
            else:
                os.kill(int(pid), signal.SIGKILL)
            signals_sent.append("SIGKILL")
        except Exception:  # noqa: BLE001 - it may have exited between the two checks
            pass
        # BOUNDED, like the grace wait above: SIGKILL cannot be refused, so a
        # pid still present after this is in uninterruptible sleep or is a
        # corpse whose parent has not reaped it, and no amount of further
        # waiting changes either. (A zombie is already reported not-alive by
        # :func:`_process_alive`, so this loop does not see one.)
        kill_deadline = time.monotonic() + max(0.0, kill_timeout_s)
        while time.monotonic() < kill_deadline and _process_alive(pid):
            sleeper(0.05)

    _reap_own_child(pid, timeout_s=kill_timeout_s)

    if _process_alive(pid):
        return {
            "name": name,
            "stopped": False,
            "was_running": True,
            "state": "kill_timeout",
            "pid": pid,
            "signals": signals_sent,
            "error": (
                f"pid {pid} was still present {kill_timeout_s:g}s after SIGKILL (uninterruptible "
                f"sleep, or a state this program cannot clear); the state file was KEPT so "
                f"`trialerror sidecar stop {name}` can be retried"
            ),
        }

    _clear_state(program_root, name, config)
    return {
        "name": name,
        "stopped": True,
        "was_running": True,
        "state": "stopped",
        "pid": pid,
        "signals": signals_sent,
    }
