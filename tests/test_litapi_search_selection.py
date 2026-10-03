"""``lit search``: what the DEFAULT does, pinned before any option exists, and
the two options that let a caller keep each provider's own top N
(``--per-provider``) or search only some providers (``--provider``).

Fake providers only: no transport, no network, no key. Every fake counts its
own calls, so "one call per provider" is asserted on the provider, not on the
client's own bookkeeping.
"""

from __future__ import annotations

import argparse

from trialerror.cli import lit as cli_lit
from trialerror.litapi.client import LitApiClient
from trialerror.litapi.errors import ProviderTransportError
from trialerror.litapi.models import WorkRecord


class _FakeProvider:
    """A provider that answers a search from a fixed list (or raises the
    queued errors first), and records every call it receives."""

    def __init__(self, name, records=(), *, scope="fake scope", errors=()):
        self.name = name
        self.search_scope = scope
        self._records = list(records)
        self._errors = list(errors)
        self.queries: list[str] = []
        self.limits: list[int] = []

    def get_by_doi(self, doi):
        return None

    def get_by_arxiv(self, arxiv_id):
        return None

    def search(self, query, *, limit=10):
        self.queries.append(query)
        self.limits.append(limit)
        if self._errors:
            raise self._errors.pop(0)
        # a fresh record per call, as a real provider builds one from each response
        return [
            WorkRecord(title=r.title, doi=r.doi, year=r.year, citation_count=r.citation_count)
            for r in self._records
        ]

    def get_citations(self, identifier, *, limit=100, offset=0):
        raise NotImplementedError


def _rec(title, doi=None, *, year=2020, cites=None):
    return WorkRecord(title=title, doi=doi, year=year, citation_count=cites)


def _titled(prefix, n):
    return [_rec(f"{prefix} paper {i}", f"10.{prefix}/{i}") for i in range(1, n + 1)]


class _Args:
    """The namespace ``lit search`` had before any option was added: the
    default path must not need the new attributes to exist."""

    def __init__(self, **kw):
        self.program_root = None
        for k, v in kw.items():
            setattr(self, k, v)


def _titles(result):
    return [r.title for r in result.records]


# -- today's default, pinned -------------------------------------------------


def test_default_keeps_the_first_limit_of_the_merged_list_in_provider_order():
    a = _FakeProvider("alpha", _titled("a", 3))
    b = _FakeProvider("beta", _titled("b", 3))

    result = LitApiClient([a, b]).search("q", limit=3)

    assert _titles(result) == ["a paper 1", "a paper 2", "a paper 3"]


def test_default_drops_the_second_provider_when_the_first_fills_the_limit():
    """The behaviour the option exists to change: pinned so a change to the
    default shows up here."""
    a = _FakeProvider("alpha", _titled("a", 5))
    b = _FakeProvider("beta", _titled("b", 5))

    result = LitApiClient([a, b]).search("q", limit=5)

    assert all(r.providers == ["alpha"] for r in result.records)
    assert result.providers_succeeded == ["alpha", "beta"]  # beta answered; its hits did not survive


def test_default_second_provider_fills_the_remaining_slots():
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 5))

    result = LitApiClient([a, b]).search("q", limit=4)

    assert _titles(result) == ["a paper 1", "a paper 2", "b paper 1", "b paper 2"]


def test_default_merges_a_record_both_providers_returned_into_one():
    shared = _rec("Shared paper", "10.9/shared", cites=3)
    a = _FakeProvider("alpha", [shared, _rec("Only alpha", "10.a/1")])
    b = _FakeProvider("beta", [_rec("Shared paper", "10.9/shared", cites=8), _rec("Only beta", "10.b/1")])

    result = LitApiClient([a, b]).search("q", limit=10)

    assert _titles(result) == ["Shared paper", "Only alpha", "Only beta"]
    assert result.records[0].providers == ["alpha", "beta"]
    assert result.records[0].citation_count == 8  # the merge takes the higher count


def test_default_result_carries_exactly_these_keys_and_no_rank_field():
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    out = LitApiClient([a, b]).search("q", limit=10).to_dict()

    assert list(out) == [
        "records", "providers_succeeded", "providers_failed",
        "provider_query_scope", "provider_retries", "provider_outcomes",
    ]
    assert out["provider_query_scope"] == {"alpha": "fake scope", "beta": "fake scope"}
    assert all("provider_ranks" not in r for r in out["records"])
    assert list(out["records"][0]) == [
        "title", "doi", "arxiv_id", "authors", "year", "venue", "abstract", "citation_count",
        "oa_pdf_url", "url", "external_ids", "providers", "other",
    ]


def test_default_asks_each_provider_once_with_the_query_verbatim_and_the_limit():
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    LitApiClient([a, b]).search("Retry  Budgets: a Study", limit=7)

    assert a.queries == b.queries == ["Retry  Budgets: a Study"]
    assert a.limits == b.limits == [7]


def test_default_retry_after_an_http_error_is_the_only_second_call_and_is_recorded():
    err = ProviderTransportError("bad request", provider="alpha", status_code=400)
    a = _FakeProvider("alpha", _titled("a", 2), errors=[err])
    b = _FakeProvider("beta", _titled("b", 2))

    result = LitApiClient([a, b]).search("Retry  Budgets: a Study", limit=5)

    assert len(a.queries) == 2 and a.queries[1] != a.queries[0]  # once more, normalised
    assert len(b.queries) == 1
    assert [x["provider"] for x in result.provider_retries] == ["alpha"]
    assert result.provider_retries[0]["outcome"] == "succeeded"


def test_cli_default_calls_the_client_without_the_new_options(monkeypatch):
    seen = {}

    class _Recorder:
        def search(self, query, *, limit=10):  # no per_provider/providers: the default call is unchanged
            seen["call"] = (query, limit)
            return LitApiClient([_FakeProvider("alpha", _titled("a", 1))]).search(query, limit=limit)

    monkeypatch.setattr(cli_lit, "_build_client", lambda args: _Recorder())

    env = cli_lit._cmd_search(_Args(query="q", limit=4))

    assert seen["call"] == ("q", 4)
    assert env["ok"] is True
    assert list(env["result"]) == [
        "records", "providers_succeeded", "providers_failed",
        "provider_query_scope", "provider_retries", "provider_outcomes",
    ]


def test_cli_parser_defaults_are_unchanged():
    parser = argparse.ArgumentParser()
    cli_lit.register(parser.add_subparsers(dest="group"))

    args = parser.parse_args(["lit", "search", "--query", "x"])

    assert args.query == "x" and args.limit == 10


# -- --per-provider: each provider keeps its own top N -------------------------


def test_per_provider_a_provider_past_the_first_keeps_its_own_hits():
    a = _FakeProvider("alpha", _titled("a", 5))
    b = _FakeProvider("beta", _titled("b", 5))

    result = LitApiClient([a, b]).search("q", limit=3, per_provider=True)

    assert _titles(result) == [
        "a paper 1", "a paper 2", "a paper 3", "b paper 1", "b paper 2", "b paper 3",
    ]
    assert [r.providers for r in result.records] == [["alpha"]] * 3 + [["beta"]] * 3


def test_per_provider_keeps_at_most_n_per_provider_even_if_a_provider_returns_more():
    a = _FakeProvider("alpha", _titled("a", 9))
    b = _FakeProvider("beta", _titled("b", 2))

    result = LitApiClient([a, b]).search("q", limit=4, per_provider=True)

    assert len(result.records) == 4 + 2
    assert result.selection["kept"] == {"alpha": 4, "beta": 2}


def test_per_provider_a_record_both_returned_appears_once_with_both_names_and_ranks():
    shared_a = _rec("Shared paper", "10.9/shared", cites=3)
    shared_b = _rec("Shared paper", "10.9/shared", cites=8)
    a = _FakeProvider("alpha", [_rec("Alpha first", "10.a/1"), shared_a, _rec("Alpha third", "10.a/3")])
    b = _FakeProvider("beta", [_rec("Beta first", "10.b/1"), _rec("Beta second", "10.b/2"), shared_b])

    result = LitApiClient([a, b]).search("q", limit=3, per_provider=True)

    assert _titles(result) == ["Alpha first", "Shared paper", "Alpha third", "Beta first", "Beta second"]
    shared = result.records[1]
    assert shared.providers == ["alpha", "beta"]
    assert shared.citation_count == 8  # the merged record, as the default merge makes it
    assert result.provider_ranks[1] == {"alpha": 2, "beta": 3}
    assert result.provider_ranks == [
        {"alpha": 1}, {"alpha": 2, "beta": 3}, {"alpha": 3}, {"beta": 1}, {"beta": 2},
    ]


def test_per_provider_ranks_are_the_providers_own_rank_not_the_merged_position():
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    out = LitApiClient([a, b]).search("q", limit=2, per_provider=True).to_dict()

    assert [(r["title"], r["provider_ranks"]) for r in out["records"]] == [
        ("a paper 1", {"alpha": 1}), ("a paper 2", {"alpha": 2}),
        ("b paper 1", {"beta": 1}), ("b paper 2", {"beta": 2}),
    ]


def test_per_provider_output_says_how_the_records_were_chosen():
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    out = LitApiClient([a, b]).search("q", limit=2, per_provider=True).to_dict()

    assert out["selection"] == {
        "mode": "per_provider", "limit": 2,
        "order": "provider order, then each provider's own rank",
        "kept": {"alpha": 2, "beta": 2},
    }
    assert list(out)[:6] == [
        "records", "providers_succeeded", "providers_failed",
        "provider_query_scope", "provider_retries", "provider_outcomes",
    ]  # every key the default has, in its order; selection is added after them


def test_per_provider_the_first_providers_block_reads_as_the_default_reads():
    a = _FakeProvider("alpha", _titled("a", 3))
    b = _FakeProvider("beta", _titled("b", 3))
    client = LitApiClient([a, b])

    default = client.search("q", limit=3)
    per = client.search("q", limit=3, per_provider=True)

    assert _titles(per)[:3] == _titles(default)


def test_per_provider_a_provider_that_failed_contributes_nothing_and_is_still_reported():
    err = ProviderTransportError("down", provider="alpha", status_code=503)
    a = _FakeProvider("alpha", _titled("a", 3), errors=[err])
    b = _FakeProvider("beta", _titled("b", 3))

    result = LitApiClient([a, b]).search("q", limit=2, per_provider=True)

    assert _titles(result) == ["b paper 1", "b paper 2"]
    assert result.providers_succeeded == ["beta"]
    assert [f["provider"] for f in result.providers_failed] == ["alpha"]
    assert result.selection["kept"] == {"alpha": 0, "beta": 2}  # a failed provider has a key, at 0


def test_per_provider_kept_counts_a_repeated_identity_inside_one_provider_once():
    a = _FakeProvider("alpha", [_rec("A1", "10.a/1"), _rec("A2", "10.a/2"), _rec("A1 again", "10.a/1")])
    b = _FakeProvider("beta", [_rec("A1", "10.a/1"), _rec("B2", "10.b/2")])

    result = LitApiClient([a, b]).search("q", limit=3, per_provider=True)

    assert _titles(result) == ["A1", "A2", "B2"]
    assert result.provider_ranks == [{"alpha": 1, "beta": 1}, {"alpha": 2}, {"beta": 2}]
    assert result.selection["kept"] == {"alpha": 2, "beta": 2}  # 3 rows from alpha, 2 distinct records


def test_per_provider_kept_has_a_key_for_a_provider_that_returned_nothing():
    a = _FakeProvider("alpha", [])
    b = _FakeProvider("beta", _titled("b", 2))

    result = LitApiClient([a, b]).search("q", limit=3, per_provider=True)

    assert result.providers_succeeded == ["alpha", "beta"]
    assert result.selection["kept"] == {"alpha": 0, "beta": 2}


def test_per_provider_kept_has_a_key_for_every_selected_provider_and_only_those():
    err = ProviderTransportError("down", provider="alpha", status_code=503)
    a = _FakeProvider("alpha", _titled("a", 3), errors=[err])
    b = _FakeProvider("beta", _titled("b", 3))
    c = _FakeProvider("gamma", _titled("c", 3))

    result = LitApiClient([a, b, c]).search("q", limit=2, per_provider=True, providers=["alpha", "beta"])

    assert list(result.selection["kept"]) == ["alpha", "beta"]
    assert result.selection["kept"] == {"alpha": 0, "beta": 2}
    assert c.queries == []


def test_per_provider_asks_each_provider_exactly_once():
    a = _FakeProvider("alpha", _titled("a", 5))
    b = _FakeProvider("beta", _titled("b", 5))

    LitApiClient([a, b]).search("q", limit=3, per_provider=True)

    assert len(a.queries) == 1 and len(b.queries) == 1
    assert a.limits == b.limits == [3]  # each is asked for N, the same as the default asks


def test_per_provider_retry_rule_is_unchanged():
    err = ProviderTransportError("bad request", provider="alpha", status_code=400)
    a = _FakeProvider("alpha", _titled("a", 2), errors=[err])
    b = _FakeProvider("beta", _titled("b", 2))

    result = LitApiClient([a, b]).search("Retry  Budgets: a Study", limit=2, per_provider=True)

    assert len(a.queries) == 2 and len(b.queries) == 1  # the one recorded retry, as by default
    assert [x["provider"] for x in result.provider_retries] == ["alpha"]


def test_per_provider_keeps_a_record_with_no_identity_standalone():
    a = _FakeProvider("alpha", [WorkRecord(), _rec("Real", "10.a/1")])
    b = _FakeProvider("beta", [WorkRecord()])

    result = LitApiClient([a, b]).search("q", limit=5, per_provider=True)

    assert len(result.records) == 3  # nothing merged, nothing lost


def test_per_provider_a_record_with_no_identity_keeps_its_place_in_its_providers_block():
    a = _FakeProvider("alpha", [WorkRecord(), _rec("A2", "10.a/2"), _rec("A3", "10.a/3")])
    b = _FakeProvider("beta", [_rec("B1", "10.b/1"), WorkRecord(), _rec("B3", "10.b/3")])

    result = LitApiClient([a, b]).search("q", limit=3, per_provider=True)

    assert _titles(result) == [None, "A2", "A3", "B1", None, "B3"]
    assert result.provider_ranks == [
        {"alpha": 1}, {"alpha": 2}, {"alpha": 3},
        {"beta": 1}, {"beta": 2}, {"beta": 3},
    ]


def test_per_provider_an_identityless_record_does_not_move_a_shared_records_place():
    shared = _rec("Shared", "10.9/s")
    a = _FakeProvider("alpha", [WorkRecord(), shared])
    b = _FakeProvider("beta", [WorkRecord(), _rec("Shared", "10.9/s"), _rec("B3", "10.b/3")])

    result = LitApiClient([a, b]).search("q", limit=3, per_provider=True)

    assert _titles(result) == [None, "Shared", None, "B3"]
    assert result.provider_ranks == [{"alpha": 1}, {"alpha": 2, "beta": 2}, {"beta": 1}, {"beta": 3}]


def test_default_still_puts_identityless_records_last():
    """The default mode's order is today's and stays: only --per-provider places them in line."""
    a = _FakeProvider("alpha", [WorkRecord(), _rec("A2", "10.a/2")])
    b = _FakeProvider("beta", [_rec("B1", "10.b/1"), WorkRecord()])

    result = LitApiClient([a, b]).search("q", limit=10)

    assert _titles(result) == ["A2", "B1", None, None]


# -- --provider: search only the named providers --------------------------------


def test_provider_selection_searches_only_the_named_provider():
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    result = LitApiClient([a, b]).search("q", limit=5, providers=["beta"])

    assert _titles(result) == ["b paper 1", "b paper 2"]
    assert a.queries == [] and len(b.queries) == 1
    assert result.providers_succeeded == ["beta"]
    assert list(result.provider_query_scope) == ["beta"]
    assert list(result.provider_outcomes) == ["beta"]


def test_provider_selection_of_two_keeps_the_clients_provider_order():
    a = _FakeProvider("alpha", _titled("a", 1))
    b = _FakeProvider("beta", _titled("b", 1))
    c = _FakeProvider("gamma", _titled("c", 1))

    result = LitApiClient([a, b, c]).search("q", limit=5, providers=["gamma", "alpha", "gamma"])

    assert result.providers_succeeded == ["alpha", "gamma"]
    assert len(a.queries) == 1 and b.queries == [] and len(c.queries) == 1


def test_provider_selection_refuses_an_unknown_name_with_the_valid_names_and_asks_nobody():
    import pytest

    from trialerror.litapi.errors import UnknownProviderError

    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    with pytest.raises(UnknownProviderError) as info:
        LitApiClient([a, b]).search("q", providers=["alpha", "gamma"])

    assert info.value.details == {"unknown": ["gamma"], "valid": ["alpha", "beta"]}
    assert "alpha, beta" in str(info.value) and "gamma" in str(info.value)
    assert a.queries == [] and b.queries == []


def test_provider_selection_combines_with_per_provider():
    a = _FakeProvider("alpha", _titled("a", 4))
    b = _FakeProvider("beta", _titled("b", 4))

    result = LitApiClient([a, b]).search("q", limit=2, per_provider=True, providers=["beta"])

    assert _titles(result) == ["b paper 1", "b paper 2"]
    assert result.provider_ranks == [{"beta": 1}, {"beta": 2}]
    assert a.queries == []


# -- the CLI: --per-provider and --provider ------------------------------------


def _parse(*argv):
    parser = argparse.ArgumentParser()
    cli_lit.register(parser.add_subparsers(dest="group"))
    return parser.parse_args(["lit", "search", "--query", "q", *argv])


def _cli_env(monkeypatch, fakes, **args):
    monkeypatch.setattr(cli_lit, "_build_client", lambda a: LitApiClient(fakes))
    return cli_lit._cmd_search(_Args(query="q", **args))


def test_cli_parses_per_provider_and_repeatable_provider():
    args = _parse("--per-provider", "--provider", "alpha", "--provider", "beta")

    assert args.per_provider is True
    assert args.providers == ["alpha", "beta"]
    assert _parse().per_provider is False and _parse().providers is None


def test_cli_per_provider_envelope_shows_ranks_and_selection_on_a_small_example(monkeypatch):
    shared = _rec("Shared paper", "10.9/shared")
    a = _FakeProvider("alpha", [_rec("Alpha one", "10.a/1"), shared])
    b = _FakeProvider("beta", [_rec("Beta one", "10.b/1"), _rec("Shared paper", "10.9/shared")])

    env = _cli_env(monkeypatch, [a, b], limit=2, per_provider=True)

    assert env["ok"] is True
    rows = [(r["title"], r["providers"], r["provider_ranks"]) for r in env["result"]["records"]]
    assert rows == [
        ("Alpha one", ["alpha"], {"alpha": 1}),
        ("Shared paper", ["alpha", "beta"], {"alpha": 2, "beta": 2}),
        ("Beta one", ["beta"], {"beta": 1}),
    ]
    assert env["result"]["selection"]["kept"] == {"alpha": 2, "beta": 2}
    assert len(a.queries) == len(b.queries) == 1


def test_cli_provider_searches_only_the_named_provider(monkeypatch):
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    env = _cli_env(monkeypatch, [a, b], limit=5, providers=["beta"])

    assert env["ok"] is True
    assert [r["title"] for r in env["result"]["records"]] == ["b paper 1", "b paper 2"]
    assert a.queries == [] and len(b.queries) == 1
    assert "selection" not in env["result"]  # --provider alone changes who is asked, not what is kept


def test_cli_unknown_provider_is_refused_with_the_valid_names_and_nobody_is_asked(monkeypatch):
    a = _FakeProvider("alpha", _titled("a", 2))
    b = _FakeProvider("beta", _titled("b", 2))

    env = _cli_env(monkeypatch, [a, b], limit=5, providers=["gamma"])

    assert env["ok"] is False
    assert env["error"]["code"] == "UnknownProviderError"
    assert env["error"]["details"] == {"unknown": ["gamma"], "valid": ["alpha", "beta"]}
    assert "alpha, beta" in env["error"]["message"]
    assert a.queries == [] and b.queries == []


def test_cli_help_documents_both_options_and_the_valid_names():
    parser = argparse.ArgumentParser()
    group = cli_lit.register(parser.add_subparsers())
    search_parser = group._subparsers._group_actions[0].choices["search"]  # type: ignore[attr-defined]
    text = " ".join(search_parser.format_help().split())

    assert "--per-provider" in text and "--provider NAME" in text
    assert "provider_ranks" in text and "one call per provider" in text
    assert "openalex, semanticscholar" in text


def test_the_external_api_facts_doc_describes_both_options():
    from pathlib import Path

    import pytest

    path = Path(__file__).resolve().parents[1] / "docs" / "EXTERNAL_API_FACTS.md"
    if not path.is_file():
        pytest.skip("docs/EXTERNAL_API_FACTS.md is not part of this distribution")
    doc = path.read_text(encoding="utf-8")
    assert "--per-provider" in doc and "--provider NAME" in doc and "provider_ranks" in doc


# -- --limit below 1 is refused before any provider is called ----------------------


def test_client_refuses_a_limit_below_one_before_asking_anyone():
    import pytest

    for limit in (0, -1, -50):
        for flags in ({}, {"per_provider": True}, {"providers": ["alpha"]}):
            a = _FakeProvider("alpha", _titled("a", 3))
            b = _FakeProvider("beta", _titled("b", 3))
            with pytest.raises(ValueError, match="limit >= 1"):
                LitApiClient([a, b]).search("q", limit=limit, **flags)
            assert a.queries == [] and b.queries == []


def test_client_still_accepts_a_limit_of_one():
    a = _FakeProvider("alpha", _titled("a", 3))
    b = _FakeProvider("beta", _titled("b", 3))

    assert _titles(LitApiClient([a, b]).search("q", limit=1)) == ["a paper 1"]
    assert _titles(LitApiClient([a, b]).search("q", limit=1, per_provider=True)) == ["a paper 1", "b paper 1"]


def test_cli_refuses_a_limit_below_one_with_the_usage_envelope_and_builds_no_client(monkeypatch):
    def _never(args):
        raise AssertionError("a client was built for a refused limit")

    monkeypatch.setattr(cli_lit, "_build_client", _never)

    for limit in (0, -2):
        for flags in ({}, {"per_provider": True}, {"providers": ["openalex"]}):
            env = cli_lit._cmd_search(_Args(query="q", limit=limit, **flags))

            assert env["ok"] is False
            assert env["command"] == "lit.search"
            assert env["error"]["code"] == "usage"  # the code other commands use for a bad count
            assert set(env["error"]) == {"code", "message"}
            assert f"--limit {limit}" in env["error"]["message"] and "1 or more" in env["error"]["message"]


def test_cli_refusal_has_the_shape_of_another_commands_bad_argument_envelope():
    """The same ``{ok, command, error: {code, message}}`` shape ``ingest quality`` gives a bad ``--sample``."""
    from trialerror.util.envelope import error_envelope

    other = error_envelope("ingest.quality", "usage", "--sample 0 measures nothing")
    ours = cli_lit._cmd_search(_Args(query="q", limit=0))

    assert set(ours) == set(other)
    assert set(ours["error"]) == set(other["error"])


def test_cli_limit_of_one_is_not_refused(monkeypatch):
    a = _FakeProvider("alpha", _titled("a", 3))
    env = _cli_env(monkeypatch, [a], limit=1)

    assert env["ok"] is True and len(env["result"]["records"]) == 1


def test_the_guide_says_the_limit_refusal_changes_only_invalid_input():
    from pathlib import Path

    import pytest

    path = Path(__file__).resolve().parents[1] / "docs" / "EXTERNAL_API_FACTS.md"
    if not path.is_file():
        pytest.skip("docs/EXTERNAL_API_FACTS.md is not part of this distribution")
    doc = path.read_text(encoding="utf-8")
    flat = " ".join(doc.split())
    assert "`--limit` must be at least 1" in flat
    assert "only for inputs that were already invalid" in flat
    assert "distinct kept records" in flat
