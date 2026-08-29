"""Tests for ``trialerror.litapi.models``: DOI/arXiv/title normalization and
``WorkRecord.identity_key``'s DOI-preferred precedence order (design
brief, verbatim: "DOI-preferred identity, arxiv->DOI normalization")."""

from __future__ import annotations

from trialerror.litapi.models import (
    WorkRecord,
    arxiv_to_doi,
    normalize_arxiv_id,
    normalize_doi,
    normalize_title,
)


def test_normalize_doi_strips_url_wrapper():
    assert normalize_doi("https://doi.org/10.1234/Fixture.5678") == "10.1234/fixture.5678"


def test_normalize_doi_strips_dx_doi_org_wrapper():
    assert normalize_doi("http://dx.doi.org/10.1234/x") == "10.1234/x"


def test_normalize_doi_strips_doi_colon_prefix():
    assert normalize_doi("doi:10.1234/x") == "10.1234/x"


def test_normalize_doi_bare_doi_lowercased():
    assert normalize_doi("10.1234/X") == "10.1234/x"


def test_normalize_doi_none_and_empty():
    assert normalize_doi(None) is None
    assert normalize_doi("") is None
    assert normalize_doi("   ") is None


def test_normalize_arxiv_id_strips_prefix_and_version():
    assert normalize_arxiv_id("arXiv:2101.00001v2") == "2101.00001"
    assert normalize_arxiv_id("2101.00001") == "2101.00001"


def test_normalize_arxiv_id_none_and_empty():
    assert normalize_arxiv_id(None) is None
    assert normalize_arxiv_id("") is None


def test_arxiv_to_doi_synthesizes_arxiv_prefix():
    assert arxiv_to_doi("arXiv:2101.00001v3") == "10.48550/arxiv.2101.00001"


def test_arxiv_to_doi_none_when_no_id():
    assert arxiv_to_doi(None) is None
    assert arxiv_to_doi("") is None


def test_normalize_title_casefolds_and_collapses_punctuation():
    assert normalize_title("  A Fixture Paper: About Metadata!  ") == "a fixture paper about metadata"


def test_normalize_title_none_and_empty():
    assert normalize_title(None) is None
    assert normalize_title("   ") is None


def test_identity_key_prefers_doi_over_arxiv_and_title():
    record = WorkRecord(title="Some Title", doi="10.1234/x", arxiv_id="2101.00001")
    assert record.identity_key() == "10.1234/x"


def test_identity_key_falls_back_to_arxiv_derived_doi():
    record = WorkRecord(title="Some Title", doi=None, arxiv_id="2101.00001")
    assert record.identity_key() == "10.48550/arxiv.2101.00001"


def test_identity_key_falls_back_to_normalized_title():
    record = WorkRecord(title="Some Title!!", doi=None, arxiv_id=None)
    assert record.identity_key() == "some title"


def test_identity_key_none_when_nothing_to_key_on():
    record = WorkRecord(title=None, doi=None, arxiv_id=None)
    assert record.identity_key() is None


def test_work_record_to_dict_shape():
    record = WorkRecord(title="T", doi="10.1/x", authors=["A"], year=2020, providers=["openalex"])
    d = record.to_dict()
    assert d["title"] == "T"
    assert d["doi"] == "10.1/x"
    assert d["authors"] == ["A"]
    assert d["providers"] == ["openalex"]
    assert "external_ids" in d and "other" in d
