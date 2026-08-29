"""The untrusted-content wrapper. ``DESIGN_v0.md`` Appendix B (MCP anti-
pattern compliance): "Unsanitized Resource Content: all corpus text served
through the untrusted-wrapper (fence-forgery-safe) + ingest-time
sanitizer." Design Section 5.1: "``trialerror-knowledge`` -- Resource Gateway
(read-only; all content sanitized, untrusted-wrapped, and license-fenced
per Section 7)."

Two independent defenses compose here:

- The ingest-time sanitizer (``trialerror.ingest.sanitizer``, M7) strips
  Trojan-Source/invisible-codepoint injection payloads BEFORE text ever
  reaches ``element``/``chunk`` rows -- this module does not repeat that
  work.
- This module's job is different: mark served text as DATA, not
  instructions, for whatever reads the MCP tool result -- and do so in a
  way a malicious (or merely unlucky) document body cannot defeat by
  containing a string that looks like the wrapper's own closing delimiter.
  A wrapper that just concatenated ``f"<open>{text}</close>"`` would let a
  corpus chunk containing the literal substring ``</open>`` forge an early
  close and inject text that reads, to whatever consumes the wrapped
  output, as being OUTSIDE the untrusted region -- "fence-forgery-safe"
  names exactly this failure mode.

:func:`untrusted_wrap`'s defense: neutralize any occurrence of the EXACT
delimiter strings inside the content (a zero-width space spliced into the
tag name) before wrapping, so the delimiter can never appear byte-for-byte
inside the wrapped body -- only the two delimiters this function itself
emits are ever unbroken. This changes zero visible characters (a
zero-width space renders invisibly) so it does not corrupt legitimate
excerpt text, including the vanishingly unlikely case of a chunk that
happens to contain the tag name for some other reason.
"""

from __future__ import annotations

__all__ = ["UNTRUSTED_OPEN", "UNTRUSTED_CLOSE", "untrusted_wrap"]

UNTRUSTED_OPEN = "<untrusted-document-content>"
UNTRUSTED_CLOSE = "</untrusted-document-content>"

#: Zero-width space (U+200B) -- invisible when rendered, but breaks an
#: exact substring match against :data:`UNTRUSTED_OPEN`/:data:`UNTRUSTED_CLOSE`.
_ZWSP = "​"


def _neutralize(text: str) -> str:
    return text.replace(UNTRUSTED_OPEN, f"<untrusted-document-content{_ZWSP}>").replace(
        UNTRUSTED_CLOSE, f"</untrusted-document-content{_ZWSP}>"
    )


def untrusted_wrap(text: str) -> str:
    """Wrap ``text`` in the untrusted-content delimiters, having first
    neutralized any occurrence of those exact delimiters already present
    in ``text`` -- so the returned string's outermost
    :data:`UNTRUSTED_OPEN`/:data:`UNTRUSTED_CLOSE` pair is always the real
    boundary, never one forged from corpus content."""
    return f"{UNTRUSTED_OPEN}\n{_neutralize(text)}\n{UNTRUSTED_CLOSE}"
