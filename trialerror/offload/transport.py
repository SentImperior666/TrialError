"""How the DEV worker reaches the queue: the seven-verb transport.

Two implementations of one small interface:

- :class:`SshTransport` -- the real thing. One ``ssh <host> <verb> [<id>]``
  per verb, against an alias (``Host te-offload``) whose key is
  forced-command-restricted to ``offload-shell.sh``. Deliberately NO
  ``ControlMaster`` multiplexing: Windows OpenSSH does not support it
  (design section 4, operator-experience gap 7), so one connection per
  verb is the honest shape rather than a fast path that only works on the
  developer's other OS.
- :class:`LocalTransport` -- the same seven verbs against a queue on this
  machine's own filesystem, through
  :mod:`trialerror.offload.protocol`'s server functions and
  :func:`trialerror.offload.shell.parse_command`'s gate. This is what the
  tests drive (no SSH anywhere in the suite, design section 4's C-unit
  row) and it is also what a same-machine worker would use if DEV and the
  program root ever sat on one box.

Both raise :class:`TransportError` for a failed verb; the worker treats
that as "this job is not mine right now" and moves on, never as a reason
to recompute work it already holds.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

from trialerror.offload import protocol
from trialerror.offload.shell import parse_command

__all__ = ["TransportError", "Transport", "LocalTransport", "SshTransport"]


class TransportError(RuntimeError):
    """A verb was refused or could not be delivered."""


class Transport(Protocol):
    """The seven verbs, and nothing else."""

    def list_jobs(self) -> list[str]: ...
    def claim(self, job_id: str) -> dict[str, Any]: ...
    def pull(self, job_id: str) -> bytes: ...
    def push(self, job_id: str, data: bytes) -> None: ...
    def publish(self, job_id: str) -> None: ...
    def return_job(self, job_id: str) -> None: ...
    def heartbeat(self, job_id: str) -> None: ...


class LocalTransport:
    """In-process transport against a queue directory on this filesystem.

    Every call is routed through :func:`parse_command` first, so a test
    that drives this class exercises the same verb/id validation the SSH
    wrapper applies -- a job id that the wrapper would refuse is refused
    here too, rather than sneaking past because Python was handed a
    already-parsed argument.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        worker_id: str = "dev",
        max_payload_bytes: int | None = None,
    ):
        self.root = protocol.ensure_layout(Path(root))
        self.worker_id = worker_id
        self.max_payload_bytes = max_payload_bytes

    # -- gate -------------------------------------------------------------
    def _gate(self, verb: str, job_id: str | None = None) -> None:
        command = verb if job_id is None else f"{verb} {job_id}"
        parsed = parse_command(command)
        if not parsed.ok:
            raise TransportError(f"offload-shell would refuse {command!r}: {parsed.reason}")

    # -- verbs ------------------------------------------------------------
    def list_jobs(self) -> list[str]:
        self._gate("list")
        return protocol.server_list(self.root)

    def claim(self, job_id: str) -> dict[str, Any]:
        self._gate("claim", job_id)
        try:
            return protocol.server_claim(self.root, job_id, worker_id=self.worker_id)
        except protocol.OffloadError as exc:
            raise TransportError(str(exc)) from exc

    def pull(self, job_id: str) -> bytes:
        self._gate("pull", job_id)
        try:
            return protocol.server_pull(self.root, job_id, worker_id=self.worker_id)
        except protocol.OffloadError as exc:
            raise TransportError(str(exc)) from exc

    def push(self, job_id: str, data: bytes) -> None:
        self._gate("push", job_id)
        try:
            protocol.check_payload_size(data, f"push {job_id}", max_bytes=self.max_payload_bytes)
            protocol.server_push(self.root, job_id, data, worker_id=self.worker_id)
        except protocol.OffloadError as exc:
            raise TransportError(str(exc)) from exc

    def publish(self, job_id: str) -> None:
        self._gate("publish", job_id)
        try:
            protocol.server_publish(self.root, job_id, worker_id=self.worker_id)
        except protocol.OffloadError as exc:
            raise TransportError(str(exc)) from exc

    def return_job(self, job_id: str) -> None:
        self._gate("return", job_id)
        try:
            protocol.server_return(self.root, job_id, worker_id=self.worker_id)
        except protocol.OffloadError as exc:
            raise TransportError(str(exc)) from exc

    def heartbeat(self, job_id: str) -> None:
        self._gate("heartbeat", job_id)
        try:
            protocol.server_heartbeat(self.root, job_id, worker_id=self.worker_id)
        except protocol.OffloadError as exc:
            raise TransportError(str(exc)) from exc


class SshTransport:
    """``ssh <host> <verb> [<job_id>]`` -- one connection per verb.

    ``host`` is an ``~/.ssh/config`` alias, not a hostname: the alias is
    where ``IdentityFile ~/.ssh/te_offload`` and ``IdentitiesOnly yes``
    live, so the worker can never accidentally present the operator's
    general-purpose key to the offload endpoint (design section 4 /
    setup-day step 6).

    ``timeout_s`` bounds every verb. ``pull``/``push`` carry the payloads,
    so they get their own, larger bound -- and, since SEC-4, a SIZE bound
    as well: a verb whose payload exceeds ``max_payload_bytes`` fails
    rather than transferring, matching the wrapper's own refusal on the
    far side. Truncating instead would hand the sandbox a corrupt archive
    that cannot satisfy the manifest it will be checked against.
    """

    def __init__(
        self,
        host: str,
        *,
        ssh_exe: str = "ssh",
        timeout_s: float = 60.0,
        transfer_timeout_s: float = 1800.0,
        max_payload_bytes: int | None = None,
        extra_args: tuple[str, ...] = (),
    ):
        self.host = host
        self.ssh_exe = ssh_exe
        self.timeout_s = timeout_s
        self.transfer_timeout_s = transfer_timeout_s
        self.max_payload_bytes = max_payload_bytes
        self.extra_args = tuple(extra_args)

    def _run(
        self, verb: str, job_id: str | None = None, *, stdin: bytes | None = None, timeout_s: float | None = None
    ) -> bytes:
        parsed = parse_command(verb if job_id is None else f"{verb} {job_id}")
        if not parsed.ok:
            # Refuse locally too: a malformed id must not even reach the wire.
            raise TransportError(f"offload: {parsed.reason}")
        argv = [self.ssh_exe, *self.extra_args, self.host, verb]
        if job_id is not None:
            argv.append(job_id)
        try:
            completed = subprocess.run(
                argv,
                input=stdin if stdin is not None else b"",
                capture_output=True,
                timeout=timeout_s if timeout_s is not None else self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"offload: `{verb}` timed out against {self.host}") from exc
        except OSError as exc:
            raise TransportError(f"offload: could not run {self.ssh_exe!r}: {exc}") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise TransportError(
                f"offload: `{verb}{'' if job_id is None else ' ' + job_id}` failed on {self.host} "
                f"(exit {completed.returncode}): {stderr[-500:]}"
            )
        return completed.stdout or b""

    def list_jobs(self) -> list[str]:
        out = self._run("list").decode("utf-8", "replace")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def claim(self, job_id: str) -> dict[str, Any]:
        out = self._run("claim", job_id)
        try:
            return json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"offload claim {job_id}: manifest was not JSON") from exc

    def pull(self, job_id: str) -> bytes:
        data = self._run("pull", job_id, timeout_s=self.transfer_timeout_s)
        return self._bound(data, f"pull {job_id}")

    def push(self, job_id: str, data: bytes) -> None:
        # SEC-4: refused HERE too, not only by the wrapper -- a payload the
        # far side is going to reject should not spend half an hour on the
        # wire first.
        self._bound(data, f"push {job_id}")
        self._run("push", job_id, stdin=data, timeout_s=self.transfer_timeout_s)

    def _bound(self, data: bytes, verb: str) -> bytes:
        try:
            return protocol.check_payload_size(data, verb, max_bytes=self.max_payload_bytes)
        except protocol.OffloadProtocolError as exc:
            raise TransportError(str(exc)) from exc

    def publish(self, job_id: str) -> None:
        self._run("publish", job_id)

    def return_job(self, job_id: str) -> None:
        self._run("return", job_id)

    def heartbeat(self, job_id: str) -> None:
        self._run("heartbeat", job_id)
