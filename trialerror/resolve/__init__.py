"""``trialerror.resolve`` — one id resolver. Design
``L10_plain-words-resolver-packet-outbox.md`` Section 2: "any id can be
looked up in words." Not exposed to seats (Section 6 trap 1): the knowledge
MCP server's slice-scoped tools are untouched, and this package is never
registered on any MCP server.
"""

from __future__ import annotations

from trialerror.resolve.base import Description, describe

__all__ = ["Description", "describe"]
