"""Injection-defense sanitizer wrapper. Design Section 6 stage 3:
"Injection-defense sanitizer (Trojan-Source/invisible codepoints;
book-to-skill port, vendored MIT) runs HERE, version-stamped on the
document."

Thin wrapper over the vendored ``sanitize_extracted_text`` (verbatim MIT
port, ``vendored/book-to-skill-sanitizer/sanitize.py`` -- see
``vendored/VENDORED.md`` for the manifest row) so a version bump to the
sanitization RULES (not just re-vendoring the same upstream commit) is a
change to :data:`SANITIZER_VERSION` here, stamped onto every
``document.sanitizer_version`` -- the same "version now, so a future rule
change can't silently re-shift already-anchored text" discipline
``stream_v1`` applies to chunking/anchoring.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

__all__ = ["SANITIZER_VERSION", "sanitize"]

#: Bump when the underlying vendored rules change (a re-vendor at a newer
#: upstream commit, or a local patch) -- NOT on every trivial re-vendor at
#: the same logical rule set.
SANITIZER_VERSION = "book-to-skill-v1"

_VENDORED_PATH = Path(__file__).resolve().parents[2] / "vendored" / "book-to-skill-sanitizer" / "sanitize.py"


def _load_vendored_sanitize_module():
    """Load the vendored module by file path (no ``sys.path`` mutation --
    a generically-named top-level module called "sanitize" has no business
    on this process's import path) with bytecode caching disabled for the
    duration: a stray ``vendored/book-to-skill-sanitizer/__pycache__/``
    directory would fail ``trialerror doctor --license-audit`` (M0's check
    scans every FILE under a vendored item's directory for the header
    block; a compiled ``.pyc`` has none). ``sys.dont_write_bytecode`` is
    restored immediately after, so this has no effect on any other import
    in the process."""
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("_trialerror_vendored_book_to_skill_sanitize", _VENDORED_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


sanitize_extracted_text = _load_vendored_sanitize_module().sanitize_extracted_text


def sanitize(text: str) -> tuple[str, int]:
    """Strip document-borne prompt-injection codepoints from ``text``.

    Returns ``(sanitized_text, removed_count)``. Called on every element's
    text during normalize/OCR, before it is written to the ``element``
    table -- so ``stream_v1`` (built from already-sanitized element text)
    never has to re-derive or care about sanitization.
    """
    return sanitize_extracted_text(text)
