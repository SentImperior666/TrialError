"""The tantivy-backed lexical tier (C-0073 item 4 / C-0080). Same job as
:mod:`trialerror.retrieve.ftssearch` -- design Section 7 pipeline step 1,
"FTS5/BM25 prefilter to <=500 candidates" -- against an embedded tantivy
(Lucene-lineage, Rust, MIT) inverted index instead of SQLite's ``chunk_fts``
virtual table.

**Why**: ``spikes/index_bakeoffs/BAKEOFF_REPORT.md`` Sec A.3, the verdict of
record: "SWITCH to tantivy -- tantivy wins on every measured axis at every
measured scale" (query p50 23x/51x/64x faster at 15k/150k/750k chunks, disk
~2.1x smaller, build 1.4-1.9x faster). Design ``DESIGN_v0.md`` D2 had already
pre-scoped the swap: "the fusion layer is backend-agnostic so swapping in
tantivy later is config + an indexer" -- this module is that indexer, and
:mod:`trialerror.retrieve.lexical` is that config.

**The index is DERIVED STATE, never truth.** ``knowledge.db``'s ``chunk``
table is the source of truth; this index is rebuildable from it at any time
(``trialerror ingest reindex-fulltext``) -- the same "indexes are cache,
never truth, rebuildable from chunks+emb" convention design Section 6 stage
7 already states for ``chunk_fts``/``vec_chunks__*``. Three consequences
this module implements rather than merely asserts:

1. **Readiness is a sidecar, written LAST.** A tantivy index directory
   becomes *usable* only once ``trialerror_fulltext.json``
   (:data:`META_FILENAME`) sits beside tantivy's own ``meta.json`` with a
   matching :data:`SCHEMA_VERSION`. :func:`reindex` deletes
   that sidecar BEFORE it starts rebuilding and rewrites it only after the
   final ``commit()`` -- so a crash mid-rebuild leaves a directory that
   every reader treats as "not ready" and falls back to FTS5 over, rather
   than a half-built index silently serving a fraction of the corpus.
   Same for a crash mid-incremental-batch: the sidecar's chunk fingerprint
   stops matching ``knowledge.db``'s, and ``doctor``'s
   ``fulltext_index_stale`` check says so.
2. **The fingerprint is order-independent and incrementally maintainable.**
   :func:`chunk_fingerprint` XOR-folds a 16-byte BLAKE2b digest per
   ``chunk_id``; XOR is commutative, so the doctor can recompute the whole
   corpus's value from a single unordered ``SELECT chunk_id FROM chunk``
   scan (no text read, no sort) and :meth:`FulltextIndex.add_chunks` can
   fold newly-added ids into the stored value without rescanning. ids are
   unique (PRIMARY KEY), so XOR's duplicate-cancellation hazard cannot
   arise.
3. **Adds are idempotent.** :meth:`FulltextIndex.add_chunks` asks the index
   which of the offered ``chunk_id``\\ s it already holds (one
   ``TermSetQuery``) and writes only the rest -- so re-running ``index``
   for a document after a kill, exactly the restart-safety discipline
   ``trialerror.ingest.handlers``' module docstring describes for every
   other stage, adds nothing and changes no fingerprint.

**Query semantics are deliberately identical to FTS5's**, not
tantivy-native-maximal: :func:`trialerror.retrieve.ftssearch.fts_query_string`
quotes every WHITESPACE token as its own literal phrase and space-joins the
result, which FTS5 reads as an implicit AND of phrases -- and a "phrase" of
one term is just that term, but a token that itself tokenizes to more than
one term (``state-of-the-art`` -> ``state``/``of``/``the``/``art``, an
email address, a ``U.S.A.``-style abbreviation) becomes a real ADJACENCY
requirement under FTS5. :meth:`FulltextIndex.search` mirrors this exactly,
per whitespace token: a token that analyzes to one term is a ``TermQuery``,
a token that analyzes to more than one is a ``Query.phrase_query`` with
``slop=0`` (exact, in-order adjacency), and the per-token queries are
AND-ed together the same way FTS5 space-joins its phrases. Fix-pass note
(finding D-1): an earlier revision built one flat conjunction of
``TermQuery`` clauses over the WHOLE query's analyzed terms, which is a
strictly wider match than FTS5's phrase-per-token semantics for any
punctuated token -- measured, that let ``state-of-the-art`` and
``ops@example.com``-shaped queries return chunks FTS5 would not. The ``text``
field is therefore indexed with ``index_option="position"`` (not ``"freq"``),
and :data:`SCHEMA_VERSION` bumped, so a pre-fix index is treated as
not-ready and rebuilt rather than silently queried with the old (wider)
semantics. No OR/NOT, no fuzzy either way -- a user typing ``NOT`` gets a
literal search term on both backends. Building the query from
``TextAnalyzer.analyze`` output rather than ``Index.parse_query`` also means
tantivy's query grammar is never exposed to raw user text, so the whole
class of "``:``/``-``/``^``/unbalanced-quote breaks the query" bugs
``fts_query_string`` exists to prevent cannot occur here either.

**Analyzer parity, and its two honest gaps**: FTS5 runs ``porter unicode61``
(Unicode-aware tokenization + diacritic folding, then the ORIGINAL 1980
Porter stemmer). :data:`ANALYZER_NAME` mirrors that as
``simple -> lowercase -> ascii_fold -> stemmer("english")``, with two
measured, bounded divergences from it (both widen recall, never narrow it,
and neither changes what a hit MEANS -- only whether a handful of extra
terms are considered the "same" word):

1. tantivy's Snowball English stemmer is Porter2, FTS5's is the original
   1980 Porter -- measured over the test corpora's own ~1,200-term
   vocabulary, exactly 2 terms diverge (``intentionally`` -> ``intention``
   under Porter1 vs ``intent`` under Porter2, and ``this`` -> ``thi`` vs
   ``this``).
2. ``Filter.ascii_fold()`` folds every Latin letter to its ASCII base
   (``Ø`` -> ``o``, ``ß`` -> ``ss``, ...); FTS5 unicode61's diacritic
   folding removes COMBINING marks (``café``/``naïfs`` fold to
   ``cafe``/``naifs`` on both backends -- that much agrees) but does not
   fold a distinct base letter, so ``Øresund`` matches a query of
   ``oresund`` on tantivy and not on FTS5. tantivy-py ships no filter that
   reproduces unicode61's exact folding table, so this is documented and
   measured (parity tolerance 3) rather than eliminated.

``tests/test_retrieval_backend_parity.py`` pins both: it measures each
divergence against the live corpus every run, and for every term where the
two analyzers agree it demands byte-identical result rows.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
from hashlib import blake2b
from pathlib import Path
from typing import Any, Iterable, Sequence

from trialerror.util.timeutil import now

__all__ = [
    "ANALYZER_NAME",
    "ANALYZER_VERSION",
    "META_FILENAME",
    "SCHEMA_VERSION",
    "TantivyUnavailableError",
    "FulltextIndexNotReadyError",
    "FulltextIndex",
    "create_fulltext_index",
    "add_chunks",
    "chunk_fingerprint",
    "corpus_fingerprint",
    "index_status",
    "iter_chunk_rows",
    "open_fulltext_index",
    "reindex",
    "tantivy_module",
    "tantivy_available",
]

#: Bumped whenever the on-disk field layout or the analyzer pipeline
#: changes in a way that makes an existing index's postings wrong for the
#: current query path. A sidecar carrying a different value is treated as
#: "not ready" (the reader falls back to FTS5, doctor says reindex) rather
#: than silently queried with mismatched semantics.
#:
#: v1 -> v2 (fix pass, finding D-1): ``text`` moved from
#: ``index_option="freq"`` to ``"position"`` so phrase queries are possible
#: at all -- a v1 index has no ``.pos`` file and cannot serve the v2 query
#: path's phrase clauses correctly, so v1 sidecars must be treated as
#: not-ready rather than queried with the field they lack.
SCHEMA_VERSION = 2

#: The registered name of this codebase's custom text analyzer. It must be
#: registered on EVERY ``tantivy.Index`` handle (read or write) before use
#: -- tantivy stores the tokenizer NAME in the schema, not the tokenizer.
ANALYZER_NAME = "trialerror_en_v1"
ANALYZER_VERSION = 1

#: Readiness sidecar, written after the final commit (see module docstring).
#: NOT ``meta.json`` -- that name is taken: tantivy keeps its OWN index
#: manifest (schema + segment list, rewritten atomically on every commit)
#: at ``<index_dir>/meta.json``, so a sidecar by that name silently
#: destroys the index it was meant to describe.
META_FILENAME = "trialerror_fulltext.json"

#: tantivy-py refuses a writer heap below 15MB/thread. 64MB single-threaded
#: is comfortably above that and keeps the segment layout of a rebuild
#: deterministic in CONTENT (segment filenames are UUIDs, so a rebuild is
#: never byte-identical; the documents, postings, and therefore every query
#: result are).
_WRITER_HEAP_BYTES = 64 * 1024 * 1024
_WRITER_THREADS = 1

#: How many ``chunk`` rows :func:`reindex` pulls out of SQLite at a time.
_REINDEX_BATCH = 2_000

#: ``{resolved_index_dir: (meta_stat_key, tantivy.Index)}`` -- opening an
#: index mmaps its files, so re-opening per query would throw away the very
#: cheapness this migration is for. The cache key includes the sidecar's
#: ``(mtime_ns, size)`` so any rebuild (which always rewrites the sidecar)
#: invalidates it automatically.
_INDEX_CACHE: dict[str, tuple[tuple[int, int], Any]] = {}


class TantivyUnavailableError(RuntimeError):
    """``tantivy`` (the ``tantivy-py`` binding) could not be imported. Only
    raised by paths that were EXPLICITLY asked for tantivy (the
    ``reindex-fulltext`` CLI, the doctor check's own probe); the serving
    path in :mod:`trialerror.retrieve.lexical` never raises -- it falls back
    to FTS5."""


class FulltextIndexNotReadyError(RuntimeError):
    """An index directory holds tantivy files but no valid readiness
    sidecar -- an interrupted rebuild. Raised only by
    :func:`create_fulltext_index` (the append path), never by the serving
    path, which treats the same state as "fall back to FTS5"."""


def tantivy_module():
    """The imported ``tantivy`` module, or ``None`` when the binding isn't
    installed. Deliberately not cached in a module global beyond
    ``sys.modules``' own caching, so a test that manipulates import state
    sees the truth."""
    try:
        import tantivy  # noqa: PLC0415 -- optional dependency, probed at call time
    except Exception:  # pragma: no cover - exercised via the fallback tests
        return None
    return tantivy


def tantivy_available() -> bool:
    return tantivy_module() is not None


def _require_tantivy():
    mod = tantivy_module()
    if mod is None:
        raise TantivyUnavailableError(
            "the 'tantivy' package (tantivy-py) is not importable in this environment; "
            "install it (pip install 'tantivy>=0.26,<0.27') or set trialerror.toml "
            "[retrieve] fulltext_backend = \"fts5\" to stay on the SQLite FTS5 tier"
        )
    return mod


# ---------------------------------------------------------------------------
# fingerprinting (see module docstring point 2)
# ---------------------------------------------------------------------------

_FP_BYTES = 16
_FP_ZERO = "0" * (_FP_BYTES * 2)


def _fold(ids: Iterable[str]) -> int:
    acc = 0
    for chunk_id in ids:
        acc ^= int.from_bytes(blake2b(chunk_id.encode("utf-8"), digest_size=_FP_BYTES).digest(), "big")
    return acc


def chunk_fingerprint(chunk_ids: Iterable[str], *, base: str | None = None) -> str:
    """An order-independent fingerprint of a SET of ``chunk_id``\\ s, as a
    32-char hex string. ``base`` folds the new ids into an existing
    fingerprint (what :meth:`FulltextIndex.add_chunks` does) -- since the
    fold is XOR, ``chunk_fingerprint(b, base=chunk_fingerprint(a))`` equals
    ``chunk_fingerprint(a + b)`` for disjoint ``a``/``b``."""
    acc = int(base, 16) if base else 0
    return format(acc ^ _fold(chunk_ids), f"0{_FP_BYTES * 2}x")


def corpus_fingerprint(conn) -> tuple[int, str]:
    """``(chunk_count, fingerprint)`` for the whole ``chunk`` table -- one
    unordered single-column scan, no text read, no sort. This is the
    "fast fingerprint" the ``fulltext_index_stale`` doctor check compares
    against the index's sidecar."""
    count = 0
    acc = 0
    for (chunk_id,) in conn.execute("SELECT chunk_id FROM chunk"):
        count += 1
        acc ^= int.from_bytes(blake2b(chunk_id.encode("utf-8"), digest_size=_FP_BYTES).digest(), "big")
    return count, format(acc, f"0{_FP_BYTES * 2}x")


def iter_chunk_rows(conn, *, batch_size: int = _REINDEX_BATCH) -> Iterable[list[tuple[str, str]]]:
    """Stream ``(chunk_id, text)`` batches out of ``knowledge.db`` in
    ``chunk_id`` order -- ordered so a rebuild assigns tantivy DocIds
    deterministically (DocIds are the final tie-break in a BM25 top-k, so
    an unordered rebuild could reorder tied hits between two rebuilds of
    the same corpus)."""
    cur = conn.execute("SELECT chunk_id, text FROM chunk ORDER BY chunk_id")
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            return
        yield [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------------------
# sidecar
# ---------------------------------------------------------------------------


def _meta_path(index_dir: Path) -> Path:
    return Path(index_dir) / META_FILENAME


def read_meta(index_dir: Path) -> dict[str, Any] | None:
    """The readiness sidecar, or ``None`` when it is absent, unreadable,
    malformed, or written by a different :data:`SCHEMA_VERSION`/analyzer
    version -- every one of which means "do not query this directory"."""
    path = _meta_path(index_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != SCHEMA_VERSION or raw.get("analyzer_version") != ANALYZER_VERSION:
        return None
    return raw


def _write_meta(index_dir: Path, *, chunk_count: int, fingerprint: str) -> dict[str, Any]:
    meta = {
        "schema_version": SCHEMA_VERSION,
        "analyzer": ANALYZER_NAME,
        "analyzer_version": ANALYZER_VERSION,
        "chunk_count": int(chunk_count),
        "chunk_fingerprint": fingerprint,
        "updated_ts": now(),
    }
    path = _meta_path(index_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # written whole, then renamed: a torn sidecar reads as "not ready"
    # anyway (read_meta returns None on a JSON error), but an atomic
    # replace means a reader never even sees the torn state.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return meta


def _clear_meta(index_dir: Path) -> None:
    _meta_path(index_dir).unlink(missing_ok=True)


def _meta_stat_key(index_dir: Path) -> tuple[int, int] | None:
    try:
        st = _meta_path(index_dir).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# ---------------------------------------------------------------------------
# the index handle
# ---------------------------------------------------------------------------


def _build_analyzer(tantivy):
    """``simple -> lowercase -> ascii_fold -> snowball(english)`` -- the
    closest available mirror of FTS5's ``porter unicode61`` (see the module
    docstring's "Analyzer parity" note for the one gap)."""
    return (
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
        .filter(tantivy.Filter.lowercase())
        .filter(tantivy.Filter.ascii_fold())
        .filter(tantivy.Filter.stemmer("english"))
        .build()
    )


def _build_schema(tantivy):
    builder = tantivy.SchemaBuilder()
    # chunk_id: the primary key. ``raw`` so the stored string round-trips
    # unchanged, ``basic`` because it is only ever matched as a whole term
    # (the allowlist TermSetQuery / the idempotency probe), never scored.
    builder.add_text_field("chunk_id", stored=True, tokenizer_name="raw", index_option="basic")
    # text: indexed, NOT stored. Nothing in the serving path reads chunk
    # text back out of tantivy (``trialerror.retrieve.engine`` re-reads it from
    # knowledge.db, which is the source of truth and carries the license
    # tier the fence needs) -- so storing it would duplicate the corpus on
    # disk for nothing. ``position`` (not ``freq``) IS required, not merely
    # available: FTS5 quotes every whitespace token as its own literal
    # phrase (``fts_query_string``), which is an adjacency requirement
    # whenever a token itself tokenizes to more than one term
    # (``state-of-the-art``, an email address, ``U.S.A.``-style
    # abbreviations); a ``freq``-only field has no positions and cannot
    # serve that as a phrase query, so it silently widened to a same-chunk
    # conjunction instead (finding D-1, fixed in this schema version --
    # see :meth:`FulltextIndex.search`).
    builder.add_text_field("text", stored=False, tokenizer_name=ANALYZER_NAME, index_option="position")
    return builder.build()


class FulltextIndex:
    """A ready-to-query tantivy index over the program's ``chunk`` corpus.

    Obtain one with :func:`open_fulltext_index` (returns ``None`` when the
    directory is absent or not ready) or :func:`create_fulltext_index` (the
    write paths). Instances are cheap: the underlying ``tantivy.Index`` is
    process-cached per directory, and ``tantivy``'s own readers are
    point-in-time snapshots handed out per :meth:`search` call."""

    def __init__(self, index_dir: Path, index, tantivy):
        self.index_dir = Path(index_dir)
        self._index = index
        self._tantivy = tantivy
        self._schema = index.schema
        self._analyzer = _build_analyzer(tantivy)

    # -- reads ------------------------------------------------------------

    def analyze(self, text: str) -> list[str]:
        """The query text run through the SAME analyzer the indexed field
        uses -- the tokens a ``TermQuery`` must be built from."""
        return list(self._analyzer.analyze(text))

    def num_docs(self) -> int:
        return int(self._index.searcher().num_docs)

    def existing_chunk_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        """Which of ``chunk_ids`` this index already holds (one
        ``TermSetQuery``) -- the idempotency probe :meth:`add_chunks` uses."""
        ids = list(chunk_ids)
        if not ids:
            return set()
        tantivy = self._tantivy
        query = tantivy.Query.term_set_query(self._schema, "chunk_id", ids)
        searcher = self._index.searcher()
        hits = searcher.search(query, len(ids)).hits
        return {searcher.doc(addr).get_first("chunk_id") for _score, addr in hits}

    def search(
        self, query: str, *, limit: int, chunk_id_allowlist: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """The lexical tier, same contract as
        :func:`trialerror.retrieve.ftssearch.fts_search`: rows
        ``{chunk_id, bm25}`` ordered BEST FIRST, at most ``limit`` of them.

        Query construction mirrors ``fts_query_string`` PER WHITESPACE
        TOKEN (module docstring, "Query semantics"), not as one flat
        conjunction over every analyzed term: a token that analyzes to a
        single term is a ``TermQuery``; a token that analyzes to more than
        one (a hyphenated word, an email address, a ``U.S.A.``-style
        abbreviation) is a ``phrase_query`` at ``slop=0`` -- exact,
        in-order adjacency, the same thing FTS5's phrase-quoting of that
        token demands. Fixing this at the whitespace-token boundary (rather
        than the whole query) is what keeps ``"two words"`` an AND of two
        independent terms exactly as before, while ``"state-of-the-art"``
        stops matching chunks where those words merely co-occur out of
        order (finding D-1).

        ``bm25`` carries FTS5's sign convention (lower is better) -- SQLite's
        ``bm25()`` returns the NEGATED BM25 score, so this returns
        ``-tantivy_score`` and the two backends' values are directly
        comparable rather than merely both "a score". Ties (equal score) are
        broken by ``chunk_id`` ascending -- the same secondary key
        ``fts_search`` now sorts by -- so that when ``limit`` truncates a
        tied block, both backends keep the SAME subset of it rather than an
        arbitrary one each (finding D-3); only the true 500-candidate-cap
        saturation case can still select different candidates, and that is
        documented separately (parity tolerance 2 / the migration note in
        ``docs/reviews/IMPL_lane-d-tantivy.md`` Section 4).

        An empty/whitespace query, a query whose every token the analyzer
        drops, or an EMPTY (not ``None``) allowlist all return ``[]`` --
        each the same short-circuit ``fts_search`` applies."""
        tokens = query.split()
        if not tokens:
            return []
        tantivy = self._tantivy
        clauses = []
        for token in tokens:
            terms = self.analyze(token)
            if not terms:
                continue
            if len(terms) == 1:
                clauses.append((tantivy.Occur.Must, tantivy.Query.term_query(self._schema, "text", terms[0])))
            else:
                clauses.append((tantivy.Occur.Must, tantivy.Query.phrase_query(self._schema, "text", terms)))
        if not clauses:
            return []
        if chunk_id_allowlist is not None:
            allow = list(chunk_id_allowlist)
            if not allow:
                return []
            # tantivy's TermSetQuery has no equivalent of SQLite's
            # 32,766-bound-variable ceiling, which is what made the FTS5
            # backend's `chunk_id IN (...)` allowlist a scale hazard
            # (BAKEOFF_REPORT.md Sec B.3 names the same ceiling for the
            # vector tier's IN-list).
            clauses.append((tantivy.Occur.Must, tantivy.Query.term_set_query(self._schema, "chunk_id", allow)))
        boolean = tantivy.Query.boolean_query(clauses)
        searcher = self._index.searcher()
        hits = searcher.search(boolean, max(int(limit), 0)).hits
        resolved = [(score, searcher.doc(addr).get_first("chunk_id")) for score, addr in hits]
        resolved.sort(key=lambda pair: (-pair[0], pair[1]))
        return [{"chunk_id": cid, "bm25": -float(score)} for score, cid in resolved]

    # -- writes -----------------------------------------------------------

    def add_chunks(self, rows: Iterable[tuple[str, str]]) -> dict[str, Any]:
        """Add ``(chunk_id, text)`` rows the index does not already hold,
        commit, and fold the newly-written ids into the sidecar
        fingerprint. Idempotent (see module docstring point 3); returns
        ``{"added": n, "skipped": m, "chunk_count": total}``."""
        offered = list(rows)
        if not offered:
            return {"added": 0, "skipped": 0, "chunk_count": self.num_docs()}
        already = self.existing_chunk_ids([cid for cid, _ in offered])
        pending = [(cid, text) for cid, text in offered if cid not in already]
        if not pending:
            return {"added": 0, "skipped": len(offered), "chunk_count": self.num_docs()}

        tantivy = self._tantivy
        writer = self._index.writer(heap_size=_WRITER_HEAP_BYTES, num_threads=_WRITER_THREADS)
        for chunk_id, text in pending:
            writer.add_document(tantivy.Document(chunk_id=[chunk_id], text=[text or ""]))
        writer.commit()
        writer.wait_merging_threads()
        self._index.reload()

        meta = read_meta(self.index_dir) or {}
        base = meta.get("chunk_fingerprint") if isinstance(meta.get("chunk_fingerprint"), str) else None
        fingerprint = chunk_fingerprint([cid for cid, _ in pending], base=base)
        total = self.num_docs()
        _write_meta(self.index_dir, chunk_count=total, fingerprint=fingerprint)
        # this handle IS the freshly-reloaded index, so re-key the cache to
        # the new sidecar stat instead of evicting (which would force the
        # next query to re-mmap for nothing).
        _recache(self.index_dir, self._index)
        return {"added": len(pending), "skipped": len(offered) - len(pending), "chunk_count": total}


def _cache_key(index_dir: Path) -> str:
    return str(Path(index_dir).resolve())


def _recache(index_dir: Path, index) -> None:
    key = _cache_key(index_dir)
    stat_key = _meta_stat_key(index_dir)
    if stat_key is None:
        _INDEX_CACHE.pop(key, None)
    else:
        _INDEX_CACHE[key] = (stat_key, index)


def _open_raw_index(index_dir: Path, tantivy):
    """Open (never create) the tantivy index at ``index_dir``, reusing the
    process cache when the sidecar hasn't changed since it was opened."""
    key = _cache_key(index_dir)
    stat_key = _meta_stat_key(index_dir)
    cached = _INDEX_CACHE.get(key)
    if cached is not None and stat_key is not None and cached[0] == stat_key:
        return cached[1]
    index = tantivy.Index.open(str(index_dir))
    index.register_tokenizer(ANALYZER_NAME, _build_analyzer(tantivy))
    if stat_key is not None:
        _INDEX_CACHE[key] = (stat_key, index)
    return index


def open_fulltext_index(index_dir: Path | str) -> FulltextIndex | None:
    """The read path's entry point. Returns ``None`` -- never raises --
    when tantivy isn't installed, the directory doesn't exist, it isn't a
    tantivy index, or its readiness sidecar is missing/stale-versioned
    (mid-rebuild, or built by an older :data:`SCHEMA_VERSION`). Every one
    of those is a "fall back to FTS5" signal, not an error: the lexical
    tier is a prefilter, and a program that has simply never run
    ``reindex-fulltext`` must still be able to search."""
    tantivy = tantivy_module()
    if tantivy is None:
        return None
    index_dir = Path(index_dir)
    if read_meta(index_dir) is None:
        return None
    try:
        if not tantivy.Index.exists(str(index_dir)):
            return None
        index = _open_raw_index(index_dir, tantivy)
    except Exception:
        # a corrupt/partially-written index directory is a fall-back
        # condition too -- doctor reports it, search still answers.
        return None
    return FulltextIndex(index_dir, index, tantivy)


def create_fulltext_index(index_dir: Path | str) -> FulltextIndex:
    """Open the index at ``index_dir``, creating an empty one (and its
    sidecar) if none exists. Write paths only.

    Raises :class:`TantivyUnavailableError` when tantivy isn't installed
    (a caller that reached here asked for tantivy explicitly), and
    :class:`FulltextIndexNotReadyError` when the directory holds index
    files but NO valid readiness sidecar. That combination means a rebuild
    died partway through, and the one thing this function must not do is
    bless the survivors: writing a fresh sidecar over them would mark a
    fraction of the corpus "ready" and the serving path would trust it.
    :func:`reindex` -- which starts by wiping -- is the way out, and the
    error says so."""
    tantivy = _require_tantivy()
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    if not tantivy.Index.exists(str(index_dir)):
        index = tantivy.Index(_build_schema(tantivy), path=str(index_dir))
        index.register_tokenizer(ANALYZER_NAME, _build_analyzer(tantivy))
        _write_meta(index_dir, chunk_count=0, fingerprint=_FP_ZERO)
    elif read_meta(index_dir) is None:
        raise FulltextIndexNotReadyError(
            f"the tantivy index at {index_dir} has no valid {META_FILENAME} readiness sidecar "
            "(a rebuild was interrupted, or it was built by an older schema version); rebuild it "
            "with `trialerror ingest reindex-fulltext` rather than appending to it"
        )
    else:
        index = _open_raw_index(index_dir, tantivy)
    _recache(index_dir, index)
    return FulltextIndex(index_dir, index, tantivy)


# ---------------------------------------------------------------------------
# maintenance entry points
# ---------------------------------------------------------------------------


def add_chunks(index_dir: Path | str, rows: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Incremental maintenance -- what ``trialerror.ingest.handlers.run_index``
    calls in the same code path that maintains ``chunk_fts`` today."""
    return create_fulltext_index(index_dir).add_chunks(rows)


def _rmtree_released(index_dir: Path) -> None:
    """``shutil.rmtree``, with one gc-then-retry.

    tantivy reads through ``mmap``, and Windows refuses to unlink a mapped
    file. Evicting :data:`_INDEX_CACHE` normally drops the last reference
    (CPython frees it there and then), but any live
    :class:`FulltextIndex` still held elsewhere in this process -- a
    ``TantivyBackend`` sitting in an in-flight search, say -- keeps the
    mapping alive. A collection releases those the moment nothing real
    references them; if something genuinely still does, the second failure
    is reported with the cause named rather than as a bare
    ``PermissionError`` on a path."""
    if not index_dir.exists():
        return
    try:
        shutil.rmtree(index_dir)
        return
    except OSError:
        gc.collect()
    try:
        shutil.rmtree(index_dir)
    except OSError as exc:
        raise OSError(
            f"could not clear the tantivy index directory {index_dir} for a rebuild: {exc}. "
            "On Windows this means the index files are still memory-mapped by a live reader in "
            "this process -- run `trialerror ingest reindex-fulltext` from its own CLI process, "
            "or stop the dashboard/MCP server holding the index, and retry."
        ) from exc


def reindex(conn, index_dir: Path | str, *, batch_size: int = _REINDEX_BATCH) -> dict[str, Any]:
    """Rebuild the whole index from ``knowledge.db``'s ``chunk`` table,
    from scratch. ``conn`` is any DB-API connection to knowledge.db (a
    ``Store.knowledge``, or a read-only ``trialerror.stores.connection.connect``
    handle -- this function never writes SQL).

    Crash-safety (module docstring point 1): the readiness sidecar is
    removed FIRST, the directory is wiped, documents are streamed in
    ``chunk_id`` order, ONE commit lands them all, and only then is the
    sidecar rewritten. A process killed anywhere in the middle leaves a
    directory every reader treats as not-ready (-> FTS5 fallback) and the
    doctor reports as stale; re-running this function is always the fix."""
    tantivy = _require_tantivy()
    index_dir = Path(index_dir)
    _clear_meta(index_dir)
    _INDEX_CACHE.pop(_cache_key(index_dir), None)
    _rmtree_released(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

    index = tantivy.Index(_build_schema(tantivy), path=str(index_dir))
    index.register_tokenizer(ANALYZER_NAME, _build_analyzer(tantivy))
    writer = index.writer(heap_size=_WRITER_HEAP_BYTES, num_threads=_WRITER_THREADS)

    total = 0
    acc = 0
    for batch in iter_chunk_rows(conn, batch_size=batch_size):
        for chunk_id, text in batch:
            writer.add_document(tantivy.Document(chunk_id=[chunk_id], text=[text or ""]))
            acc ^= int.from_bytes(blake2b(chunk_id.encode("utf-8"), digest_size=_FP_BYTES).digest(), "big")
            total += 1
    writer.commit()
    writer.wait_merging_threads()
    index.reload()

    fingerprint = format(acc, f"0{_FP_BYTES * 2}x")
    _write_meta(index_dir, chunk_count=total, fingerprint=fingerprint)
    _recache(index_dir, index)
    return {
        "index_dir": str(index_dir),
        "chunks_indexed": total,
        "indexed_docs": int(index.searcher().num_docs),
        "chunk_fingerprint": fingerprint,
        "schema_version": SCHEMA_VERSION,
        "analyzer": ANALYZER_NAME,
    }


def index_status(conn, index_dir: Path | str, *, cheap: bool = False) -> dict[str, Any]:
    """What the ``fulltext_index_stale`` doctor check reports on. Compares
    three numbers rather than two, so a sidecar that has drifted from the
    index it describes (a crash between ``commit()`` and the sidecar write)
    is caught as well as an index that has drifted from the corpus:

    - ``db_chunks``/``db_fingerprint`` -- the corpus (source of truth),
    - ``meta_chunks``/``meta_fingerprint`` -- what the sidecar claims,
    - ``index_docs`` -- what the tantivy index actually holds.

    ``state`` is one of ``"ok"``, ``"missing"`` (never built / mid-rebuild
    / stale schema version), ``"stale"`` (any of the three disagree), or
    ``"unavailable"`` (tantivy-py not installed).

    ``cheap=True`` (fix pass, finding LD-04): skip :func:`corpus_fingerprint`
    -- an unordered ``SELECT chunk_id FROM chunk`` over the WHOLE corpus,
    with a BLAKE2b digest per row -- in favor of a plain indexed
    ``COUNT(*)``, and judge ``"ok"``/``"stale"`` on chunk COUNTS agreeing
    rather than fingerprints. This is what ``engine.corpus_stats`` (an
    on-demand summary call -- ``trialerror query stats`` / MCP tool #8, not a
    per-page-load caller, but one whose whole job is a cheap summary) asks
    for: at multi-million-chunk scale a "how fresh is the index" line
    should not itself become a full-table scan. The trade is real and
    disclosed, not hidden -- a same-count/different-content drift (a chunk
    replaced without changing the corpus size) is invisible to ``cheap``
    mode and visible to the default one, which is exactly why the DOCTOR's
    ``fulltext_index_stale`` check always calls this with ``cheap=False``.
    ``db_fingerprint``/``meta_fingerprint`` in the returned dict are ``None``
    in cheap mode (never computed, so never compared)."""
    index_dir = Path(index_dir)
    if cheap:
        db_chunks = int(conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])
        db_fingerprint = None
    else:
        db_chunks, db_fingerprint = corpus_fingerprint(conn)
    out: dict[str, Any] = {
        "index_dir": str(index_dir),
        "db_chunks": db_chunks,
        "db_fingerprint": db_fingerprint,
        "meta_chunks": None,
        "meta_fingerprint": None,
        "index_docs": None,
        "schema_version": SCHEMA_VERSION,
    }
    if not tantivy_available():
        out["state"] = "unavailable"
        return out
    meta = read_meta(index_dir)
    if meta is None:
        out["state"] = "missing"
        return out
    out["meta_chunks"] = meta.get("chunk_count")
    out["meta_fingerprint"] = meta.get("chunk_fingerprint")
    handle = open_fulltext_index(index_dir)
    if handle is None:
        out["state"] = "missing"
        return out
    out["index_docs"] = handle.num_docs()
    if cheap:
        agrees = out["meta_chunks"] == db_chunks and out["index_docs"] == db_chunks
    else:
        agrees = (
            out["meta_fingerprint"] == db_fingerprint
            and out["meta_chunks"] == db_chunks
            and out["index_docs"] == db_chunks
        )
    out["state"] = "ok" if agrees else "stale"
    return out
