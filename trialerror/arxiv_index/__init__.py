"""``trialerror.arxiv_index`` -- the all-arXiv semantic search feature
(``docs/reviews/ALL_ARXIV_SEARCH.md`` Sec 2's "local index plan", built out
in the ``build-arxiv-kaggle-index`` session, corrections v69/C-0069's
sibling deliverable). Builds a STANDALONE local semantic-search index over
the arXiv Xplorer author's Kaggle-published embeddings dataset
(``tomtum/openai-arxiv-embeddings``: MIT-licensed, OpenAI
``text-embedding-3-large``, 3072-dim, title+abstract only, ~2.7-2.9M rows,
~34.9GB zip, weekly updates).

This is deliberately a SEPARATE store from ``trialerror.stores`` knowledge.db --
not a program's research corpus, not XID-governed, no citation/fencing
machinery -- just a big precomputed vector index a program can query. Five
submodules, mirroring this repo's own subsystem-per-file convention
(``trialerror/ingest/backends.py`` + ``handlers.py`` split, applied here):

- :mod:`trialerror.arxiv_index.store` -- the standalone sqlite db: schema
  (vec0-or-fallback vector table + metadata table + build-state table),
  open/close, disk preflight.
- :mod:`trialerror.arxiv_index.encoder` -- the OpenAI ``text-embedding-3-large``
  QUERY-time encoder seam (``Fake``/``OpenAI``, mirrors
  ``trialerror.ingest.backends``'s ``EmbedBackend`` split) -- reads the API key
  ONLY from a configured file path, exactly like
  :func:`trialerror.litapi.config.resolve_api_key`'s "never inline, never log
  it" discipline (this module reuses that function directly).
- :mod:`trialerror.arxiv_index.ingest` -- streaming zip ingest (``zipfile``
  member streams, never full-extract), resumable via the jobs ledger.
- :mod:`trialerror.arxiv_index.query` -- the query path: sqlite-vec's native
  ``MATCH ... ORDER BY distance LIMIT k`` operator (BAKEOFF_REPORT.md
  Sec B.4b's named trigger case -- this dataset's scale is exactly what
  that section describes), with a brute-force cosine fallback for a
  machine without the sqlite-vec extension.
- :mod:`trialerror.arxiv_index.handlers` -- ``@register_handler("arxiv_index_build")``,
  auto-discovered by ``trialerror.jobs.registry.discover_and_register_handlers``
  purely because this package has a ``handlers.py`` (zero shared-file
  edits to wire it in).
- :mod:`trialerror.arxiv_index.checks` -- ``arxiv_index_ready`` doctor check,
  auto-discovered by ``trialerror.util.doctor.discover_and_register_checks`` the
  same way (this package has a ``checks.py``).

**TRIALERROR-DEV-NOTE (dataset schema: ASSUMED, loudly, per the build brief's
own escape hatch):** the Kaggle dataset's own page publishes a
schema.org ``Dataset`` block (license/model/dims/size/version -- all
CONFIRMED, see ``docs/reviews/ALL_ARXIV_SEARCH.md`` Sec 2) but no per-row
file format or column list, and the dataset's own example notebook
(``kaggle.com/code/tomtum/arxiv-embeddings-example``) is a JS-rendered SPA
page with no server-side-embedded source this session could recover
passively (confirmed by direct ``curl`` of the raw HTML -- no embedded
JSON, unlike the dataset page itself). This build did NOT download the
34.9GB zip to inspect it directly (out of scope, no Kaggle credentials
exist on this machine per the build brief). The ASSUMED schema below is
grounded in the one thing that IS independently, passively confirmed: the
companion ``Cornell-University/arxiv`` dataset (this dataset's own
declared "in sync with" source) publishes its column list on ITS OWN
Kaggle page in plain prose -- ``id``, ``submitter``, ``authors``,
``title``, ``comments``, ``journal-ref``, ``doi``, ``abstract``,
``categories``, ``versions`` -- as a newline-delimited JSON file (one
object per paper). This build assumes the embeddings dataset carries the
same shared field names (``id``/``title``/``abstract``/``categories``/
``authors``, whichever the OpenAI dataset's own row happens to include)
PLUS an ``embedding`` field (list of 3072 floats), streamed as one or more
``.jsonl``-glob members inside the zip -- the only format
:func:`trialerror.arxiv_index.ingest.iter_zip_records` can stream
member-by-member via the stdlib ``zipfile`` module the way the build brief
requires (a columnar format like parquet would need a non-stdlib reader
and typically isn't line-streamable from a raw zip member the same way).
**If the real file turns out to be a different format or field-naming**,
:data:`trialerror.arxiv_index.ingest.DEFAULT_FIELD_MAP` and
``[litapi.arxiv_index].member_glob``/``record_format`` are the two knobs
an operator adjusts -- see ``docs/USER_SETUP.md`` Sec 3e's explicit
"if the real download doesn't match this" note.
"""

from __future__ import annotations
