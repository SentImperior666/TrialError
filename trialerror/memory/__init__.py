"""``trialerror.memory`` — ops-shaped agent memory (NOT chat recall). Design
Section 2 (subsystem table, row K): "L0/L1/L2 tiers, progressive search,
cross-account merge." Design Section 9.7: "ops-shaped, reimplemented
thin: ``memory_item(id, key, tier L0/L1/L2, kind rule/fact/lesson/
preference/index, body, l0_abstract, updated_ts, account_id, status)``;
boot loads L0 index + targeted L1 (Athena tiered-boot); progressive-
disclosure search; export/import to ``memory/*.md`` for git sync with
**MegaMemory merge port** (vendored MIT: conflict-group UUIDs,
``::left/::right`` divergence surfaced to the operator, never
auto-resolved)." Design Section 12 (M11 row): "tiered items, L0 index,
progressive search, md export/import, 2-way merge."

This module answers the project's own named pain (``CLAUDE.md``'s memory-
sync protocol; project memory's "cross-account collision risk flagged"):
two Claude Code accounts editing the SAME logical memory topic between
git syncs used to mean "repo wins, seed if empty" — a silent
last-writer-wins that drops one side. :mod:`trialerror.memory.merge`'s two-way
merge instead SURFACES the divergence as a conflict group holding BOTH
versions, resolved later by an explicit human/agent call — never
automatically.

Public surface
--------------
- :func:`put_item` / :func:`get_item` — full read/write over one
  ``memory_item`` row, upserting by ``(key, account_id)``.
- :func:`search_items` / :func:`boot_bundle` — progressive-disclosure
  reads (INDEX rows only — id/key/tier/kind/``l0_abstract``/account/
  updated_ts/status; never ``body``, see :data:`trialerror.memory.api.
  INDEX_FIELDS`); :func:`boot_bundle` is THE M6 integration point (see
  ``trialerror/memory/api.py`` module docstring for the boot/close notes at the
  bottom of this docstring).
- :func:`two_way_merge` / :func:`resolve_conflict` / :func:`list_conflicts`
  — the merge engine (``trialerror.memory.merge``): dedup by content hash,
  conflicts kept as BOTH-sides groups, resolved by an explicit
  ``keep=left|right|both`` call.
- :func:`export_memory` / :func:`import_memory` — the git-sync boundary
  (``trialerror.memory.render``): one file per item under ``memory/*.md`` plus
  a rendered ``MEMORY.md`` index; import re-parses and feeds
  :func:`two_way_merge`.

``trialerror/memory/checks.py`` registers this module's ``trialerror doctor`` checks
(``memory_unresolved_conflict_groups``, ``memory_l0_index_budget``) by the
same auto-discovery convention every other subsystem uses.

Notes for later builders (recorded here, not just in the build report, so
they survive a future re-read of just this module):

- **M6 (session lifecycle, boot/close capture):** call
  :func:`boot_bundle` at ``SessionStart`` with the open session's
  ``account_id`` and a ``token_budget`` from ``trialerror.toml``'s ``[memory]``
  table (``trialerror.memory.checks._configured_token_budget`` shows the exact
  read pattern — inline it, don't import a private helper). At session
  close, capture new lessons/facts/preferences the session learned via
  repeated :func:`put_item` calls (one per distinct ``key``) — NOT a bulk
  dump; ``put_item``'s upsert-by-key + content-hash no-op makes a close
  hook that runs every session, even one that learned nothing new, cheap
  and idempotent by construction.
- **M14 (ops MCP server) / M8 (knowledge MCP server, ``memory_search``
  tool, design Section 5.1 #9):** the read-only progressive-disclosure
  surface (:func:`search_items` then :func:`get_item` for chosen ids) is
  what design Section 5.1's ``trialerror-knowledge`` tool #9 (``memory_search``)
  should wrap. Side-effecting memory operations (put a lesson, resolve a
  conflict) are NOT in ``trialerror-ops``'s 12-tool list as specified (design
  Section 5.1) — if a later module wants agent-callable writes to memory,
  :func:`put_item` and :func:`resolve_conflict` are the functions to wrap;
  this module takes no position on which MCP server that lands in.
"""

from __future__ import annotations

from trialerror.memory.api import (
    DEFAULT_TOKEN_BUDGET,
    INDEX_FIELDS,
    KINDS,
    TIERS,
    boot_bundle,
    estimate_tokens,
    get_item,
    put_item,
    search_items,
)
from trialerror.memory.content import CONTENT_FIELDS, content_sha256, items_identical
from trialerror.memory.merge import (
    MERGE_SUFFIX_LEFT,
    MERGE_SUFFIX_RIGHT,
    MergeResult,
    list_conflicts,
    resolve_conflict,
    two_way_merge,
)
from trialerror.memory.render import (
    FRONT_MATTER_FIELDS,
    INDEX_FILENAME,
    export_memory,
    import_memory,
    parse_item_markdown,
    render_item_markdown,
    slug_for_key,
)

__all__ = [
    "TIERS",
    "KINDS",
    "INDEX_FIELDS",
    "DEFAULT_TOKEN_BUDGET",
    "put_item",
    "get_item",
    "search_items",
    "boot_bundle",
    "estimate_tokens",
    "CONTENT_FIELDS",
    "content_sha256",
    "items_identical",
    "MERGE_SUFFIX_LEFT",
    "MERGE_SUFFIX_RIGHT",
    "MergeResult",
    "two_way_merge",
    "resolve_conflict",
    "list_conflicts",
    "INDEX_FILENAME",
    "FRONT_MATTER_FIELDS",
    "slug_for_key",
    "render_item_markdown",
    "parse_item_markdown",
    "export_memory",
    "import_memory",
]
