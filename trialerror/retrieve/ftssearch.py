"""FTS5/BM25 prefilter tier. Design Section 7 pipeline step 1: "FTS5/BM25
prefilter to <=500 candidates (exact terms, ids, proper nouns -- the
paper-qa two-stage pattern)". Queries the ``chunk_fts`` table M7's ``index``
handler populates (design Section 4.1: "porter+unicode61").
"""

from __future__ import annotations

from typing import Any, Sequence

from trialerror.stores.store import Store

__all__ = ["DEFAULT_FTS_CANDIDATE_LIMIT", "fts_query_string", "fts_search"]

#: Design Section 7: "FTS5/BM25 prefilter to <=500 candidates".
DEFAULT_FTS_CANDIDATE_LIMIT = 500


def fts_query_string(query: str) -> str:
    """Build a syntactically-safe FTS5 ``MATCH`` string out of arbitrary
    user text: every whitespace-delimited token is quoted as its own
    literal phrase (internal ``"`` doubled per FTS5's quoting rule) and
    phrases are space-joined, which FTS5 treats as an implicit AND.

    A raw pass-through of user text into ``MATCH`` breaks on FTS5's own
    query-syntax characters (``-``, ``:``, unbalanced ``"``, ``NOT``/``OR``
    as bare tokens, ...) -- quoting every token as a literal phrase sidesteps
    all of that while still matching "exact terms, ids, proper nouns" (the
    design's own framing of what this tier is FOR).
    """
    tokens = query.split()
    if not tokens:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def fts_search(
    store: Store,
    query: str,
    *,
    limit: int = DEFAULT_FTS_CANDIDATE_LIMIT,
    chunk_id_allowlist: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the FTS prefilter. Returns rows ``{chunk_id, bm25}`` ordered
    best-first (FTS5's ``bm25()`` is lower-is-better, so the SQL orders
    ascending; callers wanting a 1-based "best first" rank just enumerate
    the returned list in order).

    ``chunk_id_allowlist``, when given, restricts candidates to that set
    (used by :mod:`trialerror.retrieve.engine` to apply ``SearchRequest.filters``
    -- e.g. ``source_ids``/``kind``/``license_tier``/``year`` -- without
    this module needing to know anything about ``source``/``document``
    joins itself).
    """
    match = fts_query_string(query)
    if not match.strip('"'):
        return []
    if chunk_id_allowlist is not None:
        allowlist = list(chunk_id_allowlist)
        if not allowlist:
            return []
        placeholders = ",".join("?" for _ in allowlist)
        sql = (
            f"SELECT chunk_id, bm25(chunk_fts) AS bm25 FROM chunk_fts "
            f"WHERE chunk_fts MATCH ? AND chunk_id IN ({placeholders}) "
            f"ORDER BY bm25(chunk_fts) ASC LIMIT ?"
        )
        params: list[Any] = [match, *allowlist, limit]
    else:
        sql = "SELECT chunk_id, bm25(chunk_fts) AS bm25 FROM chunk_fts WHERE chunk_fts MATCH ? ORDER BY bm25(chunk_fts) ASC LIMIT ?"
        params = [match, limit]
    rows = store.knowledge.execute(sql, params).fetchall()
    return [{"chunk_id": r["chunk_id"], "bm25": r["bm25"]} for r in rows]
