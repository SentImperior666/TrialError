"""The vast.ai OCR executor's configuration: DEV's backend-config-root toml.

:func:`load_vast_config` is pure parsing and validation. It refuses by key
name (:class:`~trialerror.vastai.errors.VastConfigError`, with
``next_actions``) and never touches the filesystem: the key, the ssh identity,
the requirements lock and the model manifest are checked for EXISTENCE by the
separate :func:`check_runtime_files`, at worker start, and nothing here ever
opens the key or the identity.

With no ``[vastai]`` table and ``[ingest.ocr] executor`` absent or
``"local"``, loading succeeds and the result says vast.ai is off
(``cfg.enabled`` is false): today's DEV behaviour, byte for byte.

:data:`CONFIG_KEYS` lists every key with its default and a one-line meaning
(lane contract section 4); the docs test iterates it.

One ``[vastai]`` table serves two lanes. The embedding lane (the public
TrialError copy's embedding backend, ``trialerror vastai run``) reads it with
its own loader, :func:`trialerror.vastai.tiers.load_vast_config`; this loader
accepts every key of that lane with its public default and meaning
(:data:`EMBED_KEYS`, parsed into :class:`EmbedSettings`, which the OCR lane
never reads), so no key of the embedding lane is refused here. Values are
still validated: the shared keys as the OCR lane needs them (finite caps,
non-negative times), the embedding lane's own as that lane converts them,
plus a ``pip_packages`` that is a bare string.
The four keys the lanes share with different defaults (``image``,
``startup_s``, ``safety``, ``disk_gb``) take the public defaults in
``[vastai]``; the OCR lane reads its own under ``[vastai.ocr]``, and
``cfg.image``, ``cfg.startup_s``, ``cfg.safety`` and ``cfg.disk_gb`` are the
OCR lane's values. A ``ttl_cap_s`` above 4 h is clamped to 4 h with a note
(``cfg.notes``), as the embedding lane's loader clamps it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from trialerror.vastai.api import looks_like_a_key
from trialerror.vastai.errors import VastConfigError
from trialerror.vastai.tiers import (
    ABSOLUTE_TTL_CAP_S,
    DEFAULT_TIER,
    DEFAULT_TIERS,
    FORBIDDEN_KEYS,
    RENTAL_TYPE,
    Tier,
    make_tier,
    normalise_gpu_name,
)

__all__ = [
    "EXECUTOR_LOCAL",
    "EXECUTOR_VASTAI",
    "EXECUTORS",
    "LICENSE_TIERS",
    "NAMEABLE_LICENSE_TIERS",
    "REMOTE_SCRATCH_MODES",
    "WHEN_REFUSED_MODES",
    "MAX_APPROVAL_DAYS",
    "APPROVAL_FILENAME",
    "HIGH_TIER_APPROVAL_FILENAME",
    "DEFAULT_MARKER_VERSION",
    "PACKAGED_MODELS_MARKER_VERSION",
    "PACKAGED_MODELS_MANIFEST",
    "BYTES_PER_MB",
    "ConfigKey",
    "CONFIG_KEYS",
    "EMBED_KEYS",
    "OcrSettings",
    "EmbedSettings",
    "EgressPolicy",
    "VastConfig",
    "load_vast_config",
    "runtime_files",
    "check_runtime_files",
    "packaged_models_manifest",
]

EXECUTOR_LOCAL = "local"
EXECUTOR_VASTAI = "vastai"
EXECUTORS = (EXECUTOR_LOCAL, EXECUTOR_VASTAI)

#: ``source.license_tier``'s domain (the knowledge store's CHECK constraint).
LICENSE_TIERS = ("open", "academic_oa", "user_owned_scan", "commercial_restricted", "unknown")
#: What ``[vastai.egress] allow_license_tiers`` may name: ``unknown`` cannot be
#: named; such a document leaves only by its sha256.
NAMEABLE_LICENSE_TIERS = tuple(t for t in LICENSE_TIERS if t != "unknown")

REMOTE_SCRATCH_MODES = ("shm", "shm_or_disk")
WHEN_REFUSED_MODES = ("return", "local")
MAX_APPROVAL_DAYS = 7
APPROVAL_FILENAME = "vastai-ocr.approval"
HIGH_TIER_APPROVAL_FILENAME = "vastai-high-tier.approval"
DEFAULT_MARKER_VERSION = "1.10.2"
#: The marker version whose model manifest ships with the package.
PACKAGED_MODELS_MARKER_VERSION = "1.10.2"
#: Package-data path of that manifest, relative to ``trialerror/vastai/``
#: (``sha256sum`` format, paths relative to the model cache root; generated
#: from DEV's cache, C1).
PACKAGED_MODELS_MANIFEST = "remote/marker-models-1.10.2.sha256"
#: Sizes and rates in this package count a megabyte as 10**6 bytes.
BYTES_PER_MB = 1_000_000
#: The OCR lane's image: its torch is DEV's marker venv's torch (C1).
OCR_IMAGE = "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime"
#: The embedding lane's defaults, as its own loader has them.
EMBED_IMAGE = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
EMBED_PIP_PACKAGES = ("sentence-transformers>=5.0", "transformers>=4.51")

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# the key catalogue
# ---------------------------------------------------------------------------
class ConfigKey(NamedTuple):
    """One configuration key: ``[table] key``, its default (the Python value;
    ``None`` where there is none), the default as the docs print it, and a
    one-line meaning."""

    table: str
    key: str
    default: Any
    default_text: str
    meaning: str

    @property
    def qualified(self) -> str:
        return f"[{self.table}] {self.key}"


def _toml_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_text(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)


def _k(table: str, key: str, default: Any, meaning: str, text: str | None = None) -> ConfigKey:
    return ConfigKey(table, key, default, text if text is not None else _toml_text(default), meaning)


CONFIG_KEYS: tuple[ConfigKey, ...] = (
    _k("ingest.ocr", "executor", EXECUTOR_LOCAL,
       '"vastai" sends this worker\'s OCR to vast.ai; any other value than "local" or "vastai" is refused by name'),
    _k("vastai", "api_key_path", None,
       "the key file's path; its existence is checked at start, its contents are read only by the API client at call time",
       'required with executor = "vastai"'),
    _k("vastai", "ssh_identity_path", None,
       "the private half of the dedicated vast.ai key pair; only ssh opens it", "required"),
    _k("vastai", "approval_path", None, "where the operator-minted egress approval lives (design 9.3)",
       "<directory of api_key_path>/vastai-ocr.approval"),
    _k("vastai", "tier", DEFAULT_TIER, 'the GPU tier (low / mid / high); "high" still needs `trialerror vastai approve-high`'),
    _k("vastai", "type", RENTAL_TYPE,
       'the rental type; only on-demand exists: "bid" (interruptible) is refused by name, because a preempted lease strands the document'),
    _k("vastai", "max_job_usd", 3.0, "worst-case dollars one job may cost; finite and > 0", "3.00"),
    _k("vastai", "max_run_usd", 10.0, "worst-case dollars one worker run may cost; finite and > 0", "10.00"),
    _k("vastai", "max_approval_usd", 25.0,
       "the ceiling of an approval's envelope (its max_total_usd); finite and > 0", "25.00"),
    _k("vastai", "ttl_cap_s", ABSOLUTE_TTL_CAP_S,
       "the longest a lease may live; may be lowered, never raised: a value above 4 h is clamped to 4 h, with a note"),
    _k("vastai", "startup_s", 1200,
       "the embedding lane's: seconds the TTL reserves for boot, image, pip and the model download "
       "(the OCR lane reads [vastai.ocr] startup_s)"),
    _k("vastai", "safety", 2.0,
       "the embedding lane's: multiplier on the estimated compute time (the OCR lane reads [vastai.ocr] safety)"),
    _k("vastai", "grace_s", 300, "seconds added to every TTL"),
    _k("vastai", "image", EMBED_IMAGE,
       "the embedding lane's base image (the OCR lane reads [vastai.ocr] image)"),
    _k("vastai", "image_cuda", "13.0", "the image's CUDA version; hosts must offer cuda_max_good >= it"),
    _k("vastai", "disk_gb", 40,
       "the embedding lane's container disk per lease (GB) (the OCR lane reads [vastai.ocr] disk_gb)"),
    _k("vastai", "poll_interval_s", 10, "seconds between instance-status polls"),
    _k("vastai", "doctor_api_check", True, "whether the doctor lists the account's instances"),
    _k("vastai", "batch_size", 64, "the embedding lane's: chunks per embedding batch on the instance"),
    _k("vastai", "max_jobs_per_run", 50,
       "the embedding lane's: embed jobs one `trialerror vastai run` takes from the queue (--max-jobs may lower it)"),
    _k("vastai", "pip_packages", list(EMBED_PIP_PACKAGES),
       "the embedding lane's: the packages pip installs on the instance"),
    _k("vastai", "module_dir", None,
       "the embedding lane's: the directory of the embed_backend.py the instance runs, "
       "used when [ingest.embed.query] module_dir is not set", "unset"),
    _k("vastai.gpu_factors", "<GPU name>", None,
       "the embedding lane's throughput relative to DEV per card, over its built-in table",
       "the embedding lane's built-in table"),
    _k("vastai.tiers.<t>", "gpus", None, "the GPU names the tier admits", "the ported table"),
    _k("vastai.tiers.<t>", "min_vram_gb", None, "the tier's GPU memory floor (GB)", "the ported table"),
    _k("vastai.tiers.<t>", "max_dph", None, "the tier's effective hourly price ceiling ($/h, disk included)",
       "the ported table"),
    _k("vastai.tiers.<t>", "min_reliability", None, "the tier's host reliability floor", "the ported table"),
    _k("vastai.ocr", "requirements_lock", "marker-requirements.lock",
       "the hashed requirements lock (relative to the backend-config-root), written by `trialerror vastai lock-deps`"),
    _k("vastai.ocr", "models_manifest", "",
       '"" = the packaged manifest for marker 1.10.2; a path overrides; another marker_version needs one'),
    _k("vastai.ocr", "max_range_pixels", 2_000_000_000, "the pixel budget of one marker_single range"),
    _k("vastai.ocr", "max_range_pages", 128, "the page cap of one range, applied after the pixel budget"),
    _k("vastai.ocr", "fixed_mem_gb", 16, "the fixed RAM of one marker_single invocation (GB)"),
    _k("vastai.ocr", "mem_bytes_per_px", 9.4, "RAM per planned pixel of a range"),
    _k("vastai.ocr", "ram_headroom", 0.8, "the share of a host's RAM a range may use (0-1]"),
    _k("vastai.ocr", "dev_pages_per_min", 7.5, "DEV's measured OCR speed, the speed of a card with factor 1.0"),
    _k("vastai.ocr", "reload_s", 60, "seconds of model load per range"),
    _k("vastai.ocr", "uplink_mb_s", 1.0, "DEV's upload rate (MB/s) used to price the transfer"),
    _k("vastai.ocr", "bootstrap_download_gb", 13.0,
       "image + wheels + models, priced as download bandwidth in the worst case"),
    _k("vastai.ocr", "max_document_mb", 1024, "the largest document that may leave (MB)"),
    _k("vastai.ocr", "max_leases_per_job", 2, "leases one job may use, failovers included"),
    _k("vastai.ocr", "max_inet_cost_per_gb", 0.02, "the bandwidth price ceiling ($/GB, both directions)"),
    _k("vastai.ocr", "canary_min_similarity", 0.97, "the canary page's minimum text similarity"),
    _k("vastai.ocr", "range_timeout_s", 0,
       "per-range timeout; 0 = derived (2 x the expected range time + 300 s, never past the deadline)"),
    _k("vastai.ocr", "image", OCR_IMAGE, "the OCR lane's base image; its torch is DEV's marker venv's torch (C1)"),
    _k("vastai.ocr", "startup_s", 1500, "the OCR lane's: seconds the TTL reserves for boot, image, wheels and models"),
    _k("vastai.ocr", "safety", 1.5, "the OCR lane's: multiplier on the estimated compute time (>= 1)"),
    _k("vastai.ocr", "disk_gb", 32, "the OCR lane's container disk rented with each lease (GB)"),
    _k("vastai.ocr_gpu_factors", "<GPU name>", None, "OCR speed relative to DEV per card; every card not listed is 1.0",
       "empty (every card 1.0)"),
    _k("vastai.egress", "allow_license_tiers", [],
       "licence tiers whose documents may leave; unknown cannot be named"),
    _k("vastai.egress", "allow_documents", [], "input sha256s that may leave, whatever their tier"),
    _k("vastai.egress", "require_datacenter", True, "datacenter (Secure Cloud) hosts only"),
    _k("vastai.egress", "require_verified", True, "verified hosts only"),
    _k("vastai.egress", "allow_geolocations", [], "host countries allowed; empty = any"),
    _k("vastai.egress", "remote_scratch", "shm",
       '"shm" refuses a host whose /dev/shm is too small; "shm_or_disk" allows the container disk, except for shm_required_tiers'),
    _k("vastai.egress", "shm_required_tiers", ["commercial_restricted"],
       "tiers that must stay in RAM-backed /dev/shm on the host"),
    _k("vastai.egress", "require_approval", True,
       "whether an operator-minted, sealed approval is needed on top of the config switch"),
    _k("vastai.egress", "approval_max_days", MAX_APPROVAL_DAYS,
       "an approval's longest lifetime in days; may be lowered, never raised above 7"),
    _k("vastai.egress", "when_refused", "return",
       '"return" leaves a refused document pending; "local" runs it on DEV\'s own marker (needs [ingest.ocr] marker_single_exe)'),
)

#: The keys that belong to the embedding lane (the public TrialError copy's
#: embedding backend, read by :func:`trialerror.vastai.tiers.load_vast_config`).
#: This loader accepts them with their public defaults and meaning, into
#: ``cfg.embed``; the OCR lane never reads them.
EMBED_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("vastai", "image"),
    ("vastai", "startup_s"),
    ("vastai", "safety"),
    ("vastai", "disk_gb"),
    ("vastai", "batch_size"),
    ("vastai", "max_jobs_per_run"),
    ("vastai", "pip_packages"),
    ("vastai", "module_dir"),
    ("vastai.gpu_factors", "<GPU name>"),
})

_DEFAULTS: dict[tuple[str, str], Any] = {(k.table, k.key): k.default for k in CONFIG_KEYS}
_KNOWN: dict[str, set[str]] = {}
for _key in CONFIG_KEYS:
    _KNOWN.setdefault(_key.table, set()).add(_key.key)
_VASTAI_SUBTABLES = {"tiers", "ocr", "egress", "ocr_gpu_factors", "gpu_factors"}


# ---------------------------------------------------------------------------
# the parsed configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OcrSettings:
    requirements_lock: Path
    models_manifest: Path
    models_manifest_packaged: bool
    max_range_pixels: float
    max_range_pages: int
    fixed_mem_gb: float
    mem_bytes_per_px: float
    ram_headroom: float
    dev_pages_per_min: float
    reload_s: float
    uplink_mb_s: float
    bootstrap_download_gb: float
    max_document_mb: float
    max_leases_per_job: int
    max_inet_cost_per_gb: float
    canary_min_similarity: float
    range_timeout_s: float
    #: the four keys the OCR lane reads here rather than in ``[vastai]``
    #: (whose values are the embedding lane's)
    image: str
    startup_s: float
    safety: float
    disk_gb: int

    @property
    def max_document_bytes(self) -> int:
        return int(self.max_document_mb * BYTES_PER_MB)


@dataclass(frozen=True)
class EmbedSettings:
    """The embedding lane's ``[vastai]`` keys, with the public defaults and
    meaning. Parsed so that this loader accepts them; the embedding lane
    reads them with its own loader, and the OCR lane never reads them.
    ``gpu_factor_overrides`` holds only the ``[vastai.gpu_factors]`` entries
    (normalised names); the embedding lane lays them over its built-in
    table."""

    image: str = EMBED_IMAGE
    startup_s: float = 1200.0
    safety: float = 2.0
    disk_gb: int = 40
    batch_size: int = 64
    max_jobs_per_run: int = 50
    pip_packages: tuple[str, ...] = EMBED_PIP_PACKAGES
    module_dir: str | None = None
    gpu_factor_overrides: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class EgressPolicy:
    """``[vastai.egress]``, normalised: lists deduplicated and sorted, sha256s
    lower-case, countries upper-case. :meth:`canonical` is what an approval
    seals."""

    allow_license_tiers: tuple[str, ...] = ()
    allow_documents: tuple[str, ...] = ()
    require_datacenter: bool = True
    require_verified: bool = True
    allow_geolocations: tuple[str, ...] = ()
    remote_scratch: str = "shm"
    shm_required_tiers: tuple[str, ...] = ("commercial_restricted",)
    require_approval: bool = True
    approval_max_days: float = MAX_APPROVAL_DAYS
    when_refused: str = "return"

    def canonical(self) -> dict[str, Any]:
        return {
            "allow_documents": list(self.allow_documents),
            "allow_geolocations": list(self.allow_geolocations),
            "allow_license_tiers": list(self.allow_license_tiers),
            "approval_max_days": float(self.approval_max_days),
            "remote_scratch": self.remote_scratch,
            "require_approval": bool(self.require_approval),
            "require_datacenter": bool(self.require_datacenter),
            "require_verified": bool(self.require_verified),
            "shm_required_tiers": list(self.shm_required_tiers),
            "when_refused": self.when_refused,
        }

    def names_anything(self) -> bool:
        return bool(self.allow_license_tiers or self.allow_documents)


@dataclass(frozen=True)
class VastConfig:
    config_root: Path
    executor: str
    #: a ``[vastai]`` table exists, or ``executor = "vastai"``
    configured: bool
    api_key_path: Path | None
    ssh_identity_path: Path | None
    approval_path: Path | None
    #: where ``approve-high`` writes (``guard.approval_write_path``); readers use ``guard.approval_read_path``
    high_tier_approval_path: Path | None
    rental_type: str
    tier: str
    tiers: Mapping[str, Tier]
    max_job_usd: float
    max_run_usd: float
    max_approval_usd: float
    ttl_cap_s: float
    grace_s: float
    image_cuda: str
    poll_interval_s: float
    doctor_api_check: bool
    marker_version: str
    marker_single_exe: str | None
    ocr: OcrSettings
    gpu_factors: Mapping[str, float]
    egress: EgressPolicy
    #: the embedding lane's keys (never read by the OCR lane)
    embed: EmbedSettings = field(default_factory=EmbedSettings)
    #: what the loader adjusted rather than refused (a ttl_cap_s above 4 h)
    notes: tuple[str, ...] = ()

    # The OCR lane's image, startup, safety and disk: [vastai.ocr], because the
    # [vastai] keys of the same names are the embedding lane's.
    @property
    def image(self) -> str:
        return self.ocr.image

    @property
    def startup_s(self) -> float:
        return self.ocr.startup_s

    @property
    def safety(self) -> float:
        return self.ocr.safety

    @property
    def disk_gb(self) -> int:
        return self.ocr.disk_gb

    @property
    def enabled(self) -> bool:
        """``[ingest.ocr] executor = "vastai"``: this worker's OCR goes to vast.ai."""
        return self.executor == EXECUTOR_VASTAI

    @property
    def status(self) -> str:
        if self.enabled:
            text = (
                f"vast.ai is on: [ingest.ocr] executor = \"vastai\", tier {self.tier!r}, caps "
                f"${self.max_job_usd:.2f}/job, ${self.max_run_usd:.2f}/run, ${self.max_approval_usd:.2f}/approval"
            )
        elif self.configured:
            text = "vast.ai is off: a [vastai] table exists, but [ingest.ocr] executor is \"local\""
        else:
            text = "vast.ai is off: no [vastai] table and [ingest.ocr] executor is \"local\" (absent)"
        return text + "".join(f"; note: {note}" for note in self.notes)

    @property
    def active_tier(self) -> Tier:
        return self.tiers[self.tier]

    @property
    def image_cuda_version(self) -> float:
        return float(self.image_cuda)

    def gpu_factor(self, gpu_name: str) -> float:
        return float(self.gpu_factors.get(normalise_gpu_name(gpu_name), 1.0))


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
def _refuse(message: str, *next_actions: str) -> VastConfigError:
    return VastConfigError(message, next_actions=list(next_actions))


def _table(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _refuse(f"[{name}] must be a table, not {type(value).__name__}")
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(
    table: str,
    key: str,
    value: Any,
    *,
    minimum: float | None = None,
    exclusive_minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
    why: str = "",
) -> float:
    name = f"[{table}] {key}"
    if not _is_number(value):
        raise _refuse(f"{name} = {value!r}: must be a number")
    if not math.isfinite(float(value)):
        raise _refuse(f"{name} = {value!r}: must be finite{why}")
    if integer and float(value) != int(value):
        raise _refuse(f"{name} = {value!r}: must be a whole number")
    v = float(value)
    if exclusive_minimum is not None and v <= exclusive_minimum:
        raise _refuse(f"{name} = {value!r}: must be > {exclusive_minimum:g}{why}")
    if minimum is not None and v < minimum:
        raise _refuse(f"{name} = {value!r}: must be >= {minimum:g}{why}")
    if maximum is not None and v > maximum:
        raise _refuse(f"{name} = {value!r}: must be <= {maximum:g}{why}")
    return v


def _boolean(table: str, key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _refuse(f"[{table}] {key} = {value!r}: must be true or false")
    return value


def _string(table: str, key: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _refuse(f"[{table}] {key} = {value!r}: must be a {'' if allow_empty else 'non-empty '}string")
    return value


def _string_list(table: str, key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _refuse(f"[{table}] {key} must be a list of strings")
    return [v.strip() for v in value]


def _choice(table: str, key: str, value: Any, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise _refuse(f"[{table}] {key} = {value!r}: must be one of {', '.join(repr(c) for c in choices)}")
    return value


def _check_keys(table: str, values: Mapping[str, Any], known: set[str], *, subtables: set[str] = frozenset()) -> None:
    bad = [k for k in FORBIDDEN_KEYS if k in values]
    if bad:
        raise _refuse(
            f"[{table}] {', '.join(bad)}: no keep-alive, reuse or TTL extension exists by design -- an instance "
            "lives exactly as long as its OCR job (design section 10)",
            f"remove {', '.join(bad)} from [{table}]",
        )
    unknown = sorted(k for k in values if k not in known and k not in subtables)
    if unknown:
        raise _refuse(
            f"[{table}] {', '.join(unknown)}: not a vast.ai OCR setting (a misspelt key would silently keep its "
            f"default); known keys: {', '.join(sorted(known))}",
            f"remove or correct {', '.join(unknown)} in [{table}]",
        )


def _path(config_root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else config_root / p


def _key_path(table: str, key: str, value: Any, config_root: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _refuse(f"[{table}] {key} must be a non-empty path string")
    if looks_like_a_key(value):
        raise _refuse(
            f"[{table}] {key} looks like key material, not a path (value withheld) -- configure the PATH of the file",
            f"put the secret in a file and set [{table}] {key} to that file's path",
        )
    return _path(config_root, value)


def packaged_models_manifest() -> Path:
    """Where the packaged marker model manifest lives (package data)."""
    return Path(__file__).resolve().parent / PACKAGED_MODELS_MANIFEST


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def _parse_tiers(vast: Mapping[str, Any]) -> dict[str, Tier]:
    tiers = dict(DEFAULT_TIERS)
    raw_tiers = vast.get("tiers") or {}
    if not isinstance(raw_tiers, dict):
        raise _refuse("[vastai.tiers] must be a table of [vastai.tiers.<low|mid|high>] tables")
    for name, t in raw_tiers.items():
        base = tiers.get(name)
        if base is None:
            raise _refuse(f"[vastai.tiers.{name}]: unknown tier (low / mid / high)")
        if not isinstance(t, dict):
            raise _refuse(f"[vastai.tiers.{name}] must be a table")
        table = f"vastai.tiers.{name}"
        _check_keys(table, t, _KNOWN["vastai.tiers.<t>"])
        gpus = _string_list(table, "gpus", t.get("gpus", list(base.gpus)))
        if not gpus:
            raise _refuse(f"[{table}] gpus must name at least one GPU")
        tiers[name] = make_tier(
            name,
            gpus,
            _number(table, "min_vram_gb", t.get("min_vram_gb", base.min_vram_gb), exclusive_minimum=0),
            _number(table, "max_dph", t.get("max_dph", base.max_dph), exclusive_minimum=0,
                    why=" (a price ceiling; zero or negative is refused)"),
            _number(table, "min_reliability", t.get("min_reliability", base.min_reliability), minimum=0, maximum=1),
        )
    return tiers


def _parse_egress(egress_raw: Mapping[str, Any], *, marker_single_exe: str | None) -> EgressPolicy:
    t = "vastai.egress"
    _check_keys(t, egress_raw, _KNOWN[t])
    d = lambda key: egress_raw.get(key, _DEFAULTS[(t, key)])  # noqa: E731

    tiers: set[str] = set()
    for tier in _string_list(t, "allow_license_tiers", d("allow_license_tiers")):
        if tier == "unknown":
            raise _refuse(
                "[vastai.egress] allow_license_tiers names 'unknown': a document of unknown licence cannot be allowed "
                "by tier",
                "name that document's input sha256 in [vastai.egress] allow_documents instead",
            )
        if tier not in NAMEABLE_LICENSE_TIERS:
            raise _refuse(
                f"[vastai.egress] allow_license_tiers names {tier!r}: not a licence tier "
                f"({', '.join(NAMEABLE_LICENSE_TIERS)})"
            )
        tiers.add(tier)

    docs: set[str] = set()
    for index, sha in enumerate(_string_list(t, "allow_documents", d("allow_documents"))):
        if not _SHA256_RE.match(sha):
            raise _refuse(
                f"[vastai.egress] allow_documents[{index}] = {sha!r}: not a sha256 (64 hexadecimal characters)",
                "copy the input sha256 from the job's manifest (inputs[0].sha256)",
            )
        docs.add(sha.lower())

    geos: set[str] = set()
    for geo in _string_list(t, "allow_geolocations", d("allow_geolocations")):
        if not geo:
            raise _refuse("[vastai.egress] allow_geolocations contains an empty string")
        geos.add(geo.upper())

    shm_tiers: set[str] = set()
    for tier in _string_list(t, "shm_required_tiers", d("shm_required_tiers")):
        if tier not in LICENSE_TIERS:
            raise _refuse(
                f"[vastai.egress] shm_required_tiers names {tier!r}: not a licence tier ({', '.join(LICENSE_TIERS)})"
            )
        shm_tiers.add(tier)

    when_refused = _choice(t, "when_refused", d("when_refused"), WHEN_REFUSED_MODES)
    if when_refused == "local" and not (marker_single_exe or "").strip():
        raise _refuse(
            "[vastai.egress] when_refused = \"local\" runs refused documents on DEV's own marker, but "
            "[ingest.ocr] marker_single_exe is not set",
            "set [ingest.ocr] marker_single_exe, or set [vastai.egress] when_refused = \"return\"",
        )
    return EgressPolicy(
        allow_license_tiers=tuple(sorted(tiers)),
        allow_documents=tuple(sorted(docs)),
        require_datacenter=_boolean(t, "require_datacenter", d("require_datacenter")),
        require_verified=_boolean(t, "require_verified", d("require_verified")),
        allow_geolocations=tuple(sorted(geos)),
        remote_scratch=_choice(t, "remote_scratch", d("remote_scratch"), REMOTE_SCRATCH_MODES),
        shm_required_tiers=tuple(sorted(shm_tiers)),
        require_approval=_boolean(t, "require_approval", d("require_approval")),
        approval_max_days=_number(
            t, "approval_max_days", d("approval_max_days"), exclusive_minimum=0, maximum=MAX_APPROVAL_DAYS,
            why=" (an approval lives at most 7 days)",
        ),
        when_refused=when_refused,
    )


def _parse_ocr(ocr_raw: Mapping[str, Any], *, config_root: Path) -> OcrSettings:
    t = "vastai.ocr"
    _check_keys(t, ocr_raw, _KNOWN[t])
    d = lambda key: ocr_raw.get(key, _DEFAULTS[(t, key)])  # noqa: E731
    lock = _string(t, "requirements_lock", d("requirements_lock"))
    manifest = _string(t, "models_manifest", d("models_manifest"), allow_empty=True).strip()
    return OcrSettings(
        requirements_lock=_path(config_root, lock),
        models_manifest=_path(config_root, manifest) if manifest else packaged_models_manifest(),
        models_manifest_packaged=not manifest,
        max_range_pixels=_number(t, "max_range_pixels", d("max_range_pixels"), exclusive_minimum=0),
        max_range_pages=int(_number(t, "max_range_pages", d("max_range_pages"), minimum=1, integer=True)),
        fixed_mem_gb=_number(t, "fixed_mem_gb", d("fixed_mem_gb"), minimum=0),
        mem_bytes_per_px=_number(t, "mem_bytes_per_px", d("mem_bytes_per_px"), exclusive_minimum=0),
        ram_headroom=_number(t, "ram_headroom", d("ram_headroom"), exclusive_minimum=0, maximum=1),
        dev_pages_per_min=_number(t, "dev_pages_per_min", d("dev_pages_per_min"), exclusive_minimum=0),
        reload_s=_number(t, "reload_s", d("reload_s"), minimum=0),
        uplink_mb_s=_number(t, "uplink_mb_s", d("uplink_mb_s"), exclusive_minimum=0),
        bootstrap_download_gb=_number(t, "bootstrap_download_gb", d("bootstrap_download_gb"), minimum=0),
        max_document_mb=_number(t, "max_document_mb", d("max_document_mb"), exclusive_minimum=0,
                                why=" (a cap; zero or negative is refused)"),
        max_leases_per_job=int(_number(t, "max_leases_per_job", d("max_leases_per_job"), minimum=1, integer=True)),
        max_inet_cost_per_gb=_number(t, "max_inet_cost_per_gb", d("max_inet_cost_per_gb"), exclusive_minimum=0,
                                     why=" (a price ceiling; zero or negative is refused)"),
        canary_min_similarity=_number(t, "canary_min_similarity", d("canary_min_similarity"),
                                      exclusive_minimum=0, maximum=1),
        range_timeout_s=_number(t, "range_timeout_s", d("range_timeout_s"), minimum=0),
        image=_string(t, "image", d("image")),
        startup_s=_number(t, "startup_s", d("startup_s"), minimum=0),
        safety=_number(t, "safety", d("safety"), minimum=1, why=" (a TTL shorter than the estimate would expire every job)"),
        disk_gb=int(_number(t, "disk_gb", d("disk_gb"), minimum=1, integer=True)),
    )


def _as_embed(table: str, key: str, value: Any, convert: Any, what: str) -> Any:
    """An embedding-lane value, converted as that lane's own loader converts
    it (``float``/``int``): refused by name only where that conversion
    fails, so this loader never refuses a value the embedding lane takes."""
    try:
        return convert(value)
    except (TypeError, ValueError, OverflowError):
        raise _refuse(
            f"[{table}] {key} = {value!r}: must be {what} (the embedding lane reads it as one)",
            f"correct [{table}] {key}",
        ) from None


def _parse_embed(vast: Mapping[str, Any]) -> EmbedSettings:
    """The embedding lane's ``[vastai]`` keys, with the public defaults."""
    t = "vastai"
    d = lambda key: vast.get(key, _DEFAULTS[(t, key)])  # noqa: E731
    image = d("image")
    if not isinstance(image, str):
        raise _refuse(f"[vastai] image = {image!r}: must be a string (an image name)", "correct [vastai] image")
    packages = d("pip_packages")
    if not isinstance(packages, list):
        raise _refuse(
            f"[vastai] pip_packages = {packages!r}: must be a list of package specifiers (a bare string would be "
            "read one character at a time)",
            "write [vastai] pip_packages as a list, e.g. [\"sentence-transformers>=5.0\"]",
        )
    module_dir = vast.get("module_dir")
    if module_dir is not None and not isinstance(module_dir, str):
        raise _refuse(f"[vastai] module_dir = {module_dir!r}: must be a directory path string",
                      "correct [vastai] module_dir")
    factors_raw = vast.get("gpu_factors") or {}
    if not isinstance(factors_raw, dict):
        raise _refuse("[vastai.gpu_factors] must be a table of GPU name = factor",
                      "write [vastai.gpu_factors] as a table of GPU name = factor")
    return EmbedSettings(
        image=image,
        startup_s=_as_embed(t, "startup_s", d("startup_s"), float, "a number"),
        safety=_as_embed(t, "safety", d("safety"), float, "a number"),
        disk_gb=_as_embed(t, "disk_gb", d("disk_gb"), int, "a whole number"),
        batch_size=_as_embed(t, "batch_size", d("batch_size"), int, "a whole number"),
        max_jobs_per_run=_as_embed(t, "max_jobs_per_run", d("max_jobs_per_run"), int, "a whole number"),
        pip_packages=tuple(str(p) for p in packages),
        module_dir=module_dir,
        gpu_factor_overrides={
            normalise_gpu_name(str(name)): _as_embed("vastai.gpu_factors", str(name), value, float, "a number")
            for name, value in factors_raw.items()
        },
    )


def load_vast_config(toml: Mapping[str, Any], *, config_root: Path | str) -> VastConfig:
    """Parse and validate the vast.ai settings of DEV's toml (``toml`` = the
    whole parsed ``trialerror.toml`` of the backend-config-root).

    Raises :class:`~trialerror.vastai.errors.VastConfigError` naming the key.
    Pure: nothing is read from disk (see :func:`check_runtime_files`)."""
    config_root = Path(config_root)
    ingest = _table(toml, "ingest")
    ingest_ocr = ingest.get("ocr") or {}
    if not isinstance(ingest_ocr, dict):
        raise _refuse("[ingest.ocr] must be a table")
    executor = ingest_ocr.get("executor", EXECUTOR_LOCAL)
    if executor not in EXECUTORS:
        raise _refuse(
            f"[ingest.ocr] executor = {executor!r}: must be \"local\" (DEV's own marker) or \"vastai\"",
            "set [ingest.ocr] executor = \"local\" or \"vastai\"",
        )
    marker_version = str(ingest_ocr.get("marker_version") or DEFAULT_MARKER_VERSION)
    marker_single_exe = ingest_ocr.get("marker_single_exe")
    marker_single_exe = str(marker_single_exe) if marker_single_exe else None

    has_table = "vastai" in toml
    vast = _table(toml, "vastai")
    _check_keys("vastai", vast, _KNOWN["vastai"], subtables=_VASTAI_SUBTABLES)
    d = lambda key: vast.get(key, _DEFAULTS[("vastai", key)])  # noqa: E731
    t = "vastai"

    rental_type = vast.get("type", RENTAL_TYPE)
    if rental_type == "bid":
        raise _refuse(
            "[vastai] type = \"bid\": interruptible instances are refused -- a preempted lease strands the document "
            "on the host",
            "remove [vastai] type (on-demand is the only rental type)",
        )
    if rental_type != RENTAL_TYPE:
        raise _refuse(f"[vastai] type = {rental_type!r}: the only rental type is \"ondemand\"")

    tiers = _parse_tiers(vast)
    tier = d("tier")
    if tier not in tiers:
        raise _refuse(f"[vastai] tier = {tier!r}: must be one of low / mid / high")

    caps = {}
    for key in ("max_job_usd", "max_run_usd", "max_approval_usd"):
        caps[key] = _number(t, key, d(key), exclusive_minimum=0, why=" (every cap is finite; zero or negative is refused)")
    notes: list[str] = []
    ttl_raw = d("ttl_cap_s")
    if _is_number(ttl_raw) and float(ttl_raw) > ABSOLUTE_TTL_CAP_S:
        # Clamped, as the embedding lane's loader clamps it (same note text):
        # the TTL cap may be lowered, never raised above 4 h.
        notes.append(f"ttl_cap_s {float(ttl_raw):.0f} clamped to the absolute cap {ABSOLUTE_TTL_CAP_S}")
        ttl_raw = ABSOLUTE_TTL_CAP_S
    ttl_cap_s = _number(
        t, "ttl_cap_s", ttl_raw, exclusive_minimum=0, maximum=ABSOLUTE_TTL_CAP_S,
        why=f" (the TTL cap may be lowered, never raised above {ABSOLUTE_TTL_CAP_S} s = 4 h)",
    )
    image_cuda_raw = d("image_cuda")
    image_cuda = str(image_cuda_raw)
    try:
        if isinstance(image_cuda_raw, bool) or float(image_cuda) <= 0 or not math.isfinite(float(image_cuda)):
            raise ValueError
    except ValueError:
        raise _refuse(f"[vastai] image_cuda = {image_cuda_raw!r}: must be a CUDA version such as \"13.0\"") from None

    factors_raw = vast.get("ocr_gpu_factors") or {}
    if not isinstance(factors_raw, dict):
        raise _refuse("[vastai.ocr_gpu_factors] must be a table of GPU name = factor")
    gpu_factors = {
        normalise_gpu_name(name): _number("vastai.ocr_gpu_factors", name, value, exclusive_minimum=0)
        for name, value in factors_raw.items()
    }

    ocr_raw = vast.get("ocr") or {}
    egress_raw = vast.get("egress") or {}
    if not isinstance(ocr_raw, dict):
        raise _refuse("[vastai.ocr] must be a table")
    if not isinstance(egress_raw, dict):
        raise _refuse("[vastai.egress] must be a table")
    ocr = _parse_ocr(ocr_raw, config_root=config_root)
    egress = _parse_egress(egress_raw, marker_single_exe=marker_single_exe)
    embed = _parse_embed(vast)

    api_key_path = _key_path(t, "api_key_path", vast.get("api_key_path"), config_root)
    ssh_identity_path = _key_path(t, "ssh_identity_path", vast.get("ssh_identity_path"), config_root)
    approval_raw = vast.get("approval_path")
    if approval_raw is not None:
        approval_path = _path(config_root, _string(t, "approval_path", approval_raw))
    else:
        approval_path = api_key_path.parent / APPROVAL_FILENAME if api_key_path is not None else None
    from trialerror.vastai.guard import approval_write_path  # the one writer resolution (round 4, d)

    high_tier_path = approval_write_path(config_root, api_key_path)

    cfg = VastConfig(
        config_root=config_root,
        executor=executor,
        configured=has_table or executor == EXECUTOR_VASTAI,
        api_key_path=api_key_path,
        ssh_identity_path=ssh_identity_path,
        approval_path=approval_path,
        high_tier_approval_path=high_tier_path,
        rental_type=RENTAL_TYPE,
        tier=tier,
        tiers=tiers,
        max_job_usd=caps["max_job_usd"],
        max_run_usd=caps["max_run_usd"],
        max_approval_usd=caps["max_approval_usd"],
        ttl_cap_s=ttl_cap_s,
        grace_s=_number(t, "grace_s", d("grace_s"), minimum=0),
        image_cuda=image_cuda,
        poll_interval_s=_number(t, "poll_interval_s", d("poll_interval_s"), minimum=0),
        doctor_api_check=_boolean(t, "doctor_api_check", d("doctor_api_check")),
        marker_version=marker_version,
        marker_single_exe=marker_single_exe,
        ocr=ocr,
        gpu_factors=gpu_factors,
        egress=egress,
        embed=embed,
        notes=tuple(notes),
    )
    if cfg.enabled:
        if cfg.api_key_path is None:
            raise _refuse(
                "[vastai] api_key_path is required with [ingest.ocr] executor = \"vastai\"",
                "set [vastai] api_key_path to the PATH of the operator-placed key file",
            )
        if cfg.ssh_identity_path is None:
            raise _refuse(
                "[vastai] ssh_identity_path is required with [ingest.ocr] executor = \"vastai\"",
                "set [vastai] ssh_identity_path to the private half of the dedicated vast.ai key pair",
            )
        if cfg.ocr.models_manifest_packaged and cfg.marker_version != PACKAGED_MODELS_MARKER_VERSION:
            raise _refuse(
                f"[ingest.ocr] marker_version = {cfg.marker_version!r} has no packaged model manifest (the package "
                f"ships one for marker {PACKAGED_MODELS_MARKER_VERSION})",
                "set [vastai.ocr] models_manifest to a manifest generated from DEV's own model cache",
            )
    return cfg


def runtime_files(cfg: VastConfig) -> dict[str, dict[str, Any]]:
    """The files a vast.ai worker needs, with whether each EXISTS. Stat
    only: nothing is opened, the key and the identity least of all."""
    entries = {
        "[vastai] api_key_path": cfg.api_key_path,
        "[vastai] ssh_identity_path": cfg.ssh_identity_path,
        "[vastai.ocr] requirements_lock": cfg.ocr.requirements_lock,
        "[vastai.ocr] models_manifest": cfg.ocr.models_manifest,
    }
    return {
        name: {"path": str(path) if path is not None else None, "exists": bool(path is not None and path.is_file())}
        for name, path in entries.items()
    }


def check_runtime_files(cfg: VastConfig) -> dict[str, Path]:
    """Worker start: every runtime file must exist. Raises
    :class:`VastConfigError` naming each missing key and path (never a
    content). Returns ``{key name: path}``."""
    report = runtime_files(cfg)
    missing = [(name, entry["path"]) for name, entry in report.items() if not entry["exists"]]
    if missing:
        hints = {
            "[vastai.ocr] requirements_lock": "run `trialerror vastai lock-deps` to write the hashed requirements lock",
            "[vastai.ocr] models_manifest": "set [vastai.ocr] models_manifest to a manifest generated from DEV's "
            "model cache",
        }
        actions = [hints.get(name, f"place the file, or correct {name}") for name, _p in missing]
        raise VastConfigError(
            "vast.ai worker cannot start: missing "
            + "; ".join(f"{name} ({p})" + (f" -- {hints[name]}" if name in hints else "") for name, p in missing),
            next_actions=actions,
        )
    return {name: Path(entry["path"]) for name, entry in report.items()}
