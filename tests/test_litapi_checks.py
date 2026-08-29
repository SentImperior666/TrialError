"""Tests for ``trialerror.litapi.checks``: ``litapi_config_present`` (always
runs) and ``litapi_live_reachable`` (skipped unless
``TRIALERROR_LITAPI_LIVE_TESTS=1`` -- and even THEN, this test suite never
touches the real network: the live-path test below monkeypatches
``trialerror.litapi.transport.UrllibTransport`` with an in-file fake so the
branch logic is exercised with zero sockets opened, honoring the design
brief's "NO live network calls in tests" even for the one check whose
whole job is checking live reachability)."""

from __future__ import annotations

from trialerror.litapi import checks
from trialerror.litapi.transport import TransportResponse
from trialerror.util.doctor import DoctorContext


def test_config_present_skips_with_no_program_root():
    result = checks.check_litapi_config_present(DoctorContext(program_root=None))
    assert result.status == "skip"


def test_config_present_skips_with_no_trialerror_toml(tmp_path):
    result = checks.check_litapi_config_present(DoctorContext(program_root=tmp_path))
    assert result.status == "skip"


def test_config_present_warns_when_mailto_unset(tmp_path):
    (tmp_path / "trialerror.toml").write_text('[program]\nid = "x"\n', encoding="utf-8")
    result = checks.check_litapi_config_present(DoctorContext(program_root=tmp_path))
    assert result.status == "warn"
    assert "mailto" in result.message


def test_config_present_passes_when_mailto_configured(tmp_path):
    (tmp_path / "trialerror.toml").write_text(
        '[program]\nid = "x"\n\n[litapi.openalex]\nmailto = "me@example.org"\n', encoding="utf-8"
    )
    result = checks.check_litapi_config_present(DoctorContext(program_root=tmp_path))
    assert result.status == "pass"
    assert result.details["openalex_mailto_set"] is True


def test_live_reachable_skips_without_env_flag(monkeypatch):
    monkeypatch.delenv(checks.LIVE_TESTS_ENV_VAR, raising=False)
    result = checks.check_litapi_live_reachable(DoctorContext(program_root=None))
    assert result.status == "skip"


class _FakeUrllibTransport:
    """In-file stand-in for the real transport -- see this module's
    docstring: keeps this test fully offline even while exercising the
    live-reachability check's own branch logic."""

    def __init__(self, *, default_timeout_s: float = 5.0):
        self.default_timeout_s = default_timeout_s

    def get(self, url, *, headers=None, timeout_s=None):
        if "semanticscholar" in url:
            raise RuntimeError("simulated network failure")
        return TransportResponse(status_code=200, json_body=None, text="")


def test_live_reachable_runs_and_reports_per_provider_when_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv(checks.LIVE_TESTS_ENV_VAR, "1")
    monkeypatch.setattr("trialerror.litapi.transport.UrllibTransport", _FakeUrllibTransport)

    result = checks.check_litapi_live_reachable(DoctorContext(program_root=tmp_path))

    assert result.status == "fail"
    assert "semanticscholar" in result.message
    assert result.details["openalex"] == "HTTP 200"
    assert "error" in result.details["semanticscholar"]


class _AllOkTransport:
    def __init__(self, *, default_timeout_s: float = 5.0):
        pass

    def get(self, url, *, headers=None, timeout_s=None):
        return TransportResponse(status_code=200)


def test_live_reachable_passes_when_both_providers_ok(tmp_path, monkeypatch):
    monkeypatch.setenv(checks.LIVE_TESTS_ENV_VAR, "1")
    monkeypatch.setattr("trialerror.litapi.transport.UrllibTransport", _AllOkTransport)

    result = checks.check_litapi_live_reachable(DoctorContext(program_root=tmp_path))

    assert result.status == "pass"


# ---------------------------------------------------------------------------
# litapi_providers_ready (v3-acquisition build) -- config-inspection only,
# NO network at all (unlike litapi_live_reachable above), so every test
# here needs no env-flag gating.
# ---------------------------------------------------------------------------


def test_providers_ready_skips_with_no_program_root():
    result = checks.check_litapi_providers_ready(DoctorContext(program_root=None))
    assert result.status == "skip"


def test_providers_ready_skips_with_no_trialerror_toml(tmp_path):
    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))
    assert result.status == "skip"


def test_providers_ready_all_needs_setup_when_nothing_configured(tmp_path):
    (tmp_path / "trialerror.toml").write_text('[program]\nid = "x"\n', encoding="utf-8")

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.status == "warn"
    assert result.details["openalex"]["status"] == "needs-key"
    assert result.details["openalex"]["signup_url"] == checks.OPENALEX_KEY_INFO_URL
    assert result.details["semanticscholar"]["status"] == "throttled-shared-pool"
    assert result.details["semanticscholar"]["signup_url"] == checks.SEMANTICSCHOLAR_KEY_INFO_URL
    assert result.details["arxiv"]["status"] == "ready"
    assert result.details["unpaywall"]["status"] == "needs-email"
    assert result.details["unpaywall"]["signup_url"] == checks.UNPAYWALL_INFO_URL
    assert "openalex" in result.message
    assert "unpaywall" in result.message


def test_providers_ready_unpaywall_ready_once_mailto_configured(tmp_path):
    (tmp_path / "trialerror.toml").write_text(
        '[program]\nid = "x"\n\n[litapi.unpaywall]\nmailto = "me@example.org"\n', encoding="utf-8"
    )

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.details["unpaywall"]["status"] == "ready"
    assert result.status == "warn"  # openalex/semanticscholar still unconfigured


def test_providers_ready_openalex_ready_once_key_configured(tmp_path):
    key_path = tmp_path / "openalex.key"
    key_path.write_text("secret-oa-key", encoding="utf-8")
    (tmp_path / "trialerror.toml").write_text(
        f'[program]\nid = "x"\n\n[litapi.openalex]\napi_key_path = "{key_path.name}"\n', encoding="utf-8"
    )

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.details["openalex"]["status"] == "ready"


def test_providers_ready_pass_when_all_four_configured(tmp_path):
    oa_key = tmp_path / "openalex.key"
    oa_key.write_text("secret-oa-key", encoding="utf-8")
    s2_key = tmp_path / "s2.key"
    s2_key.write_text("secret-s2-key", encoding="utf-8")
    (tmp_path / "trialerror.toml").write_text(
        "[program]\nid = \"x\"\n\n"
        f'[litapi.openalex]\napi_key_path = "{oa_key.name}"\n\n'
        f'[litapi.semanticscholar]\napi_key_path = "{s2_key.name}"\n\n'
        '[litapi.unpaywall]\nmailto = "me@example.org"\n',
        encoding="utf-8",
    )

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.status == "pass"
    assert result.message == "all litapi providers ready"
    # scoped to the four REST Provider rows -- alphaxiv/arxivxplorer (v-allarxiv-search
    # build) are opt-in features this test never enables, so both stay "disabled"
    # (an intentional non-warning state, see the dedicated tests below), not "ready".
    for name in ("openalex", "semanticscholar", "arxiv", "unpaywall"):
        assert result.details[name]["status"] == "ready"
    assert result.details["alphaxiv"]["status"] == "disabled"
    assert result.details["arxivxplorer"]["status"] == "disabled"


def test_providers_ready_is_auto_discoverable():
    """Same convention every other doctor check follows -- registered by
    module-level import side effect, no edits to trialerror/util/doctor.py."""
    from trialerror.util.doctor import clear_registry, discover_and_register_checks, registered_checks

    clear_registry()
    discover_and_register_checks()
    assert "litapi_providers_ready" in registered_checks()


# ---------------------------------------------------------------------------
# alphaxiv / arxivxplorer rows (v-allarxiv-search build)
# ---------------------------------------------------------------------------


def test_providers_ready_alphaxiv_disabled_by_default_does_not_warn(tmp_path):
    (tmp_path / "trialerror.toml").write_text('[program]\nid = "x"\n', encoding="utf-8")

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.details["alphaxiv"]["status"] == "disabled"
    assert "alphaxiv" not in result.message  # disabled is not counted into needs_setup/throttled


def test_providers_ready_alphaxiv_needs_key_when_enabled_without_key(tmp_path):
    (tmp_path / "trialerror.toml").write_text(
        '[program]\nid = "x"\n\n[litapi.alphaxiv]\nenabled = true\n', encoding="utf-8"
    )

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.details["alphaxiv"]["status"] == "needs-key"
    assert result.details["alphaxiv"]["signup_url"] == checks.ALPHAXIV_KEY_INFO_URL
    assert "alphaxiv" in result.message


def test_providers_ready_alphaxiv_ready_untested_live_when_enabled_with_key(tmp_path):
    key_path = tmp_path / "alphaxiv.key"
    key_path.write_text("secret-alphaxiv-key", encoding="utf-8")
    (tmp_path / "trialerror.toml").write_text(
        "[program]\nid = \"x\"\n\n"
        "[litapi.alphaxiv]\nenabled = true\n"
        f'api_key_path = "{key_path.name}"\n',
        encoding="utf-8",
    )

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.details["alphaxiv"]["status"] == "ready-untested-live"


def test_providers_ready_arxivxplorer_disabled_by_default(tmp_path):
    (tmp_path / "trialerror.toml").write_text('[program]\nid = "x"\n', encoding="utf-8")

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.details["arxivxplorer"]["status"] == "disabled"


def test_providers_ready_arxivxplorer_ready_untested_live_when_enabled(tmp_path):
    (tmp_path / "trialerror.toml").write_text(
        '[program]\nid = "x"\n\n[litapi.arxivxplorer]\nenabled = true\n', encoding="utf-8"
    )

    result = checks.check_litapi_providers_ready(DoctorContext(program_root=tmp_path))

    assert result.details["arxivxplorer"]["status"] == "ready-untested-live"
    # never counted into needs_setup/throttled -- enabling it alone must not force a warn
    # (the four REST providers above are still unconfigured in this fixture, so the
    # overall status is "warn" anyway -- assert arxivxplorer specifically isn't why).
    assert "arxivxplorer" not in result.message
