"""M2's own reference job handler. Registered here so
``discover_and_register_handlers`` (``trialerror.jobs.registry``) has at least
one real handler to find with zero other modules installed, and so a
future handler author (M7's OCR/embed/index/extract handlers) has a
minimal worked example of the ``JobContext`` contract to copy."""

from __future__ import annotations

from trialerror.jobs.registry import register_handler


@register_handler("noop")
def noop(ctx) -> None:  # ctx: trialerror.jobs.worker.JobContext
    """Claim, checkpoint once, and complete immediately -- a zero-work
    smoke-test handler for exercising the ledger/worker plumbing end to
    end (``trialerror jobs start-worker --job-id JOB-x --kind custom --payload
    '{"handler": "noop"}'``) without any real GPU/embedding backend."""
    ctx.set_checkpoint({"ran": True})
