"""The lexical (keyword/BM25) tier's backend seam. Design Section 7
pipeline step 1 has always been "FTS5/BM25 prefilter to <=500 candidates";
``DESIGN_v0.md`` D2 pre-scoped its replacement -- "the fusion layer is
backend-agnostic so swapping in tantivy later is config + an indexer".
This module is that ``config``: one :class:`LexicalBackend` protocol, two
implementations, and the resolution rules that pick between them.

``trialerror.retrieve.engine`` calls :func:`lexical_search` and gets back the
SAME ``[{chunk_id, bm25}, ...]`` best-first rows ``fts_search`` always
returned, plus the name of the backend that actually produced them (which
the engine surfaces as ``SearchResponse.stats.fulltext_backend`` -- design
Section 7's "engine reports what it used", applied one level deeper than
tiers).

**Resolution order** (:func:`resolve_backend`):

1. ``trialerror.toml`` ``[retrieve] fulltext_backend`` -- ``"tantivy"`` or
   ``"fts5"``. An unrecognized value is treated as unset (and noted once),
   never as a hard failure: a typo in a config file must not take search
   down.
2. Unset -> ``"tantivy"`` (:data:`DEFAULT_FULLTEXT_BACKEND`), the C-0080
   default of record.
3. ``"tantivy"`` resolved but ``tantivy-py`` is not importable -> ``fts5``,
   noted once on stderr.
4. ``"tantivy"`` resolved, importable, but this program has no READY index
   (never built, mid-rebuild, or built by an older schema version -- see
   :func:`trialerror.retrieve.tantivysearch.open_fulltext_index`) -> ``fts5``,
   noted once on stderr. A READY index whose document count no longer
   matches ``knowledge.db``'s chunk count (fix pass, finding D-8 -- e.g. a
   :func:`maintain_index` call that hit the D-4 failure path) is treated
   the same way: one cheap ``COUNT(*)`` -- not the doctor's full
   fingerprint scan -- closes the window between a drift and the next
   ``doctor`` run rather than leaving it unbounded.

Rules 3 and 4 are why the switch is safe to merge ahead of any operator
action: an existing program keeps answering searches out of ``chunk_fts``
exactly as before until someone runs ``trialerror ingest reindex-fulltext``,
and ``doctor``'s ``fulltext_index_stale`` check is what tells them to. The
engine NEVER crashes over a missing optional dependency or a missing index
-- the same posture ``trialerror.stores.vecindex`` takes for ``sqlite-vec``.

**"Noted once"**: this codebase has no logging framework (deliberately --
its surfaces are CLI JSON envelopes, doctor checks, and the events ledger),
so a degradation that is neither an error nor a per-call event is written
to stderr exactly once per process by :func:`note_once`, in the same
``[trialerror.<area>]``-prefixed style ``trialerror.dashboard.serve`` uses.
Tests read :func:`_notes_emitted` rather than capturing stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from trialerror.retrieve.ftssearch import DEFAULT_FTS_CANDIDATE_LIMIT, fts_search
from trialerror.retrieve import tantivysearch

__all__ = [
    "FULLTEXT_BACKENDS",
    "DEFAULT_FULLTEXT_BACKEND",
    "LexicalBackend",
    "Fts5Backend",
    "TantivyBackend",
    "configured_backend_name",
    "resolve_backend",
    "lexical_search",
    "maintain_index",
    "fulltext_index_dir",
    "note_once",
]

#: Every value ``[retrieve] fulltext_backend`` may take.
FULLTEXT_BACKENDS: tuple[str, ...] = ("tantivy", "fts5")

#: C-0080 / BAKEOFF_REPORT.md Sec A.3's verdict of record, as the
#: zero-config default. Degrades to ``"fts5"`` per rules 3/4 above.
DEFAULT_FULLTEXT_BACKEND = "tantivy"


# ---------------------------------------------------------------------------
# one-shot degradation notes
# ---------------------------------------------------------------------------

_NOTED: set[str] = set()


def note_once(key: str, message: str) -> bool:
    """Write ``message`` to stderr the first time ``key`` is seen in this
    process; return whether it was actually written."""
    if key in _NOTED:
        return False
    _NOTED.add(key)
    print(f"[trialerror.retrieve] {message}", file=sys.stderr)
    return True


def _notes_emitted() -> frozenset[str]:
    """Test seam: which :func:`note_once` keys have fired this process."""
    return frozenset(_NOTED)


def _reset_notes() -> None:
    """Test seam: forget every emitted note (a fresh-process simulation)."""
    _NOTED.clear()


# ---------------------------------------------------------------------------
# the protocol + the two backends
# ---------------------------------------------------------------------------


@runtime_checkable
class LexicalBackend(Protocol):
    """What ``trialerror.retrieve.engine``'s step-1 prefilter needs, and
    nothing more. ``search`` returns rows ``{chunk_id, bm25}`` ordered
    best-first, at most ``limit`` of them, where ``bm25`` follows SQLite's
    sign convention (LOWER is better) on every backend. An empty query, a
    query that analyzes to no terms, and an EMPTY (not ``None``)
    ``chunk_id_allowlist`` each return ``[]``; ``None`` means "no
    allowlist, search the whole corpus"."""

    name: str

    def search(
        self,
        store: Any,
        query: str,
        *,
        limit: int = DEFAULT_FTS_CANDIDATE_LIMIT,
        chunk_id_allowlist: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]: ...


class Fts5Backend:
    """The incumbent: SQLite FTS5's ``chunk_fts`` virtual table, unchanged
    (:mod:`trialerror.retrieve.ftssearch`). Kept as the permanent fallback,
    not as dead code -- rules 3/4 in the module docstring both land here,
    and it is the only backend that works with nothing but stdlib SQLite."""

    name = "fts5"

    def search(
        self,
        store: Any,
        query: str,
        *,
        limit: int = DEFAULT_FTS_CANDIDATE_LIMIT,
        chunk_id_allowlist: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return fts_search(store, query, limit=limit, chunk_id_allowlist=chunk_id_allowlist)


class TantivyBackend:
    """The C-0080 default: an embedded tantivy index under the program root
    (:mod:`trialerror.retrieve.tantivysearch`). Holds an already-opened,
    already-verified-ready :class:`~trialerror.retrieve.tantivysearch.FulltextIndex`
    -- :func:`resolve_backend` is the only thing that constructs one, and it
    only does so once readiness has been established, so this class never
    needs a "maybe the index isn't there" branch of its own."""

    name = "tantivy"

    def __init__(self, index: tantivysearch.FulltextIndex):
        self.index = index

    def search(
        self,
        store: Any,
        query: str,
        *,
        limit: int = DEFAULT_FTS_CANDIDATE_LIMIT,
        chunk_id_allowlist: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.index.search(query, limit=limit, chunk_id_allowlist=chunk_id_allowlist)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _program_config(store: Any) -> dict[str, Any]:
    """``trialerror.toml``'s raw dict for this store's program, or ``{}``.
    Mirrors ``trialerror.retrieve.engine._load_program_config`` exactly
    (same best-effort, never-raise posture); duplicated rather than
    imported to keep the import edge one-way (engine -> lexical)."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    program_root = getattr(store, "program_root", None)
    if program_root is None:
        return {}
    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


def configured_backend_name(config: Mapping[str, Any] | None) -> str:
    """``[retrieve] fulltext_backend`` normalized, or
    :data:`DEFAULT_FULLTEXT_BACKEND`. An unrecognized value is noted once
    and treated as unset (module docstring rule 1)."""
    raw = (config or {}).get("retrieve", {})
    if not isinstance(raw, Mapping):
        return DEFAULT_FULLTEXT_BACKEND
    value = raw.get("fulltext_backend")
    if value is None:
        return DEFAULT_FULLTEXT_BACKEND
    name = str(value).strip().lower()
    if name not in FULLTEXT_BACKENDS:
        note_once(
            f"bad_backend:{name}",
            f"trialerror.toml [retrieve] fulltext_backend={value!r} is not one of "
            f"{FULLTEXT_BACKENDS!r}; using {DEFAULT_FULLTEXT_BACKEND!r}",
        )
        return DEFAULT_FULLTEXT_BACKEND
    return name


def _cheap_chunk_count(store: Any) -> int | None:
    """A single indexed ``SELECT COUNT(*) FROM chunk`` -- NOT the full XOR
    fingerprint scan :func:`trialerror.retrieve.tantivysearch.corpus_fingerprint`
    does. Returns ``None`` (never raises) when ``store`` exposes no
    ``knowledge`` connection or the query fails for any reason -- this is a
    best-effort guard, not a source of truth."""
    conn = getattr(store, "knowledge", None)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT COUNT(*) FROM chunk").fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def fulltext_index_dir(store: Any, config: Mapping[str, Any] | None = None) -> Path | None:
    """Where THIS store's tantivy index lives, or ``None`` for a store with
    no ``program_root`` (the doctor's knowledge-only shim, for one)."""
    from trialerror.stores import paths

    program_root = getattr(store, "program_root", None)
    if program_root is None:
        return None
    cfg = dict(config) if config is not None else _program_config(store)
    return paths.fulltext_index_path(Path(program_root), cfg)


def resolve_backend(store: Any, *, config: Mapping[str, Any] | None = None) -> LexicalBackend:
    """Pick the lexical backend for ``store``, applying the module
    docstring's four rules. Never raises; always returns something that can
    answer a query."""
    cfg = dict(config) if config is not None else _program_config(store)
    wanted = configured_backend_name(cfg)
    if wanted == "fts5":
        return Fts5Backend()

    if not tantivysearch.tantivy_available():
        note_once(
            "tantivy_missing",
            "fulltext_backend=tantivy but the 'tantivy' package is not importable; "
            "falling back to the SQLite FTS5 tier (pip install 'tantivy>=0.26,<0.27' to enable it)",
        )
        return Fts5Backend()

    index_dir = fulltext_index_dir(store, cfg)
    if index_dir is None:
        note_once(
            "tantivy_no_program_root",
            "fulltext_backend=tantivy but this store exposes no program_root to locate the "
            "index under; falling back to the SQLite FTS5 tier",
        )
        return Fts5Backend()

    handle = tantivysearch.open_fulltext_index(index_dir)
    if handle is None:
        note_once(
            f"tantivy_index_not_ready:{index_dir}",
            f"fulltext_backend=tantivy but no ready tantivy index at {index_dir} "
            "(never built, mid-rebuild, or built by an older schema version); falling back to "
            "the SQLite FTS5 tier -- run `trialerror ingest reindex-fulltext` to enable it",
        )
        return Fts5Backend()

    # Fix pass, finding D-8: a valid readiness sidecar only means "built by
    # this schema version and not mid-rebuild" -- it says nothing about
    # whether the corpus has moved since the last successful add/rebuild
    # (e.g. `maintain_index` hit the D-4 failure path). Between such a drift
    # and the next `doctor` run there was previously NO guard on the
    # serving path at all -- an unbounded silent-under-recall window. One
    # indexed COUNT(*) closes it at roughly zero cost; the full XOR
    # fingerprint (which also catches same-count content drift) stays the
    # doctor's job.
    db_chunks = _cheap_chunk_count(store)
    index_docs = handle.num_docs()
    if db_chunks is not None and db_chunks != index_docs:
        note_once(
            f"tantivy_index_chunk_count_mismatch:{index_dir}",
            f"fulltext_backend=tantivy but the index at {index_dir} holds {index_docs} doc(s) while "
            f"knowledge.db has {db_chunks} chunk(s); falling back to the SQLite FTS5 tier until "
            "`trialerror ingest reindex-fulltext` repairs it",
        )
        return Fts5Backend()

    return TantivyBackend(handle)


def maintain_index(
    store: Any, rows: Sequence[tuple[str, str]], *, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Keep the tantivy index in step with newly-written ``chunk`` rows --
    called from ``trialerror.ingest.handlers.run_index``, in the same code
    path that maintains ``chunk_fts``. ``rows`` are ``(chunk_id, text)``
    pairs that are ALREADY committed to ``knowledge.db``.

    Three outcomes, reported as ``action``:

    - ``"skip"`` -- the program is on ``fulltext_backend = "fts5"``, or
      tantivy-py isn't installed, or this store has no ``program_root``.
      Nothing to do; ``chunk_fts`` alone still serves every search.
    - ``"add"`` -- a READY index exists; the rows it doesn't already hold
      are appended and committed (idempotent, so a re-run of the ``index``
      stage after a kill adds nothing).
    - ``"rebuild"`` -- no ready index exists yet, so the whole index is
      built from ``knowledge.db``. This is the case that makes the switch
      self-healing on an EXISTING program: incrementally appending only the
      document being ingested would leave a *ready-looking* index covering
      a fraction of the corpus, which the serving path would then trust and
      quietly under-recall from. Rebuilding instead is correct by
      construction, costs a one-time full pass (bake-off: ~5,300 chunks/s),
      and happens exactly once -- every later ingest takes the ``"add"``
      path. An operator who would rather control when that pass happens
      runs ``trialerror ingest reindex-fulltext`` first, or pins
      ``fulltext_backend = "fts5"``.
    - ``"failed"`` -- maintaining the index raised (fix pass, finding D-4):
      a stray file where the index directory must go, a second tantivy
      writer already holding the index's lock (a concurrent
      ``reindex-fulltext``, or another worker process -- both are designed
      scenarios, not edge cases), or anything else. Noted once on stderr
      and returned as data, NEVER re-raised: ``chunk_fts``/``knowledge.db``
      have already committed by the time this runs, so a cache that failed
      to update is exactly the "derived state, never truth" skew
      ``fulltext_index_stale`` exists to catch and ``reindex-fulltext``
      exists to repair -- it must never fail the ``index`` ingest stage
      itself.
    """
    cfg = dict(config) if config is not None else _program_config(store)
    result: dict[str, Any] = {"backend": configured_backend_name(cfg), "action": "skip", "added": 0}
    if result["backend"] != "tantivy" or not tantivysearch.tantivy_available():
        return result
    index_dir = fulltext_index_dir(store, cfg)
    if index_dir is None:
        return result

    try:
        if tantivysearch.open_fulltext_index(index_dir) is not None:
            try:
                outcome = tantivysearch.add_chunks(index_dir, rows)
            except tantivysearch.FulltextIndexNotReadyError:
                # the sidecar vanished between the readiness probe and the
                # append (a concurrent rebuild, or a kill during one) -- fall
                # through to the rebuild rather than surfacing a race as a
                # failed ingest job.
                pass
            else:
                result.update({"action": "add", "added": outcome["added"], "chunk_count": outcome["chunk_count"]})
                return result

        note_once(
            f"tantivy_first_build:{index_dir}",
            f"no tantivy full-text index at {index_dir} yet -- building it once from knowledge.db "
            "(later ingests append incrementally)",
        )
        outcome = tantivysearch.reindex(store.knowledge, index_dir)
        result.update({"action": "rebuild", "added": outcome["chunks_indexed"], "chunk_count": outcome["chunks_indexed"]})
        return result
    except Exception as exc:  # noqa: BLE001 -- an ingest job must never fail over a cache
        note_once(
            f"tantivy_maintain_failed:{index_dir}",
            f"failed to maintain the tantivy full-text index at {index_dir}: {exc} -- knowledge.db writes "
            "already committed and search still answers (from FTS5, or a stale-but-usable tantivy index); "
            "`trialerror doctor --only fulltext_index_stale` will report the resulting skew, and "
            "`trialerror ingest reindex-fulltext` repairs it",
        )
        result.update({"action": "failed", "error": str(exc)})
        return result


def lexical_search(
    store: Any,
    query: str,
    *,
    limit: int = DEFAULT_FTS_CANDIDATE_LIMIT,
    chunk_id_allowlist: Sequence[str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Run the lexical tier through whichever backend
    :func:`resolve_backend` picks. Returns ``(rows, backend_name)``."""
    backend = resolve_backend(store, config=config)
    return backend.search(store, query, limit=limit, chunk_id_allowlist=chunk_id_allowlist), backend.name
