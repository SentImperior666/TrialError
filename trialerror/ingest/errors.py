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
