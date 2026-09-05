"""Tests for the ``trialerror lit`` CLI group (``trialerror/cli/lit.py``). The
provider-plumbing (``_build_client``) is monkeypatched to a stub client
built from the SAME hand-written stubs ``test_litapi_client.py`` uses --
this file tests only the CLI-layer concerns (argparse wiring, envelope
shape, error-code mapping), not provider/reconciliation logic (covered
elsewhere)."""

from __future__ import annotations

import argparse

import pytest

from trialerror.cli import lit as cli_lit
from trialerror.litapi.client import LitApiClient, LookupResult, SearchResult
from trialerror.litapi.errors import AllProvidersFailedError
from trialerror.litapi.models import CitationEdge, CitationsPage, WorkRecord
from trialerror.util.envelope import PROTOCOL_VERSION


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


def test_cmd_citations_ok(monkeypatch):
    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _StubClient())
    args = _Args(identifier="10.1/x", limit=20, offset=0)

    env = cli_lit._cmd_citations(args)

    assert env["ok"] is True
    assert env["result"]["provider"] == "stub"
    assert env["result"]["items"] == [
        {"title": "Citer", "doi": None, "arxiv_id": None, "year": None, "authors": [], "external_ids": {}}
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
    assert env["nextActions"] == [
        {"kind": "shell", "argv": ["trialerror", "jobs", "start-worker", "--job-id", "JOB-1"],
         "description": "run the enqueued pipeline job"}
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
