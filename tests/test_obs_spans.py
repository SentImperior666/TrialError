"""``trialerror.obs.spans``: the span wrappers for the design's four Section 4.5
emission points. Uses a spy tracer (records span name + attributes, no real
network/SDK needed) to assert exact attribute shape, and the REAL
``trialerror.jobs``/``trialerror.artifacts``/``trialerror.budget`` modules (via the
``store``/``program_root`` fixtures from ``tests/conftest.py``) to prove
every ``traced_*`` wrapper calls the real underlying function UNCHANGED --
this is the "wrap run_one/ledger calls... WITHOUT touching trialerror/jobs/"
integration contract, verified from the outside: nothing in ``trialerror/jobs/``,
``trialerror/budget/``, or ``trialerror/artifacts/`` is imported for patching, only
called.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from trialerror.artifacts import gates
from trialerror.jobs import ledger
from trialerror.obs import semconv, spans, tracer
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


# ---------------------------------------------------------------------
# spy tracer -- records span name + attributes, no OTel/network involved
# ---------------------------------------------------------------------


class _RecordedSpan:
    def __init__(self, name: str, attributes: dict | None) -> None:
        self.name = name
        self.attributes: dict = dict(attributes or {})
        self.ended = False
        self.end_time = None

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value

    def set_attributes(self, attrs) -> None:
        self.attributes.update(attrs)

    def is_recording(self) -> bool:
        return not self.ended

    def end(self, end_time=None) -> None:
        self.ended = True
        self.end_time = end_time


class _SpyTracer:
    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []

    @contextmanager
    def start_as_current_span(self, name, attributes=None, **_kw):
        span = _RecordedSpan(name, attributes)
        self.spans.append(span)
        try:
            yield span
        finally:
            span.end()  # mirrors the real SDK's end_on_exit=True default

    def start_span(self, name, attributes=None, **_kw):
        span = _RecordedSpan(name, attributes)
        self.spans.append(span)
        return span


@pytest.fixture(autouse=True)
def spy(monkeypatch):
    """Every test in this module gets tracer.get_tracer() replaced with a
    fresh spy -- trialerror.obs.spans calls ``tracer.get_tracer()`` (attribute
    lookup at call time, not a bound import), so patching the attribute on
    the shared ``tracer`` module is what ``trialerror.obs.spans`` actually
    observes."""
    tracer.reset_for_tests()
    s = _SpyTracer()
    monkeypatch.setattr(tracer, "get_tracer", lambda: s)
    yield s
    tracer.reset_for_tests()


def _one_span(spy: _SpyTracer) -> _RecordedSpan:
    assert len(spy.spans) == 1
    return spy.spans[0]


# ---------------------------------------------------------------------
# launch (booked -> reconciled) -> invoke_agent
# ---------------------------------------------------------------------


def test_launch_span_attributes(spy):
    with spans.launch_span(
        launch_id="LNCH-x", agent_kind="researcher", model="sonnet", parent_launch="LNCH-parent", actual_tokens=123, program="PROG-1"
    ):
        pass
    span = _one_span(spy)
    assert span.name == "invoke_agent researcher"
    assert span.attributes[semconv.GEN_AI_OPERATION_NAME] == semconv.OP_INVOKE_AGENT
    assert span.attributes[semconv.GEN_AI_AGENT_NAME] == "researcher"
    assert span.attributes[semconv.GEN_AI_REQUEST_MODEL] == "sonnet"
    assert span.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] == 123
    assert span.attributes[semconv.TRIALERROR_LAUNCH_ID] == "LNCH-x"
    assert span.attributes[semconv.TRIALERROR_PARENT_LAUNCH] == "LNCH-parent"
    assert span.attributes[semconv.TRIALERROR_PROGRAM] == "PROG-1"
    assert span.ended is True


def test_launch_span_omits_none_valued_attributes(spy):
    with spans.launch_span(launch_id="LNCH-x", agent_kind="researcher", model="sonnet"):
        pass
    span = _one_span(spy)
    assert semconv.TRIALERROR_PARENT_LAUNCH not in span.attributes
    assert semconv.GEN_AI_USAGE_OUTPUT_TOKENS not in span.attributes


def test_launch_span_backdates_end_time_when_end_ts_given(spy):
    with spans.launch_span(launch_id="LNCH-x", agent_kind="a", model="m", start_ts="2026-01-01T00:00:00.000Z", end_ts="2026-01-01T00:05:00.000Z"):
        pass
    span = _one_span(spy)
    # 5 real minutes later, in nanoseconds.
    assert span.end_time == 300_000_000_000 + spans._ts_to_ns("2026-01-01T00:00:00.000Z")


def _make_launch(store, *, account_id, session_id, agent_kind="tester", model="sonnet", est_tokens=10) -> str:
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id,
            "account_id": account_id,
            "program_id": "PROG-test",
            "session_id": session_id,
            "agent_kind": agent_kind,
            "model_class": "top",
            "model": model,
            "purpose": "test",
            "est_tokens": est_tokens,
            "booked_ts": now(),
            "state": "PROVISIONAL",
        },
    )
    return launch_id


def _account_and_session(store) -> tuple[str, str]:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "test", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    return account_id, session_id


def test_traced_reconcile_launch_calls_the_real_reconcile_and_emits_a_span(store, spy):
    account_id, session_id = _account_and_session(store)
    launch_id = _make_launch(store, account_id=account_id, session_id=session_id)

    result = spans.traced_reconcile_launch(store, launch_id=launch_id, actual_tokens=77)

    # The REAL reconcile_launch ran -- same effect as calling it directly.
    assert result["state"] == "RECONCILED"
    assert result["actual_tokens"] == 77

    span = _one_span(spy)
    assert span.name == "invoke_agent tester"
    assert span.attributes[semconv.TRIALERROR_LAUNCH_ID] == launch_id
    assert span.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] == 77
    assert span.ended is True


def test_traced_reconcile_launch_configures_program_root_for_drop_bookkeeping(store, program_root, spy, monkeypatch):
    captured = {}
    monkeypatch.setattr(tracer, "configure", lambda **kw: captured.update(kw))
    account_id, session_id = _account_and_session(store)
    launch_id = _make_launch(store, account_id=account_id, session_id=session_id)
    spans.traced_reconcile_launch(store, launch_id=launch_id, actual_tokens=1, program_root=program_root)
    assert captured.get("program_root") == program_root


# ---------------------------------------------------------------------
# retrieval call -> retrieval
# ---------------------------------------------------------------------


def test_retrieval_span_attributes(spy):
    with spans.retrieval_span(query="what is a trialerror?", tiers=["fts", "vec"], k=8, program="PROG-1", result_chunk_ids=["CHK-1", "CHK-2"]):
        pass
    span = _one_span(spy)
    assert span.name == semconv.OP_RETRIEVAL
    assert span.attributes[semconv.GEN_AI_OPERATION_NAME] == semconv.OP_RETRIEVAL
    assert span.attributes[semconv.TRIALERROR_QUERY_HASH] == spans.query_hash("what is a trialerror?")
    assert span.attributes[semconv.TRIALERROR_TIERS] == ["fts", "vec"]
    assert span.attributes[semconv.TRIALERROR_K] == 8
    assert span.attributes[semconv.TRIALERROR_PROGRAM] == "PROG-1"
    assert span.attributes[semconv.TRIALERROR_RESULT_CHUNK_IDS] == ["CHK-1", "CHK-2"]


def test_retrieval_span_never_carries_the_raw_query_text(spy):
    """Design Section 4.5: "query hash", not the query itself -- verbatim-
    leak posture (see query_hash's own docstring)."""
    with spans.retrieval_span(query="a very specific verbatim query string"):
        pass
    span = _one_span(spy)
    assert "a very specific verbatim query string" not in span.attributes.values()


# ---------------------------------------------------------------------
# verification step / gate transition -> execute_tool
# ---------------------------------------------------------------------


def test_verification_span_attributes(spy):
    with spans.verification_span(procedure="citecheck", subject_id="ART-1", verdict="PASS"):
        pass
    span = _one_span(spy)
    assert span.name == "execute_tool citecheck"
    assert span.attributes[semconv.GEN_AI_OPERATION_NAME] == semconv.OP_EXECUTE_TOOL
    assert span.attributes[semconv.TRIALERROR_PROCEDURE] == "citecheck"
    assert span.attributes[semconv.TRIALERROR_SUBJECT_ID] == "ART-1"
    assert span.attributes[semconv.TRIALERROR_VERDICT] == "PASS"


def _open_gate_fixture(store) -> tuple[str, str, str]:
    """account + session + launch + template + artifact + a real,
    legitimately-opened (via trialerror.artifacts.gates.open_gate, UNCHANGED)
    draft gate. Returns (launch_id, artifact_id, gate_id)."""
    account_id, session_id = _account_and_session(store)
    launch_id = _make_launch(store, account_id=account_id, session_id=session_id)
    insert(store, "template", {"type_key": "note", "title": "Note", "version": "1", "path": "templates/note.md", "gated": 1})
    artifact_id = new_id("ART")
    insert(
        store,
        "artifact",
        {
            "artifact_id": artifact_id,
            "type": "note",
            "title": "test artifact",
            "path": "artifacts/test.md",
            "sha256": "2" * 64,
            "status": "draft",
            "registered_by_launch": launch_id,
        },
    )
    gate_row = gates.open_gate(store, artifact_id=artifact_id)
    return launch_id, artifact_id, gate_row["gate_id"]


def test_traced_advance_gate_draft_to_failed(store, spy):
    launch_id, _artifact_id, gate_id = _open_gate_fixture(store)
    result = spans.traced_advance_gate(store, gate_id=gate_id, to_state="failed", by_launch=launch_id)
    assert result["state"] == "failed"  # the real advance_gate ran

    span = _one_span(spy)
    assert span.name == "execute_tool gate.advance"
    assert span.attributes[semconv.TRIALERROR_SUBJECT_ID] == gate_id
    assert span.attributes[semconv.TRIALERROR_VERDICT] == "failed"  # attached post-call from the resulting row


def test_traced_gate_pass_flow_submit_verdict_union(store, spy):
    launch_id, _artifact_id, gate_id = _open_gate_fixture(store)

    submitted = spans.traced_submit_gate(store, gate_id=gate_id, by_launch=launch_id)
    assert submitted["state"] == "submitted"

    verdicted = spans.traced_record_verdict(store, gate_id=gate_id, verdict="PASS", by_launch=launch_id)
    assert verdicted["state"] == "gated"
    assert verdicted["verdict"] == "PASS"

    unioned = spans.traced_apply_union(store, gate_id=gate_id, by_launch=launch_id)
    assert unioned["state"] == "union_applied"

    assert len(spy.spans) == 3
    procedures = [s.attributes[semconv.TRIALERROR_PROCEDURE] for s in spy.spans]
    assert procedures == ["gate.submit", "gate.verdict", "gate.apply-union"]
    # verdict_label = row["verdict"] or row["state"] (see _traced_gate_call):
    # submit's row has no verdict yet -> falls back to its resulting state;
    # record_verdict's row now carries "PASS" -> that wins; apply_union's
    # row STILL carries "PASS" (nothing clears the verdict column on the
    # union_applied transition) -> that wins there too, not "union_applied".
    assert spy.spans[0].attributes[semconv.TRIALERROR_VERDICT] == "submitted"
    assert spy.spans[1].attributes[semconv.TRIALERROR_VERDICT] == "PASS"
    assert spy.spans[2].attributes[semconv.TRIALERROR_VERDICT] == "PASS"


def test_traced_gate_call_propagates_real_refusals(store, spy):
    """Tracing must never SWALLOW a real business-logic error either --
    only tracer-side failures are guarded (see spans._guarded's
    docstring)."""
    from trialerror.artifacts.errors import IllegalTransitionError

    launch_id, _artifact_id, gate_id = _open_gate_fixture(store)
    with pytest.raises(IllegalTransitionError):
        spans.traced_apply_union(store, gate_id=gate_id, by_launch=launch_id)  # draft -> union_applied is illegal
    # And the illegal attempt still emitted a span before raising? No --
    # apply_union raises INSIDE the traced call, before the row is
    # returned; the span should still have been opened (attempted) even
    # though the call inside it raised.
    assert len(spy.spans) == 1


# ---------------------------------------------------------------------
# job lifecycle -> one span per attempt
# ---------------------------------------------------------------------


def test_traced_run_one_completes_a_real_noop_job(store, spy):
    job = ledger.enqueue(store, kind="custom", payload={"handler": "noop"})
    result = spans.traced_run_one(store, job_id=job["job_id"], kind="custom", payload={"handler": "noop"})

    assert result["status"] == "complete"  # the real run_one ran

    span = _one_span(spy)
    assert span.attributes[semconv.TRIALERROR_JOB_ID] == job["job_id"]
    assert span.attributes[semconv.TRIALERROR_JOB_KIND] == "custom"
    assert span.attributes["trialerror.job_status"] == "complete"
    assert span.ended is True

    # The real ledger row is genuinely settled -- not a side effect of tracing.
    row = ledger.get_job(store, job["job_id"])
    assert row["state"] == "complete"


def test_traced_run_one_records_failure_class_on_a_logic_failure(store, spy):
    import tests._job_handlers  # noqa: F401 - registers "test_always_fails"

    job = ledger.enqueue(store, kind="custom", payload={"handler": "test_always_fails", "message": "boom"})
    result = spans.traced_run_one(store, job_id=job["job_id"], kind="custom", payload={"handler": "test_always_fails", "message": "boom"})
    assert result["status"] in ("failed", "abandoned")

    span = _one_span(spy)
    assert span.attributes[semconv.TRIALERROR_FAILURE_CLASS] == "logic"


def test_traced_run_one_idle_when_queue_empty(store, spy):
    result = spans.traced_run_one(store)
    assert result["status"] == "idle"
    span = _one_span(spy)
    assert span.attributes["trialerror.job_status"] == "idle"
