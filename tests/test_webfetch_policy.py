"""Host policy, caps, pacing and counters — the operator's half of a fetch.

The design's ruling L-A2 puts the set of fetchable hosts on the *host*
machine, read-only in the sidecar and absent from the workspace: an injected
agent inside the research container can propose a host but cannot name a
receiver. These tests hold the loader to the two properties that ruling rests
on — it is **exact** (no wildcards, ever) and it is **strict** (a file it
cannot fully understand is an error, never a partially applied policy).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch import policy as policy_module
from trialerror.webfetch.policy import (
    ALLOWED_HOSTS_FILENAME,
    KNOWN_FLAGS,
    POLICY_FILENAME,
    ROBOTS_OVERRIDES_FILENAME,
    Caps,
    Counters,
    HostPacer,
    Policy,
    PolicyError,
    parse_size,
)


def write_policy_dir(
    root: Path,
    *,
    hosts: str = "example.com\n",
    toml: str = "",
    overrides: str | None = None,
) -> Path:
    directory = root / "policy"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ALLOWED_HOSTS_FILENAME).write_text(hosts, encoding="utf-8")
    (directory / POLICY_FILENAME).write_text(toml, encoding="utf-8")
    if overrides is not None:
        (directory / ROBOTS_OVERRIDES_FILENAME).write_text(overrides, encoding="utf-8")
    return directory


# --------------------------------------------------------------------------
# the allowlist
# --------------------------------------------------------------------------


def test_hosts_flags_and_comments_load(tmp_path: Path) -> None:
    directory = write_policy_dir(
        tmp_path,
        hosts=(
            "# the seven wave_0 hosts, approved by a human on the host machine\n"
            "diataxis.fr\n"
            "\n"
            "github.com git\n"
            "docs.internal.example http keep-query   # trailing comment\n"
        ),
    )
    loaded = Policy.load(directory)
    assert set(loaded.hosts) == {"diataxis.fr", "github.com", "docs.internal.example"}
    assert loaded.rule_for("github.com").allow_git is True
    assert loaded.rule_for("github.com").allow_http is False
    docs = loaded.rule_for("docs.internal.example")
    assert (docs.allow_http, docs.keep_query, docs.allow_git) == (True, True, False)


def test_the_line_that_approved_a_host_is_recorded(tmp_path: Path) -> None:
    """``host_rule`` goes into every ``result.json``: a provenance record has
    to answer "who allowed this, and where do I go to change my mind"."""
    directory = write_policy_dir(tmp_path, hosts="a.example\nb.example\n")
    assert Policy.load(directory).rule_for("b.example").source == f"{ALLOWED_HOSTS_FILENAME}:2"


def test_a_wildcard_line_is_an_error_not_a_warning(tmp_path: Path) -> None:
    """A wildcard would let an attacker publish a receiving page under a
    trusted provider — the exact channel the allowlist exists to close."""
    directory = write_policy_dir(tmp_path, hosts="*.substack.com\n")
    with pytest.raises(PolicyError, match="wildcard"):
        Policy.load(directory)


def test_an_unknown_flag_is_an_error(tmp_path: Path) -> None:
    directory = write_policy_dir(tmp_path, hosts="example.com post\n")
    with pytest.raises(PolicyError, match="unknown flag"):
        Policy.load(directory)


def test_known_flags_are_exactly_the_three_the_design_names() -> None:
    assert KNOWN_FLAGS == {"http", "keep-query", "git"}


def test_a_duplicate_host_is_an_error(tmp_path: Path) -> None:
    directory = write_policy_dir(tmp_path, hosts="example.com\nexample.com git\n")
    with pytest.raises(PolicyError, match="duplicate"):
        Policy.load(directory)


def test_an_ip_literal_cannot_be_approved(tmp_path: Path) -> None:
    """The allowlist holds names. An address on a line here would be a
    literal the URL layer has already refused by shape."""
    directory = write_policy_dir(tmp_path, hosts="169.254.169.254\n")
    with pytest.raises(PolicyError, match="ip_literal"):
        Policy.load(directory)


def test_a_reserved_suffix_cannot_be_approved(tmp_path: Path) -> None:
    directory = write_policy_dir(tmp_path, hosts="nas.fritz.box\n")
    with pytest.raises(PolicyError, match="host_reserved_suffix"):
        Policy.load(directory)


def test_a_missing_allowlist_is_an_error(tmp_path: Path) -> None:
    directory = tmp_path / "policy"
    directory.mkdir()
    (directory / POLICY_FILENAME).write_text("", encoding="utf-8")
    with pytest.raises(PolicyError, match="allowlist not found"):
        Policy.load(directory)


def test_a_missing_policy_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="policy directory not found"):
        Policy.load(tmp_path / "nope")


def test_an_unapproved_host_is_refused_with_the_named_reason(tmp_path: Path) -> None:
    loaded = Policy.load(write_policy_dir(tmp_path))
    with pytest.raises(WebFetchRefused) as caught:
        loaded.rule_for("evil.example")
    assert caught.value.reason == "host_not_allowed"
    assert caught.value.context["host"] == "evil.example"


def test_a_subdomain_of_an_approved_host_is_not_approved(tmp_path: Path) -> None:
    """Exact FQDN means exact: approving ``example.com`` must not approve
    ``anything.example.com``."""
    loaded = Policy.load(write_policy_dir(tmp_path, hosts="example.com\n"))
    with pytest.raises(WebFetchRefused) as caught:
        loaded.rule_for("drop.example.com")
    assert caught.value.reason == "host_not_allowed"


# --------------------------------------------------------------------------
# policy.toml
# --------------------------------------------------------------------------


def test_defaults_match_the_design(tmp_path: Path) -> None:
    caps = Policy.load(write_policy_dir(tmp_path)).caps
    assert caps.max_url_len == 2048
    assert caps.max_query_len == 512
    assert caps.max_html_bytes == 5 * 1024**2
    assert caps.max_pdf_bytes == 64 * 1024**2
    assert caps.max_git_archive_bytes == 200 * 1024**2
    assert caps.max_redirects == 5
    assert caps.min_host_interval_s == 3.0
    assert caps.crawl_delay_cap_s == 30.0
    assert (caps.per_host_daily, caps.global_daily, caps.agent_daily) == (50, 500, 100)
    assert caps.decompress_ratio_cap == 20.0
    assert caps.manifest_expires_after_s == 86400.0


def test_sizes_accept_both_spellings(tmp_path: Path) -> None:
    directory = write_policy_dir(
        tmp_path, toml='max_html_bytes = "2MiB"\nmax_pdf_bytes = 1048576\n'
    )
    caps = Policy.load(directory).caps
    assert caps.max_html_bytes == 2 * 1024**2
    assert caps.max_pdf_bytes == 1024**2


@pytest.mark.parametrize("value", ["5 MB", "banana", -1, True, None, 1.5])
def test_unreadable_sizes_are_refused(value: object) -> None:
    with pytest.raises(PolicyError):
        parse_size(value, what="test")


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    """The strict loader is the fail-closed surface: a cap silently ignored
    because of a typo is a cap that is not enforced."""
    directory = write_policy_dir(tmp_path, toml='max_htm_bytes = "2MiB"\n')
    with pytest.raises(PolicyError, match="unknown key"):
        Policy.load(directory)


def test_allow_loopback_cannot_be_set_from_a_file(tmp_path: Path) -> None:
    """The one seam that could turn the address policy off is a constructor
    argument in Python, never a configuration key — so a file that names it
    is rejected as an unknown key rather than honoured."""
    directory = write_policy_dir(tmp_path, toml="allow_loopback = true\n")
    with pytest.raises(PolicyError, match="unknown key"):
        Policy.load(directory)


def test_the_policy_loader_never_mentions_allow_loopback() -> None:
    """Design §4, "Config misuse": the grep is the test. If this string ever
    appears in the loader, some path exists from a mounted file to the
    address policy."""
    source = inspect.getsource(policy_module)
    assert "allow_loopback" not in source


@pytest.mark.parametrize(
    "toml,message",
    [
        ("max_redirects = -1", "non-negative integer"),
        ('max_redirects = "five"', "non-negative integer"),
        ("read_timeout_s = -2.0", "non-negative number"),
        ("mode = 3", "must be a string"),
        ('mode = "anything"', "must be 'allowlist' or 'denylist'"),
        ("contact_mailto = 5", "must be a string"),
        ('honor_tdm_optout = "yes"', "must be a boolean"),
        ("not valid toml at all [[", "invalid TOML"),
    ],
)
def test_malformed_values_are_refused(tmp_path: Path, toml: str, message: str) -> None:
    directory = write_policy_dir(tmp_path, toml=toml + "\n")
    with pytest.raises(PolicyError, match=message):
        Policy.load(directory)


def test_the_two_knobs_load(tmp_path: Path) -> None:
    directory = write_policy_dir(
        tmp_path,
        toml='contact_mailto = "ops@example.com"\nhonor_tdm_optout = true\n',
    )
    loaded = Policy.load(directory)
    assert loaded.contact_mailto == "ops@example.com"
    assert loaded.honor_tdm_optout is True


def test_the_contact_address_is_never_hard_coded() -> None:
    """Ruling L-A3: the address is set at deploy. An empty default is the
    honest one — a wrong address in a User-Agent is worse than none."""
    assert Policy().contact_mailto == ""
    assert Policy().honor_tdm_optout is False


# --------------------------------------------------------------------------
# fail-closed mode
# --------------------------------------------------------------------------


def test_denylist_mode_is_refused_when_the_deployment_requires_an_allowlist(
    tmp_path: Path,
) -> None:
    directory = write_policy_dir(tmp_path, toml='mode = "denylist"\n')
    with pytest.raises(PolicyError, match="fail-closed"):
        Policy.load(directory, require_allowlist=True)


def test_denylist_mode_accepts_any_syntactically_valid_host_on_a_workstation(
    tmp_path: Path,
) -> None:
    directory = write_policy_dir(tmp_path, toml='mode = "denylist"\n')
    loaded = Policy.load(directory, require_allowlist=False)
    rule = loaded.rule_for("some.host.example")
    assert rule.flags == frozenset()
    assert "denylist" in rule.source


# --------------------------------------------------------------------------
# robots overrides
# --------------------------------------------------------------------------


def test_overrides_are_optional(tmp_path: Path) -> None:
    assert Policy.load(write_policy_dir(tmp_path)).robots_overrides == {}


def test_overrides_map_a_url_to_a_ruling(tmp_path: Path) -> None:
    directory = write_policy_dir(
        tmp_path,
        overrides="# operator ruling, in writing\nhttps://example.com/a C-0069\n",
    )
    loaded = Policy.load(directory)
    assert loaded.robots_ruling_for("https://example.com/a") == "C-0069"
    assert loaded.robots_ruling_for("https://example.com/b") is None


@pytest.mark.parametrize("line", ["https://example.com/a\n", "a b c\n", "https://x/a rul ing\n"])
def test_a_malformed_override_line_is_an_error(tmp_path: Path, line: str) -> None:
    directory = write_policy_dir(tmp_path, overrides=line)
    with pytest.raises(PolicyError):
        Policy.load(directory)


# --------------------------------------------------------------------------
# caps
# --------------------------------------------------------------------------


def test_the_byte_cap_depends_on_the_content_class() -> None:
    caps = Caps()
    assert caps.max_bytes_for("html") == caps.max_html_bytes
    assert caps.max_bytes_for("text") == caps.max_html_bytes
    assert caps.max_bytes_for("pdf") == caps.max_pdf_bytes
    assert caps.max_bytes_for("git") == caps.max_git_archive_bytes


def test_with_caps_returns_a_copy(tmp_path: Path) -> None:
    loaded = Policy.load(write_policy_dir(tmp_path))
    tuned = loaded.with_caps(max_html_bytes=17)
    assert tuned.caps.max_html_bytes == 17
    assert loaded.caps.max_html_bytes == 5 * 1024**2
    assert tuned.hosts is loaded.hosts


# --------------------------------------------------------------------------
# pacing
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_the_first_request_to_a_host_never_waits() -> None:
    clock = FakeClock()
    pacer = HostPacer(3.0, _time_fn=clock.time, _sleep_fn=clock.sleep)
    assert pacer.wait("example.com") == 0.0
    assert clock.slept == []


def test_a_second_request_waits_out_the_interval() -> None:
    clock = FakeClock()
    pacer = HostPacer(3.0, _time_fn=clock.time, _sleep_fn=clock.sleep)
    pacer.wait("example.com")
    clock.now += 1.0
    assert pacer.wait("example.com") == pytest.approx(2.0)
    assert clock.slept == [pytest.approx(2.0)]


def test_pacing_is_per_host() -> None:
    clock = FakeClock()
    pacer = HostPacer(3.0, _time_fn=clock.time, _sleep_fn=clock.sleep)
    pacer.wait("a.example")
    assert pacer.wait("b.example") == 0.0


def test_a_crawl_delay_raises_the_interval_but_never_lowers_it() -> None:
    clock = FakeClock()
    pacer = HostPacer(3.0, _time_fn=clock.time, _sleep_fn=clock.sleep)
    pacer.set_crawl_delay("slow.example", 10.0)
    pacer.set_crawl_delay("fast.example", 0.5)
    assert pacer.interval_for("slow.example") == 10.0
    assert pacer.interval_for("fast.example") == 3.0


def test_enough_elapsed_time_means_no_wait() -> None:
    clock = FakeClock()
    pacer = HostPacer(3.0, _time_fn=clock.time, _sleep_fn=clock.sleep)
    pacer.wait("example.com")
    clock.now += 99.0
    assert pacer.wait("example.com") == 0.0


# --------------------------------------------------------------------------
# counters
# --------------------------------------------------------------------------


class FakeDay:
    def __init__(self, value: str = "2026-09-05") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


def test_counters_persist_across_instances(tmp_path: Path) -> None:
    day = FakeDay()
    caps = Caps()
    first = Counters(tmp_path, caps, _day_fn=day)
    first.record("example.com", "operator_list", bytes_in=10, bytes_out=5)
    second = Counters(tmp_path, caps, _day_fn=day)
    snapshot = second.snapshot()
    assert snapshot["global"] == 1
    assert snapshot["hosts"]["example.com"] == 1
    assert snapshot["bytes"] == 15


def test_counters_reset_on_a_new_day(tmp_path: Path) -> None:
    day = FakeDay("2026-09-05")
    counters = Counters(tmp_path, Caps(), _day_fn=day)
    counters.record("example.com", "agent")
    day.value = "2026-09-06"
    assert counters.snapshot()["global"] == 0
    counters.check("example.com", "agent")


def test_the_global_daily_cap_refuses_before_any_network_work(tmp_path: Path) -> None:
    caps = Caps(global_daily=2)
    counters = Counters(tmp_path, caps, _day_fn=FakeDay())
    counters.record("a.example", "operator_list")
    counters.record("b.example", "operator_list")
    with pytest.raises(WebFetchRefused) as caught:
        counters.check("c.example", "operator_list")
    assert caught.value.reason == "daily_cap"


def test_the_per_host_cap_is_per_host(tmp_path: Path) -> None:
    counters = Counters(tmp_path, Caps(per_host_daily=1), _day_fn=FakeDay())
    counters.record("a.example", "operator_list")
    with pytest.raises(WebFetchRefused) as caught:
        counters.check("a.example", "operator_list")
    assert caught.value.reason == "host_cap"
    counters.check("b.example", "operator_list")


def test_the_agent_cap_applies_only_to_agent_origin(tmp_path: Path) -> None:
    counters = Counters(tmp_path, Caps(agent_daily=1), _day_fn=FakeDay())
    counters.record("a.example", "agent")
    with pytest.raises(WebFetchRefused) as caught:
        counters.check("a.example", "agent")
    assert caught.value.reason == "agent_cap"
    counters.check("a.example", "operator_list")


def test_the_byte_cap_counts_both_directions(tmp_path: Path) -> None:
    counters = Counters(tmp_path, Caps(daily_bytes=100), _day_fn=FakeDay())
    counters.record("a.example", "operator_list", bytes_in=60, bytes_out=60)
    with pytest.raises(WebFetchRefused) as caught:
        counters.check("a.example", "operator_list")
    assert caught.value.reason == "daily_cap"


def test_a_refused_attempt_still_consumes_cap(tmp_path: Path) -> None:
    """An attempt that reached the network cost the remote host something and
    moved bytes out of this network, whether or not it produced a page."""
    counters = Counters(tmp_path, Caps(), _day_fn=FakeDay())
    counters.record("a.example", "agent", bytes_in=0, bytes_out=412)
    snapshot = counters.snapshot()
    assert snapshot["global"] == 1 and snapshot["agent"] == 1 and snapshot["bytes"] == 412


def test_a_corrupt_counter_file_is_treated_as_a_fresh_day(tmp_path: Path) -> None:
    """The caps bound politeness and exfiltration; they are not a
    safety-critical interlock. Refusing every fetch because a JSON file got
    truncated would turn a cosmetic failure into an outage."""
    (tmp_path / "counters.json").write_text("{not json", encoding="utf-8")
    counters = Counters(tmp_path, Caps(), _day_fn=FakeDay())
    assert counters.snapshot()["global"] == 0


def test_the_counter_file_is_json_on_disk(tmp_path: Path) -> None:
    counters = Counters(tmp_path, Caps(), _day_fn=FakeDay("2026-09-05"))
    counters.record("a.example", "operator_list")
    written = json.loads((tmp_path / "counters.json").read_text(encoding="utf-8"))
    assert written["day"] == "2026-09-05"
    assert written["hosts"] == {"a.example": 1}
