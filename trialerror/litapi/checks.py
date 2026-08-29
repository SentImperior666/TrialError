"""``trialerror.litapi``'s doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` purely because this
file exists at ``trialerror/litapi/checks.py`` (that function's own docstring:
"a new subsystem registers checks by adding a file, never by editing this
function") -- landing this module required zero edits to
``trialerror/util/doctor.py`` or any other shared file.

Three checks now:

- ``litapi_config_present`` -- always runs; reports whether a program's
  ``trialerror.toml`` has a usable ``[litapi]`` section (warns, doesn't fail,
  on a missing ``mailto``/absent section -- both providers work keyless
  per the mining reports, just less politely/reliably). Pre-dates
  ``docs/EXTERNAL_API_FACTS.md``; kept as-is (see that check's own
  docstring below for why ``litapi_providers_ready`` is the more current
  source of truth on OpenAlex/S2 readiness specifically).
- ``litapi_live_reachable`` -- SKIPPED by default (no live network in
  doctor either, matching the F18 discipline this package's live-smoke
  test follows -- see ``trialerror.litapi.transport``'s module docstring).
  Only performs a real network call when ``TRIALERROR_LITAPI_LIVE_TESTS=1`` is
  set, the exact same env-flag gate the live-smoke test uses, so "doctor
  went live" and "the live-smoke test would run" are always the same
  condition.
- ``litapi_providers_ready`` (v3-acquisition build, C-0064 flags F1/F2
  RESOLVED) -- always runs, NO live network (config-inspection only, same
  posture as ``litapi_config_present``): per-provider readiness across all
  four providers now shipped, grounded in ``docs/EXTERNAL_API_FACTS.md``'s
  now-current facts rather than the original (pre-resolution) "keyless is
  fine" assumption. See that check's own docstring for the exact
  per-provider status vocabulary.
"""

from __future__ import annotations

import os

from trialerror.litapi.config import load_litapi_config, resolve_api_key
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "LIVE_TESTS_ENV_VAR",
    "OPENALEX_KEY_INFO_URL",
    "SEMANTICSCHOLAR_KEY_INFO_URL",
    "UNPAYWALL_INFO_URL",
    "ALPHAXIV_KEY_INFO_URL",
    "check_litapi_config_present",
    "check_litapi_live_reachable",
    "check_litapi_providers_ready",
]

LIVE_TESTS_ENV_VAR = "TRIALERROR_LITAPI_LIVE_TESTS"

#: Signup/info URLs surfaced by ``litapi_providers_ready`` below. Each is
#: taken VERBATIM from ``docs/EXTERNAL_API_FACTS.md``'s own "Source"
#: column for the relevant fact (not independently re-fetched/confirmed as
#: a literal "create a key here" deep link by this build -- the mining
#: report's own citation discipline: point at the documented, sourced
#: entry point, not a guessed subpage).
OPENALEX_KEY_INFO_URL = "https://help.openalex.org"  # EXTERNAL_API_FACTS.md FLAG 2 Source column
SEMANTICSCHOLAR_KEY_INFO_URL = "https://www.semanticscholar.org/product/api"  # FLAG 1 Source column
UNPAYWALL_INFO_URL = "https://unpaywall.org/faq"  # quick-confirms Source column
#: v-allarxiv-search build (docs/reviews/ALL_ARXIV_SEARCH.md): alphaXiv's own
#: MCP-setup docs page, the one this build actually fetched for signup/key
#: steps (see docs/USER_SETUP.md Sec 3c for the transcribed steps).
ALPHAXIV_KEY_INFO_URL = "https://www.alphaxiv.org/docs/mcp"


def _load_raw_config(ctx: DoctorContext) -> dict | None:
    if ctx.program_root is None:
        return None
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = ctx.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return None
    try:
        return load_config(cfg_path).raw
    except Exception:
        return None


@register_check("litapi_config_present", category="litapi")
def check_litapi_config_present(ctx: DoctorContext) -> CheckResult:
    raw = _load_raw_config(ctx)
    if raw is None:
        return CheckResult(
            name="litapi_config_present", category="litapi", status="skip",
            message="no trialerror.toml found (program_root not configured, or program not yet initialized)",
        )
    litapi_cfg = load_litapi_config(raw)
    warnings: list[str] = []
    if not litapi_cfg.openalex.mailto:
        warnings.append(
            "litapi.openalex.mailto is unset -- requests will land in OpenAlex's default (non-polite) pool"
        )
    if litapi_cfg.openalex.base_url and litapi_cfg.semanticscholar.base_url:
        pass  # both resolved (config or built-in default) -- nothing further to check here.

    status = "warn" if warnings else "pass"
    message = "; ".join(warnings) if warnings else "litapi config resolved for both providers"
    return CheckResult(
        name="litapi_config_present", category="litapi", status=status, message=message,
        details={
            "openalex_base_url": litapi_cfg.openalex.base_url,
            "openalex_mailto_set": bool(litapi_cfg.openalex.mailto),
            "semanticscholar_base_url": litapi_cfg.semanticscholar.base_url,
            "semanticscholar_api_key_path_set": bool(litapi_cfg.semanticscholar.api_key_path),
        },
    )


@register_check("litapi_live_reachable", category="litapi")
def check_litapi_live_reachable(ctx: DoctorContext) -> CheckResult:
    if os.environ.get(LIVE_TESTS_ENV_VAR) != "1":
        return CheckResult(
            name="litapi_live_reachable", category="litapi", status="skip",
            message=f"live network checks are opt-in; set {LIVE_TESTS_ENV_VAR}=1 to run them",
        )

    raw = _load_raw_config(ctx)
    litapi_cfg = load_litapi_config(raw)

    from trialerror.litapi.transport import UrllibTransport

    transport = UrllibTransport(default_timeout_s=5.0)
    results: dict[str, str] = {}
    offenders: list[str] = []
    for provider_cfg in (litapi_cfg.openalex, litapi_cfg.semanticscholar):
        try:
            response = transport.get(provider_cfg.base_url, timeout_s=5.0)
            results[provider_cfg.name] = f"HTTP {response.status_code}"
            if response.status_code >= 500:
                offenders.append(provider_cfg.name)
        except Exception as exc:  # noqa: BLE001 - deliberate: one dead provider must not crash the check
            results[provider_cfg.name] = f"error: {exc}"
            offenders.append(provider_cfg.name)

    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} provider(s) unreachable: {', '.join(offenders)}"
        if offenders else "both providers reachable"
    )
    return CheckResult(
        name="litapi_live_reachable", category="litapi", status=status, message=message, details=results
    )


@register_check("litapi_providers_ready", category="litapi")
def check_litapi_providers_ready(ctx: DoctorContext) -> CheckResult:
    """Config-inspection-only (no network -- same posture as
    ``litapi_config_present``) per-provider readiness, grounded in
    ``docs/EXTERNAL_API_FACTS.md``'s now-resolved facts:

    - ``openalex``: ``"ready"`` if an API key is configured, else
      ``"needs-key"`` -- OpenAlex made a key MANDATORY 2026-02-13 (a
      keyless call now only gets a 100-credit grace allowance, then HTTP
      409 on every subsequent call). This is the one status this check
      treats as closest to "will actually break" among the three
      needs-setup states, since the grace allowance is small and one-time.
    - ``semanticscholar``: ``"ready"`` if a key is configured, else
      ``"throttled-shared-pool"`` -- keyless calls still WORK, just against
      a GLOBAL 5,000-req/5-min pool shared with every other unauthenticated
      caller on the planet (~16.7 req/s aggregate, not per-caller); a free
      key raises this to a dedicated 1 RPS tier on the endpoints this
      package's client actually calls (search/batch/recommendations).
    - ``arxiv``: always ``"ready"`` -- fully keyless by design; the 1
      request/3 seconds ToU limit is enforced client-side
      (``ArxivProvider``'s own ``RateLimiter``, config-defaulted to
      exactly that pace), not a readiness gate.
    - ``unpaywall``: ``"ready"`` if ``[litapi.unpaywall].mailto`` (the
      required ``email=`` identifier) is configured, else
      ``"needs-email"`` -- ``UnpaywallProvider`` itself hard-refuses every
      call with no email configured (``ProviderConfigError``), so this
      status predicts a real, immediate failure, not just a degraded mode.
    - ``alphaxiv`` (v-allarxiv-search build) -- NOT a :class:`Provider`
      row like the four above (alphaXiv is wired as a standalone MCP
      connection, not a REST client this package calls -- see
      ``trialerror.litapi.config.AlphaxivConfig``'s own docstring). Three-value
      vocabulary, distinct from the other rows on purpose (this build never
      makes a live alphaXiv call to verify anything, unlike the other four,
      which this package's own clients actually exercise):
      ``"disabled"`` (``[litapi.alphaxiv].enabled = false``, the default --
      not a problem, an intentional opt-out); ``"needs-key"`` (enabled but
      no API key file resolves -- note this is advisory only: alphaXiv's
      default auth mode is OAuth 2.1 browser sign-in, which needs no key at
      all, so "needs-key" here means "no key configured for the
      non-interactive/headless path specifically", not "will fail");
      ``"ready-untested-live"`` (enabled AND a key resolves) -- named
      untested deliberately: this build never created an alphaXiv account
      or made a live call (operator-only step, see
      ``docs/USER_SETUP.md`` Sec 3c), so "ready" alone would overclaim.
    - ``arxivxplorer`` (v-allarxiv-search build, C-0069) -- two-value:
      ``"disabled"`` (``[litapi.arxivxplorer].enabled = false``, the
      default -- C-0069's binding guardrail) or ``"ready-untested-live"``
      (enabled -- this build's own offline tests exercise
      :class:`~trialerror.litapi.providers.arxivxplorer_web.ArxivxplorerWebProvider`
      against canned fixtures only, per that module's own EXPERIMENTAL/
      FRAGILE disclosure; no live call happens from this check either).

    Overall ``status`` is ``"warn"`` when any provider needs setup or is
    running throttled, ``"pass"`` when every provider is fully ready --
    deliberately never ``"fail"``, matching ``litapi_config_present``'s own
    "a missing-but-fixable config knob doesn't fail the whole doctor run"
    philosophy; ``litapi_live_reachable`` (a real transport failure) is
    the check that reserves ``"fail"`` for this subsystem.
    """
    raw = _load_raw_config(ctx)
    if raw is None:
        return CheckResult(
            name="litapi_providers_ready", category="litapi", status="skip",
            message="no trialerror.toml found (program_root not configured, or program not yet initialized)",
        )
    litapi_cfg = load_litapi_config(raw)
    program_root = ctx.program_root

    statuses: dict[str, dict] = {}

    if resolve_api_key(litapi_cfg.openalex, program_root=program_root):
        statuses["openalex"] = {"status": "ready", "message": "API key configured"}
    else:
        statuses["openalex"] = {
            "status": "needs-key",
            "message": (
                "OpenAlex has required a free API key since 2026-02-13 -- keyless calls get a "
                "one-time 100-credit grace allowance, then HTTP 409 on every subsequent call "
                "(docs/EXTERNAL_API_FACTS.md FLAG 2)"
            ),
            "signup_url": OPENALEX_KEY_INFO_URL,
        }

    if resolve_api_key(litapi_cfg.semanticscholar, program_root=program_root):
        statuses["semanticscholar"] = {
            "status": "ready",
            "message": "API key configured (1 RPS search/batch/recommendations tier, 10 RPS other endpoints)",
        }
    else:
        statuses["semanticscholar"] = {
            "status": "throttled-shared-pool",
            "message": (
                "running keyless -- shares a GLOBAL 5,000-req/5-min pool with every other "
                "unauthenticated caller on the planet (~16.7 req/s aggregate, not per-caller); a "
                "free key raises this to a dedicated 1 RPS tier (docs/EXTERNAL_API_FACTS.md FLAG 1)"
            ),
            "signup_url": SEMANTICSCHOLAR_KEY_INFO_URL,
        }

    statuses["arxiv"] = {
        "status": "ready",
        "message": "keyless by design; 1 request/3 seconds client-side throttle enforced (info.arxiv.org ToU)",
    }

    if litapi_cfg.unpaywall.mailto:
        statuses["unpaywall"] = {"status": "ready", "message": "email identifier configured"}
    else:
        statuses["unpaywall"] = {
            "status": "needs-email",
            "message": (
                "Unpaywall requires an email= identifier on every call -- set "
                "[litapi.unpaywall].mailto in trialerror.toml (UnpaywallProvider refuses every call "
                "with none configured)"
            ),
            "signup_url": UNPAYWALL_INFO_URL,
        }

    # v-allarxiv-search build: alphaxiv/arxivxplorer are NOT counted into
    # needs_setup/throttled below -- see this check's own docstring. Neither
    # is a live-verified Provider this package calls, so neither can make
    # the overall doctor status worse than the four rows above already do;
    # "disabled" is the intentional default, not something to warn about.
    if not litapi_cfg.alphaxiv.enabled:
        statuses["alphaxiv"] = {
            "status": "disabled",
            "message": "opt-in feature, off by default -- set [litapi.alphaxiv].enabled = true after "
            "completing the operator-only account/key steps (docs/USER_SETUP.md Sec 3c)",
        }
    elif resolve_api_key(litapi_cfg.alphaxiv, program_root=program_root):
        statuses["alphaxiv"] = {
            "status": "ready-untested-live",
            "message": "enabled with an API key configured -- this check never makes a live call to "
            "verify it (see this check's own docstring)",
        }
    else:
        statuses["alphaxiv"] = {
            "status": "needs-key",
            "message": "enabled with no API key file configured for the non-interactive path -- either "
            "set [litapi.alphaxiv].api_key_path or rely on the MCP server's default OAuth 2.1 "
            "browser sign-in instead (advisory only, see this check's own docstring)",
            "signup_url": ALPHAXIV_KEY_INFO_URL,
        }

    statuses["arxivxplorer"] = (
        {
            "status": "ready-untested-live",
            "message": "enabled -- offline-tested against canned fixtures only, never against the live "
            "site by this check (see trialerror.litapi.providers.arxivxplorer_web's own EXPERIMENTAL/"
            "FRAGILE docstring)",
        }
        if litapi_cfg.arxivxplorer.enabled
        else {
            "status": "disabled",
            "message": "opt-in feature, off by default (C-0069's binding guardrail) -- set "
            "[litapi.arxivxplorer].enabled = true to turn it on",
        }
    )

    needs_setup = [name for name, s in statuses.items() if s["status"] in ("needs-key", "needs-email")]
    throttled = [name for name, s in statuses.items() if s["status"] == "throttled-shared-pool"]

    if needs_setup:
        status = "warn"
        message = f"{len(needs_setup)} provider(s) need setup: {', '.join(sorted(needs_setup))}"
        if throttled:
            message += f"; {len(throttled)} running throttled: {', '.join(sorted(throttled))}"
    elif throttled:
        status = "warn"
        message = f"{len(throttled)} provider(s) running in a degraded/throttled mode: {', '.join(sorted(throttled))}"
    else:
        status = "pass"
        message = "all litapi providers ready"

    return CheckResult(
        name="litapi_providers_ready", category="litapi", status=status, message=message, details=statuses
    )
