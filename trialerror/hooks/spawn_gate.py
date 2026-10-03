"""PreToolUse hook: the physical spawn gate. Design Section 5.4 (PreToolUse:
Task row) + Section 1 commitment 1 ("Enforcement over convention ...
Budget-at-spawn is a PreToolUse hook that refuses an unbooked `Task`
call... Nothing load-bearing is a prompt.").

Claude Code invokes this hook (via ``trialerror hook spawn-gate``, wired in
``plugin/hooks/hooks.json``; design Section 12's M6 row used to say "hook
command lines invoke `python` explicitly (Windows)", which is exactly the
wiring that failed with exit 127 on Linux -- see :mod:`trialerror.hooks`)
for every tool call the plugin's hook configuration matches
against; it receives one JSON object on stdin (the PreToolUse payload:
``session_id``, ``cwd``, ``hook_event_name``, ``tool_name``, ``tool_input``,
...) and communicates its verdict purely through the process exit code,
per Claude Code's hook protocol:

- exit 0 -> the tool call proceeds.
- exit 2 -> the tool call is BLOCKED; stderr is surfaced back to the agent
  as the refusal reason (design: "exit 2 (spawn REFUSED) with the exact
  `trialerror budget book` command to run").

All decision logic lives in :mod:`trialerror.budget.gate` (pytest imports and
calls it directly, with no stdin/subprocess involved - design Section 12 M3
row: "live-CC hook tests are orchestrator-executed integration items", i.e.
only the ACTUAL live-Claude-Code round trip needs a real session; the gate
logic itself is unit-tested here). This file is deliberately thin: parse
stdin, resolve the program/platform roots, call the gate, translate the
verdict to an exit code.

TRIALERROR-DEV-NOTE (matcher wiring): this hook assumes it is only invoked for
``tool_name in SUBAGENT_TOOL_NAMES`` (see :mod:`trialerror.hooks`).
``plugin/hooks/hooks.json`` now enforces that with a ``^(Task|Agent)$``
``PreToolUse`` matcher, so the assumption holds under the shipped
manifest. Belt and braces, THIS module still defends itself: any
``tool_name`` outside that set (or a payload it can't parse at all)
passes through (exit 0) rather than guessing - see ``_evaluate`` below.
Confirming that Claude Code actually applies the matcher, rather than
merely that the fast path works, remains a live-session item (see
``tests/acceptance/test_gpu_and_live_cc_journeys.py``).

TRIALERROR-DEV-NOTE (Task->Agent rename, found 2026-09-05): this docstring
used to say the matcher, and this module's own comparison, were
``"Task"``-only. Live evidence on the sandbox host (03:34Z, the sandbox container
container) showed Claude Code 2.1.261 invoking the subagent tool as
``Agent`` rather than ``Task`` -- the old ``tool_name != "Task"`` check
silently let every real spawn through unbooked (no refusal, no
``hook_alive{hook=spawn_gate}`` row). Both the manifest matcher above and
the comparison below now gate on ``SUBAGENT_TOOL_NAMES = ("Task", "Agent")``
(:mod:`trialerror.hooks`) so a future rename of either name is a one-line
fix in one place instead of a repeat of this incident.

TRIALERROR-DEV-NOTE (cwd assumption): ``trialerror.util.config.find_program_root``
walks up from the hook payload's ``cwd`` looking for ``trialerror.toml``. This
assumes Claude Code's hook cwd is inside (or at) the program scaffold - 
true once M6's session boot ritual is the thing that launched the session,
not guaranteed for an ad-hoc hook invocation. Flagged for the M6 builder.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from trialerror.hooks import SUBAGENT_TOOL_NAMES

#: Where a subagent definition may live, relative to a root this hook can
#: see. Searched in order for the ``subagent_type`` a Task call names, so
#: ``agent_model_matches_booking`` can read the file's declared ``model``
#: when the call itself named none.
_AGENT_DIRS: tuple[tuple[str, ...], ...] = (
    ("plugin", "agents"),
    (".claude", "agents"),
)

#: This plugin's own manifest, read for its declared ``name`` -- the exact
#: namespace Claude Code prefixes onto every agent it ships under
#: ``--plugin-dir`` (``<name>:<agent>``). Read from the manifest rather
#: than hardcoded so a future rename of the plugin cannot silently reopen
#: new-N1 (stripping a namespace this plugin no longer claims, or failing
#: to strip the one it does).
_PLUGIN_MANIFEST = Path(__file__).resolve().parents[2] / "plugin" / ".claude-plugin" / "plugin.json"

_FRONTMATTER_MODEL_RE = re.compile(r"^model\s*:\s*(.+?)\s*$", re.MULTILINE)


#: Claude Code exports this to every hook process: the root of the plugin
#: whose hook is running. Under ``--plugin-dir`` that is the directory the
#: plugin was loaded from, which may not be this checkout.
_PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"

#: manifest path -> its declared name (or ``None``), read once per process.
_PLUGIN_NAME_CACHE: dict[str, str | None] = {}


def _plugin_root_from_env() -> Path | None:
    raw = os.environ.get(_PLUGIN_ROOT_ENV)
    return Path(raw) if raw else None


def _read_plugin_name(manifest: Path) -> str | None:
    key = str(manifest)
    if key not in _PLUGIN_NAME_CACHE:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        name = data.get("name") if isinstance(data, dict) else None
        _PLUGIN_NAME_CACHE[key] = name if isinstance(name, str) and name else None
    return _PLUGIN_NAME_CACHE[key]


def _this_plugins_namespace_prefix() -> str | None:
    """This plugin's own ``"<name>:"`` prefix, or ``None`` if the manifest
    cannot be read or names nothing usable. The name comes from
    ``$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json`` when that variable is
    set, else from this repository's ``plugin/.claude-plugin/plugin.json``,
    and is cached per process. Best-effort, like :func:`_frontmatter_model`
    itself: a manifest this process cannot find or parse yields no claim,
    never a refusal."""
    root = _plugin_root_from_env()
    name = _read_plugin_name(root / ".claude-plugin" / "plugin.json") if root is not None else None
    if name is None:
        name = _read_plugin_name(_PLUGIN_MANIFEST)
    return f"{name}:" if name else None


def _frontmatter_block(text: str) -> str | None:
    """The body of a leading ``---`` … ``---`` YAML frontmatter block, or
    ``None`` when the file opens with anything else.

    Slicing this out before searching is what keeps a PROSE line that
    happens to begin ``model:`` from being read as the file's pin
    (verification finding V-8). The direction that matters is the false
    refusal: an agent file whose body discusses ``model: haiku`` would
    otherwise refuse a perfectly correct top-class spawn."""
    if not text.startswith("---"):
        return None
    rest = text[3:]
    if rest[:1] not in ("\n", "\r"):
        return None  # "---text" is not a fence
    end = re.search(r"^---\s*$", rest, re.MULTILINE)
    return rest[: end.start()] if end else None


def _frontmatter_model(subagent_type: str | None, program_root: Path) -> str | None:
    """The ``model:`` a subagent definition file declares, or ``None``.

    A Task call that names no ``model`` is not a call with no model: Claude
    Code uses whatever the agent file pins. Reading that file is what stops
    "just leave model out of the call" from being a way around the guard.

    Read from the frontmatter block ALONE: a file with no frontmatter, or
    one whose pin lives only in its prose, makes no claim.

    Best-effort by design — an agent file this process cannot find or read
    yields ``None`` (no claim), never a refusal, because "no such file from
    here" is a statement about the hook's view of the filesystem, not about
    the spawn.

    N1 (review finding, 2026-09-27): under ``--plugin-dir`` every plugin
    agent's name is namespaced ``<plugin>:<agent>`` (e.g.
    ``trialerror:prompt-only``) and the bare name is refused outright, so
    a real spawn always names the qualified form. Before this fix, the
    qualified form was joined onto ``_AGENT_DIRS`` verbatim (looking for a
    file literally named ``trialerror:prompt-only.md``, which never
    exists), so this always returned ``None`` for the ONE form real spawns
    actually use -- silently turning the model-pin guard off for every
    plugin-qualified spawn, not just this agent's.

    New-N1 (second review pass): the first fix stripped up to the LAST
    ``:`` unconditionally, so ``otherplugin:critic`` (another loaded
    plugin's own agent, coincidentally sharing a bare name with one of
    ours) resolved against THIS plugin's ``critic.md`` and made a claim
    about a file that has nothing to do with the actual spawn. Only this
    plugin's own namespace (:func:`_this_plugins_namespace_prefix`,
    read from its manifest -- ``trialerror:`` today) is ever stripped;
    any other prefix (a different plugin's name, a bare drive letter, a
    multi-colon name) makes no claim at all, the same as an unparseable
    name would.

    Where the plugin's own name and agent files are read from: the plugin
    Claude Code is running hooks for (``$CLAUDE_PLUGIN_ROOT``) when it is set
    -- its manifest names the plugin, and its ``agents/`` folder is searched
    first -- and otherwise this repository's own ``plugin/`` folder. The name
    is cached per process."""
    if not subagent_type:
        return None
    name = str(subagent_type).strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    search_roots: list[tuple[Path, tuple[tuple[str, ...], ...]]] = []
    if ":" in name:
        prefix = _this_plugins_namespace_prefix()
        if prefix is None or not name.startswith(prefix):
            return None
        name = name[len(prefix):].strip()
        if not name or "/" in name or "\\" in name or name.startswith(".") or ":" in name:
            return None
        # This plugin's own qualified name: its own agents folder comes first.
        env_root = _plugin_root_from_env()
        if env_root is not None:
            search_roots.append((env_root, (("agents",),)))
    search_roots.extend((root, _AGENT_DIRS) for root in (program_root, Path(__file__).resolve().parents[2]))
    for root, dirs in search_roots:
        for parts in dirs:
            path = root.joinpath(*parts, f"{name}.md")
            try:
                if not path.is_file():
                    continue
                head = path.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                continue
            block = _frontmatter_block(head)
            if block is None:
                continue
            match = _FRONTMATTER_MODEL_RE.search(block)
            if match:
                return match.group(1).strip().strip("\"'")
    return None


def _spawn_transcript_dir(payload: dict) -> str | None:
    """``<dirname(transcript_path)>/<session_id>``: where a started
    subagent's ``subagents/agent-<id>.meta.json`` lives. Only when both keys
    are non-empty strings and the result is absolute; otherwise ``None``."""
    transcript_path = payload.get("transcript_path")
    session_id = payload.get("session_id")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        candidate = os.path.join(os.path.dirname(transcript_path), session_id)
    except (TypeError, ValueError):
        return None
    return candidate if os.path.isabs(candidate) else None


def _evaluate(payload: dict) -> tuple[int, str | None]:
    """Returns ``(exit_code, stderr_message)``. Kept separate from
    ``main()`` so a test can call it directly with a crafted payload dict
    instead of piping JSON through a real stdin/subprocess."""
    tool_name = payload.get("tool_name")
    if tool_name not in SUBAGENT_TOOL_NAMES:
        return 0, None

    tool_input = payload.get("tool_input") or {}
    prompt_text = tool_input.get("prompt") or tool_input.get("description") or ""
    if not prompt_text and tool_input:
        # TRIALERROR-DEV-NOTE (tool_input schema assumption, FU-11 verification
        # finding FU11-V5): the Task->Agent rename above proved the tool NAME
        # is not stable across Claude Code versions; the shape of tool_input
        # (that a subagent call carries "prompt" or "description") is an
        # equally unverified assumption. If a future rename moves the prompt
        # text under some other key, fall back to scanning the WHOLE
        # serialized tool_input for the `launch_id:` token rather than
        # treating the call as promptless -- cheap hardening, not a full fix
        # (a live Claude Code session must still confirm the real key name).
        prompt_text = json.dumps(tool_input, ensure_ascii=False)
    cwd = payload.get("cwd") or "."
    # The spawn's identity, for the launch it consumes. Both are read with
    # ``.get()`` and a missing or oddly typed value is simply ``None``: a
    # payload key the gate does not find must never be a reason to refuse.
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        tool_use_id = None

    # Deferred imports: keep import cost off the (much more common)
    # not-a-subagent-call fast path above.
    from trialerror.budget.gate import evaluate_spawn_for_open_session, resolve_open_session
    from trialerror.events.api import record_hook_alive_once
    from trialerror.stores.store import open_store
    from trialerror.util.config import ConfigError, ProgramRootIsHarnessError, find_program_root, load_config

    # N9 (fix check): L8 part F's find_program_root() now raises
    # ProgramRootIsHarnessError instead of silently returning the harness's
    # own checkout. Every other hook (SessionStart, Stop, PostToolUse)
    # already swallows a find_program_root() failure and carries on with
    # whatever program-scoped feature degrades; this gate is the one that
    # fails CLOSED on an unhandled exception (main()'s own catch-all,
    # "an unexpected bug ... must not fail OPEN") -- so left unhandled, a
    # session whose cwd is the harness checkout would refuse EVERY subagent
    # spawn. Treated the same as "no program root found" here: the refusal
    # means "no programme here", not "something is wrong", so the spawn
    # passes through with a stderr note instead of failing closed.
    try:
        program_root = find_program_root(cwd) or Path(cwd)
    except ProgramRootIsHarnessError as exc:
        return 0, f"spawn gate: {exc} -- no program root; passing the spawn through"

    policy: dict[str, str] | None = None
    model_classes: dict[str, str] | None = None
    try:
        config = load_config(program_root / "trialerror.toml")
        policy = dict(config.models) if config.models else None
        model_classes = {str(k): str(v) for k, v in config.model_classes.items()} or None
    except ConfigError:
        policy = None
        model_classes = None

    # agent_model_matches_booking's input: the model the Task call names,
    # or -- when it names none -- the one the subagent's own definition file
    # pins. Either can be absent, and absent means "no claim", not "fine".
    agent_model = tool_input.get("model") or _frontmatter_model(
        tool_input.get("subagent_type"), program_root
    )

    try:
        store = open_store(program_root)
    except Exception as exc:  # noqa: BLE001 - this IS a subagent spawn call; cannot verify -> fail CLOSED
        return 2, f"spawn gate: could not open program stores at {program_root}: {exc}"

    try:
        # FX-8 (C-0064): a hook_alive row with payload.hook == "spawn_gate"
        # -- distinct from session_start.py's own "session_start" value --
        # is the physical liveness marker close_session's hooks_partial
        # check needs (see trialerror.sessions.lifecycle's own TRIALERROR-DEV-NOTE).
        # Recorded regardless of the gate's verdict below, same "the hook
        # fired" posture session_start.py's module docstring states.
        session = resolve_open_session(store)
        record_hook_alive_once(
            store, session_id=session["session_id"] if session is not None else None, hook_name="spawn_gate"
        )
        result = evaluate_spawn_for_open_session(
            store,
            prompt_text,
            policy=policy,
            agent_model=agent_model,
            model_classes=model_classes,
            tool_use_id=tool_use_id,
            transcript_dir=_spawn_transcript_dir(payload),
        )
    finally:
        store.close()

    if result.allowed:
        return 0, None

    msg = f"SPAWN REFUSED [{result.code}]: {result.message}"
    if result.next_command:
        msg += "\nfix: " + " ".join(result.next_command)
    return 2, msg


def _record_hook_keys(payload: dict) -> None:
    """L3 (design Section 3.3, ``hook_payload_keys`` row: "tool_use_id
    (Agent pre and post)"). Best-effort and swallowed at the call site --
    this gate fails CLOSED on an unexpected error (module docstring), and a
    probe-log write must never be the reason a spawn gets refused."""
    from trialerror.hooks.probe_log import append_hook_record

    append_hook_record(payload, hook="spawn_gate")


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        # Can't even parse the hook payload -> can't tell if this is a
        # subagent spawn call -> pass through rather than blocking every tool.
        return 0

    try:
        _record_hook_keys(payload)
    except Exception:  # noqa: BLE001 - never let this affect the gate's verdict
        pass

    try:
        code, message = _evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - an unexpected bug in the gate must not fail OPEN
        print(f"spawn gate: internal error evaluating spawn: {exc}", file=sys.stderr)
        return 2

    if message:
        print(message, file=sys.stderr)
    return code

