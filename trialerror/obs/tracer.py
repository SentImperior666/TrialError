"""The tracer singleton: OTel SDK setup + the Arize Phoenix OTLP/HTTP sink,
or a graceful no-op when the ``obs`` extra isn't installed. Design Section
4.5: "Sink: Arize Phoenix run locally ... ELv2 -- cleared for internal use,
never resold ... Emission is fire-and-forget over OTLP/HTTP to localhost
with a 250ms budget and silent drop if Phoenix is down: tracing must never
block operations."

TRIALERROR-DEV-NOTE (Phoenix as a served tool, not vendored code): this module
talks to Phoenix ONLY as an OTLP/HTTP receiver over the network (the same
protocol any OTel-instrumented app would use against any OTel collector) --
nothing from the ``arize-phoenix`` package is imported here or anywhere in
``trialerror/obs/``. That is the design's ELv2 license boundary in Section 12's
own words: "use as a served tool, do not fork/embed its code." ``trialerror obs
start-phoenix`` (``trialerror/cli/obs.py``) launches the real ``phoenix`` console
script as a subprocess for exactly the same reason -- it is invoked, never
imported.

No-op contract: every public function in this module is safe to call
whether or not ``opentelemetry-sdk`` / ``opentelemetry-exporter-otlp-proto-
http`` are importable. :data:`OTEL_AVAILABLE` records which case a given
process is in; :func:`get_tracer` hands back a real
:class:`opentelemetry.sdk.trace.Tracer` in one case and a structurally
identical (same two call shapes: ``start_as_current_span`` context manager,
``start_span``) do-nothing stand-in in the other -- callers in
``trialerror.obs.spans`` never branch on which they got.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from trialerror.obs import state

__all__ = [
    "OTEL_AVAILABLE",
    "DEFAULT_OTLP_ENDPOINT",
    "EXPORT_TIMEOUT_S",
    "ENV_ENDPOINT",
    "is_available",
    "resolve_endpoint",
    "probe_reachable",
    "configure",
    "get_tracer",
    "noop_tracer",
    "noop_span",
    "flush",
    "shutdown",
    "reset_for_tests",
]

#: Arize Phoenix's own default: ``phoenix serve`` listens on :6006 for both
#: its UI and its bundled OTLP/HTTP collector, which accepts spans POSTed to
#: ``/v1/traces`` -- the standard OTLP/HTTP trace-signal path. Verified live
#: against the installed ``arize-phoenix`` package at M12 build time
#: (``phoenix.config.get_env_port() == 6006``).
DEFAULT_OTLP_ENDPOINT = "http://localhost:6006/v1/traces"

#: design Section 4.5: "OTLP/HTTP to localhost with a 250ms budget."
EXPORT_TIMEOUT_S = 0.25

#: Overrides :data:`DEFAULT_OTLP_ENDPOINT` -- e.g. a non-default Phoenix
#: port, or a remote collector for a later v1 deployment shape. Read once,
#: at :func:`configure` time (same "env var override, no argv" convention
#: ``trialerror.stores.paths.platform_root`` uses for ``TRIALERROR_PLATFORM_ROOT``).
ENV_ENDPOINT = "TRIALERROR_OBS_OTLP_ENDPOINT"

try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the subprocess-hidden-deps test
    OTEL_AVAILABLE = False


def is_available() -> bool:
    """Whether ``opentelemetry-sdk`` + the OTLP/HTTP exporter are
    importable in THIS process. ``trialerror.obs.checks``' ``obs_exporter_
    reachable`` check reports ``skip`` (not ``fail``) when this is
    ``False`` -- the ``obs`` extra is optional by design (Section 12's M12
    row / the M2 integration contract: "core install must stay dependency-
    light; everything no-ops cleanly without them")."""
    return OTEL_AVAILABLE


def resolve_endpoint(endpoint: str | None = None) -> str:
    return endpoint or os.environ.get(ENV_ENDPOINT) or DEFAULT_OTLP_ENDPOINT


def probe_reachable(endpoint: str | None = None, *, timeout_s: float = 0.5) -> bool:
    """Plain TCP connect probe against ``endpoint``'s host:port (default:
    :func:`resolve_endpoint`'s own resolution) -- ``True`` if something is
    listening, ``False`` otherwise. Deliberately NOT an OTel/HTTP round trip
    (a POSTed span could still be refused downstream) -- reachable only
    tells you SOMETHING is bound to that port, which is exactly the
    granularity two callers need: ``trialerror.obs.checks.check_exporter_
    reachable``'s own doctor probe (refactored onto this shared helper,
    build-v2-polish O-4), and ``trialerror obs start-phoenix``'s idempotent-start
    check (``trialerror/cli/obs.py``) -- avoid spawning a second ``phoenix
    serve`` on top of a port already occupied by ANY listener, Phoenix or
    otherwise."""
    import socket
    from urllib.parse import urlparse

    resolved = resolve_endpoint(endpoint)
    parsed = urlparse(resolved)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


class _NoOpSpan:
    """Structurally matches the subset of ``opentelemetry.trace.Span``
    :mod:`trialerror.obs.spans` calls -- every method is a no-op returning
    ``None``, so span-wrapper code never has to branch on availability."""

    def set_attribute(self, *_a: Any, **_k: Any) -> None:
        pass

    def set_attributes(self, *_a: Any, **_k: Any) -> None:
        pass

    def record_exception(self, *_a: Any, **_k: Any) -> None:
        pass

    def set_status(self, *_a: Any, **_k: Any) -> None:
        pass

    def is_recording(self) -> bool:
        return False

    def end(self, *_a: Any, **_k: Any) -> None:
        pass


class _NoOpTracer:
    """Structurally matches ``opentelemetry.trace.Tracer``'s two span-
    creation call shapes. Handed back by :func:`get_tracer` whenever OTel
    deps are absent OR Phoenix emission was never configured -- the
    graceful degradation path the M12 build brief requires ("emission
    no-ops silently with Phoenix down")."""

    @contextmanager
    def start_as_current_span(self, _name: str, **_kw: Any) -> Iterator[_NoOpSpan]:
        yield _NoOpSpan()

    def start_span(self, _name: str, **_kw: Any) -> _NoOpSpan:
        return _NoOpSpan()


if OTEL_AVAILABLE:

    class _CountingOTLPExporter(OTLPSpanExporter):  # type: ignore[misc]
        """Wraps :class:`OTLPSpanExporter` to route every non-``SUCCESS``
        export result (Phoenix unreachable, timed out, refused) into
        :func:`trialerror.obs.state.record_span_drop` -- the ``obs_span_drop_
        counter`` doctor check's data source. ``export`` itself already
        catches its own network errors and returns ``SpanExportResult.
        FAILURE`` (verified against the installed ``opentelemetry-exporter-
        otlp-proto-http==1.44.0`` source at M12 build time: the HTTP POST is
        bounded by the constructor's own ``timeout``, with the internal
        retry-with-backoff loop itself deadline-checked against that same
        ``timeout`` -- so this class's ``try/except`` is defense-in-depth,
        not the primary path."""

        def __init__(self, *args: Any, program_root: Path | None = None, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._program_root = program_root

        def export(self, spans: Any) -> "SpanExportResult":
            try:
                result = super().export(spans)
            except Exception as exc:  # noqa: BLE001 - deliberate: emission must never raise
                state.record_span_drop(self._program_root, count=len(spans), reason=f"{type(exc).__name__}: {exc}")
                return SpanExportResult.FAILURE
            if result != SpanExportResult.SUCCESS:
                state.record_span_drop(self._program_root, count=len(spans), reason="exporter reported non-SUCCESS result")
            return result


_lock = threading.Lock()
_tracer: Any = None
_provider: Any = None
_configured_program_root: Path | None = None


def configure(
    *,
    endpoint: str | None = None,
    service_name: str = "trialerror",
    program_root: Path | str | None = None,
    timeout_s: float = EXPORT_TIMEOUT_S,
) -> None:
    """Idempotent process-wide setup. The first call wins; later calls
    (including the implicit one inside :func:`get_tracer`) are no-ops --
    matching the "one TracerProvider per process" contract the OTel SDK
    itself expects. Call :func:`reset_for_tests` between tests that need a
    fresh provider (e.g. to point at a different fixture endpoint)."""
    global _tracer, _provider, _configured_program_root
    with _lock:
        if _tracer is not None:
            return
        _configured_program_root = Path(program_root) if program_root is not None else None
        if not OTEL_AVAILABLE:
            _tracer = _NoOpTracer()
            return

        resolved_endpoint = resolve_endpoint(endpoint)
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = _CountingOTLPExporter(
            endpoint=resolved_endpoint, timeout=timeout_s, program_root=_configured_program_root
        )
        processor = BatchSpanProcessor(exporter, export_timeout_millis=int(timeout_s * 1000))
        provider.add_span_processor(processor)
        _provider = provider
        _tracer = provider.get_tracer("trialerror.obs", schema_url="https://opentelemetry.io/schemas/1.44.0")


def get_tracer() -> Any:
    """The tracer to create spans on. Configures with all-default settings
    (endpoint from :data:`ENV_ENDPOINT`/:data:`DEFAULT_OTLP_ENDPOINT`, no
    ``program_root`` -- meaning span-drop bookkeeping stays in-process-only,
    see ``trialerror.obs.state``) on first call if nothing configured it yet."""
    if _tracer is None:
        configure()
    return _tracer


def noop_tracer() -> Any:
    """A fresh no-op tracer instance, on demand -- ``trialerror.obs.spans``'
    ``_guarded``/``_guarded_manual`` helpers fall back to this if
    :func:`get_tracer`/span-creation itself raises for any reason, so a
    broken tracer can never stop the business code a span wraps from
    running (the M12 build brief's non-negotiable invariant)."""
    return _NoOpTracer()


def noop_span() -> Any:
    """A fresh no-op span instance, on demand -- see :func:`noop_tracer`."""
    return _NoOpSpan()


def flush(timeout_s: float = EXPORT_TIMEOUT_S) -> bool:
    """Bounded flush: forces the batch processor to hand its queued spans
    to the exporter NOW rather than waiting for its own schedule (default
    5s), capped at ``timeout_s``. Every ``trialerror.obs.spans`` wrapper calls
    this once its span ends -- without it, a short-lived CLI process (a
    single ``trialerror budget reconcile``, a ``trialerror jobs start-worker --mode
    once``) would very often exit before the batch processor's background
    thread ever got around to exporting, silently losing every span for a
    reason unrelated to Phoenix being reachable. Bounded the same 250ms as
    the exporter itself (design Section 4.5) so this can never become the
    thing that blocks operations. Returns ``True`` if nothing needed
    flushing, OTel isn't installed, or the queued spans were HANDED TO the
    exporter within the timeout; the SDK's own ``force_flush`` return value
    otherwise (``False`` only if the timeout elapsed before that handoff
    finished -- e.g. a very large backlog, not the common case). This is
    NOT the same thing as "Phoenix accepted them": whether the exporter's
    own POST actually succeeded is a separate, already-bounded concern
    :class:`_CountingOTLPExporter` owns (a failed POST still counts as
    "handed off" here, and is reported instead via ``trialerror.obs.state``'s
    span-drop counter)."""
    if _provider is None:
        return True
    try:
        return bool(_provider.force_flush(timeout_millis=int(timeout_s * 1000)))
    except Exception:  # noqa: BLE001 - deliberate: flushing must never raise
        return False


def shutdown(timeout_s: float = 1.0) -> None:
    """Flush + shut down the provider. Mainly a test/smoke-script
    convenience (``trialerror obs smoke`` calls this at the end of its run) --
    production CLI processes exit right after their own :func:`flush`
    call and don't need this."""
    global _tracer, _provider, _configured_program_root
    with _lock:
        if _provider is not None:
            try:
                _provider.shutdown()
            except Exception:  # noqa: BLE001 - deliberate: shutdown must never raise
                pass
        _tracer = None
        _provider = None
        _configured_program_root = None


def reset_for_tests() -> None:
    """Test-only: drop the singleton WITHOUT attempting a network shutdown
    call (unlike :func:`shutdown`) -- for tests that configured against a
    fixture endpoint that may already be gone."""
    global _tracer, _provider, _configured_program_root
    with _lock:
        _tracer = None
        _provider = None
        _configured_program_root = None
