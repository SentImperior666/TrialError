"""Span wrappers for the design's four emission points (Section 4.5's
table). This is the "decorator/context-manager layer in your own package
that other modules opt into" the M2 integration note calls for: NOTHING in
``trialerror/jobs/``, ``trialerror/budget/``, or ``trialerror/artifacts/`` is edited or
monkeypatched. Two shapes are offered, per call site:

- A **generic context manager** (``retrieval_span``, ``verification_span``)
  for a subsystem that doesn't exist yet at M12 build time (M8's
  ``trialerror/retrieve/``) or whose call shape this module shouldn't presume to
  own (a future verification step outside the gate state machine) -- the
  future module imports the context manager itself and wraps its own hot
  path, opt-in, with zero coupling back to ``trialerror.obs``.
- A **traced_* wrapper function** (``traced_run_one``, ``traced_advance_
  gate``, ``traced_submit_gate``, ``traced_record_verdict``, ``traced_
  apply_union``, ``traced_reconcile_launch``) for a subsystem that DOES
  already exist (M2's jobs ledger, M10's gate state machine, M3's budget
  reconcile): calls the real function from its real module, unchanged, from
  inside a span. A caller that wants tracing calls the ``traced_*`` name
  instead of the underlying one; a caller that doesn't, still calls the
  underlying one directly -- ``trialerror/jobs/worker.py`` etc. never know this
  module exists.

Every wrapper here is safe to call with zero OTel/Phoenix deps installed
(see ``trialerror.obs.tracer``'s no-op contract) and never lets a tracing/
flush failure change the wrapped call's own return value or exception --
the one invariant the M12 build brief states as non-negotiable ("tracing
must never break a workflow").
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from trialerror.obs import semconv, tracer
from trialerror.util.timeutil import parse

__all__ = [
    "launch_span",
    "traced_reconcile_launch",
    "retrieval_span",
    "verification_span",
    "traced_advance_gate",
    "traced_submit_gate",
    "traced_record_verdict",
    "traced_apply_union",
    "job_attempt_span",
    "traced_run_one",
    "query_hash",
]


def _attrs(**kwargs: Any) -> dict[str, Any]:
    """Drop ``None``-valued kwargs and JSON-encode anything that isn't an
    OTel attribute-safe scalar/sequence-of-scalars (the SDK raises on
    e.g. a bare ``list[dict]`` or ``dict`` attribute value)."""
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)):
            out[k] = v
        elif isinstance(v, (list, tuple)) and all(isinstance(x, (str, bool, int, float)) for x in v):
            out[k] = list(v)
        else:
            out[k] = json.dumps(v, ensure_ascii=False, default=str)
    return out


def _ts_to_ns(ts: str) -> int:
    """``trialerror.util.timeutil.now()``-shaped ISO-8601 string -> epoch
    nanoseconds, the unit ``Span.start_time``/``end_on_exit`` traffic in."""
    return int(parse(ts).timestamp() * 1_000_000_000)


def query_hash(query: str) -> str:
    """Design Section 4.5: "retrieval call | ``retrieval`` | query hash,
    ...". A hash, not the raw query text, is what rides the span attribute
    -- the same untrusted/no-verbatim-leak posture the design applies to
    quoted source text elsewhere (Section 7's serving-path license fence),
    applied here to whatever a user typed into a retrieval query."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


@contextmanager
def _guarded(start: "Any") -> Iterator[Any]:
    """The M12 build brief's non-negotiable invariant, in one place:
    "tracing must never break a workflow." ``start`` is a zero-arg callable
    returning an ``opentelemetry.trace.Tracer.start_as_current_span(...)``-
    shaped context manager. Both the call to ``start()`` AND its
    ``__enter__()`` are individually guarded -- a plain ``with
    t.start_as_current_span(...):`` would NOT catch a failure inside
    ``__enter__()`` (that is where an OTel ``@contextmanager``-based
    tracer's actual span-creation logic runs, on first ``next()``, not on
    the bare call that constructs the generator) -- so this is the
    intentionally more defensive shape, not a stylistic preference. On
    ANY failure from either step this falls back to a local no-op span
    (:func:`trialerror.obs.tracer.noop_span`) instead of propagating, and the
    caller's own business code (the ``yield`` body) still runs exactly as
    if nothing were being traced. Exceptions raised BY that business code
    are untouched -- only the tracer-side enter/exit calls are guarded.
    Also owns the post-span :func:`trialerror.obs.tracer.flush` call, guarded
    the same way, so every span-wrapper function below gets it for free."""
    cm = None
    span = None
    try:
        cm = start()
        span = cm.__enter__()
    except Exception:  # noqa: BLE001 - deliberate: see docstring
        cm = None
        span = tracer.noop_span()
    try:
        yield span
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - deliberate: see docstring
                pass
        try:
            tracer.flush()
        except Exception:  # noqa: BLE001 - deliberate: see docstring
            pass


@contextmanager
def _guarded_manual(start: "Any", end: "Any") -> Iterator[Any]:
    """:func:`_guarded`'s sibling for the manual ``start_span``/``span.end
    (end_time=...)`` shape :func:`launch_span` needs (a backdated span
    can't use ``start_as_current_span``'s implicit exit-time end -- see
    that function's own docstring). ``start``/``end`` are zero-arg /
    one-arg (``span``) callables; same guarantee as :func:`_guarded`: a
    failure in either never stops the ``yield`` body from running, and
    never leaks past this function."""
    try:
        span = start()
    except Exception:  # noqa: BLE001 - deliberate: see _guarded's docstring
        span = tracer.noop_span()
    try:
        yield span
    finally:
        try:
            end(span)
        except Exception:  # noqa: BLE001 - deliberate: see _guarded's docstring
            pass
        try:
            tracer.flush()
        except Exception:  # noqa: BLE001 - deliberate: see _guarded's docstring
            pass


# ---------------------------------------------------------------------
# launch (booked -> reconciled) -> invoke_agent
# ---------------------------------------------------------------------


@contextmanager
def launch_span(
    *,
    launch_id: str,
    agent_kind: str,
    model: str,
    parent_launch: str | None = None,
    actual_tokens: int | None = None,
    program: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> Iterator[Any]:
    """Generic ``invoke_agent`` span for one launch. ``start_ts``/``end_ts``
    (``trialerror.util.timeutil.now()``-shaped ISO-8601 strings), if given,
    backdate the span to the launch's real ``booked_ts``/``reconciled_ts``
    instead of "now" -- a launch is usually reported on well after it
    actually ran (:func:`traced_reconcile_launch` below is the caller that
    does this in practice).

    TRIALERROR-DEV-NOTE (usage split): design Section 4.5 names ``gen_ai.usage.
    {input,output}_tokens`` (from reconcile) as the launch span's usage
    attributes. ``platform.launch`` (Section 4.3 DDL) tracks exactly ONE
    settled number, ``actual_tokens`` -- there is no input/output split in
    the schema to draw from. Rather than fabricate a split, this emits
    ``gen_ai.usage.output_tokens`` only, carrying the schema's one real
    number; ``gen_ai.usage.input_tokens`` is left unset. A real split would
    need a schema change outside M12's lane (``trialerror/stores/`` is M1's).
    """
    attrs = _attrs(
        **{
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_INVOKE_AGENT,
            semconv.GEN_AI_AGENT_NAME: agent_kind,
            semconv.GEN_AI_REQUEST_MODEL: model,
            semconv.GEN_AI_USAGE_OUTPUT_TOKENS: actual_tokens,
            semconv.TRIALERROR_LAUNCH_ID: launch_id,
            semconv.TRIALERROR_PARENT_LAUNCH: parent_launch,
            semconv.TRIALERROR_PROGRAM: program,
        }
    )
    span_kwargs: dict[str, Any] = {"attributes": attrs}
    if start_ts is not None:
        span_kwargs["start_time"] = _ts_to_ns(start_ts)

    # start_span (NOT start_as_current_span): a launch is reported well
    # after it ran (see traced_reconcile_launch below) -- end_time must be
    # settable to the launch's real reconciled_ts, not call-time "now",
    # which only Span.end(end_time=...) (not a context manager's implicit
    # exit-time end) supports.
    def _start() -> Any:
        return tracer.get_tracer().start_span(f"{semconv.OP_INVOKE_AGENT} {agent_kind}", **span_kwargs)

    def _end(span: Any) -> None:
        span.end(end_time=_ts_to_ns(end_ts) if end_ts is not None else None)

    with _guarded_manual(_start, _end) as span:
        yield span


def traced_reconcile_launch(store: Any, *, launch_id: str, program_root: str | Path | None = None, **kwargs: Any) -> dict:
    """Calls the real ``trialerror.budget.pools.reconcile_launch`` UNCHANGED,
    then emits the launch's ``invoke_agent`` span backdated across its
    whole ``booked_ts -> reconciled_ts`` lifetime -- the one point in the
    launch's life where every Section 4.5 attribute (model, agent_kind,
    settled tokens) is simultaneously on hand. Import-local (not top-level)
    so ``trialerror.obs`` never becomes an import-time dependency of
    ``trialerror.budget`` or vice versa -- pure opt-in, per this module's
    docstring."""
    from trialerror.budget.pools import reconcile_launch
    from trialerror.stores import get as store_get

    if program_root is not None:
        # Best-effort, idempotent (a no-op if some earlier call already
        # configured the tracer): wires span-drop bookkeeping to THIS
        # program root so `trialerror doctor --program-root ...` can see it (see
        # trialerror.obs.state's module docstring for why that needs a file at
        # all). Must happen before the launch_span() call below, whose
        # first tracer.get_tracer() would otherwise configure with no
        # program_root and win by first-caller-wins idempotency.
        tracer.configure(program_root=program_root)
    result = reconcile_launch(store, launch_id=launch_id, **kwargs)
    row = store_get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    if row is None:  # defensive: reconcile_launch itself would already have raised
        return result
    with launch_span(
        launch_id=launch_id,
        agent_kind=row.get("agent_kind"),
        model=row.get("model"),
        parent_launch=row.get("parent_launch"),
        actual_tokens=row.get("actual_tokens"),
        program=row.get("program_id"),
        start_ts=row.get("booked_ts"),
        end_ts=row.get("reconciled_ts"),
    ):
        pass
    return result


# ---------------------------------------------------------------------
# retrieval call -> retrieval  (generic: M8 doesn't exist yet at M12 time)
# ---------------------------------------------------------------------


@contextmanager
def retrieval_span(
    *,
    query: str,
    tiers: Sequence[str] | None = None,
    k: int | None = None,
    program: str | None = None,
    result_chunk_ids: Sequence[str] | None = None,
) -> Iterator[Any]:
    """Design Section 4.5: "retrieval call | ``retrieval`` | query hash,
    tiers used, k, result chunk_ids (bounded), trialerror.program". Bounded per
    the design's own parenthetical: ``result_chunk_ids`` (if given at
    context-exit time via ``span.set_attribute`` -- see usage note below)
    should already be truncated by the caller before being attached; this
    function does not itself cap the sequence, since it has no opinion on
    the right bound for a subsystem it doesn't own.

    M8 (``trialerror/retrieve/``, not yet built) is the intended caller: wrap
    the hybrid fts->vec->RRF engine's top-level query function in this
    context manager, passing ``result_chunk_ids`` once the ranked result
    set is known (either up front, or via ``span.set_attribute(trialerror.obs.
    semconv.TRIALERROR_RESULT_CHUNK_IDS, [...])`` before the ``with`` block
    exits)."""
    attrs = _attrs(
        **{
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_RETRIEVAL,
            semconv.TRIALERROR_QUERY_HASH: query_hash(query),
            semconv.TRIALERROR_TIERS: list(tiers) if tiers is not None else None,
            semconv.TRIALERROR_K: k,
            semconv.TRIALERROR_PROGRAM: program,
            semconv.TRIALERROR_RESULT_CHUNK_IDS: list(result_chunk_ids) if result_chunk_ids is not None else None,
        }
    )
    with _guarded(lambda: tracer.get_tracer().start_as_current_span(semconv.OP_RETRIEVAL, attributes=attrs)) as span:
        yield span


# ---------------------------------------------------------------------
# verification step / gate transition -> execute_tool
# ---------------------------------------------------------------------


@contextmanager
def verification_span(*, procedure: str, subject_id: str, verdict: str | None = None) -> Iterator[Any]:
    """Design Section 4.5: "verification step | ``execute_tool`` |
    procedure, subject_id, verdict label". Generic -- used directly by
    :func:`traced_advance_gate` et al. below, and available for M9's
    citecheck/hypothesis pipeline (also not yet built) to import the same
    way M8 imports :func:`retrieval_span`."""
    attrs = _attrs(
        **{
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_EXECUTE_TOOL,
            semconv.TRIALERROR_PROCEDURE: procedure,
            semconv.TRIALERROR_SUBJECT_ID: subject_id,
            semconv.TRIALERROR_VERDICT: verdict,
        }
    )
    name = f"{semconv.OP_EXECUTE_TOOL} {procedure}"
    with _guarded(lambda: tracer.get_tracer().start_as_current_span(name, attributes=attrs)) as span:
        yield span


def _traced_gate_call(fn_name: str, procedure: str, store: Any, *, gate_id: str, **kwargs: Any) -> dict:
    """Shared body for the four ``traced_*_gate``/``traced_record_verdict``
    wrappers below: call the real ``trialerror.artifacts.gates`` function
    UNCHANGED inside a ``verification_span``, then attach the resulting
    ``to_state``/``verdict`` as the span's verdict label. Import-local for
    the same reason as :func:`traced_reconcile_launch`."""
    from trialerror.artifacts import gates

    fn = getattr(gates, fn_name)
    with verification_span(procedure=procedure, subject_id=gate_id) as span:
        row = fn(store, gate_id=gate_id, **kwargs)
        verdict_label = row.get("verdict") or row.get("state")
        span.set_attribute(semconv.TRIALERROR_VERDICT, verdict_label or "")
        return row


def traced_advance_gate(store: Any, *, gate_id: str, to_state: str, by_launch: str, **kwargs: Any) -> dict:
    return _traced_gate_call("advance_gate", "gate.advance", store, gate_id=gate_id, to_state=to_state, by_launch=by_launch, **kwargs)


def traced_submit_gate(store: Any, *, gate_id: str, by_launch: str, **kwargs: Any) -> dict:
    return _traced_gate_call("submit_gate", "gate.submit", store, gate_id=gate_id, by_launch=by_launch, **kwargs)


def traced_record_verdict(store: Any, *, gate_id: str, verdict: str, **kwargs: Any) -> dict:
    return _traced_gate_call("record_verdict", "gate.verdict", store, gate_id=gate_id, verdict=verdict, **kwargs)


def traced_apply_union(store: Any, *, gate_id: str, by_launch: str, **kwargs: Any) -> dict:
    return _traced_gate_call("apply_union", "gate.apply-union", store, gate_id=gate_id, by_launch=by_launch, **kwargs)


# ---------------------------------------------------------------------
# job lifecycle -> one span per attempt
# ---------------------------------------------------------------------


@contextmanager
def job_attempt_span(*, job_id: str, kind: str | None = None) -> Iterator[Any]:
    """Design Section 4.5: "job lifecycle | span per attempt | trialerror.
    job_id, kind, failure_class, checkpoint progress". One span per
    :func:`trialerror.jobs.worker.run_one` call -- ``run_one`` claims and
    settles exactly one attempt, so this context manager's lifetime IS one
    attempt by construction (see :func:`traced_run_one`)."""
    attrs = _attrs(**{semconv.GEN_AI_OPERATION_NAME: semconv.OP_EXECUTE_TOOL, semconv.TRIALERROR_JOB_ID: job_id, semconv.TRIALERROR_JOB_KIND: kind})
    name = f"job {kind or '?'}"
    with _guarded(lambda: tracer.get_tracer().start_as_current_span(name, attributes=attrs)) as span:
        yield span


def traced_run_one(store: Any, *, program_root: str | Path | None = None, **kwargs: Any) -> dict:
    """Calls the real ``trialerror.jobs.worker.run_one`` UNCHANGED, wrapped in
    one ``job_attempt_span``. ``run_one``'s own return shape (``{"status":
    ..., "job_id": ..., "worker_id": ...}``) doesn't carry ``kind``/
    ``failure_class``/``checkpoint`` for a job it settled -- those are read
    back off the ledger row (``trialerror.jobs.ledger.get_job``) once the call
    returns, the same "read back what changed" shape
    :func:`traced_reconcile_launch` uses for the launch row."""
    from trialerror.jobs import ledger

    if program_root is not None:
        tracer.configure(program_root=program_root)  # see traced_reconcile_launch's identical note

    job_id = kwargs.get("job_id")
    kind = kwargs.get("kind")
    with job_attempt_span(job_id=job_id or "?", kind=kind) as span:
        result = _call_run_one(store, **kwargs)
        settled_id = result.get("job_id")
        if settled_id:
            span.set_attribute(semconv.TRIALERROR_JOB_ID, settled_id)
            row = ledger.get_job(store, settled_id)
            if row is not None:
                span.set_attribute(semconv.TRIALERROR_JOB_KIND, row.get("kind") or "")
                if row.get("failure_class"):
                    span.set_attribute(semconv.TRIALERROR_FAILURE_CLASS, row["failure_class"])
                if row.get("attempts") is not None:
                    span.set_attribute(semconv.TRIALERROR_JOB_ATTEMPT, row["attempts"])
                if row.get("checkpoint"):
                    span.set_attribute(semconv.TRIALERROR_CHECKPOINT_PROGRESS, row["checkpoint"])
        span.set_attribute("trialerror.job_status", result.get("status") or "")
    return result


def _call_run_one(store: Any, **kwargs: Any) -> dict:
    from trialerror.jobs.worker import run_one

    return run_one(store, **kwargs)
