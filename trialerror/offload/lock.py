"""Single-instance lock for the DEV GPU worker.

Design section 4: "single-instance lock (``msvcrt.locking`` on
``%LOCALAPPDATA%/trialerror/offload/worker.lock``)". Two workers on one
laptop would fight over the GPU and could both claim (each would win a
different job, which is harmless) but would also both write into the same
per-job work directory (which is not). The lock is an OS-level byte-range
lock rather than a pid file: a worker killed with the power button
releases it the moment the process dies, with nothing to clean up.

Also the home of the worker's local state root, kept off OneDrive on
purpose (design section 4: "results under
``%LOCALAPPDATA%/trialerror/offload/work/<job>/`` (never OneDrive)") --
a synced folder would upload every intermediate marker artefact and can
lock files mid-write.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = ["WorkerAlreadyRunning", "worker_state_dir", "default_lock_path", "single_instance_lock"]


class WorkerAlreadyRunning(RuntimeError):
    """Another ``trialerror offload worker`` already holds the lock."""


def worker_state_dir() -> Path:
    """``%LOCALAPPDATA%/trialerror/offload`` on Windows; the XDG state dir
    (``~/.local/state/trialerror/offload``) elsewhere. Never the program
    root: this is DEV-local scratch, not part of the record."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "trialerror" / "offload"


def default_lock_path() -> Path:
    return worker_state_dir() / "worker.lock"


@contextmanager
def single_instance_lock(path: Path | str | None = None) -> Iterator[Path]:
    """Hold an exclusive, non-blocking lock on ``path`` for the block's
    duration; raise :class:`WorkerAlreadyRunning` if someone else has it.

    ``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere -- both
    release automatically when the file handle closes or the process dies,
    which is the property a pid file cannot offer."""
    lock_path = Path(path) if path is not None else default_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkerAlreadyRunning(
                    f"another offload worker holds {lock_path} -- close it first "
                    "(only one DEV GPU worker may run at a time)"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkerAlreadyRunning(
                    f"another offload worker holds {lock_path} -- close it first "
                    "(only one DEV GPU worker may run at a time)"
                ) from exc
        try:
            os.truncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:  # pragma: no cover - informational only
            pass
        yield lock_path
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)
