"""The sandbox side of the seam: resolve a stage from published outputs,
or park it.

This is the five-step logic design section 4 spells out for a handler
whose backend is ``"offload"``, in one place so ``run_ocr`` and
``run_embed`` share it exactly:

1. ``done/<job_id>/`` present -> verify every payload sha and the
   ``expect`` block -> feed the result into the SAME tail the Real backends
   feed. Mismatch -> the result is moved to ``failed/`` and the stage
   raises a LOGIC failure (attempt burned, visible).
2. else if the manifest already exists in ``pending/ u claimed/*/ u done/ u
   failed/`` -> nothing new is written.
3. else write inputs + manifest (``offload_attempts = 0``).
4. if ``failed/<job_id>/`` exists -> raise a LOGIC failure (terminal).
5. otherwise raise ``EnvironmentalFailure("awaiting DEV GPU",
   retry_delay_s=1800)``.

Steps 1 and 4 are checked before 2/3 here, which is the same set of rules
in the only order that can be evaluated once: a job cannot be both
published and pending.

**Why an environmental failure and not a pause.** Parking as
``EnvironmentalFailure`` means the ledger re-queues the job WITHOUT
consuming an attempt (``trialerror.jobs.ledger.fail``'s environmental arm),
so a DEV laptop that stays off for three weeks costs the job nothing: its
``attempts`` column is still 0 when the GPU finally comes back. That is
the property the whole "DEV is intermittent" design rests on, and it is
why the retry budget is tracked separately, inside the marker, as
``offload_attempts`` -- that counter measures GPU failures, which are real
failures, rather than absences, which are not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# The model boundary, shared with the query side (lane F-1b item 3): the
# semantics are this path's -- it produced the stored vectors -- but the
# FUNCTION now lives with the query-side clients that have to apply the same
# one (theirs kept ``\r`` and passed whitespace-only text through, so they
# encoded a different string than the corpus was encoded from). Re-exported
# below under this module's own name (the same object, not a wrapper), so the
# worker's chunk path and every existing import keep resolving.
#
# Import direction: ``trialerror.ingest.backends`` imports
# ``trialerror.jobs.worker`` (as this module already does) and
# ``trialerror.offload.marker``, neither of which reaches back here -- so this
# is not a cycle. ``tests/test_ingest_embeddable_text.py`` imports both
# modules in both orders to keep it that way.
from trialerror.ingest.backends import embeddable_text as embeddable_text
from trialerror.jobs.worker import EnvironmentalFailure
from trialerror.offload import protocol
from trialerror.offload.marker import config_hash

__all__ = [
    "PARK_RETRY_DELAY_S",
    "OCR_INPUT_STEM",
    "OCR_OUTPUT_NAME",
    "EMBED_INPUT_NAME",
    "EMBED_OUTPUT_NAME",
    "ChunkPayloadError",
    "MissingStageInputError",
    "ocr_input_name",
    "build_chunks_payload",
    "read_chunks_payload",
    "rebuild_inputs",
    "embeddable_text",
    "offload_ocr_result",
    "offload_embed_vectors",
]

#: design section 4 step 5: ``retry_delay_s=1800``. Half an hour is the
#: cadence the sandbox's jobs loop already runs at, so a parked job is
#: re-examined roughly once per loop rather than spinning.
PARK_RETRY_DELAY_S = 1800

OCR_INPUT_STEM = "input"
OCR_OUTPUT_NAME = "pages.json"
EMBED_INPUT_NAME = "chunks.jsonl"
EMBED_OUTPUT_NAME = "vectors.jsonl"

_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}\Z")


def _read_input_under_program_root(program_root: Path | str, raw_path: Path | str) -> bytes:
    """Read a stage input, refusing any path that resolves outside the
    program root (SEC-8).

    ``document.raw_path`` is data: it comes from whatever registered the
    document, it is stored verbatim, and ``_resolve_raw_path`` honours it
    as-is when it is absolute. Locally that only ever names a file the
    process could already read. Offloading changes the consequence: this
    byte string is copied into ``offload/pending/<job>/`` and then handed
    over SSH to another machine, so an arbitrary absolute ``raw_path``
    would be an exfiltration channel with a queue and a courier attached.
    The queue only ever carries the program's own files."""
    root = Path(program_root).resolve()
    resolved = Path(raw_path).resolve()
    try:
        inside = resolved.is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - different drives on Windows
        inside = False
    if not inside:
        raise RuntimeError(
            f"offload: refusing to queue {resolved} -- an offloaded stage may only send files "
            f"from inside the program root ({root}). Fix the document's raw_path, or re-register "
            "the document from a location inside the program."
        )
    return resolved.read_bytes()


def ocr_input_name(raw_path: Path | str) -> str:
    """``input.pdf`` for a ``.pdf`` raw file, ``input`` for anything whose
    extension is not a plain short alphanumeric one. The extension is
    carried because marker dispatches on it; it is sanitized because the
    name becomes a path component on two machines."""
    suffix = Path(raw_path).suffix
    return f"{OCR_INPUT_STEM}{suffix.lower()}" if _SAFE_SUFFIX_RE.match(suffix) else OCR_INPUT_STEM


# ---------------------------------------------------------------------------
# payload encoding (shared with the DEV worker, which decodes them)
# ---------------------------------------------------------------------------
class ChunkPayloadError(protocol.OffloadProtocolError):
    """A ``chunks.jsonl`` line could not be decoded.

    Named, and carrying the LINE NUMBER and (where it can still be read out
    of the broken line) the ``chunk_id``, because the bare
    ``json.JSONDecodeError`` this replaces said only "Unterminated string
    starting at: line 1 column 69" -- a column in a line the operator never
    sees, on a job whose error.json named no document and no chunk. Three
    live documents failed on every DEV attempt behind that message."""


#: The characters ``str.splitlines()`` treats as line terminators but
#: ``json.dumps(..., ensure_ascii=False)`` does NOT escape: U+0085 NEL,
#: U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR. (``json`` escapes
#: every C0 control, U+0000-U+001F, so ``\n``/``\r``/``\x0b``/``\x0c``/
#: ``\x1c``-``\x1e`` are already safe; these three are not C0 and survive.)
#:
#: THE DEFECT (live, three documents, every DEV attempt): one chunk whose
#: text contained one of these produced a single valid JSON line that
#: ``read_chunks_payload``'s ``str.splitlines()`` then cut in two, so the
#: worker handed ``json.loads`` a fragment ending mid-string --
#: ``JSONDecodeError: Unterminated string starting at: line 1 column 69``,
#: column 69 being exactly where the ``"text"`` value opens. The JSON was
#: never malformed; the line-splitting disagreed with the JSON grammar
#: about what a line is.
_RAW_LINE_BREAKS = {
    "\u0085": "\\u0085",  # NEL
    "\u2028": "\\u2028",  # LINE SEPARATOR
    "\u2029": "\\u2029",  # PARAGRAPH SEPARATOR
}


def _escape_raw_line_breaks(line: str) -> str:
    """Replace the three raw line-break characters above with their JSON
    ``\\uXXXX`` escapes. Safe as a blanket replacement: every structural
    byte of one of these lines is ASCII (the keys are ASCII, the numbers are
    ASCII), so a match can only ever be inside a string literal, and the
    escape decodes back to the identical character."""
    for raw, escaped in _RAW_LINE_BREAKS.items():
        if raw in line:
            line = line.replace(raw, escaped)
    return line


def build_chunks_payload(chunks: Sequence[dict[str, Any]]) -> bytes:
    """``chunks.jsonl``: one ``{"chunk_id", "seq", "text"}`` per line, in
    the order the manifest's ``expect.chunk_ids`` lists them. ALL chunks of
    the document travel together (design v3 delta N2: the real backend
    embeds in batches of eight, so batching is the WORKER's business, not
    the queue's).

    ``ensure_ascii=False`` is kept -- the payload is mostly non-ASCII prose
    and ``\\uXXXX``-escaping all of it would roughly double the bytes on a
    wire whose payload size is capped -- so the three line-break characters
    ``json`` leaves raw under that flag are escaped afterwards
    (:data:`_RAW_LINE_BREAKS`). The assertion below is the law rather than
    the comment: ONE record is ONE physical line, verified per line, so the
    next character class somebody's normalizer starts emitting fails here,
    in the writer, on the machine that owns the record -- not 20 minutes
    later as an unattributable decode error on the GPU host."""
    lines = []
    for c in chunks:
        line = _escape_raw_line_breaks(
            json.dumps(
                {"chunk_id": c["chunk_id"], "seq": c.get("seq"), "text": c["text"]},
                ensure_ascii=False,
            )
        )
        if len(line.splitlines()) != 1:
            raise ChunkPayloadError(
                f"chunk {c['chunk_id']!r}: its text contains a character that would split the "
                f"chunks.jsonl line "
                f"({_describe_line_breaks(line)}) -- refusing to queue a payload the DEV worker "
                "cannot decode"
            )
        lines.append(line)
    return ("\n".join(lines) + "\n").encode("utf-8")


#: Every character ``str.splitlines()`` treats as a line terminator, spelled
#: out rather than probed per character: a LONE line-break character's own
#: ``.splitlines()`` returns ``['']`` -- length 1 -- so "does THIS one
#: character split a line?" is a question ``splitlines`` cannot be asked.
_LINE_BREAK_CODEPOINTS = frozenset(
    "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
)


def _describe_line_breaks(text: str) -> str:
    """``U+2028 at offset 71`` for every character in ``text`` that
    ``str.splitlines()`` would break on -- the diagnostic both the writer's
    refusal and the reader's error need."""
    found = [
        f"U+{ord(ch):04X} at offset {i}"
        for i, ch in enumerate(text)
        if ch in _LINE_BREAK_CODEPOINTS
    ]
    return ", ".join(found) if found else "no line-break character found"


_CHUNK_ID_PREFIX_RE = re.compile(r'"chunk_id"\s*:\s*"([^"]{1,128})"')


def read_chunks_payload(data: bytes) -> list[dict[str, Any]]:
    """Decode a ``chunks.jsonl`` payload into ``[{"chunk_id", "seq",
    "text"}, ...]``.

    Split on ``"\\n"`` ONLY -- what :func:`build_chunks_payload` joined with
    -- never ``str.splitlines()``, whose idea of a line break includes three
    characters the JSON grammar treats as ordinary string content (see
    :data:`_RAW_LINE_BREAKS`). That alone makes this reader tolerant of the
    payloads the old writer produced: a raw U+2028 inside a JSON string is
    legal JSON, so a job already parked with one decodes correctly here
    without being re-queued.

    A line that still will not decode raises :class:`ChunkPayloadError`
    naming the 1-based line number and, when it can be recovered from the
    broken text, the ``chunk_id`` -- the two facts that turn "this job fails
    every time" into "this chunk of this document is the problem"."""
    out: list[dict[str, Any]] = []
    text = data.decode("utf-8")
    for lineno, line in enumerate(text.split("\n"), start=1):
        # Only the trailing newline's empty tail, and any stray CR a
        # transport added, are stripped -- never the payload's own content.
        line = line.strip("\r").strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (TypeError, ValueError) as exc:
            match = _CHUNK_ID_PREFIX_RE.search(line)
            named = f"chunk {match.group(1)!r}" if match else "chunk (id unreadable)"
            raise ChunkPayloadError(
                f"chunks.jsonl line {lineno}: {named} did not decode -- {exc} "
                f"[{_describe_line_breaks(line)}]"
            ) from exc
        if not isinstance(record, dict) or "chunk_id" not in record:
            raise ChunkPayloadError(
                f"chunks.jsonl line {lineno}: decoded to {type(record).__name__}, expected an "
                'object with a "chunk_id"'
            )
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# rebuilding a queued job's inputs from the record (lane FB-8a)
# ---------------------------------------------------------------------------
class MissingStageInputError(protocol.OffloadProtocolError):
    """The payload an offloaded stage would send is no longer on the sandbox.

    Raised by :func:`rebuild_inputs`, and the reason ``trialerror jobs
    retry`` refuses BY NAME rather than queueing a job the GPU worker will
    claim, fail on, and hand back three times: the document was retracted,
    its raw file was moved out of the program root, or its chunk rows no
    longer match the chunk ids the manifest promised the worker."""


def _document_row(store, doc_id: str) -> dict[str, Any] | None:
    row = store.knowledge.execute(
        "SELECT * FROM document WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def rebuild_inputs(store, manifest: dict[str, Any]) -> list[tuple[str, bytes]]:
    """The inputs this manifest's stage would send TODAY, read back out of
    the record.

    This is :func:`requeue_marker`'s rule ("the store is the durable truth,
    the queue is a courier") applied to a marker whose input directory no
    longer exists at all: :func:`~trialerror.offload.protocol.fail_marker`
    purges ``pending/<job_id>/`` when it makes a marker terminal, so a retry
    cannot salvage the bytes and must re-derive them from the same two
    sources ``offload_ocr_result``/``offload_embed_vectors`` build them from.

    Every way that can fail is a NAMED refusal
    (:class:`MissingStageInputError`), because the alternative -- queueing a
    job whose payload is absent, truncated, or about a different chunk set
    than the manifest's ``expect`` block promises -- spends GPU time to
    arrive back at ``failed/`` with a less informative error than this one.
    """
    stage = manifest.get("stage")
    doc_id = manifest.get("doc_id")
    expect = manifest.get("expect") or {}
    job_id = manifest.get("job_id")
    if not doc_id:
        raise MissingStageInputError(
            f"offload retry {job_id}: this marker names no doc_id, so there is no record to "
            "rebuild its input payload from"
        )
    doc = _document_row(store, doc_id)
    if doc is None:
        raise MissingStageInputError(
            f"offload retry {job_id}: document {doc_id!r} is no longer in this program's record "
            "(retracted, or never registered here) -- nothing to send to the GPU worker"
        )

    if stage == "ocr":
        raw_value = doc.get("raw_path")
        if not raw_value:
            raise MissingStageInputError(
                f"offload retry {job_id}: document {doc_id!r} has no raw_path"
            )
        raw_path = Path(raw_value)
        if not raw_path.is_absolute():
            raw_path = Path(store.program_root) / raw_path
        if not raw_path.is_file():
            raise MissingStageInputError(
                f"offload retry {job_id}: the document's raw file is missing on the sandbox "
                f"({raw_path}) -- re-acquire the document before retrying the OCR job"
            )
        wanted = expect.get("input_name") or ocr_input_name(raw_path)
        current = ocr_input_name(raw_path)
        if wanted != current:
            raise MissingStageInputError(
                f"offload retry {job_id}: this marker expects an input named {wanted!r} and the "
                f"document's raw file would now be sent as {current!r} -- marker dispatches on "
                "that extension, so the raw file behind this document is not the one the job was "
                "queued for; re-enqueue the stage instead of retrying this marker"
            )
        return [(wanted, _read_input_under_program_root(store.program_root, raw_path))]

    if stage == "embed":
        # Exactly the shape ``_run_embed_body`` hands ``offload_embed_vectors``
        # (chunk_id + text, ordered by seq, no ``seq`` key), so a retried
        # payload is byte-identical to a freshly queued one rather than merely
        # equivalent.
        rows = [
            {"chunk_id": r["chunk_id"], "text": r["text"]}
            for r in store.knowledge.execute(
                "SELECT chunk_id, text FROM chunk WHERE doc_id = ? ORDER BY seq", (doc_id,)
            ).fetchall()
        ]
        if not rows:
            raise MissingStageInputError(
                f"offload retry {job_id}: document {doc_id!r} has no chunk rows left -- the "
                "chunk stage has to run again before its embeddings can be retried"
            )
        wanted_ids = expect.get("chunk_ids")
        if wanted_ids is not None and [r["chunk_id"] for r in rows] != list(wanted_ids):
            raise MissingStageInputError(
                f"offload retry {job_id}: the document's chunk rows no longer match the chunk "
                f"ids this marker promised the worker ({len(rows)} now, "
                f"{len(list(wanted_ids))} then) -- it was re-chunked since, so re-enqueue the "
                "embed stage instead of retrying this marker"
            )
        name = expect.get("input_name") or EMBED_INPUT_NAME
        return [(name, build_chunks_payload(rows))]

    raise MissingStageInputError(
        f"offload retry {job_id}: unknown offload stage {stage!r} -- this harness can rebuild "
        "the inputs of 'ocr' and 'embed' jobs only"
    )


# The model boundary (``embeddable_text``) is imported at the top of this
# module from :mod:`trialerror.ingest.backends`: since lane F-1b item 3 it is
# ONE function, shared with the query-side clients, and this module re-exports
# it under the name the worker's chunk path already used.


# ---------------------------------------------------------------------------
# the shared resolve-or-park core
# ---------------------------------------------------------------------------
def _root(ctx) -> Path:
    return protocol.offload_root(ctx.store.program_root)


def _reject(root: Path, job_id: str, manifest: dict[str, Any], reason: str) -> RuntimeError:
    """Move a bad published result to ``failed/`` and build the logic
    failure the caller raises. Separated from the raise so every rejection
    site records the same way."""
    protocol.fail_marker(root, job_id, manifest=manifest, error=reason)
    return RuntimeError(f"offload {job_id}: {reason}")


def _resolve_or_park(
    ctx,
    *,
    stage: str,
    doc_id: str | None,
    config: dict[str, Any],
    expect: dict[str, Any],
    build_inputs: Callable[[], Iterable[tuple[str, bytes]]],
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Returns ``(result, done_dir, manifest)`` when the DEV worker has
    published a verified result; otherwise raises
    :class:`~trialerror.jobs.worker.EnvironmentalFailure` (parked) or
    ``RuntimeError`` (terminal, a logic failure)."""
    root = _root(ctx)
    job_id = protocol.validate_job_id(ctx.job_id)
    cfg_hash = config_hash(config)
    located = protocol.find_manifest(root, job_id)

    # -- step 1: a published result ---------------------------------------
    if located is not None and located[0] == "done":
        # SEC-1: prefer the sandbox's OWN copy of the manifest. The one in
        # done/<job>/ arrived through a directory the restricted DEV key
        # can write into; queued/ never was.
        manifest = protocol.trusted_manifest(root, job_id, located[1])
        error = protocol.published_error(root, job_id)
        if error is not None:
            _handle_worker_error(root, job_id, manifest, error, build_inputs)  # always raises
        if manifest.get("config_hash") != cfg_hash:
            # SEC-2. The published result answers a question this stage is
            # no longer asking: the marker was queued against a different
            # [ingest.*] table than the one loaded now. The design is
            # explicit that "SANDBOX owns the jobs ledger and the truth",
            # and the truth is the CURRENT configuration -- so the stale
            # result is discarded and the work re-queued under the current
            # hash, rather than folded in because it verifies against the
            # stale manifest that shipped with it. No offload attempt is
            # burned: the previous attempt did not fail, it answered a
            # superseded question.
            protocol.requeue_marker(
                root,
                job_id,
                manifest={**manifest, "config_hash": cfg_hash, "expect": dict(expect)},
                inputs=build_inputs(),
            )
            ctx.set_checkpoint({"offload": "requeued_config_change", "job_id": job_id, "stage": stage})
            raise EnvironmentalFailure(
                f"offload {job_id}: the published result was produced for a different "
                f"[ingest.{stage}] configuration than this program now has; discarded and "
                "re-queued for the DEV GPU",
                retry_delay_s=PARK_RETRY_DELAY_S,
            )
        try:
            result = protocol.verify_published(root, job_id, manifest)
        except protocol.OffloadVerificationError as exc:
            raise _reject(root, job_id, manifest, str(exc)) from exc
        return result, protocol.done_dir(root) / job_id, manifest

    # -- step 4: terminal ---------------------------------------------------
    if located is not None and located[0] == "failed":
        manifest = protocol.trusted_manifest(root, job_id, located[1])
        err = protocol.read_json(protocol.failed_dir(root) / job_id / protocol.ERROR_FILENAME)
        raise RuntimeError(
            f"offload {job_id}: terminal after {manifest.get('offload_attempts')} DEV attempt(s) -- "
            f"{err.get('error')} (inspect {protocol.failed_dir(root) / job_id}; delete that directory "
            "to let the stage queue a fresh attempt)"
        )

    # -- step 2: already queued or in flight -------------------------------
    if located is not None:
        ctx.set_checkpoint({"offload": located[0], "job_id": job_id, "stage": stage})
        raise EnvironmentalFailure(
            f"awaiting DEV GPU: offload {stage} job {job_id} is {located[0]} "
            "(run the GPU worker on DEV)",
            retry_delay_s=PARK_RETRY_DELAY_S,
        )

    # -- step 3: queue it ---------------------------------------------------
    protocol.queue_marker(
        root,
        job_id=job_id,
        stage=stage,
        doc_id=doc_id,
        expect=expect,
        config_hash=cfg_hash,
        inputs=build_inputs(),
    )
    ctx.set_checkpoint({"offload": "pending", "job_id": job_id, "stage": stage})
    raise EnvironmentalFailure(
        f"awaiting DEV GPU: offload {stage} job {job_id} queued (run the GPU worker on DEV)",
        retry_delay_s=PARK_RETRY_DELAY_S,
    )


def _handle_worker_error(
    root: Path,
    job_id: str,
    manifest: dict[str, Any],
    error: dict[str, Any],
    build_inputs: Callable[[], Iterable[tuple[str, bytes]]],
) -> None:
    """The DEV worker published an ``error.json`` instead of a result.

    Burn one OFFLOAD attempt. Under budget: re-queue (environmental, so the
    ledger row keeps its own attempts) -- inputs are rebuilt from the store
    rather than salvaged. Out of budget: the marker becomes terminal and
    the stage raises a logic failure, which the ledger consumes an attempt
    for; the next two claims hit the ``failed/`` branch above and consume
    the remaining two, so the row ends ``abandoned`` exactly as design
    acceptance C-fail requires."""
    attempts = int(manifest.get("offload_attempts", 0)) + 1
    max_attempts = int(manifest.get("max_attempts", protocol.DEFAULT_MAX_OFFLOAD_ATTEMPTS))
    manifest = {**manifest, "offload_attempts": attempts}
    reason = str(error.get("error") or "unknown DEV-side failure")
    if attempts >= max_attempts:
        protocol.fail_marker(root, job_id, manifest=manifest, error=reason)
        raise RuntimeError(
            f"offload {job_id}: DEV worker failed {attempts}/{max_attempts} times -- {reason}"
        )
    protocol.requeue_marker(root, job_id, manifest=manifest, inputs=build_inputs())
    raise EnvironmentalFailure(
        f"offload {job_id}: DEV attempt {attempts}/{max_attempts} failed ({reason}); re-queued",
        retry_delay_s=PARK_RETRY_DELAY_S,
    )


# ---------------------------------------------------------------------------
# the two stage entry points the handlers call
# ---------------------------------------------------------------------------
def offload_ocr_result(ctx, *, doc: dict[str, Any], raw_path: Path, ocr_cfg: dict[str, Any]):
    """Return an :class:`~trialerror.ingest.backends.OcrResult` built from
    the DEV worker's published pages, or park/fail the job."""
    from trialerror.ingest.backends import OcrPage, OcrResult

    input_name = ocr_input_name(raw_path)
    declared_pages = doc.get("page_count")
    expect = {
        "stage": "ocr",
        "backend": ocr_cfg.get("expect_backend") or None,
        "version": ocr_cfg.get("expect_version") or None,
        "outputs": [OCR_OUTPUT_NAME],
        "input_name": input_name,
        # Lane e1e Part B: the ``document.page_count`` column, when this
        # program has one for this document. A worker that chunks the
        # document into page ranges reads the page tree again to plan them,
        # and a disagreement means the file over there is not the file the
        # record is about -- the one check the two sides can make against
        # each other without shipping the document back.
        #
        # FIX V-7, so nobody leans on it harder than it holds: NOTHING counts
        # pages at registration. The column is written by one route in the
        # tree (the DjVu -> derived-PDF conversion in ``ingest/handlers.py``),
        # so a PDF acquired directly declares ``None`` here and the check
        # never fires for it. An absent number is not checked rather than
        # checked against zero, in both directions.
        "page_count": int(declared_pages) if isinstance(declared_pages, int) and declared_pages > 0 else None,
    }

    def build_inputs() -> list[tuple[str, bytes]]:
        return [(input_name, _read_input_under_program_root(ctx.store.program_root, raw_path))]

    result, done, manifest = _resolve_or_park(
        ctx,
        stage="ocr",
        doc_id=doc.get("doc_id"),
        config=ocr_cfg,
        expect=expect,
        build_inputs=build_inputs,
    )
    root = _root(ctx)
    job_id = ctx.job_id
    try:
        payload = json.loads((done / OCR_OUTPUT_NAME).read_text(encoding="utf-8"))
        pages_raw = payload["pages"]
        pages = [
            OcrPage(page_number=int(p["page_number"]), text=str(p["text"]))
            for p in pages_raw
        ]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _reject(root, job_id, manifest, f"unreadable {OCR_OUTPUT_NAME}: {exc}") from exc
    if not pages:
        raise _reject(root, job_id, manifest, f"{OCR_OUTPUT_NAME} contains no pages")
    ctx.set_checkpoint({"offload": "published", "ocr_pages": len(pages), "job_id": job_id})
    return OcrResult(
        pages=pages,
        ocr_backend=str(result.get("backend")),
        ocr_version=str(result.get("version")),
    )


def offload_embed_vectors(
    ctx,
    *,
    doc_id: str,
    chunks: Sequence[dict[str, Any]],
    embed_cfg: dict[str, Any],
    model_key: str,
    dims: int,
) -> dict[str, list[float]]:
    """Return ``{chunk_id: vector}`` for EVERY chunk of the document from
    the DEV worker's published vectors, or park/fail the job."""
    chunk_ids = [c["chunk_id"] for c in chunks]
    expect = {
        "stage": "embed",
        "model_key": model_key,
        "dims": int(dims),
        "chunk_count": len(chunk_ids),
        "chunk_ids": chunk_ids,
        "outputs": [EMBED_OUTPUT_NAME],
        "input_name": EMBED_INPUT_NAME,
    }

    def build_inputs() -> list[tuple[str, bytes]]:
        return [(EMBED_INPUT_NAME, build_chunks_payload(chunks))]

    _result, done, manifest = _resolve_or_park(
        ctx,
        stage="embed",
        doc_id=doc_id,
        config=embed_cfg,
        expect=expect,
        build_inputs=build_inputs,
    )
    root = _root(ctx)
    job_id = ctx.job_id
    vectors: list[list[float]] = []
    try:
        for line in (done / EMBED_OUTPUT_NAME).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                vectors.append([float(x) for x in json.loads(line)])
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _reject(root, job_id, manifest, f"unreadable {EMBED_OUTPUT_NAME}: {exc}") from exc

    if len(vectors) != len(chunk_ids):
        raise _reject(
            root,
            job_id,
            manifest,
            f"{EMBED_OUTPUT_NAME} has {len(vectors)} vector(s) for {len(chunk_ids)} chunk(s)",
        )
    bad = next((i for i, v in enumerate(vectors) if len(v) != int(dims)), None)
    if bad is not None:
        raise _reject(
            root,
            job_id,
            manifest,
            f"vector {bad} has {len(vectors[bad])} dimension(s), expected {dims}",
        )
    ctx.set_checkpoint(
        {"offload": "published", "embedded": len(vectors), "model_key": model_key, "job_id": job_id}
    )
    return dict(zip(chunk_ids, vectors))
