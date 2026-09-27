"""Tests for ``trialerror.litapi.config``: ``[litapi]`` section resolution
(conservative defaults when absent -- design brief) and
``resolve_api_key``'s "never read a key except from a configured path"
contract."""

from __future__ import annotations

from trialerror.litapi.config import load_litapi_config, resolve_api_key


def test_load_litapi_config_defaults_when_section_absent():
    cfg = load_litapi_config({})

    assert cfg.openalex.base_url == "https://api.openalex.org"
    assert cfg.openalex.mailto is None
    assert cfg.openalex.min_interval_s == 1.0
    assert cfg.openalex.retry_attempts == 3
    # lane FB-acq item 2: 429 joined every provider's retry list -- bounded by
    # max_total_wait_s and honouring the provider's own Retry-After.
    assert cfg.openalex.retry_on_status == (429, 500)
    assert cfg.openalex.max_total_wait_s == 30.0

    assert cfg.semanticscholar.base_url == "https://api.semanticscholar.org"
    assert cfg.semanticscholar.retry_attempts == 5
    assert cfg.semanticscholar.retry_on_status == (403, 429)
    assert cfg.semanticscholar.api_key_header == "x-api-key"


def test_load_litapi_config_none_raw_defaults_too():
    cfg = load_litapi_config(None)
    assert cfg.openalex.base_url == "https://api.openalex.org"


def test_load_litapi_config_reads_configured_values():
    raw = {
        "litapi": {
            "openalex": {"base_url": "https://openalex.example", "mailto": "me@example.org", "min_interval_s": 0.5},
            "semanticscholar": {"api_key_path": "keys/s2.key", "min_interval_s": 2.0, "retry_attempts": 9},
        }
    }
    cfg = load_litapi_config(raw)

    assert cfg.openalex.base_url == "https://openalex.example"
    assert cfg.openalex.mailto == "me@example.org"
    assert cfg.openalex.min_interval_s == 0.5
    assert cfg.semanticscholar.api_key_path == "keys/s2.key"
    assert cfg.semanticscholar.min_interval_s == 2.0
    assert cfg.semanticscholar.retry_attempts == 9


def test_config_provider_lookup_by_name():
    cfg = load_litapi_config({})
    assert cfg.provider("openalex") is cfg.openalex
    assert cfg.provider("semanticscholar") is cfg.semanticscholar
    assert cfg.provider("arxiv") is cfg.arxiv
    assert cfg.provider("unpaywall") is cfg.unpaywall
    import pytest

    with pytest.raises(KeyError):
        cfg.provider("crossref")


# ---------------------------------------------------------------------------
# arxiv / unpaywall (v3-acquisition build, C-0064 flags F1/F2 RESOLVED)
# ---------------------------------------------------------------------------


def test_load_litapi_config_arxiv_defaults_grounded_in_external_api_facts():
    cfg = load_litapi_config({})

    # https, not arXiv's own historical http:// docs examples -- an egress
    # policy admitting only tcp/443 (this harness's own container included)
    # refuses plaintext http outright, and every other provider default in
    # this module is already https (see test_all_provider_default_base_urls_use_https
    # below for the pinned, all-providers version of this assertion).
    assert cfg.arxiv.base_url == "https://export.arxiv.org/api"
    # docs/EXTERNAL_API_FACTS.md quick-confirms: "1 request per 3 seconds",
    # the documented, enforced ToU limit -- not a conservative guess.
    assert cfg.arxiv.min_interval_s == 3.0
    assert cfg.arxiv.retry_attempts == 3
    assert cfg.arxiv.retry_on_status == (429, 500, 503)  # lane FB-acq item 2 added 429
    # lane FB-acq item 4: the two arxiv-ONLY keys. ASSUMED endpoint -- see
    # ArxivProvider.get_license's own TRIALERROR-DEV-NOTE; these are the two an
    # operator changes if arXiv's OAI host or path differs.
    assert cfg.arxiv.license_lookup is True
    assert cfg.arxiv.oai_base_url == "https://export.arxiv.org/oai2"


def test_all_provider_default_base_urls_use_https():
    """Pin (task litapi-arxiv-https): a container-side egress policy that
    admits only tcp/443 (C-0093(a)) must never be broken by a provider
    quietly defaulting to plaintext http -- every one of the four real
    ``Provider`` base URLs, both as resolved on a :class:`LitApiConfig`
    and in :data:`trialerror.litapi.config._DEFAULT_BASE_URLS` itself, must
    be https."""
    from trialerror.litapi.config import _DEFAULT_BASE_URLS

    cfg = load_litapi_config({})
    for name in ("openalex", "semanticscholar", "arxiv", "unpaywall"):
        base_url = cfg.provider(name).base_url
        assert base_url.startswith("https://"), f"{name}.base_url is not https: {base_url!r}"

    for name, url in _DEFAULT_BASE_URLS.items():
        assert url.startswith("https://"), f"_DEFAULT_BASE_URLS[{name!r}] is not https: {url!r}"


def test_load_litapi_config_unpaywall_defaults():
    cfg = load_litapi_config({})

    assert cfg.unpaywall.base_url == "https://api.unpaywall.org/v2"
    assert cfg.unpaywall.mailto is None
    assert cfg.unpaywall.min_interval_s == 1.0
    assert cfg.unpaywall.retry_attempts == 3
    assert cfg.unpaywall.retry_on_status == (429, 500)  # lane FB-acq item 2 added 429
    # the arxiv-only keys are not filled for any other provider
    assert cfg.unpaywall.oai_base_url is None


def test_load_litapi_config_reads_configured_arxiv_and_unpaywall_values():
    raw = {
        "litapi": {
            "arxiv": {"min_interval_s": 5.0, "retry_attempts": 2},
            "unpaywall": {"mailto": "me@example.org", "min_interval_s": 0.5},
        }
    }
    cfg = load_litapi_config(raw)

    assert cfg.arxiv.min_interval_s == 5.0
    assert cfg.arxiv.retry_attempts == 2
    assert cfg.unpaywall.mailto == "me@example.org"
    assert cfg.unpaywall.min_interval_s == 0.5


def test_arxiv_has_no_api_key_concept_resolve_api_key_always_none(tmp_path):
    # arXiv is fully keyless -- resolve_api_key never has anything to find,
    # even with an (irrelevant) api_key_path configured.
    raw = {"litapi": {"arxiv": {}}}
    cfg = load_litapi_config(raw).arxiv
    assert resolve_api_key(cfg) is None


def test_resolve_api_key_returns_none_when_unconfigured():
    cfg = load_litapi_config({}).openalex
    assert resolve_api_key(cfg) is None


def test_resolve_api_key_returns_none_when_file_missing(tmp_path):
    raw = {"litapi": {"openalex": {"api_key_path": str(tmp_path / "nope.key")}}}
    cfg = load_litapi_config(raw).openalex
    assert resolve_api_key(cfg) is None


def test_resolve_api_key_reads_and_strips_file_contents(tmp_path):
    key_path = tmp_path / "s2.key"
    key_path.write_text("  super-secret-key\n", encoding="utf-8")
    raw = {"litapi": {"semanticscholar": {"api_key_path": str(key_path)}}}
    cfg = load_litapi_config(raw).semanticscholar

    assert resolve_api_key(cfg) == "super-secret-key"


def test_resolve_api_key_resolves_relative_path_against_program_root(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "s2.key").write_text("relative-key", encoding="utf-8")
    raw = {"litapi": {"semanticscholar": {"api_key_path": "keys/s2.key"}}}
    cfg = load_litapi_config(raw).semanticscholar

    assert resolve_api_key(cfg, program_root=tmp_path) == "relative-key"
    # without a program_root, a relative path resolves against CWD -- almost
    # certainly not the fixture file -- and must not raise, only miss.
    assert resolve_api_key(cfg, program_root=None) is None


# ---------------------------------------------------------------------------
# alphaxiv / arxivxplorer (v-allarxiv-search build)
# ---------------------------------------------------------------------------


def test_alphaxiv_defaults_off_with_no_key_and_the_documented_mcp_endpoint():
    cfg = load_litapi_config({}).alphaxiv

    assert cfg.enabled is False
    assert cfg.api_key_path is None
    assert cfg.mcp_endpoint == "https://api.alphaxiv.org/mcp/v1"


def test_alphaxiv_reads_configured_values():
    raw = {"litapi": {"alphaxiv": {"enabled": True, "api_key_path": "keys/alphaxiv.key"}}}
    cfg = load_litapi_config(raw).alphaxiv

    assert cfg.enabled is True
    assert cfg.api_key_path == "keys/alphaxiv.key"


def test_resolve_api_key_also_accepts_an_alphaxiv_config(tmp_path):
    key_path = tmp_path / "alphaxiv.key"
    key_path.write_text("alpha-secret\n", encoding="utf-8")
    raw = {"litapi": {"alphaxiv": {"enabled": True, "api_key_path": str(key_path)}}}
    cfg = load_litapi_config(raw).alphaxiv

    assert resolve_api_key(cfg) == "alpha-secret"


def test_arxivxplorer_defaults_off_with_c0069_guardrail_values():
    cfg = load_litapi_config({}).arxivxplorer

    assert cfg.enabled is False
    assert cfg.base_url == "https://search.arxivxplorer.com"
    assert cfg.min_interval_s == 3.0  # C-0069: ">=3s spacing" floor
    assert cfg.daily_request_cap == 200
    assert cfg.cache_ttl_s == 86400


def test_arxivxplorer_reads_configured_values():
    raw = {
        "litapi": {
            "arxivxplorer": {"enabled": True, "daily_request_cap": 50, "min_interval_s": 5.0}
        }
    }
    cfg = load_litapi_config(raw).arxivxplorer

    assert cfg.enabled is True
    assert cfg.daily_request_cap == 50
    assert cfg.min_interval_s == 5.0


# ---------------------------------------------------------------------------
# arxiv_index (build-arxiv-kaggle-index session)
# ---------------------------------------------------------------------------


def test_arxiv_index_defaults():
    cfg = load_litapi_config({}).arxiv_index

    assert cfg.db_path == "data/arxiv_index.sqlite3"
    assert cfg.dims == 3072  # text-embedding-3-large's real dimensionality
    assert cfg.batch_size == 500
    assert cfg.member_glob == "*.jsonl"
    assert cfg.min_free_gb == 80.0  # the build machine's own disk-preflight floor
    assert cfg.api_key_path is None


def test_arxiv_index_reads_configured_values():
    raw = {
        "litapi": {
            "arxiv_index": {
                "db_path": "custom/idx.sqlite3",
                "dims": 1536,
                "batch_size": 100,
                "member_glob": "*.json",
                "min_free_gb": 10.0,
                "api_key_path": "keys/openai.key",
            }
        }
    }
    cfg = load_litapi_config(raw).arxiv_index

    assert cfg.db_path == "custom/idx.sqlite3"
    assert cfg.dims == 1536
    assert cfg.batch_size == 100
    assert cfg.member_glob == "*.json"
    assert cfg.min_free_gb == 10.0
    assert cfg.api_key_path == "keys/openai.key"


def test_resolve_api_key_also_accepts_an_arxiv_index_config(tmp_path):
    key_path = tmp_path / "openai.key"
    key_path.write_text("sk-openai-secret\n", encoding="utf-8")
    raw = {"litapi": {"arxiv_index": {"api_key_path": str(key_path)}}}
    cfg = load_litapi_config(raw).arxiv_index

    assert resolve_api_key(cfg) == "sk-openai-secret"


def test_resolve_api_key_arxiv_index_none_when_unconfigured():
    cfg = load_litapi_config({}).arxiv_index
    assert resolve_api_key(cfg) is None


def test_arxiv_license_lookup_and_oai_base_url_are_configurable():
    cfg = load_litapi_config(
        {"litapi": {"arxiv": {"license_lookup": False, "oai_base_url": "https://example.org/oai"}}}
    )

    assert cfg.arxiv.license_lookup is False
    assert cfg.arxiv.oai_base_url == "https://example.org/oai"


def test_max_total_wait_s_is_configurable_per_provider():
    cfg = load_litapi_config({"litapi": {"openalex": {"max_total_wait_s": 5.0}}})

    assert cfg.openalex.max_total_wait_s == 5.0
    assert cfg.semanticscholar.max_total_wait_s == 30.0


# ---------------------------------------------------------------------------
# [litapi.investigate] (lane SI part B, item B3)
# ---------------------------------------------------------------------------


def test_investigate_config_defaults_and_override():
    """FAILS BEFORE lane SI part B: there was no ``[litapi.investigate]``
    table. The defaults are the brief's; any key can be overridden; the whole
    table is what a dossier echoes as its ``thresholds``; a value outside its
    range is a problem the investigate verbs refuse on -- it does not break
    loading for the verbs that never read it."""
    from trialerror.litapi.config import InvestigateConfig

    defaults = load_litapi_config({}).investigate
    assert defaults == InvestigateConfig()
    assert defaults.to_dict() == {
        "title_floor": 0.90, "year_tolerance": 1, "foundational_before": 2000, "max_calls_per_seed": 8,
        "citing_limit": 20, "review_limit": 10, "references_limit": 10, "author_works_years": 10,
        "author_works_limit": 8, "abstract_max_words": 350,
    }
    assert defaults.problems() == []

    tuned = load_litapi_config(
        {"litapi": {"investigate": {"title_floor": 0.85, "foundational_before": 1990, "max_calls_per_seed": "5"}}}
    ).investigate
    assert (tuned.title_floor, tuned.foundational_before, tuned.max_calls_per_seed) == (0.85, 1990, 5)
    assert tuned.citing_limit == 20 and tuned.abstract_max_words == 350

    broken = load_litapi_config(
        {"litapi": {"investigate": {"title_floor": 1.5, "citing_limit": 0, "year_tolerance": -1}}}
    )
    assert broken.openalex.base_url == "https://api.openalex.org"  # everything else still loads
    problems = broken.investigate.problems()
    assert len(problems) == 3
    assert any("title_floor" in p for p in problems)
