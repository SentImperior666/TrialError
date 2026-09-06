"""``[webfetch]`` — off by default, fail-closed in the sandbox.

Two things are being tested and only one of them is ordinary. The ordinary
one is that the knobs of design §5 parse and validate. The other is a
*negative space* test: the keys that must never exist. A config key that
could name a host, add a header or turn off the address policy would move a
security decision from a host-owned file into a file the research container
can write — which is exactly what ruling L-A2 refused. Those tests fail if
such a key is ever added, which is the only way a rule like that survives
contact with a future implementer in a hurry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trialerror.webfetch import config as config_module
from trialerror.webfetch.config import (
    DEFAULT_LICENSE_TIER,
    FORBIDDEN_KEYS,
    WebFetchConfig,
    WebFetchConfigError,
    WebFetchDisabledError,
    load_webfetch_config,
)

SANDBOX = {
    "enabled": True,
    "sandbox": True,
    "mode": "allowlist",
    "require_sidecar": True,
    "contact_mailto": "ops@example.org",
}


def load(**section):
    return load_webfetch_config({"webfetch": section})


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_a_program_with_no_webfetch_table_is_unchanged_and_off():
    """Most programs have no ``[webfetch]`` at all and must behave exactly as
    they did before this lane existed."""
    cfg = load_webfetch_config(None)
    assert cfg.enabled is False
    assert load_webfetch_config({}).enabled is False


def test_requiring_enabled_raises_a_distinct_error():
    """Not a config error — the config working. The CLI turns this into a
    next-action naming the line to add, not into a stack trace."""
    with pytest.raises(WebFetchDisabledError, match="disabled"):
        load_webfetch_config({}, require_enabled=True)
    with pytest.raises(WebFetchDisabledError):
        load(enabled=False).require_enabled()
    load(enabled=True).require_enabled()  # must not raise


def test_the_defaults_are_the_documented_ones():
    cfg = load(enabled=True)
    assert cfg.mode == "allowlist"
    assert cfg.require_sidecar is True
    assert cfg.honor_tdm_optout is False  # ruling L-A3
    assert cfg.default_license_tier == DEFAULT_LICENSE_TIER == "unknown"
    assert cfg.queue_dir == "/workspace/webfetch"
    assert cfg.wait_s == 45.0


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section,fragment",
    [
        ({"mode": "whatever"}, "mode"),
        ({"default_license_tier": "free"}, "default_license_tier"),
        ({"wait_s": 0}, "wait_s"),
        ({"wait_s": -1}, "wait_s"),
        ({"poll_interval_s": 90, "wait_s": 45}, "poll_interval_s"),
        ({"enabled": "yes"}, "true or false"),
        ({"wait_s": "soon"}, "must be a number"),
        ({"queue_dir": 3}, "must be a string"),
    ],
)
def test_a_wrong_value_stops_the_verb_that_read_it(section, fragment):
    with pytest.raises(WebFetchConfigError, match=fragment):
        load(**section)


def test_an_unknown_key_is_refused_rather_than_ignored():
    """A key this build does not understand is a setting the operator
    believes is in force and is not."""
    with pytest.raises(WebFetchConfigError, match="unknown key"):
        load(enabled=True, timeout_s=5)


def test_a_webfetch_table_that_is_not_a_table_is_refused():
    with pytest.raises(WebFetchConfigError, match="must be a table"):
        load_webfetch_config({"webfetch": ["enabled"]})


# ---------------------------------------------------------------------------
# the sandbox posture (fail-closed)
# ---------------------------------------------------------------------------


def test_the_sandbox_posture_loads_when_it_is_the_approved_one():
    cfg = load(**SANDBOX)
    assert cfg.sandbox is True
    assert cfg.mode == "allowlist"


def test_the_sandbox_refuses_a_denylist():
    """Ruling L-A2's rejected alternative: a denylist leaves a bounded
    exfiltration channel to any public host. Still available on a
    workstation, refused for the deployment."""
    with pytest.raises(WebFetchConfigError, match="allowlist"):
        load(**{**SANDBOX, "mode": "denylist"})
    load(enabled=True, mode="denylist")  # a workstation may


def test_the_sandbox_refuses_to_fetch_without_the_sidecar():
    """Fetching from inside the research container is the open-egress
    channel C-0076 removed."""
    with pytest.raises(WebFetchConfigError, match="require_sidecar"):
        load(**{**SANDBOX, "require_sidecar": False})


def test_the_sandbox_refuses_an_anonymous_user_agent():
    """C-0069: identify honestly. The User-Agent carries this address."""
    for value in (None, "", "   "):
        with pytest.raises(WebFetchConfigError, match="contact_mailto"):
            load(**{**SANDBOX, "contact_mailto": value})


# ---------------------------------------------------------------------------
# negative space: the keys that must never exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(FORBIDDEN_KEYS))
def test_every_forbidden_key_is_refused_loudly(key):
    with pytest.raises(WebFetchConfigError, match="refuses to read"):
        load(enabled=True, **{key: True})


def test_the_loader_has_no_loopback_or_host_knob_in_its_source():
    """Design §4, "Config misuse": grep test. ``NetGuard(allow_loopback=True)``
    is a constructor argument on a Python object, reachable from a test that
    imports it and from nowhere else. If it ever becomes readable from a
    file, this fails."""
    source = Path(config_module.__file__).read_text(encoding="utf-8")
    known = source.split("_KNOWN_KEYS", 1)[1].split(")", 1)[0]
    for forbidden in ("allow_loopback", "allow_private", "allowed_hosts", "proxy", "user_agent"):
        assert forbidden not in known, f"{forbidden} became a readable config key"


def test_the_known_key_set_and_the_dataclass_agree():
    """A field with no key is unreachable; a key with no field is a silent
    no-op. Either way the operator's config says something the build does
    not do."""
    fields = {f for f in WebFetchConfig.__dataclass_fields__}
    assert fields == set(config_module._KNOWN_KEYS)
    assert not (fields & FORBIDDEN_KEYS)


# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------


def test_an_absolute_queue_dir_is_honoured_and_a_relative_one_is_joined(tmp_path):
    absolute = load(enabled=True, queue_dir="/workspace/webfetch").queue_path(tmp_path)
    assert absolute.as_posix().endswith("/workspace/webfetch")

    relative = load(enabled=True, queue_dir="q").queue_path(tmp_path)
    assert relative == tmp_path / "q"


def test_the_raw_dir_is_inside_an_ingest_root_and_host_named(tmp_path):
    """``raw/web/<host>`` sits under the default ``raw`` ingest root by
    construction, so ``add_document``'s in-tree check passes without any
    caller widening ``[paths].ingest_roots``."""
    directory = load(enabled=True).raw_dir(tmp_path, "en.wikipedia.org")
    assert directory == tmp_path / "raw" / "web" / "en.wikipedia.org"


@pytest.mark.parametrize("host", ["../../etc", "..", ".", "a/b", "C:\\windows", "", "   "])
def test_a_hostile_host_cannot_escape_the_raw_directory(tmp_path, host):
    """``urlcheck`` has already refused anything that is not a DNS name by
    the time a host reaches here. Sanitizing again is cheap, and the cost of
    being wrong about "already validated" is a directory outside the corpus."""
    web = (tmp_path / "raw" / "web").resolve()
    directory = load(enabled=True).raw_dir(tmp_path, host).resolve()
    assert directory.parent == web
    assert directory != web
