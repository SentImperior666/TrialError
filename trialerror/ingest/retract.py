"""Retract one ingested document: take its derived rows back out of the
record, keep the document row itself, and say why.

**The failure this exists for.** A document ingested through a fake OCR
stand-in put ~94,000 garbage elements and 624 chunks into a live corpus and
its search index, and there was no way to take them out again. Every write
verb in this package is additive; ``rechunk``/``re-embed`` re-derive but
never subtract; ``reindex-fulltext`` rebuilds the index from a ``chunk``
table that still held the garbage. The corpus had no undo.

**What retraction is, and is not.** It is not a delete. The ``document``
row STAYS -- so does its ``source`` row -- because "this document was
ingested and then withdrawn, for this reason, by this launch" is a fact
about the record worth keeping, and because a deleted row would let the
same bad file be re-ingested with no trace of the first attempt. What goes
is everything DERIVED from the document's content:

    element, chunk, quote_anchor, chunk_fts, vec_chunks__<model>,
    the emb cache rows no other document shares, the derived archive text
    (``<archive_dir>/<doc_id>.txt``), any derived PDF tree
    (``<archive_dir>/derived/<doc_id>/``), and the document's entries in
    the tantivy full-text index.

Afterwards ``ingest add`` of the same raw file under the same source is an
ordinary new document -- the retracted one stays put, retracted.

**Why the retraction lives in ``record`` and not in a new column.**
``document.status``'s CHECK constraint (``knowledge.db`` v1) allows exactly
``registered|normalized|parsed|chunked|embedded|indexed|failed``. Adding
``'retracted'`` means rewriting that constraint, which is a knowledge
migration, and knowledge v5 is spoken for by another unmerged lane -- a
second v5 would collide. So this lane uses what the schema already offers:
the generic ``record`` register (``register_key =
'ingest.retraction'``), the same table :mod:`trialerror.ingest.extract`
already uses for its merge-review queue, indexed by ``register_key``. One
row per retracted document, carrying the reason, the timestamp, the launch
and the counts. ``document.status`` is set to ``'failed'`` -- the only
allowed value that means "this document did not end in a usable state" --
and ``ocr_backend``/``ocr_version`` are cleared, because they described a
derivation that no longer exists.

That is a deviation, disclosed rather than hidden, and the register is a
real indexed table rather than a provenance column abused as a flag. The
follow-up when the migration lane is free: add ``'retracted'`` to the
status CHECK plus ``retracted_ts``/``retracted_reason`` columns, and make
:func:`retracted_doc_ids` read the column instead. Every caller here goes
through that one function precisely so that change is a one-line move.

**What retraction refuses.** A document whose anchors are cited by
``claim`` rows (an extraction pass ran over it) is REFUSED, not
force-deleted (:class:`~trialerror.ingest.errors.RetractBlockedError`,
naming the count): those claims are knowledge somebody accepted, and
silently destroying them to satisfy a cleanup command is not a trade this
module is entitled to make on its own.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from trialerror.ingest.errors import DocumentNotFoundError, RetractBlockedError
from trialerror.stores.store import Store
from trialerror.stores.writer import get, require_xid_targets
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "RETRACTION_REGISTER_KEY",
    "RETRACTED_DOCUMENT_STATUS",
    "RetractBlockedError",
    "retract_document",
    "retracted_doc_ids",
    "is_retracted",
    "retraction_record",
]

#: ``record.register_key`` for the retraction ledger (see the module
#: docstring for why this table and not a new column).
RETRACTION_REGISTER_KEY = "ingest.retraction"

#: What ``document.status`` reads for a retracted document. NOT
#: ``'retracted'`` -- see the module docstring; the register row is the
#: authoritative signal and this is the closest allowed value.
RETRACTED_DOCUMENT_STATUS = "failed"


# ---------------------------------------------------------------------------
# reading the register
# ---------------------------------------------------------------------------


def retracted_doc_ids(conn) -> set[str]:
    """Every retracted ``doc_id``, from any DB-API connection to
    knowledge.db (a ``Store.knowledge``, or a doctor's read-only handle).

    The ONE place the "is this document retracted" question is answered, so
    the eventual migration to a real ``document.status = 'retracted'`` is a
    change to this function's body and nothing else. Never raises: a
    program whose ``record`` table predates this feature simply has no such
    rows, and a doctor check must not fall over on one."""
    try:
        rows = conn.execute(
            "SELECT payload FROM record WHERE register_key = ?", (RETRACTION_REGISTER_KEY,)
        ).fetchall()
    except Exception:  # noqa: BLE001 - a doctor read must degrade, not crash
        return set()
    out: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] if hasattr(row, "keys") else row[0])
        except (TypeError, ValueError):
            continue
        doc_id = payload.get("doc_id")
        if isinstance(doc_id, str):
            out.add(doc_id)
    return out


def is_retracted(conn, doc_id: str) -> bool:
    return doc_id in retracted_doc_ids(conn)


def retraction_record(conn, doc_id: str) -> dict[str, Any] | None:
    """The retraction payload for ``doc_id`` (reason, ts, launch, counts),
    or ``None``. What ``trialerror ingest status`` reports."""
    try:
        rows = conn.execute(
            "SELECT payload FROM record WHERE register_key = ? ORDER BY seq DESC",
            (RETRACTION_REGISTER_KEY,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        try:
            payload = json.loads(row["payload"] if hasattr(row, "keys") else row[0])
        except (TypeError, ValueError):
            continue
        if payload.get("doc_id") == doc_id:
            return payload
    return None


# ---------------------------------------------------------------------------
# the write path
# ---------------------------------------------------------------------------


def _vec_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name LIKE 'vec_chunks__%'"
    ).fetchall()
    return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]


def _claims_anchored_in(conn, doc_id: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM claim c
            JOIN quote_anchor a ON a.anchor_id = c.anchor_id
            WHERE a.doc_id = ?
            """,
            (doc_id,),
        ).fetchone()[0]
    )


def _shared_shas(conn, doc_id: str) -> set[str]:
    """``chunk.sha256`` values this document shares with ANOTHER document.

    ``emb`` is hash-addressed on purpose -- its own schema comment says the
    row "outlives any one chunk row that happened to produce that text" --
    so two documents containing the same paragraph share one cache row.
    Retracting one of them must not blind the other, which would show up
    later as an ``embedding_missing`` doctor warning and a silent recall
    hole. Only the shas nobody else uses are deleted."""
    rows = conn.execute(
        """
        SELECT DISTINCT sha256 FROM chunk
        WHERE doc_id != ? AND sha256 IN (SELECT sha256 FROM chunk WHERE doc_id = ?)
        """,
        (doc_id, doc_id),
    ).fetchall()
    return {r["sha256"] if hasattr(r, "keys") else r[0] for r in rows}


def _archive_paths(store: Store, doc: dict[str, Any], config: dict[str, Any] | None) -> list[Path]:
    """The derived files on disk: the normalized stream text at the
    document's own ``rel_path``, and the whole per-document derived tree
    (which is where ``trialerror.ingest.normalize_djvu`` puts a converted
    PDF). The operator's own RAW INPUT is never touched -- it is not the
    pipeline's output, and ``ingest add`` has to be able to re-ingest it.

    Note that "the operator's raw input" and ``document.raw_path`` are not
    always the same file: on both DjVu routes the row's ``raw_path`` is
    rewritten to the DERIVED pdf inside the tree removed here, so the
    surviving row ends up pointing at a file that is gone. That is by
    design (nothing is left to check a citation against, and the ``.djvu``
    itself is untouched and re-ingestable) but it is not obvious, so
    :func:`retract_document` reports it as ``raw_path_removed`` rather
    than leaving a reader of the event to work it out."""
    from trialerror.ingest.pipeline import DEFAULT_ARCHIVE_DIR

    archive_dir_value = (config or {}).get("paths", {}).get("archive_dir", DEFAULT_ARCHIVE_DIR)
    paths = [store.program_root / doc["rel_path"]]
    paths.append(store.program_root / archive_dir_value / "derived" / doc["doc_id"])
    return paths


#: How many ``chunk_id`` placeholders go into one ``... IN (?, ?, ...)``.
#: ``SQLITE_LIMIT_VARIABLE_NUMBER`` is 32,766 on a current build and 999 on
#: an older one; 900 is under both, so the batching does not depend on
#: which SQLite the operator's Python was linked against.
_CHUNK_ID_BATCH = 900


def _delete_by_chunk_ids(conn, table: str, chunk_ids: list[str]) -> int:
    """``DELETE FROM <table> WHERE chunk_id IN (...)``, in batches; returns
    the total rowcount.

    One placeholder per chunk_id walks straight into
    ``SQLITE_LIMIT_VARIABLE_NUMBER``: a 32,816-chunk document raised a bare
    ``sqlite3.OperationalError('too many SQL variables')`` which escaped
    ``_cmd_retract``'s handlers entirely, so the CLI answered a traceback
    instead of an error envelope. Nothing was destroyed (the transaction
    rolled back), but retraction is the one verb whose whole purpose is
    cleaning up an ingest that went wrong at scale, and the live incident
    that prompted it was ~94,000 elements -- the size at which it works
    must not be smaller than the size at which it is needed."""
    removed = 0
    for start in range(0, len(chunk_ids), _CHUNK_ID_BATCH):
        batch = chunk_ids[start : start + _CHUNK_ID_BATCH]
        placeholders = ",".join("?" for _ in batch)
        removed += conn.execute(
            f"DELETE FROM {table} WHERE chunk_id IN ({placeholders})", batch
        ).rowcount
    return removed


def _remove_path(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path)
            return True
        if path.exists():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def retract_document(
    store: Store,
    *,
    doc_id: str,
    launch_id: str,
    reason: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retract ``doc_id``. Returns
    ``{"doc_id", "already_retracted", "removed": {...}, "files_removed",
    "raw_path_removed", "fulltext", "reason", "ts", "launch_id"}``.

    ``raw_path_removed`` is the entry of ``files_removed`` that
    ``document.raw_path`` pointed at, or ``None``. It is not ``None`` on a
    DjVu document: both DjVu routes rewrite ``raw_path`` to the derived PDF
    inside the tree this removes, so the surviving row dangles by design
    (see :func:`_archive_paths`).

    Raises :class:`~trialerror.ingest.errors.DocumentNotFoundError` for an
    unknown document, :class:`RetractBlockedError` when ``claim`` rows are
    anchored in it, and
    :class:`~trialerror.stores.errors.XidTargetMissingError` when
    ``launch_id`` names no registered launch (the same guard every other
    write verb in this package applies -- an unattributable retraction is
    exactly the kind of record change that must not be possible).

    Idempotent: a second call removes nothing and reports
    ``already_retracted``.
    """
    require_xid_targets(store, "event", {"launch_id": launch_id})

    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    if doc is None:
        raise DocumentNotFoundError(f"no such document: {doc_id!r}")

    conn = store.knowledge
    ts = now()
    if is_retracted(conn, doc_id):
        return {
            "doc_id": doc_id,
            "already_retracted": True,
            "removed": {
                "elements": 0,
                "chunks": 0,
                "quote_anchors": 0,
                "embeddings": 0,
                "fts_rows": 0,
                "vec_rows": 0,
            },
            "files_removed": [],
            "raw_path_removed": None,
            "fulltext": {"action": "skip", "removed": 0},
            "jobs_cancelled": [],
            "jobs_held": [],
            "reason": reason,
            "ts": ts,
            "launch_id": launch_id,
        }

    blocking_claims = _claims_anchored_in(conn, doc_id)
    if blocking_claims:
        raise RetractBlockedError(
            f"document {doc_id!r} has {blocking_claims} claim(s) anchored in it -- retracting would "
            "destroy extracted knowledge that outlived the document's own derived rows. Retract or "
            "reject those claims first (trialerror extract reject), then retry."
        )

    chunk_rows = [
        dict(r)
        for r in conn.execute("SELECT chunk_id, sha256 FROM chunk WHERE doc_id = ?", (doc_id,)).fetchall()
    ]
    chunk_ids = [r["chunk_id"] for r in chunk_rows]
    shared = _shared_shas(conn, doc_id)
    doomed_shas = sorted({r["sha256"] for r in chunk_rows} - shared)

    # A vec0 virtual table is unreadable on a connection that has not
    # loaded the extension; the default backend is a plain table, but a
    # program running TRIALERROR_VEC_BACKEND=sqlite_vec needs this.
    from trialerror.stores.vecindex import try_load_sqlite_vec

    try_load_sqlite_vec(conn)
    vec_tables = _vec_tables(conn)

    removed = {"elements": 0, "chunks": 0, "quote_anchors": 0, "embeddings": 0, "fts_rows": 0, "vec_rows": 0}
    vec_tables_failed: list[str] = []

    from trialerror.artifacts._txn import raw_insert, raw_update

    # ONE transaction for the whole subtraction plus the register row, so a
    # crash cannot leave a half-retracted document.
    #
    # The delete ORDER below is dependency-correct on its own
    # (quote_anchor -> chunk -> element; measured: without deferral, doing
    # it the other way round really does raise FOREIGN KEY constraint
    # failed). defer_foreign_keys makes the set order-INDEPENDENT instead,
    # which is what makes this safe against the next FK somebody adds to
    # one of these tables -- on a destructive path, "correct as long as
    # nobody reorders these six statements" is a worse property to rely on
    # than the pragma costs. (SQLite clears it at each COMMIT/ROLLBACK; the
    # finally covers the raise-before-commit path.)
    conn.execute("PRAGMA defer_foreign_keys = ON")
    try:
        with conn:
            removed["quote_anchors"] = conn.execute(
                "DELETE FROM quote_anchor WHERE doc_id = ?", (doc_id,)
            ).rowcount
            if chunk_ids:
                removed["fts_rows"] = _delete_by_chunk_ids(conn, "chunk_fts", chunk_ids)
                for table in vec_tables:
                    try:
                        removed["vec_rows"] += _delete_by_chunk_ids(conn, table, chunk_ids)
                    except Exception:  # noqa: BLE001 - an unreadable vec table is reported, not fatal
                        vec_tables_failed.append(table)
            removed["chunks"] = conn.execute("DELETE FROM chunk WHERE doc_id = ?", (doc_id,)).rowcount
            for sha in doomed_shas:
                removed["embeddings"] += conn.execute(
                    "DELETE FROM emb WHERE chunk_sha256 = ?", (sha,)
                ).rowcount
            removed["elements"] = conn.execute("DELETE FROM element WHERE doc_id = ?", (doc_id,)).rowcount

            raw_update(
                conn,
                "document",
                pk_column="doc_id",
                pk_value=doc_id,
                changes={
                    "status": RETRACTED_DOCUMENT_STATUS,
                    # These described a derivation that no longer exists --
                    # and leaving ocr_backend = 'fake' behind would keep a
                    # retracted document in the fake_backend_rows count
                    # forever, which is the opposite of the point.
                    "ocr_backend": None,
                    "ocr_version": None,
                },
            )

            seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM record WHERE register_key = ?",
                    (RETRACTION_REGISTER_KEY,),
                ).fetchone()[0]
            )
            payload = {
                "doc_id": doc_id,
                "source_id": doc["source_id"],
                "reason": reason,
                "launch_id": launch_id,
                "ts": ts,
                "removed": dict(removed),
            }
            raw_insert(
                conn,
                "record",
                {
                    "record_id": new_id("REC"),
                    "register_key": RETRACTION_REGISTER_KEY,
                    "artifact_id": None,
                    "seq": seq,
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "anchors": None,
                    "created_ts": ts,
                },
            )
    finally:
        conn.execute("PRAGMA defer_foreign_keys = OFF")

    # Everything below is DERIVED state outside knowledge.db (design
    # Section 6 stage 7: "indexes are cache, never truth"), so it happens
    # after the transaction that made the truth true. A failure here leaves
    # skew the fulltext_index_stale doctor check reports and
    # `ingest reindex-fulltext` repairs -- it never un-retracts.
    from trialerror.retrieve import lexical

    fulltext = lexical.prune_index(store, chunk_ids, config=config)

    files_removed: list[str] = []
    # Which of the removed paths (if any) is the one ``document.raw_path``
    # still points at. On both DjVu routes raw_path was rewritten to the
    # derived PDF, which lives in the tree removed above -- so the
    # surviving row dangles, deliberately. Named here rather than left for
    # a reader of the event to deduce.
    raw_path_removed: str | None = None
    raw_value = doc.get("raw_path")
    raw_resolved: Path | None = None
    if raw_value:
        raw_candidate = Path(raw_value)
        raw_resolved = (
            raw_candidate if raw_candidate.is_absolute() else store.program_root / raw_candidate
        ).resolve()

    for path in _archive_paths(store, doc, config):
        resolved = path.resolve()  # BEFORE removal: a deleted path has no real one
        if not _remove_path(path):
            continue
        try:
            recorded = resolved.relative_to(store.program_root.resolve()).as_posix()
        except ValueError:
            recorded = str(path)
        files_removed.append(recorded)
        if raw_resolved is not None and (raw_resolved == resolved or resolved in raw_resolved.parents):
            raw_path_removed = recorded

    # Lane FB-3 item 9 (observed 2026-09-15): a duplicate registration was
    # retracted while its normalize job was still pending, and the job
    # survived -- an orphan the next worker would have claimed and run
    # against a document whose derived rows had just been removed. The
    # operator paused them by hand. Retraction now cancels its own pending
    # work, with the retraction's reason on each settled row.
    #
    # After the knowledge transaction, deliberately: jobs.db is a different
    # file, and a queue that could not be reached must never un-retract the
    # document. A job a worker currently HOLDS is not touched here -- the
    # run-time guard in trialerror.jobs.worker is what stops that one, at its
    # next claim, from deriving anything.
    jobs_cancelled: list[dict[str, Any]] = []
    jobs_held: list[dict[str, Any]] = []
    jobs_note: str | None = None
    try:
        from trialerror.jobs import ledger as jobs_ledger

        outcome = jobs_ledger.abandon_pending_for_doc(
            store, doc_id, reason=f"document retracted: {reason}"
        )
        jobs_cancelled = outcome["cancelled"]
        # Fix pass V-6: a job a worker is HOLDING is not settled here (that
        # would let its worker complete a row the ledger had closed) -- but
        # it is reported. It was previously skipped in silence, so an
        # operator retracting a duplicate whose normalize stage was RUNNING
        # was told what had been cancelled and not that a stage was still
        # running against the document they had just withdrawn. The run-time
        # guard fires at CLAIM, so it catches that job's successors, not the
        # job itself.
        jobs_held = outcome["held"]
        if jobs_held:
            holders = ", ".join(
                f"{j['job_id']} ({j['kind']}, {j['state']}, worker {j['claimed_by']})" for j in jobs_held
            )
            jobs_note = (
                f"{len(jobs_held)} job(s) of this document are held by a worker and were NOT "
                f"cancelled: {holders}. The retraction stands; that stage will finish against a "
                "document that no longer has derived rows. Pause it (`trialerror jobs pause`) and "
                "settle it with `trialerror jobs abandon`, or wait for its lease and "
                "`trialerror jobs tick`"
            )
    except Exception as exc:  # noqa: BLE001 - a queue this cannot reach never un-retracts a document
        jobs_note = (
            f"could not settle this document's pending jobs ({type(exc).__name__}); the retraction "
            "itself stands, and the stage runner refuses to start any stage on a retracted document"
        )

    from trialerror.events.api import append_event

    append_event(
        store,
        event_type="document_retracted",
        payload={
            "doc_id": doc_id,
            "source_id": doc["source_id"],
            "reason": reason,
            "removed": dict(removed),
            "files_removed": files_removed,
            "raw_path_removed": raw_path_removed,
            "fulltext": fulltext,
            "vec_tables_failed": vec_tables_failed,
            "jobs_cancelled": jobs_cancelled,
            "jobs_held": jobs_held,
            "jobs_note": jobs_note,
        },
        launch_id=launch_id,
        ts=ts,
    )

    result = {
        "doc_id": doc_id,
        "already_retracted": False,
        "removed": removed,
        "files_removed": files_removed,
        "raw_path_removed": raw_path_removed,
        "fulltext": fulltext,
        "jobs_cancelled": jobs_cancelled,
        "jobs_held": jobs_held,
        "reason": reason,
        "ts": ts,
        "launch_id": launch_id,
    }
    if jobs_note:
        result["jobs_note"] = jobs_note
    if vec_tables_failed:
        result["vec_tables_failed"] = vec_tables_failed
    return result
