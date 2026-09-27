"""The command channel to a rented instance: one ``ssh`` call per command
(the vast.ai OCR design, section 7).

DEV always initiates; the instance never connects to anything on DEV's side.
Every remote command is one string whose dynamic parts are ``shlex.quote``d,
run by the instance's shell. There is no scp or rsync: an upload is the bytes
on the stdin of ``cat > <path>``, checked by a remote ``sha256sum``; a
download is ``cat -- <path>``, re-hashed here.

**Hardening (design 7.2): the queue key must be unreachable from a rented
host.** :meth:`SshShell.argv` reads no user ssh config (``-F none``), offers
exactly one identity -- the dedicated vast.ai key pair's private half, which
``ssh`` opens and TrialError never does -- never consults or forwards an
agent, opens no other channel back to DEV, and trusts the instance's fresh
host key on first use in a known_hosts file of its own lease. A restricted
queue key loaded into an agent can therefore be neither offered nor
forwarded.

Ported from the public TrialError copy's embedding backend:
``SshChannel._base`` (extended) and ``wait_reachable`` (exit 255 is retried
within the startup budget; a rejected identity fails after a BOUNDED grace --
round 6, :data:`SSH_AUTH_GRACE_S` -- where the ported code failed at once).
[assumption] ``-F none`` and
``IdentityAgent=none`` behave on Windows OpenSSH as on OpenSSH elsewhere;
settle at the first live run (design 15.9).
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from trialerror.vastai.errors import VastError

__all__ = [
    "REMOTE_ROOT_SHM",
    "REMOTE_ROOT_DISK",
    "SSH_HARDENING_OPTIONS",
    "SSH_AUTH_GRACE_S",
    "SSH_AUTH_ATTEMPTS",
    "RemoteShell",
    "SshShell",
    "ShellError",
    "ConnectionLost",
    "IdentityRejected",
    "RemoteTimeout",
    "TransferMismatch",
    "lease_dir",
    "known_hosts_path",
    "q",
    "upload_verified",
    "download_verified",
]

#: Where a lease's files live on the instance: RAM-backed ``/dev/shm``, or,
#: when ``[vastai.egress] remote_scratch = "shm_or_disk"`` allows it, the
#: container disk. Neutral names only: ``<root>/<lease id>/input.pdf``.
REMOTE_ROOT_SHM = "/dev/shm/te"
REMOTE_ROOT_DISK = "/var/tmp/te"

#: How long a ``Permission denied`` is retried before it is believed, from the
#: FIRST one (seconds), and at most :data:`SSH_AUTH_ATTEMPTS` probes.
#:
#: Round 6. The ported rule was "a rejected identity fails at once: retrying
#: only bills", and that was right while an unregistered key was the likely
#: cause. It no longer is: :mod:`trialerror.vastai.sshkeys` asks the ACCOUNT
#: for free BEFORE the create and refuses ``key-missing`` there, so a refusal
#: at the first probe of a host that answered ``Permission denied (publickey)``
#: -- which means its sshd DID complete an auth exchange -- is at least as
#: likely to be the instance not having the account's keys in place yet. A
#: bounded retry costs about $0.004 at $0.16/h against a whole wasted rental;
#: the bound, and the refusal that says how many attempts over how long, are
#: what keep it honest. UNSETTLED LIVE: whether a later attempt succeeds is
#: exactly what ``vastai ssh-probe`` asks for a few cents.
SSH_AUTH_GRACE_S = 90.0
SSH_AUTH_ATTEMPTS = 3

#: Every ``-o`` option :meth:`SshShell.argv` carries, beyond the per-lease
#: ``UserKnownHostsFile`` and the connection timeouts.
SSH_HARDENING_OPTIONS: tuple[str, ...] = (
    "IdentitiesOnly=yes",
    "IdentityAgent=none",
    "ForwardAgent=no",
    "ClearAllForwardings=yes",
    "ForwardX11=no",
    "PermitLocalCommand=no",
    "BatchMode=yes",
    "StrictHostKeyChecking=accept-new",
    "LogLevel=ERROR",
)

_LEASE_ID_RE = re.compile(r"^VOCR-[0-9a-f]{16}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ShellError(VastError):
    """A remote command failed. ``rc`` is ssh's exit status (the remote
    command's, unless it is 255); ``stderr`` its tail."""

    def __init__(self, message: str, *, rc: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.rc = rc
        self.stderr = stderr


class ConnectionLost(ShellError):
    """ssh itself failed (exit 255): the instance died, its sshd went away,
    the proxy refused, or it never became reachable within the budget."""


class IdentityRejected(ShellError):
    """The instance refused the configured identity (``Permission denied``):
    the vast.ai key pair's public half is not registered with the account, or
    ``[vastai] ssh_identity_path`` is not its private half. Another host
    would refuse it too."""


class RemoteTimeout(ShellError):
    """The command did not return within its timeout (the local ``ssh``
    child was killed)."""


class TransferMismatch(ShellError):
    """An upload's remote sha256 or a download's local re-hash disagreed
    with the expected digest after every attempt."""


def q(value: Any) -> str:
    """``shlex.quote`` -- every dynamic part of a remote command goes through it."""
    return shlex.quote(str(value))


def lease_dir(lease_id: str, *, root: str = REMOTE_ROOT_SHM) -> str:
    """``<root>/<lease id>``: the lease's whole footprint on the instance."""
    if not _LEASE_ID_RE.match(str(lease_id)):
        raise ValueError(f"not a vast.ai OCR lease id: {lease_id!r}")
    if root not in (REMOTE_ROOT_SHM, REMOTE_ROOT_DISK):
        raise ValueError(f"not a remote scratch root: {root!r}")
    return f"{root}/{lease_id}"


def known_hosts_path(state_dir: Path | str, lease_id: str) -> Path:
    """The per-lease known_hosts file on DEV: every instance has a fresh host
    key, so trust is on first use, per lease."""
    if not _LEASE_ID_RE.match(str(lease_id)):
        raise ValueError(f"not a vast.ai OCR lease id: {lease_id!r}")
    return Path(state_dir) / "known_hosts" / str(lease_id)


class RemoteShell(Protocol):
    """What the OCR backend needs from a channel. ``run`` returns
    ``(rc, stdout, stderr)`` and raises :class:`RemoteTimeout` /
    :class:`ShellError` only when no exit status exists."""

    def run(self, cmd: str, *, stdin_bytes: bytes | None = None, timeout_s: float) -> tuple[int, bytes, bytes]: ...

    def wait_reachable(
        self,
        *,
        check: Callable[[], None],
        sleep: Callable[[float], None],
        timeout_s: float,
        interval_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None: ...

    def close(self) -> None: ...


class SshShell:
    """The real channel: one ``ssh`` process per command. Nothing here reads
    the identity file; its PATH goes on the argv."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        identity_path: Path | str,
        known_hosts: Path | str,
        user: str = "root",
        ssh_exe: str = "ssh",
        connect_timeout_s: int = 20,
        verbose: bool = False,
        auth_grace_s: float = SSH_AUTH_GRACE_S,
    ) -> None:
        if not _HOST_RE.match(str(host)):
            raise ValueError(f"refusing an ssh host that is not a plain host name: {host!r}")
        if not identity_path:
            raise ValueError("an ssh identity path is required ([vastai] ssh_identity_path)")
        self.host = str(host)
        self.port = int(port)
        self.identity_path = Path(identity_path)
        self.known_hosts = Path(known_hosts)
        self.user = str(user)
        self.ssh_exe = str(ssh_exe)
        self.connect_timeout_s = int(connect_timeout_s)
        self.verbose = bool(verbose)
        self.auth_grace_s = float(auth_grace_s)

    def argv(self, remote_cmd: str | None = None) -> list[str]:
        """The hardened ``ssh`` argv (design 7.2). With ``verbose`` it also
        carries ``-v`` and raises ``LogLevel`` to match (``ssh-probe`` only:
        ``-v`` names the identity file, the key fingerprints it OFFERS and the
        server's replies -- never any private half -- and the probe keeps only
        an allow-list of those lines)."""
        cmd = [self.ssh_exe, "-F", "none", "-T", "-p", str(self.port), "-i", str(self.identity_path)]
        if self.verbose:
            cmd.insert(1, "-v")
        for option in SSH_HARDENING_OPTIONS:
            if self.verbose and option.startswith("LogLevel="):
                option = "LogLevel=DEBUG1"
            cmd += ["-o", option]
        cmd += [
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", f"ConnectTimeout={self.connect_timeout_s}",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=4",
            f"{self.user}@{self.host}",
        ]
        if remote_cmd is not None:
            cmd.append(remote_cmd)
        return cmd

    def run(self, cmd: str, *, stdin_bytes: bytes | None = None, timeout_s: float) -> tuple[int, bytes, bytes]:
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(
                self.argv(cmd),
                input=stdin_bytes if stdin_bytes is not None else b"",
                capture_output=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise RemoteTimeout(f"remote command did not return within {timeout_s:.0f} s") from None
        except OSError as exc:
            raise ShellError(f"could not run {self.ssh_exe!r}: {exc}") from None
        return int(res.returncode), bytes(res.stdout or b""), bytes(res.stderr or b"")

    def wait_reachable(
        self,
        *,
        check: Callable[[], None],
        sleep: Callable[[float], None],
        timeout_s: float,
        interval_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        on_probe: Callable[[int, int, str], None] | None = None,
    ) -> None:
        wait_reachable(self, check=check, sleep=sleep, timeout_s=timeout_s, interval_s=interval_s, clock=clock,
                       probe_timeout_s=self.connect_timeout_s + 30, auth_grace_s=self.auth_grace_s,
                       on_probe=on_probe)

    def close(self) -> None:
        return None


def wait_reachable(
    shell: Any,
    *,
    check: Callable[[], None],
    sleep: Callable[[float], None],
    timeout_s: float,
    interval_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    probe_timeout_s: float = 50.0,
    auth_grace_s: float = 0.0,
    auth_attempts: int = SSH_AUTH_ATTEMPTS,
    on_probe: Callable[[int, int, str], None] | None = None,
) -> None:
    """Block until ``true`` runs on the instance (ported). An instance
    reported ``running`` may still refuse ssh for a while [observed
    2026-09-18]: exit 255 is retried every ``interval_s`` until ``timeout_s``.
    ``check`` is the lease's TTL check, so the watchdog still governs.

    A rejected identity is retried for ``auth_grace_s`` from the first refusal
    and at most ``auth_attempts`` times in all (:data:`SSH_AUTH_GRACE_S` says
    why, and why the bound is both a time and a count), then raises
    :class:`IdentityRejected` naming the attempts and the seconds. With
    ``auth_grace_s = 0`` -- the ported behaviour -- the first refusal raises.

    ``on_probe(attempt, rc, stderr)`` sees every probe's FULL stderr, which is
    how ``ssh-probe`` records what each attempt was told."""
    start = clock()
    first_denied: float | None = None
    denials = 0
    attempt = 0
    while True:
        check()
        attempt += 1
        try:
            rc, _out, err = shell.run("true", timeout_s=probe_timeout_s)
            full = err.decode("utf-8", "replace")
            text = err[-400:].decode("utf-8", "replace").strip()
        except RemoteTimeout:
            rc, text = 255, "ssh probe timed out"
            full = text
        if on_probe is not None:
            on_probe(attempt, rc, full)
        if rc == 0:
            return
        if "Permission denied" in text:
            denials += 1
            now = clock()
            if first_denied is None:
                first_denied = now
            waited = now - first_denied
            if auth_grace_s > 0 and denials < max(1, int(auth_attempts)) and waited < auth_grace_s \
                    and now - start < timeout_s:
                sleep(interval_s)
                continue
            more = (f" after {denials} attempt(s) over {waited:.0f} s" if auth_grace_s > 0 else "")
            raise IdentityRejected(
                f"ssh refused the identity{more} ({text}) -- is the dedicated key pair's public half registered "
                "with the vast.ai account, and [vastai] ssh_identity_path its private half?",
                rc=rc, stderr=text,
            )
        if "Host key verification failed" in text:
            raise ConnectionLost(f"ssh host key verification failed on a fresh per-lease file ({text})", rc=rc,
                                 stderr=text)
        if rc != 255:
            raise ConnectionLost(f"ssh reachability probe failed ({rc}): {text}", rc=rc, stderr=text)
        if clock() - start >= timeout_s:
            raise ConnectionLost(f"ssh not reachable after {timeout_s:.0f} s: {text}", rc=rc, stderr=text)
        sleep(interval_s)


def _tail(data: bytes, n: int = 800) -> str:
    return data[-n:].decode("utf-8", "replace").strip()


def upload_verified(
    shell: Any, data: bytes, remote_path: str, *, expected_sha256: str | None = None, timeout_s: float,
    attempts: int = 2,
) -> str:
    """Write ``data`` to ``remote_path`` through stdin and compare the remote
    ``sha256sum`` with ``expected_sha256`` (default: the bytes' own). One
    re-transfer on a mismatch, then :class:`TransferMismatch`. Returns the
    verified digest."""
    expected = (expected_sha256 or hashlib.sha256(data).hexdigest()).lower()
    cmd = f"cat > {q(remote_path)} && sha256sum -- {q(remote_path)}"
    seen = ""
    for _attempt in range(max(1, attempts)):
        rc, out, err = shell.run(cmd, stdin_bytes=data, timeout_s=timeout_s)
        if rc == 255:
            raise ConnectionLost(f"ssh lost during an upload: {_tail(err)}", rc=rc, stderr=_tail(err))
        if rc != 0:
            raise ShellError(f"upload to the instance failed ({rc}): {_tail(err)}", rc=rc, stderr=_tail(err))
        seen = out.decode("utf-8", "replace").strip().split(" ", 1)[0].lower()
        if seen == expected:
            return seen
    raise TransferMismatch(
        f"upload hash mismatch after {attempts} attempt(s): the instance holds {seen[:12] or 'nothing'}..., "
        f"expected {expected[:12]}..."
    )


def download_verified(
    shell: Any, remote_path: str, *, expected_sha256: str, timeout_s: float, attempts: int = 2
) -> bytes:
    """``cat`` ``remote_path`` back and re-hash it here; one re-fetch on a
    mismatch, then :class:`TransferMismatch`."""
    expected = str(expected_sha256 or "").lower()
    if not _SHA256_RE.match(expected):
        raise TransferMismatch(f"the instance reported no valid sha256 for {remote_path}")
    got = ""
    for _attempt in range(max(1, attempts)):
        rc, out, err = shell.run(f"cat -- {q(remote_path)}", timeout_s=timeout_s)
        if rc == 255:
            raise ConnectionLost(f"ssh lost during a download: {_tail(err)}", rc=rc, stderr=_tail(err))
        if rc != 0:
            raise ShellError(f"download from the instance failed ({rc}): {_tail(err)}", rc=rc, stderr=_tail(err))
        got = hashlib.sha256(out).hexdigest()
        if got == expected:
            return out
    raise TransferMismatch(
        f"download hash mismatch after {attempts} attempt(s): got {got[:12]}..., the instance reported {expected[:12]}..."
    )
