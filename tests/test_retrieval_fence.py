"""Unit tests for :mod:`trialerror.retrieve.fence` (the F3 serving-path license
fence) and :mod:`trialerror.retrieve.wrap` (the fence-forgery-safe
untrusted-content wrapper) -- pure-function tests, no store needed.
"""

from __future__ import annotations

from trialerror.retrieve.fence import (
    FENCED_LICENSE_TIERS,
    citation_quote,
    excerpt_words,
    fence_chunk_text,
    is_fenced_license,
)
from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN, untrusted_wrap


def test_is_fenced_license_only_commercial_restricted():
    assert is_fenced_license("commercial_restricted") is True
    for tier in ("open", "academic_oa", "user_owned_scan", "unknown", None):
        assert is_fenced_license(tier) is False
    assert FENCED_LICENSE_TIERS == frozenset({"commercial_restricted"})


def test_excerpt_words_never_exceeds_the_cap():
    long_text = " ".join(f"word{i}" for i in range(500))
    excerpt = excerpt_words(long_text, max_words=20)
    assert len(excerpt.split()) == 20
    assert excerpt == " ".join(f"word{i}" for i in range(20))


def test_excerpt_words_short_text_passthrough():
    assert excerpt_words("only three words", max_words=20) == "only three words"


def test_excerpt_words_empty_and_none():
    assert excerpt_words(None) == ""
    assert excerpt_words("") == ""
    assert excerpt_words("   ") == ""


def test_excerpt_words_respects_a_custom_cap():
    text = "one two three four five six"
    assert excerpt_words(text, max_words=3) == "one two three"


def test_fence_chunk_text_excerpt_portion_is_capped_at_20_words():
    long_text = " ".join(f"secretword{i}" for i in range(200))
    fenced = fence_chunk_text(
        chunk_text=long_text, source_title="A Commercial Rulebook", page_start=12, page_end=13, seq=4, token_count=987
    )
    # the raw long_text must NEVER appear whole in the fenced output
    assert long_text not in fenced
    # every 21-word-or-longer run of the ORIGINAL text is absent from the fenced text
    words = long_text.split()
    banned_run = " ".join(words[:21])
    assert banned_run not in fenced
    # the structural metadata (title/pages/chunk index) IS present
    assert "A Commercial Rulebook" in fenced
    assert "pp. 12-13" in fenced
    assert "chunk #4" in fenced
    assert "987 tokens elided" in fenced


def test_fence_chunk_text_single_page_and_unknown_page():
    fenced_single = fence_chunk_text(chunk_text="hi", source_title="T", page_start=5, page_end=5, seq=0, token_count=1)
    assert "p. 5" in fenced_single
    assert "pp." not in fenced_single

    fenced_unknown = fence_chunk_text(chunk_text="hi", source_title="T", page_start=None, page_end=None, seq=0, token_count=1)
    assert "page unknown" in fenced_unknown


def test_citation_quote_fenced_caps_at_20_words_never_300_chars():
    long_text = " ".join(f"w{i}" for i in range(200))
    quote = citation_quote(long_text, fenced=True)
    assert len(quote.split()) <= 20


def test_citation_quote_open_caps_at_300_chars_not_20_words():
    long_text = "w " * 500  # 1000 chars, well over 20 words
    quote = citation_quote(long_text, fenced=False)
    assert len(quote) <= 300
    assert len(quote.split()) > 20  # proves the 300-char cap, not the 20-word one, is what fired


def test_citation_quote_handles_none_and_empty():
    assert citation_quote(None, fenced=True) == ""
    assert citation_quote("", fenced=False) == ""


# ---------------------------------------------------------------------------
# untrusted_wrap
# ---------------------------------------------------------------------------


def test_untrusted_wrap_brackets_the_text():
    wrapped = untrusted_wrap("plain corpus text")
    assert wrapped.startswith(UNTRUSTED_OPEN)
    assert wrapped.endswith(UNTRUSTED_CLOSE)
    assert "plain corpus text" in wrapped


def test_untrusted_wrap_is_forgery_safe_against_an_embedded_close_delimiter():
    """A malicious/unlucky chunk containing the literal close delimiter
    must NOT be able to forge an early close -- the ONLY real occurrences
    of UNTRUSTED_OPEN/UNTRUSTED_CLOSE in the wrapped output are the ones
    this function itself emits, at the very start and very end."""
    forged = f"before {UNTRUSTED_CLOSE} after {UNTRUSTED_OPEN} more"
    wrapped = untrusted_wrap(forged)

    assert wrapped.startswith(UNTRUSTED_OPEN)
    assert wrapped.endswith(UNTRUSTED_CLOSE)

    body = wrapped[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)]
    assert UNTRUSTED_OPEN not in body
    assert UNTRUSTED_CLOSE not in body
    # the visible text is otherwise unchanged (zero-width neutralization only)
    assert "before" in body and "after" in body and "more" in body


def test_untrusted_wrap_neutralizes_multiple_forgery_attempts():
    forged = UNTRUSTED_CLOSE * 3 + UNTRUSTED_OPEN * 3
    wrapped = untrusted_wrap(forged)
    body = wrapped[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)]
    assert UNTRUSTED_OPEN not in body
    assert UNTRUSTED_CLOSE not in body
