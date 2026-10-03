"""REQ-2026-09-27-08 -- ``plugin/agents/prompt-only.md``, a generic
subagent definition whose whole input is its rendered spawn prompt.

Some launches need their whole input to be their prompt: no file, shell,
search, fetch or MCP tool of any kind. None of the plugin's other agents
fit -- ``lens`` refuses such prompts outright, ``critic`` carries ``Read``,
``general-purpose`` carries every tool -- so this suite pins the new
agent's frontmatter exactly:

- its ``tools:`` line names the smallest set Claude Code will spawn (a
  single tool, and no reading tool of any kind, native or MCP);
- its ``disallowedTools:`` line blocks every MCP server tool;
- its ``omitClaudeMd:`` line keeps every CLAUDE.md off the launch, since
  Claude Code otherwise injects them regardless of ``tools:``;
- its body carries no developer notes and nothing programme-specific.

(Review finding S1: this file used to also reuse ``scripts/export_public.py``'s
identity/mission-word gate patterns directly, but ``scripts/`` is never
exported, so that check could only ever fail once this file itself
shipped publicly. ``tests/test_export_public.py`` -- which is excluded
from the export for the same reason -- covers this agent file against
every one of those gate's real checks instead.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "agents"
REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_ONLY_PATH = AGENTS_DIR / "prompt-only.md"
OPERATOR_GUIDE_PATH = REPO_ROOT / "docs" / "OPERATOR_GUIDE.md"

#: The native, file/shell/search/fetch-reading tools the charter names by
#: name, widened (review finding N4) to every other native tool that reads
#: a file, runs a command, searches, fetches, or reaches another
#: agent/session/store -- none of these may appear in prompt-only's
#: ``tools:`` line, even though the exact ``== ["TaskStop"]`` pin below
#: already covers it; this set is the guard for if that pin ever loosens.
NATIVE_READING_TOOLS = {
    "Read", "Grep", "Glob", "Bash", "PowerShell", "WebFetch", "WebSearch",
    "NotebookRead", "Edit", "Write", "Agent",
    "SendMessage", "Skill", "ToolSearch", "Monitor",
    "Artifact", "ArtifactComments", "ArtifactData",
    "EnterWorktree", "ExitWorktree",
}


def _text() -> str:
    return PROMPT_ONLY_PATH.read_text(encoding="utf-8")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal parser for this repo's flat, single-line-valued frontmatter
    (see ``tests/test_plugin_agents_allowlists.py``'s own copy of this
    docstring for why this is a purpose-built parser rather than a real
    YAML one: ``pyyaml`` is not a declared dependency)."""
    assert text.startswith("---\n"), "prompt-only.md must open with a '---' frontmatter fence"
    end = text.index("\n---\n", 4)
    fm_text = text[4:end]
    body = text[end + 5:]
    assert body.strip(), "prompt-only.md: frontmatter present but body is empty"
    fields: dict[str, str] = {}
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _list_field(fields: dict[str, str], key: str) -> list[str]:
    assert key in fields, f"no {key}: line in frontmatter"
    return [t.strip() for t in fields[key].split(",") if t.strip()]


@pytest.fixture(scope="module")
def fields() -> dict[str, str]:
    return _parse_frontmatter(_text())


@pytest.fixture(scope="module")
def body() -> str:
    text = _text()
    return text[text.index("\n---\n", 4) + 5:]


# ---------------------------------------------------------------------------
# existence + valid frontmatter
# ---------------------------------------------------------------------------


def test_prompt_only_file_exists():
    assert PROMPT_ONLY_PATH.is_file()


def test_frontmatter_has_the_required_fields(fields):
    assert fields["name"] == "prompt-only"
    assert fields["description"], "description must not be empty"
    assert "tools" in fields
    assert "disallowedTools" in fields
    assert fields.get("model"), "model must be declared, never left to inherit"
    assert "omitClaudeMd" in fields, "no omitClaudeMd: line in frontmatter"


# ---------------------------------------------------------------------------
# tools: -- the smallest non-empty set, and no reading tool in it
# ---------------------------------------------------------------------------


def test_tools_is_pinned_to_exactly_one_tool(fields):
    """Claude Code refuses to spawn an agent whose ``tools:`` line is empty
    ("would be spawned with zero tools -- refusing"), so the smallest legal
    set is exactly one tool -- pinned here so a future edit can't silently
    grow it back toward the default (every tool) or shrink it to the
    refused empty list.

    Two earlier choices were tried live (2026-09-27, installed Claude Code
    2.1.280) and refused before this one:

    - ``TodoWrite`` (the charter's own example of "writes to the session's
      own scratch state") -- refused: "unrecognized [TodoWrite]". Not
      offered to subagents (or, per the installed build's own changelog,
      to any Opus/Sonnet-5/Fable-5-family model) in this build at all.
    - ``ReportFindings`` -- refused: "not available to subagents
      [ReportFindings]". Recognized as a real tool, but reserved for the
      main session.

    A diagnostic spawn of the built-in ``general-purpose`` agent (which
    inherits every tool) enumerated the actual subagent-eligible roster in
    this build: ``Agent, Artifact, ArtifactComments, ArtifactData, Bash,
    Edit, EnterWorktree, ExitWorktree, Glob, Grep, Monitor, NotebookEdit,
    PowerShell, Read, SendMessage, Skill, TaskStop, ToolSearch, WebFetch,
    WebSearch, Write`` -- every one of those reads files, runs commands,
    searches, fetches, spawns further agents, or communicates with another
    session/agent, EXCEPT ``TaskStop``: it takes only a ``task_id`` and
    stops an already-named background task, reading and communicating
    nothing. Confirmed live to spawn cleanly."""
    tools = _list_field(fields, "tools")
    assert tools == ["TaskStop"], (
        f"prompt-only must be tool-locked to exactly ['TaskStop'] -- the one subagent-"
        f"eligible native tool (confirmed live) that reads nothing at all; got {tools}"
    )


def test_no_reading_tool_appears_in_the_tools_line(fields):
    tools = set(_list_field(fields, "tools"))
    overlap = tools & NATIVE_READING_TOOLS
    assert not overlap, f"prompt-only's tools: line carries a reading tool: {overlap}"


def test_no_mcp_tool_appears_in_the_tools_line(fields):
    tools = _list_field(fields, "tools")
    assert not any(t.startswith("mcp__") for t in tools), (
        "prompt-only's tools: line must grant no MCP server tool at all"
    )


# ---------------------------------------------------------------------------
# disallowedTools: -- every MCP tool blocked, whichever servers a session has
# ---------------------------------------------------------------------------


def test_disallowed_tools_blocks_every_mcp_server_with_one_wildcard(fields):
    """A session's configured MCP servers vary by environment (this plugin
    ships none of its own in ``.mcp.json``; an operator may register
    ``trialerror-ops``/``trialerror-knowledge``, or nothing, or something
    else). Naming servers one at a time in ``disallowedTools`` would leave
    any server this file's author didn't know about un-blocked, so the
    line must be the bare ``mcp__*`` wildcard -- confirmed (2026-09-27,
    installed Claude Code 2.1.280) to actually block MCP tools in a
    subagent's ``disallowedTools``, a bug fixed at 2.1.178 after landing
    broken (silently ignored) at the field's introduction."""
    disallowed = _list_field(fields, "disallowedTools")
    assert disallowed == ["mcp__*"], (
        f"prompt-only's disallowedTools: line must be exactly ['mcp__*'] -- got {disallowed}"
    )


# ---------------------------------------------------------------------------
# model: -- follows the plugin's other agents
# ---------------------------------------------------------------------------


def test_model_follows_the_plugins_other_agents(fields):
    assert fields["model"] == "opus", (
        "prompt-only's model: line must match the plugin's other agents "
        f"(critic/verifier/lens all pin 'opus'); got {fields['model']!r}"
    )


# ---------------------------------------------------------------------------
# omitClaudeMd: -- review finding B1
# ---------------------------------------------------------------------------


def test_omit_claude_md_is_pinned_true(fields):
    """Verified live (2026-09-27, installed Claude Code 2.1.280): without
    this line, Claude Code injects every CLAUDE.md on the launch path (the
    user's own, and any project/local one on the spawning cwd's path) into
    the subagent's context regardless of its ``tools:`` line -- a scratch
    ``CLAUDE.md`` canary was quoted back verbatim by a foreground
    prompt-only launch that carried no ``omitClaudeMd:`` line. Adding
    ``omitClaudeMd: true`` removed it; a managed-policy CLAUDE.md is kept
    by design regardless of this setting."""
    assert fields["omitClaudeMd"] == "true", (
        f"prompt-only must set omitClaudeMd: true -- got {fields['omitClaudeMd']!r}"
    )


# ---------------------------------------------------------------------------
# the body: no developer notes, nothing programme-specific
# ---------------------------------------------------------------------------


def test_body_carries_no_developer_notes(body):
    for marker in ("TRIALERROR-DEV-NOTE", "DEV-NOTE", "TODO", "FIXME"):
        assert marker not in body, f"prompt-only.md carries instructions only, found {marker!r}"


#: Vocabulary that belongs to this harness's own AIIF/ideation feature (or
#: any other specific research programme run on it) -- a generic plugin
#: agent must name none of it. Checked case-insensitively against the body
#: only (the frontmatter's description is allowed to explain, in the
#: charter's own words, what kinds of launches this agent suits, so long as
#: it names no mechanism -- checked separately below).
PROGRAMME_SPECIFIC_TERMS = (
    "aiif", "ideation", "lens", "corpus", "novelty", "recipe card",
    "derivation", "brainwriting", "pre-registration", "prereg",
    "assumption_buster", "trialerror",
)


@pytest.mark.parametrize("term", PROGRAMME_SPECIFIC_TERMS)
def test_body_names_nothing_programme_specific(body, term):
    assert term not in body.lower(), f"prompt-only.md's body names {term!r}, which is programme-specific"


def test_description_names_no_mechanism_either(fields):
    """The description may say *when* to reach for this agent (the
    charter's own example: 'reads only its prompt and answers in the form
    the prompt asks'), but not name any one programme's mechanism."""
    description = fields["description"].lower()
    for term in ("aiif", "ideation", "lens", "corpus", "novelty", "derivation"):
        assert term not in description, f"prompt-only's description names {term!r}"


# ---------------------------------------------------------------------------
# the guide: review finding S1 -- it must not drift from the frontmatter
# ---------------------------------------------------------------------------


def test_the_guide_names_the_tool_the_frontmatter_pins(fields):
    """Review finding S1: an earlier version of ``docs/OPERATOR_GUIDE.md``
    named ``ReportFindings`` -- the SECOND tool this agent tried and was
    refused live -- as the shipped tool, silently drifted from the
    ``a1b770e`` fix that corrected the agent file and this test module but
    never touched the guide. Reading the frontmatter's own ``tools:`` line
    and requiring the guide to name it exactly is what keeps that
    particular drift from recurring unnoticed."""
    tool = _list_field(fields, "tools")[0]
    guide = OPERATOR_GUIDE_PATH.read_text(encoding="utf-8")
    assert f"`{tool}`" in guide, (
        f"docs/OPERATOR_GUIDE.md does not name prompt-only's actual tool ({tool!r})"
    )
