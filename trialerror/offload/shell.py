"""A Python port of ``deploy/sandbox/offload-shell.sh``'s command parsing
and refusal rules.

The restricted DEV worker key on the queue host carries
``restrict,command="…/offload-shell.sh"``, so the ONLY thing that key can
ever do is hand a string to that wrapper as ``$SSH_ORIGINAL_COMMAND``.
**That wrapper is the security of the key** (design section 4). Its rules
are therefore worth stating twice: once in POSIX shell, where they
actually run on the host (no Python, no harness install out there), and
once here, where they can be unit-tested on any platform and reused by
:class:`trialerror.offload.transport.LocalTransport` so an in-process test
run goes through the same gate a real SSH request does.

``tests/test_offload_shell.py`` drives ONE refusal table through both
implementations -- this module directly, and the shell script under ``sh``
when an ``sh`` is available -- so the two can never quietly diverge.

Exit codes (shared with the shell script, which is why they are constants
here and not bare integers)::

    0  accepted
    2  unknown or missing verb
    3  malformed job id
    4  protocol/IO failure while performing an accepted verb
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "EXIT_OK",
    "EXIT_BAD_VERB",
    "EXIT_BAD_ID",
    "EXIT_IO",
    "VERBS",
    "VERBS_WITH_ID",
    "ParsedCommand",
    "parse_command",
]

EXIT_OK = 0
EXIT_BAD_VERB = 2
EXIT_BAD_ID = 3
EXIT_IO = 4

#: design section 4: "accepts only: list | claim <id> | pull <id> |
#: push <id> | publish <id> | return <id> | heartbeat <id>".
VERBS = ("list", "claim", "pull", "push", "publish", "return", "heartbeat")
VERBS_WITH_ID = tuple(v for v in VERBS if v != "list")

#: SEC-6: ``\Z``, not ``$`` -- Python's ``$`` also matches immediately
#: before a trailing newline, so ``"abc\\n"`` would have been accepted as
#: the job id ``abc``. The shell's ``case`` glob has no such allowance, so
#: ``$`` made the Python half of a deliberately-identical rule the looser
#: one. Same change in ``trialerror.offload.protocol.JOB_ID_RE``.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")

#: The characters ``sh`` splits ``$SSH_ORIGINAL_COMMAND`` on with the
#: default ``IFS`` -- space, tab, newline, and nothing else (SEC-6).
_IFS_SPLIT = re.compile(r"[ \t\n]+")


@dataclass(frozen=True)
class ParsedCommand:
    """The wrapper's verdict on one ``$SSH_ORIGINAL_COMMAND``."""

    ok: bool
    exit_code: int
    verb: str | None = None
    job_id: str | None = None
    reason: str = ""


def _bad(code: int, reason: str) -> ParsedCommand:
    return ParsedCommand(ok=False, exit_code=code, reason=reason)


def parse_command(command: str | None) -> ParsedCommand:
    r"""Apply the wrapper's rules to one command string.

    Splitting is on the shell's own ``IFS`` -- space, tab and newline --
    matching ``set -- $SSH_ORIGINAL_COMMAND`` exactly: there is no quoting,
    no globbing (the script sets ``set -f``) and no shell evaluation of the
    request anywhere, so a request can never be more than "a verb and at
    most one word".

    SEC-6: this used to be ``str.split()``, which splits on ALL Python
    whitespace -- ``\r``, ``\v``, ``\f`` and the unicode spaces
    included. ``sh`` splits on none of those, so ``"claim a\rb"`` was two
    words here and one (refused) word there: the Python oracle was quietly
    accepting a shape the wrapper rejects, which is the one thing this
    port exists to prevent.
    """
    if command is None:
        return _bad(EXIT_BAD_VERB, "no command (interactive login refused)")
    parts = [w for w in _IFS_SPLIT.split(command) if w]
    if not parts:
        return _bad(EXIT_BAD_VERB, "empty command")
    verb = parts[0]
    if verb not in VERBS:
        return _bad(EXIT_BAD_VERB, f"unknown verb {verb!r}")
    if verb == "list":
        if len(parts) != 1:
            return _bad(EXIT_BAD_VERB, "verb 'list' takes no argument")
        return ParsedCommand(ok=True, exit_code=EXIT_OK, verb="list")
    if len(parts) != 2:
        return _bad(EXIT_BAD_VERB, f"verb {verb!r} takes exactly one job id")
    job_id = parts[1]
    if job_id in (".", ".."):
        return _bad(EXIT_BAD_ID, f"refused job id {job_id!r} (path traversal)")
    if not _JOB_ID_RE.match(job_id):
        return _bad(EXIT_BAD_ID, f"refused job id {job_id!r}")
    return ParsedCommand(ok=True, exit_code=EXIT_OK, verb=verb, job_id=job_id)
