"""Rebuild one embedding model key's vector index from the ``emb`` rows.

**The failure this exists for** (live, measured 2026-09-10). A program's
``knowledge.db`` held 16,173 chunks and 16,088 ``emb`` rows under the real
embedding key, and 2,173 entries in that key's ``vec_chunks__*`` table.
Semantic retrieval was answering from 2,173 of 16,088 vectors and no check
said so: ``embedding_missing`` and ``embedding_stale`` both read ``emb``
rows, and nothing compared the INDEX against them. The cause was the
``index`` stage's job id
(:func:`trialerror.ingest.handlers.index_job_id` -- it did not name the
model key, so the embed stage's hand-off for a NEW key looked like a
resumed hand-off for the old one and was dropped); that defect is fixed at
the writer, and this verb is how a corpus that already has the hole gets out
of it without re-running a GPU.

**Why a rebuild is safe where a purge needs ceremony.** "indexes are cache,
never truth" (design Section 6 stage 7): every entry this verb writes is
derived from an ``emb`` row that stays exactly where it was, so the worst a
botched run can do is cost an operator a second run. It still takes a
``--launch-id``, for one reason that is not symmetry: it REPLACES the live
search surface for a key, and a program whose semantic retrieval changed
shape has to be able to say which launch changed it. ``reindex-fulltext``
takes no launch because it rebuilds a file-backed index beside the database;
this one writes rows into the record.

**Delete-and-refill in one transaction, never drop-and-create.** The same
reasoning ``trialerror.ingest.purge`` spells out for its own vector work:
``vec_chunks__<model_key>`` is schema created on demand by
:func:`trialerror.stores.vecindex.ensure_vec_table` and its ROWS are the
data. One ``BEGIN`` around the delete and every insert means a crash
mid-rebuild leaves the OLD index -- partial, already flagged by the
``vector_index_stale`` check, and still answering -- rather than an empty
table that answers nothing and looks healthy to a reader that only counts
tables.

**What it refuses.** An unregistered ``--launch-id`` (the L-E4 pre-flight
``retract``/``purge-embeddings`` use, before a row is touched); a key this
program has never embedded or indexed under; a key whose ``emb`` rows
disagree about ``dims`` (one fixed-width table cannot hold both, and
guessing which is right is not this verb's call); and a key whose existing
vector table cannot be read on this connection (a ``vec0`` table with the
sqlite-vec extension absent -- the rebuild would fail halfway through the
delete).
"""

from __future__ import annotations

from typing import Any

from trialerror.ingest.errors import (
    UnknownEmbedModelKeyError,
    VectorIndexDimsConflictError,
    VectorIndexUnreadableError,
)
from trialerror.stores.store import Store
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, vec_table_name
from trialerror.stores.writer import require_xid_targets
from trialerror.util.timeutil import now

__all__ = [
    "REINDEX_EVENT_TYPE",
    "UnknownEmbedModelKeyError",
    "VectorIndexDimsConflictError",
    "VectorIndexUnreadableError",
    "reindex_vectors",
]

#: The one event this verb writes -- same ``"type"``-keyed shape as
#: ``embeddings_purged``: the counts, the key, the launch.
REINDEX_EVENT_TYPE = "vectors_reindexed"

#: Rows per ``executemany`` page. The refill runs inside ONE transaction, so
#: this bounds only how much of the cursor is held in memory at a time; the
#: live rebuild is ~16,000 rows of 2048 float32s (8 KB each), which is why
#: the whole result set is deliberately not materialized.
_INSERT_PAGE = 1000


def _table_exists(conn, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _registry_row(conn, model_key: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "vec_index_registry"):
        return None
    row = conn.execute(
        "SELECT * FROM vec_index_registry WHERE model_key = ?", (model_key,)
    ).fetchone()
    return dict(row) if row is not None else None


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _probe_readable(conn, table: str) -> int:
    """Row count, or a named refusal. Probed BEFORE anything is written for
    the same reason ``purge``'s probe is: a ``vec0`` table on a connection
    without the extension fails on the DELETE, and a rebuild that cannot
    delete must not have started."""
    try:
        return _count(conn, table)
    except Exception as exc:  # noqa: BLE001 - reported as a named refusal
        raise VectorIndexUnreadableError(
            f"the vector index table {table!r} exists but cannot be read ({exc}) -- refusing to "
            "rebuild an index this connection cannot empty first. This is a sqlite-vec program: "
            "install the extension, or run against a program whose table is a plain one."
        ) from exc


def _emb_dims(conn, model_key: str) -> int | None:
    """The single ``dims`` value this key's ``emb`` rows carry, or ``None``
    when it has no rows. Raises when they disagree."""
    rows = conn.execute(
        "SELECT DISTINCT dims FROM emb WHERE model_key = ? ORDER BY dims", (model_key,)
    ).fetchall()
    dims = [int(r["dims"]) for r in rows]
    if not dims:
        return None
    if len(dims) > 1:
        raise VectorIndexDimsConflictError(
            f"the {len(dims)} distinct dims values {dims} appear on emb rows for model_key "
            f"{model_key!r} -- one vector table holds one width, so there is no index to build "
            "here. Purge the superseded width's rows (trialerror ingest purge-embeddings) or "
            "re-embed the corpus under one configuration, then reindex."
        )
    return dims[0]


def reindex_vectors(
    store: Store,
    *,
    model_key: str,
    launch_id: str,
    dry_run: bool = False,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild ``vec_chunks__<model_key>`` from the ``emb`` rows of
    ``model_key``, one entry per chunk whose ``sha256`` that key has embedded.

    Returns ``{"model_key", "emb_rows", "vec_rows_before", "vec_rows_after",
    "dry_run"}``. ``emb_rows`` counts the key's ``emb`` rows (hash-addressed,
    so two chunks sharing text share one); ``vec_rows_after`` counts index
    entries (chunk-addressed), which is why the two numbers can legitimately
    differ -- see :func:`trialerror.ingest.checks._vector_index_gap`.

    Idempotent: a second run rebuilds the same entries and reports
    ``vec_rows_before == vec_rows_after``. ``dry_run`` reports the counts the
    real run would produce (``vec_rows_after`` being the projection) and
    writes nothing at all -- no table, no registry row, no event.

    Raises :class:`~trialerror.stores.errors.XidTargetMissingError` for a
    launch that names no ``platform.launch`` row,
    :class:`~trialerror.ingest.errors.UnknownEmbedModelKeyError` for a key
    this program has neither embedded nor registered,
    :class:`~trialerror.ingest.errors.VectorIndexDimsConflictError` when the
    key's rows disagree about width, and
    :class:`~trialerror.ingest.errors.VectorIndexUnreadableError` when an
    existing table cannot be read here. Each refuses before any write.

    ``config`` is the program's ``trialerror.toml``, needed only so the
    resident similarity matrix this rebuild invalidates is looked for where
    ``[paths] index_dir`` actually puts it -- the same parameter, for the same
    reason, as :func:`trialerror.ingest.purge.purge_embeddings`. Omitting it
    costs correctness nothing (the per-query fingerprint check rebuilds a
    cache of the old table either way) and costs one rebuild.
    """
    # The destructive-act pre-flight, first: an unattributable rebuild of the
    # live search surface is refused before the store is read.
    require_xid_targets(store, "event", {"launch_id": launch_id})

    conn = store.knowledge

    # A vec0 virtual table is unreadable on a connection that has not loaded
    # the extension; the default backend is a plain table, but a program
    # running TRIALERROR_VEC_BACKEND=sqlite_vec needs this.
    from trialerror.stores.vecindex import try_load_sqlite_vec

    try_load_sqlite_vec(conn)

    table = vec_table_name(model_key)
    registry = _registry_row(conn, model_key)
    emb_rows = int(
        conn.execute("SELECT COUNT(*) AS n FROM emb WHERE model_key = ?", (model_key,)).fetchone()["n"]
    )
    if emb_rows == 0 and registry is None:
        known = [
            r["model_key"]
            for r in conn.execute("SELECT DISTINCT model_key FROM emb ORDER BY model_key").fetchall()
        ]
        raise UnknownEmbedModelKeyError(
            f"no embedding rows and no vector-index registry row for model_key {model_key!r} -- "
            f"this program has never embedded or indexed under that key. Keys it does carry: "
            f"{known or 'none'}."
        )

    dims = _emb_dims(conn, model_key)
    if dims is None:
        dims = int(registry["dims"]) if registry is not None else 0

    existed = _table_exists(conn, table)
    vec_rows_before = _probe_readable(conn, table) if existed else 0

    # The projection the dry run reports and the real run then produces: one
    # entry per CHUNK this key has an embedding for.
    projected = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM chunk c
            WHERE EXISTS (SELECT 1 FROM emb e WHERE e.chunk_sha256 = c.sha256 AND e.model_key = ?)
            """,
            (model_key,),
        ).fetchone()["n"]
    )

    if dry_run:
        return {
            "model_key": model_key,
            "emb_rows": emb_rows,
            "vec_rows_before": vec_rows_before,
            "vec_rows_after": projected,
            "dry_run": True,
        }

    ts = now()
    # Schema first, outside the data transaction: ``ensure_vec_table`` commits
    # its own registry write (``with conn:``), so calling it from inside the
    # refill would end that transaction early -- the exact trap
    # ``events.api.append_event_in_txn``'s docstring names.
    declared_backend = ensure_vec_table(conn, model_key, dims)

    # What the table on disk actually IS, which is not always what
    # ``ensure_vec_table`` would have created: ``CREATE TABLE IF NOT EXISTS``
    # is a no-op on an existing table, so a program whose table was built
    # under the other backend keeps its shape. The fallback table carries a
    # ``model_key`` column and a ``vec0`` one does not -- the same
    # discrimination ``purge._vec_table_for`` makes, and the insert shape
    # follows the table rather than the configuration.
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    backend = VecBackend.FALLBACK if "model_key" in columns else VecBackend.SQLITE_VEC
    if backend != declared_backend:
        # Leave the bookkeeping truthful rather than let the registry claim a
        # backend the table contradicts.
        with conn:
            conn.execute(
                "UPDATE vec_index_registry SET backend = ? WHERE model_key = ?",
                (backend.value, model_key),
            )

    source = conn.execute(
        """
        SELECT c.chunk_id AS chunk_id, e.dims AS dims, e.vector AS vector
        FROM chunk c
        JOIN emb e ON e.chunk_sha256 = c.sha256 AND e.model_key = ?
        ORDER BY c.chunk_id
        """,
        (model_key,),
    )
    if backend == VecBackend.SQLITE_VEC:
        insert_sql = f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)"

        def _params(row):
            return (row["chunk_id"], row["vector"])
    else:
        insert_sql = f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)"

        def _params(row):
            return (row["chunk_id"], model_key, int(row["dims"]), row["vector"])

    # ONE transaction: the delete and every insert land together, so an
    # interrupted rebuild leaves the old (partial) index rather than none.
    with conn:
        conn.execute(f"DELETE FROM {table}")
        while True:
            page = source.fetchmany(_INSERT_PAGE)
            if not page:
                break
            conn.executemany(insert_sql, [_params(r) for r in page])

    vec_rows_after = _count(conn, table)

    # Lane F-1 item E: every row of this key's vector table was just
    # replaced, so its resident similarity matrix
    # (:mod:`trialerror.retrieve.vecmatrix`) is a cache of the table that used
    # to be there. Invalidated explicitly rather than left to the
    # fingerprint check, for the same reason this verb writes an event even
    # when no count moved: the live search surface changed here.
    from trialerror.retrieve.vecmatrix import invalidate as invalidate_vecmatrix

    invalidate_vecmatrix(store.program_root, model_key, config)

    result = {
        "model_key": model_key,
        "emb_rows": emb_rows,
        "vec_rows_before": vec_rows_before,
        "vec_rows_after": vec_rows_after,
        "dry_run": False,
    }

    # Always, on a real run -- including a run that changed no count. Unlike
    # ``purge``'s "no event for a purge that removed nothing", the rebuild
    # REPLACED the live search surface for this key whether or not the
    # arithmetic moved, and that is the act the record has to carry.
    from trialerror.events.api import append_event

    append_event(
        store,
        event_type=REINDEX_EVENT_TYPE,
        payload={
            "model_key": model_key,
            "vec_table": table,
            "dims": dims,
            "backend": backend.value,
            "emb_rows": emb_rows,
            "vec_rows_before": vec_rows_before,
            "vec_rows_after": vec_rows_after,
        },
        launch_id=launch_id,
        ts=ts,
    )
    return result
