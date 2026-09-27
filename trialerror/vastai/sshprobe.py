"""``vastai ssh-probe`` (round 6): the cheapest live answer to one question --
can the configured identity authenticate on a rented instance?

**Why it exists.** On 2026-09-19 the canary reached ``Permission denied
(publickey)`` 313 s and $0.0309 into a rental, and the account's key list read
back ``registered`` an hour later. The two readings are both true only if
something OTHER than registration refused the key: the instance not having the
account's keys in place yet, a different ``ssh`` binary or identity file than
the one that worked on 2026-09-18, or the proxy host/port. None of that can be
settled offline, and the canary is an expensive way to ask: it rents, boots,
pulls an image, installs wheels and downloads models before it needs ssh at
all.

**What this does instead.** Rent the cheapest offer the SAME egress policy
admits, wait for ``running``, attempt ssh with the bounded auth grace
(:data:`trialerror.vastai.shell.SSH_AUTH_GRACE_S`), record what each attempt
was told, run ``nvidia-smi -L`` if it gets in, destroy, confirm destroyed.
Nothing is uploaded: no document, no queue key, no bootstrap. At $0.16/h a
probe that answers in five minutes costs about **$0.013**, and its TTL caps the
worst case.

**What is recorded, and what is not.** ``ssh -v`` names the identity FILE, the
key fingerprints it OFFERS and the server's replies; it never prints a private
half. This module keeps only lines matching :data:`VERBOSE_KEEP` and drops
anything else unread, including any line carrying a base64 run long enough to
be a key blob (:data:`_BLOB`). The count of dropped lines is reported so that a
silent filter cannot hide a failure. **The error text too:** the shell's own
exceptions embed a raw tail of that stderr, so what reaches
``ProbeOutcome.error`` passes the same filter (:func:`_safe_error`) -- at
``DEBUG1`` OpenSSH prints no private half, and this keeps that from being the
only reason it holds.

No document, no queue credential and no key text leaves this machine. The
probe makes vast.ai calls and one ssh connection **only** when a custodian runs
it with ``--rent``; every test drives it on fakes.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from trialerror.vastai import shell as sh

__all__ = [
    "VERBOSE_KEEP",
    "NVIDIA_SMI_CMD",
    "NOTHING_SHA256",
    "ProbeAttempt",
    "ProbeOutcome",
    "sanitize_verbose",
    "run_ssh_probe",
]

#: The ONLY ``ssh -v`` lines this probe keeps: what identity file was read and
#: whether it parsed, which local and remote ssh answered, the host key's
#: fingerprint, which key fingerprints were OFFERED, what the server said, and
#: the errors about the identity file's permissions. Each entry is a substring
#: (matched case-sensitively, as OpenSSH writes it).
VERBOSE_KEEP: tuple[str, ...] = (
    "Local version string",
    "Remote protocol version",
    "Connecting to",
    "Connection established",
    "identity file",
    "Identity file",
    "Server host key",
    "Host key fingerprint",
    "Offering public key",
    "Offering key",
    "Trying private key",
    "Server accepts key",
    "Authentications that can continue",
    "Next authentication method",
    "Authentication succeeded",
    "Authenticated to",
    "Permission denied",
    "No more authentication methods",
    "Too many authentication failures",
    "UNPROTECTED PRIVATE KEY FILE",
    "Permissions for",
    "bad permissions",
    "Load key",
    "kex_exchange_identification",
    "Connection closed by",
    "Connection reset by",
    "connect to host",
    "Operation timed out",
    "banner exchange",
)

#: A base64 run this long is a key blob, not a fingerprint (``SHA256:`` plus 43
#: characters). A kept line carrying one is dropped anyway.
_BLOB = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")

#: What the probe asks the instance for once it is in: one line, the card's
#: name. It is the whole payload of a successful probe.
NVIDIA_SMI_CMD = "nvidia-smi -L"

#: The ledger wants a document digest and a byte size on every ``intent``
#: row (the charter's record of what left the machine). A probe sends no
#: bytes, so its row carries the digest OF NO BYTES and ``bytes = 0``: a
#: probe row is recognisable, and the accounting stays in the two kinds the
#: spend view already reads.
NOTHING_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass
class ProbeAttempt:
    """One ssh attempt: its exit status and the lines kept from its stderr."""

    attempt: int
    rc: int
    lines: list[str] = field(default_factory=list)
    dropped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"attempt": self.attempt, "rc": self.rc, "lines": list(self.lines), "dropped": self.dropped}


@dataclass
class ProbeOutcome:
    """What the probe learned. ``authenticated`` is the answer; everything else
    is the evidence for the next decision."""

    authenticated: bool = False
    attempts: list[ProbeAttempt] = field(default_factory=list)
    denials: int = 0
    elapsed_s: float = 0.0
    gpu: str | None = None
    error: str | None = None
    error_kind: str | None = None

    @property
    def verdict(self) -> str:
        if self.authenticated:
            return "authenticated"
        return self.error_kind or "no-answer"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "authenticated": self.authenticated,
            "denials": self.denials,
            "attempts": [a.as_dict() for a in self.attempts],
            "elapsed_s": round(self.elapsed_s, 1),
            "gpu": self.gpu,
            "error": self.error,
            "error_kind": self.error_kind,
        }

    def reading(self) -> str:
        """One sentence a runbook can act on."""
        if self.authenticated:
            if self.denials:
                return (
                    f"the identity authenticated on attempt {len(self.attempts)} after {self.denials} "
                    f"Permission denied over {self.elapsed_s:.0f} s: the refusal is a RACE, not the key. Keep the "
                    "auth grace (raise it if the denials used it all up) and the canary can run."
                )
            return (
                "the identity authenticated on the first attempt: ssh through the vast.ai proxy works with this "
                "key, this binary and this file. The 2026-09-19 refusal was not reproduced."
            )
        if self.error_kind == "identity-rejected":
            return (
                f"every attempt was refused ({self.denials} over {self.elapsed_s:.0f} s) while the account's key "
                "list says registered: NOT a race. Compare the offered fingerprint in the kept lines with the "
                "registered one, and the local version string with the binary that worked on 2026-09-18."
            )
        return f"no auth verdict ({self.error_kind}): {self.error}"


def sanitize_verbose(text: str, *, keep: tuple[str, ...] = VERBOSE_KEEP) -> tuple[list[str], int]:
    """``(kept lines, how many were dropped)``. A line is kept only if it
    carries one of ``keep`` AND no base64 run long enough to be a key blob.
    ``debug`` prefixes are left as OpenSSH wrote them, so the evidence reads
    like the terminal it came from."""
    kept: list[str] = []
    dropped = 0
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if any(token in line for token in keep) and not _BLOB.search(line):
            kept.append(line[:300])
        else:
            dropped += 1
    return kept, dropped


def _safe_error(exc: BaseException) -> str:
    """A shell exception's text with the raw ``ssh`` tail it embeds passed
    through :func:`sanitize_verbose`.

    :func:`trialerror.vastai.shell.wait_reachable` puts ``stderr[-400:]``
    verbatim into the message it raises, and that message is what an operator
    reads first (``ProbeOutcome.error``, the envelope, ``--log-file``). Keeping
    it as it stands would walk host text past the allow-list this module's
    contract rests on. Where the filter keeps nothing, the class and the count
    of dropped lines stand in, so a filtered-away message is still visible AS
    one -- the same rule the kept lines follow."""
    kept, dropped = sanitize_verbose(str(exc))
    if kept:
        return "; ".join(kept)[:400]
    return f"{exc.__class__.__name__}: no line passed the allow-list ({dropped} dropped)"


def run_ssh_probe(
    shell: Any,
    *,
    check: Callable[[], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_s: float,
    interval_s: float = 10.0,
    auth_grace_s: float = sh.SSH_AUTH_GRACE_S,
    auth_attempts: int = sh.SSH_AUTH_ATTEMPTS,
    nvidia_cmd: str = NVIDIA_SMI_CMD,
    log: Callable[[str], None] | None = None,
) -> ProbeOutcome:
    """Attempt ssh on a running instance and report what happened. Never
    raises for an ssh answer: a refusal, a lost connection or a timeout is the
    result. ``check`` is the lease's TTL check and still governs."""
    out = ProbeOutcome()
    started = clock()
    say = log or (lambda _message: None)

    def on_probe(attempt: int, rc: int, stderr: str) -> None:
        lines, dropped = sanitize_verbose(stderr)
        out.attempts.append(ProbeAttempt(attempt=attempt, rc=rc, lines=lines, dropped=dropped))
        say(f"  . ssh attempt {attempt}: rc {rc}, {len(lines)} line(s) kept, {dropped} dropped")
        for line in lines:
            say(f"    | {line}")

    try:
        sh.wait_reachable(
            shell, check=check, sleep=sleep, timeout_s=timeout_s, interval_s=interval_s, clock=clock,
            auth_grace_s=auth_grace_s, auth_attempts=auth_attempts, on_probe=on_probe,
        )
        out.authenticated = True
    except sh.IdentityRejected as exc:
        out.error, out.error_kind = _safe_error(exc), "identity-rejected"
    except sh.ConnectionLost as exc:
        out.error, out.error_kind = _safe_error(exc), "connection-lost"
    except sh.ShellError as exc:  # a local ssh that will not run at all
        out.error, out.error_kind = _safe_error(exc), "shell-error"
    # "Permission denied" is in VERBOSE_KEEP, so it survives the filter at any
    # log level: an attempt that carries it was an auth refusal.
    out.denials = sum(1 for a in out.attempts if any("Permission denied" in line for line in a.lines))
    out.elapsed_s = clock() - started
    if out.authenticated:
        try:
            rc, stdout, stderr = shell.run(nvidia_cmd, timeout_s=60.0)
            first = (stdout or b"").decode("utf-8", "replace").strip().splitlines()
            out.gpu = first[0][:200] if rc == 0 and first else None
            if rc != 0:
                kept, _dropped = sanitize_verbose((stderr or b"").decode("utf-8", "replace"))
                out.error = f"{nvidia_cmd} exited {rc}" + (f": {kept[0]}" if kept else "")
                out.error_kind = "gpu-check-failed"
        except sh.ShellError as exc:
            out.error, out.error_kind = _safe_error(exc), "gpu-check-failed"
        say(f"  . {nvidia_cmd}: {out.gpu or out.error}")
    say(f"= ssh probe: {out.reading()}")
    return out
