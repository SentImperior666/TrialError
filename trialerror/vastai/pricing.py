"""The OCR cost model (design sections 6.2-6.3 and 10): range plan, RAM
floor, the offer search and its client-side re-check, TTL, worst case, and
the spend caps -- all decided BEFORE anything is created.

Memory (6.2)::

    need_gb(range)  = fixed_mem_gb + mem_bytes_per_px * pixels_in_range / 1e9
    min_cpu_ram_gb  = need_gb(largest planned range) / ram_headroom

``pixels_in_range`` is the range's page count times the largest page's raster
at the planning DPI (the planner's own arithmetic, without its safety factor).

Time and money (6.3)::

    compute_s  = pages * 60 / (dev_pages_per_min * factor) + n_ranges * reload_s
    transfer_s = document_bytes / (uplink_mb_s * 1e6)
    ttl_s      = min(ttl_cap_s, startup_s + transfer_s + safety * compute_s + grace_s)
    worst_usd  = effective_dph * ttl_s / 3600
               + (bootstrap_download_gb + document_gb) * inet_down_cost
               + output_gb * inet_up_cost

Caps, each against the worst case (10):

* job      -- ``worst <= min([vastai] max_job_usd, the approval's max_job_usd)``
* run      -- ``spent in this worker run + worst <= [vastai] max_run_usd``
* approval -- ``spent under the approval's nonce + worst <= min(its max_total_usd, [vastai] max_approval_usd)``
* TTL      -- the uncapped TTL must fit ``ttl_cap_s``
* credit   -- ``account credit >= worst``, when the API exposes the credit

Spent = settled ledger estimates plus the worst case of every unsettled
lease (:func:`trialerror.vastai.ledger.spend_view`).

**The server's filtering is not trusted.** :func:`offer_query` sends every
host requirement to the search, and :func:`offer_refusals` re-checks every
returned offer against the egress host requirements, the RAM floor and the
price ceilings; a non-matching offer is dropped. A search that ignored a
filter can therefore never route a document to a non-datacenter or
unverified host.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from trialerror.ingest.backends import DEFAULT_BOUNDED_DPI, PageRange, page_boxes, plan_page_ranges
from trialerror.vastai.api import read_api_key
from trialerror.vastai.config import BYTES_PER_MB, VastConfig
from trialerror.vastai.errors import (
    VastApiError,
    VastKeyMissing,
    VastPlanRefused,
    VastSpendRefused,
)
from trialerror.vastai.guard import HighTierRefused, verify_high_tier_approval
from trialerror.vastai.ledger import Ledger, SpendView, spend_view
from trialerror.vastai.tiers import effective_dph

__all__ = [
    "BYTES_PER_GB",
    "OUTPUT_BYTES_PER_PAGE",
    "OFFER_SEARCH_LIMIT",
    "DATACENTER_FIELDS",
    "HOSTING_TYPE_DATACENTER",
    "OFFER_DEBUG_FIELDS",
    "datacenter_signal",
    "offer_debug_rows",
    "DocumentPlan",
    "OfferEstimate",
    "PricedJob",
    "need_gb",
    "plan_document",
    "offer_query",
    "offer_refusals",
    "estimate_offer",
    "rank_estimates",
    "derived_range_timeout_s",
    "job_cap_usd",
    "approval_cap_usd",
    "check_envelopes",
    "spend_from_ledger",
    "price_job",
]

BYTES_PER_GB = 1_000_000_000
#: [estimate] markdown per page fetched back from the host (marker writes a
#: few KB of text per book page; 8 KB is on the generous side).
OUTPUT_BYTES_PER_PAGE = 8_000
OFFER_SEARCH_LIMIT = 64
_POINTS_PER_INCH = 72.0


def need_gb(pixels_in_range: float, cfg: VastConfig) -> float:
    return cfg.ocr.fixed_mem_gb + cfg.ocr.mem_bytes_per_px * float(pixels_in_range) / 1e9


# ---------------------------------------------------------------------------
# the document
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DocumentPlan:
    pages: int
    ranges: tuple[PageRange, ...]
    dpi: float
    max_page_px: float
    largest_range_pages: int
    largest_range_px: float
    need_gb: float
    min_cpu_ram_gb: float
    max_range_pixels: float
    max_range_pages: int

    @property
    def n_ranges(self) -> int:
        return len(self.ranges)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "ranges": [r.flag_value for r in self.ranges],
            "n_ranges": self.n_ranges,
            "dpi": self.dpi,
            "largest_range_pages": self.largest_range_pages,
            "need_gb": round(self.need_gb, 2),
            "min_cpu_ram_gb": round(self.min_cpu_ram_gb, 2),
            "max_range_pixels": self.max_range_pixels,
            "max_range_pages": self.max_range_pages,
        }


def plan_document(
    source: Path | str | Sequence[tuple[float, float]],
    cfg: VastConfig,
    *,
    dpi: float = DEFAULT_BOUNDED_DPI,
) -> DocumentPlan:
    """The vast.ai range plan of a PDF (a path, read with ``page_boxes``) or
    of its page boxes in points: DEV's planner with the vast.ai budgets
    (``[vastai.ocr] max_range_pixels`` and ``max_range_pages``), then the RAM
    floor of the largest range."""
    boxes = page_boxes(Path(source)) if isinstance(source, (str, Path)) else list(source)
    ranges = plan_page_ranges(
        boxes, dpi=dpi, max_range_pixels=cfg.ocr.max_range_pixels, max_range_pages=cfg.ocr.max_range_pages
    )
    max_page_px = 0.0
    for width_pt, height_pt in boxes:
        max_page_px = max(
            max_page_px, (float(width_pt) / _POINTS_PER_INCH * dpi) * (float(height_pt) / _POINTS_PER_INCH * dpi)
        )
    largest = max((r.count for r in ranges), default=0)
    need = need_gb(largest * max_page_px, cfg)
    return DocumentPlan(
        pages=len(boxes),
        ranges=tuple(ranges),
        dpi=float(dpi),
        max_page_px=max_page_px,
        largest_range_pages=largest,
        largest_range_px=largest * max_page_px,
        need_gb=need,
        min_cpu_ram_gb=need / cfg.ocr.ram_headroom,
        max_range_pixels=cfg.ocr.max_range_pixels,
        max_range_pages=cfg.ocr.max_range_pages,
    )


# ---------------------------------------------------------------------------
# the search and the re-check
# ---------------------------------------------------------------------------
def offer_query(cfg: VastConfig, *, min_cpu_ram_gb: float, limit: int = OFFER_SEARCH_LIMIT) -> dict[str, Any]:
    """The read-only search body: tier, price, bandwidth, reliability, RAM,
    CUDA, disk and the egress host requirements [spec: search-offers
    reference; ``cpu_ram``/``gpu_ram`` in MB as the listing reports them].
    On-demand only."""
    tier = cfg.active_tier
    egress = cfg.egress
    query: dict[str, Any] = {
        "gpu_name": {"in": [g.replace(" ", "_") for g in tier.gpus] + list(tier.gpus)},
        "gpu_ram": {"gte": int(tier.min_vram_gb * 1024)},
        "num_gpus": {"eq": 1},
        "dph_total": {"lte": tier.max_dph},
        "reliability": {"gte": tier.min_reliability},
        "cpu_ram": {"gte": int(math.ceil(min_cpu_ram_gb * 1024))},
        "cuda_max_good": {"gte": cfg.image_cuda_version},
        "inet_down_cost": {"lte": cfg.ocr.max_inet_cost_per_gb},
        "inet_up_cost": {"lte": cfg.ocr.max_inet_cost_per_gb},
        "disk_space": {"gte": cfg.disk_gb},
        "allocated_storage": float(cfg.disk_gb),
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "type": cfg.rental_type,
        "order": [["dph_total", "asc"]],
        "limit": int(limit),
    }
    if egress.require_verified:
        query["verified"] = {"eq": True}
    if egress.require_datacenter:
        query["datacenter"] = {"eq": True}
    if egress.allow_geolocations:
        query["geolocation"] = {"in": list(egress.allow_geolocations)}
    return query


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _is_true(value: Any) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)


#: Where an offer says it is a datacentre host, most authoritative first.
#: ONE constant: the first field PRESENT in the offer decides, and only a
#: value this table maps to ``True`` admits the host.
#:
#: [RECORDED LIVE, 2026-09-20 00:21Z] ``plan --offer-keys 3`` on the high tier
#: with the API-side filter ``datacenter: {eq: True}``: the offer objects
#: vast.ai's ``POST /bundles/`` returns carry **no** ``datacenter`` and no
#: ``is_datacenter`` key (that name is the search filter's, not the object's).
#: They carry ``hosting_type``, and on every recorded offer the datacentre
#: filter returned its value is the integer ``1``
#: (``tests/test_vastai_r6_datacenter.py`` pins that shape's 100 key names and
#: values). No other value has been seen live and vast.ai's own client is not
#: among this lane's references, so **only ``hosting_type == 1`` admits**:
#: ``0`` is read as its complement, and any other value (a string, a float, a
#: bool, 2) is "a value this version does not map". All three refuse under
#: ``require_datacenter``, so the check still fails closed and no document can
#: leave to a host whose kind is unknown.
DATACENTER_FIELDS: tuple[str, ...] = ("datacenter", "is_datacenter", "hosting_type")

#: The one ``hosting_type`` value recorded on offers the API's own datacentre
#: filter returned. Only this admits; see :data:`DATACENTER_FIELDS`.
HOSTING_TYPE_DATACENTER: int = 1

#: What ``plan --offer-keys`` may print from a raw offer: non-sensitive
#: identity, price and capability fields only. Never the whole object, never
#: anything from the account.
OFFER_DEBUG_FIELDS: tuple[str, ...] = (
    "id", "gpu_name", "dph_total", "geolocation", "verification", "verified", "datacenter", "is_datacenter",
    "hosting_type", "host_id", "machine_id", "cuda_max_good", "cpu_ram", "num_gpus", "reliability", "rentable",
    "static_ip", "direct_port_count",
)


def datacenter_signal(offer: Mapping[str, Any]) -> tuple[bool | None, str]:
    """Is this a datacentre host? ``(verdict, what was read)``.

    ``True`` a positive signal, ``False`` a positive contrary signal, ``None``
    no field of :data:`DATACENTER_FIELDS` is present or its value is one this
    version does not map. The second item names the field and the value, so a
    refusal can say what it looked at. Only ``True`` admits a host under
    ``[vastai.egress] require_datacenter``: ``False`` and ``None`` both
    refuse."""
    for field in DATACENTER_FIELDS:
        if field not in offer:
            continue
        value = offer.get(field)
        seen = f"{field}={value!r}"
        if field == "hosting_type":
            # The live shape (recorded 2026-09-20): an integer, 1 on every
            # offer the API's own datacentre filter returned.
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f"{seen} (not an integer; only hosting_type == 1 is a datacentre)"
            if value == HOSTING_TYPE_DATACENTER:
                return True, seen
            if value == 0:
                return False, seen
            return None, f"{seen} (a value this version does not map; only hosting_type == 1 is a datacentre)"
        if _is_true(value):
            return True, seen
        if value is False or value == 0:
            return False, seen
        return None, f"{seen} (a value this version does not map)"
    return None, "none of " + "/".join(DATACENTER_FIELDS) + " is in the offer"


def offer_debug_rows(offers: Iterable[Mapping[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """For ``plan --offer-keys``: the KEY NAMES of the first ``limit`` raw
    offers and the values of :data:`OFFER_DEBUG_FIELDS` only."""
    rows: list[dict[str, Any]] = []
    for offer in offers:
        if len(rows) >= max(0, int(limit)):
            break
        rows.append({
            "offer_id": offer.get("id"),
            "keys": sorted(str(k) for k in offer),
            "values": {f: offer[f] for f in OFFER_DEBUG_FIELDS if f in offer},
        })
    return rows


def _is_verified(offer: Mapping[str, Any]) -> bool:
    if "verification" in offer:
        return str(offer.get("verification") or "").strip().lower() == "verified"
    return _is_true(offer.get("verified"))


def _country(geolocation: Any) -> str:
    return str(geolocation or "").split(",")[-1].strip().upper()


def offer_refusals(
    offer: Mapping[str, Any],
    cfg: VastConfig,
    *,
    min_cpu_ram_gb: float,
    exclude_machine_ids: Iterable[Any] = (),
) -> list[str]:
    """Why ``offer`` may not be rented for this job (empty = admitted). Fails
    closed: a field the check needs and the offer lacks is a refusal."""
    out: list[str] = []
    if offer.get("id") is None:
        out.append("no offer id")
    if int(offer.get("num_gpus") or 1) != 1:
        out.append(f"num_gpus {offer.get('num_gpus')} != 1")
    out.extend(cfg.active_tier.refusals(dict(offer), disk_gb=cfg.disk_gb))
    egress = cfg.egress
    if egress.require_datacenter:
        verdict, read = datacenter_signal(offer)
        if verdict is not True:
            out.append(f"not a datacenter host ([vastai.egress] require_datacenter; read {read})")
    if egress.require_verified and not _is_verified(offer):
        out.append("not a verified host ([vastai.egress] require_verified)")
    if egress.allow_geolocations:
        country, full = _country(offer.get("geolocation")), str(offer.get("geolocation") or "").strip().upper()
        if country not in egress.allow_geolocations and full not in egress.allow_geolocations:
            out.append(f"geolocation {offer.get('geolocation')!r} is not in [vastai.egress] allow_geolocations")
    ram = _number(offer.get("cpu_ram"))
    if ram is None or ram / 1024.0 < min_cpu_ram_gb:
        out.append(f"cpu_ram {offer.get('cpu_ram')} MB is below the RAM floor of {min_cpu_ram_gb:.1f} GB")
    cuda = _number(offer.get("cuda_max_good"))
    if cuda is None or cuda < cfg.image_cuda_version:
        out.append(f"cuda_max_good {offer.get('cuda_max_good')} < the image's CUDA {cfg.image_cuda}")
    for key in ("inet_down_cost", "inet_up_cost"):
        price = _number(offer.get(key))
        if price is None or price < 0 or price > cfg.ocr.max_inet_cost_per_gb:
            out.append(f"{key} {offer.get(key)} is not within [0, {cfg.ocr.max_inet_cost_per_gb}] $/GB")
    disk = _number(offer.get("disk_space"))
    if disk is None or disk < cfg.disk_gb:
        out.append(f"disk_space {offer.get('disk_space')} GB < [vastai.ocr] disk_gb = {cfg.disk_gb}")
    if offer.get("rentable") is False or offer.get("rented") is True:
        out.append("not rentable now")
    if offer.get("host_id") is None or offer.get("machine_id") is None:
        out.append("no host_id / machine_id (the ledger must record who held the document)")
    excluded = {str(m) for m in exclude_machine_ids}
    if offer.get("machine_id") is not None and str(offer.get("machine_id")) in excluded:
        out.append(f"machine {offer.get('machine_id')} is excluded for this job (an earlier lease failed on it)")
    return out


# ---------------------------------------------------------------------------
# one offer's numbers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OfferEstimate:
    offer: dict[str, Any]
    factor: float
    dph: float
    compute_s: float
    transfer_s: float
    ttl_uncapped_s: float
    ttl_s: float
    down_gb: float
    up_gb: float
    bandwidth_usd: float
    worst_usd: float
    pages_per_usd: float

    @property
    def offer_id(self) -> Any:
        return self.offer.get("id")

    @property
    def gpu_name(self) -> str:
        return str(self.offer.get("gpu_name") or "")

    @property
    def host_id(self) -> Any:
        return self.offer.get("host_id")

    @property
    def machine_id(self) -> Any:
        return self.offer.get("machine_id")

    @property
    def inet_down_cost(self) -> float:
        return float(self.offer.get("inet_down_cost") or 0.0)

    @property
    def inet_up_cost(self) -> float:
        return float(self.offer.get("inet_up_cost") or 0.0)

    @property
    def fits_ttl_cap(self) -> bool:
        return self.ttl_uncapped_s <= self.ttl_s

    def intent_fields(self) -> dict[str, Any]:
        """The offer's part of a ledger ``intent`` row."""
        o = self.offer
        return {
            "offer_id": o.get("id"),
            "host_id": o.get("host_id"),
            "machine_id": o.get("machine_id"),
            "datacenter": datacenter_signal(o)[0] is True,
            # What the verdict was READ from, so a saved ledger row or plan
            # output answers "which field, which value?" without a live read
            # (round 5 had to ask for one: the summary rows kept only the
            # derived boolean).
            "datacenter_read": datacenter_signal(o)[1],
            "verified": _is_verified(o),
            "verification": o.get("verification"),
            "geolocation": o.get("geolocation"),
            "gpu_name": o.get("gpu_name"),
            "dph": round(self.dph, 6),
            "inet_down_cost": o.get("inet_down_cost"),
            "inet_up_cost": o.get("inet_up_cost"),
            "ttl_s": round(self.ttl_s, 1),
            "worst_usd": round(self.worst_usd, 6),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.intent_fields(),
            "factor": self.factor,
            "compute_s": round(self.compute_s, 1),
            "transfer_s": round(self.transfer_s, 1),
            "ttl_uncapped_s": round(self.ttl_uncapped_s, 1),
            "down_gb": round(self.down_gb, 4),
            "up_gb": round(self.up_gb, 4),
            "bandwidth_usd": round(self.bandwidth_usd, 4),
            "pages_per_usd": round(self.pages_per_usd, 2),
        }


def estimate_offer(
    offer: Mapping[str, Any],
    plan: DocumentPlan,
    cfg: VastConfig,
    *,
    document_bytes: int,
    output_bytes: int | None = None,
    uplink_mb_s: float | None = None,
) -> OfferEstimate:
    """Design 6.3 for one offer. ``uplink_mb_s`` defaults to the configured
    rate (a caller may pass one re-measured from the ledger);
    ``output_bytes`` to :data:`OUTPUT_BYTES_PER_PAGE` per page."""
    offer = dict(offer)
    o = cfg.ocr
    factor = cfg.gpu_factor(offer.get("gpu_name", ""))
    compute_s = plan.pages * 60.0 / (o.dev_pages_per_min * factor) + plan.n_ranges * o.reload_s
    rate = float(uplink_mb_s if uplink_mb_s is not None else o.uplink_mb_s)
    transfer_s = float(document_bytes) / (rate * BYTES_PER_MB)
    uncapped = cfg.startup_s + transfer_s + cfg.safety * compute_s + cfg.grace_s
    ttl_s = min(cfg.ttl_cap_s, uncapped)
    dph = effective_dph(offer, cfg.disk_gb)
    out_bytes = plan.pages * OUTPUT_BYTES_PER_PAGE if output_bytes is None else int(output_bytes)
    down_gb = o.bootstrap_download_gb + float(document_bytes) / BYTES_PER_GB
    up_gb = out_bytes / BYTES_PER_GB
    bandwidth = down_gb * float(offer.get("inet_down_cost") or 0.0) + up_gb * float(offer.get("inet_up_cost") or 0.0)
    worst = dph * ttl_s / 3600.0 + bandwidth
    return OfferEstimate(
        offer=offer,
        factor=factor,
        dph=dph,
        compute_s=compute_s,
        transfer_s=transfer_s,
        ttl_uncapped_s=uncapped,
        ttl_s=ttl_s,
        down_gb=down_gb,
        up_gb=up_gb,
        bandwidth_usd=bandwidth,
        worst_usd=worst,
        pages_per_usd=(plan.pages / worst) if worst > 0 else 0.0,
    )


def rank_estimates(estimates: Iterable[OfferEstimate]) -> list[OfferEstimate]:
    """Best estimated pages per dollar first (with every factor at 1.0: the
    cheapest worst case); ties by effective price, then offer id."""
    return sorted(estimates, key=lambda e: (-e.pages_per_usd, e.dph, str(e.offer_id)))


def derived_range_timeout_s(
    cfg: VastConfig, *, range_pages: int, factor: float = 1.0, remaining_s: float | None = None
) -> float:
    """``[vastai.ocr] range_timeout_s``, or when it is 0: twice the expected
    range time plus 300 s -- never past the lease deadline (``remaining_s``)."""
    o = cfg.ocr
    if o.range_timeout_s > 0:
        timeout = o.range_timeout_s
    else:
        timeout = 2.0 * (range_pages * 60.0 / (o.dev_pages_per_min * factor) + o.reload_s) + 300.0
    return max(0.0, min(timeout, remaining_s)) if remaining_s is not None else timeout


# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------
def job_cap_usd(cfg: VastConfig, *approval_caps: float | None) -> float:
    """``min([vastai] max_job_usd, every approval's max_job_usd)``."""
    return min([cfg.max_job_usd] + [float(c) for c in approval_caps if c is not None])


def approval_cap_usd(cfg: VastConfig, approval_max_total_usd: float | None) -> float | None:
    """``min(the approval's max_total_usd, [vastai] max_approval_usd)``;
    ``None`` without an approval (``require_approval = false``)."""
    if approval_max_total_usd is None:
        return None
    return min(float(approval_max_total_usd), cfg.max_approval_usd)


def check_envelopes(
    cfg: VastConfig, spend: SpendView, *, approval_max_total_usd: float | None = None
) -> None:
    """The cheap admission check (no API call): refuse when the worker run's
    or the approval's envelope is already spent. Raises
    :class:`VastSpendRefused` (``cap-run`` / ``cap-approval``)."""
    if spend.run_usd >= cfg.max_run_usd:
        raise VastSpendRefused(
            "cap-run",
            f"spend refused: this worker run has spent ${spend.run_usd:.2f} (unsettled leases at their worst case) of "
            f"[vastai] max_run_usd = ${cfg.max_run_usd:.2f}. Nothing was rented.",
            next_actions=["start a new worker run", "or raise [vastai] max_run_usd"],
            details={"run_usd": spend.run_usd, "max_run_usd": cfg.max_run_usd},
        )
    cap = approval_cap_usd(cfg, approval_max_total_usd)
    if cap is not None and spend.approval_usd >= cap:
        raise VastSpendRefused(
            "cap-approval",
            f"spend refused: the approval's envelope ${cap:.2f} is spent (${spend.approval_usd:.2f}, unsettled leases "
            "at their worst case). Nothing was rented.",
            next_actions=["the OPERATOR runs `trialerror vastai approve-ocr` for a new envelope"],
            details={"approval_usd": spend.approval_usd, "approval_cap_usd": cap},
        )


def spend_from_ledger(
    ledger: Ledger, *, approval_nonce: str | None, worker_run_id: str | None, job_id: str | None = None
) -> SpendView:
    """The spend view of the ledger's rows (torn lines are ignored here; the
    doctor reports them). With ``job_id``, ``job_usd`` is what that job has
    booked in this worker run (its failover leases included)."""
    return spend_view(ledger.read().rows, approval_nonce=approval_nonce, worker_run_id=worker_run_id, job_id=job_id)


@dataclass(frozen=True)
class PricedJob:
    """The outcome of :func:`price_job`: ``offers`` are the admitted offers
    within every cap, best first -- the lease tries them in this order."""

    plan: DocumentPlan
    offers: tuple[OfferEstimate, ...]
    query: dict[str, Any]
    searched: int
    rejected: tuple[tuple[Any, tuple[str, ...]], ...]
    job_cap_usd: float
    run_left_usd: float
    approval_left_usd: float | None
    credit_usd: float | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def best(self) -> OfferEstimate:
        return self.offers[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.as_dict(),
            "offers": [e.as_dict() for e in self.offers],
            "searched": self.searched,
            "rejected": [{"offer_id": oid, "reasons": list(r)} for oid, r in self.rejected],
            "job_cap_usd": self.job_cap_usd,
            "run_left_usd": round(self.run_left_usd, 6),
            "approval_left_usd": None if self.approval_left_usd is None else round(self.approval_left_usd, 6),
            "credit_usd": self.credit_usd,
            "notes": list(self.notes),
        }


def _spend_refused(code: str, message: str, actions: list[str], **details: Any) -> VastSpendRefused:
    return VastSpendRefused(code, f"{message} Nothing was rented and the document was not sent.",
                            next_actions=actions, details=details)


def price_job(
    client: Any,
    cfg: VastConfig,
    plan: DocumentPlan,
    *,
    document_bytes: int,
    spend: SpendView | None = None,
    approval_max_job_usd: float | None = None,
    approval_max_total_usd: float | None = None,
    exclude_machine_ids: Iterable[Any] = (),
    output_bytes: int | None = None,
    uplink_mb_s: float | None = None,
    check_credit: bool = True,
    now: datetime | None = None,
    key_reader: Callable[[Any], str] | None = None,
) -> PricedJob:
    """One read-only offer search, the client-side re-check, the estimates,
    the ranking and every cap. Returns :class:`PricedJob` or raises
    :class:`VastPlanRefused` (``no-offer``, ``api-error``, ``key-missing``) /
    :class:`VastSpendRefused` (``cap-*``, ``credit-low``, and the high tier's
    ``approval-*``). Nothing is created here."""
    spend = spend or SpendView()
    now = now or datetime.now(timezone.utc)
    notes: list[str] = []
    high_cap: float | None = None
    if cfg.tier == "high":
        try:
            body = verify_high_tier_approval(  # reads where guard.approval_read_path says (round 4, d)
                cfg.config_root, cfg.api_key_path, now=now, key_reader=key_reader or read_api_key,
            )
        except HighTierRefused as exc:
            raise VastSpendRefused(
                exc.reason_code,
                f"{exc} Nothing was rented.",
                next_actions=["the OPERATOR runs `trialerror vastai approve-high`", "or set [vastai] tier = \"mid\""],
                disable_executor=exc.reason_code == "key-missing",
            ) from None
        high_cap = float(body.get("max_job_usd") or 0) or None
        notes.append(f"high tier approved until {body.get('expires')}")
    check_envelopes(cfg, spend, approval_max_total_usd=approval_max_total_usd)

    query = offer_query(cfg, min_cpu_ram_gb=plan.min_cpu_ram_gb)
    try:
        offers = client.search_offers(query)
    except VastKeyMissing as exc:
        raise VastPlanRefused(
            "key-missing", f"vast.ai key unavailable: {exc}. Nothing was rented.",
            next_actions=list(exc.next_actions) or ["place the key file at [vastai] api_key_path"],
            disable_executor=True,
        ) from None
    except VastApiError as exc:
        raise VastPlanRefused(
            "api-error", f"the vast.ai offer search failed: {exc}. Nothing was rented.",
            next_actions=["retry later (market or platform state, not the document)"],
            details={"status": exc.status},
        ) from None

    admitted: list[dict[str, Any]] = []
    rejected: list[tuple[Any, tuple[str, ...]]] = []
    for offer in offers:
        reasons = offer_refusals(offer, cfg, min_cpu_ram_gb=plan.min_cpu_ram_gb, exclude_machine_ids=exclude_machine_ids)
        if reasons:
            rejected.append((offer.get("id"), tuple(reasons)))
        else:
            admitted.append(offer)
    if not admitted:
        raise VastPlanRefused(
            "no-offer",
            f"no vast.ai offer passes the requirements (searched {len(offers)}; tier {cfg.tier!r}, RAM floor "
            f"{plan.min_cpu_ram_gb:.1f} GB, datacenter={cfg.egress.require_datacenter}, "
            f"verified={cfg.egress.require_verified}). Nothing was rented and the document was not sent.",
            next_actions=["retry later", "or review [vastai.tiers] and [vastai.egress] host requirements"],
            details={"searched": len(offers), "rejected": [{"offer_id": i, "reasons": list(r)} for i, r in rejected[:20]]},
        )

    ranked = rank_estimates(
        estimate_offer(o, plan, cfg, document_bytes=document_bytes, output_bytes=output_bytes, uplink_mb_s=uplink_mb_s)
        for o in admitted
    )
    fits = [e for e in ranked if e.fits_ttl_cap]
    if not fits:
        best = ranked[0]
        raise _spend_refused(
            "cap-ttl",
            f"spend refused: the job needs an estimated {best.ttl_uncapped_s:.0f} s of lease but [vastai] ttl_cap_s is "
            f"{cfg.ttl_cap_s:.0f} s.",
            ["lower [vastai.ocr] max_range_pages or split the document", "or leave it for a DEV-GPU run"],
            ttl_uncapped_s=best.ttl_uncapped_s, ttl_cap_s=cfg.ttl_cap_s,
        )
    job_cap = job_cap_usd(cfg, approval_max_job_usd, high_cap)
    # The cap bounds the JOB: what its earlier leases booked in this run (a
    # failover's predecessors, unsettled ones at their worst case) counts.
    job_spent = spend.job_usd
    within = [e for e in fits if job_spent + e.worst_usd <= job_cap]
    if not within and job_spent > 0:
        best = fits[0]
        raise _spend_refused(
            "cap-job",
            f"spend refused: this job has spent ${job_spent:.2f} (earlier leases, unsettled ones at their worst case); "
            f"another worst case of ${best.worst_usd:.2f} would pass the per-job cap ${job_cap:.2f}.",
            ["retry the job in a new worker run (its range cache keeps what was produced)",
             "or raise [vastai] max_job_usd (and the approval's, with `trialerror vastai approve-ocr`)"],
            worst_usd=best.worst_usd, job_usd=job_spent, job_cap_usd=job_cap,
        )
    if not within:
        best = fits[0]
        raise _spend_refused(
            "cap-job",
            f"spend refused: worst-case cost ${best.worst_usd:.2f} (${best.dph:.3f}/h x TTL {best.ttl_s:.0f} s + "
            f"${best.bandwidth_usd:.2f} bandwidth) exceeds the per-job cap ${job_cap:.2f}.",
            ["raise [vastai] max_job_usd (and the approval's, with `trialerror vastai approve-ocr`)",
             "or leave it for a DEV-GPU run"],
            worst_usd=best.worst_usd, job_cap_usd=job_cap,
        )
    run_left = cfg.max_run_usd - spend.run_usd
    fits_run = [e for e in within if e.worst_usd <= run_left]
    if not fits_run:
        best = within[0]
        raise _spend_refused(
            "cap-run",
            f"spend refused: this worker run has spent ${spend.run_usd:.2f}; another worst case of "
            f"${best.worst_usd:.2f} would pass [vastai] max_run_usd = ${cfg.max_run_usd:.2f}.",
            ["start a new worker run", "or raise [vastai] max_run_usd"],
            worst_usd=best.worst_usd, run_usd=spend.run_usd, max_run_usd=cfg.max_run_usd,
        )
    approval_cap = approval_cap_usd(cfg, approval_max_total_usd)
    approval_left = None if approval_cap is None else approval_cap - spend.approval_usd
    fits_approval = fits_run if approval_left is None else [e for e in fits_run if e.worst_usd <= approval_left]
    if not fits_approval:
        best = fits_run[0]
        raise _spend_refused(
            "cap-approval",
            f"spend refused: ${spend.approval_usd:.2f} is spent under this approval; another worst case of "
            f"${best.worst_usd:.2f} would pass its envelope ${approval_cap:.2f}.",
            ["the OPERATOR runs `trialerror vastai approve-ocr` for a new envelope"],
            worst_usd=best.worst_usd, approval_usd=spend.approval_usd, approval_cap_usd=approval_cap,
        )
    credit = None
    if check_credit:
        try:
            credit = client.account_credit()
        except VastApiError as exc:
            raise VastPlanRefused(
                "api-error", f"the vast.ai account read failed: {exc}. Nothing was rented.",
                next_actions=["retry later"], details={"status": exc.status},
            ) from None
    final = fits_approval if credit is None else [e for e in fits_approval if e.worst_usd <= credit]
    if not final:
        best = fits_approval[0]
        raise _spend_refused(
            "credit-low",
            f"spend refused: the account's credit ${credit:.2f} is below the worst case ${best.worst_usd:.2f} (a lease "
            "the account cannot pay for is stopped mid-job).",
            ["add credit to the vast.ai account"],
            credit_usd=credit, worst_usd=best.worst_usd,
        )
    if credit is None and check_credit:
        notes.append("the account's credit is not exposed by the API; the credit cap was not applied")
    return PricedJob(
        plan=plan,
        offers=tuple(final),
        query=query,
        searched=len(offers),
        rejected=tuple(rejected),
        job_cap_usd=job_cap,
        run_left_usd=run_left,
        approval_left_usd=approval_left,
        credit_usd=credit,
        notes=tuple(notes),
    )
