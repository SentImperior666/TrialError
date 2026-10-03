"""Unit cost from Claude Code transcripts (F1). Design
``design/L3_unit-cost-probes-canaries.md`` Section 2.

A "unit" is one traceable piece of Claude Code work on a host: a main
session, a subagent, or a workflow agent. This package reads the host's own
transcript files -- never message text, ids/keys/counts/timestamps only
(trap 2) -- and sums each unit's token usage under the corrected rule for
streamed messages (:mod:`trialerror.units.reader`).
"""

from __future__ import annotations
