"""Not a test module (pytest only collects ``test_*.py``) -- shared JSON
fixture loader for the ``trialerror.litapi`` test suite. The canned response
bodies themselves live at ``tests/fixtures/litapi/*.json`` (design brief:
"Ship 2-3 canned fixtures per provider (a DOI hit, a not-found, a
citations page)").

v3-acquisition build (C-0064 flags F1/F2 RESOLVED): also serves
``ArxivProvider``'s canned fixtures, which are raw Atom XML (arXiv's own
wire format -- unlike OpenAlex/Semantic Scholar's JSON), hence
:func:`load_text_fixture` alongside the pre-existing :func:`load_fixture`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "litapi"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_text_fixture(name: str) -> str:
    """Raw-text (non-JSON) fixture loader -- used for arXiv's Atom XML
    responses (``FakeTransport.add_response`` with a raw
    ``TransportResponse(text=...)``, not the JSON-only ``add_json``
    convenience)."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")
