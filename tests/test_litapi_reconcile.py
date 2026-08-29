"""Tests for ``trialerror.litapi.reconcile``: the post-processor that merges
redundant provider results into one normalized record (design brief:
"DOI-preferred identity, arxiv->DOI normalization, dedup, provenance =
which providers contributed")."""

from __future__ import annotations

import pytest

from trialerror.litapi.models import WorkRecord
from trialerror.litapi.reconcile import merge_one, reconcile_many


def test_merge_one_single_record_returns_a_copy_not_the_same_object():
    original = WorkRecord(title="T", doi="10.1/x", authors=["A"], providers=["openalex"])
    merged = merge_one([original])
    assert merged is not original
    assert merged.to_dict() == original.to_dict()


def test_merge_one_empty_raises():
    with pytest.raises(ValueError):
        merge_one([])


def test_merge_one_fills_missing_fields_from_second_record():
    a = WorkRecord(title="T", doi="10.1/x", year=None, venue=None, providers=["openalex"])
    b = WorkRecord(title="T", doi="10.1/x", year=2020, venue="Journal X", providers=["semanticscholar"])
    merged = merge_one([a, b])
    assert merged.year == 2020
    assert merged.venue == "Journal X"


def test_merge_one_first_record_wins_when_both_have_a_value():
    a = WorkRecord(title="Title From A", doi="10.1/x", providers=["openalex"])
    b = WorkRecord(title="Title From B", doi="10.1/x", providers=["semanticscholar"])
    merged = merge_one([a, b])
    assert merged.title == "Title From A"


def test_merge_one_citation_count_takes_max():
    a = WorkRecord(doi="10.1/x", citation_count=5, providers=["openalex"])
    b = WorkRecord(doi="10.1/x", citation_count=42, providers=["semanticscholar"])
    merged = merge_one([a, b])
    assert merged.citation_count == 42


def test_merge_one_longest_author_list_wins():
    a = WorkRecord(doi="10.1/x", authors=["Ada Fixture"], providers=["openalex"])
    b = WorkRecord(doi="10.1/x", authors=["Ada Fixture", "Bo Canned"], providers=["semanticscholar"])
    merged = merge_one([a, b])
    assert merged.authors == ["Ada Fixture", "Bo Canned"]


def test_merge_one_longest_abstract_wins():
    a = WorkRecord(doi="10.1/x", abstract="short", providers=["openalex"])
    b = WorkRecord(doi="10.1/x", abstract="a much longer abstract text", providers=["semanticscholar"])
    merged = merge_one([a, b])
    assert merged.abstract == "a much longer abstract text"


def test_merge_one_unions_external_ids():
    a = WorkRecord(doi="10.1/x", external_ids={"openalex": "W1"}, providers=["openalex"])
    b = WorkRecord(doi="10.1/x", external_ids={"semanticscholar": "S1"}, providers=["semanticscholar"])
    merged = merge_one([a, b])
    assert merged.external_ids == {"openalex": "W1", "semanticscholar": "S1"}


def test_merge_one_provenance_lists_every_contributing_provider():
    a = WorkRecord(doi="10.1/x", providers=["openalex"])
    b = WorkRecord(doi="10.1/x", providers=["semanticscholar"])
    merged = merge_one([a, b])
    assert merged.providers == ["openalex", "semanticscholar"]


def test_reconcile_many_groups_by_doi():
    a = WorkRecord(title="Paper One", doi="10.1/one", providers=["openalex"])
    b = WorkRecord(title="Paper One", doi="10.1/one", providers=["semanticscholar"])
    c = WorkRecord(title="Paper Two", doi="10.1/two", providers=["openalex"])

    merged = reconcile_many([a, b, c])

    assert len(merged) == 2
    one = next(r for r in merged if r.doi == "10.1/one")
    assert sorted(one.providers) == ["openalex", "semanticscholar"]


def test_reconcile_many_groups_arxiv_only_record_via_synthesized_doi():
    a = WorkRecord(title="Preprint", doi=None, arxiv_id="2101.00001", providers=["openalex"])
    b = WorkRecord(title="Preprint", doi=None, arxiv_id="arXiv:2101.00001v2", providers=["semanticscholar"])

    merged = reconcile_many([a, b])

    assert len(merged) == 1
    assert sorted(merged[0].providers) == ["openalex", "semanticscholar"]


def test_reconcile_many_falls_back_to_title_when_no_doi_or_arxiv():
    a = WorkRecord(title="A Rare Title!", doi=None, arxiv_id=None, providers=["openalex"])
    b = WorkRecord(title="a rare title", doi=None, arxiv_id=None, providers=["semanticscholar"])

    merged = reconcile_many([a, b])

    assert len(merged) == 1
    assert sorted(merged[0].providers) == ["openalex", "semanticscholar"]


def test_reconcile_many_keeps_unkeyable_records_standalone():
    keyed = WorkRecord(title="Real Paper", doi="10.1/x", providers=["openalex"])
    unkeyable = WorkRecord(title=None, doi=None, arxiv_id=None, providers=["semanticscholar"])

    merged = reconcile_many([keyed, unkeyable])

    assert len(merged) == 2


def test_reconcile_many_preserves_first_appearance_order():
    a = WorkRecord(title="First", doi="10.1/first", providers=["openalex"])
    b = WorkRecord(title="Second", doi="10.1/second", providers=["openalex"])
    merged = reconcile_many([a, b])
    assert [r.doi for r in merged] == ["10.1/first", "10.1/second"]
