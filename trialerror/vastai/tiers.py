"""Tiers, config, and the throughput / TTL / cost model.

Every number here is labelled the way ``docs/VASTAI_EMBED_DESIGN.md``
section 3 labels it: **measured** on this laptop's DEV GPU, or an
**estimate**. None is an observed vast.ai price -- ``max_dph`` values are
ceilings the operator sets, not prices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEV_TOKENS_PER_S",
    "TOKENS_PER_BYTE",
    "ABSOLUTE_TTL_CAP_S",
    "DEFAULT_TIER",
    "DEFAULT_GPU_FACTORS",
    "DEFAULT_TIERS",
    "FORBIDDEN_KEYS",
    "Tier",
    "VastConfig",
    "VastConfigError",
    "PlanRefused",
    "Plan",
    "load_vast_config",
    "normalise_gpu_name",
    "estimate_tokens",
    "ttl_for",
    "rank_offers",
    "plan_run",
]

#: [measured] embeddings_local/results/qwen3-4b.json -- Qwen3-Embedding-4B,
#: bf16, batch 4, max_seq 1024, on the RTX 5080 Laptop GPU (DEV).
DEV_TOKENS_PER_S = 4291.2
#: [measured] embeddings_local/results/corpus_token_estimate.json.
TOKENS_PER_BYTE = 0.2863

#: The absolute TTL ceiling. Config may LOWER it (``ttl_cap_s``), never raise
#: it: a value above this is clamped, and the clamp is reported.
ABSOLUTE_TTL_CAP_S = 4 * 3600

DEFAULT_TIER = "mid"

#: [estimate] throughput relative to DEV (= 1.0), from relative dense
#: FP16/BF16 tensor throughput in vendor specs; +-50%. Recalibrate from the
#: measured ``tokens_per_s`` each ``vastai_run`` event records.
DEFAULT_GPU_FACTORS: dict[str, float] = {
    # low
    "RTX 4060 TI": 0.5,
    "RTX 5060 TI": 0.6,
    "RTX A4000": 0.6,
    "RTX 4000ADA": 0.6,
    # mid
    "RTX 3090": 0.9,
    "RTX 3090 TI": 1.0,
    "RTX A5000": 0.9,
    "RTX 4070S TI": 0.9,
    "RTX 4070 TI SUPER": 0.9,
    "RTX 4080": 1.1,
    "RTX 4080S": 1.2,
    "RTX 5070 TI": 1.1,
    "RTX 5080": 1.4,
    # high
    "RTX 4090": 1.8,
    "RTX 5090": 2.5,
    "L40S": 2.0,
    "A100 PCIE": 2.5,
    "A100 SXM4": 2.5,
    "H100 PCIE": 4.5,
    "H100 SXM": 6.0,
}

#: Keys whose presence is refused by name: design section 4.2 item 7 says
#: no keep-alive exists, and a config that asks for one must fail loudly
#: rather than be silently ignored.
FORBIDDEN_KEYS = ("keep_alive", "keepalive", "reuse_instance", "ttl_extend", "extend_ttl")


class VastConfigError(ValueError):
    pass


class PlanRefused(RuntimeError):
    """The run must not start (cost cap, TTL cap, no offer, ...)."""


def normalise_gpu_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).replace("_", " ")).strip().upper()


@dataclass(frozen=True)
class Tier:
    name: str
    gpus: tuple[str, ...]
    min_vram_gb: float
    max_dph: float
    min_reliability: float

    def admits(self, offer: dict[str, Any]) -> bool:
        if normalise_gpu_name(offer.get("gpu_name", "")) not in self.gpus:
            return False
        if float(offer.get("gpu_ram") or 0) / 1024.0 < self.min_vram_gb:  # vast.ai gpu_ram is MB
            return False
        if float(offer.get("dph_total") or 1e9) > self.max_dph:
            return False
        rel = offer.get("reliability", offer.get("reliability2", 0))
        return float(rel or 0) >= self.min_reliability


def _tier(name: str, gpus: list[str], vram: float, dph: float, rel: float) -> Tier:
    return Tier(name, tuple(normalise_gpu_name(g) for g in gpus), vram, dph, rel)


DEFAULT_TIERS: dict[str, Tier] = {
    "low": _tier("low", ["RTX 4060 Ti", "RTX 5060 Ti", "RTX A4000", "RTX 4000Ada"], 16, 0.25, 0.95),
    "mid": _tier(
        "mid",
        ["RTX 3090", "RTX 3090 Ti", "RTX A5000", "RTX 4070S Ti", "RTX 4070 Ti Super", "RTX 4080",
         "RTX 4080S", "RTX 5070 Ti", "RTX 5080"],
        16, 0.45, 0.97,
    ),
    "high": _tier(
        "high",
        ["RTX 4090", "RTX 5090", "L40S", "A100 PCIE", "A100 SXM4", "H100 PCIE", "H100 SXM"],
        24, 2.50, 0.98,
    ),
}


@dataclass
class VastConfig:
    program_root: Path
    api_key_path: Path | None
    ssh_identity_path: Path | None
    tier: str = DEFAULT_TIER
    tiers: dict[str, Tier] = field(default_factory=lambda: dict(DEFAULT_TIERS))
    gpu_factors: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GPU_FACTORS))
    max_job_usd: float = 3.00
    ttl_cap_s: float = ABSOLUTE_TTL_CAP_S
    startup_s: float = 1200.0  # [estimate] boot + image + pip + ~8 GB model download
    safety: float = 2.0
    grace_s: float = 300.0
    image: str = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
    disk_gb: int = 40
    pip_packages: tuple[str, ...] = ("sentence-transformers>=5.0", "transformers>=4.51")
    batch_size: int = 64
    max_jobs_per_run: int = 50
    poll_interval_s: float = 10.0
    notes: list[str] = field(default_factory=list)


def _path(program_root: Path, value: Any) -> Path | None:
    if not value:
        return None
    p = Path(str(value))
    return p if p.is_absolute() else program_root / p


def load_vast_config(raw: dict[str, Any], program_root: Path | str) -> VastConfig:
    """``raw`` = the whole parsed ``trialerror.toml``. Reads ``[vastai]``."""
    program_root = Path(program_root)
    table = dict(raw.get("vastai") or {})
    bad = [k for k in FORBIDDEN_KEYS if k in table]
    if bad:
        raise VastConfigError(
            f"[vastai] {', '.join(bad)}: no keep-alive / reuse / TTL extension exists by design -- an "
            "instance lives exactly as long as its embedding job (docs/VASTAI_EMBED_DESIGN.md 4.2)"
        )
    cfg = VastConfig(
        program_root=program_root,
        api_key_path=_path(program_root, table.get("api_key_path")),
        ssh_identity_path=_path(program_root, table.get("ssh_identity_path")),
    )
    tier = str(table.get("tier") or DEFAULT_TIER)
    tiers = dict(DEFAULT_TIERS)
    for name, t in (table.get("tiers") or {}).items():
        base = tiers.get(name)
        if base is None:
            raise VastConfigError(f"[vastai.tiers.{name}]: unknown tier (low / mid / high)")
        tiers[name] = _tier(
            name,
            list(t.get("gpus", base.gpus)),
            float(t.get("min_vram_gb", base.min_vram_gb)),
            float(t.get("max_dph", base.max_dph)),
            float(t.get("min_reliability", base.min_reliability)),
        )
    if tier not in tiers:
        raise VastConfigError(f"[vastai] tier = {tier!r}: must be one of low / mid / high")
    cfg.tier = tier
    cfg.tiers = tiers
    for gpu, f in (table.get("gpu_factors") or {}).items():
        cfg.gpu_factors[normalise_gpu_name(gpu)] = float(f)
    for key in ("max_job_usd", "startup_s", "safety", "grace_s", "poll_interval_s"):
        if key in table:
            setattr(cfg, key, float(table[key]))
    for key in ("disk_gb", "batch_size", "max_jobs_per_run"):
        if key in table:
            setattr(cfg, key, int(table[key]))
    if "image" in table:
        cfg.image = str(table["image"])
    if "pip_packages" in table:
        cfg.pip_packages = tuple(str(x) for x in table["pip_packages"])
    cap = float(table.get("ttl_cap_s", ABSOLUTE_TTL_CAP_S))
    if cap > ABSOLUTE_TTL_CAP_S:
        cfg.notes.append(f"ttl_cap_s {cap:.0f} clamped to the absolute cap {ABSOLUTE_TTL_CAP_S}")
        cap = ABSOLUTE_TTL_CAP_S
    cfg.ttl_cap_s = cap
    if cfg.max_job_usd <= 0:
        raise VastConfigError("[vastai] max_job_usd must be > 0")
    return cfg


def estimate_tokens(total_text_bytes: int) -> float:
    return total_text_bytes * TOKENS_PER_BYTE


def ttl_for(compute_s: float, cfg: VastConfig) -> tuple[float, float]:
    """``(ttl_s, uncapped_ttl_s)``: ``startup + safety * compute + grace``,
    capped at ``cfg.ttl_cap_s`` (itself <= :data:`ABSOLUTE_TTL_CAP_S`)."""
    uncapped = cfg.startup_s + cfg.safety * compute_s + cfg.grace_s
    return min(uncapped, cfg.ttl_cap_s, ABSOLUTE_TTL_CAP_S), uncapped


HOURS_PER_MONTH = 730.0


def effective_dph(offer: dict[str, Any], disk_gb: float | None) -> float:
    """What the instance bills per hour for THIS lease: the GPU price plus
    storage for the disk the lease rents. The search's ``dph_total`` prices a
    small default disk instead (first live run, 2026-09-18: offer listed at
    $0.143/h, instance billed $0.181/h with a 40 GB disk -- exactly
    ``dph_base + 40 * storage_cost / 730``). Falls back to ``dph_total`` when
    the offer lacks the fields."""
    base, storage = offer.get("dph_base"), offer.get("storage_cost")
    if disk_gb is None or base is None or storage is None:
        return float(offer.get("dph_total") or 0)
    return float(base) + float(disk_gb) * float(storage) / HOURS_PER_MONTH


def rank_offers(
    offers: list[dict[str, Any]], tier: Tier, factors: dict[str, float], *, disk_gb: float | None = None
) -> list[tuple[float, float, dict]]:
    """Offers the tier admits, best estimated tokens-per-dollar first:
    ``[(tokens_per_usd, est_tokens_s, offer), ...]``, priced at
    :func:`effective_dph` for ``disk_gb``. A GPU with no factor is skipped
    (an unknown card cannot be sized, so it cannot be capped)."""
    ranked = []
    for offer in offers:
        if int(offer.get("num_gpus") or 1) != 1 or not tier.admits(offer):
            continue
        factor = factors.get(normalise_gpu_name(offer.get("gpu_name", "")))
        dph = effective_dph(offer, disk_gb)
        if not factor or dph <= 0:
            continue
        tps = DEV_TOKENS_PER_S * factor
        ranked.append((tps * 3600.0 / dph, tps, offer))
    ranked.sort(key=lambda r: (-r[0], effective_dph(r[2], disk_gb)))
    return ranked


@dataclass
class Plan:
    tier: str
    offer: dict[str, Any]
    tokens: float
    est_tokens_s: float
    compute_s: float
    ttl_s: float
    dph: float
    worst_case_usd: float
    max_job_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "offer_id": self.offer.get("id"),
            "gpu_name": self.offer.get("gpu_name"),
            "dph_total": self.dph,
            "tokens_est": round(self.tokens),
            "est_tokens_s": round(self.est_tokens_s, 1),
            "compute_s_est": round(self.compute_s, 1),
            "ttl_s": round(self.ttl_s, 1),
            "worst_case_usd": round(self.worst_case_usd, 4),
            "max_job_usd": self.max_job_usd,
        }


def plan_run(
    offers: list[dict[str, Any]],
    total_text_bytes: int,
    cfg: VastConfig,
    *,
    max_job_usd: float | None = None,
) -> Plan:
    """Pick the offer and size the lease, or raise :class:`PlanRefused`.

    Refusals, in order: no admissible offer; the job would need a TTL
    beyond the cap; the WORST-CASE cost (``dph * TTL``, the most the lease
    can bill before the watchdog destroys it) exceeds the per-job cap."""
    tier = cfg.tiers[cfg.tier]
    cap_usd = min(cfg.max_job_usd, max_job_usd) if max_job_usd is not None else cfg.max_job_usd
    ranked = rank_offers(offers, tier, cfg.gpu_factors, disk_gb=cfg.disk_gb)
    if not ranked:
        raise PlanRefused(
            f"no vast.ai offer passes tier {tier.name!r} (GPUs {', '.join(tier.gpus)}; >= {tier.min_vram_gb} GB; "
            f"<= ${tier.max_dph}/h; reliability >= {tier.min_reliability})"
        )
    tokens = estimate_tokens(total_text_bytes)
    _score, tps, offer = ranked[0]
    compute_s = tokens / tps
    ttl, uncapped = ttl_for(compute_s, cfg)
    if uncapped > ttl:
        raise PlanRefused(
            f"this batch needs an estimated {uncapped:.0f} s of lease but the TTL cap is {ttl:.0f} s -- "
            "run fewer jobs (--max-jobs) rather than raising the cap"
        )
    dph = effective_dph(offer, cfg.disk_gb)  # what the instance will bill, disk included
    worst = dph * ttl / 3600.0
    plan = Plan(tier.name, offer, tokens, tps, compute_s, ttl, dph, worst, cap_usd)
    if worst > cap_usd:
        raise PlanRefused(
            f"worst-case cost ${worst:.2f} (${dph:.3f}/h x TTL {ttl:.0f} s) exceeds the per-job cap "
            f"${cap_usd:.2f} -- refused before any instance was created"
        )
    return plan
