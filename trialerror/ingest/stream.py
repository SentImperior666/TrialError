"""``stream_v1``: the ONE canonical document-text serialization M7 writers
and M8/M9 readers share (design Section 4.1, verbatim -- the F6 blocking
finding's resolution).

    stream_v1(doc): concatenate element.text in seq order, joined by
    "\\n\\n"; Table elements contribute text (never text_as_html);
    PageBreak, Header, Footer, PageNumber, and Image elements contribute
    nothing; elements whose text is empty are likewise skipped -- they
    contribute no segment and no bare joiner, rather than an empty string
    between two "\\n\\n".

This function is named, versioned (:data:`STREAM_FN`), and stamped on
every anchor (``quote_anchor.stream_fn``) so a future ``stream_v2`` can
never silently shift offsets underneath an already-written anchor.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["STREAM_FN", "EXCLUDED_ELEMENT_TYPES", "JOINER", "stream_v1", "stream_v1_with_spans"]

#: design Section 4.1: "PageBreak, Header, Footer, PageNumber, and Image
#: elements contribute nothing" -- regardless of their ``text`` field.
EXCLUDED_ELEMENT_TYPES = frozenset({"PageBreak", "Header", "Footer", "PageNumber", "Image"})

JOINER = "\n\n"
STREAM_FN = "stream_v1"


def _segment_text(element: Mapping[str, Any]) -> str | None:
    """The text ``element`` contributes to the stream, or ``None`` if it
    contributes nothing (excluded type, or empty/missing ``text`` --
    design Section 4.1's "Table elements contribute text (never
    text_as_html)" is automatic here: this reads ``element["text"]``
    only, never ``text_as_html``, for every type including Table)."""
    if element.get("type") in EXCLUDED_ELEMENT_TYPES:
        return None
    text = element.get("text")
    if not text:
        return None
    return text


def _ordered(elements: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(elements, key=lambda e: e["seq"])


def stream_v1(elements: Sequence[Mapping[str, Any]]) -> str:
    """Build the canonical text stream for one document's elements.

    ``elements`` need not already be seq-ordered (sorted defensively here)
    but MUST all belong to the same document -- callers are responsible for
    that scoping (this function has no doc_id of its own to check against).
    """
    segments = [s for s in (_segment_text(e) for e in _ordered(elements)) if s is not None]
    return JOINER.join(segments)


def stream_v1_with_spans(
    elements: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, tuple[int, int]]]:
    """Like :func:`stream_v1`, but also returns each CONTRIBUTING element's
    ``[start, end)`` character span within the built stream, keyed by
    ``element["element_id"]``. Excluded/empty-text elements are absent from
    the returned span map (they contribute no segment to point at) -- a
    chunk's anchor span is computed by its caller as the union of its
    ``element_first``..``element_last`` contributing elements' spans (see
    ``trialerror.ingest.anchors``).
    """
    ordered = _ordered(elements)
    parts: list[str] = []
    spans: dict[str, tuple[int, int]] = {}
    cursor = 0
    first = True
    for element in ordered:
        segment = _segment_text(element)
        if segment is None:
            continue
        if not first:
            cursor += len(JOINER)
            parts.append(JOINER)
        start = cursor
        parts.append(segment)
        cursor += len(segment)
        spans[element["element_id"]] = (start, cursor)
        first = False
    return "".join(parts), spans
