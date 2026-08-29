"""FX-11 (docs/reviews/IMPL_REVIEW_VERDICT.md SD-3/OB-3 v1 ticket) —
``plugin/agents/`` subagent definitions carrying the design Section 5.1
per-agent MCP server allowlists.

OB-3's finding, verbatim: "``plugin/agents/`` (subagent definitions:
critic, verifier, lens templates — carrying the §5.1 per-agent MCP server
allowlists) does not exist ... The server-allowlist mechanism (Haiku-class
subagents scoped to one server) currently has no artifact implementing
it." This suite proves the three named files exist, are valid Claude Code
subagent definitions (frontmatter with ``name``/``description``/``tools``),
and that their ``tools:`` allowlists match design Section 5.1's stated
policy exactly:

- ``critic`` — tool-locked ``[Read]`` (design §5.3 verbatim), no MCP server
  tool at all.
- ``verifier`` / ``lens`` — "gets ``trialerror-knowledge`` alone: 11 tools"
  (design §5.1 verbatim): exactly the live ``trialerror-knowledge`` server's
  tool set, cross-checked against ``trialerror.mcp.knowledge.build_tools`` so a
  future change to that server's tool roster fails this test instead of
  silently drifting from the allowlist file; and NO ``trialerror-ops`` tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trialerror.mcp.knowledge import build_tools as build_knowledge_tools
from trialerror.mcp.ops import build_tools as build_ops_tools

AGENTS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "agents"


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Minimal parser for our own flat, single-line-valued YAML frontmatter
    (``name``/``description``/``tools``/``model``, one per line, no nested
    structures) — deliberately not a real YAML parser: ``pyyaml`` is not a
    declared dependency of this project (present in this dev venv only as
    an incidental transitive of the optional ``obs`` extra), and these
    three files are hand-authored by this same fix, so a tiny purpose-built
    parser is both sufficient and dependency-free."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: must open with a '---' frontmatter fence"
    end = text.index("\n---\n", 4)
    fm_text = text[4:end]
    body = text[end + 5 :]
    assert body.strip(), f"{path.name}: frontmatter present but body is empty"
    fields: dict[str, str] = {}
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _tool_list(fields: dict[str, str]) -> list[str]:
    assert "tools" in fields, "no tools: line in frontmatter"
    return [t.strip() for t in fields["tools"].split(",") if t.strip()]


@pytest.fixture(scope="module")
def live_knowledge_tool_names() -> set[str]:
    return set(build_knowledge_tools(program_root=Path("unused-nonexistent-root")).keys())


@pytest.fixture(scope="module")
def live_ops_tool_names() -> set[str]:
    return set(build_ops_tools(program_root=Path("unused-nonexistent-root")).keys())


# ---------------------------------------------------------------------------
# Existence + the exact three names OB-3 itself lists.
# ---------------------------------------------------------------------------


def test_agents_dir_exists_with_exactly_the_three_named_subagents():
    assert AGENTS_DIR.is_dir(), "plugin/agents/ does not exist — this is the OB-3/FX-11 gap itself"
    names = {p.stem for p in AGENTS_DIR.glob("*.md")}
    assert names == {"critic", "verifier", "lens"}


@pytest.mark.parametrize("stem", ["critic", "verifier", "lens"])
def test_each_agent_file_has_valid_frontmatter(stem):
    fields = _parse_frontmatter(AGENTS_DIR / f"{stem}.md")
    assert fields["name"] == stem
    assert fields["description"], "description must not be empty"
    assert "tools" in fields
    assert fields.get("model"), "Haiku-class per design §5.1 — model should be declared, not left to inherit"


# ---------------------------------------------------------------------------
# critic: tool-locked [Read], design §5.3 verbatim — no MCP tool at all.
# ---------------------------------------------------------------------------


def test_critic_is_tool_locked_to_read_only():
    fields = _parse_frontmatter(AGENTS_DIR / "critic.md")
    tools = _tool_list(fields)
    assert tools == ["Read"], f"design §5.3: critic must be tool-locked [Read] only; got {tools}"
    assert not any(t.startswith("mcp__") for t in tools)
    for forbidden in ("Edit", "Write", "Bash"):
        assert forbidden not in tools


# ---------------------------------------------------------------------------
# verifier / lens: trialerror-knowledge alone, 11 tools, design §5.1 verbatim.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem", ["verifier", "lens"])
def test_verifier_and_lens_get_trialerror_knowledge_alone(stem, live_knowledge_tool_names, live_ops_tool_names):
    fields = _parse_frontmatter(AGENTS_DIR / f"{stem}.md")
    tools = _tool_list(fields)

    assert len(tools) == 11, f"design §5.1: '{stem} gets trialerror-knowledge alone: 11 tools' — got {len(tools)}"
    assert all(t.startswith("mcp__trialerror-knowledge__") for t in tools), f"{stem}: every tool must be a trialerror-knowledge tool, no bare native tool and no trialerror-ops tool"

    declared_bare_names = {t.removeprefix("mcp__trialerror-knowledge__") for t in tools}
    # cross-check against the LIVE server's own tool roster (trialerror.mcp.knowledge.build_tools)
    # so a future change to that server's tools fails THIS test rather than
    # silently drifting from the allowlist file.
    assert declared_bare_names == live_knowledge_tool_names == {
        "search", "get_chunk", "get_source", "get_document_outline", "resolve_quote",
        "similar", "graph_neighbors", "corpus_stats", "memory_search", "list_requests", "poll_job",
    }

    # no trialerror-ops tool leaked in under any naming.
    live_ops_bare = {f"mcp__trialerror-ops__{n}" for n in live_ops_tool_names}
    assert not (set(tools) & live_ops_bare)
    assert not any("trialerror-ops" in t for t in tools)


def test_knowledge_and_ops_tool_counts_match_the_design_table():
    """Sanity anchor for the two fixtures above — design §5.1's own stated
    counts (11 / 12) are what make "trialerror-knowledge alone: 11 tools"
    meaningful in the first place."""
    assert len(build_knowledge_tools(program_root=Path("unused-nonexistent-root"))) == 11
    assert len(build_ops_tools(program_root=Path("unused-nonexistent-root"))) == 12


# ---------------------------------------------------------------------------
# Cross-file: no two agents accidentally share an over-broad allowlist, and
# nothing grants the full 23-tool (both-servers) surface design §5.1 warns
# pushes a Haiku-class subagent past its tool-selection-accuracy ceiling.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem", ["critic", "verifier", "lens"])
def test_no_agent_exceeds_the_haiku_tool_ceiling(stem):
    fields = _parse_frontmatter(AGENTS_DIR / f"{stem}.md")
    tools = _tool_list(fields)
    # design §5.1: "Haiku-class tool-selection accuracy drops 91%->87%
    # between 10 and 15 tools in context" — 15 is the stated ceiling these
    # Haiku-class agents must stay under.
    assert len(tools) <= 15, f"{stem}: {len(tools)} tools exceeds design §5.1's stated Haiku-class ceiling"
