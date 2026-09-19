"""``trialerror.memory.render`` — markdown export/import. Design Section 3.2
(per-program scaffold): "``memory/`` # rendered memory export (git-synced,
cross-account)." Design Section 9.7: "export/import to ``memory/*.md``
for git sync." Design Section 4 (rendered-views rule): these files are
VIEWS, regenerated from ``memory_item`` truth — never hand-edited (same
rule ``trialerror.law.digest`` follows for ``LAW_DIGEST.md``).

**File shape — one file per item, literally matching the design's own
``memory/*.md`` glob:** ``sync-export`` writes ``memory/<slug(key)>.md``
per active item plus one reserved index file ``memory/MEMORY.md`` (a
human-readable table of contents — the same shape as the harness's own
real cross-account memory index, one link per topic). ``sync-import``
globs a directory for ``*.md``, skips the reserved index file, parses
every other file as one item, and hands the parsed list to
``trialerror.memory.merge.two_way_merge`` as the "foreign" side.

**Per-item file format** — a machine-parsed front-matter block (an HTML
comment, so it renders invisibly wherever the file is viewed as markdown)
followed by the body VERBATIM, byte-for-byte:

.. code-block:: text

    <!-- trialerror-memory-item
    memory_item_id: MEM-...
    key: some-topic
    tier: L0
    kind: rule
    account_id: ACC-...
    updated_ts: 2026-08-29T00:00:00.000Z
    status: active
    l0_abstract: one-line summary (newlines collapsed to spaces)
    -->

    <body, exactly as stored, no further normalization>

The front matter deliberately does NOT carry a ``content_sha256`` field
(same principle ``trialerror.law.digest`` states explicitly: a hash is metadata
ABOUT a render, embedding it inside the render itself would make it
self-referential) — ``trialerror.memory.content.content_sha256`` recomputes it
on demand, at merge time, from the parsed fields.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from trialerror.memory.merge import MergeResult, two_way_merge
from trialerror.stores.store import Store
from trialerror.util.atomic import atomic_write_text
from trialerror.util.timeutil import now

__all__ = [
    "INDEX_FILENAME",
    "FRONT_MATTER_FIELDS",
    "slug_for_key",
    "render_item_markdown",
    "parse_item_markdown",
    "export_memory",
    "import_memory",
]

#: Reserved filename ``sync-import`` never parses as an item — the
#: human-readable table of contents ``sync-export`` (re)writes alongside
#: the per-item files.
INDEX_FILENAME = "MEMORY.md"

FRONT_MATTER_FIELDS: tuple[str, ...] = (
    "memory_item_id",
    "key",
    "tier",
    "kind",
    "account_id",
    "updated_ts",
    "status",
    "l0_abstract",
)

_MARKER_OPEN = "<!-- trialerror-memory-item"
_MARKER_CLOSE = "-->\n\n"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug_for_key(key: str) -> str:
    """Filename-safe slug for ``key`` (lowercased, non-alnum runs collapsed
    to a single ``-``, leading/trailing ``-`` trimmed). Falls back to
    ``"item"`` for a key that slugs to nothing (e.g. all-punctuation)."""
    slug = _SLUG_RE.sub("-", key.lower()).strip("-")
    return slug or "item"


def render_item_markdown(row: Mapping[str, Any]) -> str:
    """One item -> its file content. Pure function of ``row`` (same
    input always produces the same byte-identical output — the property
    that makes re-exporting an unchanged store idempotent at the file
    level, not just the DB level)."""
    lines = [_MARKER_OPEN]
    for field in FRONT_MATTER_FIELDS:
        if field == "l0_abstract":
            value = (row.get("l0_abstract") or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        else:
            raw = row.get(field)
            value = "" if raw is None else str(raw)
        lines.append(f"{field}: {value}")
    lines.append("-->")
    header = "\n".join(lines) + "\n\n"
    body = row.get("body") or ""
    return header + body


def parse_item_markdown(text: str) -> dict[str, Any]:
    """Inverse of :func:`render_item_markdown`. Raises :class:`ValueError`
    for a file that doesn't carry a well-formed ``trialerror-memory-item``
    front-matter block (a hand-authored file that skipped the format, or
    a truncated one) — a caller-visible refusal, never a silent partial
    import."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if _MARKER_CLOSE not in normalized:
        raise ValueError(
            "parse_item_markdown: malformed trialerror-memory-item file "
            "(missing the '-->' front-matter terminator followed by a blank line)"
        )
    header_text, body = normalized.split(_MARKER_CLOSE, 1)
    if _MARKER_OPEN not in header_text:
        raise ValueError(f"parse_item_markdown: missing {_MARKER_OPEN!r} marker")

    fields: dict[str, str] = {}
    for line in header_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!--") or not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip()

    row: dict[str, Any] = {field: fields.get(field) for field in FRONT_MATTER_FIELDS}
    row["l0_abstract"] = row["l0_abstract"] or None
    row["body"] = body
    return row


def _full_active_rows(store: Store, *, account_id: str | None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM memory_item WHERE status = 'active'"
    params: list[Any] = []
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY key ASC"
    return [dict(r) for r in store.ops.execute(sql, params).fetchall()]


def _render_index(rows: list[dict[str, Any]], *, ts: str) -> str:
    lines = ["# Memory index", "", f"Rendered {ts} from {len(rows)} active item(s). "
             "This file is a RENDERED VIEW (trialerror.memory) — never hand-edit it; "
             "the canonical rows live in ops.db's `memory_item` table.", ""]
    if not rows:
        lines.append("_(no active memory items)_")
    for row in rows:
        filename = f"{slug_for_key(row['key'])}.md"
        abstract = row.get("l0_abstract") or "(no abstract)"
        lines.append(f"- [{row['key']}]({filename}) — {row['tier']}/{row['kind']}: {abstract}")
    return "\n".join(lines).rstrip() + "\n"


def export_memory(store: Store, *, out_dir: Path | str, account_id: str | None = None, ts: str | None = None) -> dict[str, Any]:
    """Render every ACTIVE memory item (optionally scoped to
    ``account_id``) as one file each under ``out_dir``, plus the
    :data:`INDEX_FILENAME` table of contents. Re-running against an
    unchanged store produces byte-identical files (same property
    ``trialerror.law``'s digest rendering has, and for the same reason: a pure
    function of the current row set)."""
    ts = ts or now()
    out_dir = Path(out_dir)
    rows = _full_active_rows(store, account_id=account_id)

    files: list[dict[str, Any]] = []
    for row in rows:
        filename = f"{slug_for_key(row['key'])}.md"
        path = out_dir / filename
        atomic_write_text(path, render_item_markdown(row))
        files.append({"key": row["key"], "path": str(path), "filename": filename})

    index_path = out_dir / INDEX_FILENAME
    atomic_write_text(index_path, _render_index(rows, ts=ts))

    return {"out_dir": str(out_dir), "index_path": str(index_path), "files": files, "count": len(rows)}


def import_memory(store: Store, *, in_dir: Path | str, ts: str | None = None) -> MergeResult:
    """Parse every ``*.md`` file under ``in_dir`` (skipping
    :data:`INDEX_FILENAME`) as one foreign item each, then run
    :func:`trialerror.memory.merge.two_way_merge` against ``store``'s current
    ``memory_item`` rows. Re-importing your own unchanged export is a
    no-op (content-hash dedup — see ``trialerror.memory.merge``'s module
    docstring): zero new rows, zero conflicts."""
    in_dir = Path(in_dir)
    foreign_items: list[dict[str, Any]] = []
    for path in sorted(in_dir.glob("*.md")):
        if path.name == INDEX_FILENAME:
            continue
        foreign_items.append(parse_item_markdown(path.read_text(encoding="utf-8")))
    return two_way_merge(store, foreign_items=foreign_items, ts=ts)
