"""Not a test module (pytest only collects ``test_*.py``) -- a minimal,
real HTTP OTLP/trace collector used by the M12 suite instead of a full
``phoenix serve`` instance: fast, dependency-free beyond what ``trialerror[obs]``
already pulls in, and gives tests something to assert real decoded span
content against (name, attributes) rather than only "did export() return
SUCCESS". A real (manually-run) ``phoenix serve`` round trip is this
build's separate live-Phoenix smoke -- see the M12 build report.
"""

from __future__ import annotations

import gzip
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)


class FakeOtlpCollector:
    """A real (loopback) HTTP server speaking just enough OTLP/HTTP to let
    :class:`opentelemetry.exporter.otlp.proto.http.trace_exporter.
    OTLPSpanExporter` succeed against it. Always responds 200 -- this
    fixture exists to prove SHAPE (what a real exporter really sends), not
    to exercise failure handling (``FailingCollector`` below, and a closed
    port, cover that).
    """

    def __init__(self) -> None:
        self.received_requests: list[ExportTraceServiceRequest] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib method name
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                req = ExportTraceServiceRequest()
                req.ParseFromString(body)
                collector.received_requests.append(req)
                resp_bytes = ExportTraceServiceResponse().SerializeToString()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)

            def log_message(self, *_a: Any) -> None:  # silence stdlib's default stderr logging
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> str:
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1/traces"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def all_spans(self) -> list[Any]:
        """Flatten every span across every received request/resource/scope."""
        out = []
        for req in self.received_requests:
            for rs in req.resource_spans:
                for ss in rs.scope_spans:
                    out.extend(ss.spans)
        return out


def span_attr(span: Any, key: str) -> Any:
    """Pull one attribute's scalar value off a decoded OTLP ``Span`` proto
    by key, or ``None`` if absent -- a small convenience over the proto's
    ``AnyValue`` oneof shape."""
    for kv in span.attributes:
        if kv.key == key:
            v = kv.value
            which = v.WhichOneof("value")
            return getattr(v, which) if which else None
    return None
