"""``tests/_ports.py`` -- the one place server tests get a port from.

Three test files used to carry the same six-line ``_free_port()``, and the
window it leaves open (bind 0, close, hand the number to a subprocess that
binds it a moment later) is what makes a server test fail fifteen seconds
later as "never came up" (lane C, finding F7). This file is the contract for
what replaced them.
"""

from __future__ import annotations

import socket

import pytest

from tests import _ports


def test_a_port_is_never_handed_out_twice_in_one_process():
    """The half of the race this module can actually close. A freshly-exited
    server makes reuse likely -- the kernel is free to hand its port straight
    back -- and the second test then binds a number the OS still holds state
    for."""
    ports = [_ports.free_port() for _ in range(25)]
    assert len(set(ports)) == len(ports)
    assert set(ports) <= _ports.handed_out()


def test_the_port_it_returns_can_actually_be_bound():
    """The probe asks for ``SO_EXCLUSIVEADDRUSE`` where the platform has it,
    because that is what the server asks for (M-LU-4): a number that cannot
    be taken exclusively must not be handed out as if it could."""
    port = _ports.free_port()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        assert sock.getsockname()[1] == port
    finally:
        sock.close()


def test_it_refuses_rather_than_loops_when_it_cannot_find_a_fresh_one(monkeypatch):
    """A test that cannot get a port should say so. Simulated by telling the
    module every port it finds is already spent."""
    monkeypatch.setattr(_ports, "_HANDED_OUT", set(range(1, 65536)))
    with pytest.raises(RuntimeError, match="could not find an unused local port"):
        _ports.free_port(attempts=3)
