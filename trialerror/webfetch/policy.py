"""Host policy and caps — the operator's half of the fetch decision.

Design §3.3 and ruling **L-A2**: the set of fetchable hosts is an exact-FQDN
allowlist that lives on the *host* machine, is mounted read-only into the
sidecar, and is absent from the workspace the agent can write. Agents may
*propose* a host; a human approves it. That is what closes the
URL-as-exfiltration-channel (design §4 T2): an injected agent cannot name a
receiver, so the widest channel it has left is the choice of page among hosts
the operator already trusts.

Three files, all read fresh on every job (the "no restart to change policy"
pattern the deployment's domain list already uses):

``allowed-hosts.conf``
    One exact FQDN per line, optional space-separated flags. **No
    wildcards** — ``*.example.com`` would let an attacker publish a page on
    any subdomain of a trusted provider and use it as a drop box, which is
    the whole thing the allowlist exists to prevent. A wildcard line is a
    load error, not a warning.

``policy.toml``
    Caps and two knobs. Unknown keys are a load error: this file is the
    fail-closed surface (design §4, "Config misuse"), and a cap silently
    ignored because of a typo is a cap that is not enforced.

``robots-overrides.conf``
    ``<url_norm> <ruling-id>`` — the only way a manifest's
    ``robots_override_ruling`` is honored. The precedent is the disclosure
    this project already made for one named web client: the operator decides,
    per named application, in writing.

Flags on an allowlist line:

``http``
    plain ``http://`` is acceptable for this host (default: https only).
``keep-query``
    agent-origin URLs may keep their query string for this host (default:
    stripped; operator-origin URLs always keep it).
``git``
    this host may be cloned rather than fetched.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from trialerror.util.atomic import atomic_write_text
from trialerror.util.timeutil import now_dt
from trialerror.webfetch import WebFetchError, WebFetchRefused
from trialerror.webfetch.urlcheck import peek_host

__all__ = [
    "PolicyError",
    "KNOWN_FLAGS",
    "Caps",
    "HostRule",
    "Policy",
    "Counters",
    "HostPacer",
    "parse_size",
    "ALLOWED_HOSTS_FILENAME",
    "POLICY_FILENAME",
    "ROBOTS_OVERRIDES_FILENAME",
]

ALLOWED_HOSTS_FILENAME = "allowed-hosts.conf"
POLICY_FILENAME = "policy.toml"
ROBOTS_OVERRIDES_FILENAME = "robots-overrides.conf"

KNOWN_FLAGS: frozenset[str] = frozenset({"http", "keep-query", "git"})

_SIZE_RE = re.compile(r"^\s*(\d+)\s*(B|KiB|MiB|GiB)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
_RULING_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class PolicyError(WebFetchError):
    """A policy file is missing, unreadable, or structurally invalid.

    Never downgraded to a warning: an unreadable policy means the sidecar
    does not know what it is allowed to do, and the only safe answer to that
    is to refuse everything.
    """


def parse_size(value: object, *, what: str) -> int:
    """Accept ``5242880`` or ``"5MiB"`` and return bytes.

    The design writes caps as ``5MiB``/``2GiB``; TOML has no size type, so
    both spellings are accepted and the human-readable one is preferred in
    the shipped template.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a size
        raise PolicyError(f"{what}: expected a size, got a boolean")
    if isinstance(value, int):
        if value < 0:
            raise PolicyError(f"{what}: size must not be negative")
        return value
    if isinstance(value, str):
        m = _SIZE_RE.match(value)
        if m:
            unit = (m.group(2) or "B").lower()
            return int(m.group(1)) * _SIZE_UNITS[unit]
    raise PolicyError(f"{what}: cannot read {value!r} as a size (try 5MiB, 200MiB, 12345)")


@dataclass(frozen=True)
class Caps:
    """Every numeric limit the fetcher enforces. Defaults are design §3.3."""

    max_url_len: int = 2048
    max_query_len: int = 512
    max_html_bytes: int = 5 * 1024**2
    max_pdf_bytes: int = 64 * 1024**2
    max_git_archive_bytes: int = 200 * 1024**2
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    total_timeout_s: float = 90.0
    git_timeout_s: float = 300.0
    max_redirects: int = 5
    min_host_interval_s: float = 3.0
    crawl_delay_cap_s: float = 30.0
    per_host_daily: int = 50
    global_daily: int = 500
    agent_daily: int = 100
    daily_bytes: int = 2 * 1024**3
    queue_disk_cap: int = 10 * 1024**3
    robots_ttl_s: float = 86400.0
    manifest_expires_after_s: float = 86400.0
    decompress_ratio_cap: float = 20.0

    #: Which cap applies to a fetched body, by content class.
    def max_bytes_for(self, content_class: str) -> int:
        if content_class == "pdf":
            return self.max_pdf_bytes
        if content_class == "git":
            return self.max_git_archive_bytes
        return self.max_html_bytes


_SIZE_FIELDS = frozenset(
    {
        "max_html_bytes",
        "max_pdf_bytes",
        "max_git_archive_bytes",
        "daily_bytes",
        "queue_disk_cap",
    }
)
_FLOAT_FIELDS = frozenset(
    {
        "connect_timeout_s",
        "read_timeout_s",
        "total_timeout_s",
        "git_timeout_s",
        "min_host_interval_s",
        "crawl_delay_cap_s",
        "robots_ttl_s",
        "manifest_expires_after_s",
        "decompress_ratio_cap",
    }
)
_INT_FIELDS = frozenset(
    {
        "max_url_len",
        "max_query_len",
        "max_redirects",
        "per_host_daily",
        "global_daily",
        "agent_daily",
    }
)
#: Top-level keys of ``policy.toml`` that are not caps.
_NON_CAP_KEYS = frozenset({"mode", "contact_mailto", "honor_tdm_optout"})


@dataclass(frozen=True)
class HostRule:
    """One approved host and its flags, with the line that approved it.

    ``source`` ("allowed-hosts.conf:12") is copied into every ``result.json``
    so a provenance record answers "who said this host was allowed, and
    where do I go to change my mind" without a second lookup.
    """

    host: str
    flags: frozenset[str] = frozenset()
    source: str = ""

    @property
    def allow_http(self) -> bool:
        return "http" in self.flags

    @property
    def keep_query(self) -> bool:
        return "keep-query" in self.flags

    @property
    def allow_git(self) -> bool:
        return "git" in self.flags


@dataclass(frozen=True)
class Policy:
    """A snapshot of the three policy files, loaded fresh per job."""

    mode: str = "allowlist"
    caps: Caps = field(default_factory=Caps)
    hosts: Mapping[str, HostRule] = field(default_factory=dict)
    robots_overrides: Mapping[str, str] = field(default_factory=dict)
    contact_mailto: str = ""
    honor_tdm_optout: bool = False
    policy_dir: Path | None = None

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, policy_dir: str | Path, *, require_allowlist: bool = False) -> "Policy":
        """Read the three files from ``policy_dir``.

        ``require_allowlist=True`` is the sandbox's fail-closed setting: a
        policy that asks for ``mode = "denylist"`` is *refused*, not
        downgraded. Denylist mode exists for development on a workstation,
        where the operator is watching; it must never be reachable by
        editing a file the deployment mounted.
        """
        directory = Path(policy_dir)
        if not directory.is_dir():
            raise PolicyError(f"policy directory not found: {directory}")

        caps_kwargs, mode, contact_mailto, honor_tdm_optout = cls._load_toml(
            directory / POLICY_FILENAME
        )
        if mode not in ("allowlist", "denylist"):
            raise PolicyError(f"{POLICY_FILENAME}: mode must be 'allowlist' or 'denylist'")
        if require_allowlist and mode != "allowlist":
            raise PolicyError(
                f"{POLICY_FILENAME}: mode={mode!r} refused — this deployment requires "
                "mode='allowlist' (fail-closed; see the design's §4 'Config misuse' row)"
            )
        caps = Caps(**caps_kwargs)
        hosts = cls._load_hosts(directory / ALLOWED_HOSTS_FILENAME)
        overrides = cls._load_overrides(directory / ROBOTS_OVERRIDES_FILENAME)
        return cls(
            mode=mode,
            caps=caps,
            hosts=hosts,
            robots_overrides=overrides,
            contact_mailto=contact_mailto,
            honor_tdm_optout=honor_tdm_optout,
            policy_dir=directory,
        )

    @staticmethod
    def _load_toml(path: Path) -> tuple[dict, str, str, bool]:
        if not path.is_file():
            raise PolicyError(f"policy file not found: {path}")
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise PolicyError(f"invalid TOML in {path}: {exc}") from exc
        except OSError as exc:
            raise PolicyError(f"could not read {path}: {exc}") from exc

        known = _SIZE_FIELDS | _FLOAT_FIELDS | _INT_FIELDS | _NON_CAP_KEYS
        unknown = sorted(set(raw) - known)
        if unknown:
            raise PolicyError(
                f"{path.name}: unknown key(s) {unknown} — this loader is strict on purpose; "
                f"known keys are {sorted(known)}"
            )

        caps_kwargs: dict = {}
        for key in _SIZE_FIELDS:
            if key in raw:
                caps_kwargs[key] = parse_size(raw[key], what=f"{path.name}:{key}")
        for key in _INT_FIELDS:
            if key in raw:
                value = raw[key]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise PolicyError(f"{path.name}:{key} must be a non-negative integer")
                caps_kwargs[key] = value
        for key in _FLOAT_FIELDS:
            if key in raw:
                value = raw[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    raise PolicyError(f"{path.name}:{key} must be a non-negative number")
                caps_kwargs[key] = float(value)

        mode = raw.get("mode", "allowlist")
        if not isinstance(mode, str):
            raise PolicyError(f"{path.name}:mode must be a string")
        contact = raw.get("contact_mailto", "")
        if not isinstance(contact, str):
            raise PolicyError(f"{path.name}:contact_mailto must be a string")
        honor = raw.get("honor_tdm_optout", False)
        if not isinstance(honor, bool):
            raise PolicyError(f"{path.name}:honor_tdm_optout must be a boolean")
        return caps_kwargs, mode, contact, honor

    @staticmethod
    def _load_hosts(path: Path) -> dict[str, HostRule]:
        if not path.is_file():
            raise PolicyError(f"allowlist not found: {path}")
        rules: dict[str, HostRule] = {}
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.split("#", 1)[0].strip()
            if not stripped:
                continue
            parts = stripped.split()
            host_token, flag_tokens = parts[0], parts[1:]
            if "*" in host_token:
                raise PolicyError(
                    f"{path.name}:{lineno}: wildcard host {host_token!r} is refused — the "
                    "allowlist holds exact FQDNs only (a wildcard would let an attacker "
                    "publish a receiving page under a trusted provider)"
                )
            try:
                host = peek_host(f"https://{host_token}/")
            except WebFetchRefused as exc:
                raise PolicyError(
                    f"{path.name}:{lineno}: {host_token!r} is not a usable host ({exc.reason})"
                ) from exc
            unknown_flags = sorted(set(flag_tokens) - KNOWN_FLAGS)
            if unknown_flags:
                raise PolicyError(
                    f"{path.name}:{lineno}: unknown flag(s) {unknown_flags}; "
                    f"known flags are {sorted(KNOWN_FLAGS)}"
                )
            if host in rules:
                raise PolicyError(f"{path.name}:{lineno}: duplicate host {host!r}")
            rules[host] = HostRule(
                host=host, flags=frozenset(flag_tokens), source=f"{path.name}:{lineno}"
            )
        return rules

    @staticmethod
    def _load_overrides(path: Path) -> dict[str, str]:
        if not path.is_file():
            return {}
        overrides: dict[str, str] = {}
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.split("#", 1)[0].strip()
            if not stripped:
                continue
            parts = stripped.split()
            if len(parts) != 2:
                raise PolicyError(
                    f"{path.name}:{lineno}: expected '<url_norm> <ruling-id>', got {stripped!r}"
                )
            url_norm, ruling = parts
            if not _RULING_RE.match(ruling):
                raise PolicyError(f"{path.name}:{lineno}: ruling id {ruling!r} has an odd shape")
            overrides[url_norm] = ruling
        return overrides

    # -- queries ---------------------------------------------------------
    def rule_for(self, host: str) -> HostRule:
        """Return the approved rule for ``host`` or refuse.

        In ``denylist`` mode (development only) any syntactically valid host
        is accepted with no flags — the design records this as a
        development-mode affordance with a disclosed, bounded residual, and
        the sandbox refuses to load a policy that selects it.
        """
        rule = self.hosts.get(host)
        if rule is not None:
            return rule
        if self.mode == "denylist":
            return HostRule(host=host, flags=frozenset(), source="policy.toml:mode=denylist")
        raise WebFetchRefused(
            "host_not_allowed",
            f"{host} is not on the allowlist; propose it and have a human approve it",
            host=host,
        )

    def robots_ruling_for(self, url_norm: str) -> str | None:
        return self.robots_overrides.get(url_norm)

    def with_caps(self, **overrides: object) -> "Policy":
        """Test/tuning seam: a copy with some caps replaced."""
        return replace(self, caps=replace(self.caps, **overrides))  # type: ignore[arg-type]


class HostPacer:
    """``min_host_interval_s`` between two requests to the same host, plus a
    per-host ``Crawl-delay`` when robots.txt asked for one.

    Deliberately in-memory: the design's P4 says the sidecar keeps only a
    robots cache and daily counters, so a restart re-earns the interval
    rather than replaying persisted timers. The sidecar is single-threaded,
    so this is the only pacing gate there is.

    ``_time_fn``/``_sleep_fn`` are the same test seams the existing rate
    limiter in this codebase uses; production callers never pass them.
    """

    def __init__(
        self,
        min_interval_s: float,
        *,
        _time_fn: Callable[[], float] | None = None,
        _sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval_s = float(min_interval_s)
        self._time_fn = _time_fn or time.monotonic
        self._sleep_fn = _sleep_fn or time.sleep
        self._last: dict[str, float] = {}
        self._extra: dict[str, float] = {}

    def set_crawl_delay(self, host: str, delay_s: float) -> None:
        self._extra[host] = max(0.0, float(delay_s))

    def interval_for(self, host: str) -> float:
        return max(self.min_interval_s, self._extra.get(host, 0.0))

    def wait(self, host: str) -> float:
        """Sleep as long as politeness requires; return the seconds slept."""
        interval = self.interval_for(host)
        now = self._time_fn()
        slept = 0.0
        last = self._last.get(host)
        if interval > 0 and last is not None:
            remaining = interval - (now - last)
            if remaining > 0:
                self._sleep_fn(remaining)
                slept = remaining
                now = self._time_fn()
        self._last[host] = now
        return slept


class Counters:
    """Daily request and byte counters, persisted in the sidecar's state dir.

    One UTC calendar day, four caps (design §3.3): per host, global, per
    agent-origin request, and total bytes. A counter file that cannot be
    read is treated as "today, all zero" — the alternative (refusing every
    fetch because a JSON file got truncated) turns a cosmetic failure into
    an outage, and the caps are a politeness/exfiltration bound rather than
    a safety-critical interlock. The reset is recorded in the audit trail by
    the sidecar's own log line.
    """

    def __init__(
        self,
        state_dir: str | Path,
        caps: Caps,
        *,
        _day_fn: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(state_dir) / "counters.json"
        self.caps = caps
        self._day_fn = _day_fn or (lambda: now_dt().strftime("%Y-%m-%d"))
        self._data = self._read()

    def _read(self) -> dict:
        fresh = {"day": self._day_fn(), "global": 0, "agent": 0, "bytes": 0, "hosts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fresh
        if not isinstance(data, dict) or data.get("day") != fresh["day"]:
            return fresh
        for key, default in (("global", 0), ("agent", 0), ("bytes", 0)):
            if not isinstance(data.get(key), int):
                data[key] = default
        if not isinstance(data.get("hosts"), dict):
            data["hosts"] = {}
        return data

    def _roll(self) -> None:
        today = self._day_fn()
        if self._data.get("day") != today:
            self._data = {"day": today, "global": 0, "agent": 0, "bytes": 0, "hosts": {}}

    def _flush(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, sort_keys=True) + "\n")

    def snapshot(self) -> dict:
        self._roll()
        return json.loads(json.dumps(self._data))

    def check(self, host: str, origin: str) -> None:
        """Refuse *before* any network work when a cap is already reached."""
        self._roll()
        if self._data["global"] >= self.caps.global_daily:
            raise WebFetchRefused(
                "daily_cap", f"global daily cap {self.caps.global_daily} reached", host=host
            )
        if self._data["hosts"].get(host, 0) >= self.caps.per_host_daily:
            raise WebFetchRefused(
                "host_cap", f"per-host daily cap {self.caps.per_host_daily} reached", host=host
            )
        if origin == "agent" and self._data["agent"] >= self.caps.agent_daily:
            raise WebFetchRefused(
                "agent_cap", f"agent daily cap {self.caps.agent_daily} reached", host=host
            )
        if self._data["bytes"] >= self.caps.daily_bytes:
            raise WebFetchRefused(
                "daily_cap", f"daily byte cap {self.caps.daily_bytes} reached", host=host
            )

    def record(self, host: str, origin: str, *, bytes_in: int = 0, bytes_out: int = 0) -> None:
        """Count one *attempt* — refused attempts count too.

        An attempt that was refused after a socket was opened still cost the
        remote host something and still moved bytes out of this network, so
        it consumes cap the same way a success does.
        """
        self._roll()
        self._data["global"] += 1
        self._data["hosts"][host] = self._data["hosts"].get(host, 0) + 1
        if origin == "agent":
            self._data["agent"] += 1
        self._data["bytes"] += max(0, int(bytes_in)) + max(0, int(bytes_out))
        self._flush()
