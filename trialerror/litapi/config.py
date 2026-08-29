"""``trialerror.toml`` ``[litapi]`` section loader. Mirrors
``trialerror.util.config``'s own "fields read generically" posture (that
module's docstring, verbatim): the fields this section exposes are read
into small typed dataclasses HERE (rather than staying a raw dict, unlike
M0's own not-yet-consumed ``[models]``/``[license]`` tables) because this
build is the first and only consumer of ``[litapi]`` -- there is no future
module whose own build session gets to decide the shape.

Shape (all keys optional; every field has a conservative default so a
program with no ``[litapi]`` section at all still gets a working,
FakeTransport-free-usable config object)::

    [litapi.openalex]
    base_url = "https://api.openalex.org"
    mailto = "you@example.org"          # OpenAlex "polite pool" (design brief)
    api_key_path = ""                   # path to a file holding the key; NEVER inline
    min_interval_s = 1.0                # conservative default -- see TODO below
    retry_attempts = 3                  # mining report: OpenAlex client retries 3x on ReadTimeout/500
    timeout_s = 15.0

    [litapi.semanticscholar]
    base_url = "https://api.semanticscholar.org"
    api_key_path = ""
    api_key_header = "x-api-key"
    min_interval_s = 1.0
    retry_attempts = 5                  # mining report: S2 client retries 5x on flaky SSL/403
    timeout_s = 15.0

    [litapi.arxiv]
    base_url = "http://export.arxiv.org/api"
    min_interval_s = 3.0                # info.arxiv.org ToU: 1 request per 3 seconds (enforced, not a guess)
    retry_attempts = 3
    timeout_s = 15.0

    [litapi.unpaywall]
    base_url = "https://api.unpaywall.org/v2"
    mailto = "you@example.org"          # REQUIRED -- Unpaywall's email= identification param (see this
                                         # module's docstring: reuses the same field name OpenAlex's
                                         # now-discontinued polite-pool `mailto` used, for the identical
                                         # "identify yourself in every call" purpose)
    min_interval_s = 1.0
    retry_attempts = 3
    timeout_s = 15.0

    [litapi.alphaxiv]
    # v-allarxiv-search build (docs/reviews/ALL_ARXIV_SEARCH.md): alphaXiv is
    # NOT a REST `Provider` -- it's an official MCP server
    # (`https://api.alphaxiv.org/mcp/v1`), wired as a standalone `claude mcp
    # add` connection (docs/USER_SETUP.md Sec 3c has the exact recipe), never
    # forced into the get_by_doi/get_by_arxiv/search/get_citations protocol.
    # This section is a READINESS GATE ONLY -- trialerror.litapi.checks folds it
    # into litapi_providers_ready's per-provider status dict so `trialerror lit
    # doctor` reports it alongside the four real Provider clients, but no
    # code in this package ever calls alphaXiv's API directly.
    enabled = false                     # default OFF -- the operator opts in explicitly (no account/ToS
                                         # acceptance happens on this session's or this package's behalf)
    api_key_path = ""                   # path to a file holding an alphaXiv API key (Settings > API Keys
                                         # on alphaxiv.org); NEVER inline. Leaving this empty while
                                         # enabled = true means the operator intends OAuth 2.1 browser
                                         # sign-in instead (the MCP server's own default auth mode) --
                                         # that path has no key for this config to resolve at all.
    mcp_endpoint = "https://api.alphaxiv.org/mcp/v1"  # override only if alphaXiv changes its endpoint

    [litapi.arxivxplorer]
    # v-allarxiv-search build, C-0069 (browser-equivalent client, guardrails
    # binding): arxivxplorer.com's search flow, replayed politely and
    # disabled by default. See trialerror.litapi.providers.arxivxplorer_web's
    # module docstring for the full guardrail list and the EXPERIMENTAL/
    # FRAGILE disclosure -- this section only carries the gate + tuning
    # knobs, never the request-shape facts themselves (those live in code,
    # discovered by live browser-network inspection, not guessed here).
    enabled = false                     # default OFF -- C-0069's binding "disabled-by-default config gate"
    base_url = "https://search.arxivxplorer.com"  # the recovered search-API host (NOT arxivxplorer.com
                                         # itself, which is the static frontend -- see module docstring)
    min_interval_s = 3.0                # C-0069 guardrail floor: ">=3s spacing" -- same pacing class as
                                         # arxiv.py's ToU-enforced interval, chosen for the same reason
                                         # (no published rate limit exists to size against, so this uses
                                         # the one conservative number this package already trusts)
    daily_request_cap = 200             # C-0069 guardrail: "daily cap" -- mission-specified default
    cache_ttl_s = 86400                 # 24h -- reuse the sqlite response cache before re-requesting an
                                         # identical (query, filters) tuple at all (politeness, not just pacing)
    timeout_s = 15.0
    retry_attempts = 3

    [litapi.arxiv_index]
    # build-arxiv-kaggle-index session: the standalone all-arXiv semantic
    # search index over the arXiv Xplorer author's Kaggle-published
    # embeddings dataset (tomtum/openai-arxiv-embeddings -- MIT license,
    # OpenAI text-embedding-3-large, 3072-dim, title+abstract only,
    # ~2.7-2.9M rows, ~34.9GB zip). See trialerror.arxiv_index's package
    # docstring for the full architecture + the ASSUMED-schema disclosure.
    # This is a SEPARATE store from knowledge.db -- db_path is its own file,
    # not [paths].stores_dir.
    db_path = "data/arxiv_index.sqlite3"  # relative to program_root unless absolute; data/ is gitignored
    dims = 3072                         # text-embedding-3-large's real dimensionality (dims-sanity doctor check)
    batch_size = 500                    # ingest commit batch size (trialerror.arxiv_index.ingest)
    member_glob = "*.jsonl"             # ASSUMED zip-member glob -- see trialerror.arxiv_index's own docstring
    min_free_gb = 80.0                  # HARD FACT this build was given: 106GB free on the build machine;
                                         # this is the disk-preflight floor a build refuses to start below
    api_key_path = ""                   # path to a file holding an OpenAI API key (query-time embedding
                                         # calls ONLY -- the corpus vectors are precomputed, never re-embedded
                                         # by this package); NEVER inline. resolve_api_key's usual discipline.

TRIALERROR-DEV-NOTE / TODO (per the C-0064 litapi-preview mission brief,
verbatim constraint -- "DO NOT hardcode rate-limit numbers; read them from
trialerror.toml config with conservative defaults + a clear TODO pointing at
docs/EXTERNAL_API_FACTS.md"): the ``min_interval_s`` defaults below (1.0s
= ~1 request/second) are deliberately conservative placeholders, NOT a
verified rate-limit figure, for **openalex**/**semanticscholar** still.
The mining reports flagged both providers' real limits as unresolved
(``docs/mining/S1-scilit-1__semantic-scholar-api.md``: "1000/sec
unauthenticated vs 1 RPS keyed" framing, unverified, and an immediate HTTP
429 on a single live test call during that mining session;
``docs/mining/S1-scilit-1__openalex-api.md``: a `cost_usd`-per-response
field suggesting a pricing tier exists, pricing page not independently
fetched).

**UPDATE (C-0064 v3-acquisition build, session 1f478c74c):**
``docs/EXTERNAL_API_FACTS.md`` now exists (the concurrent "flag-probes"
agent's own follow-up landed it same-session) and gives REAL, sourced
numbers for both flagged providers -- S2's real unauthenticated ceiling is
a **5,000-req/5-min GLOBAL SHARED pool** (~16.7 req/s aggregate across
every unauthenticated caller on the planet, not per-caller), keyed access
starts at **1 RPS** on search/batch/recommendations (10 RPS elsewhere);
OpenAlex now **requires** a key as of 2026-02-13 (100-credit keyless grace
then HTTP 409), free-tier hard ceiling **100 req/s**. This build
deliberately does NOT change the two ``_DEFAULT_MIN_INTERVAL_S``/
``_DEFAULT_RETRY_ATTEMPTS`` entries below for openalex/semanticscholar --
that is a numeric-tuning change out of this build's own scoped deliverable
list (its job was the KEY-GATED READINESS doctor check --
``trialerror.litapi.checks.check_litapi_providers_ready`` -- which is the
mechanism that now actually surfaces "OpenAlex needs a key" /
"Semantic Scholar is running on a throttled shared pool" to a caller, not
a client-side interval retune). The two NEW providers this build adds
(**arxiv**, **unpaywall**) get their defaults grounded directly in
``docs/EXTERNAL_API_FACTS.md``'s quick-confirms table from day one (see
their entries in :data:`_DEFAULT_MIN_INTERVAL_S` below) -- unlike
openalex/semanticscholar's placeholders, these were never unverified
guesses to begin with. A program can always override any of these four
providers' ``min_interval_s`` explicitly in its own ``trialerror.toml``, which
always wins over every built-in default here either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ProviderApiConfig",
    "AlphaxivConfig",
    "ArxivxplorerConfig",
    "ArxivIndexConfig",
    "LitApiConfig",
    "load_litapi_config",
    "resolve_api_key",
]

#: See the module docstring's TRIALERROR-DEV-NOTE. openalex/semanticscholar
#: remain conservative placeholders, not a verified figure. arxiv IS the
#: documented, enforced ToU limit (docs/EXTERNAL_API_FACTS.md
#: quick-confirms: "1 request per 3 seconds ... official ToU wording").
#: unpaywall is grounded in that same table's "~100,000 calls/day"
#: convention (flagged there as WebSearch-aggregated, not primary-source-
#: verified) -- 1.0 req/s sustained stays comfortably under that daily
#: budget (~86,400/day at 1.0s) with margin, rather than pacing right up
#: against an unconfirmed ceiling. Config always overrides every entry here.
_DEFAULT_MIN_INTERVAL_S: dict[str, float] = {
    "openalex": 1.0,
    "semanticscholar": 1.0,
    "arxiv": 3.0,
    "unpaywall": 1.0,
}
#: Mining-report-grounded retry counts (see this module's docstring) --
#: openalex/semanticscholar ARE lifted near-verbatim from observed
#: production behavior in paper-qa's client code, not a rate-limit guess.
#: arxiv/unpaywall have no equivalent documented retry-count precedent (no
#: mining report covered either client's retry behavior) -- 3 is the same
#: conservative default openalex/unpaywall already use, not independently
#: grounded for these two specifically.
_DEFAULT_RETRY_ATTEMPTS: dict[str, int] = {
    "openalex": 3,
    "semanticscholar": 5,
    "arxiv": 3,
    "unpaywall": 3,
}
_DEFAULT_BASE_URLS: dict[str, str] = {
    "openalex": "https://api.openalex.org",
    "semanticscholar": "https://api.semanticscholar.org",
    # export.arxiv.org is arXiv's own documented dedicated API host (distinct
    # from the arxiv.org web host) -- path is completed by the provider's
    # own "/query" suffix, same base_url+path convention openalex/s2 use.
    "arxiv": "http://export.arxiv.org/api",
    "unpaywall": "https://api.unpaywall.org/v2",
}
_DEFAULT_TIMEOUT_S = 15.0
#: known provider names -- the one place :func:`_provider_config`'s
#: per-name default lookups and :meth:`LitApiConfig.provider`'s routing
#: both key off of, so adding a 5th provider later touches exactly the
#: four dicts above + this tuple + the two extra fields below, nothing else.
_PROVIDER_NAMES: tuple[str, ...] = ("openalex", "semanticscholar", "arxiv", "unpaywall")


@dataclass(frozen=True)
class ProviderApiConfig:
    """Resolved ``[litapi.<provider>]`` config for one provider."""

    name: str
    base_url: str
    mailto: str | None = None
    api_key_path: str | None = None
    api_key_header: str = "x-api-key"
    min_interval_s: float = 1.0
    retry_attempts: int = 3
    retry_on_status: tuple[int, ...] = ()
    timeout_s: float = _DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class AlphaxivConfig:
    """``[litapi.alphaxiv]`` -- a READINESS-GATE-ONLY config (module
    docstring): alphaXiv is wired as a standalone MCP connection
    (``docs/USER_SETUP.md`` Sec 3c), never as a :class:`Provider`. Nothing
    in this package reads ``api_key_path`` to make a call -- only
    ``trialerror.litapi.checks`` resolves it, to report readiness."""

    enabled: bool = False
    api_key_path: str | None = None
    mcp_endpoint: str = "https://api.alphaxiv.org/mcp/v1"


@dataclass(frozen=True)
class ArxivxplorerConfig:
    """``[litapi.arxivxplorer]`` -- gate + tuning knobs for
    :class:`trialerror.litapi.providers.arxivxplorer_web.ArxivxplorerWebProvider`
    (C-0069). ``enabled`` defaults ``False`` (the binding guardrail); every
    other field has a conservative default matching the module docstring's
    disclosed guardrails, never a value the caller must supply."""

    enabled: bool = False
    base_url: str = "https://search.arxivxplorer.com"
    min_interval_s: float = 3.0
    daily_request_cap: int = 200
    cache_ttl_s: int = 86400
    timeout_s: float = 15.0
    retry_attempts: int = 3


@dataclass(frozen=True)
class ArxivIndexConfig:
    """``[litapi.arxiv_index]`` -- the standalone all-arXiv semantic search
    index (``trialerror.arxiv_index`` package, build-arxiv-kaggle-index
    session). ``api_key_path`` is duck-typed identically to
    :class:`ProviderApiConfig`/:class:`AlphaxivConfig` (both share the same
    ``api_key_path``-only-source attribute shape) so
    :func:`resolve_api_key` accepts this dataclass too without any
    reimplementation -- see that function's own docstring."""

    db_path: str = "data/arxiv_index.sqlite3"
    dims: int = 3072
    batch_size: int = 500
    member_glob: str = "*.jsonl"
    min_free_gb: float = 80.0
    api_key_path: str | None = None


@dataclass(frozen=True)
class LitApiConfig:
    """The full ``[litapi]`` section, resolved."""

    openalex: ProviderApiConfig
    semanticscholar: ProviderApiConfig
    arxiv: ProviderApiConfig
    unpaywall: ProviderApiConfig
    alphaxiv: AlphaxivConfig
    arxivxplorer: ArxivxplorerConfig
    arxiv_index: ArxivIndexConfig

    def provider(self, name: str) -> ProviderApiConfig:
        if name == "openalex":
            return self.openalex
        if name == "semanticscholar":
            return self.semanticscholar
        if name == "arxiv":
            return self.arxiv
        if name == "unpaywall":
            return self.unpaywall
        raise KeyError(f"no such litapi provider config: {name!r} (known: {_PROVIDER_NAMES!r})")


def _provider_config(name: str, raw: dict[str, Any]) -> ProviderApiConfig:
    retry_on_status = raw.get("retry_on_status")
    if retry_on_status is None:
        # Mining-report-grounded per-provider defaults (module docstring):
        # OpenAlex's production client retries on HTTP 500; Semantic
        # Scholar's retries on HTTP 403 (both empirically observed, per
        # inline comments in the paper-qa client code the mining reports
        # cite). ReadTimeout/SSL-error retries are transport-level (not
        # status-code-keyed) and are out of this dataclass's scope.
        # arxiv/unpaywall (v3-acquisition build): no equivalent mining-report
        # precedent exists for either -- 500 is the same generic
        # server-error-is-transient default every other unconfirmed choice
        # in this module uses; arxiv additionally retries on 503 (a
        # documented-elsewhere-in-the-ecosystem "temporarily overloaded"
        # code for the export.arxiv.org host, not independently confirmed
        # this session -- TRIALERROR-DEV-NOTE, flagged).
        if name == "openalex":
            retry_on_status = (500,)
        elif name == "semanticscholar":
            retry_on_status = (403,)
        elif name == "arxiv":
            retry_on_status = (500, 503)
        else:  # unpaywall
            retry_on_status = (500,)
    return ProviderApiConfig(
        name=name,
        base_url=str(raw.get("base_url", _DEFAULT_BASE_URLS[name])),
        mailto=raw.get("mailto"),
        api_key_path=raw.get("api_key_path") or None,
        api_key_header=str(raw.get("api_key_header", "x-api-key")),
        min_interval_s=float(raw.get("min_interval_s", _DEFAULT_MIN_INTERVAL_S[name])),
        retry_attempts=int(raw.get("retry_attempts", _DEFAULT_RETRY_ATTEMPTS[name])),
        retry_on_status=tuple(retry_on_status),
        timeout_s=float(raw.get("timeout_s", _DEFAULT_TIMEOUT_S)),
    )


def _alphaxiv_config(raw: dict[str, Any]) -> AlphaxivConfig:
    return AlphaxivConfig(
        enabled=bool(raw.get("enabled", False)),
        api_key_path=raw.get("api_key_path") or None,
        mcp_endpoint=str(raw.get("mcp_endpoint", "https://api.alphaxiv.org/mcp/v1")),
    )


def _arxivxplorer_config(raw: dict[str, Any]) -> ArxivxplorerConfig:
    return ArxivxplorerConfig(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url", "https://search.arxivxplorer.com")),
        min_interval_s=float(raw.get("min_interval_s", 3.0)),
        daily_request_cap=int(raw.get("daily_request_cap", 200)),
        cache_ttl_s=int(raw.get("cache_ttl_s", 86400)),
        timeout_s=float(raw.get("timeout_s", 15.0)),
        retry_attempts=int(raw.get("retry_attempts", 3)),
    )


def _arxiv_index_config(raw: dict[str, Any]) -> ArxivIndexConfig:
    return ArxivIndexConfig(
        db_path=str(raw.get("db_path", "data/arxiv_index.sqlite3")),
        dims=int(raw.get("dims", 3072)),
        batch_size=int(raw.get("batch_size", 500)),
        member_glob=str(raw.get("member_glob", "*.jsonl")),
        min_free_gb=float(raw.get("min_free_gb", 80.0)),
        api_key_path=raw.get("api_key_path") or None,
    )


def load_litapi_config(program_config_raw: dict[str, Any] | None) -> LitApiConfig:
    """Build a :class:`LitApiConfig` from a program's already-loaded
    ``trialerror.toml`` ``raw`` dict (``trialerror.util.config.ProgramConfig.raw``,
    or ``{}``/``None`` for "no trialerror.toml found" -- every field defaults
    cleanly, per this module's docstring)."""
    root = (program_config_raw or {}).get("litapi", {}) or {}
    return LitApiConfig(
        openalex=_provider_config("openalex", root.get("openalex", {}) or {}),
        semanticscholar=_provider_config("semanticscholar", root.get("semanticscholar", {}) or {}),
        arxiv=_provider_config("arxiv", root.get("arxiv", {}) or {}),
        unpaywall=_provider_config("unpaywall", root.get("unpaywall", {}) or {}),
        alphaxiv=_alphaxiv_config(root.get("alphaxiv", {}) or {}),
        arxivxplorer=_arxivxplorer_config(root.get("arxivxplorer", {}) or {}),
        arxiv_index=_arxiv_index_config(root.get("arxiv_index", {}) or {}),
    )


def resolve_api_key(
    cfg: ProviderApiConfig | AlphaxivConfig | ArxivIndexConfig, *, program_root: Path | None = None
) -> str | None:
    """Read an API key ONLY from the configured file path (design brief:
    "Never read a key except from a configured path; never log it.") --
    there is no environment-variable or inline-config fallback here on
    purpose, so a key can never end up committed inside ``trialerror.toml``
    itself or an accidentally-logged env dump. Returns ``None`` when no
    path is configured or the file doesn't exist (callers treat that as
    "proceed keyless", matching both providers' documented free/keyless
    tiers per the mining reports) -- this function never raises for a
    missing key, only for an unreadable *existing* file (a real
    misconfiguration worth surfacing). Accepts :class:`AlphaxivConfig` and
    :class:`ArxivIndexConfig` too (v-allarxiv-search /
    build-arxiv-kaggle-index builds) -- all three dataclasses share the
    same ``api_key_path``-only-source shape, and their own readiness/
    encoder-seam code (``trialerror.litapi.checks``, ``trialerror.arxiv_index.encoder``)
    reuses this exact function rather than reimplementing the "never log
    it" discipline a third time."""
    if not cfg.api_key_path:
        return None
    path = Path(cfg.api_key_path)
    if not path.is_absolute() and program_root is not None:
        path = program_root / path
    if not path.is_file():
        return None
    key = path.read_text(encoding="utf-8").strip()
    return key or None
