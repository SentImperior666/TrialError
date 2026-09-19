"""One place the tests get a TCP port from (lane C, finding F7).

Three files used to carry the same six-line ``_free_port()``: bind port 0,
read the number the kernel picked, close the socket, hand the number to a
subprocess that binds it a moment later. Between the close and that bind the
port belongs to nobody, so anything that binds in the window wins -- and
since M-LU-4 the dashboard server asks for ``SO_EXCLUSIVEADDRUSE`` on win32,
so the loser does not quietly share the port, it fails to bind and dies. The
test then sits in ``_wait_for_server`` for fifteen seconds and reports a
server that "never came up", naming neither the collision nor the winner.

What this module can fix, it fixes:

- **The window between probes in one process.** Ports handed out here are
  remembered, so two tests in the same session can never be given the same
  number -- the case a freshly-exited server makes likely, since the kernel
  is then free to hand its port straight back while the OS still holds state
  for it.
- **A probe that does not represent the real bind.** The probe socket takes
  the same ``SO_EXCLUSIVEADDRUSE`` the server will ask for, so a number that
  cannot actually be taken exclusively is never handed out as if it could.

What it cannot fix: another process on the machine binding the port in the
gap. The complete fix is ``--port 0`` with the server printing the port it
actually took, which is a CLI change and a separate step -- recorded in
``docs/reviews/IMPL_lane-c-stage2-merge.md``'s fix-pass section. Until then
``_wait_for_server`` is given the server's ``Popen`` so a lost race is
reported as "the process exited with code N" at once, rather than as a
fifteen-second timeout with no cause.
"""

from __future__ import annotations

import socket
import threading

__all__ = ["free_port", "handed_out"]

_HANDED_OUT: set[int] = set()
_LOCK = threading.Lock()


def free_port(*, attempts: int = 50) -> int:
    """A 127.0.0.1 TCP port that is free right now and that this process has
    not handed out before.

    Raises ``RuntimeError`` rather than looping forever if the kernel keeps
    returning numbers already spent -- a test that cannot get a port should
    say so, not hang."""
    with _LOCK:
        for _ in range(attempts):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # Mirror the server's own bind (M-LU-4) so the probe answers
                # the question the server will ask, not an easier one.
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                    except OSError:  # pragma: no cover - platform-dependent
                        pass
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            finally:
                sock.close()
            if port in _HANDED_OUT:
                continue
            _HANDED_OUT.add(port)
            return port
    raise RuntimeError(
        f"could not find an unused local port in {attempts} attempts "
        f"({len(_HANDED_OUT)} already handed out in this process)"
    )


def handed_out() -> frozenset[int]:
    """Every port :func:`free_port` has returned in this process. For tests
    about this module; nothing else should need it."""
    with _LOCK:
        return frozenset(_HANDED_OUT)
