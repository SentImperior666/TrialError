"""Round 4: the custodian's decisions (d) and (e), as tests.

(e) ``vastai_live_instances`` keeps the public result: another root's
TrialError-labelled instance past its deadline, or a malformed TrialError
label, fails the check with ``trialerror vastai reap`` as the next action;
another root's instance within its deadline, and an instance that is not
TrialError's, only warn. The doctor never touches any of them.

(d) One function decides where ``approve-high`` writes the high-tier approval
(beside the key file) and one decides what every reader reads (beside the key
file, falling back to ``<root>/keys/`` when only that one exists).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

import trialerror.cli.vastai as cli_vastai
from trialerror.cli import build_parser
from trialerror.util.config import load_config
from trialerror.util.doctor import DoctorContext
from trialerror.vastai import checks as vchecks
from trialerror.vastai import guard, runner
from trialerror.vastai.api import VastClient
from trialerror.vastai.config import load_vast_config
from trialerror.vastai.guard import program_fingerprint
from trialerror.vastai.lease import make_label
from trialerror.vastai.pricing import price_job
from tests._vastai_fakes import (  # noqa: F401 - fixtures
    FakeVast,
    dev_toml,
    isolated_state,
    network_tripwire,
    toml_text,
    write_key,
)

#: a deadline long past, and one far ahead
_PAST, _AHEAD = 1, 4_000_000_000


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state):  # noqa: F811
    yield


def _root(tmp_path, **toml_kwargs):
    root = tmp_path / "devroot"
    write_key(root / "keys")
    (root / "trialerror.toml").write_text(toml_text(dev_toml(**toml_kwargs)), encoding="utf-8")
    return root


def _live(monkeypatch, root, instances):
    fake = FakeVast()
    fake.instances = dict(instances)
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=fake.http))
    result = vchecks.check_vastai_live_instances(DoctorContext(program_root=root))
    assert fake.verbs("DELETE") == [] and set(fake.instances) == set(instances), "the doctor never touches one"
    return result


# ---------------------------------------------------------------------------
# (e) vastai_live_instances keeps the public result
# ---------------------------------------------------------------------------
def test_e_another_roots_instance_past_its_deadline_fails_naming_reap(tmp_path, monkeypatch):
    root = _root(tmp_path)
    result = _live(monkeypatch, root, {8: {"id": 8, "label": make_label("f" * 64, "VAST-embed", _PAST)}})
    assert result.status == "fail", result.message
    assert "another root" in result.message
    assert result.message.endswith("run `trialerror vastai reap` and check the vast.ai console"), result.message
    assert [e["instance_id"] for e in result.details["other_roots_overdue_or_malformed"]] == [8]


def test_e_a_malformed_trialerror_label_fails_naming_reap(tmp_path, monkeypatch):
    root = _root(tmp_path)
    result = _live(monkeypatch, root, {5: {"id": 5, "label": "trialerror|garbled"}})
    assert result.status == "fail", result.message
    assert "malformed" in result.message and "run `trialerror vastai reap`" in result.message


def test_e_this_roots_overdue_ocr_lease_keeps_the_reap_ocr_text(tmp_path, monkeypatch):
    root = _root(tmp_path)
    fp = program_fingerprint(root)
    result = _live(monkeypatch, root, {
        7: {"id": 7, "label": make_label(fp, "VOCR-late", _PAST)},
        8: {"id": 8, "label": make_label("f" * 64, "VAST-embed", _PAST)},
    })
    assert result.status == "fail"
    assert "1 vast.ai instance(s) of this root" in result.message
    assert "(an OCR lease: `trialerror vastai reap --ocr`)" in result.message and "another root" in result.message


def test_e_another_roots_instance_within_its_deadline_only_warns(tmp_path, monkeypatch):
    root = _root(tmp_path)
    result = _live(monkeypatch, root, {10: {"id": 10, "label": make_label("e" * 64, "VAST-live", _AHEAD)}})
    assert result.status == "warn" and "another TrialError root" in result.message, result.message
    assert [e["instance_id"] for e in result.details["other_roots"]] == [10]


def test_e_an_instance_that_is_not_trialerrors_only_warns(tmp_path, monkeypatch):
    root = _root(tmp_path)
    result = _live(monkeypatch, root, {4: {"id": 4, "label": "someone-else's instance"}})
    assert result.status == "warn" and "not TrialError's" in result.message, result.message


# ---------------------------------------------------------------------------
# (d) one resolution for the high-tier approval: the writer, and every reader
# ---------------------------------------------------------------------------
_DOC_SHA = "cd" * 32
_APPROVAL = "vastai-high-tier.approval"


def _moved_key_root(tmp_path):
    """A root on the high tier whose FAKE key lies outside ``keys/`` (in ``vault/``)."""
    root = tmp_path / "devroot"
    write_key(root / "vault")
    raw = dev_toml(vastai={"api_key_path": "vault/vastai.key", "tier": "high"},
                   egress={"allow_documents": [_DOC_SHA]})
    (root / "trialerror.toml").write_text(toml_text(raw), encoding="utf-8")
    return root


def _cfg(root):
    return load_vast_config(load_config(root / "trialerror.toml").raw, config_root=root)


def _mint(monkeypatch, root, key, path=None):
    out = io.StringIO()
    monkeypatch.setattr(guard, "_is_interactive", lambda: True)

    def typed(_prompt=""):
        return re.findall(r"Type ([0-9a-f]+) to confirm", out.getvalue())[-1]

    return guard.mint_high_tier_approval(root, key, hours=2, max_job_usd=2.0, input_fn=typed, out=out, path=path)


def _runner_reads(root, cfg):
    """The embedding runner's reader, kept verbatim: it passes no path."""
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "guard.verify_high_tier_approval(program_root, cfg.api_key_path)" in source
    return guard.verify_high_tier_approval(root, cfg.api_key_path)["max_job_usd"]


def _pricing_reads(cfg) -> str:
    """The OCR pricing's reader: ``price_job`` refuses on the approval before it
    touches the client or the plan; past the approval it meets these stubs."""

    class _Past(Exception):
        pass

    class _Stub:
        def __getattr__(self, name):
            raise _Past(name)

    try:
        price_job(_Stub(), cfg, _Stub(), document_bytes=1)
    except _Past:
        return "found"
    except Exception as exc:  # noqa: BLE001 - the refusal names what is missing
        return f"{type(exc).__name__}: {exc}"
    return "found"


def _doctor_reads(root, expected: Path) -> bool:
    result = vchecks.check_vastai_high_tier(DoctorContext(program_root=root))
    tail = str(Path(expected.parent.name) / expected.name)
    return result.status == "warn" and "approval_expires" in result.details and f"{tail} (expires" in result.message


def test_d_approve_high_writes_beside_a_moved_key_and_every_reader_finds_it(tmp_path, monkeypatch):
    root = _moved_key_root(tmp_path)
    out = io.StringIO()
    monkeypatch.setattr(cli_vastai, "_out", out)
    monkeypatch.setattr(guard, "_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli_vastai, "_input", lambda _p="": re.findall(r"Type ([0-9a-f]+) to confirm", out.getvalue())[-1]
    )
    args = build_parser().parse_args(
        ["vastai", "approve-high", "--backend-config-root", str(root), "--hours", "4", "--max-job-usd", "5"]
    )
    env = args.handler(args)
    assert env["ok"] is True, env
    beside = root / "vault" / _APPROVAL
    assert sorted(tmp_path.rglob(_APPROVAL)) == [beside]  # beside the key, and nowhere else
    cfg = _cfg(root)
    assert _runner_reads(root, cfg) == 5.0
    assert _pricing_reads(cfg) == "found"
    assert _doctor_reads(root, beside)


def test_d_a_legacy_approval_under_keys_is_found_by_every_reader(tmp_path, monkeypatch):
    root = _moved_key_root(tmp_path)
    cfg = _cfg(root)
    legacy = _mint(monkeypatch, root, cfg.api_key_path, path=root / "keys" / _APPROVAL)
    assert legacy == guard.approval_path(root) and not (root / "vault" / _APPROVAL).exists()
    assert _runner_reads(root, cfg) == 2.0
    assert _pricing_reads(cfg) == "found"
    assert _doctor_reads(root, legacy)


def test_d_with_the_default_key_location_both_resolutions_are_the_public_path(tmp_path, monkeypatch):
    root = _root(tmp_path, egress={"allow_documents": [_DOC_SHA]})
    key = root / "keys" / "vastai.key"
    public = guard.approval_path(root)
    assert guard.approval_write_path(root, key) == public and guard.approval_read_path(root, key) == public
    assert guard.approval_write_path(root, None) == public
    assert _cfg(root).high_tier_approval_path.resolve() == public.resolve()
    assert _mint(monkeypatch, root, key) == public and guard.approval_read_path(root, key) == public
