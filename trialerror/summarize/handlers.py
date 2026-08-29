"""The ``summarize`` job handler -- rides M2's ledger the same way M7's
``trialerror/ingest/handlers.py`` does (build brief: "envelope-producing, riding
the M2 ledger like extract does"). Auto-discovered by
``trialerror.jobs.registry.discover_and_register_handlers`` purely because this
file lives at ``trialerror/summarize/handlers.py`` (one direct subpackage of
``trialerror``, a ``handlers.py`` inside it) -- zero shared-file edits needed to
wire it in, the exact mechanism that module's own docstring describes.

**Why this handler never calls an LLM (mirrors M7's ``run_extract`` stub's
own reasoning, but this handler is NOT a stub -- it does real, useful
work without one):** no LLM-calling infrastructure exists in this offline
jobs/CLI layer (design Section 5.3: "one-shot orchestration lives [in
skills], not in servers"). What this handler DOES do, durably and
resumably:

1. Discover which subjects still need a summary (missing or stale --
   :func:`trialerror.summarize.api.find_stale_or_missing_document_summaries`),
   when the caller didn't name explicit targets.
2. Build a judgment envelope for each target
   (:func:`trialerror.summarize.api.build_summary_envelope`).
3. If the caller supplied a precomputed answer for a target (``payload
   ["judgments"][subject_id]`` -- an agent that already ran the real
   authoring step out-of-band, exactly the ``--judgments-file`` contract
   ``trialerror/cli/verify.py`` documents for citecheck/hypothesis), write it
   immediately via :func:`~trialerror.summarize.api.store_summary`.
4. Otherwise, the built envelope is recorded PENDING in the job's
   checkpoint -- ready for a caller with an agent handy to fill and
   resubmit (``trialerror summarize run --judgments-file ...``), never lost.

This is what "summaries can be batch-generated per corpus" (the build
brief) means in an offline-worker world: one durable, resumable pass over
the whole corpus that does everything EXCEPT the one step (authoring) that
structurally requires an LLM.

**Restart-safety** (design Section 6: "each idempotent, content-hash-keyed,
and resumable"): the auto-discovery path re-derives its target list from
the STORE ITSELF on every call (a resumed run's already-summarized
documents simply no longer appear in ``find_stale_or_missing_document_
summaries``'s output); the explicit-target path additionally skips any
target whose CURRENT summary already matches its CURRENT
``subject_sha256`` (nothing changed since it was last summarized) unless
the caller supplied a fresh judgment for it anyway. ``ctx.set_checkpoint``
is called after every target for liveness/heartbeat and an informational
progress payload, same division of labor ``trialerror.jobs.worker.JobContext.
set_checkpoint``'s own docstring describes.
"""

from __future__ import annotations

from typing import Any

from trialerror.jobs.registry import register_handler
from trialerror.summarize.api import DEFAULT_WORD_CAP, build_summary_envelope, find_stale_or_missing_document_summaries, get_summary, store_summary
from trialerror.summarize.errors import SummarizeError

__all__ = ["run_summarize"]


@register_handler("summarize")
def run_summarize(ctx) -> None:  # ctx: trialerror.jobs.worker.JobContext
    """Job payload shape (all of ``trialerror ingest``'s stage jobs' own
    convention: a flat JSON dict, no positional args):

    ``{"subject_kind": "document"|"collection" (default "document"),
    "created_by_launch": <LNCH-...>, "word_cap": int (default 150),
    "procedure_version": str (default "1"),
    "targets": [{"subject_id": ..., "doc_ids": [...]?}, ...]  -- omitted
    means auto-discover every document missing or stale a summary
    (subject_kind='document' only -- a 'collection' target set has no
    natural auto-discovery, since collections are caller-defined
    groupings; this handler raises if targets is omitted for
    subject_kind='collection'),
    "judgments": {subject_id: body_text, ...}  -- optional, precomputed
    answers for some or all targets}``
    """
    payload = ctx.payload
    store = ctx.store
    subject_kind = payload.get("subject_kind", "document")
    created_by_launch = payload["created_by_launch"]
    word_cap = int(payload.get("word_cap", DEFAULT_WORD_CAP))
    procedure_version = payload.get("procedure_version", "1")
    judgments: dict[str, str] = dict(payload.get("judgments") or {})
    targets = payload.get("targets")

    if targets is None:
        if subject_kind != "document":
            raise RuntimeError(
                "summarize: 'targets' omitted (auto-discovery) is only supported for "
                "subject_kind='document' -- a 'collection' target set is caller-defined "
                "and must be given explicitly in the job payload"
            )
        discovery = find_stale_or_missing_document_summaries(store)
        targets = [{"subject_id": doc_id} for doc_id in discovery["missing"] + discovery["stale"]]

    written: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def _checkpoint(i: int) -> None:
        ctx.set_checkpoint(
            {
                "processed": i,
                "total": len(targets),
                "written": dict(written),
                "pending": len(pending),
                "skipped": len(skipped),
            }
        )

    for i, target in enumerate(targets):
        subject_id = target["subject_id"]
        doc_ids = target.get("doc_ids")
        try:
            envelope = build_summary_envelope(
                store, subject_kind=subject_kind, subject_id=subject_id, doc_ids=doc_ids, word_cap=word_cap
            )
        except SummarizeError as exc:
            skipped.append({"subject_id": subject_id, "error": str(exc)})
            _checkpoint(i + 1)
            continue

        body = judgments.get(subject_id)
        existing = get_summary(store, subject_kind=subject_kind, subject_id=subject_id)
        already_current = existing is not None and existing["subject_sha256"] == envelope["subject_sha256"]
        if already_current and body is None:
            _checkpoint(i + 1)
            continue

        if body is not None:
            row = store_summary(
                store, envelope=envelope, body=body, issued_by_launch=created_by_launch, procedure_version=procedure_version
            )
            written[subject_id] = row["summary_id"]
        else:
            pending.append(envelope)
        _checkpoint(i + 1)

    ctx.set_checkpoint(
        {
            "processed": len(targets),
            "total": len(targets),
            "written": written,
            "pending_envelopes": pending,
            "skipped": skipped,
        }
    )
