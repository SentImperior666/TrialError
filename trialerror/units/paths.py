"""Classify a path under ``~/.claude/projects/<slug>/`` into the kind of
transcript file it is, and pull the ids out of the path itself.

Design Section 2.2 (``paths.py``): re-derive the classification/parent-session
rules an existing internal transcript-inventory tool used, rather than
importing them -- this package has no dependency on that tool, and its own
classification vocabulary (``FileKind``) is not that tool's.

Layout observed on DEV (design Section 1, verified against
``~/.claude/projects`` on 2026-09-27), relative to one project slug
directory::

    <sessionId>.jsonl                                          main
    <sessionId>/subagents/agent-<id>.jsonl                     subagent transcript
    <sessionId>/subagents/agent-<id>.meta.json                 subagent meta
    <sessionId>/subagents/workflows/wf_<id>/agent-<id>.meta.json  workflow-agent meta
    <sessionId>/subagents/workflows/wf_<id>/journal.jsonl      workflow journal
    <sessionId>/workflows/wf_<id>.json                         ignored (not a transcript)
    <sessionId>/tool-results/...                               ignored
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

__all__ = ["FileKind", "Classified", "classify"]


class FileKind:
    """The kinds :func:`classify` recognizes. Plain string constants (not an
    ``enum.Enum``) so a classified value round-trips through the ``unit.kind``
    CHECK constraint's own string vocabulary without a translation layer --
    note ``MAIN``/``SUBAGENT_JSONL`` are two *file* kinds that both map onto
    the *unit* kind ``'main'``/``'subagent'`` (:mod:`trialerror.units.scan`
    does that mapping, not this module)."""

    MAIN = "main"
    SUBAGENT_JSONL = "subagent_jsonl"
    SUBAGENT_META = "subagent_meta"
    WORKFLOW_AGENT_META = "workflow_agent_meta"
    WORKFLOW_JOURNAL = "workflow_journal"


_AGENT_PREFIX = "agent-"
_WF_RUN_RE = re.compile(r"^(wf_[^/\\]+)$")


@dataclass(frozen=True)
class Classified:
    kind: str
    session_id: str
    agent_id: str | None
    workflow_run_id: str | None


def _agent_id_from_stem(stem: str) -> str | None:
    """``stem`` is the filename with its ENTIRE ``agent-`` suffix removed
    already (``<id>`` from ``agent-<id>.jsonl``'s ``.jsonl``-stripped stem,
    or from ``agent-<id>.meta.json``'s ``.meta.json``-stripped stem -- each
    call site strips its own full suffix before calling this).

    S-5 fix round: this used to take only a LEADING RUN OF HEX CHARACTERS
    (``^agent-([0-9a-fA-F]+)``), so ``agent-acompact-7f3e2a`` and
    ``agent-acompact-9d1c44`` both truncated to id ``"ac"`` -- two different
    subagent files silently sharing one unit key -- and ``agent-xyz`` (no
    hex run at all right after the prefix) matched nothing and produced
    ``agent_id=None``, which collided with the SESSION's own main unit key
    (``<host>/<session>/-``). The id is now the WHOLE remainder after
    ``agent-``, whatever characters it contains -- never truncated, and
    never ``None`` for a file that genuinely has the ``agent-`` prefix."""
    if not stem.startswith(_AGENT_PREFIX):
        return None
    rest = stem[len(_AGENT_PREFIX) :]
    return rest or None  # "agent-.jsonl" (empty id) is not a real id either


def classify(rel_path: str) -> Classified | None:
    """Classify ``rel_path`` (POSIX- or Windows-style, relative to one
    project slug directory) into a :class:`Classified`, or ``None`` when it
    is not a file this lane reads at all (a ``workflows/wf_<id>.json``
    manifest, ``tool-results/``, ``memory/``, a stray non-jsonl file, ...).

    Never touches the filesystem -- pure string/path logic, so it is cheap
    to call on every path a directory walk yields before deciding whether to
    open the file.
    """
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if not parts:
        return None
    session_id = parts[0]

    # main: exactly one path component, a bare "<sessionId>.jsonl".
    if len(parts) == 1:
        name = parts[0]
        if name.endswith(".jsonl"):
            main_session_id = name[: -len(".jsonl")]
            return Classified(kind=FileKind.MAIN, session_id=main_session_id, agent_id=None, workflow_run_id=None)
        return None

    if "subagents" not in parts:
        return None
    sub_idx = parts.index("subagents")
    tail = parts[sub_idx + 1 :]
    if not tail:
        return None

    if tail[0] == "workflows":
        # <sessionId>/subagents/workflows/wf_<id>/{agent-<id>.meta.json,journal.jsonl}
        wf_tail = tail[1:]
        if len(wf_tail) != 2:
            return None
        wf_part, filename = wf_tail
        if not _WF_RUN_RE.match(wf_part):
            return None
        workflow_run_id = wf_part
        if filename == "journal.jsonl":
            return Classified(
                kind=FileKind.WORKFLOW_JOURNAL,
                session_id=session_id,
                agent_id=None,
                workflow_run_id=workflow_run_id,
            )
        if filename.endswith(".meta.json"):
            agent_id = _agent_id_from_stem(filename[: -len(".meta.json")])
            if agent_id is None:
                # S-5: never map a subagent/workflow-agent file to
                # agent_id=None -- that collides with the SESSION's own
                # main unit key. Ignore the file (and count it) instead.
                return None
            return Classified(
                kind=FileKind.WORKFLOW_AGENT_META,
                session_id=session_id,
                agent_id=agent_id,
                workflow_run_id=workflow_run_id,
            )
        return None

    # <sessionId>/subagents/agent-<id>.{jsonl,meta.json} -- not workflows.
    if len(tail) != 1:
        return None
    filename = tail[0]
    if filename.endswith(".meta.json"):
        agent_id = _agent_id_from_stem(filename[: -len(".meta.json")])
        if agent_id is None:
            return None  # S-5: see the WORKFLOW_AGENT_META branch's own note above
        return Classified(kind=FileKind.SUBAGENT_META, session_id=session_id, agent_id=agent_id, workflow_run_id=None)
    if filename.endswith(".jsonl"):
        agent_id = _agent_id_from_stem(filename[: -len(".jsonl")])
        if agent_id is None:
            return None
        return Classified(
            kind=FileKind.SUBAGENT_JSONL, session_id=session_id, agent_id=agent_id, workflow_run_id=None
        )
    return None
