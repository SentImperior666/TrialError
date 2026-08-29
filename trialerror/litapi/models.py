"""Normalized record shapes + identity normalization. Mirrors (field-for-
field where it maps cleanly, per the mining report's "safe to mirror"
note) paper-qa's ``DocDetails`` (Apache-2.0, ``src/paperqa/types.py``, see
``docs/mining/S1-scilit-1__paper-qa.md``) without importing paper-qa
itself -- this package builds its own small dataclasses against our own
provider clients rather than pulling in paper-qa's ``Settings``/``Docs``
object graph (the mining report's own "pattern-only" integration-path
recommendation).

Identity normalization (design brief: "DOI-preferred identity, arxiv->DOI
normalization"):

- :func:`normalize_doi` -- lowercases and strips any ``doi:``/
  ``https://doi.org/`` wrapper a provider's response happened to include
  (OpenAlex returns full URLs, e.g. ``"https://doi.org/10.1234/x"``;
  Semantic Scholar returns bare DOIs, e.g. ``"10.1234/x"`` -- both must
  normalize to the same string for reconciliation to group them).
- :func:`normalize_arxiv_id` -- strips an ``arXiv:`` prefix and a trailing
  version suffix (``v2``) case-insensitively.
- :func:`arxiv_to_doi` -- arXiv's own self-assigned DOI prefix
  (``10.48550/arXiv.<id>``, a real, documented arXiv convention since
  2022) used as the reconciliation fallback identity for an
  arXiv-only-preprint record with no journal DOI from either provider yet
  -- this is the "arxiv->DOI normalization" the design brief names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "WorkRecord",
    "CitationEdge",
    "CitationsPage",
    "normalize_doi",
    "normalize_arxiv_id",
    "arxiv_to_doi",
    "normalize_title",
]

_DOI_PREFIX_RE = re.compile(r"^\s*(doi\s*:\s*|https?://(dx\.)?doi\.org/)", re.IGNORECASE)
_ARXIV_PREFIX_RE = re.compile(r"^\s*arxiv\s*:\s*", re.IGNORECASE)
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)
_TITLE_NONWORD_RE = re.compile(r"[^a-z0-9]+")


def normalize_doi(doi: str | None) -> str | None:
    """Lowercase, strip a ``doi:``/``https://doi.org/`` wrapper, strip
    surrounding whitespace. ``None``/empty in, ``None`` out."""
    if not doi:
        return None
    stripped = _DOI_PREFIX_RE.sub("", doi.strip())
    stripped = stripped.strip().strip("/")
    return stripped.lower() or None


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """Strip an ``arXiv:`` prefix and a trailing version suffix
    (``2101.00001v2`` -> ``2101.00001``) so the same preprint at two
    versions still reconciles to one identity. ``None``/empty in,
    ``None`` out."""
    if not arxiv_id:
        return None
    stripped = _ARXIV_PREFIX_RE.sub("", arxiv_id.strip()).strip()
    stripped = _ARXIV_VERSION_RE.sub("", stripped)
    return stripped.lower() or None


def arxiv_to_doi(arxiv_id: str | None) -> str | None:
    """arXiv's own DOI prefix (``10.48550/arXiv.<id>``). Used only as a
    reconciliation identity fallback when no provider returned a native
    (journal) DOI -- never presented to a caller as "the" DOI without also
    carrying the original ``arxiv_id`` field, since a paper can later gain
    a distinct journal DOI that supersedes this synthesized one."""
    normalized = normalize_arxiv_id(arxiv_id)
    if not normalized:
        return None
    return f"10.48550/arxiv.{normalized}"


def normalize_title(title: str | None) -> str | None:
    """Casefold + collapse-non-alphanumerics normalization, the
    last-resort reconciliation key when neither record carries a DOI or
    arXiv id. TRIALERROR-DEV-NOTE (scope deviation, disclosed): paper-qa's own
    reconciliation does real title-*similarity* fuzzy matching (per the
    mining report). This v1-preview build does exact-normalized-title
    matching only, to keep the dependency footprint at zero (no
    rapidfuzz/thefuzz) within the bounded preview scope -- a v1 full build
    is the natural place to add fuzzy matching if reconciliation misses
    from near-duplicate titles (whitespace/punctuation/subtitle drift)
    turn out to matter in practice."""
    if not title:
        return None
    normalized = _TITLE_NONWORD_RE.sub(" ", title.strip().casefold()).strip()
    return normalized or None


@dataclass
class WorkRecord:
    """One normalized literature record. Field selection mirrors the
    mining reports' field lists directly: OpenAlex's ``select=`` param
    convention and Semantic Scholar's ``fields=`` convention both name
    (nearly) this same set (title, authors, year, venue, citation counts,
    OA PDF url, abstract) -- see each provider module's own
    ``*_FIELDS``/``select=`` constant for the exact wire-level list."""

    title: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    abstract: str | None = None
    citation_count: int | None = None
    oa_pdf_url: str | None = None
    url: str | None = None
    #: provider-native ids this record resolved through, e.g.
    #: ``{"openalex": "W123...", "semanticscholar": "649def..."}``.
    external_ids: dict[str, str] = field(default_factory=dict)
    #: provenance -- which provider(s) contributed to this record. A
    #: freshly-fetched single-provider record carries exactly one name;
    #: after :func:`trialerror.litapi.reconcile.merge_one`/``reconcile_many`` it
    #: carries every provider that agreed on this identity.
    providers: list[str] = field(default_factory=list)
    #: provider-specific extras not promoted to a first-class field above
    #: (paper-qa's ``DocDetails.other`` bag pattern, per the mining
    #: report), keyed by provider name so two providers' extras never
    #: collide: ``{"semanticscholar": {"influentialCitationCount": 5}}``.
    other: dict[str, Any] = field(default_factory=dict)

    def identity_key(self) -> str | None:
        """DOI-preferred identity (design brief, verbatim): normalized DOI
        first, then the arXiv-derived DOI fallback, then a normalized-title
        last resort. Returns ``None`` only when the record carries none of
        the three (a malformed/empty record) -- callers should treat that
        as "cannot be reconciled, keep standalone"."""
        doi = normalize_doi(self.doi)
        if doi:
            return doi
        arxiv_doi = arxiv_to_doi(self.arxiv_id)
        if arxiv_doi:
            return arxiv_doi
        return normalize_title(self.title)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "authors": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "abstract": self.abstract,
            "citation_count": self.citation_count,
            "oa_pdf_url": self.oa_pdf_url,
            "url": self.url,
            "external_ids": dict(self.external_ids),
            "providers": list(self.providers),
            "other": dict(self.other),
        }


@dataclass
class CitationEdge:
    """One entry in a citations listing (a paper citing, or cited by, the
    lookup target -- which direction is a property of which provider
    endpoint/method produced the :class:`CitationsPage`, not of this
    dataclass itself)."""

    title: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    external_ids: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "year": self.year,
            "authors": list(self.authors),
            "external_ids": dict(self.external_ids),
        }


@dataclass
class CitationsPage:
    """One page of a citations listing, as returned by exactly ONE
    provider (:meth:`trialerror.litapi.client.LitApiClient.get_citations` tries
    providers in order and returns the first success -- see that module's
    docstring for why citations are NOT reconciled/merged across
    providers in this v1-preview build)."""

    items: list[CitationEdge]
    provider: str
    offset: int
    limit: int
    total: int | None = None
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() for i in self.items],
            "provider": self.provider,
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "has_more": self.has_more,
        }
