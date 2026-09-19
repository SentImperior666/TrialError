"""Lane FB-1 item F10b: the normalised fallback retry.

The raw query is always the first attempt. A provider that answers with an
HTTP error status gets asked once more with ``normalize_title(query)`` --
never a provider that answered successfully, never a DOI or arXiv id, never
an unreachable host, and never silently.
"""

from __future__ import annotations

import pytest

from trialerror.litapi.client import LitApiClient, _normalized_retry_query
from trialerror.litapi.errors import (
    AllProvidersFailedError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from trialerror.litapi.models import WorkRecord, looks_like_identifier, normalize_title

_MESSY = "Retry Budgets:  a Study of Tail Latency (preprint)"


class _Recorder:
    """A provider that fails the first call with a chosen error and records
    every query it was asked."""

    search_scope = "test"

    def __init__(self, name="openalex", *, first_error=None, records=None, retry_error=None):
        self.name = name
        self._first_error = first_error
        self._retry_error = retry_error
        self._records = records if records is not None else [WorkRecord(title="T", doi="10.1/x")]
        self.queries: list[str] = []

    def get_by_doi(self, doi):
        return None

    def get_by_arxiv(self, arxiv_id):
        return None

    def search(self, query, *, limit=10):
        self.queries.append(query)
        if len(self.queries) == 1 and self._first_error is not None:
            raise self._first_error
        if len(self.queries) == 2 and self._retry_error is not None:
            raise self._retry_error
        return list(self._records)

    def get_citations(self, identifier, *, limit=100, offset=0):
        raise NotImplementedError


def _http(status: int, name="openalex") -> ProviderTransportError:
    return ProviderTransportError(f"HTTP {status}", provider=name, status_code=status)


def _unreachable(name="openalex") -> ProviderTransportError:
    return ProviderTransportError("connection refused", provider=name, host="example.invalid", scheme="https")


# ---------------------------------------------------------------------------
# the decision function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 422, 429, 500, 503, 599])
def test_a_4xx_or_5xx_earns_a_retry(status):
    assert _normalized_retry_query(_MESSY, _http(status)) == normalize_title(_MESSY)


@pytest.mark.parametrize("status", [200, 201, 204, 301, 399, 600])
def test_nothing_outside_4xx_5xx_earns_a_retry(status):
    assert _normalized_retry_query(_MESSY, _http(status)) is None


def test_a_transport_level_failure_never_earns_a_retry():
    """No HTTP response was received, so there is no evidence the query was
    the problem -- and normalization cannot reach an unreachable host."""
    assert _normalized_retry_query(_MESSY, _unreachable()) is None


def test_a_not_found_never_earns_a_retry():
    assert _normalized_retry_query(_MESSY, ProviderNotFoundError("nope", provider="openalex")) is None


@pytest.mark.parametrize(
    "identifier",
    ["10.1234/abc.def", "doi:10.1234/x", "https://doi.org/10.1234/x", "2401.01234", "arXiv:2401.01234v2", "math.GT/0309136"],
)
def test_dois_and_arxiv_ids_are_never_normalised(identifier):
    assert looks_like_identifier(identifier) is True
    assert _normalized_retry_query(identifier, _http(500)) is None


def test_a_query_that_normalises_to_itself_is_not_re_sent():
    assert _normalized_retry_query("retry budgets", _http(500)) is None


def test_a_query_that_normalises_to_nothing_is_not_re_sent():
    assert _normalized_retry_query("!!! ???", _http(500)) is None


# ---------------------------------------------------------------------------
# the retry through the client
# ---------------------------------------------------------------------------


def test_the_raw_query_is_the_first_attempt_and_the_normalised_one_the_second():
    provider = _Recorder(first_error=_http(400))
    result = LitApiClient([provider]).search(_MESSY)

    assert provider.queries == [_MESSY, normalize_title(_MESSY)]
    assert result.providers_succeeded == ["openalex"]
    assert result.providers_failed == []
    assert result.records


def test_the_retry_is_recorded():
    provider = _Recorder(first_error=_http(429))
    result = LitApiClient([provider]).search(_MESSY)

    assert len(result.provider_retries) == 1
    entry = result.provider_retries[0]
    assert entry["provider"] == "openalex"
    assert entry["status_code"] == 429
    assert entry["retried_query"] == normalize_title(_MESSY)
    assert entry["outcome"] == "succeeded"
    assert "HTTP 429" in entry["first_error"]
    assert result.to_dict()["provider_retries"] == result.provider_retries


def test_a_retry_that_fails_too_is_recorded_and_reported_as_a_failure():
    provider = _Recorder(first_error=_http(500), retry_error=_http(500))
    with pytest.raises(AllProvidersFailedError):
        LitApiClient([provider]).search(_MESSY)

    # ...and with a second, healthy provider the run still succeeds:
    failing = _Recorder("openalex", first_error=_http(500), retry_error=_http(500))
    healthy = _Recorder("arxiv", records=[WorkRecord(title="T2", arxiv_id="2401.00002")])
    result = LitApiClient([failing, healthy]).search(_MESSY)

    assert result.provider_retries[0]["outcome"] == "failed"
    assert [f["provider"] for f in result.providers_failed] == ["openalex"]
    assert result.providers_succeeded == ["arxiv"]


def test_a_provider_that_answered_is_never_asked_twice():
    """An empty result is an answer. Asking again with a different query
    would be inventing recall the provider did not report."""
    provider = _Recorder(records=[])
    result = LitApiClient([provider]).search(_MESSY)
    assert provider.queries == [_MESSY]
    assert result.provider_retries == []


def test_at_most_one_retry_per_provider():
    provider = _Recorder(first_error=_http(500), retry_error=_http(500))
    healthy = _Recorder("arxiv")
    LitApiClient([provider, healthy]).search(_MESSY)
    assert len(provider.queries) == 2


def test_one_providers_retry_does_not_change_what_another_is_asked():
    failing = _Recorder("openalex", first_error=_http(400))
    healthy = _Recorder("arxiv")
    LitApiClient([failing, healthy]).search(_MESSY)
    assert healthy.queries == [_MESSY]


def test_an_identifier_query_that_errors_is_reported_not_retried():
    provider = _Recorder(first_error=_http(500))
    with pytest.raises(AllProvidersFailedError):
        LitApiClient([provider]).search("10.1234/abc.def")
    assert provider.queries == ["10.1234/abc.def"]


def test_the_doi_and_arxiv_lookups_gained_no_retry_at_all():
    """Identifier lookups are a different verb, and this item does not touch
    them: one attempt, per provider, as before."""

    class _LookupRecorder(_Recorder):
        def __init__(self):
            super().__init__(first_error=None)
            self.doi_calls = 0

        def get_by_doi(self, doi):
            self.doi_calls += 1
            raise _http(500)

    provider = _LookupRecorder()
    with pytest.raises(AllProvidersFailedError):
        LitApiClient([provider]).lookup_doi("10.1234/abc.def")
    assert provider.doi_calls == 1


def test_the_external_api_facts_doc_states_the_retry():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "EXTERNAL_API_FACTS.md").read_text(encoding="utf-8")
    assert "normalize_title" in doc
