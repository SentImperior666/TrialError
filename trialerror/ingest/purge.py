"""Remove a SUPERSEDED embedding model key's rows from the record.

**The failure this exists for.** A program whose ``[ingest.embed]`` table
named a placeholder backend embedded its whole corpus under that backend's
own key (``fake-<dims>``, 16 dimensions). The program was then pointed at a
real model and every document was re-embedded under the real key. Both sets
of rows now sit in ``emb`` side by side, because ``emb``'s primary key is
``(chunk_sha256, model_key)``: the second embedding did not overwrite the
first, it added a parallel one. The doctor's ``fake_backend_rows`` check
goes on failing with the placeholder count, ``ingest re-embed`` cannot help
(by design it only ADDS rows for the configured key), and direct SQL against
a live store is not a move the program's rules permit. There was no verb for
"this model key is superseded; take its rows out".

**Why ``re-embed`` does not delete the old rows on its own.** The obvious
shape -- "re-embed replaces" -- is the wrong one, and the reason is the
failure mode, not tidiness. A re-embed of a large document is a long job
that can die at any point: a GPU worker that never comes back, a laptop
lid, a killed process. If the first thing such a job did was delete the
chunks' existing vectors, a half-finished run would leave chunks with NO
embedding at all -- unsearchable, and invisible in exactly the way a
superseded-but-present vector is not. So the embed stage is purely additive
and resumable (a chunk whose ``(sha256, model_key)`` already has a row is
skipped, which is what makes a kill-mid-embed resume byte-identical), and
the subtraction is a SEPARATE, explicit, attributable verb that an operator
runs once the new key is known to be complete. ``ingest re-embed`` adds;
``ingest purge-embeddings`` subtracts; the order is never reversed.

**What the purge refuses.**

* The program's CURRENTLY CONFIGURED embed key
  (:class:`~trialerror.ingest.errors.ActiveEmbedKeyPurgeError`). Resolved
  through the SAME function the doctor's ``embedding_missing`` /
  ``embedding_stale`` checks resolve it with
  (:func:`trialerror.ingest.checks.configured_embed_model_key`, which
  follows the embed-backend loader branch for branch) so "the key this
  program is actually writing" means one thing in this codebase, not two.
  Purging the active key would delete the live search surface and leave the
  corpus silently unembedded -- the precise outcome the additive-embed
  design above exists to prevent.
* A missing or unregistered ``--launch-id``
  (:class:`~trialerror.stores.errors.XidTargetMissingError`, through the
  same :func:`trialerror.stores.writer.require_xid_targets` pre-flight
  ``retract`` uses). The L-E4 posture: a destructive act has to be
  attributable to a booked launch BEFORE anything is touched.

**Decisions worth naming.**

*Shared ``chunk.sha256`` values are purged, unlike in ``retract``.*
``retract`` deliberately keeps an ``emb`` row whose text another document
also contains, because that row is still live knowledge serving the other
document. Here the opposite holds: the key itself is superseded, so its row
is garbage for EVERY document that shares the hash, and keeping it would
mean the verb could never make ``fake_backend_rows`` pass. A ``--doc-id``
purge therefore removes the named document's rows even when another
document shares the hash -- it can only ever remove rows of the superseded
key, never of the active one.

*Rows with no surviving chunk are purged too (unscoped runs only).*
``emb`` is hash-addressed and has no FK to ``chunk`` on purpose, so a
rechunk leaves rows behind whose ``chunk_sha256`` no chunk carries any
more. ``fake_backend_rows`` counts those rows, so a purge that skipped them
could not clear the check. A ``--doc-id`` purge leaves them alone: they
belong to no document, so they are not in that document's scope.

*The vector table is emptied, not dropped.* ``vec_chunks__<model_key>`` is
schema created on demand by :func:`trialerror.stores.vecindex.ensure_vec_table`;
its ROWS are the data this verb is entitled to remove. An empty table for a
dead key costs nothing, is not read by any check (both embedding checks look
only at the ACTIVE key's table), and dropping it would be the one part of
this operation that a re-index could not undo.

*One transaction per document.* Each document's ``emb`` rows and its
vector-index entries commit together, so a crash mid-purge leaves whole
documents done and whole documents untouched -- never a document whose
vectors are gone from ``emb`` but still answering from the index.
``defer_foreign_keys`` is not needed here (neither ``emb`` nor the vector
tables carry foreign keys) and is deliberately not used, so nothing in this
module depends on a pragma it does not need.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from trialerror.ingest.errors import (
    ActiveEmbedKeyPurgeError,
    DocumentNotFoundError,
    PurgeIndexUnreadableError,
)
from trialerror.stores.store import Store
from trialerror.stores.writer import get, require_xid_targets
from trialerror.util.timeutil import now

__all__ = [
    "PURGE_EVENT_TYPE",
    "ActiveEmbedKeyPurgeError",
    "PurgeIndexUnreadableError",
    "purge_embeddings",
]

#: The one event this verb writes. Same ``"type"``-keyed shape as
#: ``document_retracted``: the counts, the scope and the launch.
PURGE_EVENT_TYPE = "embeddings_purged"

#: How many values go into one ``... IN (?, ?, ...)``. Same number, same
#: reason as :data:`trialerror.ingest.retract._CHUNK_ID_BATCH`:
#: ``SQLITE_LIMIT_VARIABLE_NUMBER`` is 999 on an older SQLite build and
#: 32,766 on a current one, so 900 (plus the handful of prefix parameters
#: below) is under both and the batching does not depend on which SQLite
#: the operator's Python was linked against. The live purge this verb was
#: written for is ~14,000 rows; the size at which it works must not be
#: smaller than the size at which it is needed.
_VALUE_BATCH = 900


def _delete_in_batches(
    conn,
    sql: str,
    values: Sequence[str],
    *,
    prefix_params: Iterable[Any] = (),
) -> int:
    """``conn.execute(sql.format(placeholders=...), ...)`` in batches over
    ``values``; returns the summed rowcount. ``sql`` carries a single
    ``{placeholders}`` slot and takes ``prefix_params`` first."""
    prefix = tuple(prefix_params)
    removed = 0
    for start in range(0, len(values), _VALUE_BATCH):
        batch = tuple(values[start : start + _VALUE_BATCH])
        placeholders = ",".join("?" for _ in batch)
        removed += conn.execute(sql.format(placeholders=placeholders), prefix + batch).rowcount
    return removed


def _vec_table_for(conn, model_key: str) -> tuple[str | None, bool]:
    """``(table_name, has_model_key_column)`` for ``model_key``'s vector
    index, or ``(None, False)`` when this program never indexed that key.

    The fallback (default) backend's table carries a ``model_key`` column
    and the ``sqlite-vec`` ``vec0`` virtual table does not, so the delete
    below adds that predicate only when the column is really there --
    belt-and-braces, since the table name already encodes the key."""
    from trialerror.stores.vecindex import vec_table_name

    table = vec_table_name(model_key)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        return None, False
    try:
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:  # noqa: BLE001 - an unreadable vec0 table is handled by the probe below
        columns = set()
    return table, "model_key" in columns


def _probe_vec_table(conn, table: str) -> None:
    """Refuse BEFORE deleting anything if the key's vector table exists but
    cannot be read (a ``vec0`` virtual table on a connection whose
    ``sqlite-vec`` extension would not load). Half a purge -- ``emb`` rows
    gone, index entries still answering -- is worse than no purge, and this
    is the one failure mode that could produce it."""
    try:
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except Exception as exc:  # noqa: BLE001 - reported as a named refusal
        raise PurgeIndexUnreadableError(
            f"the vector index table {table!r} exists but cannot be read ({exc}) -- refusing to "
            "purge the embedding rows while their index entries would have to stay behind. This "
            "is a sqlite-vec program: install the extension (or run with "
            "TRIALERROR_VEC_BACKEND=fallback on a program whose table is a plain one) and retry."
        ) from exc


def _scope_rows(conn, model_key: str, doc_id: str | None) -> list[dict[str, Any]]:
    sql = """
        SELECT c.doc_id AS doc_id, c.chunk_id AS chunk_id, c.sha256 AS sha256
        FROM chunk c
        JOIN emb e ON e.chunk_sha256 = c.sha256
        WHERE e.model_key = ?
    """
    params: tuple[Any, ...] = (model_key,)
    if doc_id is not None:
        sql += " AND c.doc_id = ?"
        params = (model_key, doc_id)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _orphan_shas(conn, model_key: str) -> list[str]:
    """``emb.chunk_sha256`` values under ``model_key`` that no ``chunk`` row
    carries any more (see the module docstring)."""
    rows = conn.execute(
        """
        SELECT e.chunk_sha256 AS sha FROM emb e
        LEFT JOIN chunk c ON c.sha256 = e.chunk_sha256
        WHERE e.model_key = ? AND c.sha256 IS NULL
        GROUP BY e.chunk_sha256
        """,
        (model_key,),
    ).fetchall()
    return [r["sha"] for r in rows]


def _chunks_without_any_embedding(conn, *, doc_id: str | None, excluding_key: str | None) -> int:
    """Chunks IN SCOPE (the named document, or the whole program) with no
    ``emb`` row under ANY model key.

    ``excluding_key`` makes the same count a PROJECTION rather than an
    observation: passing the key about to be purged answers "how many
    chunks would have no embedding at all once it is gone", which is what
    ``--dry-run`` must report and what the real run then confirms."""
    emb_predicate = "SELECT 1 FROM emb e WHERE e.chunk_sha256 = c.sha256"
    params: list[Any] = []
    if excluding_key is not None:
        emb_predicate += " AND e.model_key != ?"
        params.append(excluding_key)
    sql = f"SELECT COUNT(*) AS n FROM chunk c WHERE NOT EXISTS ({emb_predicate})"
    if doc_id is not None:
        sql += " AND c.doc_id = ?"
        params.append(doc_id)
    return int(conn.execute(sql, tuple(params)).fetchone()["n"])


def purge_embeddings(
    store: Store,
    *,
    model_key: str,
    launch_id: str,
    doc_id: str | None = None,
    dry_run: bool = False,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delete every ``emb`` row whose ``model_key`` is ``model_key`` (for one
    document when ``doc_id`` is given), together with that key's
    vector-index entries for the same chunks.

    Returns ``{"model_key", "documents", "rows_deleted",
    "index_entries_deleted", "chunks_now_without_any_embedding",
    "dry_run"}``. ``documents`` counts the documents rows were removed from;
    ``chunks_now_without_any_embedding`` is the post-purge count over the
    scope (see :func:`_chunks_without_any_embedding`) and is the number that
    tells an operator whether an ``ingest re-embed`` still has to run.

    Raises :class:`~trialerror.ingest.errors.ActiveEmbedKeyPurgeError` for
    the configured key, :class:`~trialerror.stores.errors.XidTargetMissingError`
    for a launch that names no ``platform.launch`` row,
    :class:`~trialerror.ingest.errors.DocumentNotFoundError` for an unknown
    ``doc_id``, and
    :class:`~trialerror.ingest.errors.PurgeIndexUnreadableError` when the
    key's vector table exists but cannot be read. Every one of them refuses
    before a single row is touched.

    Idempotent: a second run finds nothing and reports zeros. ``dry_run``
    reports the identical counts and writes nothing -- no rows, no event.
    """
    # The destructive-act pre-flight, first and in this order: an
    # unattributable purge is refused before the store is even read, and
    # the active-key refusal is a property of the CONFIG, so neither needs
    # a row to have been looked at to answer.
    require_xid_targets(store, "event", {"launch_id": launch_id})

    from trialerror.ingest.checks import configured_embed_model_key

    active = configured_embed_model_key(config)
    if model_key == active:
        raise ActiveEmbedKeyPurgeError(
            f"refusing to purge {model_key!r}: it is the embed model key this program is "
            "CONFIGURED to write ([ingest.embed]). Purging it would delete the live search "
            "surface and leave the corpus unembedded. Purge a SUPERSEDED key instead -- the one "
            "the record still carries from an earlier backend (trialerror doctor --only "
            "fake_backend_rows names them)."
        )

    conn = store.knowledge
    if doc_id is not None and get(store, "document", pk_column="doc_id", pk_value=doc_id) is None:
        raise DocumentNotFoundError(f"no such document: {doc_id!r}")

    # A vec0 virtual table is unreadable on a connection that has not
    # loaded the extension; the default backend is a plain table, but a
    # program running TRIALERROR_VEC_BACKEND=sqlite_vec needs this.
    from trialerror.stores.vecindex import try_load_sqlite_vec

    try_load_sqlite_vec(conn)
    vec_table, vec_has_model_key = _vec_table_for(conn, model_key)
    if vec_table is not None:
        _probe_vec_table(conn, vec_table)

    rows = _scope_rows(conn, model_key, doc_id)
    by_doc: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        bucket = by_doc.setdefault(row["doc_id"], {"chunk_ids": [], "shas": []})
        bucket["chunk_ids"].append(row["chunk_id"])
        if row["sha256"] not in bucket["shas"]:
            bucket["shas"].append(row["sha256"])
    orphans = _orphan_shas(conn, model_key) if doc_id is None else []

    ts = now()

    if dry_run:
        # Counted, never touched. ``rows_deleted`` is exact because emb's
        # PK is (chunk_sha256, model_key): one row per distinct sha.
        seen: set[str] = set()
        rows_deleted = 0
        for bucket in by_doc.values():
            for sha in bucket["shas"]:
                if sha not in seen:
                    seen.add(sha)
                    rows_deleted += 1
        rows_deleted += len(orphans)
        index_entries = 0
        if vec_table is not None:
            if doc_id is None:
                index_entries = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM {vec_table}"
                        + (" WHERE model_key = ?" if vec_has_model_key else ""),
                        (model_key,) if vec_has_model_key else (),
                    ).fetchone()["n"]
                )
            else:
                chunk_ids = by_doc.get(doc_id, {"chunk_ids": []})["chunk_ids"]
                for start in range(0, len(chunk_ids), _VALUE_BATCH):
                    batch = tuple(chunk_ids[start : start + _VALUE_BATCH])
                    placeholders = ",".join("?" for _ in batch)
                    index_entries += int(
                        conn.execute(
                            f"SELECT COUNT(*) AS n FROM {vec_table} WHERE chunk_id IN ({placeholders})",
                            batch,
                        ).fetchone()["n"]
                    )
        return {
            "model_key": model_key,
            "documents": len(by_doc),
            "rows_deleted": rows_deleted,
            "index_entries_deleted": index_entries,
            "chunks_now_without_any_embedding": _chunks_without_any_embedding(
                conn, doc_id=doc_id, excluding_key=model_key
            ),
            "dry_run": True,
        }

    rows_deleted = 0
    index_entries_deleted = 0
    documents_touched = 0
    deleted_shas: set[str] = set()

    # ONE transaction per document (module docstring): a crash mid-purge
    # leaves whole documents done and whole documents untouched.
    for scoped_doc_id, bucket in by_doc.items():
        shas = [s for s in bucket["shas"] if s not in deleted_shas]
        with conn:
            removed_rows = (
                _delete_in_batches(
                    conn,
                    "DELETE FROM emb WHERE model_key = ? AND chunk_sha256 IN ({placeholders})",
                    shas,
                    prefix_params=(model_key,),
                )
                if shas
                else 0
            )
            removed_index = (
                _delete_in_batches(
                    conn,
                    f"DELETE FROM {vec_table} WHERE chunk_id IN ({{placeholders}})",
                    bucket["chunk_ids"],
                )
                if vec_table is not None and bucket["chunk_ids"]
                else 0
            )
        deleted_shas.update(shas)
        rows_deleted += removed_rows
        index_entries_deleted += removed_index
        if removed_rows or removed_index:
            documents_touched += 1

    # The remainder, on an unscoped purge only: rows no chunk carries any
    # more, and index entries for chunk_ids that are likewise gone. Both
    # belong to the superseded key and nothing else, so the whole table's
    # leftovers go in one transaction of their own.
    if doc_id is None:
        leftover_shas = [s for s in orphans if s not in deleted_shas]
        with conn:
            if leftover_shas:
                rows_deleted += _delete_in_batches(
                    conn,
                    "DELETE FROM emb WHERE model_key = ? AND chunk_sha256 IN ({placeholders})",
                    leftover_shas,
                    prefix_params=(model_key,),
                )
                deleted_shas.update(leftover_shas)
            if vec_table is not None:
                index_entries_deleted += conn.execute(
                    f"DELETE FROM {vec_table}"
                    + (" WHERE model_key = ?" if vec_has_model_key else ""),
                    (model_key,) if vec_has_model_key else (),
                ).rowcount

    result = {
        "model_key": model_key,
        "documents": documents_touched,
        "rows_deleted": rows_deleted,
        "index_entries_deleted": index_entries_deleted,
        "chunks_now_without_any_embedding": _chunks_without_any_embedding(
            conn, doc_id=doc_id, excluding_key=None
        ),
        "dry_run": False,
    }

    # No event for a purge that removed nothing. A second (idempotent) run
    # is not a change to the record, and an audit log that records no-ops
    # is a worse audit log -- the same reason ``retract``'s
    # ``already_retracted`` path writes none.
    if index_entries_deleted:
        # Lane F-1 item E: the key's resident similarity matrix is now a
        # cache of rows that no longer exist. The fingerprint would catch it
        # on the next query anyway; invalidating here means the next query
        # rebuilds instead of the one after it noticing.
        from trialerror.retrieve.vecmatrix import invalidate as invalidate_vecmatrix

        invalidate_vecmatrix(store.program_root, model_key, config)

    if rows_deleted or index_entries_deleted:
        from trialerror.events.api import append_event

        append_event(
            store,
            event_type=PURGE_EVENT_TYPE,
            payload={
                "model_key": model_key,
                "doc_id": doc_id,
                "documents": documents_touched,
                "rows_deleted": rows_deleted,
                "index_entries_deleted": index_entries_deleted,
                "chunks_now_without_any_embedding": result["chunks_now_without_any_embedding"],
                "vec_table": vec_table,
            },
            launch_id=launch_id,
            ts=ts,
        )
    return result
