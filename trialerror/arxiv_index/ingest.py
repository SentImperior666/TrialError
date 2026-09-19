"""Streaming, resumable zip ingest. Build brief item 3: "reading directly
from the zip member streams (``zipfile`` module), batched inserts,
resumable via a jobs-ledger job kind (``arxiv_index_build``) with
checkpoints -- kill-mid-build must resume ... Disk preflight + progress
events."

**Two layouts, one entry point**: :func:`build_index_from_zip` auto-detects
which layout ``zip_path`` actually uses from its member NAMES (no new CLI
flag, no config knob -- see ``trialerror/arxiv_index/__init__.py``'s module
docstring for the full CONFIRMED-vs-ASSUMED history):

- **``csv+dat`` (the REAL Kaggle ``openai-arxiv-embeddings.zip`` layout,
  CONFIRMED by direct inspection of the 33GB file, fix-arxiv-ingest-layout
  session)** -- chosen whenever the zip contains BOTH a member named
  :data:`DEFAULT_CSV_MEMBER` (``papers.csv``, header ``index,id,journal``,
  one metadata row per paper) AND a member named :data:`DEFAULT_DAT_MEMBER`
  (``vectors.dat``, the SAME rows' embeddings as raw concatenated
  little-endian float32, ``dims*4`` bytes per row, ZERO framing/delimiters
  between rows) -- see :func:`_detect_csv_dat_members`. Row ``i`` of the
  csv aligns with row ``i`` of the dat (verified integer-exact against the
  real file: 3,569,548 csv data rows, dat size exactly
  ``3,569,548 * 3072 * 4`` bytes). This is now the layout the real 33GB
  zip actually needs -- see :func:`_build_index_from_csv_dat`.
- **``jsonl`` (the ORIGINAL assumed layout, kept for back-compat + the
  existing test suite)** -- the fallback when no csv+dat pair is found:
  every ``member_glob``-matching member is newline-delimited JSON, one
  record per line, per :data:`DEFAULT_FIELD_MAP`. Unchanged from the
  original build.

**No title/abstract/authors/categories/doi in the real zip**: ``papers.csv``
is index -> (arxiv_id, journal) only. The ``arxiv_meta`` row the csv+dat
path inserts therefore leaves ``title``/``abstract``/``categories``/
``authors``/``published``/``doi`` NULL (the ``arxiv_meta`` schema already
allows this -- only ``ingested_ts`` is ``NOT NULL``, see
``trialerror.arxiv_index.store.ensure_schema``) and only fills ``journal_ref``.
**Seam for a later session**: title hydration for a query's top-k results
is NOT built in this pass -- the existing keyless
``trialerror.litapi.providers.arxiv.ArxivProvider`` (no API key needed) is the
natural place to hydrate titles for the handful of ids a query actually
returns, called AFTER :func:`trialerror.arxiv_index.query.semantic_search`
narrows 3.5M rows down to ``k``, never during ingest (which never makes a
network call, by design, for all 3.5M rows).

**Streaming, precisely**: neither layout's ingest path ever calls
``ZipFile.extract``/``extractall`` -- every record is read via
``ZipFile.open(member)`` (a file-like object over the DEFLATE stream,
decompressed on the fly). The jsonl path wraps that in a
``io.TextIOWrapper`` for line iteration; the csv+dat path opens BOTH
member streams concurrently (``zf.open(csv_member)`` wrapped in
``io.TextIOWrapper`` + ``csv.reader``, and ``zf.open(dat_member)`` read in
raw fixed-size chunks via :func:`_read_exact` -- no ``numpy``, no
conversion: the ``dims*4``-byte chunk read per row IS ALREADY
``trialerror.stores.vecindex.serialize_vector_fallback``'s own wire format
(packed little-endian float32), so it is passed straight into
:func:`_insert_vector_row` as a raw ``bytes`` blob). The 34.9GB real zip is
never larger on disk than the zip itself plus whatever this call has
committed to ``db_path`` so far.

**Resume, precisely**: ``checkpoint`` (the same dict shape
``trialerror.jobs.worker.JobContext.set_checkpoint`` persists) records
``members_done`` (zip member names fully ingested -- the csv+dat layout
uses the single synthetic key :data:`CSV_DAT_LAYOUT_KEY` for this, since
it is one logical member-pair, not a list of independent members) and, for
the member in progress, how many rows of it have already been committed
(``records_seen_in_current_member``). A resumed call re-opens the zip from
the start (zip member streams cannot be seeked to an arbitrary record --
only sequential read is possible within one member) and skip-reads BOTH
streams up to that row count before doing any DB work for it: the jsonl
path still pays the cheap decompress/parse cost per skipped line (unchanged
from the original build); the csv+dat path does a bulk discard instead (csv
rows via repeated ``next(reader)``, dat bytes via :func:`_skip_exact` in
bounded-size chunks) rather than a naive read-one-row-at-a-time loop --
still fundamentally decompress-speed-bound (documented, acceptable: this
session did not attempt a random-access index into the DEFLATE stream),
just without the extra per-row Python-object overhead the naive form would
add across a possible resume near row 3,000,000+. Every DB write is
additionally wrapped to tolerate a duplicate arxiv_id (see
:func:`_insert_vector_row`'s own docstring for why this needs an explicit
try/except rather than plain ``INSERT OR IGNORE`` for the vec0 case) -- so
even an imperfect skip boundary can never double-count or crash, only waste
a little redundant parsing. This is the same "idempotent,
re-derive what's-already-durable-from-the-store-itself" posture
``trialerror/ingest/handlers.py``'s own module docstring describes for every
other resumable handler in this repo, adapted to a source that is a zip
member stream instead of a knowledge-store table.

**Row-count integrity (csv+dat only)**: the csv+dat layout is a CONFIRMED,
not assumed, format -- so unlike the jsonl path (which tolerates and just
counts malformed/incomplete records as ``rows_skipped``, because the real
file's exact shape was never independently verified), the csv+dat path
treats any row-count mismatch as a hard, loud failure, never a silently
partial index: :data:`_build_index_from_csv_dat` (a) asserts
``vectors.dat``'s own zip-central-directory-recorded uncompressed size is
an exact multiple of ``dims*4`` BEFORE reading a single byte (catches a
wrong ``dims`` or a genuinely different wire format immediately), (b) every
per-row read of the dat stream is byte-exact-or-raise via
:func:`_read_exact` (catches a truncated/corrupt member mid-stream --
``zipfile`` itself additionally CRC-checks each member on stream close, an
independent safety net this guard does not replace), and (c) at the end of
a full (non-resumed-partial) pass asserts
``rows_ingested == csv data-row count == dat_bytes / (dims*4)`` -- any
mismatch raises :class:`ArxivIndexIngestError` rather than marking the
build ``status=complete``.

``_raise_after_rows`` is a test-only seam (mirrors
``trialerror.ingest.backends.FakeEmbedBackend``'s ``delay_s``/
``trialerror.util.atomic``'s own kill-mid-write seams: "internal seams used by
the kill-mid-write test to slow/interrupt it deterministically; production
callers never pass them") -- raises :class:`SimulatedKillError` after that
many rows have been committed IN THIS CALL, so a test can prove
kill-mid-build resume without a real subprocess/process-kill. Applies to
both layouts identically.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trialerror.arxiv_index.store import (
    META_TABLE_NAME,
    VEC_TABLE_NAME,
    VecBackend,
    ensure_schema,
    serialize_vector_fallback,
    set_build_state,
)
from trialerror.util.timeutil import now

__all__ = [
    "DEFAULT_MEMBER_GLOB",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_FIELD_MAP",
    "DEFAULT_CSV_MEMBER",
    "DEFAULT_DAT_MEMBER",
    "CSV_DAT_LAYOUT_KEY",
    "ArxivIndexIngestError",
    "SchemaAssumptionError",
    "SimulatedKillError",
    "BuildProgress",
    "build_index_from_zip",
]

DEFAULT_MEMBER_GLOB = "*.jsonl"
DEFAULT_BATCH_SIZE = 500

#: The REAL Kaggle zip's two member names (module docstring; confirmed by
#: direct inspection, fix-arxiv-ingest-layout session) -- exact-match (or
#: exact basename match under a subdirectory, in case a future weekly
#: refresh nests them) is how :func:`_detect_csv_dat_members` decides which
#: layout branch :func:`build_index_from_zip` takes. Not exposed as a CLI
#: flag/config knob (build brief: "auto-detected from member names") --
#: unlike the jsonl path's ``member_glob``, there is nothing to override
#: here because the real file's names are now CONFIRMED, not assumed.
DEFAULT_CSV_MEMBER = "papers.csv"
DEFAULT_DAT_MEMBER = "vectors.dat"

#: The synthetic ``members_done``/``current_member`` checkpoint key the
#: csv+dat layout uses in place of a real per-member zip entry name (module
#: docstring's "Resume, precisely" paragraph) -- the two real member names
#: are joined with ``+`` specifically so this string can never collide with
#: an actual zip member name (no real member name legally contains ``+``
#: between two ``.``-having path segments the way this constant is built).
CSV_DAT_LAYOUT_KEY = f"{DEFAULT_CSV_MEMBER}+{DEFAULT_DAT_MEMBER}"

#: Bulk skip-read chunk size for :func:`_skip_exact` (csv+dat resume) --
#: bounds memory while still resolving a resume near row 3,000,000+ in a
#: handful of chunk reads per row-range rather than one Python-level call
#: per 12,288-byte row (module docstring's "Resume, precisely" paragraph).
_DAT_SKIP_CHUNK_BYTES = 8 * 1024 * 1024

#: See ``trialerror/arxiv_index/__init__.py``'s module docstring for the full
#: ASSUMED-schema disclosure. Each target column maps to an ordered tuple
#: of candidate source-JSON keys tried in order (first present wins) --
#: this is the ONE knob an operator overrides (``[litapi.arxiv_index]``
#: ``field_map_json``, see ``trialerror/litapi/config.py``) if the real
#: download's field names differ from this assumption.
DEFAULT_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "arxiv_id": ("id", "arxiv_id"),
    "title": ("title",),
    "abstract": ("abstract", "summary"),
    "categories": ("categories",),
    "authors": ("authors",),
    "published": ("update_date", "published", "date"),
    "doi": ("doi",),
    "journal_ref": ("journal-ref", "journal_ref"),
    "embedding": ("embedding", "vector"),
}

#: Required target columns -- a record missing either makes the row
#: unusable (no primary key, or no vector to index) and is skipped (or, for
#: the very first record this call ever sees, raised loudly -- see
#: :func:`build_index_from_zip`'s own docstring).
_REQUIRED_FIELDS = ("arxiv_id", "embedding")


class ArxivIndexIngestError(RuntimeError):
    pass


class SchemaAssumptionError(ArxivIndexIngestError):
    """Raised when the FIRST record this call ever parses is missing a
    required field under every candidate key name in ``field_map`` -- the
    loud, fail-fast signal that the ASSUMED schema
    (``trialerror/arxiv_index/__init__.py`` module docstring) does not match the
    real downloaded file, before this call silently skips millions of rows
    as garbage."""


class SimulatedKillError(RuntimeError):
    """Test-only -- see this module's docstring's ``_raise_after_rows``
    paragraph. Never raised by a production call (the parameter defaults
    to ``None``, which never triggers this)."""


@dataclass
class BuildProgress:
    members_done: list[str] = field(default_factory=list)
    current_member: str | None = None
    records_seen_in_current_member: int = 0
    rows_ingested: int = 0
    rows_skipped: int = 0
    started_ts: str = ""
    updated_ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "members_done": list(self.members_done),
            "current_member": self.current_member,
            "records_seen_in_current_member": self.records_seen_in_current_member,
            "rows_ingested": self.rows_ingested,
            "rows_skipped": self.rows_skipped,
            "started_ts": self.started_ts,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_checkpoint(cls, checkpoint: Mapping[str, Any] | None) -> "BuildProgress":
        checkpoint = checkpoint or {}
        return cls(
            members_done=list(checkpoint.get("members_done", []) or []),
            current_member=checkpoint.get("current_member"),
            records_seen_in_current_member=int(checkpoint.get("records_seen_in_current_member", 0) or 0),
            rows_ingested=int(checkpoint.get("rows_ingested", 0) or 0),
            rows_skipped=int(checkpoint.get("rows_skipped", 0) or 0),
            started_ts=checkpoint.get("started_ts") or now(),
            updated_ts=checkpoint.get("updated_ts") or "",
        )


def _first_present(raw: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _normalize_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _extract_record(raw: Mapping[str, Any], field_map: Mapping[str, Sequence[str]]) -> dict[str, Any] | None:
    arxiv_id = _first_present(raw, field_map["arxiv_id"])
    embedding = _first_present(raw, field_map["embedding"])
    if arxiv_id is None or embedding is None or not isinstance(embedding, (list, tuple)):
        return None
    return {
        "arxiv_id": str(arxiv_id),
        "title": _normalize_scalar(_first_present(raw, field_map.get("title", ()))),
        "abstract": _normalize_scalar(_first_present(raw, field_map.get("abstract", ()))),
        "categories": _normalize_scalar(_first_present(raw, field_map.get("categories", ()))),
        "authors": _normalize_scalar(_first_present(raw, field_map.get("authors", ()))),
        "published": _normalize_scalar(_first_present(raw, field_map.get("published", ()))),
        "doi": _normalize_scalar(_first_present(raw, field_map.get("doi", ()))),
        "journal_ref": _normalize_scalar(_first_present(raw, field_map.get("journal_ref", ()))),
        "embedding": [float(v) for v in embedding],
    }


def _insert_vector_row(conn: sqlite3.Connection, backend: VecBackend, arxiv_id: str, dims: int, blob: bytes) -> bool:
    """Insert one row into the vector table; returns ``True`` if inserted,
    ``False`` if it was already present (duplicate, tolerated -- see this
    module's own docstring on why resume can re-offer already-committed
    rows). ``INSERT OR IGNORE`` is NOT sufficient for the ``vec0`` virtual
    table case -- confirmed empirically (this build's own interactive
    probe against the real installed ``sqlite-vec`` extension): a ``vec0``
    primary-key conflict raises ``sqlite3.OperationalError`` ("UNIQUE
    constraint failed on <table> primary key") even under ``OR IGNORE``,
    unlike an ordinary sqlite table. The plain fallback table (an ordinary
    table) DOES honor ``OR IGNORE`` normally, but this function uses the
    same try/except shape for both branches so callers don't need to know
    which backend is live."""
    try:
        if backend == VecBackend.SQLITE_VEC:
            conn.execute(f"INSERT INTO {VEC_TABLE_NAME}(arxiv_id, embedding) VALUES (?, ?)", (arxiv_id, blob))
        else:
            conn.execute(
                f"INSERT INTO {VEC_TABLE_NAME}(arxiv_id, dims, vector) VALUES (?, ?, ?)", (arxiv_id, dims, blob)
            )
        return True
    except sqlite3.OperationalError as exc:
        if "unique constraint failed" not in str(exc).lower():
            raise
        return False


def _flush_batch(conn: sqlite3.Connection, batch: list[dict[str, Any]], backend: VecBackend, dims: int) -> int:
    inserted = 0
    ts = now()
    with conn:
        for rec in batch:
            blob = serialize_vector_fallback(rec["embedding"])
            if not _insert_vector_row(conn, backend, rec["arxiv_id"], dims, blob):
                continue  # already indexed (resumed re-offer) -- meta insert below still runs (OR IGNORE)
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {META_TABLE_NAME}
                    (arxiv_id, title, abstract, categories, authors, published, doi, journal_ref, ingested_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["arxiv_id"], rec["title"], rec["abstract"], rec["categories"],
                    rec["authors"], rec["published"], rec["doi"], rec["journal_ref"], ts,
                ),
            )
            inserted += 1
    return inserted


def _detect_csv_dat_members(names: Sequence[str]) -> tuple[str, str] | None:
    """Returns ``(csv_member, dat_member)`` if ``names`` (a zip's own
    ``namelist()``) contains a member matching :data:`DEFAULT_CSV_MEMBER`
    AND one matching :data:`DEFAULT_DAT_MEMBER`, else ``None`` (the jsonl
    ``member_glob`` path is used instead -- module docstring). A match is
    either an exact name, or a name ending in ``/<target>`` (tolerates a
    subdirectory prefix, in case a future weekly Kaggle refresh nests the
    two files -- the real zip inspected this session did not)."""

    def _find(target: str) -> str | None:
        for n in names:
            if n == target or n.endswith("/" + target):
                return n
        return None

    csv_member = _find(DEFAULT_CSV_MEMBER)
    dat_member = _find(DEFAULT_DAT_MEMBER)
    if csv_member is not None and dat_member is not None:
        return csv_member, dat_member
    return None


def _read_exact(fh: Any, n: int, *, what: str) -> bytes:
    """Read EXACTLY ``n`` bytes from ``fh`` (a zip member stream) or raise
    loudly -- module docstring's row-count-integrity item (b). A short read
    (including a 0-byte read at EOF) means either a truncated/corrupt
    ``vectors.dat`` member or a csv/dat row-count mismatch; both are fatal,
    never a silently-partial vector."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = fh.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != n:
        raise ArxivIndexIngestError(
            f"short read from {what}: expected {n} bytes, got {len(data)} -- csv/dat byte-exact "
            "row alignment is mandatory for the csv+dat layout (trialerror/arxiv_index/ingest.py module "
            "docstring); this means either a truncated/corrupt zip member or a csv-row-count vs "
            "dat-byte-count mismatch. A genuinely corrupt zip member will usually also fail its own "
            "CRC check on stream close (zipfile.BadZipFile) independently of this guard."
        )
    return data


def _skip_exact(fh: Any, n: int, *, what: str) -> None:
    """Bulk-discard exactly ``n`` bytes from ``fh`` in bounded-size chunks
    (:data:`_DAT_SKIP_CHUNK_BYTES`) -- the csv+dat resume path's
    equivalent of the jsonl path's per-line skip-and-discard loop (module
    docstring's "Resume, precisely" paragraph)."""
    remaining = n
    while remaining > 0:
        chunk = fh.read(min(remaining, _DAT_SKIP_CHUNK_BYTES))
        if not chunk:
            raise ArxivIndexIngestError(
                f"resume skip-read ran off the end of {what} before reaching the checkpointed row "
                "-- the checkpoint is inconsistent with this zip file (different file, or a "
                "truncated/replaced one since the checkpoint was written?)"
            )
        remaining -= len(chunk)


def _flush_batch_csv_dat(conn: sqlite3.Connection, batch: list[dict[str, Any]], backend: VecBackend, dims: int) -> int:
    """Same shape/contract as :func:`_flush_batch`, but for csv+dat batch
    records (``{"arxiv_id", "journal_ref", "vector_blob"}`` -- note
    ``vector_blob`` is the RAW bytes read straight off the dat stream, NOT
    a list of floats: module docstring's "no conversion, no numpy needed"
    -- so unlike :func:`_flush_batch`, this never calls
    ``serialize_vector_fallback``). Metadata insert only fills
    ``arxiv_id``/``journal_ref``/``ingested_ts`` -- ``papers.csv`` carries
    no title/abstract/categories/authors/doi (module docstring), and the
    ``arxiv_meta`` schema already allows those columns to be NULL."""
    inserted = 0
    ts = now()
    with conn:
        for rec in batch:
            if not _insert_vector_row(conn, backend, rec["arxiv_id"], dims, rec["vector_blob"]):
                continue  # already indexed (resumed re-offer) -- meta insert below still runs (OR IGNORE)
            conn.execute(
                f"INSERT OR IGNORE INTO {META_TABLE_NAME} (arxiv_id, journal_ref, ingested_ts) VALUES (?, ?, ?)",
                (rec["arxiv_id"], rec["journal_ref"], ts),
            )
            inserted += 1
    return inserted


def _build_index_from_csv_dat(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    csv_member: str,
    dat_member: str,
    *,
    dims: int,
    batch_size: int,
    backend: VecBackend,
    progress: BuildProgress,
    on_progress: Callable[[dict[str, Any]], None] | None,
    _raise_after_rows: int | None,
) -> None:
    """The csv+dat layout's own ingest loop (module docstring) -- mutates
    ``progress`` in place (same convention :func:`build_index_from_zip`'s
    jsonl loop uses) and persists a checkpoint after every flushed batch.
    Returns normally (no return value) once EITHER the pair was already
    fully ingested by a prior call (a no-op replay, mirroring the jsonl
    loop's ``if member in members_done: continue``) OR this call finished
    ingesting it AND the row-count integrity check (module docstring)
    passed -- in both cases ``progress.members_done`` ends up containing
    :data:`CSV_DAT_LAYOUT_KEY`. Raises :class:`SimulatedKillError` (test
    seam) or :class:`ArxivIndexIngestError`/:class:`SchemaAssumptionError`
    on any integrity failure, exactly like the jsonl loop.
    """
    if CSV_DAT_LAYOUT_KEY in set(progress.members_done):
        return  # already fully ingested by a prior call -- pure no-op replay

    row_bytes = dims * 4
    dat_info = zf.getinfo(dat_member)
    if dat_info.file_size % row_bytes != 0:
        raise ArxivIndexIngestError(
            f"{dat_member} uncompressed size ({dat_info.file_size} bytes, from the zip's own central "
            f"directory) is not a multiple of dims*4 ({row_bytes} bytes, dims={dims}) -- the "
            "vectors.dat wire-format assumption (trialerror/arxiv_index/ingest.py module docstring) does "
            "not match this file; check --dims against the real embedding model's dimensionality"
        )
    expected_total_rows = dat_info.file_size // row_bytes

    skip_target = progress.records_seen_in_current_member if progress.current_member == CSV_DAT_LAYOUT_KEY else 0
    seen_in_member = 0
    total_committed_this_call = 0
    batch: list[dict[str, Any]] = []

    def _flush() -> None:
        nonlocal batch, total_committed_this_call
        if not batch:
            return
        inserted = _flush_batch_csv_dat(conn, batch, backend, dims)
        progress.rows_ingested += inserted
        progress.rows_skipped += len(batch) - inserted
        total_committed_this_call += inserted
        batch = []
        progress.current_member = CSV_DAT_LAYOUT_KEY
        progress.records_seen_in_current_member = seen_in_member
        progress.updated_ts = now()
        set_build_state(
            conn,
            {"checkpoint_json": json.dumps(progress.to_dict()), "rows_ingested": progress.rows_ingested, "status": "building"},
        )
        if on_progress:
            on_progress(progress.to_dict())
        if _raise_after_rows is not None and total_committed_this_call >= _raise_after_rows:
            raise SimulatedKillError(
                f"simulated kill after {total_committed_this_call} rows committed this call "
                "(test-only seam -- see this module's docstring)"
            )

    with zf.open(csv_member) as csv_raw, zf.open(dat_member) as dat_fh:
        csv_text = io.TextIOWrapper(csv_raw, encoding="utf-8", errors="replace", newline="")
        reader = csv.reader(csv_text)
        try:
            header = next(reader)
        except StopIteration:
            raise SchemaAssumptionError(f"{csv_member} is empty (no header row) -- expected at least a header")
        col_idx = {name.strip(): i for i, name in enumerate(header)}
        if "id" not in col_idx:
            raise SchemaAssumptionError(
                f"{csv_member}'s header {header!r} has no 'id' column -- the papers.csv schema "
                "assumption (trialerror/arxiv_index/ingest.py module docstring) does not match this file"
            )
        id_idx = col_idx["id"]
        journal_idx = col_idx.get("journal")

        if skip_target > 0:
            for _ in range(skip_target):
                try:
                    next(reader)
                except StopIteration:
                    raise ArxivIndexIngestError(
                        f"resume checkpoint wants to skip {skip_target} csv rows but {csv_member} ran "
                        "out first -- checkpoint is inconsistent with this zip file"
                    )
            _skip_exact(dat_fh, skip_target * row_bytes, what=dat_member)
            seen_in_member = skip_target

        for row in reader:
            seen_in_member += 1
            blob = _read_exact(dat_fh, row_bytes, what=dat_member)
            arxiv_id = row[id_idx].strip() if id_idx < len(row) else ""
            if not arxiv_id:
                raise SchemaAssumptionError(
                    f"{csv_member} row {seen_in_member} has an empty/missing id -- the papers.csv "
                    "schema assumption (trialerror/arxiv_index/ingest.py module docstring) requires a "
                    "non-empty id on every data row"
                )
            journal_ref = None
            if journal_idx is not None and journal_idx < len(row):
                raw_journal = row[journal_idx].strip()
                journal_ref = raw_journal or None
            batch.append({"arxiv_id": arxiv_id, "journal_ref": journal_ref, "vector_blob": blob})
            if len(batch) >= batch_size:
                _flush()

        _flush()

    if seen_in_member != expected_total_rows:
        raise ArxivIndexIngestError(
            f"row-count integrity check failed: {csv_member} produced {seen_in_member} data rows but "
            f"{dat_member} implies {expected_total_rows} rows ({dat_info.file_size} bytes / {row_bytes} "
            "bytes-per-row) -- refusing to mark the index complete (never a silent partial index)"
        )
    if progress.rows_ingested != expected_total_rows:
        raise ArxivIndexIngestError(
            f"row-count integrity check failed: rows_ingested={progress.rows_ingested} but expected "
            f"{expected_total_rows} (from {dat_member}'s byte size) -- refusing to mark the index "
            "complete (never a silent partial index)"
        )

    progress.members_done = sorted(set(progress.members_done) | {CSV_DAT_LAYOUT_KEY})
    progress.current_member = None
    progress.records_seen_in_current_member = 0


def build_index_from_zip(
    conn: sqlite3.Connection,
    zip_path: Path | str,
    *,
    dims: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    member_glob: str = DEFAULT_MEMBER_GLOB,
    field_map: Mapping[str, Sequence[str]] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    _raise_after_rows: int | None = None,
) -> dict[str, Any]:
    """Ingest every record from every ``member_glob``-matching member
    inside ``zip_path`` into ``conn`` (already schema'd, or schema'd here
    via :func:`trialerror.arxiv_index.store.ensure_schema` if this is the first
    call). Returns the final progress dict (same shape ``on_progress`` is
    called with after every flushed batch) -- a caller resumes a killed
    build by passing the LAST progress dict this function (or the prior
    call) returned/reported back in as ``checkpoint``.

    Raises :class:`SchemaAssumptionError` immediately if the very FIRST
    record parsed across the whole call is missing a required field
    (module docstring) -- every subsequent malformed/incomplete record is
    just counted in ``rows_skipped`` instead, so one bad row deep in a
    multi-million-row file never aborts an otherwise-good build.
    """
    resolved_field_map = dict(DEFAULT_FIELD_MAP)
    if field_map:
        resolved_field_map.update(field_map)

    backend = ensure_schema(conn, dims=dims)
    progress = BuildProgress.from_checkpoint(checkpoint)
    members_done = set(progress.members_done)
    resume_member = progress.current_member
    resume_skip = progress.records_seen_in_current_member if resume_member else 0
    seen_any_record = progress.rows_ingested > 0 or progress.rows_skipped > 0
    total_committed_this_call = 0

    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

        # Layout auto-detection (module docstring): the csv+dat layout --
        # the REAL Kaggle zip's confirmed shape -- takes priority whenever
        # both its member names are present, entirely bypassing the jsonl
        # member_glob matching below (so a zip with ONLY csv+dat members
        # never hits the "no zip members matched glob" error, and a zip
        # with BOTH layouts present -- test-only, the real zip has exactly
        # 2 members -- deterministically uses csv+dat, never a silent mix
        # of both).
        csv_dat_layout = _detect_csv_dat_members(names)
        if csv_dat_layout is not None:
            csv_member, dat_member = csv_dat_layout
            _build_index_from_csv_dat(
                conn, zf, csv_member, dat_member,
                dims=dims, batch_size=batch_size, backend=backend, progress=progress,
                on_progress=on_progress, _raise_after_rows=_raise_after_rows,
            )
            progress.updated_ts = now()
            set_build_state(
                conn,
                {
                    "checkpoint_json": json.dumps(progress.to_dict()),
                    "rows_ingested": progress.rows_ingested,
                    "rows_skipped": progress.rows_skipped,
                    "zip_path": str(zip_path),
                    "dims": dims,
                    "layout": "csv_dat",
                    "member_glob": f"{csv_member}+{dat_member}",
                    "status": "complete",
                },
            )
            if on_progress:
                on_progress(progress.to_dict())
            return progress.to_dict()

        members = sorted(n for n in names if fnmatch.fnmatch(n, member_glob))
        if not members:
            preview = names[:20]
            raise ArxivIndexIngestError(
                f"no zip members matched glob {member_glob!r} inside {zip_path} -- "
                f"members present (first 20 of {len(names)}): {preview!r}. "
                "This is the ASSUMED-schema knob: if the real download uses a different "
                "extension/layout, set [litapi.arxiv_index].member_glob accordingly "
                "(see trialerror/arxiv_index/__init__.py's module docstring)."
            )

        for member in members:
            if member in members_done:
                continue
            skip_target = resume_skip if member == resume_member else 0
            seen_in_member = 0
            batch: list[dict[str, Any]] = []

            with zf.open(member) as raw_fh:
                text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8", errors="replace")
                for line in text_fh:
                    line = line.strip()
                    if not line:
                        continue
                    seen_in_member += 1
                    if seen_in_member <= skip_target:
                        continue
                    try:
                        raw_record = json.loads(line)
                    except json.JSONDecodeError:
                        progress.rows_skipped += 1
                        seen_any_record = True
                        continue
                    record = _extract_record(raw_record, resolved_field_map)
                    if record is None:
                        if not seen_any_record:
                            raise SchemaAssumptionError(
                                f"the first record in {member!r} is missing a required field under every "
                                f"candidate key name (required: {_REQUIRED_FIELDS!r}, tried: {resolved_field_map!r}, "
                                f"raw record keys seen: {sorted(raw_record.keys()) if isinstance(raw_record, dict) else type(raw_record)!r}) -- "
                                "the ASSUMED schema (trialerror/arxiv_index/__init__.py module docstring) does not match "
                                "this file; adjust [litapi.arxiv_index].field_map_json / member_glob"
                            )
                        progress.rows_skipped += 1
                        seen_any_record = True
                        continue
                    seen_any_record = True
                    if len(record["embedding"]) != dims:
                        progress.rows_skipped += 1
                        continue
                    batch.append(record)
                    if len(batch) >= batch_size:
                        inserted = _flush_batch(conn, batch, backend, dims)
                        progress.rows_ingested += inserted
                        progress.rows_skipped += len(batch) - inserted
                        total_committed_this_call += inserted
                        batch.clear()
                        progress.current_member = member
                        progress.records_seen_in_current_member = seen_in_member
                        progress.updated_ts = now()
                        set_build_state(
                            conn,
                            {
                                "checkpoint_json": json.dumps(progress.to_dict()),
                                "rows_ingested": progress.rows_ingested,
                                "status": "building",
                            },
                        )
                        if on_progress:
                            on_progress(progress.to_dict())
                        if _raise_after_rows is not None and total_committed_this_call >= _raise_after_rows:
                            raise SimulatedKillError(
                                f"simulated kill after {total_committed_this_call} rows committed this call "
                                "(test-only seam -- see this module's docstring)"
                            )

                if batch:
                    inserted = _flush_batch(conn, batch, backend, dims)
                    progress.rows_ingested += inserted
                    progress.rows_skipped += len(batch) - inserted
                    total_committed_this_call += inserted
                    progress.current_member = member
                    progress.records_seen_in_current_member = seen_in_member
                    progress.updated_ts = now()
                    set_build_state(
                        conn,
                        {"checkpoint_json": json.dumps(progress.to_dict()), "rows_ingested": progress.rows_ingested, "status": "building"},
                    )
                    if on_progress:
                        on_progress(progress.to_dict())
                    if _raise_after_rows is not None and total_committed_this_call >= _raise_after_rows:
                        raise SimulatedKillError(
                            f"simulated kill after {total_committed_this_call} rows committed this call "
                            "(test-only seam -- see this module's docstring)"
                        )

            members_done.add(member)
            progress.members_done = sorted(members_done)
            resume_member = None  # only the resumed member (if any) ever uses a nonzero skip_target

    progress.current_member = None
    progress.records_seen_in_current_member = 0
    progress.updated_ts = now()
    set_build_state(
        conn,
        {
            "checkpoint_json": json.dumps(progress.to_dict()),
            "rows_ingested": progress.rows_ingested,
            "rows_skipped": progress.rows_skipped,
            "zip_path": str(zip_path),
            "dims": dims,
            "member_glob": member_glob,
            "status": "complete",
        },
    )
    if on_progress:
        on_progress(progress.to_dict())
    return progress.to_dict()
