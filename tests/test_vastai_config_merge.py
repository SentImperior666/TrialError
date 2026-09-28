"""vast.ai round 3 (C-3, rows 75-78 and 94): one ``[vastai]`` table, two lanes.

The embedding lane (the public TrialError copy's embedding backend) reads
``[vastai]`` with its own loader, ``trialerror.vastai.tiers.load_vast_config``;
the OCR lane reads it with ``trialerror.vastai.config.load_vast_config``. The
decision (te-vastai, under D1): ours never refuses a toml that is valid for the
embedding lane --

* every public key is accepted with its public default and meaning, the
  embedding lane's own keys included (``cfg.embed``);
* a ``ttl_cap_s`` above 4 h is clamped with the public note, not refused;
* the four keys the lanes share with different defaults (``image``,
  ``startup_s``, ``safety``, ``disk_gb``) take the public defaults in
  ``[vastai]``, and the OCR lane reads its own under ``[vastai.ocr]`` with
  today's OCR defaults, so its behaviour does not change;
* unknown keys are still refused by name;
* the ``program init`` template's ``[vastai]`` block loads under both.

No network (the tripwire fails any real call), no GPU, no real key.
"""

from __future__ import annotations

import dataclasses
import tomllib

import pytest

from tests._vastai_fakes import (
    dev_toml,
    isolated_state,  # noqa: F401 - fixture
    network_tripwire,  # noqa: F401 - fixture
    write_key,
)
from trialerror.vastai import tiers
from trialerror.vastai.config import (
    CONFIG_KEYS,
    EMBED_KEYS,
    EmbedSettings,
    load_vast_config,
)
from trialerror.vastai.errors import VastConfigError
from trialerror.vastai.pricing import offer_query

OCR_IMAGE = "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime"
EMBED_IMAGE = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state):  # noqa: F811 - the fixtures above
    yield


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "devroot"
    write_key(r / "keys")
    return r


def _ours(raw, root):
    return load_vast_config(raw, config_root=root)


def _public(raw, root):
    return tiers.load_vast_config(raw, root)


# ---------------------------------------------------------------------------
# every public key, with its public default and meaning
# ---------------------------------------------------------------------------
def test_the_embedding_keys_default_to_the_public_loaders_defaults(root):
    raw = {"vastai": {"api_key_path": "keys/vastai.key"}}
    embed, public = _ours(raw, root).embed, _public(raw, root)
    assert embed == EmbedSettings()
    for name in ("image", "startup_s", "safety", "disk_gb", "batch_size", "max_jobs_per_run", "pip_packages"):
        assert getattr(embed, name) == getattr(public, name), name
    assert (embed.image, embed.startup_s, embed.safety, embed.disk_gb) == (EMBED_IMAGE, 1200, 2.0, 40)
    assert (embed.batch_size, embed.max_jobs_per_run) == (64, 50)
    assert embed.pip_packages == ("sentence-transformers>=5.0", "transformers>=4.51")
    assert embed.module_dir is None and embed.gpu_factor_overrides == {}


def test_config_keys_carry_the_public_defaults_of_the_embedding_keys():
    public_defaults = {f.name: f.default for f in dataclasses.fields(tiers.VastConfig)
                       if f.default is not dataclasses.MISSING}
    by_name = {(k.table, k.key): k for k in CONFIG_KEYS}
    assert EMBED_KEYS <= set(by_name)
    for table, key in EMBED_KEYS:
        if (table, key) in (("vastai", "module_dir"), ("vastai.gpu_factors", "<GPU name>")):
            continue  # no scalar default: unset / the embedding lane's built-in table
        default = by_name[(table, key)].default
        expected = public_defaults[key]
        assert (tuple(default) if isinstance(default, list) else default) == expected, key
    # the keys the lanes share with the same default keep it
    for key in ("tier", "max_job_usd", "ttl_cap_s", "grace_s", "poll_interval_s"):
        assert by_name[("vastai", key)].default == public_defaults[key], key


def test_every_public_key_set_is_accepted_and_read_as_the_public_loader_reads_it(root):
    vast = {
        "api_key_path": "keys/vastai.key",
        "ssh_identity_path": "keys/vastai_ed25519",
        "tier": "low",
        "max_job_usd": 0.01,
        "ttl_cap_s": 7200,
        "startup_s": 0.3,
        "safety": 1.0,
        "grace_s": 0,
        "poll_interval_s": 0,
        "image": "pytorch/pytorch:other-tag",
        "disk_gb": 50,
        "batch_size": 16,
        "max_jobs_per_run": 5,
        "pip_packages": ["sentence-transformers>=5.1"],
        "module_dir": "tools/embeddings_local",
        "gpu_factors": {"RTX 5090": 3.0, "rtx 4090": 2.0},
        "tiers": {"low": {"gpus": ["RTX 4060 Ti"], "min_vram_gb": 16, "max_dph": 0.2, "min_reliability": 0.9}},
    }
    raw = {"vastai": vast}
    ours, public = _ours(raw, root), _public(raw, root)
    embed = ours.embed
    for name in ("image", "startup_s", "safety", "disk_gb", "batch_size", "max_jobs_per_run", "pip_packages"):
        assert getattr(embed, name) == getattr(public, name), name
    for name, factor in embed.gpu_factor_overrides.items():
        assert public.gpu_factors[name] == factor
    assert embed.module_dir == "tools/embeddings_local"
    for name in ("tier", "max_job_usd", "ttl_cap_s", "grace_s", "poll_interval_s"):
        assert getattr(ours, name) == getattr(public, name), name
    assert ours.tiers["low"].max_dph == public.tiers["low"].max_dph == 0.2
    # ... and none of the embedding lane's values reaches the OCR lane
    assert (ours.image, ours.startup_s, ours.safety, ours.disk_gb) == (OCR_IMAGE, 1500, 1.5, 32)


@pytest.mark.parametrize("vast", [
    {"api_key_path": "keys/vastai.key", "poll_interval_s": 0, "startup_s": 0.3, "safety": 1.0, "grace_s": 0},
    {"api_key_path": "keys/vastai.key", "poll_interval_s": 0, "max_job_usd": 0.01},
])
def test_the_public_tests_tomls_load_under_ours(root, vast):
    """The ``[vastai]`` tables the public tests/test_vastai.py writes."""
    raw = {"vastai": vast}
    assert _ours(raw, root).embed.startup_s == _public(raw, root).startup_s


# ---------------------------------------------------------------------------
# ttl_cap_s above 4 h: clamped with a note, as the public loader does
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cap", [14401, 99999, float("inf")])
def test_a_ttl_cap_above_four_hours_is_clamped_with_the_public_note(root, cap):
    raw = {"vastai": {"ttl_cap_s": cap}}
    ours, public = _ours(raw, root), _public(raw, root)
    assert ours.ttl_cap_s == public.ttl_cap_s == 14400
    assert list(ours.notes) == list(public.notes) == [f"ttl_cap_s {float(cap):.0f} clamped to the absolute cap 14400"]
    assert ours.status.endswith(f"; note: {ours.notes[0]}")


def test_a_ttl_cap_at_or_below_four_hours_carries_no_note(root):
    for cap in (14400, 3600):
        cfg = _ours({"vastai": {"ttl_cap_s": cap}}, root)
        assert cfg.ttl_cap_s == cap and cfg.notes == ()
    assert "note" not in _ours(dev_toml(), root).status


# ---------------------------------------------------------------------------
# the OCR lane reads image / startup_s / safety / disk_gb under [vastai.ocr]
# ---------------------------------------------------------------------------
def test_the_ocr_lane_keeps_its_defaults_whatever_the_embedding_lane_sets(root):
    cfg = _ours(dev_toml(vastai={"image": "embed-only", "startup_s": 60, "safety": 3.0, "disk_gb": 99}), root)
    assert (cfg.image, cfg.startup_s, cfg.safety, cfg.disk_gb) == (OCR_IMAGE, 1500, 1.5, 32)
    assert (cfg.ocr.image, cfg.ocr.startup_s, cfg.ocr.safety, cfg.ocr.disk_gb) == (OCR_IMAGE, 1500, 1.5, 32)
    assert (cfg.embed.image, cfg.embed.startup_s, cfg.embed.safety, cfg.embed.disk_gb) == ("embed-only", 60, 3.0, 99)
    query = offer_query(cfg, min_cpu_ram_gb=16)
    assert query["disk_space"] == {"gte": 32} and query["allocated_storage"] == 32.0


def test_the_ocr_lane_reads_its_own_values_under_vastai_ocr(root):
    cfg = _ours(dev_toml(ocr={"image": "ocr-image:tag", "startup_s": 900, "safety": 1.2, "disk_gb": 48}), root)
    assert (cfg.image, cfg.startup_s, cfg.safety, cfg.disk_gb) == ("ocr-image:tag", 900, 1.2, 48)
    assert cfg.embed == EmbedSettings()
    assert offer_query(cfg, min_cpu_ram_gb=16)["disk_space"] == {"gte": 48}


@pytest.mark.parametrize("key, bad, words", [
    ("safety", 0.5, "safety = 0.5: must be >= 1"),
    ("startup_s", -1, "startup_s = -1: must be >= 0"),
    ("disk_gb", 0, "disk_gb = 0: must be >= 1"),
    ("disk_gb", 20.5, "disk_gb = 20.5: must be a whole number"),
    ("image", "", "image = '': must be a non-empty string"),
])
def test_the_ocr_values_keep_their_validation_under_vastai_ocr(root, key, bad, words):
    with pytest.raises(VastConfigError, match=rf"\[vastai\.ocr\] {words}"):
        _ours(dev_toml(ocr={key: bad}), root)


# ---------------------------------------------------------------------------
# unknown keys are still refused by name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kw, words", [
    ({"vastai": {"batch_sise": 8}}, r"\[vastai\] batch_sise: not a vast.ai OCR setting"),
    ({"vastai": {"module_directory": "x"}}, r"\[vastai\] module_directory: not a vast.ai OCR setting"),
    ({"ocr": {"batch_size": 8}}, r"\[vastai.ocr\] batch_size: not a vast.ai OCR setting"),
    ({"ocr": {"image_tag": "x"}}, r"\[vastai.ocr\] image_tag: not a vast.ai OCR setting"),
])
def test_unknown_keys_are_still_refused_by_name(root, kw, words):
    with pytest.raises(VastConfigError, match=words) as info:
        _ours(dev_toml(**kw), root)
    assert info.value.next_actions


@pytest.mark.parametrize("vast, words", [
    ({"batch_size": "many"}, r"\[vastai\] batch_size = 'many': must be a whole number"),
    ({"pip_packages": "sentence-transformers"}, r"\[vastai\] pip_packages = .*must be a list"),
    ({"gpu_factors": 2.0}, r"\[vastai.gpu_factors\] must be a table"),
    ({"gpu_factors": {"RTX 5090": "fast"}}, r"\[vastai.gpu_factors\] RTX 5090 = 'fast': must be a number"),
    ({"module_dir": 5}, r"\[vastai\] module_dir = 5: must be a directory path string"),
])
def test_an_embedding_value_the_embedding_lane_cannot_read_is_refused_by_name(root, vast, words):
    with pytest.raises(VastConfigError, match=words):
        _ours({"vastai": vast}, root)


# ---------------------------------------------------------------------------
# the program init template's [vastai] block loads under both loaders
# ---------------------------------------------------------------------------
def _template_vastai_block() -> dict:
    from trialerror.cli.program import _TRIALERROR_TOML_TEMPLATE

    lines = _TRIALERROR_TOML_TEMPLATE.splitlines()
    start = lines.index("# [vastai]")
    body = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        body.append(line[1:].lstrip())
    return tomllib.loads("\n".join(body))


def test_the_program_template_vastai_block_is_valid_for_both_loaders(root):
    raw = _template_vastai_block()
    assert set(raw) == {"vastai"} and set(raw["vastai"]["tiers"]) == {"low", "mid", "high"}
    ours, public = _ours(raw, root), _public(raw, root)
    assert ours.tier == public.tier == "mid" and ours.ttl_cap_s == public.ttl_cap_s == 14400
    assert ours.embed.startup_s == public.startup_s == 1200
    for name in ("low", "mid", "high"):
        assert ours.tiers[name] == tiers.DEFAULT_TIERS[name], name
    assert ours.api_key_path == root / "keys" / "vastai.key"
