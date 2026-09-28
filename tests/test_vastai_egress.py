"""vast.ai OCR executor, lane L1: the egress policy and the sealed approval
(design section 9.2-9.3). Pure decisions on a manifest: nothing here talks to
vast.ai, and the tripwire fails any test that tries.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests._vastai_fakes import (
    FAKE_KEY,
    dev_toml,
    isolated_state,  # noqa: F401 - fixture
    network_tripwire,  # noqa: F401 - fixture
    write_key,
)
from trialerror.vastai.config import load_vast_config
from trialerror.vastai.egress import (
    EGRESS_PURPOSE,
    NOTHING_SENT,
    approval_body,
    decide_egress,
    egress_digest,
    read_egress_approval,
    sign_egress_approval,
    verify_egress_approval,
    write_egress_approval,
)
from trialerror.vastai.errors import EgressRefused, VastConfigError, VastError
from trialerror.vastai.guard import mac
from trialerror.vastai.ledger import Ledger

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
SHA1 = "1" * 64
SHA2 = "2" * 64


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state):  # noqa: F811
    yield


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "devroot"
    write_key(r / "keys")
    return r


def _cfg(root, executor="vastai", **egress):
    return load_vast_config(dev_toml(executor=executor, egress=egress), config_root=root)


def _manifest(sha=SHA1, size=1_000_000, tier="open", job_id="JOB-egress-1", doc_id="DOC-1"):
    expect = {"stage": "ocr", "backend": None, "version": None, "outputs": ["pages.json"],
              "input_name": "input.pdf", "page_count": None}
    if tier is not None:
        expect["license_tier"] = tier
    return {"schema": 1, "job_id": job_id, "stage": "ocr", "doc_id": doc_id, "config_hash": "h",
            "inputs": [{"name": "input.pdf", "sha256": sha, "bytes": size}], "expect": expect}


def _approve(cfg, now=NOW, **kw):
    return sign_egress_approval(cfg, now=now, **kw)


def test_the_default_config_lets_nothing_leave(root):
    cfg = load_vast_config({"program": {"id": "x"}}, config_root=root)
    with pytest.raises(VastConfigError, match="nothing may leave DEV"):
        decide_egress(cfg, _manifest(), approval=None, now=NOW)
    with pytest.raises(VastConfigError):
        decide_egress(_cfg(root, executor="local", allow_documents=[SHA1]), _manifest(), approval=None, now=NOW)


def test_no_approval_refuses_by_name_and_says_nothing_was_sent(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    decision = decide_egress(cfg, _manifest(), approval=None, now=NOW)
    assert not decision.allowed and decision.reason_code == "approval-missing"
    assert decision.message.endswith(NOTHING_SENT)
    assert any("approve-ocr" in a for a in decision.next_actions)
    with pytest.raises(EgressRefused) as info:
        decision.raise_if_refused()
    assert info.value.reason_code == "approval-missing" and info.value.details["sha256"] == SHA1


def test_a_named_document_leaves_under_a_minted_approval(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    body = _approve(cfg, max_job_usd=2.0, max_total_usd=20.0)
    decision = decide_egress(cfg, _manifest(tier=None), approval=body, now=NOW + timedelta(hours=1))
    assert decision.allowed and decision.basis == "sha256" and decision.reason_code is None
    assert decision.approval_nonce == body["nonce"]
    assert (decision.approval_max_job_usd, decision.approval_max_total_usd) == (2.0, 20.0)
    assert "nothing has been sent yet" in decision.message


def test_a_named_tier_leaves_and_an_unnamed_one_does_not(root):
    cfg = _cfg(root, allow_license_tiers=["open"])
    body = _approve(cfg)
    assert decide_egress(cfg, _manifest(tier="open"), approval=body, now=NOW).basis == "tier"
    decision = decide_egress(cfg, _manifest(tier="commercial_restricted"), approval=body, now=NOW)
    assert decision.reason_code == "tier-not-allowed" and NOTHING_SENT in decision.message
    assert any(SHA1 in a for a in decision.next_actions)


def test_a_marker_without_a_tier_passes_only_by_sha256(root):
    cfg = _cfg(root, allow_license_tiers=["open"])
    decision = decide_egress(cfg, _manifest(tier=None), approval=_approve(cfg), now=NOW)
    assert decision.reason_code == "tier-missing"
    cfg = _cfg(root, allow_license_tiers=["open"], allow_documents=[SHA1])
    assert decide_egress(cfg, _manifest(tier=None), approval=_approve(cfg), now=NOW).allowed


def test_an_unknown_tier_document_leaves_only_when_its_sha256_is_named(root):
    cfg = _cfg(root, allow_license_tiers=["open", "academic_oa"])
    decision = decide_egress(cfg, _manifest(tier="unknown"), approval=_approve(cfg), now=NOW)
    assert decision.reason_code == "tier-not-allowed"
    cfg = _cfg(root, allow_documents=[SHA1])
    decision = decide_egress(cfg, _manifest(tier="unknown"), approval=_approve(cfg), now=NOW)
    assert decision.allowed and decision.basis == "sha256"


def test_hand_written_and_edited_approvals_are_refused(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    forged = {**approval_body(cfg, issued=NOW), "mac": "0" * 64}
    assert decide_egress(cfg, _manifest(), approval=forged, now=NOW).reason_code == "approval-invalid"
    edited = _approve(cfg)
    edited["max_total_usd"] = 500.0  # an agent raising the envelope
    decision = decide_egress(cfg, _manifest(), approval=edited, now=NOW)
    assert decision.reason_code == "approval-invalid" and "invalid signature" in decision.message


def test_an_edited_egress_table_unseals_the_approval(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    body = _approve(cfg)
    wider = _cfg(root, allow_documents=[SHA1, SHA2])
    decision = decide_egress(wider, _manifest(sha=SHA2), approval=body, now=NOW)
    assert decision.reason_code == "approval-unsealed"
    assert "changed since it was approved" in decision.message and NOTHING_SENT in decision.message
    # re-ordering or writing a default out explicitly is not a change of meaning
    same = _cfg(root, allow_documents=[SHA1.upper()], require_datacenter=True, remote_scratch="shm")
    assert egress_digest(same) == egress_digest(cfg)
    assert decide_egress(same, _manifest(), approval=body, now=NOW).allowed
    # a host requirement relaxed after sealing is a change
    relaxed = _cfg(root, allow_documents=[SHA1], require_verified=False)
    assert decide_egress(relaxed, _manifest(), approval=body, now=NOW).reason_code == "approval-unsealed"


def test_expired_and_future_dated_approvals_are_refused(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    body = _approve(cfg, days=1)
    assert decide_egress(cfg, _manifest(), approval=body, now=NOW + timedelta(days=1)).reason_code == "approval-expired"
    future = _approve(cfg, now=NOW + timedelta(hours=1))
    assert decide_egress(cfg, _manifest(), approval=future, now=NOW).reason_code == "approval-future"
    skewed = _approve(cfg, now=NOW + timedelta(minutes=2))  # small clock skew is allowed
    assert decide_egress(cfg, _manifest(), approval=skewed, now=NOW).allowed


def test_an_approval_longer_than_approval_max_days_is_refused(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    body = approval_body(cfg, issued=NOW)
    body["expires"] = (NOW + timedelta(days=8)).isoformat()
    body["mac"] = mac(FAKE_KEY, body)
    decision = decide_egress(cfg, _manifest(), approval=body, now=NOW)
    assert decision.reason_code == "approval-invalid" and "lifetime" in decision.message
    with pytest.raises(VastConfigError, match="at most"):
        approval_body(cfg, issued=NOW, days=8)


def test_an_approval_for_another_root_is_refused(tmp_path, root):
    other = tmp_path / "other-root"
    write_key(other / "keys")
    body = _approve(_cfg(other, allow_documents=[SHA1]))
    decision = decide_egress(_cfg(root, allow_documents=[SHA1]), _manifest(), approval=body, now=NOW)
    assert decision.reason_code == "approval-invalid" and "different backend-config-root" in decision.message


def test_a_high_tier_approval_is_not_an_egress_approval(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    body = {"tier": "high", "program": "x", "issued": NOW.isoformat(),
            "expires": (NOW + timedelta(hours=1)).isoformat(), "max_job_usd": 5.0, "nonce": "n"}
    body["mac"] = mac(FAKE_KEY, body)  # validly signed with the same key
    decision = decide_egress(cfg, _manifest(), approval=body, now=NOW)
    assert decision.reason_code == "approval-invalid" and "purpose" in decision.message


def test_the_approval_body_is_design_9_3s_and_carries_the_envelope(root):
    cfg = _cfg(root, allow_license_tiers=["open"], allow_documents=[SHA1, SHA2])
    body = _approve(cfg, max_job_usd=1.5, max_total_usd=12.0, nonce="fixed-nonce")
    assert set(body) == {"purpose", "program", "egress_digest", "egress_summary", "issued", "expires",
                         "max_job_usd", "max_total_usd", "nonce", "mac"}
    assert body["purpose"] == EGRESS_PURPOSE and body["egress_digest"] == egress_digest(cfg)
    assert body["egress_summary"]["allow_documents"] == 2  # counted, not listed
    assert body["egress_summary"]["allow_license_tiers"] == ["open"]
    assert (body["max_job_usd"], body["max_total_usd"], body["nonce"]) == (1.5, 12.0, "fixed-nonce")
    assert FAKE_KEY not in json.dumps(body)
    assert not cfg.approval_path.exists()  # signing is pure: nothing written
    with pytest.raises(VastConfigError, match="max_approval_usd"):
        approval_body(cfg, issued=NOW, max_total_usd=26.0)
    with pytest.raises(VastConfigError, match="finite and > 0"):
        approval_body(cfg, issued=NOW, max_job_usd=0)


def test_the_approval_file_round_trips(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    assert read_egress_approval(cfg) is None
    cfg.approval_path.write_text("not json", encoding="utf-8")
    with pytest.raises(EgressRefused) as info:
        read_egress_approval(cfg)
    assert info.value.reason_code == "approval-invalid"
    body = _approve(cfg)
    assert write_egress_approval(cfg, body) == cfg.approval_path
    assert read_egress_approval(cfg) == body
    assert verify_egress_approval(cfg, read_egress_approval(cfg), now=NOW)["nonce"] == body["nonce"]


def test_the_config_switch_alone_when_require_approval_is_false(root):
    cfg = _cfg(root, allow_documents=[SHA1], require_approval=False)
    decision = decide_egress(cfg, _manifest(), approval=None, now=NOW)
    assert decision.allowed and decision.approval_nonce is None and decision.approval_max_total_usd is None
    assert decide_egress(cfg, _manifest(sha=SHA2), approval=None, now=NOW).reason_code == "tier-not-allowed"


def test_a_document_over_max_document_mb_is_refused(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    decision = decide_egress(cfg, _manifest(size=1_024_000_001), approval=_approve(cfg), now=NOW)
    assert decision.reason_code == "document-too-large" and "max_document_mb" in decision.message
    assert decide_egress(cfg, _manifest(size=1_024_000_000), approval=_approve(cfg), now=NOW).allowed


def test_a_malformed_manifest_is_an_error_not_a_refusal(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    bad = _manifest()
    bad["inputs"][0]["sha256"] = "not-a-hash"
    with pytest.raises(VastError) as info:
        decide_egress(cfg, bad, approval=_approve(cfg), now=NOW)
    assert not isinstance(info.value, EgressRefused)


def test_a_missing_key_refuses_verification_by_name(root):
    cfg = _cfg(root, allow_documents=[SHA1])
    body = _approve(cfg)
    (root / "keys" / "vastai.key").unlink()
    decision = decide_egress(cfg, _manifest(), approval=body, now=NOW)
    assert decision.reason_code == "key-missing" and FAKE_KEY not in decision.message
    assert decision.disable_executor and decision.refusal().disable_executor  # stops vast.ai use for the run


def test_a_refusal_becomes_a_ledger_row_with_the_charter_fields(root, isolated_state):  # noqa: F811
    cfg = _cfg(root, allow_license_tiers=["open"])
    decision = decide_egress(cfg, _manifest(tier="commercial_restricted"), approval=_approve(cfg), now=NOW)
    row = Ledger(isolated_state).append(
        "refused", **decision.ledger_fields(), reason_code=decision.reason_code,
        next_action=decision.next_actions[0], message=decision.message,
    )
    assert (row["sha256"], row["bytes"], row["license_tier"]) == (SHA1, 1_000_000, "commercial_restricted")
    assert row["job_id"] == "JOB-egress-1" and row["reason_code"] == "tier-not-allowed"
