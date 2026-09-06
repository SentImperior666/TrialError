"""The ``[webfetch]`` block of a program's ``trialerror.toml``.

Two jobs, and the second is the reason this is a module rather than three
``config.get(...)`` calls scattered through the handlers.

**Job one: one place that knows the defaults.** The knobs of design §5, with
their documented values, read once and passed around as a frozen object
rather than re-derived at each use — so "what is the wait?" has exactly one
answer no matter who asks.

**Job two: fail closed.** Web fetching is off by default (C-0069's gate), and
in the sandbox it is off *unless* the posture is the one the operator
approved: an exact-FQDN allowlist enforced by a separate container. A
mistyped ``mode`` or a quietly-dropped ``require_sidecar`` must not degrade
into "fetch anyway, with looser rules" — it must stop. Every rule in
:meth:`WebFetchConfig.validate` exists because the failure it prevents would
otherwise be silent and would widen an egress channel.

**What is deliberately NOT here.** There is no ``allow_loopback`` key, no
``allow_private``, no ``extra_headers``, no ``proxy``, no way to name a host
at all. The netguard's loopback seam is a constructor argument on a Python
object (``NetGuard(allow_loopback=True)``), reachable only from a test that
imports it — never from a file an agent inside the research container can
write. Hosts live in ``allowed-hosts.conf`` on the operator's host machine,
mounted read-only into the sidecar and absent from ``/workspace`` entirely
(ruling L-A2). A config key that could add one would hand the whole allowlist
decision back to the container the design assumes is compromised.
``tests/test_webfetch_config.py`` asserts the absence, so an innocent-looking
"just for local testing" knob cannot land here unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from trialerror.webfetch import WebFetchError

__all__ = [
    "CONFIG_SECTION",
    "DEFAULT_QUEUE_DIR",
    "DEFAULT_WAIT_S",
    "DEFAULT_LICENSE_TIER",
    "DEFAULT_MANIFEST_EXPIRES_AFTER_S",
    "MODES",
    "FORBIDDEN_KEYS",
    "WebFetchConfigError",
    "WebFetchDisabledError",
    "WebFetchConfig",
    "load_webfetch_config",
]

CONFIG_SECTION = "webfetch"

#: Inside the research container this is the shared bind mount (ruling
#: L-A1 = the file queue). On a workstation it is whatever the developer
#: passes to ``webfetch sidecar --queue``; the default only has to be right
#: for the deployment.
DEFAULT_QUEUE_DIR = "/workspace/webfetch"

#: Design §2.2 step 2: how long the ``web_fetch`` handler waits in-process
#: before parking the job. A typical fetch lands inside the first run.
DEFAULT_WAIT_S = 45.0

#: How often the handler checkpoints while waiting — frequently enough to
#: keep the lease alive, rarely enough not to hammer the ledger.
DEFAULT_POLL_INTERVAL_S = 1.0

#: Design §2.2 step 2 / §5: a manifest older than this means the sidecar is
#: not running, and the job becomes a VISIBLE logic failure rather than
#: waiting forever.
DEFAULT_MANIFEST_EXPIRES_AFTER_S = 86400.0

#: Design §4 T4: ``unknown`` is served unfenced under the internal-research
#: posture, and that is disclosed rather than hidden. The operator tags
#: commercial pages explicitly.
DEFAULT_LICENSE_TIER = "unknown"

MODES: frozenset[str] = frozenset({"allowlist", "denylist"})

#: Keys that must never appear in ``[webfetch]``. Not "ignored if present":
#: refused, loudly, at load. Each of these is a knob that would move a
#: security decision from the host into a file the research container can
#: write — see the module docstring.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "allow_loopback",
        "allow_private",
        "allow_private_ips",
        "allowed_hosts",
        "allow_hosts",
        "extra_headers",
        "headers",
        "proxy",
        "proxies",
        "user_agent",
        "disable_robots",
        "ignore_robots",
        "verify_tls",
    }
)

_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "enabled",
        "sandbox",
        "queue_dir",
        "mode",
        "require_sidecar",
        "default_license_tier",
        "contact_mailto",
        "honor_tdm_optout",
        "wait_s",
        "poll_interval_s",
        "manifest_expires_after_s",
        "raw_subdir",
    }
)

_LICENSE_TIERS: frozenset[str] = frozenset(
    {"open", "academic_oa", "user_owned_scan", "commercial_restricted", "unknown"}
)


class WebFetchConfigError(WebFetchError):
    """``[webfetch]`` says something this build refuses to act on.

    Always raised at LOAD, never at use: a bad config must stop the verb
    that read it, not surface three stages later as a fetch that behaved
    unexpectedly."""


class WebFetchDisabledError(WebFetchError):
    """Web fetching is off (C-0069: "disabled-by-default gate").

    Separate from :class:`WebFetchConfigError` because it is not an error in
    the config — it is the config working. The CLI turns this into a
    next-action that tells the operator which line to add."""


@dataclass(frozen=True)
class WebFetchConfig:
    """The resolved ``[webfetch]`` block."""

    enabled: bool = False
    sandbox: bool = False
    queue_dir: str = DEFAULT_QUEUE_DIR
    mode: str = "allowlist"
    require_sidecar: bool = True
    default_license_tier: str = DEFAULT_LICENSE_TIER
    contact_mailto: str | None = None
    honor_tdm_optout: bool = False
    wait_s: float = DEFAULT_WAIT_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    manifest_expires_after_s: float = DEFAULT_MANIFEST_EXPIRES_AFTER_S
    #: Where fetched bytes land under the program root. Inside an ingest
    #: root (``raw/``) by construction, so ``add_document``'s in-tree check
    #: passes without any caller widening ``[paths].ingest_roots``.
    raw_subdir: str = "raw/web"

    def require_enabled(self) -> None:
        if not self.enabled:
            raise WebFetchDisabledError(
                "web fetching is disabled: set [webfetch] enabled = true in trialerror.toml. "
                "It is off by default on purpose (C-0069) — turning it on is a decision about "
                "egress, not a formality."
            )

    def queue_path(self, program_root: Path | str) -> Path:
        """``queue_dir`` as a concrete path.

        An absolute value is honored as given (the deployment's
        ``/workspace/webfetch``); a relative one is joined onto the program
        root, which is what makes the local development mode of design §5
        work without an absolute path in a committed config."""
        candidate = Path(self.queue_dir)
        return candidate if candidate.is_absolute() else (Path(program_root) / candidate)

    def raw_dir(self, program_root: Path | str, host: str) -> Path:
        """``<program_root>/raw/web/<host>`` — one directory per host, so a
        human can see at a glance what has been fetched from where.

        The host reaching here has already been through ``urlcheck``, so it
        is a syntactically valid DNS name. It is sanitized again anyway,
        because this value becomes a path component and the cost of being
        wrong about "already validated" is a directory outside the corpus.
        Two rules: everything but ``[A-Za-z0-9._-]`` becomes an underscore
        (so no separator survives), and a name made only of dots — ``.`` and
        ``..``, the two that name a directory rather than sitting in one —
        is replaced outright.
        """
        safe_host = "".join(c if (c.isalnum() or c in "._-") else "_" for c in host)[:120]
        if not safe_host or not safe_host.strip("."):
            safe_host = "unknown-host"
        return Path(program_root) / self.raw_subdir / safe_host

    def validate(self) -> "WebFetchConfig":
        """Refuse anything this build will not act on. Returns ``self``."""
        if self.mode not in MODES:
            raise WebFetchConfigError(
                f"[webfetch] mode = {self.mode!r} is not one of {sorted(MODES)}"
            )
        if self.default_license_tier not in _LICENSE_TIERS:
            raise WebFetchConfigError(
                f"[webfetch] default_license_tier = {self.default_license_tier!r} is not one of "
                f"{sorted(_LICENSE_TIERS)} (source.license_tier's own CHECK constraint)"
            )
        for name, value in (
            ("wait_s", self.wait_s),
            ("poll_interval_s", self.poll_interval_s),
            ("manifest_expires_after_s", self.manifest_expires_after_s),
        ):
            if value <= 0:
                raise WebFetchConfigError(f"[webfetch] {name} must be a positive number")
        if self.poll_interval_s > self.wait_s:
            raise WebFetchConfigError(
                f"[webfetch] poll_interval_s ({self.poll_interval_s}) exceeds wait_s "
                f"({self.wait_s}), so the handler would never look at the queue before parking"
            )

        if self.sandbox:
            # The three sandbox rules, each fail-closed. None of them is a
            # preference: mode='denylist' leaves a bounded exfil channel to
            # any public host (ruling L-A2's rejected alternative);
            # require_sidecar=false would mean the research container fetches
            # for itself, which is the channel C-0076 removed; and an
            # unset contact_mailto means fetching without identifying
            # honestly, which C-0069 forbids.
            if self.mode != "allowlist":
                raise WebFetchConfigError(
                    "[webfetch] sandbox = true requires mode = \"allowlist\": a denylist leaves "
                    "a bounded exfiltration channel to any public host, which ruling L-A2 "
                    "refused for the deployment (it stays available on a workstation)"
                )
            if not self.require_sidecar:
                raise WebFetchConfigError(
                    "[webfetch] sandbox = true requires require_sidecar = true: fetching from "
                    "inside the research container is the open-egress channel C-0076 removed"
                )
            if not (self.contact_mailto or "").strip():
                raise WebFetchConfigError(
                    "[webfetch] sandbox = true requires contact_mailto: C-0069 says identify "
                    "honestly, and the User-Agent carries this address"
                )
        return self


def load_webfetch_config(
    config: Mapping[str, Any] | None, *, require_enabled: bool = False
) -> WebFetchConfig:
    """Read, validate and return the ``[webfetch]`` block.

    An ABSENT block is the disabled default, not an error — most programs
    have no ``[webfetch]`` table at all and must behave exactly as they did
    before this lane existed. A PRESENT but wrong one raises: the operator
    stated an intent and it could not be honored (the same fail-closed rule
    ``trialerror.ingest.handlers._load_config`` follows for the whole file).
    """
    section = (config or {}).get(CONFIG_SECTION)
    if section is None:
        resolved = WebFetchConfig()
        if require_enabled:
            resolved.require_enabled()
        return resolved
    if not isinstance(section, Mapping):
        raise WebFetchConfigError(f"[{CONFIG_SECTION}] must be a table, got {type(section).__name__}")

    forbidden = sorted(set(section) & FORBIDDEN_KEYS)
    if forbidden:
        raise WebFetchConfigError(
            f"[{CONFIG_SECTION}] carries key(s) {forbidden}, which this build refuses to read. "
            "Hosts, headers and address policy are decided on the host in "
            "webfetch/policy/, mounted read-only into the sidecar — a config key here would "
            "move that decision into a file the research container can write (ruling L-A2)."
        )
    unknown = sorted(set(section) - _KNOWN_KEYS)
    if unknown:
        raise WebFetchConfigError(
            f"[{CONFIG_SECTION}] has unknown key(s) {unknown}; known keys are "
            f"{sorted(_KNOWN_KEYS)}. A key this build does not understand is a setting the "
            "operator believes is in force and is not."
        )

    def _bool(key: str, default: bool) -> bool:
        value = section.get(key, default)
        if not isinstance(value, bool):
            raise WebFetchConfigError(f"[{CONFIG_SECTION}] {key} must be true or false")
        return value

    def _float(key: str, default: float) -> float:
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WebFetchConfigError(f"[{CONFIG_SECTION}] {key} must be a number")
        return float(value)

    def _str(key: str, default: str | None) -> str | None:
        value = section.get(key, default)
        if value is None:
            return None
        if not isinstance(value, str):
            raise WebFetchConfigError(f"[{CONFIG_SECTION}] {key} must be a string")
        return value

    resolved = WebFetchConfig(
        enabled=_bool("enabled", False),
        sandbox=_bool("sandbox", False),
        queue_dir=_str("queue_dir", DEFAULT_QUEUE_DIR) or DEFAULT_QUEUE_DIR,
        mode=_str("mode", "allowlist") or "allowlist",
        require_sidecar=_bool("require_sidecar", True),
        default_license_tier=_str("default_license_tier", DEFAULT_LICENSE_TIER)
        or DEFAULT_LICENSE_TIER,
        contact_mailto=_str("contact_mailto", None),
        honor_tdm_optout=_bool("honor_tdm_optout", False),
        wait_s=_float("wait_s", DEFAULT_WAIT_S),
        poll_interval_s=_float("poll_interval_s", DEFAULT_POLL_INTERVAL_S),
        manifest_expires_after_s=_float("manifest_expires_after_s", DEFAULT_MANIFEST_EXPIRES_AFTER_S),
        raw_subdir=_str("raw_subdir", "raw/web") or "raw/web",
    ).validate()
    if require_enabled:
        resolved.require_enabled()
    return resolved
