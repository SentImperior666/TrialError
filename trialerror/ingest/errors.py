"""Ingestion exceptions. Mirrors ``trialerror.stores.errors``/``trialerror.jobs.errors``'s
pattern: a common base class every caller that only cares "did this ingest
call fail" can catch, plus specific subclasses for callers that need to
branch on *why*."""

from __future__ import annotations

__all__ = [
    "IngestError",
    "PathOutOfTreeError",
    "UnsupportedMediaTypeError",
    "LicenseRouteRefusedError",
    "SourceNotFoundError",
    "DocumentNotFoundError",
    "InvalidRequestTransitionError",
    "RetractBlockedError",
    "ActiveEmbedKeyPurgeError",
    "PurgeIndexUnreadableError",
    "UnknownEmbedModelKeyError",
    "VectorIndexDimsConflictError",
    "VectorIndexUnreadableError",
    "ExtractError",
    "ChunkNotFoundError",
    "GroundingError",
    "CandidateNotFoundError",
    "CandidateNotPendingError",
    "UnresolvedEntityReferenceError",
    "DjVuToolMissingError",
    "DjVuConversionError",
    "DjVuOutputTooLargeError",
    "DjVuResumeMediaTypeError",
    "InvalidNormalizerOverrideError",
    "DocumentRetractedError",
    "DocumentQualityRefusedError",
    "QualityRefusalConfigError",
    "OcrPageRangeError",
    "PageRangeFlagMissingError",
    "PageRangeNumberingError",
    "PageCountMismatchError",
]


class IngestError(Exception):
    """Base class for every error the ``trialerror.ingest`` package raises."""


class PathOutOfTreeError(IngestError):
    """Design Section 6: "register refuses paths outside raw/inbox globs
    (the ops-manifest-as-source wart)". Raised when ``add_document`` is
    given a source file path that does not resolve under the program's
    configured raw/inbox roots."""


class UnsupportedMediaTypeError(IngestError):
    """``add_document`` was given a file whose extension/media type has no
    registered normalizer (design Section 6 stage 3's format-handler list)."""


class LicenseRouteRefusedError(IngestError):
    """Design Section 6 stage 1: "legitimacy bar enforced: `register`
    refuses `acquisition_route` outside the allowlist for the program's
    license posture"."""


class SourceNotFoundError(IngestError):
    """No ``source`` row exists with the given ``source_id``."""


class DocumentNotFoundError(IngestError):
    """No ``document`` row exists with the given ``doc_id``."""


class InvalidRequestTransitionError(IngestError):
    """A request-queue state transition (design Section 6: "wanted ->
    requested -> delivered -> verifying -> archived -> indexed (+
    rejected/failed)") was attempted from a state that does not permit it."""


class RetractBlockedError(IngestError):
    """``trialerror ingest retract`` would have destroyed knowledge that
    outlived the document's own derived rows -- ``claim`` rows anchored in
    it. Refused with a count rather than cascaded away: those claims are
    extraction output somebody accepted, and dropping them to satisfy a
    cleanup command is a decision for the operator, not for the verb.
    See :mod:`trialerror.ingest.retract`."""


class ActiveEmbedKeyPurgeError(IngestError):
    """``trialerror ingest purge-embeddings`` was aimed at the embed model
    key this program is CONFIGURED to write.

    ``emb``'s primary key is ``(chunk_sha256, model_key)``, so a purge is
    scoped to one key and the only safe target is a SUPERSEDED one. Purging
    the active key would delete the live search surface in a single
    transaction and leave every chunk unembedded -- which is also why the
    embed stage never deletes on its own (see
    :mod:`trialerror.ingest.purge`'s module docstring). Resolved through
    :func:`trialerror.ingest.checks.configured_embed_model_key`, the same
    function the ``embedding_missing``/``embedding_stale`` doctor checks
    use, so "the active key" is one answer in this codebase."""


class PurgeIndexUnreadableError(IngestError):
    """The superseded key's ``vec_chunks__<model_key>`` table exists but
    cannot be read, so its entries could not be removed with the ``emb``
    rows they index.

    Refused up front rather than half-done: a purge that dropped the
    embedding rows and left the vector index answering for them is the one
    outcome worse than not purging at all. In practice this means a
    ``sqlite-vec`` (``vec0``) program on a connection where the loadable
    extension would not load -- see
    :func:`trialerror.stores.vecindex.try_load_sqlite_vec`."""


class UnknownEmbedModelKeyError(IngestError):
    """``trialerror ingest reindex-vectors`` was aimed at a ``model_key``
    this program has never embedded or indexed under (no ``emb`` rows, no
    ``vec_index_registry`` row).

    Refused rather than answered with zeros: the whole point of naming a key
    on the command line is that the key is the thing being rebuilt, and a
    typo that silently "rebuilds" an empty index for ``qwen3-4``, reports
    success, and leaves ``qwen3-4b`` exactly as broken as before is the
    failure mode this verb exists to end. The message names the keys the
    record does carry. See :mod:`trialerror.ingest.reindex`."""


class VectorIndexDimsConflictError(IngestError):
    """One embedding ``model_key``'s ``emb`` rows carry more than one
    ``dims`` value, so there is no single-width vector table to build.

    ``emb``'s primary key is ``(chunk_sha256, model_key)`` and ``dims`` is an
    ordinary column, so re-pointing a key at a differently-dimensioned model
    without purging first leaves both widths under one key. Choosing between
    them is an operator's decision (purge the superseded width, or re-embed
    the corpus under one configuration), not a rebuild's guess."""


class VectorIndexUnreadableError(IngestError):
    """``reindex-vectors`` found the key's existing ``vec_chunks__<model_key>``
    table but could not read it, so it could not empty it before refilling.

    In practice a ``vec0`` virtual table on a connection where the
    ``sqlite-vec`` loadable extension would not load (see
    :func:`trialerror.stores.vecindex.try_load_sqlite_vec`) -- the same
    condition :class:`PurgeIndexUnreadableError` names, refused for the
    mirror-image reason: there the half-done state would be embeddings gone
    with their index still answering, here it would be a rebuild that fails
    on its own DELETE."""


class DocumentRetractedError(IngestError):
    """A pipeline stage was requeued for a RETRACTED document.

    Retraction is the corpus's only subtractive verb; every other verb here
    re-derives. ``ingest rechunk`` and ``re-embed`` would rebuild rows for a
    withdrawn document and rewrite ``document.status`` back to a healthy
    value, leaving a row that reads retracted and ``embedded`` at once; a
    ``normalize`` requeue goes furthest and re-derives elements from the raw
    file retraction deliberately keeps. Refused at
    :func:`trialerror.ingest.pipeline.requeue_stage`, the one door all three
    go through. Un-retracting is a decision, not a side effect of a repair
    command -- ``ingest add`` on the same raw file makes a NEW document, and
    that is the supported way back."""


class DocumentQualityRefusedError(IngestError):
    """A pipeline stage was requeued for a document the normalize stage
    REFUSED on extraction quality (``[ingest.quality] refuse_below``).

    The refusal's whole content is that nothing downstream chunks, embeds,
    indexes or cites this text, so an unguarded ``ingest rechunk`` is a
    working un-refuse: the chain runs to the end and the document reaches
    ``status = 'indexed'`` with the refusal record still on file, which is
    the same accident
    :class:`DocumentRetractedError` exists to prevent one door along.
    Refused at :func:`trialerror.ingest.pipeline.requeue_stage`.

    The two honest ways out are both statements the operator makes on
    purpose: relax (or drop) ``[ingest.quality] refuse_below`` and re-run
    the NORMALIZE stage, which re-measures the text and clears
    ``document.status = 'failed'`` when it now passes; or fix the
    extraction route and re-ingest the raw file."""


class QualityRefusalConfigError(IngestError):
    """An ``[ingest.quality] refuse_below`` value could not be read as a
    number.

    Fail-closed, for the same reason
    :func:`trialerror.ingest.handlers._load_config` is (design D13): this is
    the one knob in ``[ingest.quality]`` that can stop an ingest, so an
    unreadable value must never be silently replaced by some other number.
    The WARN-side thresholds keep their forgiving coercion -- a mistyped
    warn bound cannot stop anything."""


# ---------------------------------------------------------------------------
# trialerror.ingest.extract (design Section 6 stage 8 / Section 11 v1 deliverable:
# "full entity/relation extraction + merge review + graph retrieval tier")
# ---------------------------------------------------------------------------


class ExtractError(IngestError):
    """Base class for every error :mod:`trialerror.ingest.extract` raises (a
    structural refusal in the extraction/merge-review pipeline, not a data
    quality judgment call -- those are the judge's to make)."""


class ChunkNotFoundError(ExtractError):
    """No ``chunk`` row exists with the given ``chunk_id`` (or it has no
    ``quote_anchor`` yet -- extraction requires the ``chunk`` stage to have
    already run, design Section 6 stage 5)."""


class GroundingError(ExtractError):
    """An extraction candidate's ``quote`` is missing, or is not an EXACT
    verbatim substring of its source chunk's text -- refused rather than
    silently accepted, per the mission's own "every one carrying its
    quote-anchor evidence" contract (an extraction cannot be evidence-
    anchored to a quote that was never actually said)."""


class CandidateNotFoundError(ExtractError):
    """No pending-review-queue ``record`` row (or ``merge_proposal`` row)
    exists with the given id."""


class CandidateNotPendingError(ExtractError):
    """``accept``/``reject`` was called against a candidate (or merge
    proposal) that is no longer ``pending``/``draft`` -- the merge-review
    queue's own "never silent auto-merge" contract means a decision, once
    made, is not silently redone."""


class UnresolvedEntityReferenceError(ExtractError):
    """A relation candidate's ``src``/``dst`` entity name has no matching
    ``entity`` row yet at accept time -- the referenced entity candidate(s)
    must be accepted first (never auto-resolved/auto-created here, same
    "never silent auto-merge" posture)."""


# ---------------------------------------------------------------------------
# trialerror.ingest.normalize_djvu (DjVu ingest normalizer)
# ---------------------------------------------------------------------------


class DjVuToolMissingError(IngestError):
    """Neither ``shutil.which`` nor a ``[ingest.djvu]`` ``trialerror.toml``
    override could find the DjVuLibre executable (``ddjvu`` or ``djvutxt``)
    the ``djvu`` job stage needs. Raised, never a bare traceback --
    :func:`trialerror.jobs.worker.run_one` settles this the same way any
    other handler exception settles: ``failure_class='logic'``, the job row
    carries the message, the worker process itself never crashes."""


class DjVuConversionError(IngestError):
    """``ddjvu``/``djvutxt`` exited non-zero, or ``ddjvu`` produced no PDF
    at the expected path. The message carries the head of the tool's own
    stderr (design: "named error with ddjvu's stderr head") so the failed
    ``job.last_error`` is actionable without re-running anything."""


class DjVuOutputTooLargeError(IngestError):
    """The PDF ``ddjvu`` produced from a ``.djvu``/``.djv`` source exceeds
    the configured size cap (``[ingest.djvu] max_pdf_bytes``, default
    :data:`trialerror.ingest.normalize_djvu.DEFAULT_DJVU_MAX_PDF_BYTES`) --
    refused rather than handed into the normalize/OCR route, mirroring
    :mod:`trialerror.webfetch.handlers`'s own ``_REPO_MAX_PDF_BYTES`` cap on a
    fetched PDF."""


class DjVuResumeMediaTypeError(IngestError):
    """Fix pass (VERIFY_ingest-djvu.md F6): a resumed ``djvu`` job
    (``run_djvu``'s "already converted" branch -- ``document.media_type``
    is no longer ``'djvu'``) found a ``media_type`` the conversion could
    never have produced. Only ``'pdf-text'``/``'pdf-scan'`` are valid
    there; anything else means this document never actually went through
    the ``djvu`` stage (a hand-crafted ``requeue_stage``/CLI payload
    pointing it at the wrong document, or a row rewritten by something
    else since). Misuse-only -- not reachable from the shipped CLI -- but
    named rather than silently guessing ``ocr`` for an unrelated document."""


class InvalidNormalizerOverrideError(IngestError):
    """Fix pass (VERIFY_ingest-djvu.md F11): ``payload['normalizer_id_override']``
    on a ``normalize``/``ocr`` job did not match the small allowlist of
    normalizer ids this codebase actually produces. Before the DjVu lane,
    ``document.normalizer_id`` could only ever hold the generic
    ``trialerror.ingest.normalizers`` constants; the override plumbing added
    for ``djvu-ddjvu`` must not become a way for an arbitrary hand-written
    job payload (``trialerror jobs start-worker --payload '{...}'``) to
    stamp free text onto a first-class provenance column."""


# ---------------------------------------------------------------------------
# page-range chunked OCR (lane e1e Part B)
# ---------------------------------------------------------------------------


class OcrPageRangeError(IngestError):
    """Base class for the refusals the page-range chunked OCR path raises.

    Every one of them is a statement that the concatenation this backend was
    about to return cannot be trusted to be the document. That is worth its
    own family: an OCR run is the one stage whose output nothing downstream
    can check -- a chunker cannot tell a book with a hundred pages missing
    from a book that is a hundred pages shorter -- so the only place a
    coverage mistake can be caught is here, at the seam that made it."""


class PageRangeFlagMissingError(OcrPageRangeError):
    """The installed ``marker_single`` does not advertise the page-range flag
    this backend was going to chunk with (``[ingest.ocr] page_range_flag``,
    probed once against ``--help``).

    Named rather than attempted: running the flag blind would either be
    rejected as an unknown argument (a failed job with a confusing message)
    or, worse, silently ignored -- and a range invocation whose range
    argument is ignored converts every one of N ranges into a full-document
    run, which is the memory failure this whole path exists to avoid,
    repeated N times."""


class PageRangeNumberingError(OcrPageRangeError):
    """The pages a range produced do not sit where that range's pages must.

    Which page a ``{N}`` marker names under a page range is a fact about the
    marker RELEASE: some number from the start of the invocation
    (``relative``), and marker-pdf 1.10.x numbers by the page's own index in
    the document (``absolute``). ``[ingest.ocr] page_range_numbering``
    selects one, or (``auto``, the default) resolves it once per document
    from the ranges' own output. This is raised when that cannot be done
    honestly:

    * a marker no convention can place (outside both windows), or one
      invocation whose markers prove both at once -- which is what a release
      numbering pages from 1 rather than 0 looks like;
    * two ranges of one document proving different conventions;
    * a document none of whose ranges after the first can prove either, so
      ``auto`` would have to guess;
    * a marker outside the window of a convention the program FORCED;
    * a concatenation that is not strictly increasing (two ranges produced
      the same page, or produced them out of order).

    Blank pages legitimately leave GAPS -- marker emits no body for them and
    this backend drops them, as the unchunked path always has -- so a gap is
    not an error and an overlap is."""


class PageCountMismatchError(OcrPageRangeError):
    """The document this backend read has a different number of pages than
    the job's manifest declared (``expect.page_count``).

    The sandbox counted the pages when it registered the document; the
    worker counts them again when it plans the ranges. The two disagreeing
    means the bytes on the GPU machine are not the bytes the record is
    about, and every page number in the result would be an assertion about
    the wrong document."""
