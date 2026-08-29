"""The two-pass boundary-aware chunker. Design Section 6 stage 5: "two-pass
boundary-aware | Unstructured algorithm port: semantic-boundary grouping ->
hard split at 1024 tok -> recombine undersized; table isolation +
header-row repeat; chunker_id/version stamped per chunk."

Pass 1 (:func:`_group_sections`) -- semantic-boundary grouping: a ``Title``
element opens a new section; a ``Table`` element is ISOLATED into its own
singleton section (never merged with surrounding prose, in either
direction); everything else accumulates into the current section.

Pass 2 (:func:`_pack_section` + :func:`_recombine_undersized`) -- hard
split at the 1024-token cap (an element whose own text alone exceeds the
cap is split at word boundaries -- the DB's ``chunk.token_count`` CHECK
constraint is otherwise violatable), then a second sub-pass merges a
too-small trailing group into its predecessor WITHIN THE SAME SECTION when
the merge still fits the cap (recombine undersized) -- section boundaries
are never crossed by a merge, so table isolation survives it for free.

A Table section whose flattened (row-joined) text alone exceeds the cap is
special-cased: split by row, repeating row 0 (the header row) into every
split piece (design: "header-row repeat").

TRIALERROR-DEV-NOTE: ``token_count`` uses a whitespace-split word count as a
cheap, dependency-free proxy for a real subword tokenizer (no tokenizer
library is part of this build's scope) -- a deliberate approximation,
consistently applied on both the packing and the DB-write side so the
1024 cap is never structurally violated even though the NUMBER doesn't
match a real BPE tokenizer's count exactly.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CHUNKER_ID",
    "CHUNKER_VERSION",
    "MAX_CHUNK_TOKENS",
    "MIN_STANDALONE_TOKENS",
    "estimate_tokens",
    "build_chunks",
]

CHUNKER_ID = "trialerror-two-pass"
CHUNKER_VERSION = "1"

MAX_CHUNK_TOKENS = 1024
#: A group below this many (estimated) tokens is a candidate to recombine
#: with its predecessor within the same section, provided the merge still
#: fits :data:`MAX_CHUNK_TOKENS`.
MIN_STANDALONE_TOKENS = 64


def estimate_tokens(text: str) -> int:
    """Whitespace-token count -- see module docstring's TRIALERROR-DEV-NOTE."""
    return len(text.split())


def _split_text_by_tokens(text: str, max_tokens: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    return [" ".join(words[i : i + max_tokens]) for i in range(0, len(words), max_tokens)]


def _group_sections(elements: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(elements, key=lambda e: e["seq"])
    sections: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for el in ordered:
        if el["type"] == "Table":
            if current:
                sections.append(current)
                current = []
            sections.append([el])
            continue
        if el["type"] == "Title" and current:
            sections.append(current)
            current = [el]
            continue
        current.append(el)
    if current:
        sections.append(current)
    return sections


def _pack_table_section(section: list[dict[str, Any]], max_tokens: int) -> list[list[tuple[dict, str]]]:
    """A Table section is always a singleton element. If its (row-joined)
    text fits the cap, it is one group; otherwise split by row with the
    header row (row 0) repeated into every resulting piece."""
    el = section[0]
    text = el.get("text") or ""
    if estimate_tokens(text) <= max_tokens:
        return [[(el, text)]] if text else []
    rows = text.split("\n")
    if not rows:
        return []
    header, body_rows = rows[0], rows[1:]
    groups: list[list[tuple[dict, str]]] = []
    current_rows = [header]
    current_tokens = estimate_tokens(header)
    for row in body_rows:
        row_tokens = estimate_tokens(row)
        if current_tokens + row_tokens > max_tokens and len(current_rows) > 1:
            groups.append([(el, "\n".join(current_rows))])
            current_rows = [header]
            current_tokens = estimate_tokens(header)
        current_rows.append(row)
        current_tokens += row_tokens
    if len(current_rows) > 1 or len(groups) == 0:
        groups.append([(el, "\n".join(current_rows))])
    return groups


def _pack_section(section: list[dict[str, Any]], max_tokens: int) -> list[list[tuple[dict, str]]]:
    if section[0]["type"] == "Table":
        return _pack_table_section(section, max_tokens)

    groups: list[list[tuple[dict, str]]] = []
    current: list[tuple[dict, str]] = []
    current_tokens = 0
    for el in section:
        text = el.get("text") or ""
        if not text:
            continue
        tokens = estimate_tokens(text)
        if tokens > max_tokens:
            if current:
                groups.append(current)
                current, current_tokens = [], 0
            for piece in _split_text_by_tokens(text, max_tokens):
                groups.append([(el, piece)])
            continue
        if current_tokens + tokens > max_tokens and current:
            groups.append(current)
            current, current_tokens = [], 0
        current.append((el, text))
        current_tokens += tokens
    if current:
        groups.append(current)
    return groups


def _group_tokens(group: list[tuple[dict, str]]) -> int:
    return sum(estimate_tokens(t) for _, t in group)


def _recombine_undersized(
    groups: list[list[tuple[dict, str]]], max_tokens: int, min_tokens: int
) -> list[list[tuple[dict, str]]]:
    """Merge a too-small group into its immediately preceding group (same
    section only -- callers apply this per-section) when the merge still
    fits ``max_tokens``. Never looks past its immediate predecessor and
    never merges a Table group (tables reach here as pre-packed
    already-capped pieces from :func:`_pack_table_section`, distinguished
    by every element in the group sharing type Table -- merging those
    would defeat "table isolation")."""
    if len(groups) <= 1:
        return groups
    result: list[list[tuple[dict, str]]] = []
    for group in groups:
        is_table = group[0][0]["type"] == "Table"
        tokens = _group_tokens(group)
        if result and not is_table and result[-1][0][0]["type"] != "Table" and tokens < min_tokens:
            merged_tokens = _group_tokens(result[-1]) + tokens
            if merged_tokens <= max_tokens:
                result[-1] = result[-1] + group
                continue
        result.append(group)
    return result


def build_chunks(
    elements: list[dict[str, Any]],
    *,
    max_tokens: int = MAX_CHUNK_TOKENS,
    min_standalone_tokens: int = MIN_STANDALONE_TOKENS,
    chunker_id: str = CHUNKER_ID,
    chunker_version: str = CHUNKER_VERSION,
) -> list[dict[str, Any]]:
    """Build chunk drafts from a document's element rows (dicts carrying at
    least ``element_id``/``seq``/``type``/``text``/``page_number``).

    Returns a list of plain dicts (one per chunk, in final ``seq`` order):
    ``seq``, ``text``, ``token_count``, ``element_first`` (element_id),
    ``element_last`` (element_id), ``page_start``, ``page_end``,
    ``chunker_id``, ``chunker_version`` -- everything :mod:`trialerror.ingest.pipeline`
    needs to insert a ``chunk`` row (plus ``chunk_id``/``doc_id``/``sha256``/
    ``created_ts``, which are the caller's job: sha256 needs the final text,
    which is right here, but hashing is centralized in
    ``trialerror.ingest.pipeline`` alongside every other content-hash in this
    package for one-place auditability).
    """
    sections = _group_sections(elements)
    all_groups: list[list[tuple[dict, str]]] = []
    for section in sections:
        groups = _pack_section(section, max_tokens)
        groups = _recombine_undersized(groups, max_tokens, min_standalone_tokens)
        all_groups.extend(groups)

    chunks: list[dict[str, Any]] = []
    for seq, group in enumerate(all_groups):
        els = [g[0] for g in group]
        texts = [g[1] for g in group]
        text = "\n\n".join(t for t in texts if t)
        token_count = min(estimate_tokens(text), max_tokens)
        pages = [e.get("page_number") for e in els if e.get("page_number") is not None]
        chunks.append(
            {
                "seq": seq,
                "text": text,
                "token_count": token_count,
                "element_first": els[0]["element_id"],
                "element_last": els[-1]["element_id"],
                "page_start": min(pages) if pages else None,
                "page_end": max(pages) if pages else None,
                "chunker_id": chunker_id,
                "chunker_version": chunker_version,
            }
        )
    return chunks
