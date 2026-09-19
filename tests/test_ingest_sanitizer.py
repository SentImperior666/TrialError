"""Tests for ``trialerror.ingest.sanitizer`` (design Section 6 stage 3:
injection-defense sanitizer, vendored book-to-skill MIT port)."""

from __future__ import annotations

from trialerror.ingest.sanitizer import SANITIZER_VERSION, sanitize


def test_sanitize_strips_zero_width_codepoints():
    text = "hello​world"
    cleaned, removed = sanitize(text)
    assert cleaned == "helloworld"
    assert removed == 1


def test_sanitize_strips_bidi_override_trojan_source():
    """CVE-2021-42574-class Trojan-Source: bidi override controls that
    change rendered order without changing the character sequence."""
    text = "safe‮text"
    cleaned, removed = sanitize(text)
    assert "‮" not in cleaned
    assert removed == 1


def test_sanitize_leaves_ordinary_text_untouched():
    text = "The quick brown fox jumps over the lazy dog. 123!"
    cleaned, removed = sanitize(text)
    assert cleaned == text
    assert removed == 0


def test_sanitize_preserves_legitimate_rtl_script():
    """design docstring: "Legitimate right-to-left books are unaffected."""
    text = "السلام"  # Arabic "peace" -- no bidi CONTROL codepoints, just RTL letters
    cleaned, removed = sanitize(text)
    assert cleaned == text
    assert removed == 0


def test_sanitizer_version_is_stamped_string():
    assert isinstance(SANITIZER_VERSION, str)
    assert SANITIZER_VERSION
