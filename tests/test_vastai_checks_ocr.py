"""vast.ai round 2, lane L3: the doctor checks ``vastai_ocr_egress`` and
``vastai_ocr_ledger`` (design 13), on fakes.

Both read local files only (the toml, the approval, the fake key to check the
approval's signature, the ledger, the run records): the network tripwire
fails any test that reaches the vast.ai client. Both pass at once where
vast.ai is not configured, which is the queue side's own doctor.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trialerror.util.config import load_config
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, registered_checks
from trialerror.vastai import checks
from trialerror.vastai.config import load_vast_config
from trialerror.vastai.egress import sign_egress_approval, write_egress_approval
from trialerror.vastai.lease import write_run_record
from trialerror.vastai.ledger import Ledger
from tests._vastai_fakes import (  # noqa: F401 - fixtures
    dev_toml,
    isolated_state,
    network_tripwire,
    toml_text,
    write_key,
)

DOC_SHA = "cd" * 32


@pytest.fixture(autouse=True)
def _guarded(network_tripwire, isolated_state):
    yield isolated_state


def _root(tmp_path: Path, raw: dict | None = None, **toml_kwargs) -> Path:
    root = tmp_path / "devroot"
    root.mkdir(exist_ok=True)
    write_key(root / "keys")
    if raw is None:
        toml_kwargs.setdefault("egress", {"allow_documents": [DOC_SHA]})
        raw = dev_toml(**toml_kwargs)
    (root / "trialerror.toml").write_text(toml_text(raw), encoding="utf-8")
    return root


def _cfg(root: Path):
    return load_vast_config(load_config(root / "trialerror.toml").raw, config_root=root)


def _approve(root: Path, **kwargs) -> dict:
    cfg = _cfg(root)
    body = sign_egress_approval(cfg, now=datetime.now(timezone.utc), **kwargs)
    write_egress_approval(cfg, body)
    return body


def _egress(root: Path):
    return checks.check_vastai_ocr_egress(DoctorContext(program_root=root))


def _ledger(root: Path):
    return checks.check_vastai_ocr_ledger(DoctorContext(program_root=root))


def test_both_checks_are_registered_in_the_vastai_category():
    discover_and_register_checks()
    registry = registered_checks()
    for name in ("vastai_ocr_egress", "vastai_ocr_ledger"):
        assert registry[name][0] == "vastai"


def test_both_pass_at_once_where_vast_ai_is_not_configured(tmp_path):
    raw = {"program": {"id": "queue-side"}, "ingest": {"ocr": {"backend": "offload"}}}
    root = _root(tmp_path, raw=raw)
    for result in (_egress(root), _ledger(root)):
        assert result.status == "pass" and "not configured" in result.message


# ---------------------------------------------------------------------------
# vastai_ocr_egress
# ---------------------------------------------------------------------------
def test_egress_with_the_local_executor_passes_and_nothing_leaves(tmp_path):
    root = _root(tmp_path, executor="local")
    result = _egress(root)
    assert result.status == "pass"
    assert 'executor = "local"' in result.message and "no document leaves" in result.message


def test_egress_without_an_approval_says_nothing_leaves(tmp_path):
    result = _egress(_root(tmp_path))
    assert result.status == "pass"
    assert "approval-missing" in result.message and "1 named document(s)" in result.message
    assert result.details["approval"]["valid"] is False


def test_egress_with_a_valid_approval_reports_expiry_and_envelope_left(tmp_path, isolated_state):
    root = _root(tmp_path)
    body = _approve(root, max_total_usd=20.0)
    Ledger(isolated_state).append("intent", lease_id="VOCR-1", sha256=DOC_SHA, bytes=10, host_id=1, datacenter=True,
                                  verified=True, worst_usd=1.5, approval_nonce=body["nonce"])
    result = _egress(root)
    assert result.status == "pass", result.message
    assert "approval valid until" in result.message and "$18.50 of $20.00 left" in result.message
    assert result.details["envelope"] == {"cap_usd": 20.0, "spent_usd": 1.5, "left_usd": 18.5}
    assert result.details["host_requirements"]["require_datacenter"] is True
    assert "mac" not in json.dumps(result.details)


def test_egress_warns_when_the_policy_names_commercial_restricted(tmp_path):
    root = _root(tmp_path, egress={"allow_license_tiers": ["open", "commercial_restricted"]})
    _approve(root)
    result = _egress(root)
    assert result.status == "warn" and "commercial_restricted" in result.message


def test_egress_warns_when_the_table_no_longer_matches_its_seal(tmp_path):
    root = _root(tmp_path)
    _approve(root)
    _root(tmp_path, egress={"allow_documents": [DOC_SHA, "ef" * 32]})  # widened after the approval
    result = _egress(root)
    assert result.status == "warn" and "no longer matches" in result.message
    assert result.details["approval"]["reason_code"] == "approval-unsealed"


def test_egress_warns_on_a_hand_written_approval(tmp_path):
    root = _root(tmp_path)
    body = _approve(root)
    cfg = _cfg(root)
    cfg.approval_path.write_text(json.dumps({**body, "max_total_usd": 25.0, "mac": "0" * 64}), encoding="utf-8")
    result = _egress(root)
    assert result.status == "warn" and "not a valid approval" in result.message


def test_egress_warns_when_the_settings_are_refused(tmp_path):
    root = _root(tmp_path, vastai={"keep_alive": True})
    result = _egress(root)
    assert result.status == "warn" and "refused" in result.message


# ---------------------------------------------------------------------------
# vastai_ocr_ledger
# ---------------------------------------------------------------------------
def _ship(state: Path, *, lease: str = "VOCR-1", iid: int = 5001, outcome: bool = False, ts: str | None = None,
          worst: float = 1.0) -> None:
    led = Ledger(state)
    stamp = {"ts": ts} if ts else {}
    led.append("intent", lease_id=lease, sha256=DOC_SHA, bytes=10, host_id=1, datacenter=True, verified=True,
               worst_usd=worst, **stamp)
    led.append("shipped", lease_id=lease, instance_id=iid, start="2026-09-18T00:05:00Z", **stamp)
    if outcome:
        led.append("outcome", lease_id=lease, instance_id=iid, end="2026-09-18T00:30:00Z", estimated_cost_usd=0.3,
                   result="published", **stamp)


def _record(state: Path, root: Path, *, lease: str = "VOCR-1", iid: int = 5001, status: str, deadline: float):
    from trialerror.vastai.guard import program_fingerprint

    write_run_record(state, {"run_id": lease, "status": status, "instance_id": iid, "deadline_epoch": deadline,
                             "program_fp": program_fingerprint(root), "label": f"trialerror|x|{lease}|1"})


def test_ledger_empty_passes(tmp_path):
    result = _ledger(_root(tmp_path))
    assert result.status == "pass" and "empty" in result.message


def test_ledger_settled_leases_pass(tmp_path, isolated_state):
    root = _root(tmp_path)
    _ship(isolated_state, outcome=True)
    result = _ledger(root)
    assert result.status == "pass", result.message


def test_ledger_fails_on_a_shipped_lease_with_no_outcome_and_no_destroy(tmp_path, isolated_state):
    root = _root(tmp_path)
    _ship(isolated_state)
    result = _ledger(root)
    assert result.status == "fail" and "5001" in result.message and "reap" in result.message


@pytest.mark.parametrize("how", ["reaped_row", "record_destroyed", "record_reaped"])
def test_ledger_a_confirmed_destroy_settles_the_shipment(tmp_path, isolated_state, how):
    root = _root(tmp_path)
    _ship(isolated_state)
    if how == "reaped_row":
        Ledger(isolated_state).append("reaped", instance_id=5001, reason="past_deadline")
    else:
        _record(isolated_state, root, status=how.split("_")[1], deadline=time.time() - 60)
    assert _ledger(root).status == "pass"


def test_ledger_a_lease_inside_its_deadline_is_in_flight_not_a_failure(tmp_path, isolated_state):
    root = _root(tmp_path)
    _ship(isolated_state)
    _record(isolated_state, root, status="running", deadline=time.time() + 3600)
    result = _ledger(root)
    assert result.status == "warn" and "in flight" in result.message


def test_ledger_a_failed_destroy_fails_even_inside_the_deadline(tmp_path, isolated_state):
    root = _root(tmp_path)
    _ship(isolated_state)
    _record(isolated_state, root, status="destroy_failed", deadline=time.time() + 3600)
    Ledger(isolated_state).append("destroy_failed", instance_id=5001, lease_id="VOCR-1")
    assert _ledger(root).status == "fail"


def test_ledger_a_past_deadline_lease_fails(tmp_path, isolated_state):
    root = _root(tmp_path)
    _ship(isolated_state)
    _record(isolated_state, root, status="running", deadline=time.time() - 60)
    assert _ledger(root).status == "fail"


def test_ledger_warns_on_a_torn_line(tmp_path, isolated_state):
    root = _root(tmp_path)
    _ship(isolated_state, outcome=True)
    with open(Ledger(isolated_state).path, "ab") as f:
        f.write(b'{"kind": "intent", "lease_id": "VOCR-9"')
    result = _ledger(root)
    assert result.status == "warn" and "torn" in result.message
    assert result.details["torn"]


def test_ledger_warns_when_seven_day_spend_passes_half_the_envelope(tmp_path, isolated_state):
    root = _root(tmp_path)
    _approve(root, max_total_usd=10.0)
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    _ship(isolated_state, lease="VOCR-old", iid=1, outcome=True, ts=old, worst=4.0)  # settled at 0.30, and old
    _ship(isolated_state, lease="VOCR-a", iid=2, outcome=True)
    assert _ledger(root).status == "pass"

    for n in range(3):  # unsettled leases count at their worst case: 3 x 2.0 = 6.0 > 5.0
        lease = f"VOCR-b{n}"
        _ship(isolated_state, lease=lease, iid=10 + n, worst=2.0)
        Ledger(isolated_state).append("reaped", instance_id=10 + n, reason="past_deadline")
    result = _ledger(root)
    assert result.status == "warn" and "more than half" in result.message and "$10.00" in result.message
    assert result.details["envelope_usd"] == 10.0
