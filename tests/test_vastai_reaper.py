"""vast.ai OCR executor, lane L1: the narrowed reaper (design 11.2) and the
two ported doctor checks, category ``vastai``.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

from tests._vastai_fakes import (
    FakeVast,
    dev_toml,
    isolated_state,  # noqa: F401 - fixture
    network_tripwire,  # noqa: F401 - fixture
    toml_text,
    write_key,
)
from trialerror.util.doctor import DoctorContext
from trialerror.vastai import checks as vchecks
from trialerror.vastai import reaper as reaper_mod
from trialerror.vastai.api import VastClient
from trialerror.vastai.errors import VastApiError
from trialerror.vastai.guard import program_fingerprint
from trialerror.vastai.lease import make_label, read_state_run_records, write_run_record
from trialerror.vastai.ledger import Ledger, lease_spend
from trialerror.vastai.reaper import pid_alive, reap_ocr

NOW = 1_900_000_000
HOST = socket.gethostname()


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state):  # noqa: F811
    yield


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "devroot"
    write_key(r / "keys")
    return r


def _record(state, root, run_id, *, pid=222, status="running", instance_id=None, deadline=NOW + 3600, fp=None,
            label=None):
    fp = fp or program_fingerprint(root)
    rec = {"run_id": run_id, "lease_id": run_id, "program_fp": fp, "pid": pid, "host": HOST, "status": status,
           "instance_id": instance_id, "deadline_epoch": deadline, "label": label or make_label(fp, run_id, deadline)}
    write_run_record(state, rec)
    return rec


def _client(root, fake):
    return VastClient(root / "keys" / "vastai.key", http=fake.http)


def _scene(root, state, tmp_path):
    fp = program_fingerprint(root)
    other_fp = program_fingerprint(tmp_path / "another-dev-root")
    fake = FakeVast()
    fake.instances = {
        1: {"id": 1, "label": make_label("f" * 64, "VAST-embed", NOW - 10)},  # other programme, past deadline
        2: {"id": 2, "label": make_label(fp, "VOCR-dead", NOW + 3600)},  # ours, owner process died
        3: {"id": 3, "label": make_label(fp, "VOCR-live", NOW + 3600)},  # ours, owner alive -> keep
        4: {"id": 4, "label": "someone-else's instance"},  # not TrialError -> never touched, not reported
        5: {"id": 5, "label": "trialerror|garbled"},  # our prefix, malformed -> reported, not destroyed
        6: {"id": 6, "label": make_label(fp, "VOCR-norecord", NOW + 3600)},  # ours, no run record
        7: {"id": 7, "label": make_label(fp, "VOCR-late", NOW - 1)},  # ours, past deadline
        8: {"id": 8, "label": None},  # unlabelled, our record says finished
        9: {"id": 9, "label": None},  # unlabelled, ANOTHER root's record names it -> not ours
        10: {"id": 10, "label": make_label("e" * 64, "VAST-live", NOW + 99)},  # other programme, live
    }
    _record(state, root, "VOCR-dead", pid=111)
    _record(state, root, "VOCR-live", pid=222)
    _record(state, root, "VOCR-late", pid=222, deadline=NOW - 1)
    _record(state, root, "VOCR-done", status="destroyed", instance_id=8)
    _record(state, root, "VOCR-theirs", status="destroyed", instance_id=9, fp=other_fp)
    return fake


def test_the_reaper_destroys_only_this_roots_instances(root, isolated_state, tmp_path):  # noqa: F811
    fake = _scene(root, isolated_state, tmp_path)
    ledger = Ledger(isolated_state)
    out = reap_ocr(_client(root, fake), config_root=root, state_dir=isolated_state, ledger=ledger,
               clock=lambda: NOW, alive=lambda pid: pid == 222, log=lambda m: None)
    by_id = {e["instance_id"]: (e["reason"], e["action"]) for e in out}
    assert by_id == {
        1: ("foreign_past_deadline", "reported"),
        2: ("owner_dead", "destroyed"),
        5: ("foreign_malformed_label", "reported"),
        6: ("no_run_record", "destroyed"),
        7: ("past_deadline", "destroyed"),
        8: ("run_finished", "destroyed"),
        10: ("foreign_live", "reported"),
    }
    assert set(fake.instances) == {1, 3, 4, 5, 9, 10}
    assert sorted(fake.verbs("DELETE")) == ["/instances/2/", "/instances/6/", "/instances/7/", "/instances/8/"]
    assert not any(e["destroyed"] for e in out if e["action"] == "reported")
    records = {r["run_id"]: r for r in read_state_run_records(isolated_state)}
    assert records["VOCR-dead"]["status"] == "reaped" and records["VOCR-live"]["status"] == "running"
    assert records["VOCR-theirs"]["status"] == "destroyed"  # another root's record is never rewritten
    reaped = [r for r in ledger.read().rows if r["kind"] == "reaped"]
    assert sorted(r["instance_id"] for r in reaped) == [2, 6, 7, 8]


def test_a_dry_run_destroys_writes_and_appends_nothing(root, isolated_state, tmp_path):  # noqa: F811
    fake = _scene(root, isolated_state, tmp_path)
    before = {r["run_id"]: r["status"] for r in read_state_run_records(isolated_state)}
    out = reap_ocr(_client(root, fake), config_root=root, state_dir=isolated_state, dry_run=True,
               clock=lambda: NOW, alive=lambda pid: pid == 222, log=lambda m: None)
    assert {e["instance_id"] for e in out if e["action"] == "would_destroy"} == {2, 6, 7, 8}
    assert fake.verbs("DELETE") == [] and len(fake.instances) == 10
    assert {r["run_id"]: r["status"] for r in read_state_run_records(isolated_state)} == before
    assert not Ledger(isolated_state).path.exists()


def test_a_reaped_instance_still_listed_is_a_destroy_failure(root, isolated_state):  # noqa: F811
    fp = program_fingerprint(root)
    fake = FakeVast()
    fake.instances = {6: {"id": 6, "label": make_label(fp, "VOCR-x", NOW + 60)}}
    fake.sticky = {6}
    ledger = Ledger(isolated_state)
    out = reap_ocr(_client(root, fake), config_root=root, state_dir=isolated_state, ledger=ledger, clock=lambda: NOW,
               log=lambda m: None)
    assert out[0]["action"] == "destroy_failed" and not out[0]["destroyed"]
    assert [r["kind"] for r in ledger.read().rows] == ["destroy_failed"]


def test_create_failures_are_reaped_when_listed_and_settled_only_by_a_successful_listing(
    root, isolated_state  # noqa: F811
):
    ledger = Ledger(isolated_state)
    ledger.append("intent", lease_id="VOCR-cf2", sha256="e" * 64, bytes=1, host_id=1, datacenter=True,
                  verified=True, worst_usd=1.25, approval_nonce="N")
    landed = _record(isolated_state, root, "VOCR-cf1", status="create_failed", deadline=NOW - 100)
    _record(isolated_state, root, "VOCR-cf2", status="create_failed", deadline=NOW - 100)
    fake = FakeVast()
    fake.list_failures = 1
    with pytest.raises(VastApiError):  # a blind reaper must not pretend it found nothing
        reap_ocr(_client(root, fake), config_root=root, state_dir=isolated_state, ledger=ledger, clock=lambda: NOW)
    assert {r["status"] for r in read_state_run_records(isolated_state)} == {"create_failed"}
    fake.instances = {21: {"id": 21, "label": landed["label"]}}  # the first create DID land
    out = reap_ocr(_client(root, fake), config_root=root, state_dir=isolated_state, ledger=ledger, clock=lambda: NOW,
               log=lambda m: None)
    assert {(e["run_id"], e["action"]) for e in out} == {("VOCR-cf1", "destroyed"), ("VOCR-cf2", "settled_absent")}
    assert fake.instances == {}
    assert {r["run_id"]: r["status"] for r in read_state_run_records(isolated_state)} == {
        "VOCR-cf1": "reaped", "VOCR-cf2": "absent"}
    spend = lease_spend(ledger.read().rows)
    assert spend["VOCR-cf2"].usd == 0.0 and spend["VOCR-cf2"].settled  # no longer counts at its worst case


def test_pid_alive_never_signals_on_windows(monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill on Windows terminates the process"))
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False and pid_alive(-5) is False
    assert reaper_mod.pid_alive is pid_alive


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def _write_toml(root, raw):
    root.mkdir(parents=True, exist_ok=True)
    (root / "trialerror.toml").write_text(toml_text(raw), encoding="utf-8")


def _factory(monkeypatch, fake):
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=fake.http))


def test_both_checks_pass_at_once_where_vastai_is_not_configured(tmp_path, monkeypatch):
    root = tmp_path / "sandbox"
    _write_toml(root, {"program": {"id": "sandbox"}, "ingest": {"ocr": {"backend": "offload"}}})
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: pytest.fail("no API call may be made"))
    monkeypatch.setattr(vchecks, "_state_dir_factory", lambda: pytest.fail("no state may be read"))
    ctx = DoctorContext(program_root=root)
    for check in (vchecks.check_vastai_live_instances, vchecks.check_vastai_high_tier):
        result = check(ctx)
        assert result.status == "pass" and "not configured" in result.message and result.category == "vastai"
    assert vchecks.check_vastai_live_instances(DoctorContext(program_root=tmp_path / "no-toml")).status == "pass"
    assert vchecks.check_vastai_high_tier(DoctorContext(program_root=None)).status == "skip"


def test_live_instances_reports_live_overdue_and_other_roots(root, monkeypatch):
    _write_toml(root, dev_toml())
    ctx = DoctorContext(program_root=root)
    fake = FakeVast()
    _factory(monkeypatch, fake)
    assert vchecks.check_vastai_live_instances(ctx).status == "pass"
    fp = program_fingerprint(root)
    fake.instances = {7: {"id": 7, "label": make_label(fp, "VOCR-x", 4_000_000_000)}}
    assert vchecks.check_vastai_live_instances(ctx).status == "warn"
    fake.instances = {7: {"id": 7, "label": make_label(fp, "VOCR-x", 1)}}
    result = vchecks.check_vastai_live_instances(ctx)
    assert result.status == "fail" and "reap" in result.message
    # round 4 (e), the public result: another root's instance past its deadline fails (and is never touched)
    fake.instances = {8: {"id": 8, "label": make_label("f" * 64, "VAST-embed", 1)}}
    result = vchecks.check_vastai_live_instances(ctx)
    assert result.status == "fail" and "another root" in result.message and "`trialerror vastai reap`" in result.message
    assert fake.instances == {8: {"id": 8, "label": make_label("f" * 64, "VAST-embed", 1)}}


def test_live_instances_fails_on_a_destroy_failed_record(root, monkeypatch, isolated_state):  # noqa: F811
    _write_toml(root, dev_toml())
    _factory(monkeypatch, FakeVast())
    _record(isolated_state, root, "VOCR-stuck", status="destroy_failed", instance_id=77)
    result = vchecks.check_vastai_live_instances(DoctorContext(program_root=root))
    assert result.status == "fail" and result.details["overdue_or_failed"][0]["run_id"] == "VOCR-stuck"


def test_live_instances_warns_when_it_cannot_see_the_account(root, monkeypatch):
    _write_toml(root, dev_toml())

    def blind_http(method, url, headers, body, timeout):
        return 503, {"error": "unavailable"}

    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=blind_http))
    result = vchecks.check_vastai_live_instances(DoctorContext(program_root=root))
    assert result.status == "warn" and "cannot be ruled out" in result.message


def test_doctor_api_check_false_skips_the_listing(root, monkeypatch):
    _write_toml(root, dev_toml(vastai={"doctor_api_check": False}))
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: pytest.fail("doctor_api_check = false"))
    result = vchecks.check_vastai_live_instances(DoctorContext(program_root=root))
    assert result.status == "pass" and any("doctor_api_check" in n for n in result.details["notes"])


def test_the_high_tier_check_warns_on_config_and_recent_use(root, isolated_state):  # noqa: F811
    _write_toml(root, dev_toml())
    ctx = DoctorContext(program_root=root)
    assert vchecks.check_vastai_high_tier(ctx).status == "pass"
    _write_toml(root, dev_toml(vastai={"tier": "high"}))
    result = vchecks.check_vastai_high_tier(ctx)
    assert result.status == "warn" and "configured" in result.message
    _write_toml(root, dev_toml())
    Ledger(isolated_state).append("intent", lease_id="VOCR-h", sha256="f" * 64, bytes=1, host_id=1,
                                  datacenter=True, verified=True, worst_usd=2.0, tier="high")
    result = vchecks.check_vastai_high_tier(ctx)
    assert result.status == "warn" and "high-tier lease" in result.message
