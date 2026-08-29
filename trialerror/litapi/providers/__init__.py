"""Provider clients: one module per literature-metadata source, each
implementing the common :class:`~trialerror.litapi.providers.base.Provider`
interface (``get_by_doi``, ``get_by_arxiv``, ``search``, ``get_citations``).

v1-preview shipped exactly two (design brief: "the two the stack decisions
chose as redundant" -- ``docs/STACK_DECISIONS_draft.md``'s citation-graph
recommendation) and documented a third-provider seam verbatim: "drop a new
``trialerror/litapi/providers/<name>.py`` implementing the same Protocol and
add it to ``trialerror.litapi.client.ALL_CLIENTS`` -- nothing else in this
package needs to change." The v3-acquisition build (C-0064 flags F1/F2
RESOLVED, ``docs/EXTERNAL_API_FACTS.md``) exercises that exact seam twice:
:class:`~trialerror.litapi.providers.arxiv.ArxivProvider` (keyless, ToU-paced)
and :class:`~trialerror.litapi.providers.unpaywall.UnpaywallProvider`
(email-identified, OA-location-only) -- both join
``trialerror.litapi.client.ALL_CLIENTS`` (``DEFAULT_CLIENTS`` stays the
original two, unchanged, per that module's own docstring).
"""

from __future__ import annotations

from trialerror.litapi.providers.arxiv import ArxivProvider
from trialerror.litapi.providers.base import Provider, RateLimiter
from trialerror.litapi.providers.openalex import OpenAlexProvider
from trialerror.litapi.providers.semanticscholar import SemanticScholarProvider
from trialerror.litapi.providers.unpaywall import UnpaywallProvider

__all__ = [
    "Provider",
    "RateLimiter",
    "OpenAlexProvider",
    "SemanticScholarProvider",
    "ArxivProvider",
    "UnpaywallProvider",
]
