"""The F3 serving-path license fence. Design Section 7: "for chunks whose
source is ``commercial_restricted``, ``search``/``get_chunk``/``similar``
NEVER serve raw chunk text to agent surfaces. The ``text`` field carries
the chunk's structured extraction/summary + section identifiers + a <=20-
word verbatim excerpt; ``fenced: true`` is set; the citation anchor still
resolves precisely." Also ``DESIGN_REVIEW_v0.md`` F3 [BLOCKING]: "license-
tier fencing in the retrieval engine itself ... never raw chunk text; full
text remains available on disk."

This module owns exactly two guarantees, both load-bearing for the M8
acceptance criterion ("fenced-corpus fixture: commercial_restricted
search/get_chunk return no verbatim run >20 words, ``fenced:true``, anchor
still resolves"):

1. :func:`excerpt_words` NEVER returns more than ``max_words`` words of its
   input, so anything built from its output cannot contain a verbatim run
   longer than the cap -- by construction, not by post-hoc scanning.
2. :func:`fence_chunk_text` never interpolates the RAW chunk text itself
   into the returned string -- only the capped excerpt plus synthesized
   (non-corpus) structural metadata (title/pages/chunk index/token count).

Applied ENGINE-LEVEL (:mod:`trialerror.retrieve.engine`), not per-surface, so
every caller -- the ``trialerror-knowledge`` MCP tools, the ``query`` CLI group,
and M9's verification pipelines -- inherits it structurally (design Section
7: "the fence lives in the retrieval engine itself, so every caller ...
inherits it").

TRIALERROR-DEV-NOTE (build-M8 judgment call): design Section 7's own prose names
exactly three fenced tools (``search``/``get_chunk``/``similar``). This
module is applied to those three AND to ``resolve_quote`` -- a caller
supplying a partial quote could otherwise pull a full verbatim
``quote_anchor.quote_text`` for a ``commercial_restricted`` source straight
out from under the fence, which would be the same law violation Section 7
exists to prevent. Broadening to a fourth tool is a strict widening of the
fence (never a narrowing), so it cannot break the stated acceptance
criterion; flagged here for the integration session's awareness.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "FENCED_LICENSE_TIERS",
    "is_fenced_license",
    "excerpt_words",
    "fence_chunk_text",
    "citation_quote",
]

#: License tiers whose chunk text is served fenced on every agent-facing
#: surface (design Section 4.1's ``source.license_tier`` domain; only this
#: one tier triggers the fence per Section 7's own prose).
FENCED_LICENSE_TIERS: frozenset[str] = frozenset({"commercial_restricted"})

#: D-COC-1: the fence's own verbatim-excerpt cap.
MAX_FENCED_EXCERPT_WORDS = 20

#: Section 7's stated cap for a NON-fenced citation's grounding excerpt.
MAX_OPEN_CITATION_QUOTE_CHARS = 300


def is_fenced_license(license_tier: str | None) -> bool:
    """Whether ``license_tier`` requires the serving-path fence."""
    return license_tier in FENCED_LICENSE_TIERS


def excerpt_words(text: str | None, max_words: int = MAX_FENCED_EXCERPT_WORDS) -> str:
    """The first ``max_words`` whitespace-delimited words of ``text``,
    stripped. Never returns more than ``max_words`` words -- the one
    function every fenced-text code path in this package routes its
    verbatim-excerpt bytes through."""
    if not text:
        return ""
    words = text.split()
    return " ".join(words[:max_words])


def fence_chunk_text(
    *,
    chunk_text: str,
    source_title: str,
    page_start: int | None,
    page_end: int | None,
    seq: int,
    token_count: int,
    max_words: int = MAX_FENCED_EXCERPT_WORDS,
) -> str:
    """Build the fenced replacement for a ``commercial_restricted`` chunk's
    ``text`` field: structured extraction/summary + section identifiers +
    a <=``max_words``-word verbatim excerpt -- design Section 7's exact
    shape. ``chunk_text`` itself is NEVER interpolated whole; only
    :func:`excerpt_words`'s capped output touches the return value."""
    excerpt = excerpt_words(chunk_text, max_words)
    if page_start and page_end and page_start != page_end:
        pages = f"pp. {page_start}-{page_end}"
    elif page_start:
        pages = f"p. {page_start}"
    else:
        pages = "page unknown"
    return (
        f"[license-fenced: commercial_restricted -- {source_title}, {pages}, "
        f"chunk #{seq} ({token_count} tokens elided; full text on disk, not served)] "
        f'verbatim excerpt (<={max_words} words): "{excerpt}..."'
    )


def citation_quote(text: str | None, *, fenced: bool) -> str:
    """The ``citation.quote`` grounding excerpt (design Section 7's
    ``SearchResponse.results[].citation.quote``): capped at
    :data:`MAX_FENCED_EXCERPT_WORDS` words when ``fenced`` (never allowed to
    exceed the D-COC-1 cap even in the citation block), else Section 7's
    plain <=300-char grounding excerpt."""
    if not text:
        return ""
    if fenced:
        return excerpt_words(text, MAX_FENCED_EXCERPT_WORDS)
    return text[:MAX_OPEN_CITATION_QUOTE_CHARS]


def source_license_tier(source: dict[str, Any] | None) -> str | None:
    """Small helper so callers building a citation block from a raw
    ``source`` row don't repeat the ``.get`` -- kept trivial and here so
    it's obvious this is the ONE place "what license tier gates the fence"
    is decided from a source row."""
    return source.get("license_tier") if source else None
