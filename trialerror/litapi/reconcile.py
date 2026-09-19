"""The post-processor: merges redundant multi-provider results into one
normalized record. Design brief: "a post-processor that merges provider
results into one normalized record (DOI-preferred identity, arxiv->DOI
normalization, dedup, provenance = which providers contributed)" -- the
paper-qa ``DEFAULT_CLIENTS``/``ALL_CLIENTS`` reconciliation pattern
(``docs/mining/S1-scilit-1__paper-qa.md``), reimplemented natively against
this package's own :class:`~trialerror.litapi.models.WorkRecord` rather than
importing paper-qa's ``MetadataPostProcessor``/``DocDetails`` (the mining
report's own "pattern-only" integration-path recommendation).

Two entry points:

- :func:`merge_one` -- merge a list of records ALREADY known to describe
  the same paper (the common case: one ``get_by_doi``/``get_by_arxiv``
  call per provider, same identifier). Used by
  ``trialerror.litapi.client.LitApiClient.lookup_doi``/``lookup_arxiv``.
- :func:`reconcile_many` -- group an unordered pool of records by identity
  (:meth:`WorkRecord.identity_key`) and merge each group, for callers that
  don't already know which records match (search results, citation
  listings pooled across providers). Used by
  ``trialerror.litapi.client.LitApiClient.search``.

TRIALERROR-DEV-NOTE (scope, disclosed): paper-qa's own post-processor layer
also includes journal-quality and retraction-checking enrichment
(``JournalQualityPostProcessor``, ``RetractionDataPostProcessor`` --
``DEFAULT_CLIENTS`` in the mining report). Neither is implemented here --
this v1-preview build has no journal-quality/retraction DATA SOURCE
integrated (that would be its own provider), so there is nothing for such
a post-processor to enrich with yet. The seam is real (a v1 build can add
one as a pure function ``WorkRecord -> WorkRecord`` run after
:func:`merge_one`/``reconcile_many``, same as paper-qa's own
provider/post-processor split), just not built in this bounded session.
"""

from __future__ import annotations

from typing import Sequence

from trialerror.litapi.models import WorkRecord

__all__ = ["merge_one", "reconcile_many"]


def _prefer(*values):
    """First non-``None``/non-empty value, left to right (provider-order
    priority -- callers control priority via the ORDER of the ``records``
    list they pass in, e.g. ``DEFAULT_CLIENTS`` order)."""
    for v in values:
        if v is not None and v != "" and v != []:
            return v
    return None


def merge_one(records: Sequence[WorkRecord]) -> WorkRecord:
    """Merge records already known to be the same paper into one. Field
    fill rule: first non-empty value wins, in the given list order
    (provider-priority -- the caller decides provider order by the order
    it queried them in); ``citation_count`` takes the MAX across records
    instead (different providers count differently and a higher count is
    never wrong to surface, per the "redundant fetching" resilience goal);
    ``authors``/``external_ids``/``other`` are unioned rather than
    picked-one; ``providers`` is the union of every contributing record's
    own ``providers`` list (each freshly-fetched record carries exactly
    one provider name -- see ``trialerror.litapi.providers.base.Provider``'s
    docstring -- so this becomes the full provenance list).

    Raises ``ValueError`` on an empty list -- there is nothing to merge
    (callers should not call this with zero records; ``LitApiClient``
    raises :class:`~trialerror.litapi.errors.AllProvidersFailedError` earlier
    in that case instead)."""
    if not records:
        raise ValueError("merge_one() requires at least one record")
    if len(records) == 1:
        # still normalize: return a copy so callers never accidentally
        # mutate a provider's freshly-returned record through the merged one.
        r = records[0]
        return WorkRecord(
            title=r.title, doi=r.doi, arxiv_id=r.arxiv_id, authors=list(r.authors), year=r.year,
            venue=r.venue, abstract=r.abstract, citation_count=r.citation_count, oa_pdf_url=r.oa_pdf_url,
            url=r.url, external_ids=dict(r.external_ids), providers=list(r.providers), other=dict(r.other),
        )

    merged = WorkRecord()
    merged.title = _prefer(*(r.title for r in records))
    merged.doi = _prefer(*(r.doi for r in records))
    merged.arxiv_id = _prefer(*(r.arxiv_id for r in records))
    merged.year = _prefer(*(r.year for r in records))
    merged.venue = _prefer(*(r.venue for r in records))
    merged.url = _prefer(*(r.url for r in records))
    merged.oa_pdf_url = _prefer(*(r.oa_pdf_url for r in records))
    # longest non-empty abstract wins (a fuller record beats a truncated one).
    abstracts = [r.abstract for r in records if r.abstract]
    merged.abstract = max(abstracts, key=len) if abstracts else None
    # longest author list wins (one provider may only carry first-author).
    author_lists = [r.authors for r in records if r.authors]
    merged.authors = max(author_lists, key=len) if author_lists else []
    citation_counts = [r.citation_count for r in records if r.citation_count is not None]
    merged.citation_count = max(citation_counts) if citation_counts else None

    external_ids: dict[str, str] = {}
    other: dict = {}
    providers: list[str] = []
    for r in records:
        for k, v in r.external_ids.items():
            external_ids.setdefault(k, v)
        for p in r.providers:
            if p not in providers:
                providers.append(p)
                other[p] = r.other
    merged.external_ids = external_ids
    merged.other = other
    merged.providers = providers
    return merged


def reconcile_many(records: Sequence[WorkRecord]) -> list[WorkRecord]:
    """Group ``records`` by :meth:`WorkRecord.identity_key` (DOI-preferred
    -> arxiv-derived-DOI -> normalized-title, see
    ``trialerror.litapi.models`` module docstring) and :func:`merge_one` each
    group. A record whose ``identity_key()`` is ``None`` (no title/DOI/
    arXiv id at all -- a malformed provider response) is kept standalone,
    unmerged, rather than dropped -- reconciliation should never silently
    lose a record.

    Order is stable: groups appear in the order their first member first
    appeared in ``records``, so a caller truncating a search result list
    (``merged[:limit]``) gets the same top results a naive dedup-free list
    would have, just deduplicated.
    """
    groups: dict[str, list[WorkRecord]] = {}
    order: list[str] = []
    standalone: list[WorkRecord] = []
    for r in records:
        key = r.identity_key()
        if key is None:
            standalone.append(r)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    merged = [merge_one(groups[key]) for key in order]
    merged.extend(standalone)
    return merged
