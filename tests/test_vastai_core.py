"""vast.ai OCR executor, lane L1: config, settlement classes, the REST client,
tiers, the planner's page cap, and the cost model with its caps.

Everything runs against ``tests/_vastai_fakes.py``: no network (the tripwire
fails any real call), no GPU, no real key.
"""

from __future__ import annotations

import builtins
import math
import pathlib

import pytest

from tests._vastai_fakes import (
    FAKE_KEY,
    FakeVast,
    NetworkTripwire,
    default_offers,
    dev_toml,
    isolated_state,  # noqa: F401 - fixture
    make_offer,
    network_tripwire,  # noqa: F401 - fixture
    write_key,
)
from trialerror.ingest.backends import DEFAULT_MAX_RANGE_PIXELS, PageRange, plan_page_ranges
from trialerror.offload.settle import ClaimReturned
from trialerror.vastai import config as vconfig
from trialerror.vastai.api import OfferUnavailable, VastClient, VastKeyMissing, read_api_key
from trialerror.vastai.config import CONFIG_KEYS, EMBED_KEYS, check_runtime_files, load_vast_config, runtime_files
from trialerror.vastai.errors import (
    REASON_CODES,
    EgressRefused,
    HostFailure,
    JobRefused,
    StackMismatch,
    VastConfigError,
    VastPlanRefused,
    VastSpendRefused,
)
from trialerror.vastai.lease import LeaseExpired
from trialerror.vastai.ledger import Ledger, SpendView
from trialerror.vastai.pricing import (
    check_envelopes,
    estimate_offer,
    offer_query,
    plan_document,
    price_job,
    spend_from_ledger,
)
from trialerror.vastai.tiers import effective_dph

LARGE_PT = (1500.0, 2300.0)  # design 6.2's large-format page, in points
A4_PT = (595.28, 841.89)
SHA = "a" * 64


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state):  # noqa: F811 - the fixtures above
    yield


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "devroot"
    write_key(r / "keys")
    return r


def _cfg(root, **kw):
    return load_vast_config(dev_toml(**kw), config_root=root)


def _client(root, fake):
    return VastClient(root / "keys" / "vastai.key", http=fake.http)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def test_no_vastai_table_and_a_local_executor_load_as_off(root):
    for raw in ({"program": {"id": "x"}}, {"ingest": {"ocr": {"executor": "local", "backend": "marker"}}}):
        cfg = load_vast_config(raw, config_root=root)
        assert cfg.enabled is False and cfg.configured is False
        assert cfg.status.startswith("vast.ai is off")
    cfg = load_vast_config(dev_toml(executor="local"), config_root=root)
    assert cfg.enabled is False and cfg.configured is True and "is off" in cfg.status


def test_defaults_are_the_lane_contracts(root):
    cfg = _cfg(root)
    assert cfg.enabled and cfg.status.startswith("vast.ai is on")
    assert (cfg.max_job_usd, cfg.max_run_usd, cfg.max_approval_usd) == (3.0, 10.0, 25.0)
    assert (cfg.ttl_cap_s, cfg.startup_s, cfg.safety, cfg.grace_s) == (14400, 1500, 1.5, 300)
    assert cfg.image == "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime" and cfg.image_cuda == "13.0"
    assert (cfg.disk_gb, cfg.poll_interval_s, cfg.doctor_api_check, cfg.tier) == (32, 10, True, "mid")
    o = cfg.ocr
    assert (o.max_range_pixels, o.max_range_pages, o.fixed_mem_gb, o.mem_bytes_per_px, o.ram_headroom) == (
        2_000_000_000, 128, 16, 9.4, 0.8)
    assert (o.dev_pages_per_min, o.reload_s, o.uplink_mb_s, o.bootstrap_download_gb) == (7.5, 60, 1.0, 13.0)
    assert (o.max_document_mb, o.max_leases_per_job, o.max_inet_cost_per_gb) == (1024, 2, 0.02)
    assert (o.canary_min_similarity, o.range_timeout_s) == (0.97, 0)
    assert o.requirements_lock == root / "marker-requirements.lock" and o.models_manifest_packaged
    e = cfg.egress
    assert (e.allow_license_tiers, e.allow_documents, e.allow_geolocations) == ((), (), ())
    assert (e.require_datacenter, e.require_verified, e.remote_scratch) == (True, True, "shm")
    assert (e.shm_required_tiers, e.require_approval, e.approval_max_days, e.when_refused) == (
        ("commercial_restricted",), True, 7, "return")
    assert cfg.approval_path == root / "keys" / "vastai-ocr.approval"
    assert cfg.gpu_factor("RTX 5090") == 1.0


def test_config_keys_describe_every_key_with_the_default_the_loader_uses(root):
    cfg = _cfg(root)
    seen = set()
    attr = {"type": "rental_type"}
    for k in CONFIG_KEYS:
        assert k.meaning and k.default_text, k
        seen.add((k.table, k.key))
        if k.default is None or k.table in ("ingest.ocr", "vastai.tiers.<t>", "vastai.ocr_gpu_factors"):
            continue
        holder = cfg.embed if (k.table, k.key) in EMBED_KEYS else (
            {"vastai": cfg, "vastai.ocr": cfg.ocr, "vastai.egress": cfg.egress}[k.table])
        if (k.table, k.key) in (("vastai.ocr", "requirements_lock"), ("vastai.ocr", "models_manifest")):
            continue
        value = getattr(holder, attr.get(k.key, k.key))
        expected = tuple(k.default) if isinstance(k.default, list) else k.default
        assert value == expected, (k.table, k.key)
    assert ("ingest.ocr", "executor") in seen and ("vastai.egress", "when_refused") in seen
    assert {"3.00", "10.00", "25.00"} <= {k.default_text for k in CONFIG_KEYS}


@pytest.mark.parametrize("table", ["vastai", "ocr", "egress"])
@pytest.mark.parametrize("key", ["keep_alive", "reuse_instance", "ttl_extend"])
def test_keep_alive_reuse_and_ttl_extension_are_refused_by_name(root, table, key):
    kw = {"vastai": {key: True}} if table == "vastai" else {table: {key: True}}
    with pytest.raises(VastConfigError, match=f"{key}: no keep-alive"):
        _cfg(root, **kw)


def test_bid_rentals_are_refused_by_name(root):
    with pytest.raises(VastConfigError, match='type = "bid": interruptible instances are refused'):
        _cfg(root, vastai={"type": "bid"})
    assert _cfg(root, vastai={"type": "ondemand"}).rental_type == "ondemand"


def test_unknown_cannot_be_named_as_a_tier(root):
    with pytest.raises(VastConfigError, match="names 'unknown'") as info:
        _cfg(root, egress={"allow_license_tiers": ["open", "unknown"]})
    assert any("allow_documents" in a for a in info.value.next_actions)
    with pytest.raises(VastConfigError, match="not a licence tier"):
        _cfg(root, egress={"allow_license_tiers": ["public_domain"]})
    cfg = _cfg(root, egress={"allow_license_tiers": ["open", "academic_oa", "open"]})
    assert cfg.egress.allow_license_tiers == ("academic_oa", "open")


@pytest.mark.parametrize("bad", ["abc", "a" * 63, "g" * 64, "a" * 65, ""])
def test_a_malformed_sha256_is_refused(root, bad):
    with pytest.raises(VastConfigError, match=r"allow_documents\[1\].*not a sha256"):
        _cfg(root, egress={"allow_documents": [SHA, bad]})


def test_sha256_is_normalised_to_lower_case(root):
    cfg = _cfg(root, egress={"allow_documents": ["AB" * 32, "ab" * 32]})
    assert cfg.egress.allow_documents == ("ab" * 32,)


@pytest.mark.parametrize("key", ["max_job_usd", "max_run_usd", "max_approval_usd", "ttl_cap_s"])
@pytest.mark.parametrize("bad", [0, -1, math.inf, math.nan, True, "3"])
def test_caps_must_be_finite_and_positive(root, key, bad):
    if key == "ttl_cap_s" and bad == math.inf:  # round 3 (C-3): above 4 h is clamped, as the public loader does
        assert _cfg(root, vastai={key: bad}).ttl_cap_s == 14400
        return
    with pytest.raises(VastConfigError, match=rf"\[vastai\] {key}"):
        _cfg(root, vastai={key: bad})


def test_ocr_caps_must_be_positive(root):
    for key in ("max_document_mb", "max_inet_cost_per_gb", "max_range_pixels"):
        with pytest.raises(VastConfigError, match=rf"\[vastai.ocr\] {key}"):
            _cfg(root, ocr={key: 0})
    with pytest.raises(VastConfigError, match="max_range_pages"):
        _cfg(root, ocr={"max_range_pages": 0})


def test_the_ttl_cap_can_be_lowered_but_never_raised_above_four_hours(root):
    # round 3 (C-3): a value above 4 h is clamped with a note, as the public loader clamps it
    cfg = _cfg(root, vastai={"ttl_cap_s": 14401})
    assert cfg.ttl_cap_s == 14400 and cfg.notes == ("ttl_cap_s 14401 clamped to the absolute cap 14400",)
    assert _cfg(root, vastai={"ttl_cap_s": 3600}).ttl_cap_s == 3600


@pytest.mark.parametrize("bad", [0, -1, 7.5, 8])
def test_approval_max_days_is_within_zero_and_seven(root, bad):
    with pytest.raises(VastConfigError, match="approval_max_days"):
        _cfg(root, egress={"approval_max_days": bad})


def test_approval_max_days_may_be_lowered(root):
    assert _cfg(root, egress={"approval_max_days": 0.5}).egress.approval_max_days == 0.5


def test_bad_choices_are_refused_by_name(root):
    with pytest.raises(VastConfigError, match="remote_scratch = 'disk'"):
        _cfg(root, egress={"remote_scratch": "disk"})
    with pytest.raises(VastConfigError, match="when_refused = 'queue'"):
        _cfg(root, egress={"when_refused": "queue"})
    with pytest.raises(VastConfigError, match="executor = 'cloud'"):
        _cfg(root, executor="cloud")
    with pytest.raises(VastConfigError, match="tier = 'huge'"):
        _cfg(root, vastai={"tier": "huge"})


def test_when_refused_local_needs_a_local_marker(root):
    with pytest.raises(VastConfigError, match="marker_single_exe is not set"):
        _cfg(root, egress={"when_refused": "local"})
    cfg = _cfg(root, egress={"when_refused": "local"}, ingest_ocr={"marker_single_exe": "marker_single"})
    assert cfg.egress.when_refused == "local"


def test_the_vastai_executor_needs_the_key_and_identity_paths(root):
    raw = dev_toml()
    del raw["vastai"]["api_key_path"]
    with pytest.raises(VastConfigError, match="api_key_path is required"):
        load_vast_config(raw, config_root=root)
    raw = dev_toml()
    del raw["vastai"]["ssh_identity_path"]
    with pytest.raises(VastConfigError, match="ssh_identity_path is required"):
        load_vast_config(raw, config_root=root)
    raw = {"ingest": {"ocr": {"executor": "vastai"}}}
    with pytest.raises(VastConfigError, match="api_key_path is required"):
        load_vast_config(raw, config_root=root)


def test_a_misspelt_key_is_refused_rather_than_ignored(root):
    with pytest.raises(VastConfigError, match="max_jb_usd: not a vast.ai OCR setting"):
        _cfg(root, vastai={"max_jb_usd": 1.0})
    with pytest.raises(VastConfigError, match=r"\[vastai.egress\] allow_tier"):
        _cfg(root, egress={"allow_tier": ["open"]})


def test_a_key_in_the_path_slot_is_refused_and_never_echoed(root):
    key_like = "0123456789abcdef" * 4
    with pytest.raises(VastConfigError) as info:
        _cfg(root, vastai={"api_key_path": key_like})
    assert key_like not in str(info.value) and "value withheld" in str(info.value)


def test_another_marker_version_needs_its_own_model_manifest(root):
    with pytest.raises(VastConfigError, match="no packaged model manifest"):
        _cfg(root, ingest_ocr={"marker_version": "1.11.0"})
    cfg = _cfg(root, ingest_ocr={"marker_version": "1.11.0"}, ocr={"models_manifest": "models-1.11.sha256"})
    assert cfg.ocr.models_manifest == root / "models-1.11.sha256" and not cfg.ocr.models_manifest_packaged


def test_tier_tables_override_the_ported_ones(root):
    cfg = _cfg(root, vastai={"tiers": {"mid": {"max_dph": 0.30, "gpus": ["RTX 3090"]}}})
    assert cfg.active_tier.max_dph == 0.30 and cfg.active_tier.gpus == ("RTX 3090",)
    with pytest.raises(VastConfigError, match=r"\[vastai.tiers.ultra\]: unknown tier"):
        _cfg(root, vastai={"tiers": {"ultra": {}}})
    with pytest.raises(VastConfigError, match="max_dph"):
        _cfg(root, vastai={"tiers": {"mid": {"max_dph": 0}}})


def test_runtime_files_are_checked_for_existence_and_never_opened(root, monkeypatch):
    cfg = _cfg(root, ocr={"models_manifest": "models.sha256"})
    with pytest.raises(VastConfigError) as info:
        check_runtime_files(cfg)
    message = str(info.value)
    assert "[vastai] ssh_identity_path" in message and "[vastai.ocr] requirements_lock" in message
    assert "[vastai] api_key_path" not in message  # the key file exists
    assert "`trialerror vastai lock-deps`" in message and any("lock-deps" in a for a in info.value.next_actions)
    (root / "keys" / "vastai_ed25519").write_text("not a real identity")
    (root / "marker-requirements.lock").write_text("pins")
    (root / "models.sha256").write_text("hashes")

    def refuse(*a, **k):
        raise AssertionError("check_runtime_files opened a file")

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", refuse)
        m.setattr(pathlib.Path, "open", refuse)
        m.setattr(pathlib.Path, "read_text", refuse)
        m.setattr(pathlib.Path, "read_bytes", refuse)
        paths = check_runtime_files(cfg)
        report = runtime_files(cfg)
    assert paths["[vastai] api_key_path"] == root / "keys" / "vastai.key"
    assert all(entry["exists"] for entry in report.values())


# ---------------------------------------------------------------------------
# settlement classes
# ---------------------------------------------------------------------------
def test_refusals_are_returned_claims_with_known_reason_codes():
    refused = EgressRefused("tier-not-allowed", "no", next_actions=["name it"])
    assert isinstance(refused, ClaimReturned) and isinstance(refused, JobRefused)
    assert refused.reason_code == "tier-not-allowed" and refused.next_actions == ["name it"]
    assert refused.disable_executor is False and refused.as_dict()["reason_code"] == "tier-not-allowed"
    assert StackMismatch("stack-mismatch", "wrong marker").disable_executor is True
    assert VastPlanRefused("key-missing", "x", disable_executor=True).disable_executor is True
    with pytest.raises(ValueError, match="unknown vast.ai refusal reason code"):
        VastSpendRefused("cap-everything", "x")
    assert not isinstance(HostFailure("died"), ClaimReturned)
    assert issubclass(LeaseExpired, KeyboardInterrupt)
    assert "stage-not-served" in REASON_CODES and len(set(REASON_CODES)) == 22


# ---------------------------------------------------------------------------
# the REST client (ported, with its recorded drifts)
# ---------------------------------------------------------------------------
def test_instance_listing_uses_v1_because_v0_is_retired(root):
    fake = FakeVast()
    assert _client(root, fake).list_instances() == []
    assert ("GET", "/instances/?owner=me") in fake.calls


def test_destroy_falls_back_to_v1_when_v0_is_retired(root):
    fake = FakeVast()
    fake.v0_delete_gone = True
    client = _client(root, fake)
    fake.instances[1] = {"id": 1, "label": "x"}
    client.destroy_instance(1)
    assert fake.instances == {} and fake.verbs("DELETE") == ["/instances/1/", "/instances/1/"]


def test_a_taken_offer_is_offer_unavailable(root):
    fake = FakeVast()
    fake.gone = {11}
    with pytest.raises(OfferUnavailable) as info:
        _client(root, fake).create_instance(11, image="img", disk_gb=32, label="l", onstart="o")
    assert info.value.offer_id == 11 and fake.instances == {}


def test_a_key_passed_where_the_path_belongs_is_never_echoed():
    key_like = "0123456789abcdef" * 4
    with pytest.raises(VastKeyMissing) as info:
        read_api_key(key_like)
    assert key_like not in str(info.value)
    with pytest.raises(VastKeyMissing) as info:
        VastClient(key_like, http=lambda *a: (200, {})).list_instances()
    assert key_like not in str(info.value)


def test_account_credit_is_best_effort(root):
    fake = FakeVast()
    client = _client(root, fake)
    assert client.account_credit() is None
    fake.credit = 12.5
    assert client.account_credit() == 12.5


def test_the_network_tripwire_catches_a_default_client(root, network_tripwire):  # noqa: F811
    client = VastClient(root / "keys" / "vastai.key")
    with pytest.raises(NetworkTripwire):
        client.list_instances()
    assert network_tripwire == [("GET", "https://console.vast.ai/api/v1/instances/")]
    network_tripwire.clear()  # seen and asserted: the teardown check would fail the test otherwise


# ---------------------------------------------------------------------------
# tiers
# ---------------------------------------------------------------------------
def test_the_plan_prices_the_disk_the_lease_rents():
    offer = {"dph_base": 0.13333, "dph_total": 0.14296, "storage_cost": 0.86667}
    assert abs(effective_dph(offer, 40) - 0.1808) < 0.001  # observed 2026-09-18
    assert effective_dph({"dph_total": 0.2}, 40) == 0.2
    assert effective_dph(offer, None) == 0.14296


def test_the_tier_ceiling_is_compared_with_the_billed_price(root):
    cfg = _cfg(root)
    cheap_listing = make_offer(20, dph_base=0.40, storage_cost=5.0, dph_total=0.41)
    assert effective_dph(cheap_listing, 32) > 0.45
    assert not cfg.active_tier.admits(cheap_listing, disk_gb=cfg.disk_gb)
    assert cfg.active_tier.admits(make_offer(21), disk_gb=cfg.disk_gb)


# ---------------------------------------------------------------------------
# the planner's page cap
# ---------------------------------------------------------------------------
def test_max_range_pages_none_is_the_dev_plan_unchanged():
    for boxes, kw in (
        ([A4_PT] * 540, {"max_range_pixels": DEFAULT_MAX_RANGE_PIXELS}),
        ([A4_PT] * 540, {"dpi": 96, "max_range_pixels": 0}),
        ([A4_PT] * 4, {"dpi": 96, "max_range_pixels": 10}),
        ([LARGE_PT] * 37 + [A4_PT] * 3, {"max_range_pixels": 2e9}),
        ([], {}),
    ):
        assert plan_page_ranges(boxes, **kw) == plan_page_ranges(boxes, **kw, max_range_pages=None)


def _planner_before_the_page_cap(boxes, *, dpi=192, max_range_pixels=64_000_000, safety=1.1):
    """``plan_page_ranges`` as it was before ``max_range_pages`` existed,
    verbatim in its arithmetic (the DEV behaviour the new keyword must not
    touch). The OCR range test files that pin it skip on Windows."""
    total = len(boxes)
    if total <= 0:
        return []
    max_area = 0.0
    for width_pt, height_pt in boxes:
        max_area = max(max_area, (float(width_pt) / 72.0 * dpi) * (float(height_pt) / 72.0 * dpi))
    if max_range_pixels <= 0 or max_area <= 0 or safety <= 0:
        return [PageRange(0, total - 1)]
    length = max(1, int(float(max_range_pixels) // (max_area * float(safety))))
    return [PageRange(first, min(first + length - 1, total - 1)) for first in range(0, total, length)]


@pytest.mark.parametrize("pages", [0, 1, 3, 17, 540])
@pytest.mark.parametrize("sizes", [[A4_PT], [LARGE_PT], [A4_PT, LARGE_PT, (0.0, 0.0)], [(10.0, 10.0)]])
def test_the_default_planner_is_byte_identical_to_the_one_before_the_page_cap(pages, sizes):
    boxes = [sizes[i % len(sizes)] for i in range(pages)]
    for dpi in (96, 192, 300):
        for budget in (DEFAULT_MAX_RANGE_PIXELS, 0, -1, 10, 2e9, 20_000_000):
            for safety in (1.1, 0, 2.0):
                expected = _planner_before_the_page_cap(boxes, dpi=dpi, max_range_pixels=budget, safety=safety)
                assert plan_page_ranges(boxes, dpi=dpi, max_range_pixels=budget, safety=safety) == expected
                assert plan_page_ranges(
                    boxes, dpi=dpi, max_range_pixels=budget, safety=safety, max_range_pages=None) == expected
    assert plan_page_ranges(boxes) == _planner_before_the_page_cap(boxes)


def test_max_range_pages_caps_the_length_after_the_pixel_budget():
    a4 = plan_page_ranges([A4_PT] * 300, max_range_pixels=2e9)
    assert [r.count for r in a4] == [300]  # 510 pages fit the pixel budget
    capped = plan_page_ranges([A4_PT] * 300, max_range_pixels=2e9, max_range_pages=128)
    assert capped == [PageRange(0, 127), PageRange(128, 255), PageRange(256, 299)]
    large = plan_page_ranges([LARGE_PT] * 540, max_range_pixels=2e9, max_range_pages=128)
    assert {r.count for r in large[:-1]} == {74}  # the pixel budget binds first
    assert [r.count for r in plan_page_ranges([A4_PT] * 300, max_range_pixels=0, max_range_pages=100)] == [100] * 3
    with pytest.raises(ValueError, match="max_range_pages"):
        plan_page_ranges([A4_PT] * 3, max_range_pages=0)


# ---------------------------------------------------------------------------
# the cost model: design 6.4's worked examples
# ---------------------------------------------------------------------------
_EXAMPLE_OFFER = {"id": 1, "gpu_name": "RTX 3090", "dph_total": 0.45, "inet_down_cost": 0.01, "inet_up_cost": 0.01}


def test_worked_example_540_large_format_pages_is_130_dollars(root):
    cfg = _cfg(root)
    plan = plan_document([LARGE_PT] * 540, cfg)
    assert plan.n_ranges == 8 and plan.largest_range_pages == 74
    assert 32.5 < plan.need_gb < 33.5 and 41 < plan.min_cpu_ram_gb <= 42  # "offers with >= 42 GB RAM"
    est = estimate_offer(_EXAMPLE_OFFER, plan, cfg, document_bytes=300_000_000)
    assert est.compute_s == 4320 + 480 and est.transfer_s == 300
    assert est.ttl_s == 1500 + 300 + 7200 + 300 == 9300
    assert abs(est.down_gb - 13.3) < 1e-9
    assert round(est.worst_usd, 2) == 1.30


def test_worked_example_300_a4_pages_is_85_cents(root):
    cfg = _cfg(root)
    plan = plan_document([A4_PT] * 300, cfg)
    assert [r.count for r in plan.ranges] == [128, 128, 44]
    est = estimate_offer(_EXAMPLE_OFFER, plan, cfg, document_bytes=120_000_000)
    assert est.compute_s == 2400 + 180 and est.ttl_s == 1500 + 120 + 3870 + 300 == 5790
    assert round(est.worst_usd, 2) == 0.85


# ---------------------------------------------------------------------------
# the search, the client-side re-check, the ranking
# ---------------------------------------------------------------------------
def test_the_offer_query_carries_every_host_requirement(root):
    cfg = _cfg(root, egress={"allow_geolocations": ["se", "NO"]})
    q = offer_query(cfg, min_cpu_ram_gb=41.3)
    assert q["datacenter"] == {"eq": True} and q["verified"] == {"eq": True}
    assert q["geolocation"] == {"in": ["NO", "SE"]}
    assert q["cpu_ram"] == {"gte": math.ceil(41.3 * 1024)} and q["cuda_max_good"] == {"gte": 13.0}
    assert q["inet_down_cost"] == q["inet_up_cost"] == {"lte": 0.02}
    assert q["reliability"] == {"gte": 0.97} and q["dph_total"] == {"lte": 0.45}
    assert q["disk_space"] == {"gte": 32} and q["allocated_storage"] == 32.0
    assert q["type"] == "ondemand" and q["rentable"] == {"eq": True}
    relaxed = offer_query(_cfg(root, egress={"require_datacenter": False, "require_verified": False}),
                          min_cpu_ram_gb=20)
    assert "datacenter" not in relaxed and "verified" not in relaxed and "geolocation" not in relaxed


def _plan(cfg, pages=100):
    return plan_document([A4_PT] * pages, cfg)


def test_a_server_that_ignores_filters_never_routes_to_a_home_or_unverified_host(root):
    offers = default_offers() + [
        make_offer(16, dph_base=0.10, cpu_ram=16 * 1024),  # below the RAM floor
        make_offer(17, dph_base=0.10, cuda_max_good=12.4),  # driver too old for the image
        make_offer(18, dph_base=0.10, inet_down_cost=0.5),  # bandwidth too dear
        make_offer(19, dph_base=0.10, host_id=None),  # cannot be recorded
        make_offer(20, dph_base=0.10, verification="deverified", verified=True),
    ]
    fake = FakeVast(offers, ignore_filters=True)
    cfg = _cfg(root)
    plan = _plan(cfg)
    priced = price_job(_client(root, fake), cfg, plan, document_bytes=50_000_000)
    assert [e.offer_id for e in priced.offers] == [11, 12]
    rejected = dict(priced.rejected)
    assert any("datacenter" in r for r in rejected[14]) and any("verified" in r for r in rejected[15])
    assert any("RAM floor" in r for r in rejected[16]) and any("cuda_max_good" in r for r in rejected[17])
    assert any("inet_down_cost" in r for r in rejected[18]) and any("host_id" in r for r in rejected[19])
    assert any("verified" in r for r in rejected[20]) and any("tier" in r for r in rejected[13])
    assert fake.verbs("PUT") == []
    # only bad hosts left: refused, nothing rented
    fake.offers = [o for o in offers if o["id"] not in (11, 12)]
    with pytest.raises(VastPlanRefused) as info:
        price_job(_client(root, fake), cfg, plan, document_bytes=50_000_000)
    assert info.value.reason_code == "no-offer" and fake.verbs("PUT") == []


def test_the_filters_are_also_applied_server_side(root):
    fake = FakeVast()
    cfg = _cfg(root)
    priced = price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1_000_000)
    assert [e.offer_id for e in priced.offers] == [11, 12] and priced.searched == 2
    assert fake.search_bodies()[0]["datacenter"] == {"eq": True}


def test_geolocation_allowlist_is_rechecked_client_side(root):
    offers = [make_offer(11), make_offer(12, geolocation="Texas, US")]
    cfg = _cfg(root, egress={"allow_geolocations": ["US"]})
    priced = price_job(_client(root, FakeVast(offers, ignore_filters=True)), cfg, _plan(cfg), document_bytes=1)
    assert [e.offer_id for e in priced.offers] == [12]


def test_offers_are_ranked_by_pages_per_dollar(root):
    offers = [make_offer(11, dph_base=0.30), make_offer(12, dph_base=0.40, gpu_name="RTX 5080"),
              make_offer(13, dph_base=0.25)]
    cfg = _cfg(root)
    priced = price_job(_client(root, FakeVast(offers)), cfg, _plan(cfg), document_bytes=1_000_000)
    assert [e.offer_id for e in priced.offers] == [13, 11, 12]  # every factor 1.0: cheapest first
    fast = _cfg(root, vastai={"ocr_gpu_factors": {"RTX 5080": 2.0}})
    priced = price_job(_client(root, FakeVast(offers)), fast, _plan(fast, 700), document_bytes=1_000_000)
    assert priced.offers[0].offer_id == 12 and priced.offers[0].factor == 2.0  # dearer per hour, cheaper per page


def test_an_excluded_machine_is_not_offered_again(root):
    cfg = _cfg(root)
    priced = price_job(_client(root, FakeVast()), cfg, _plan(cfg), document_bytes=1, exclude_machine_ids=[9011])
    assert [e.offer_id for e in priced.offers] == [12]


# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------
def test_the_job_cap_refuses_before_anything_is_created(root):
    fake = FakeVast()
    cfg = _cfg(root, vastai={"max_job_usd": 0.05})
    with pytest.raises(VastSpendRefused) as info:
        price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1_000_000)
    assert info.value.reason_code == "cap-job" and "per-job cap $0.05" in info.value.message
    assert "Nothing was rented" in info.value.message and fake.verbs("PUT") == []


def test_the_job_cap_is_the_smaller_of_config_and_approval(root):
    cfg = _cfg(root)
    client = _client(root, FakeVast())
    assert price_job(client, cfg, _plan(cfg), document_bytes=1).job_cap_usd == 3.0
    with pytest.raises(VastSpendRefused, match="per-job cap \\$0.10") as info:
        price_job(client, cfg, _plan(cfg), document_bytes=1, approval_max_job_usd=0.10)
    assert info.value.reason_code == "cap-job"


def test_bandwidth_dollars_are_part_of_the_worst_case(root):
    cfg = _cfg(root)
    plan = _plan(cfg)
    free = estimate_offer(make_offer(1, inet_down_cost=0.0, inet_up_cost=0.0), plan, cfg, document_bytes=500_000_000)
    paid = estimate_offer(make_offer(1, inet_down_cost=0.02, inet_up_cost=0.02), plan, cfg, document_bytes=500_000_000)
    assert abs((paid.worst_usd - free.worst_usd) - (13.5 * 0.02 + plan.pages * 8000 / 1e9 * 0.02)) < 1e-9


def test_the_run_cap_counts_what_this_worker_run_spent(root):
    fake = FakeVast()
    cfg = _cfg(root)
    with pytest.raises(VastSpendRefused) as info:
        price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1, spend=SpendView(run_usd=9.9))
    assert info.value.reason_code == "cap-run"
    with pytest.raises(VastSpendRefused) as info:
        check_envelopes(cfg, SpendView(run_usd=10.0))
    assert info.value.reason_code == "cap-run"
    calls = len(fake.calls)
    with pytest.raises(VastSpendRefused):
        price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1, spend=SpendView(run_usd=10.0))
    assert len(fake.calls) == calls  # an envelope already spent refuses before the search


def test_the_approval_envelope_counts_unsettled_leases_at_their_worst_case(root, isolated_state):  # noqa: F811
    ledger = Ledger(isolated_state)
    base = {"sha256": SHA, "bytes": 1, "host_id": 1, "datacenter": True, "verified": True}
    ledger.append("intent", lease_id="VOCR-a", approval_nonce="N1", worker_run_id="R0", worst_usd=20.0, **base)
    ledger.append("outcome", lease_id="VOCR-a", instance_id=1, end="t", estimated_cost_usd=2.0, result="published")
    ledger.append("intent", lease_id="VOCR-b", approval_nonce="N1", worker_run_id="R0", worst_usd=22.9, **base)
    spend = spend_from_ledger(ledger, approval_nonce="N1", worker_run_id="R1")
    assert spend.approval_usd == pytest.approx(24.9) and spend.run_usd == 0.0 and spend.unsettled == ("VOCR-b",)
    cfg = _cfg(root)
    with pytest.raises(VastSpendRefused) as info:
        price_job(_client(root, FakeVast()), cfg, _plan(cfg), document_bytes=1, spend=spend,
                  approval_max_total_usd=25.0)
    assert info.value.reason_code == "cap-approval"
    with pytest.raises(VastSpendRefused) as info:
        check_envelopes(cfg, SpendView(approval_usd=25.0), approval_max_total_usd=40.0)  # config caps it at 25
    assert info.value.reason_code == "cap-approval"


def test_the_ttl_cap_refuses_an_oversized_document(root):
    cfg = _cfg(root)
    with pytest.raises(VastSpendRefused) as info:
        price_job(_client(root, FakeVast()), cfg, plan_document([A4_PT] * 2000, cfg), document_bytes=1)
    assert info.value.reason_code == "cap-ttl" and "ttl_cap_s is 14400" in info.value.message


def test_account_credit_below_the_worst_case_refuses(root):
    fake = FakeVast()
    cfg = _cfg(root)
    priced = price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1)
    assert priced.credit_usd is None and any("credit" in n for n in priced.notes)
    fake.credit = 0.05
    with pytest.raises(VastSpendRefused) as info:
        price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1)
    assert info.value.reason_code == "credit-low"


def test_api_failures_and_a_missing_key_are_plan_refusals(root):
    fake = FakeVast()
    fake.search_status = 503
    cfg = _cfg(root)
    with pytest.raises(VastPlanRefused) as info:
        price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1)
    assert info.value.reason_code == "api-error" and not info.value.disable_executor
    (root / "keys" / "vastai.key").unlink()
    with pytest.raises(VastPlanRefused) as info:
        price_job(_client(root, FakeVast()), cfg, _plan(cfg), document_bytes=1)
    assert info.value.reason_code == "key-missing" and info.value.disable_executor
    assert FAKE_KEY not in info.value.message


def test_the_high_tier_needs_its_approval_before_any_search(root):
    fake = FakeVast()
    cfg = _cfg(root, vastai={"tier": "high"})
    with pytest.raises(VastSpendRefused) as info:
        price_job(_client(root, fake), cfg, _plan(cfg), document_bytes=1)
    assert info.value.reason_code == "approval-missing" and fake.calls == []
    assert any("approve-high" in a for a in info.value.next_actions)


def test_an_approved_high_tier_rents_high_tier_cards_within_the_approvals_job_cap(root, monkeypatch):
    import io
    import re

    from trialerror.vastai import guard

    cfg = _cfg(root, vastai={"tier": "high"})
    monkeypatch.setattr(guard, "_is_interactive", lambda: True)
    out = io.StringIO()
    guard.mint_high_tier_approval(
        root, cfg.api_key_path, hours=2, max_job_usd=2.0, out=out, path=cfg.high_tier_approval_path,
        input_fn=lambda _p: re.search(r"Type (\w+) to confirm", out.getvalue()).group(1),
    )
    priced = price_job(_client(root, FakeVast()), cfg, _plan(cfg), document_bytes=1)
    assert [e.offer_id for e in priced.offers] == [13] and priced.job_cap_usd == 2.0
    assert any("high tier approved" in n for n in priced.notes)
