"""Atomic file writes. Design Section 4/12 (M0 acceptance criterion):
"atomic write (``os.replace``)" ... "atomic-write survives kill-mid-write
test".

The pattern: write the new content to a temp file in the SAME directory as
the target (so the final rename is on one filesystem/volume), fsync it, then
``os.replace(tmp, target)``. ``os.replace`` is implemented with
``MoveFileExW(..., MOVEFILE_REPLACE_EXISTING)`` on Windows and ``rename(2)``
on POSIX — both are atomic at the filesystem level: a reader (or a killed
writer) only ever sees the fully-old or fully-new content at ``target``,
never a partial write, because a partial write only ever lands in the temp
file, which nothing else is reading.

This is the ONE write primitive every store/render path in the harness is
meant to go through for anything that isn't itself a transactional SQLite
write (rendered markdown views, digests, escrow files, etc. — see design
Section 3.2's "rendered markdown files ... are views").
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable

__all__ = ["atomic_write_bytes", "atomic_write_text"]


def atomic_write_bytes(
    path: os.PathLike | str,
    data: bytes,
    *,
    _chunk_size: int | None = None,
    _on_chunk: Callable[[int], None] | None = None,
) -> None:
    """Write ``data`` to ``path`` atomically.

    On any failure before the final rename (including this process being
    killed), ``path`` is left exactly as it was before the call — the
    partially-written data only ever exists under a temp name in the same
    directory, which is cleaned up on a controlled failure and simply
    orphaned (harmless, never read) on an uncontrolled one (kill -9 /
    TerminateProcess).

    ``_chunk_size``/``_on_chunk`` are internal seams used by the
    kill-mid-write test to slow the write down deterministically; production
    callers never pass them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            if _chunk_size:
                for i in range(0, len(data), _chunk_size):
                    f.write(data[i : i + _chunk_size])
                    f.flush()
                    os.fsync(f.fileno())
                    if _on_chunk is not None:
                        _on_chunk(i)
            else:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: os.PathLike | str,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> None:
    """Text convenience wrapper over :func:`atomic_write_bytes`.

    Windows-CRLF-tolerant by construction: line endings are normalized to
    ``\\n`` first, then re-expanded to ``newline`` (default ``\\n``, i.e. the
    file is written LF-only regardless of what mix of ``\\r\\n``/``\\n`` the
    caller's string contains) and encoded explicitly — no implicit newline
    translation, no platform-dependent surprises for a file this process
    then reads back with ``newline=''``.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    atomic_write_bytes(path, normalized.encode(encoding))
