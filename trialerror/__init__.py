"""TrialError: a reusable research-operations exoskeleton for Claude Code.

See docs/DESIGN_v0.md in the research-harness repo for the binding design.
This package (``trialerror``) is the platform core (Section 3.1 of the design):
stores, jobs, budget, law, sessions, events, ingest, retrieve, verify,
artifacts, memory, ideation, obs, mcp, cli, util.

M0 (platform skeleton) ships: trialerror.util, trialerror.cli, pyproject.toml,
vendored/VENDORED.md. Everything else is schema-only or absent until its
owning module (M1+, see docs/DESIGN_v0.md Section 12) lands.
"""

__version__ = "0.1.0"
