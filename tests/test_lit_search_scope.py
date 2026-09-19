"""Lane FB-1 item F3, literature half: each provider searches its OWN way,
and the result says which way.

No provider query is rewritten anywhere -- the scope is STATED, not
normalised away. (The one place a provider query is ever retried with a
different string is item F10b's normalised fallback, which is a retry after
a provider-side error, not a rewrite of the first attempt.)
"""

from __future__ import annotations

from trialerror.litapi.client import LitApiClient
from trialerror.litapi.models import WorkRecord
from trialerror.litapi.providers.arxiv import ArxivProvider
from trialerror.litapi.providers.base import Provider
from trialerror.litapi.providers.openalex import OpenAlexProvider
from trialerror.litapi.providers.semanticscholar import SemanticScholarProvider
from trialerror.litapi.providers.unpaywall import UnpaywallProvider


class _ScopedStub:
    def __init__(self, name, scope, records=None):
        self.name = name
        self.search_scope = scope
        self._records = records or []
        self.queries: list[str] = []

    def get_by_doi(self, doi):
        return None

    def get_by_arxiv(self, arxiv_id):
        return None

    def search(self, query, *, limit=10):
        self.queries.append(query)
        return list(self._records)

    def get_citations(self, identifier, *, limit=100, offset=0):
        raise NotImplementedError


def test_every_shipped_provider_states_its_own_scope():
    assert "title only" in OpenAlexProvider.search_scope
    assert "all fields" in ArxivProvider.search_scope
    assert "relevance" in SemanticScholarProvider.search_scope
    assert "no search endpoint" in UnpaywallProvider.search_scope


def test_the_protocol_carries_a_default_so_a_third_party_provider_is_never_silent():
    assert Provider.search_scope == "unspecified"


def test_search_reports_the_scope_beside_providers_succeeded():
    a = _ScopedStub("openalex", "title only (filter=title.search)", [WorkRecord(title="T", doi="10.1/x")])
    b = _ScopedStub("arxiv", "all fields (search_query=all:)")
    result = LitApiClient([a, b]).search("retry budgets")

    assert result.providers_succeeded == ["openalex", "arxiv"]
    assert result.provider_query_scope == {
        "openalex": "title only (filter=title.search)",
        "arxiv": "all fields (search_query=all:)",
    }
    assert result.to_dict()["provider_query_scope"] == result.provider_query_scope


def test_the_scope_is_reported_for_every_provider_asked_not_only_the_ones_that_answered():
    """A provider that returned nothing is exactly the one whose scope the
    caller needs: "openalex: 0" means nothing until you know openalex was
    asked a title-only question."""
    a = _ScopedStub("openalex", "title only (filter=title.search)")
    b = _ScopedStub("arxiv", "all fields (search_query=all:)", [WorkRecord(title="T", arxiv_id="2401.00001")])
    result = LitApiClient([a, b]).search("retry budgets")
    assert result.records
    assert set(result.provider_query_scope) == {"openalex", "arxiv"}


def test_the_query_reaches_every_provider_verbatim():
    a = _ScopedStub("openalex", "title only")
    b = _ScopedStub("arxiv", "all fields")
    LitApiClient([a, b]).search("Retry  Budgets: a Study")
    assert a.queries == b.queries == ["Retry  Budgets: a Study"]


def test_a_provider_without_the_attribute_reads_as_unspecified():
    class _Bare:
        name = "bare"

        def get_by_doi(self, doi):
            return None

        def get_by_arxiv(self, arxiv_id):
            return None

        def search(self, query, *, limit=10):
            return [WorkRecord(title="T", doi="10.1/x")]

        def get_citations(self, identifier, *, limit=100, offset=0):
            raise NotImplementedError

    result = LitApiClient([_Bare()]).search("x")
    assert result.provider_query_scope == {"bare": "unspecified"}


def test_the_cli_help_states_the_three_scopes():
    """The scopes have to be readable from `--help`, not only from the
    source: an operator comparing provider hit counts is looking at a
    terminal."""
    import argparse

    from trialerror.cli import lit as cli_lit

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    group = cli_lit.register(sub)
    group_help = " ".join(group.format_help().lower().split())
    assert "title only" in group_help
    assert "all fields" in group_help
    assert "relevance" in group_help

    search_parser = group._subparsers._group_actions[0].choices["search"]  # type: ignore[attr-defined]
    search_help = " ".join(search_parser.format_help().lower().split())
    assert "before comparing their hit counts" in search_help


def test_the_external_api_facts_doc_states_the_scopes():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "EXTERNAL_API_FACTS.md").read_text(encoding="utf-8")
    assert "title.search" in doc
    assert "search_query=all:" in doc
    assert "no search endpoint" in doc
