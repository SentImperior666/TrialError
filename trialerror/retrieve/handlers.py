"""``probe_vector_canary`` -- the async job wrapping ``vector_canary``
(design Section 3.4): "Enqueued as a ``custom`` job with handler
``probe_vector_canary``: at SessionStart, at most once per hour; after each
ingest ``index`` job completes." Auto-discovered as ``trialerror.retrieve.
handlers`` by :func:`trialerror.jobs.registry.discover_and_register_handlers`,
the same convention M7's ``ocr``/``embed``/``index``/``extract`` handlers use.
"""

from __future__ import annotations

from trialerror.jobs.registry import register_handler

__all__ = ["run_probe_vector_canary", "enqueue_vector_canary_if_due", "default_host_label"]

#: Second fix round: the ``index`` job's completion enqueue (below) used to
#: land the canary as an ordinary, immediately-claimable ``pending`` row --
#: and ``claim_next``'s ``created_ts ASC`` ordering then let it jump ahead
#: of a real pipeline job created moments later (e.g. the ``index`` job an
#: adopted offload ``embed`` result enqueues next), changing the order real
#: work runs in. The ledger has no priority column, so this uses the
#: mechanism it does have: ``enqueue(..., defer_s=...)`` stamps
#: ``next_attempt_ts`` at creation, keeping the canary ineligible for a
#: short window -- long enough for the pipeline stage that triggered it to
#: enqueue its own next job first, short enough to stay well inside the
#: design's "at most once per hour" budget for this background probe.
_CANARY_DEFER_S = 60.0


def default_host_label() -> str:
    """This machine's hostname, or ``"default"`` if it cannot be
    determined.

    N-4 fix round: the ingest ``index`` job's own completion enqueue used to
    fall back to the bare literal ``"default"`` (its payload never carries a
    ``host`` key at all), while ``SessionStart`` resolves the real hostname
    -- so the SAME machine could enqueue ``probe_vector_canary`` jobs under
    two different host labels depending on which trigger fired, doubling
    the "at most once per hour" bucket and scattering ``probe_run`` rows
    across two labels that ``probes status --host <one-of-them>`` cannot
    see together. Both call sites now share this one resolution."""
    import socket

    try:
        return socket.gethostname() or "default"
    except OSError:
        return "default"


@register_handler("probe_vector_canary")
def run_probe_vector_canary(ctx) -> None:
    """B-1 fix round. Two bugs this used to have, both from being written
    against a test's own setup rather than a real worker process:

    1. A real worker (``trialerror.cli.jobs``'s own ``discover_and_register_
       handlers()`` + claim-run loop) starts with an EMPTY probe registry --
       nothing had ever imported :mod:`trialerror.retrieve.probes`. Calling
       :func:`trialerror.probes.registry.discover_and_register_probes` here
       fixes that, the same way ``session_start.py`` already does.
    2. The worker's own ``Store`` (``trialerror.cli.jobs``) is opened with
       the ordinary ``check_same_thread=True`` default -- and
       :func:`trialerror.probes.registry.run_probes` runs each probe body on
       its own thread for the timeout. Using
       :func:`trialerror.probes.registry.run_probe_inline` instead runs the
       probe directly on THIS thread (the job's own lease/heartbeat already
       bounds how long a job may run), so no cross-thread connection is
       ever required.
    """
    from trialerror.probes.registry import ProbeContext, discover_and_register_probes, run_probe_inline
    from trialerror.util.config import resolve_program_id

    discover_and_register_probes()
    payload = ctx.payload
    host = payload.get("host") or default_host_label()
    program_root = ctx.store.program_root
    program_id = resolve_program_id(program_root) if program_root is not None else None
    probe_ctx = ProbeContext(
        host=host, platform_store=ctx.store, store=ctx.store,
        program_root=program_root, program_id=program_id,
    )
    run_probe_inline(probe_ctx, "vector_canary")


def _hour_bucket_job_id(host: str, *, ts: str) -> str:
    """A deterministic job id, one per (host, calendar hour) -- design
    Section 3.4: "at SessionStart, at most once per hour." The job ledger's
    own PRIMARY KEY does the de-duplication: a second enqueue call in the
    same hour collides on this id and is refused, no separate rate-limit
    bookkeeping needed."""
    bucket = ts[:13].replace("-", "").replace("T", "")  # "YYYYMMDDHH"
    return f"JOB-probe-vector-canary-{host}-{bucket}"


def enqueue_vector_canary_if_due(store, *, host: str) -> bool:
    """Best-effort, idempotent per (host, hour) enqueue of the
    ``probe_vector_canary`` job. Returns ``True`` iff THIS call actually
    created a new job (``False`` for "already enqueued this hour" and for
    any other failure) -- always swallows its own errors, because both call
    sites (``SessionStart``, the ``index`` job's own completion) must never
    be broken by this being best-effort, background work."""
    from trialerror.jobs.ledger import enqueue
    from trialerror.stores.errors import ValidationError
    from trialerror.util.timeutil import now

    job_id = _hour_bucket_job_id(host, ts=now())
    try:
        enqueue(
            store,
            kind="custom",
            payload={"handler": "probe_vector_canary", "host": host},
            job_id=job_id,
            defer_s=_CANARY_DEFER_S,
        )
        return True
    except ValidationError:
        return False
    except Exception:  # noqa: BLE001 - best-effort background scheduling only
        return False
