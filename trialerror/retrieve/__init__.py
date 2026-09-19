"""``trialerror.retrieve`` -- the hybrid retrieval engine. Design Section 12 (M8
row): "hybrid engine (fts->vec->RRF), citation bundle, untrusted-wrap,
serving-path license fence (Section 7), resolve_quote." Design Section 7
(Retrieval API contract): "One engine behind ``search`` (MCP), ``trialerror
query search`` (CLI), and the verification pipelines."

This package is the ONLY place that builds a ``SearchResponse``-shaped
result, applies the F3 serving-path license fence, or wraps corpus text in
the fence-forgery-safe untrusted marker -- every caller (the ``query`` CLI
group, the ``trialerror-knowledge`` MCP server, and (later) M9's verification
pipelines) is a thin wrapper over :mod:`trialerror.retrieve.engine`, never a
second implementation.
"""

from __future__ import annotations

__all__: list[str] = []
