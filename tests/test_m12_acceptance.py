"""M12 acceptance criteria (design Section 12's M12 row, verbatim):
"spans visible in local Phoenix for a scripted launch+search; emission
no-ops silently with Phoenix down (timing test)."

Acceptance -> test map
----------------------
- "spans visible in local Phoenix for a scripted launch+search"
  -> ``test_spans_visible_in_local_phoenix_for_a_scripted_launch_and_search``
     below (SKIPS with a clear reason if no local Phoenix is actually
     reachable at collection time -- see that test's docstring for why a
     protocol-level accept from a REAL Phoenix process, not a fake
     collector, is what this test insists on proving; this build's report
     records a manually-run instance of exactly this scenario as the
     live-round-trip proof).
- "emission no-ops silently with Phoenix down (timing test)"
  -> ``test_emission_noops_silently_with_phoenix_down_timing`` below (no
     skip condition -- always runs, needs no external process).

Both exercise the PUBLIC ``trialerror.obs.spans`` wrapper API end to end (not
``trialerror.obs.tracer`` directly, which ``tests/test_obs_tracer.py`` already
covers at that lower level) -- this file is the "does the actual M12
deliverable meet its own stated bar" check.
"""

from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from trialerror.obs import spans, state, tracer

pytestmark = pytest.mark.acceptance


@pytest.fixture(autouse=True)
def _reset():
    tracer.reset_for_tests()
    state.reset_for_tests()
    yield
    tracer.reset_for_tests()
    state.reset_for_tests()


def _closed_port_endpoint() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1/traces"


def _phoenix_reachable(endpoint: str, timeout_s: float = 0.5) -> bool:
    parsed = urlparse(endpoint)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout_s):
            return True
    except OSError:
        return False


def test_emission_noops_silently_with_phoenix_down_timing():
    """The scripted scenario: a launch span (booked->reconciled) and a
    retrieval span ("launch+search"), emitted through the PUBLIC
    ``trialerror.obs.spans`` API against an endpoint nothing is listening on.
    Must complete quickly (never block the workflow it wraps) AND must
    record the drop -- "no-ops silently" means no exception/no visible
    effect on the caller, NOT "nothing happened at all"."""
    tracer.configure(endpoint=_closed_port_endpoint())

    start = time.monotonic()
    with spans.launch_span(launch_id="LNCH-accept-1", agent_kind="researcher", model="sonnet", actual_tokens=500):
        pass
    with spans.retrieval_span(query="acceptance test query", tiers=["fts", "vec"], k=10, result_chunk_ids=["CHK-1"]):
        pass
    elapsed = time.monotonic() - start

    # No exception reached this point (the assertion above would never run
    # otherwise) -- that IS "no-ops silently" for the caller's workflow.
    # Bounded and fast: comfortably above the 250ms export budget (with its
    # one same-timeout retry) and comfortably below an accidental hang.
    assert elapsed < 5.0
    # And genuinely observable as dropped, not merely un-observed:
    assert state.process_drop_count() >= 2


def test_spans_visible_in_local_phoenix_for_a_scripted_launch_and_search():
    """Runs for real against ``trialerror.obs.tracer.DEFAULT_OTLP_ENDPOINT``
    (localhost:6006) IF a local Phoenix is actually up there -- start one
    with ``trialerror obs start-phoenix`` (or plain ``phoenix serve``) before
    running this test to exercise it for real; it SKIPS cleanly otherwise,
    which is the expected state for most CI/other-builder environments
    (Phoenix is an opt-in local dev tool, not a test dependency this suite
    should require -- design Section 12's own "graceful NO-OP degradation"
    principle applied to the test suite itself).

    What "visible" is checked against: :class:`trialerror.obs.tracer.
    _CountingOTLPExporter` records a span-drop ONLY on a non-``SUCCESS``
    OTLP export result (see its own docstring) -- so
    ``state.process_drop_count() == 0`` after a flush against a real
    Phoenix process is a genuine protocol-level "Phoenix's OTLP/HTTP
    collector accepted this batch" proof, not merely "no exception was
    raised". This build's own report separately records the human-facing
    confirmation (Phoenix's UI/API actually listing the spans) from a
    manually-run instance of this exact scenario.
    """
    endpoint = tracer.resolve_endpoint()
    if not _phoenix_reachable(endpoint):
        pytest.skip(f"no local Phoenix reachable at {endpoint!r} -- run `trialerror obs start-phoenix` first to exercise this live")

    tracer.configure(endpoint=endpoint)

    # The scripted scenario, named in the design's own acceptance text:
    # "a launch" then "+search" (a retrieval call).
    with spans.launch_span(
        launch_id="LNCH-accept-live", agent_kind="researcher", model="sonnet", actual_tokens=321, program="PROG-m12-accept"
    ):
        pass
    with spans.retrieval_span(query="m12 acceptance live search", tiers=["fts", "vec"], k=5, program="PROG-m12-accept"):
        pass

    flushed = tracer.flush(timeout_s=2.0)  # a real Phoenix's first request can be slower than the 250ms operational budget
    assert flushed is True
    assert state.process_drop_count() == 0, "Phoenix rejected or failed to accept the scripted spans"
