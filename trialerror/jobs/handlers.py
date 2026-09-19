"""M2's own reference job handler, plus the ideation framework's scheduled
convergent-discovery re-check. Registered here so
``discover_and_register_handlers`` (``trialerror.jobs.registry``) has at least
one real handler to find with zero other modules installed, and so a
future handler author (M7's OCR/embed/index/extract handlers) has a
minimal worked example of the ``JobContext`` contract to copy.

Both handlers ride the ledger as ``kind='custom'`` jobs whose payload names
them: ``job.kind`` is a closed CHECK (``ocr``/``embed``/``index``/
``extract``/``ingest_batch``/``watch``/``custom``), so a new handler is a new
``handler`` key in a ``custom`` payload and not a schema migration. The
worker resolves it (``trialerror.jobs.worker.run_one``: a ``custom`` job reads
``payload["handler"]``).
"""

from __future__ import annotations

from typing import Any

from trialerror.jobs.registry import register_handler

#: What ``convergent_recheck`` re-checks by default: every status that has
#: been through the screen. A record with a dossier has something to measure
#: "new" against, and ``merged``/``eliminated`` rows stay in the reference
#: sets forever and stay worth re-checking — an eliminated idea whose twin
#: shows up in later work is exactly the finding this pass exists to log.
#:
#: ``raw`` is out because it has no dossier yet, and ``archived``
#: (schema-v9) is out for the same reason and a second one: an archived row
#: is a prior round's entry, the fixed background a round is judged against,
#: and it was never screened here at all. Naming both exclusions rather than
#: saying "every status except raw", which is what this list used to be.
DEFAULT_RECHECK_STATUSES: tuple[str, ...] = ("consolidated", "promoted", "merged", "eliminated")

#: How many ideas the handler re-checks between checkpoints. One: a
#: re-check of one idea is one retrieval and (under an external query mode)
#: one egress, and a job that can resume mid-round without re-issuing a
#: query it already made is worth a checkpoint write per idea.
CHECKPOINT_EVERY = 1


@register_handler("noop")
def noop(ctx) -> None:  # ctx: trialerror.jobs.worker.JobContext
    """Claim, checkpoint once, and complete immediately -- a zero-work
    smoke-test handler for exercising the ledger/worker plumbing end to
    end (``trialerror jobs start-worker --job-id JOB-x --kind custom --payload
    '{"handler": "noop"}'``) without any real GPU/embedding backend."""
    ctx.set_checkpoint({"ran": True})


def _build_provider(kind: str, program_root) -> tuple[Any, Any]:
    """Build the R5 provider a payload names, through the SAME builder the
    screen's CLI uses (``trialerror.cli.lens._build_external_provider``).

    Imported lazily and named here rather than re-implemented: that function
    is where the program's own index/key configuration is read, and a second
    copy of it would be a second place for "which index answered" to drift.
    It is also the seam a test monkeypatches to inject a static provider, so
    this handler is drivable without a network or an index on disk."""
    from trialerror.cli.lens import _build_external_provider

    return _build_external_provider(kind, program_root)


@register_handler("convergent_recheck")
def convergent_recheck(ctx) -> None:  # ctx: trialerror.jobs.worker.JobContext
    """The scheduled convergent-discovery re-check over one round's idea
    rows: retrieve each record against the corpus AS IT STANDS NOW and,
    under a query mode that names one, against the external index; write
    whatever is new as a ``convergent_with`` link.

    **It never re-scores.** Not a label, not a status, not a verdict row: the
    single write is ``trialerror.lens.novelty.record_convergent_links``, which
    reaches one column. That is the design's own rule — a convergence found
    after a round closed is logged, never penalised — and the reason it holds
    here is structural rather than careful: nothing else is reachable from
    this handler.

    Payload::

        {"handler": "convergent_recheck",
         "round_id": "<ROUND-id>",                  # required
         "external_query_mode": "none"|"neutral_abstract"|"statement",
         "external_provider": "none"|"arxiv-index"|"litapi",
         "statuses": ["consolidated", ...],          # default: every non-raw
         "idea_ids": ["IDEA-x", ...],                # default: the whole round
         "allow_unscreened": false,                  # re-check records with no dossier
         "corpus_k": 6, "corpus_mode": "vector", "external_k": 20,
         "candidate_hit_similarity": 0.5,
         "launch_id": "<LNCH-id>"}                   # for the egress audit line

    ``statuses`` applies to an explicit ``idea_ids`` list TOO: naming a record
    does not make it screened, and a raw record has no dossier to measure
    "new" against, so every neighbour would come back as a convergent
    discovery. ``allow_unscreened: true`` lifts both the status filter and the
    dossier precondition, deliberately and in the payload where it is on the
    record. Without it, a named record that has no dossier is skipped with the
    reason carried in the checkpoint, not silently linked and not a failed
    job.

    Resumable per idea: the checkpoint carries the ids already re-checked, so
    a job that is paused or loses its lease mid-round does not re-issue an
    external query it has already made. The same property makes a re-run
    cheap rather than duplicative.
    """
    from trialerror.jobs.errors import JobError
    from trialerror.lens.novelty import NoveltyError, recheck_idea_convergence

    payload = ctx.payload
    round_id = payload.get("round_id")
    if not round_id:
        raise JobError("convergent_recheck: payload is missing the required 'round_id'")
    statuses = tuple(payload.get("statuses") or DEFAULT_RECHECK_STATUSES)
    query_mode = str(payload.get("external_query_mode") or "none")
    provider_kind = str(payload.get("external_provider") or "none")
    if query_mode != "none" and provider_kind == "none":
        raise JobError(
            f"convergent_recheck: external_query_mode={query_mode!r} names a query but external_provider is "
            "'none', so there is nothing to issue it to"
        )
    if query_mode == "none" and provider_kind != "none":
        raise JobError(
            f"convergent_recheck: external_provider={provider_kind!r} builds a provider but "
            "external_query_mode is 'none', which issues no query -- R5 would be silently idle"
        )

    store = ctx.store
    allow_unscreened = bool(payload.get("allow_unscreened"))
    wanted = payload.get("idea_ids")
    status_ph = ",".join("?" for _ in statuses)
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        # The status filter applies to a named list too, unless the payload
        # asks for the unscreened reading: naming a record does not screen it.
        sql = f"SELECT idea_id FROM idea WHERE round_id = ? AND idea_id IN ({placeholders})"
        params: list[Any] = [round_id, *[str(i) for i in wanted]]
        if not allow_unscreened:
            sql += f" AND status IN ({status_ph})"
            params.extend(statuses)
        rows = store.knowledge.execute(sql + " ORDER BY created_ts ASC, idea_id ASC", params).fetchall()
    else:
        rows = store.knowledge.execute(
            f"SELECT idea_id FROM idea WHERE round_id = ? AND status IN ({status_ph}) "
            "ORDER BY created_ts ASC, idea_id ASC",
            [round_id, *statuses],
        ).fetchall()
    idea_ids = [r["idea_id"] for r in rows]

    checkpoint = ctx.checkpoint
    done: list[str] = list(checkpoint.get("done") or [])
    linked: dict[str, list[str]] = dict(checkpoint.get("linked") or {})
    remaining = [i for i in idea_ids if i not in set(done)]

    external = None
    close_external = None
    try:
        if provider_kind != "none":
            external, close_external = _build_provider(provider_kind, store.program_root)
        for idea_id in remaining:
            try:
                result = recheck_idea_convergence(
                    store,
                    idea_id=idea_id,
                    external=external,
                    external_query_mode=query_mode,
                    corpus_k=int(payload.get("corpus_k") or 6),
                    corpus_mode=str(payload.get("corpus_mode") or "vector"),
                    external_k=int(payload.get("external_k") or 20),
                    candidate_hit_similarity=float(payload.get("candidate_hit_similarity") or 0.5),
                    launch_id=payload.get("launch_id"),
                    allow_unscreened=allow_unscreened,
                )
            except NoveltyError as exc:
                # A refusal about ONE record is not a failed job: the rest of
                # the round is still worth re-checking, and the refusal is
                # carried in the checkpoint where the next run can see it.
                linked.setdefault("_refused", []).append(f"{idea_id}: {exc}")
                done.append(idea_id)
                ctx.set_checkpoint({"round_id": round_id, "done": done, "linked": linked, "n_ideas": len(idea_ids)})
                continue
            done.append(idea_id)
            if result["new"]:
                linked[idea_id] = [link["key"] for link in result["new"]]
            ctx.set_checkpoint({"round_id": round_id, "done": done, "linked": linked, "n_ideas": len(idea_ids)})
    finally:
        if close_external is not None:
            close_external()

    ctx.set_checkpoint(
        {
            "round_id": round_id,
            "done": done,
            "linked": linked,
            "n_ideas": len(idea_ids),
            "n_linked": sum(len(v) for k, v in linked.items() if k != "_refused"),
            "external_query_mode": query_mode,
            "complete": True,
        }
    )
