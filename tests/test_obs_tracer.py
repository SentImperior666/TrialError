"""``trialerror.obs.tracer``: the process-wide tracer singleton. Exercises the
REAL OTel SDK + OTLP/HTTP exporter (installed in this dev venv) against a
real local HTTP collector (``tests._obs_fixtures.FakeOtlpCollector``) for
the success path, and against a closed port for the silent-drop/no-block
path. The genuinely-hidden-deps no-op path has its own dedicated
subprocess test (``tests/test_obs_noop_subprocess.py``) -- see that
module's docstring for why it can't just monkeypatch a flag here.
"""

from __future__ import annotations

import socket
import time

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from trialerror.obs import semconv, state, tracer
from tests._obs_fixtures import FakeOtlpCollector, span_attr


@pytest.fixture(autouse=True)
def _reset_tracer():
    tracer.reset_for_tests()
    state.reset_for_tests()
    yield
    tracer.reset_for_tests()
    state.reset_for_tests()


@pytest.fixture()
def collector():
    c = FakeOtlpCollector()
    endpoint = c.start()
    yield c, endpoint
    c.stop()


def _closed_port_endpoint() -> str:
    """A loopback port nothing is listening on -- bind, learn the OS-
    assigned port, then close immediately so the port is (almost
    certainly) refused-on-connect rather than merely slow/filtered."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1/traces"


def test_is_available_reflects_the_real_installed_deps():
    assert tracer.is_available() is True


def test_resolve_endpoint_defaults_to_localhost_6006():
    assert tracer.resolve_endpoint() == "http://localhost:6006/v1/traces"


def test_resolve_endpoint_env_var_override(monkeypatch):
    monkeypatch.setenv(tracer.ENV_ENDPOINT, "http://example.invalid:1234/v1/traces")
    assert tracer.resolve_endpoint() == "http://example.invalid:1234/v1/traces"


def test_resolve_endpoint_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv(tracer.ENV_ENDPOINT, "http://example.invalid:1234/v1/traces")
    assert tracer.resolve_endpoint("http://explicit:9/v1/traces") == "http://explicit:9/v1/traces"


def test_probe_reachable_true_against_a_real_open_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert tracer.probe_reachable(f"http://127.0.0.1:{port}/v1/traces") is True
    finally:
        srv.close()


def test_probe_reachable_false_when_nothing_listening():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listening now
    assert tracer.probe_reachable(f"http://127.0.0.1:{port}/v1/traces") is False


def test_probe_reachable_defaults_to_resolve_endpoint(monkeypatch):
    monkeypatch.setenv(tracer.ENV_ENDPOINT, "http://127.0.0.1:1/v1/traces")  # port 1: refused
    assert tracer.probe_reachable() is False


def test_configure_is_idempotent_first_call_wins(collector):
    _c, endpoint = collector
    tracer.configure(endpoint=endpoint)
    tracer.configure(endpoint="http://should-be-ignored.invalid:1/v1/traces")
    t = tracer.get_tracer()
    # A real SDK span records by default; the no-op stand-in's span never
    # does -- an API-level way to confirm the FIRST configure() (the real
    # collector endpoint) won, not the second (would still no-op-record
    # False only if get_tracer() somehow returned the NoOp stand-in).
    span = t.start_span("idempotency-probe")
    assert span.is_recording() is True
    span.end()


def test_span_round_trips_to_a_real_local_collector(collector):
    """The genuine round trip: create a real span with real GenAI
    attributes, flush it, and read back the ACTUAL decoded OTLP bytes a
    real collector received -- proves the exporter really serializes and
    POSTs what trialerror.obs.spans hands it, not just that no exception was
    raised."""
    c, endpoint = collector
    tracer.configure(endpoint=endpoint)
    t = tracer.get_tracer()
    with t.start_as_current_span(
        "invoke_agent obs-test-agent",
        attributes={
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_INVOKE_AGENT,
            semconv.GEN_AI_AGENT_NAME: "obs-test-agent",
            semconv.TRIALERROR_LAUNCH_ID: "LNCH-test123",
        },
    ):
        pass
    flushed = tracer.flush()
    assert flushed is True

    deadline = time.time() + 2.0
    while not c.all_spans() and time.time() < deadline:
        time.sleep(0.02)
    spans = c.all_spans()
    assert len(spans) == 1
    assert spans[0].name == "invoke_agent obs-test-agent"
    assert span_attr(spans[0], semconv.GEN_AI_AGENT_NAME) == "obs-test-agent"
    assert span_attr(spans[0], semconv.TRIALERROR_LAUNCH_ID) == "LNCH-test123"


def test_emission_noops_silently_and_quickly_when_phoenix_is_down():
    """Design Section 4.5 / M12 acceptance criterion: "emission no-ops
    silently with Phoenix down (timing test)". Also asserted end-to-end
    (via trialerror.obs.spans, not tracer directly) in
    tests/test_m12_acceptance.py."""
    tracer.configure(endpoint=_closed_port_endpoint())
    t = tracer.get_tracer()

    start = time.monotonic()
    with t.start_as_current_span("invoke_agent down-test"):
        pass
    tracer.flush()
    elapsed = time.monotonic() - start

    # Generous bound: the exporter's own timeout is 250ms (EXPORT_TIMEOUT_S)
    # with at most one immediate retry on a connection error (see
    # trialerror.obs.tracer._CountingOTLPExporter's docstring) -- a few seconds
    # is comfortably above any legitimate bounded path and comfortably
    # below what an accidental unbounded hang would look like. The real
    # "was it silently dropped" signal is the span-drop counter (see the
    # dedicated test below) -- force_flush()'s own return value means "was
    # the queue handed to the exporter in time", not "did Phoenix accept
    # it" (see trialerror.obs.tracer.flush's docstring), so it is deliberately
    # not asserted on here.
    assert elapsed < 5.0


def test_dropped_span_increments_the_in_process_counter_when_phoenix_is_down():
    tracer.configure(endpoint=_closed_port_endpoint())
    t = tracer.get_tracer()
    with t.start_as_current_span("invoke_agent down-test-2"):
        pass
    tracer.flush()
    assert state.process_drop_count() >= 1


def test_dropped_span_persists_to_program_root_state_when_configured(tmp_path):
    tracer.configure(endpoint=_closed_port_endpoint(), program_root=tmp_path)
    t = tracer.get_tracer()
    with t.start_as_current_span("invoke_agent down-test-3"):
        pass
    tracer.flush()
    persisted = state.read_span_drop_state(tmp_path)
    assert persisted["count"] >= 1


def test_successful_export_does_not_record_a_drop(collector):
    c, endpoint = collector
    tracer.configure(endpoint=endpoint, program_root=None)
    t = tracer.get_tracer()
    with t.start_as_current_span("invoke_agent success-test"):
        pass
    tracer.flush()
    time.sleep(0.05)
    assert state.process_drop_count() == 0


def test_flush_with_no_provider_configured_returns_true_harmlessly():
    # get_tracer() was never called in this test -> _provider stays None.
    assert tracer.flush() is True


def test_shutdown_is_safe_to_call_when_never_configured():
    tracer.shutdown()  # must not raise
