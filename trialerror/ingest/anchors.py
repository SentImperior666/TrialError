"""Quote-anchor construction and spot-resolution against ``stream_v1``.
Design Section 4.1 ``quote_anchor`` + the F6 blocking finding: offsets are
computed into the document's CANONICAL element text stream
(:func:`trialerror.ingest.stream.stream_v1_with_spans`), and every anchor is
stamped with the ``stream_fn`` version and the ``document.sha256`` it was
computed against -- so a later re-normalization (which changes
``document.sha256``) makes the anchor detectably stale (``doctor``'s
``anchors_dangling``) rather than silently wrong.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from trialerror.ingest.stream import STREAM_FN, stream_v1_with_spans

__all__ = ["sha256_hex", "build_chunk_anchor", "spot_resolve"]


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_chunk_anchor(
    *,
    doc_id: str,
    doc_sha256: str,
    elements: Sequence[Mapping[str, Any]],
    chunk_id: str,
    element_first: str,
    element_last: str,
    page_number: int | None,
) -> dict[str, Any]:
    """Build one anchor draft (minus ``anchor_id``/``created_by_launch``/
    ``created_ts``, which the caller mints/stamps) spanning a chunk's
    ``[element_first, element_last]`` run within ``stream_v1(elements)``.

    The span is the union of every CONTRIBUTING element's own span between
    (and including) ``element_first``/``element_last`` in seq order -- an
    excluded/empty-text element inside that run contributes nothing to the
    span either, exactly mirroring how it contributed nothing to the stream
    itself (design Section 4.1's "contributes no segment and no bare
    joiner").
    """
    stream_text, spans = stream_v1_with_spans(elements)
    ordered = sorted(elements, key=lambda e: e["seq"])
    first_seq = next(e["seq"] for e in ordered if e["element_id"] == element_first)
    last_seq = next(e["seq"] for e in ordered if e["element_id"] == element_last)
    run_spans = [
        spans[e["element_id"]]
        for e in ordered
        if first_seq <= e["seq"] <= last_seq and e["element_id"] in spans
    ]
    if not run_spans:
        char_start = char_end = 0
        quote_text = ""
    else:
        char_start = min(s for s, _ in run_spans)
        char_end = max(e for _, e in run_spans)
        quote_text = stream_text[char_start:char_end]

    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "page_number": page_number,
        "char_start": char_start,
        "char_end": char_end,
        "stream_fn": STREAM_FN,
        "doc_sha256": doc_sha256,
        "quote_sha256": sha256_hex(quote_text),
        "quote_text": quote_text,
    }


def spot_resolve(elements: Sequence[Mapping[str, Any]], anchor: Mapping[str, Any]) -> bool:
    """Recompute ``stream_v1(elements)[char_start:char_end]`` and compare
    its hash against ``anchor["quote_sha256"]``. ``True`` means the anchor
    still resolves correctly against the CURRENT live element set for its
    document; ``False`` is the "other half" of ``anchors_dangling`` design
    Section 4.1 names (the doc_sha256-mismatch half lives in
    ``trialerror.stores.checks``, M1's own registered check)."""
    stream_text, _ = stream_v1_with_spans(elements)
    start, end = anchor["char_start"], anchor["char_end"]
    if start < 0 or end > len(stream_text) or start > end:
        return False
    live_quote = stream_text[start:end]
    return sha256_hex(live_quote) == anchor["quote_sha256"]
