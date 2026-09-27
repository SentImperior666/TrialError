"""Tests for the ``trialerror lit`` CLI group (``trialerror/cli/lit.py``). The
provider-plumbing (``_build_client``) is monkeypatched to a stub client
built from the SAME hand-written stubs ``test_litapi_client.py`` uses --
this file tests only the CLI-layer concerns (argparse wiring, envelope
shape, error-code mapping), not provider/reconciliation logic (covered
elsewhere)."""

from __future__ import annotations

import argparse
import urllib.error

import pytest

from trialerror.cli import lit as cli_lit
from trialerror.litapi.client import LitApiClient, LookupResult, SearchResult, build_default_providers
from trialerror.litapi.config import load_litapi_config
from trialerror.litapi.errors import AllProvidersFailedError
from trialerror.litapi.models import CitationEdge, CitationsPage, WorkRecord
from trialerror.util.envelope import PROTOCOL_VERSION


class _UnreachableTransport:
    """Every ``.get`` call raises the exact class of exception a real
    ``UrllibTransport`` would surface for a host an egress policy refuses
    (litapi-arxiv-https build, C-0093(a)): ``urllib.error.URLError``. Records
    every URL it was asked for so a test can assert which provider(s) were
    actually attempted."""

    def __init__(self):
        self.urls: list[str] = []

    def get(self, url, *, headers=None, timeout_s=None):
        self.urls.append(url)
        raise urllib.error.URLError("no route to host")


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        for k, v in kw.items():
            setattr(self, k, v)


class _StubClient:
    def lookup_doi(self, doi):
        return LookupResult(record=WorkRecord(title="T", doi=doi, providers=["stub"]), providers_succeeded=["stub"])

    def lookup_arxiv(self, arxiv_id):
        raise AllProvidersFailedError(f"no record for {arxiv_id}", details={"failures": []})

    def search(self, query, *, limit=10):
        return SearchResult(records=[WorkRecord(title=f"hit for {query}")], providers_succeeded=["stub"])

    def get_citations(self, identifier, *, limit=100, offset=0):
        return CitationsPage(items=[CitationEdge(title="Citer")], provider="stub", offset=offset, limit=limit)


def test_group_name_and_help_registered():
    assert cli_lit.GROUP_NAME == "lit"
    assert cli_lit.HELP


def test_register_wires_lookup_citations_search_subcommands():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    args = parser.parse_args(["lit", "lookup", "--doi", "10.1/x"])
    assert args.lit_cmd == "lookup"
    assert args.doi == "10.1/x"

    args2 = parser.parse_args(["lit", "citations", "--id", "10.1/x", "--limit", "5"])
    assert args2.lit_cmd == "citations"
    assert args2.limit == 5

    args3 = parser.parse_args(["lit", "search", "--query", "distributed systems"])
    assert args3.lit_cmd == "search"


def test_lookup_requires_exactly_one_of_doi_or_arxiv():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    with pytest.raises(SystemExit):
        parser.parse_args(["lit", "lookup"])  # neither --doi nor --arxiv

    with pytest.raises(SystemExit):
        parser.parse_args(["lit", "lookup", "--doi", "10.1/x", "--arxiv", "2101.00001"])  # both


def test_cmd_lookup_doi_ok(monkeypatch):
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _StubClient())
    args = _Args(doi="10.1/x", arxiv_id=None)

    env = cli_lit._cmd_lookup(args)

    assert env == {
        "ok": True,
        "command": "lit.lookup",
        "protocolVersion": PROTOCOL_VERSION,
        "result": {
            "record": {
                "title": "T", "doi": "10.1/x", "arxiv_id": None, "authors": [], "year": None, "venue": None,
                "abstract": None, "citation_count": None, "oa_pdf_url": None, "url": None, "external_ids": {},
                "providers": ["stub"], "other": {},
            },
            "providers_succeeded": ["stub"],
            "providers_failed": [],
            # lane FB-acq item 2: one entry per provider ASKED, whatever it
            # answered. The stub client builds its LookupResult by hand and so
            # reports none, which is exactly what an empty dict says.
            "provider_outcomes": {},
        },
        "nextActions": [],
        "meta": {},
    }


def test_cmd_lookup_arxiv_all_providers_failed_is_error_envelope(monkeypatch):
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _StubClient())
    args = _Args(doi=None, arxiv_id="9999.99999")

    env = cli_lit._cmd_lookup(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "AllProvidersFailedError"
    assert env["error"]["details"] == {"failures": []}


def test_cmd_lookup_transport_unreachable_is_structured_no_traceback_envelope(monkeypatch):
    """litapi-arxiv-https build, task 3: a real ``LitApiClient`` (built via
    ``build_default_providers``, exercising the REAL get_with_retry wrapping
    path -- not the hand-written ``_StubClient`` the other tests here use)
    backed by a transport whose ``.get`` always raises
    ``urllib.error.URLError`` must turn into a clean ``ok=false,
    code='transport_unreachable'`` envelope, never an uncaught exception."""
    transport = _UnreachableTransport()
    config = load_litapi_config({})
    providers = build_default_providers(config, transport=transport)  # DEFAULT_CLIENTS: openalex + semanticscholar
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: LitApiClient(providers))
    args = _Args(doi="10.1/x", arxiv_id=None)

    env = cli_lit._cmd_lookup(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "transport_unreachable"
    failures = env["error"]["details"]["failures"]
    assert {f["provider"] for f in failures} == {"openalex", "semanticscholar"}
    assert all(f["code"] == "transport_unreachable" for f in failures)
    assert all(f["host"] for f in failures)  # host attempted is preserved, not lost
    assert all(f["scheme"] == "https" for f in failures)
    # every provider was actually attempted (no traceback aborted the loop early)
    assert len(transport.urls) == 2


def test_cmd_search_transport_unreachable_is_structured_envelope(monkeypatch):
    transport = _UnreachableTransport()
    config = load_litapi_config({})
    providers = build_default_providers(config, transport=transport)
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: LitApiClient(providers))
    args = _Args(query="distributed systems", limit=10)

    env = cli_lit._cmd_search(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "transport_unreachable"


def test_cmd_citations_ok(monkeypatch):
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _StubClient())
    args = _Args(identifier="10.1/x", limit=20, offset=0)

    env = cli_lit._cmd_citations(args)

    assert env["ok"] is True
    assert env["result"]["provider"] == "stub"
    assert env["result"]["items"] == [
        {
            "title": "Citer", "doi": None, "arxiv_id": None, "year": None, "authors": [], "external_ids": {},
            # lane SI item A1: two new CitationEdge keys
            "work_type": None, "citation_count": None,
        }
    ]


def test_cmd_search_ok(monkeypatch):
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _StubClient())
    args = _Args(query="distributed systems", limit=10)

    env = cli_lit._cmd_search(args)

    assert env["ok"] is True
    assert env["result"]["records"][0]["title"] == "hit for distributed systems"


def test_load_program_config_raw_returns_empty_when_no_program_root():
    assert cli_lit._load_program_config_raw(None) == {}


def test_load_program_config_raw_returns_empty_when_no_trialerror_toml(tmp_path):
    assert cli_lit._load_program_config_raw(tmp_path) == {}


def test_load_program_config_raw_reads_real_config(tmp_path):
    (tmp_path / "trialerror.toml").write_text(
        '[program]\nid = "x"\n\n[litapi.openalex]\nmailto = "me@example.org"\n', encoding="utf-8"
    )
    raw = cli_lit._load_program_config_raw(tmp_path)
    assert raw["litapi"]["openalex"]["mailto"] == "me@example.org"


# ---------------------------------------------------------------------------
# lane FB-acq item 2: the error CODE now distinguishes rate-limited from
# not-found from unreachable, reading details["provider_outcomes"].
# ---------------------------------------------------------------------------


def _failed(message, **details):
    return AllProvidersFailedError(message, details=details)


class _FailingClient:
    def __init__(self, exc):
        self._exc = exc

    def lookup_doi(self, doi):
        raise self._exc

    def lookup_arxiv(self, arxiv_id):
        raise self._exc

    def search(self, query, *, limit=10):
        raise self._exc

    def get_citations(self, identifier, *, limit=100, offset=0):
        raise self._exc


def test_cmd_lookup_rate_limited_plus_not_found_is_code_rate_limited(monkeypatch):
    """FAILS BEFORE this lane: both of these came out as the generic
    AllProvidersFailedError, with the 429 only inside a message string."""
    exc = _failed(
        "no provider returned a record",
        failures=[{"provider": "openalex", "error": "HTTP 429", "code": "rate_limited", "status_code": 429,
                   "retry_after_s": 30.0}],
        provider_outcomes={
            "openalex": {"outcome": "rate_limited", "status_code": 429, "retry_after_s": 30.0, "keyed": False},
            "semanticscholar": {"outcome": "not_found", "status_code": 404, "retry_after_s": None, "keyed": True},
        },
    )
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _FailingClient(exc))

    env = cli_lit._cmd_lookup(_Args(doi="10.1000/example", arxiv_id=None))

    assert env["error"]["code"] == "rate_limited"
    po = env["error"]["details"]["provider_outcomes"]
    assert po["semanticscholar"]["outcome"] == "not_found"  # BOTH providers are named
    assert env["nextActions"] == [
        {"kind": "shell", "argv": ["trialerror", "doctor", "--only", "litapi_providers_ready"],
         "description": "rate-limited (openalex); keyless providers: openalex -- configure a key file "
                        "or retry after 30s"},
    ]


def test_cmd_lookup_every_provider_not_found_is_code_record_not_found(monkeypatch):
    exc = _failed(
        "no provider returned a record",
        failures=[],
        provider_outcomes={
            "openalex": {"outcome": "not_found"},
            "semanticscholar": {"outcome": "not_found"},
        },
    )
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _FailingClient(exc))

    env = cli_lit._cmd_lookup(_Args(doi="10.1000/example", arxiv_id=None))

    assert env["error"]["code"] == "record_not_found"
    assert env["nextActions"] == []


def test_cmd_lookup_mixed_failures_keep_the_generic_code(monkeypatch):
    exc = _failed(
        "no provider returned a record",
        failures=[{"provider": "openalex", "error": "HTTP 500", "status_code": 500}],
        provider_outcomes={
            "openalex": {"outcome": "http_error", "status_code": 500},
            "semanticscholar": {"outcome": "rate_limited", "status_code": 429},
        },
    )
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _FailingClient(exc))

    env = cli_lit._cmd_lookup(_Args(doi="10.1000/example", arxiv_id=None))

    assert env["error"]["code"] == "AllProvidersFailedError"
    assert env["error"]["details"]["provider_outcomes"]["semanticscholar"]["outcome"] == "rate_limited"


def test_cmd_search_and_citations_share_the_rate_limited_mapping(monkeypatch):
    exc = _failed(
        "no provider could search",
        failures=[],
        provider_outcomes={"openalex": {"outcome": "rate_limited", "status_code": 429, "keyed": True}},
    )
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _FailingClient(exc))

    assert cli_lit._cmd_search(_Args(query="q", limit=10))["error"]["code"] == "rate_limited"
    citations = cli_lit._cmd_citations(_Args(identifier="10.1000/example", limit=20, offset=0))
    assert citations["error"]["code"] == "rate_limited"
    assert "keyless providers: none" in citations["nextActions"][0]["description"]


def test_pacing_dir_is_under_the_program_root_and_none_without_one(tmp_path):
    assert cli_lit._pacing_dir(None) is None
    assert cli_lit._pacing_dir(tmp_path) == tmp_path / "data" / "litapi_pacing"


# ---------------------------------------------------------------------------
# acquire (v3-acquisition build) -- CLI-layer concerns only: argparse
# wiring, envelope shape, error-code mapping. trialerror.ingest.acquire.acquire
# itself is monkeypatched to a stub (its own logic is covered end-to-end
# in tests/test_litapi_acquire.py) and Store construction is monkeypatched
# to a no-op fake so this file needs no real database at all.
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeAcquireResult:
    def __init__(self, outcome, source, document=None, job=None):
        self.outcome = outcome
        self.source = source
        self.document = document
        self.job = job

    def to_dict(self):
        return {"outcome": self.outcome, "source": self.source, "document": self.document, "job": self.job}


def test_register_wires_acquire_subcommand():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    args = parser.parse_args(["lit", "acquire", "--doi", "10.1/x", "--launch-id", "LNCH-1"])
    assert args.lit_cmd == "acquire"
    assert args.doi == "10.1/x"
    assert args.arxiv_id is None
    assert args.launch_id == "LNCH-1"
    assert args.yes is False

    args2 = parser.parse_args(["lit", "acquire", "--arxiv", "2101.00001", "--launch-id", "LNCH-1", "--yes"])
    assert args2.arxiv_id == "2101.00001"
    assert args2.yes is True


def test_acquire_requires_exactly_one_of_doi_or_arxiv():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    with pytest.raises(SystemExit):
        parser.parse_args(["lit", "acquire", "--launch-id", "LNCH-1"])  # neither
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["lit", "acquire", "--doi", "10.1/x", "--arxiv", "2101.00001", "--launch-id", "LNCH-1"]
        )  # both


def test_acquire_requires_launch_id():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    with pytest.raises(SystemExit):
        parser.parse_args(["lit", "acquire", "--doi", "10.1/x"])


def test_cmd_acquire_ok_acquired(monkeypatch, tmp_path):
    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)
    fake_result = _FakeAcquireResult(
        outcome="acquired",
        source={"source_id": "SRC-1", "title": "T"},
        document={"doc_id": "DOC-1"},
        job={"job_id": "JOB-1", "kind": "normalize"},
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi="10.1/x", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is True
    assert env["result"]["outcome"] == "acquired"
    # FB-1 item F4: "acquired" is not "searchable", and the envelope says so
    # in two actions -- the job to run, and the inline way to run it.
    assert env["nextActions"] == [
        {"kind": "shell", "argv": ["trialerror", "jobs", "start-worker", "--job-id", "JOB-1"],
         "description": "run the enqueued pipeline job (the document is not searchable until it completes)"},
        {"kind": "shell", "argv": ["trialerror", "jobs", "start-worker", "--foreground", "--job-id", "JOB-1"],
         "description": "or run it inline in this shell and watch it (one document, one job)"},
    ]
    assert fake_store.closed is True


def test_cmd_acquire_ok_queued_suggests_requests_md_rerender(monkeypatch, tmp_path):
    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)
    fake_result = _FakeAcquireResult(outcome="queued", source={"source_id": "SRC-1", "request_state": "wanted"})
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi="10.1/x", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is True
    assert env["result"]["outcome"] == "queued"
    assert env["nextActions"][0]["argv"][:3] == ["trialerror", "ingest", "requests-md"]


def test_cmd_acquire_no_program_root_is_error_envelope(monkeypatch):
    monkeypatch.setattr(cli_lit, "_resolve_program_root", lambda args: None)
    args = _Args(program_root=None, doi="10.1/x", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "no_program_root"


def test_cmd_acquire_litapi_error_is_error_envelope(monkeypatch, tmp_path):
    from trialerror.litapi.errors import AllProvidersFailedError

    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)

    def _raise(*a, **kw):
        raise AllProvidersFailedError("nope", details={"failures": []})

    monkeypatch.setattr("trialerror.ingest.acquire.acquire", _raise)
    args = _Args(program_root=str(tmp_path), doi="10.1/x", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "AllProvidersFailedError"
    assert fake_store.closed is True


def test_cmd_acquire_cost_gate_refusal_suggests_yes_flag(monkeypatch, tmp_path):
    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)

    def _raise(*a, **kw):
        raise ValueError("cost gate: estimated 999 pages exceeds threshold")

    monkeypatch.setattr("trialerror.ingest.acquire.acquire", _raise)
    args = _Args(program_root=str(tmp_path), doi="10.1/x", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "cost_gate_refused"
    assert env["nextActions"][0]["argv"][-1] == "--yes"


def test_cmd_acquire_all_metadata_providers_transport_unreachable_overrides_queued(monkeypatch, tmp_path):
    """litapi-arxiv-https build, task 3: trialerror.ingest.acquire.acquire
    itself tolerates a total metadata-lookup failure silently and returns a
    normal 'queued' AcquireResult (its own module docstring; pinned against
    the real function in tests/test_litapi_acquire.py) -- but when that
    total failure is EVERY provider being transport-unreachable (not a
    legitimate 'no record anywhere'), _cmd_acquire must report a hard
    transport_unreachable error instead of silently filing a request-queue
    row no human asked for."""
    import types

    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)
    fake_result = types.SimpleNamespace(
        outcome="queued",
        metadata_providers=[],
        metadata_failures=[
            {
                "provider": "arxiv", "error": "arxiv: transport unreachable", "code": "transport_unreachable",
                "host": "export.arxiv.org", "scheme": "https",
            },
            {
                "provider": "openalex", "error": "openalex: transport unreachable", "code": "transport_unreachable",
                "host": "api.openalex.org", "scheme": "https",
            },
        ],
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi=None, arxiv_id="2101.00001", launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "transport_unreachable"
    assert len(env["error"]["details"]["failures"]) == 2
    assert fake_store.closed is True


def test_cmd_acquire_unresolved_is_its_own_refusal_not_a_queued_ok(monkeypatch, tmp_path):
    """F18: trialerror.ingest.acquire files NO row when a provider did not
    answer, and the CLI must say so in its own code -- an ok='queued' envelope
    would read as "no open-access copy exists"."""
    import types

    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)
    fake_result = types.SimpleNamespace(
        outcome="unresolved",
        source={},
        metadata_providers=["openalex"],
        metadata_failures=[],
        oa_legs=[
            {"provider": "arxiv", "outcome": "not_attempted"},
            {"provider": "unpaywall", "outcome": "rate_limited", "status_code": 429, "retry_after_s": 2.0},
        ],
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi="10.1000/example", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is False
    assert env["error"]["code"] == "oa_resolution_unresolved"
    assert "unpaywall=rate_limited HTTP 429" in env["error"]["message"]
    assert "not \"no open-access copy exists\"" in env["error"]["message"]
    assert env["error"]["details"]["retryable"] is True
    assert env["error"]["details"]["oa_legs"] == fake_result.oa_legs
    assert env["nextActions"] == [
        {"kind": "shell",
         "argv": ["trialerror", "lit", "acquire", "--doi", "10.1000/example", "--launch-id", "LNCH-1"],
         "description": "retry after 2s"},
    ]
    assert fake_store.closed is True


def test_cmd_acquire_unresolved_without_a_retry_after_says_retry_later(monkeypatch, tmp_path):
    import types

    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: _FakeStore())
    fake_result = types.SimpleNamespace(
        outcome="unresolved", source={}, metadata_providers=["openalex"], metadata_failures=[],
        oa_legs=[{"provider": "arxiv", "outcome": "http_error", "status_code": 503}],
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi=None, arxiv_id="2101.00001", launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["error"]["details"]["retryable"] is True
    assert env["nextActions"][0]["description"] == "retry later"
    assert env["nextActions"][0]["argv"][3] == "--arxiv"


def test_cmd_acquire_unresolved_not_retryable_points_at_the_readiness_check(monkeypatch, tmp_path):
    """A 406 will be answered the same way next time, so "run it again" is not
    the action -- the readiness check is."""
    import types

    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: _FakeStore())
    fake_result = types.SimpleNamespace(
        outcome="unresolved", source={}, metadata_providers=["openalex"], metadata_failures=[],
        oa_legs=[{"provider": "unpaywall", "outcome": "http_error", "status_code": 406}],
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi="10.1000/example", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["error"]["details"]["retryable"] is False
    assert env["nextActions"][0]["argv"] == ["trialerror", "doctor", "--only", "litapi_providers_ready"]


def test_cmd_acquire_unresolved_and_nothing_reachable_still_answers_transport_unreachable(monkeypatch, tmp_path):
    """The all-unreachable case keeps its own, more actionable code and its
    wording -- with the legs added to the details."""
    import types

    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: _FakeStore())
    fake_result = types.SimpleNamespace(
        outcome="unresolved", source={}, metadata_providers=[],
        metadata_failures=[
            {"provider": "arxiv", "error": "arxiv: transport unreachable", "code": "transport_unreachable",
             "host": "export.arxiv.org", "scheme": "https"},
        ],
        oa_legs=[{"provider": "arxiv", "outcome": "transport_unreachable"}],
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi=None, arxiv_id="2101.00001", launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["error"]["code"] == "transport_unreachable"
    assert env["error"]["details"]["oa_legs"] == fake_result.oa_legs


def test_cmd_acquire_queued_with_some_providers_succeeding_is_not_overridden(monkeypatch, tmp_path):
    """Guards against over-triggering: a genuinely empty request-queue
    outcome where at least one provider DID succeed at metadata (or where
    metadata_failures is simply empty) must keep the normal ok='queued'
    envelope -- only an EMPTY metadata_providers with a non-empty,
    all-transport_unreachable metadata_failures overrides it (see the
    existing test_cmd_acquire_ok_queued_suggests_requests_md_rerender above
    for the plain-stub, no-metadata-fields-at-all case)."""
    import types

    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)
    fake_result = types.SimpleNamespace(
        outcome="queued",
        metadata_providers=["openalex"],
        metadata_failures=[
            {"provider": "arxiv", "error": "arxiv: transport unreachable", "code": "transport_unreachable",
             "host": "export.arxiv.org", "scheme": "https"},
        ],
        source={"source_id": "SRC-1", "request_state": "wanted"},
        document=None,
        job=None,
        to_dict=lambda: {"outcome": "queued", "source": {"source_id": "SRC-1", "request_state": "wanted"}},
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: fake_result)
    args = _Args(program_root=str(tmp_path), doi="10.1/x", arxiv_id=None, launch_id="LNCH-1", yes=False)

    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is True
    assert env["result"]["outcome"] == "queued"


# ---------------------------------------------------------------------------
# lit investigate run / verdict / render (lane SI part B)
# ---------------------------------------------------------------------------


def test_register_wires_investigate_subcommands():
    """FAILS BEFORE lane SI part B: there was no ``investigate`` group."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    run = parser.parse_args(
        ["lit", "investigate", "run", "--seeds-file", "seeds.jsonl", "--out-dir", "dossiers", "--launch-id", "L1",
         "--max-calls-per-seed", "3", "--resume", "--no-cache", "--arxiv-neighbours",
         "--delivered-manifest", "delivered.tsv"]
    )
    assert (run.lit_cmd, run.investigate_cmd) == ("investigate", "run")
    assert (run.seeds_file, run.out_dir, run.launch_id, run.max_calls_per_seed) == ("seeds.jsonl", "dossiers", "L1", 3)
    assert run.resume and run.no_cache and run.arxiv_neighbours and run.delivered_manifest == "delivered.tsv"
    plain = parser.parse_args(["lit", "investigate", "run", "--seeds-file", "s", "--out-dir", "d", "--launch-id", "L"])
    assert (plain.max_calls_per_seed, plain.resume, plain.no_cache, plain.arxiv_neighbours) == (None, False, False, False)

    verdict = parser.parse_args(
        ["lit", "investigate", "verdict", "--dossier", "d.json", "--verdict", "SUBSTITUTE-WITH",
         "--substitute-doi", "10.1/x", "--reason-code", "consolidated", "--detail-json", "detail.json",
         "--supersede", "--launch-id", "L1"]
    )
    assert (verdict.investigate_cmd, verdict.verdict, verdict.substitute_doi) == ("verdict", "SUBSTITUTE-WITH", "10.1/x")
    assert verdict.supersede and verdict.reason_code == "consolidated" and verdict.detail_json == "detail.json"
    with pytest.raises(SystemExit):
        parser.parse_args(["lit", "investigate", "verdict", "--dossier", "d", "--verdict", "FETCH", "--launch-id", "L"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["lit", "investigate", "verdict", "--dossier", "d", "--verdict", "SUBSTITUTE-WITH", "--launch-id", "L",
             "--substitute-doi", "10.1/x", "--substitute-isbn", "0306406152"]
        )

    render = parser.parse_args(["lit", "investigate", "render", "--list-id", "list-1", "--format", "json"])
    assert (render.investigate_cmd, render.list_id, render.render_format) == ("render", "list-1", "json")
    assert parser.parse_args(["lit", "investigate", "render", "--list-id", "x"]).render_format == "lines"


def _investigate_env(monkeypatch, store, transport):
    """A launch in the temp store, the run's providers over ``transport`` and
    the run's year pinned (the author-works URL carries it)."""
    from tests import _investigate_fixtures as fx
    from tests._ingest_fixtures import bootstrap_launch

    launch = bootstrap_launch(store)
    monkeypatch.setattr(cli_lit, "_investigate_providers", lambda program_root, cfg: fx.providers(transport))
    monkeypatch.setattr("trialerror.litapi.investigate._current_year", lambda: fx.YEAR)
    return launch


def _run_args(program_root, platform_root, seeds_file, out_dir, launch_id, **kw):
    fields = dict(
        program_root=str(program_root), platform_root=str(platform_root), seeds_file=str(seeds_file),
        out_dir=str(out_dir), launch_id=launch_id, max_calls_per_seed=None, resume=False, no_cache=False,
        arxiv_neighbours=False, delivered_manifest=None,
    )
    fields.update(kw)
    return _Args(**fields)


def test_cmd_investigate_run_retry_earns_one_resume_action_that_parses(monkeypatch, store, program_root, platform_root, tmp_path):
    from trialerror.cli import build_parser
    from trialerror.litapi.transport import FakeTransport
    from tests import _investigate_fixtures as fx

    transport = FakeTransport()
    fx.route_widgets(transport)
    transport.add_response(fx.oa_doi_url(fx.GADGETS_DOI), fx.status(429))
    transport.add_response(fx.s2_paper_url(f"DOI:{fx.GADGETS_DOI}"), fx.status(503))
    for query in ("A Theory of Gadgets", "a theory of gadgets"):
        transport.add_response(fx.oa_search_url(query), fx.status(429))
        transport.add_response(fx.s2_search_url(query), fx.status(429))
    launch = _investigate_env(monkeypatch, store, transport)
    seeds = fx.write_seeds(tmp_path / "seeds.jsonl", [fx.seed_row(fx.WIDGETS_SEED), fx.seed_row(fx.GADGETS_SEED)])

    env = cli_lit._cmd_investigate_run(
        _run_args(program_root, platform_root, seeds, tmp_path / "out", launch, max_calls_per_seed=7)
    )

    assert env["ok"] is True, env
    assert env["command"] == "lit.investigate.run"
    result = env["result"]
    assert result["states"] == {"held": 0, "wrong_identifier": 0, "retry": 1, "need_info": 0, "open": 1}
    assert result["thresholds"]["max_calls_per_seed"] == 7
    assert result["calls_used"] == 7  # widgets 5 (lookup + 4 gather), gadgets 2 (lookup + search)
    assert set(result["providers"]) == {"openalex", "semanticscholar"}
    [action] = env["nextActions"]
    argv = action["argv"]
    assert argv[:4] == ["trialerror", "lit", "investigate", "run"] and "--resume" in argv
    assert argv[argv.index("--max-calls-per-seed") + 1] == "7"
    parsed = build_parser().parse_args(argv[1:])
    assert parsed.resume and parsed.launch_id == launch and parsed.program_root == str(program_root)


def test_cmd_investigate_run_refuses_a_bad_seeds_file_and_an_unknown_launch(monkeypatch, store, program_root, platform_root, tmp_path):
    from trialerror.litapi.transport import FakeTransport
    from tests import _investigate_fixtures as fx

    transport = FakeTransport()
    launch = _investigate_env(monkeypatch, store, transport)
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"list_id": "list 1", "row_id": "r", "seed_raw": "x"}\n', encoding="utf-8")

    env = cli_lit._cmd_investigate_run(_run_args(program_root, platform_root, bad, tmp_path / "out", launch))
    assert env["ok"] is False and env["error"]["code"] == "seeds_file_invalid"
    assert env["error"]["details"]["problems"]

    good = fx.write_seeds(tmp_path / "seeds.jsonl", [fx.seed_row(fx.WIDGETS_SEED)])
    env = cli_lit._cmd_investigate_run(_run_args(program_root, platform_root, good, tmp_path / "out", "LNCH-nobody"))
    assert env["ok"] is False and env["error"]["code"] == "XidTargetMissingError"
    assert transport.calls == []


def test_cmd_investigate_run_arxiv_neighbours_without_an_index_is_a_stated_skip(monkeypatch, store, program_root, platform_root, tmp_path):
    from trialerror.litapi.transport import FakeTransport
    from tests import _investigate_fixtures as fx

    transport = FakeTransport()
    fx.route_widgets(transport)
    launch = _investigate_env(monkeypatch, store, transport)
    seeds = fx.write_seeds(tmp_path / "seeds.jsonl", [fx.seed_row(fx.WIDGETS_SEED, question="What are widgets for?")])

    env = cli_lit._cmd_investigate_run(
        _run_args(program_root, platform_root, seeds, tmp_path / "out", launch, arxiv_neighbours=True)
    )

    assert env["ok"] is True, env
    neighbours = env["result"]["arxiv_neighbours"]
    assert (neighbours["requested"], neighbours["ran"]) == (True, False)
    assert neighbours["skipped"].startswith("--arxiv-neighbours skipped: no arXiv semantic index")
    assert any("--arxiv-neighbours skipped: no arXiv semantic index" in w["message"] for w in env["warnings"])
    assert env["result"]["investigated"] == 1  # the run itself went ahead


def test_cmd_investigate_run_arxiv_neighbours_over_a_built_index(monkeypatch, store, program_root, platform_root, tmp_path):
    """One pass over the local index for the distinct questions, attached to
    every dossier of this run by its question (fake query encoder, 8-row
    synthetic index)."""
    import json as _json
    from pathlib import Path

    from trialerror.arxiv_index.encoder import FakeQueryEncoder
    from trialerror.litapi.transport import FakeTransport
    from tests import _investigate_fixtures as fx
    from tests._arxiv_index_fixtures import write_small_fixture_zip

    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=8, dims=8)
    built = cli_lit._cmd_arxiv_index_build(
        _Args(program_root=str(program_root), platform_root=str(platform_root), zip_path=str(zip_path), db_path=None,
              dims=8, batch_size=4, member_glob=None, min_free_gb=0.001, job_id=None, launch_id=None, detach=False)
    )
    assert built["ok"] is True, built
    monkeypatch.setattr(cli_lit, "_build_query_encoder", lambda litapi_cfg, program_root: FakeQueryEncoder(dims=8))
    transport = FakeTransport()
    fx.route_widgets(transport)
    fx.route_wrong_doi(transport)
    launch = _investigate_env(monkeypatch, store, transport)
    seeds = fx.write_seeds(
        tmp_path / "seeds.jsonl",
        [fx.seed_row(fx.WIDGETS_SEED, question="What are widgets for?"),
         fx.seed_row(fx.WRONG_DOI_SEED, row_id="row-2", question="What are widgets for?")],
    )

    env = cli_lit._cmd_investigate_run(
        _run_args(program_root, platform_root, seeds, tmp_path / "out", launch, arxiv_neighbours=True)
    )

    assert env["ok"] is True, env
    assert env["result"]["arxiv_neighbours"]["ran"] is True
    assert env["result"]["arxiv_neighbours"]["questions"] == 1
    for dossier_ref in env["result"]["dossiers"]:
        dossier = _json.loads(Path(dossier_ref["dossier_path"]).read_text(encoding="utf-8"))
        neighbours = dossier["evidence"]["arxiv_neighbours"]
        assert len(neighbours) == 8  # k=10 over an 8-row index
        assert {"arxiv_id", "score", "title"} <= set(neighbours[0])


def test_cmd_investigate_verdict_and_render_envelopes(monkeypatch, store, program_root, platform_root, tmp_path):
    import json as _json

    from trialerror.litapi.transport import FakeTransport
    from tests import _investigate_fixtures as fx

    transport = FakeTransport()
    fx.route_widgets(transport)
    fx.route_wrong_doi(transport)
    launch = _investigate_env(monkeypatch, store, transport)
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n[litapi.investigate]\nfoundational_before = 1980\n', encoding="utf-8"
    )
    seeds = fx.write_seeds(
        tmp_path / "seeds.jsonl",
        [fx.seed_row(fx.WIDGETS_SEED, question="What are widgets for?"),
         fx.seed_row(fx.WRONG_DOI_SEED, list_id="list-2", question="What are widgets for?")],
    )
    run = cli_lit._cmd_investigate_run(_run_args(program_root, platform_root, seeds, tmp_path / "out", launch))
    assert run["ok"] is True, run
    assert run["result"]["thresholds"]["foundational_before"] == 1980  # read from [litapi.investigate]
    paths = {d["seed_raw"]: d["dossier_path"] for d in run["result"]["dossiers"]}
    detail = tmp_path / "detail.json"
    detail.write_text(_json.dumps({"why": "the primary study"}), encoding="utf-8")

    def verdict_args(seed_raw, word, **kw):
        fields = dict(program_root=str(program_root), platform_root=str(platform_root), dossier=paths[seed_raw],
                      verdict=word, substitute_doi=None, substitute_arxiv=None, substitute_isbn=None,
                      reason_code=None, detail_json=None, supersede=False, launch_id=launch)
        fields.update(kw)
        return _Args(**fields)

    refused = cli_lit._cmd_investigate_verdict(verdict_args(fx.WRONG_DOI_SEED, "REQUEST", detail_json=str(detail)))
    assert refused["ok"] is False and refused["error"]["code"] == "resolution_not_requestable"

    recorded = cli_lit._cmd_investigate_verdict(verdict_args(fx.WIDGETS_SEED, "REQUEST", detail_json=str(detail)))
    assert recorded["ok"] is True, recorded
    assert recorded["command"] == "lit.investigate.verdict"
    assert recorded["result"]["detail"] == {"why": "the primary study", "history": []}

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2]", encoding="utf-8")
    bad_detail = cli_lit._cmd_investigate_verdict(verdict_args(fx.WIDGETS_SEED, "DROP", detail_json=str(not_an_object)))
    assert bad_detail["ok"] is False and bad_detail["error"]["code"] == "detail_invalid"

    lines = cli_lit._cmd_investigate_render(
        _Args(program_root=str(program_root), platform_root=str(platform_root), list_id="list-1", render_format="lines")
    )
    assert lines["ok"] is True, lines
    assert lines["result"]["lines"] == [
        "row-1 · What are widgets for?",
        "  FETCH Cat Writer & Dan Other, A Study of Widgets, 1986, doi:10.9999/widgets · paywalled · WHY: the primary study",
        "(held 0 · dropped 0 · substituted 0)",
    ]
    wrong = cli_lit._cmd_investigate_render(
        _Args(program_root=str(program_root), platform_root=str(platform_root), list_id="list-2", render_format="json")
    )
    assert wrong["ok"] is False and wrong["error"]["code"] == "render_refused"
    assert wrong["error"]["details"]["rows"][0]["seeds"][0]["seed_raw"] == fx.WRONG_DOI_SEED
