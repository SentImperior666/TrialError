"""Tests for ``trialerror.litapi.transport``: the ``FakeTransport`` offline
routing used by every other test in this package's suite, plus
``UrllibTransport``'s response-parsing helper (the actual socket call is
never exercised here -- that's the job of the skip-marked live-smoke
test, ``tests/test_litapi_live_smoke.py``)."""

from __future__ import annotations

import pytest

from trialerror.litapi.errors import TransportNotConfiguredError
from trialerror.litapi.transport import FakeTransport, TransportResponse, UrllibTransport


def test_fake_transport_returns_registered_response():
    transport = FakeTransport()
    transport.add_json("https://example.org/x", json_body={"hello": "world"})

    response = transport.get("https://example.org/x")

    assert response.status_code == 200
    assert response.json_body == {"hello": "world"}
    assert response.ok is True


def test_fake_transport_add_json_renders_text_too():
    transport = FakeTransport()
    transport.add_json("https://example.org/x", json_body={"a": 1})
    response = transport.get("https://example.org/x")
    assert response.text == '{"a": 1}'


def test_fake_transport_records_calls_including_headers():
    transport = FakeTransport()
    transport.add_json("https://example.org/x", json_body={})
    transport.get("https://example.org/x", headers={"x-api-key": "secret"})
    assert transport.calls == [{"url": "https://example.org/x", "headers": {"x-api-key": "secret"}}]


def test_fake_transport_unmatched_url_raises():
    transport = FakeTransport()
    with pytest.raises(TransportNotConfiguredError):
        transport.get("https://example.org/never-registered")


def test_fake_transport_non_2xx_status_ok_is_false():
    transport = FakeTransport()
    transport.add_json("https://example.org/missing", json_body={"error": "nope"}, status_code=404)
    response = transport.get("https://example.org/missing")
    assert response.status_code == 404
    assert response.ok is False


def test_transport_response_ok_boundary():
    assert TransportResponse(status_code=200).ok is True
    assert TransportResponse(status_code=299).ok is True
    assert TransportResponse(status_code=300).ok is False
    assert TransportResponse(status_code=199).ok is False


def test_urllib_transport_to_response_parses_json_body():
    resp = UrllibTransport._to_response(200, b'{"a": 1}', {"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json_body == {"a": 1}
    assert resp.text == '{"a": 1}'


def test_urllib_transport_to_response_handles_non_json_body():
    resp = UrllibTransport._to_response(500, b"not json", {})
    assert resp.status_code == 500
    assert resp.json_body is None
    assert resp.text == "not json"


def test_urllib_transport_to_response_handles_empty_body():
    resp = UrllibTransport._to_response(204, b"", {})
    assert resp.status_code == 204
    assert resp.json_body is None
    assert resp.text == ""
