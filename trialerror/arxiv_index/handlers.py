"""``arxiv_index_build`` job handler -- rides ``trialerror.jobs``'s ledger
exactly the way ``trialerror/ingest/handlers.py``'s handlers do (module
docstring there: "M7's job handlers -- ride M2's ledger"), auto-discovered
by :func:`trialerror.jobs.registry.discover_and_register_handlers` purely
because this file exists at ``trialerror/arxiv_index/handlers.py``.

**Why ``kind='custom'`` + ``payload['handler']='arxiv_index_build'``, not a
new first-class ``job.kind`` CHECK-constraint value:** the build brief asks
for "a jobs-ledger job kind (``arxiv_index_build``)" -- this handler IS
registered and addressed under exactly that name
(``trialerror.jobs.registry.get_handler("arxiv_index_build")``), but the ROW's
SQL ``job.kind`` column stays ``'custom'``. This is the repo's own existing
precedent, not a new pattern invented for this build:
``trialerror/stores/schema/jobs.py``'s own v2-migration TRIALERROR-DEV-NOTE records
that ``normalize``/``chunk`` "previously rode kind='custom' with
payload['handler'] set" before being promoted to first-class CHECK values
in a dedicated schema migration; ``trialerror/cli/summarize.py``'s batch job
still rides ``kind='custom'`` today (never promoted). Adding a THIRD schema
migration to ``trialerror/stores/schema/jobs.py`` (a file this build's mission
brief does not name and every other build session in this repo treats as
requiring its own dedicated migration-authoring session -- see that file's
own v2 TRIALERROR-DEV-NOTE for how much care a CHECK-constraint change there
takes, SQLite table-rebuild-under-FK included) is out of proportion to what
this feature needs; ``kind='custom'`` is the documented, already-supported
escape hatch for exactly this situation
(``trialerror.jobs.worker.run_one``'s own handler-resolution branch:
``if handler_name == "custom": handler_name = payload.get("handler")``).

Payload shape (``trialerror.cli.lit``'s ``arxiv-index build`` subcommand builds
this)::

    {
        "handler": "arxiv_index_build",
        "zip_path": "<path to the Kaggle zip>",
        "db_path": "<path to the standalone index db>",
        "dims": 3072,
        "batch_size": 500,
        "member_glob": "*.jsonl",
        "min_free_gb": 80.0,
        "field_map": null,            # optional override, see ingest.py
        "progress_event_every": 20,   # emit a progress event every N checkpoints
    }

Disk preflight (build brief item 3) runs ONCE per handler invocation,
before touching the zip at all -- a resumed call re-checks it too (free
space can change between attempts), never skipped on the assumption a
prior attempt already checked it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trialerror.arxiv_index.ingest import ArxivIndexIngestError, DEFAULT_BATCH_SIZE, DEFAULT_MEMBER_GLOB, build_index_from_zip
from trialerror.arxiv_index.store import DEFAULT_MIN_FREE_GB, disk_preflight, open_arxiv_index_db
from trialerror.events.api import append_event
from trialerror.jobs.registry import register_handler

__all__ = ["run_arxiv_index_build"]

DEFAULT_PROGRESS_EVENT_EVERY = 20


@register_handler("arxiv_index_build")
def run_arxiv_index_build(ctx) -> None:
    payload = ctx.payload
    zip_path = payload.get("zip_path")
    db_path = payload.get("db_path")
    if not zip_path or not db_path:
        raise ArxivIndexIngestError("arxiv_index_build job payload requires 'zip_path' and 'db_path'")
    if not Path(zip_path).is_file():
        raise ArxivIndexIngestError(f"arxiv_index_build: zip_path does not exist: {zip_path!r}")

    dims = int(payload.get("dims", 3072))
    batch_size = int(payload.get("batch_size", DEFAULT_BATCH_SIZE))
    member_glob = payload.get("member_glob", DEFAULT_MEMBER_GLOB)
    min_free_gb = float(payload.get("min_free_gb", DEFAULT_MIN_FREE_GB))
    field_map = payload.get("field_map")
    progress_event_every = int(payload.get("progress_event_every", DEFAULT_PROGRESS_EVENT_EVERY))

    disk_preflight(db_path, min_free_gb=min_free_gb)

    append_event(
        ctx.store,
        event_type="arxiv_index_build_started",
        payload={"zip_path": zip_path, "db_path": db_path, "dims": dims, "resumed": bool(ctx.checkpoint)},
        launch_id=payload.get("created_by_launch"),
    )

    conn = open_arxiv_index_db(db_path)
    call_count = {"n": 0}

    def on_progress(progress: dict[str, Any]) -> None:
        ctx.set_checkpoint(progress)
        call_count["n"] += 1
        if call_count["n"] % max(progress_event_every, 1) == 0:
            append_event(
                ctx.store,
                event_type="arxiv_index_build_progress",
                payload={
                    "rows_ingested": progress["rows_ingested"],
                    "rows_skipped": progress["rows_skipped"],
                    "current_member": progress["current_member"],
                },
                launch_id=payload.get("created_by_launch"),
            )

    try:
        final_progress = build_index_from_zip(
            conn,
            zip_path,
            dims=dims,
            batch_size=batch_size,
            member_glob=member_glob,
            field_map=field_map,
            checkpoint=ctx.checkpoint or None,
            on_progress=on_progress,
            _raise_after_rows=payload.get("_raise_after_rows"),  # test-only seam, see ingest.py docstring
        )
    finally:
        conn.close()

    append_event(
        ctx.store,
        event_type="arxiv_index_build_complete",
        payload={"rows_ingested": final_progress["rows_ingested"], "rows_skipped": final_progress["rows_skipped"]},
        launch_id=payload.get("created_by_launch"),
    )
    ctx.set_checkpoint(final_progress)
