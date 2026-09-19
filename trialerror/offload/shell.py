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

C-0097 adds ONE rule to this port and no verbs: ``heartbeat`` may carry an
optional progress payload on stdin, and :func:`progress_payload_refusal` is
the wrapper's verdict on it (refused as exit 4, the stamp untouched -- a
status file the sandbox cannot trust must not also cost the job its claim).
The reply word the verb prints is :data:`CONTROL_WORDS`.
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
    "MAX_PROGRESS_BYTES",
    "PAYLOAD_WHITESPACE",
    "RESERVED_JOB_IDS",
    "RESERVED_JOB_ID_SUFFIX",
    "PROGRESS_KEYS",
    "PROGRESS_SETTINGS_KEYS",
    "PROGRESS_ALLOWED_KEYS",
    "CONTROL_WORDS",
    "ParsedCommand",
    "parse_command",
    "progress_payload_refusal",
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

#: C-0097 D3: the ceiling on the optional progress payload a worker sends on
#: ``heartbeat``'s stdin, in bytes. Same posture as SEC-4's transfer caps --
#: the verb FAILS past the cap rather than truncating, because a truncated
#: status file is a lie about a running job rather than a smaller truth. The
#: same number is ``TE_OFFLOAD_MAX_PROGRESS_BYTES`` in
#: ``deploy/sandbox/offload-shell.sh``; keep them together.
MAX_PROGRESS_BYTES = 4096

#: D3's key list, verbatim. A payload key outside this set is refused rather
#: than ignored: the file is read by the dashboard, two doctor checks and a
#: CLI verb, and "a key nobody reads" in it is a worker and a sandbox that
#: disagree about the protocol -- worth a refusal while it is still one
#: release old.
PROGRESS_KEYS = (
    "worker_id",
    "state",
    "kind",
    "job_id",
    "units_done",
    "units_total",
    "unit",
    "started_ts",
    "pace_s_per_unit",
    "eta_s",
    "settings",
    "control_seen",
    "last_error",
)

#: The keys allowed INSIDE ``settings`` (D3, plus D9's ``resident_backends``).
PROGRESS_SETTINGS_KEYS = ("batch_size", "backend", "model_key", "code_tip", "resident_backends")

#: What the wrapper's key check actually compares against. The wrapper has no
#: JSON parser (POSIX ``sh`` on the queue host, no Python out there), so it
#: checks that every quoted key token ANYWHERE in the payload is one this
#: protocol knows -- the union of the two tuples above. The nesting itself
#: (``settings`` is an object, ``units_done`` is a number) is checked by the
#: Python side that BUILDS the payload
#: (:func:`trialerror.offload.control.validate_progress`), which is the half
#: that can parse it. Both halves agree exactly on the three refusals this
#: port exists to mirror: over the cap, not an object, unknown key.
PROGRESS_ALLOWED_KEYS = frozenset(PROGRESS_KEYS) | frozenset(PROGRESS_SETTINGS_KEYS)

#: C-0097 FIX V-11: ``claimed/<worker>/`` holds ``CONTROL.json`` and
#: ``<job>.progress.json`` beside the job manifests, so a job called ``CONTROL``
#: or one whose id ends in ``.progress`` would have a reader find a status file
#: where it expected a manifest. Nothing mints such an id; all three
#: implementations of the id rule refuse it anyway.
RESERVED_JOB_IDS = ("CONTROL",)
RESERVED_JOB_ID_SUFFIX = ".progress"

#: The bytes both halves treat as surrounding whitespace on a payload. FIX V-8:
#: this used to be ``bytes.strip()``, which also strips ``\v`` and ``\f`` --
#: the wrapper's ``tr -d ' \t\r\n'`` does not, so a payload of one vertical tab
#: was "no payload" here and "must be one JSON object" out there. Four bytes, one
#: rule, both halves.
PAYLOAD_WHITESPACE = b" \t\r\n"

#: What ``heartbeat`` prints on stdout (C-0097 D1). ``none`` is a word, not an
#: empty line: a worker that read a blank reply could not tell "no request"
#: from "the reply was lost".
CONTROL_WORDS = ("none", "pause", "resume", "stop")

#: Every ``"key":`` token in a payload, which is what the wrapper's own
#: ``grep -o`` finds. Deliberately the same shape: a key is a double-quoted
#: run of word characters followed by optional blanks and a colon.
#: Blanks only, never a newline: the wrapper's ``grep -o`` is LINE-based, so
#: a key split across lines from its colon is not a key token out there, and
#: this port must see exactly what it sees. Every payload this protocol
#: actually produces is compact JSON (one line) or pretty-printed JSON (key
#: and colon on one line), so the two readings only ever differ on input
#: nothing writes.
_PROGRESS_KEY_TOKEN = re.compile(rb'"([A-Za-z_][A-Za-z0-9_]*)"[ \t]*:')


def progress_payload_refusal(raw: bytes | None) -> str | None:
    """``None`` if the wrapper would STORE this heartbeat payload, else the
    reason it refuses (exit :data:`EXIT_IO`, the stamp untouched).

    The three rules, in the wrapper's own order:

    1. **over the cap** -- more than :data:`MAX_PROGRESS_BYTES` bytes;
    2. **not an object** -- the payload, stripped of surrounding whitespace,
       must open with ``{`` and close with ``}``;
    3. **an unknown key** -- every ``"name":`` token in it must be in
       :data:`PROGRESS_ALLOWED_KEYS`.

    An EMPTY payload (no stdin at all, which is what every caller before
    C-0097 sent) is not a refusal and not a payload: the verb stamps and
    prints its word exactly as it always did, and no progress file is
    written. That is the whole back-compatibility story for the six other
    verbs' worth of existing callers. A WHITESPACE-ONLY payload is the same
    thing in both halves (FIX V-8), stripped of the same four bytes
    (:data:`PAYLOAD_WHITESPACE`)."""
    if raw is None or raw.strip(PAYLOAD_WHITESPACE) == b"":
        return None
    if len(raw) > MAX_PROGRESS_BYTES:
        return (
            f"heartbeat payload is {len(raw)} bytes, over the {MAX_PROGRESS_BYTES}-byte cap "
            "-- refused, not truncated"
        )
    body = raw.strip(PAYLOAD_WHITESPACE)
    if not (body.startswith(b"{") and body.endswith(b"}")):
        return "heartbeat payload must be one JSON object"
    for match in _PROGRESS_KEY_TOKEN.finditer(body):
        key = match.group(1).decode("ascii", "replace")
        if key not in PROGRESS_ALLOWED_KEYS:
            return f"heartbeat payload carries an unknown key {key!r}"
    return None


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
    # C-0097 FIX V-11: the two names the claim directory reserves for a worker's
    # own status files. Refused in all three implementations of this rule -- here,
    # in `protocol.validate_job_id`, and in the wrapper -- so the halves cannot
    # disagree about an id that would make a status file look like a manifest.
    if job_id in RESERVED_JOB_IDS or job_id.endswith(RESERVED_JOB_ID_SUFFIX):
        return _bad(EXIT_BAD_ID, f"refused job id {job_id!r} (reserved name)")
    if not _JOB_ID_RE.match(job_id):
        return _bad(EXIT_BAD_ID, f"refused job id {job_id!r}")
    return ParsedCommand(ok=True, exit_code=EXIT_OK, verb=verb, job_id=job_id)
