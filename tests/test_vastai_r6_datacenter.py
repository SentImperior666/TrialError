"""Round 6: the datacentre field, mapped on the RECORDED live shape.

Round 5 left this open: the API-side filter ``datacenter: {eq: True}`` returned
8 offers and the post-check refused all 8, because the offer objects carry no
field of that name. ``plan --offer-keys 3`` was added for one free read, and the
custodian ran it on 2026-09-20 00:21Z against the high tier with the datacentre
filter on. What came back, for all three printed offers:

* **no** ``datacenter`` key and **no** ``is_datacenter`` key -- those names
  belong to the search filter, not to the object;
* ``hosting_type`` = the integer ``1``;
* ``verification`` = ``"verified"``, and the neighbouring host-kind fields
  ``vericode``, ``is_vm_deverified``, ``external``, ``static_ip``,
  ``direct_port_count``, ``public_ipaddr``.

:data:`RECORDED_KEYS` is that shape's key list verbatim (identical on all three
offers) and :data:`RECORDED_VALUES` the allow-listed values of the first one, an
RTX 5090 in South Korea at $0.548/h. The mapping is therefore: **only
``hosting_type == 1`` is a datacentre.** ``0`` is read as its complement, and
any other value -- 2, a string, a float, a bool -- is a value this version does
not map. All three of those refuse under ``require_datacenter``, so the check
still fails closed.

What is NOT recorded: the ``hosting_type`` of a NON-datacentre offer. The saved
plan rows of 2026-09-19 kept only the tool's derived boolean, so they cannot
answer it; ``intent_fields`` now carries ``datacenter_read`` so that the next
saved row does. Until such a read exists, a non-datacentre value is refused
whatever it turns out to be, which is the safe direction.

No vast.ai call, no ssh, no GPU, no key material.
"""

from __future__ import annotations

import pytest

from tests._vastai_fakes import dev_toml, make_offer, toml_text, write_key
from trialerror.vastai.pricing import (
    DATACENTER_FIELDS,
    HOSTING_TYPE_DATACENTER,
    OfferEstimate,
    datacenter_signal,
    offer_refusals,
)

#: The key names of a live offer object, recorded 2026-09-20 00:21Z by
#: ``trialerror vastai plan --offer-keys 3`` (one free read, no rental) on the
#: high tier with ``[vastai.egress] require_datacenter = true``. Identical on
#: all three printed offers.
RECORDED_KEYS: tuple[str, ...] = (
    "ask_contract_id", "avail_vol_ask_id", "avail_vol_dph", "avail_vol_size", "bundle_id", "bundled_results",
    "bw_nvlink", "cluster_id", "compute_cap", "cpu_arch", "cpu_cores", "cpu_cores_effective", "cpu_ghz", "cpu_name",
    "cpu_ram", "credit_discount_max", "cuda_max_good", "direct_port_count", "discount_rate", "discounted_dph_total",
    "discounted_hourly", "disk_bw", "disk_name", "disk_space", "dlperf", "dlperf_per_dphtotal", "dph_base",
    "dph_total", "dph_total_adj", "driver_vers", "driver_version", "duration", "end_date", "expected_reliability",
    "external", "flops_per_dphtotal", "geolocation", "geolocode", "gpu_arch", "gpu_display_active", "gpu_frac",
    "gpu_ids", "gpu_lanes", "gpu_max_power", "gpu_max_temp", "gpu_mem_bw", "gpu_name", "gpu_ram", "gpu_total_ram",
    "has_avx", "host_id", "hosting_type", "hostname", "id", "inet_down", "inet_down_cost", "inet_up", "inet_up_cost",
    "instance", "internet_down_cost_per_tb", "internet_up_cost_per_tb", "is_bid", "is_vm_deverified", "logo",
    "machine_id", "min_bid", "mobo_name", "num_gpus", "nw_disk_avg_bw", "nw_disk_max_bw", "nw_disk_min_bw",
    "os_version", "pci_gen", "pcie_bw", "public_ipaddr", "reliability", "reliability2", "reliability_mult",
    "rentable", "rented", "resource_type", "rn", "score", "search", "sla_broker_rate", "sla_r_claim", "sla_sigma_x",
    "start_date", "static_ip", "storage_cost", "storage_total_cost", "target_reliability", "time_remaining",
    "time_remaining_isbid", "total_flops", "vericode", "verification", "vms_enabled", "vram_costperhour", "webpage",
)

#: The allow-listed values of the first recorded offer (``OFFER_DEBUG_FIELDS``).
RECORDED_VALUES: dict[str, object] = {
    "id": 44173797,
    "gpu_name": "RTX 5090",
    "dph_total": 0.5481481481481482,
    "geolocation": "South Korea, KR",
    "verification": "verified",
    "hosting_type": 1,
    "host_id": 406325,
    "machine_id": 143413,
    "cuda_max_good": 13.0,
    "cpu_ram": 31197,
    "num_gpus": 1,
    "reliability": 0.9970292,
    "rentable": True,
    "static_ip": True,
    "direct_port_count": 98,
}


def recorded_offer(**overrides: object) -> dict[str, object]:
    """An offer with the recorded live KEYS (every one of them present) and the
    recorded values where they were printed. Fields the debug aid does not
    print are ``None``, which is what a fail-closed check must survive."""
    offer: dict[str, object] = {key: None for key in RECORDED_KEYS}
    offer.update(RECORDED_VALUES)
    offer.update(overrides)
    return offer


# ---------------------------------------------------------------------------
# the recorded shape
# ---------------------------------------------------------------------------
def test_the_recorded_shape_has_no_datacenter_key_and_does_have_hosting_type():
    offer = recorded_offer()
    assert "datacenter" not in offer, "the recorded object has no field of the filter's name"
    assert "is_datacenter" not in offer
    assert offer["hosting_type"] == 1
    assert offer["verification"] == "verified"


def test_the_recorded_datacentre_offer_is_a_positive_signal():
    verdict, read = datacenter_signal(recorded_offer())
    assert verdict is True
    assert read == "hosting_type=1"


def test_the_field_order_still_prefers_a_future_explicit_field():
    """If vast.ai ever adds the filter's own name to the object, that decides;
    ``hosting_type`` is the fallback, not the override."""
    assert DATACENTER_FIELDS == ("datacenter", "is_datacenter", "hosting_type")
    assert datacenter_signal(recorded_offer(datacenter=False))[0] is False
    assert datacenter_signal(recorded_offer(is_datacenter=True))[0] is True


# ---------------------------------------------------------------------------
# every value, and what it means
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, verdict",
    [
        (1, True),            # the one recorded live value
        (0, False),           # read as its complement; refuses either way
        (2, None),            # a value this version does not map
        (3, None),
        (-1, None),
        ("1", None),          # not the recorded shape: a string is not an int
        ("datacenter", None),
        (1.0, None),          # a float is not the recorded shape either
        (True, None),         # a bool is not an int here, on purpose
        (None, None),
    ],
)
def test_only_hosting_type_1_is_a_datacentre(value, verdict):
    got, read = datacenter_signal(recorded_offer(hosting_type=value))
    assert got is verdict
    assert read.startswith("hosting_type=")
    if verdict is None:
        assert "does not map" in read or "not an integer" in read


def test_an_offer_without_the_field_at_all_is_unknown_and_names_all_three():
    offer = {key: None for key in RECORDED_KEYS if key != "hosting_type"}
    verdict, read = datacenter_signal(offer)
    assert verdict is None
    assert read == "none of datacenter/is_datacenter/hosting_type is in the offer"


def test_the_constant_is_the_one_place_the_value_lives():
    assert HOSTING_TYPE_DATACENTER == 1
    assert datacenter_signal({"hosting_type": HOSTING_TYPE_DATACENTER})[0] is True


# ---------------------------------------------------------------------------
# what the egress check does with it
# ---------------------------------------------------------------------------
@pytest.fixture()
def dc_cfg(tmp_path):
    """A config that requires a datacentre host."""
    from trialerror.util.config import load_config
    from trialerror.vastai.config import load_vast_config

    root = tmp_path / "devroot"
    write_key(root / "keys")
    (root / "trialerror.toml").write_text(
        toml_text(dev_toml(egress={"require_datacenter": True, "allow_documents": ["ab" * 32]})), encoding="utf-8"
    )
    cfg = load_vast_config(load_config(root / "trialerror.toml").raw, config_root=root)
    assert cfg.egress.require_datacenter is True
    return cfg


def _dc_reasons(offer, cfg):
    return [r for r in offer_refusals(offer, cfg, min_cpu_ram_gb=1.0) if "datacenter host" in r]


def test_a_recorded_datacentre_offer_passes_the_egress_check(dc_cfg):
    offer = {**make_offer(1), "hosting_type": 1}
    offer.pop("datacenter", None)
    assert _dc_reasons(offer, dc_cfg) == []


@pytest.mark.parametrize("value", [0, 2, "datacenter", True, None])
def test_every_other_hosting_type_is_refused_and_the_refusal_names_the_value(value, dc_cfg):
    offer = {**make_offer(1), "hosting_type": value}
    offer.pop("datacenter", None)
    reasons = _dc_reasons(offer, dc_cfg)
    assert len(reasons) == 1
    assert "[vastai.egress] require_datacenter" in reasons[0]
    assert "hosting_type=" in reasons[0], "the refusal says which field and which value it read"


def test_a_missing_field_is_refused_and_the_refusal_names_the_fields(dc_cfg):
    offer = {**make_offer(1)}
    offer.pop("datacenter", None)
    reasons = _dc_reasons(offer, dc_cfg)
    assert len(reasons) == 1
    assert "none of datacenter/is_datacenter/hosting_type is in the offer" in reasons[0]


# ---------------------------------------------------------------------------
# the record keeps what was READ, so the next evidence answers this offline
# ---------------------------------------------------------------------------
def _estimate(offer) -> OfferEstimate:
    return OfferEstimate(
        offer=dict(offer), factor=1.0, dph=0.5, compute_s=1.0, transfer_s=1.0, ttl_uncapped_s=10.0, ttl_s=10.0,
        down_gb=0.0, up_gb=0.0, bandwidth_usd=0.0, worst_usd=0.01, pages_per_usd=1.0,
    )


def test_the_intent_row_records_the_field_and_value_behind_the_verdict():
    row = _estimate(recorded_offer()).intent_fields()
    assert row["datacenter"] is True
    assert row["datacenter_read"] == "hosting_type=1"


def test_the_intent_row_of_a_host_without_the_field_says_so():
    offer = {key: None for key in RECORDED_KEYS if key != "hosting_type"}
    row = _estimate(offer).intent_fields()
    assert row["datacenter"] is False
    assert row["datacenter_read"] == "none of datacenter/is_datacenter/hosting_type is in the offer"
