"""vast.ai OCR executor, lane L1: the instance lease (design sections 3, 10,
11.1) on the fake clock -- create, label, run record, ledger intent, offers
in order, the TTL watchdog, destroy retried and confirmed by listing.
"""

from __future__ import annotations

import json
import re
import threading
import time

import pytest

from tests._vastai_fakes import (
    FakeClock,
    FakeVast,
    dev_toml,
    isolated_state,  # noqa: F401 - fixture
    make_offer,
    network_tripwire,  # noqa: F401 - fixture
    write_key,
)
from trialerror.vastai.api import VastClient
from trialerror.vastai.config import load_vast_config
from trialerror.vastai.errors import HostFailure, VastPlanRefused
from trialerror.vastai.guard import program_fingerprint
from trialerror.vastai.lease import (
    LeaseExpired,
    OcrInstanceLease,
    parse_label,
    read_state_run_records,
    state_runs_dir,
)
from trialerror.vastai.ledger import Ledger, lease_spend
from trialerror.vastai.pricing import plan_document, price_job

SHA = "d" * 64
A4_PT = (595.28, 841.89)
JOB_ID = "JOB-secret-title-42"
DOC_ID = "DOC-a-book-title"
INTENT = {"job_id": JOB_ID, "doc_id": DOC_ID, "sha256": SHA, "bytes": 5_000_000, "license_tier": "open",
          "approval_nonce": "N1", "worker_run_id": "RUN-1", "tier": "mid"}


def _public_parse_label(label):
    """The public TrialError copy's embedding backend's reaper parser,
    verbatim in behaviour: any ``trialerror|`` label without exactly four
    fields is malformed (and that reaper destroys it)."""
    if not label or not str(label).startswith("trialerror|"):
        return None
    parts = str(label).split("|")
    try:
        _, prog, run_id, deadline = parts
        return {"program": prog, "run_id": run_id, "deadline_epoch": int(deadline)}
    except ValueError:
        return {"malformed": True, "label": label}


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state):  # noqa: F811
    yield


@pytest.fixture
def env(tmp_path, isolated_state):  # noqa: F811
    root = tmp_path / "devroot"
    write_key(root / "keys")
    cfg = load_vast_config(dev_toml(), config_root=root)
    fake = FakeVast()
    clock = FakeClock()
    client = VastClient(root / "keys" / "vastai.key", http=fake.http)
    plan = plan_document([A4_PT] * 100, cfg)
    priced = price_job(client, cfg, plan, document_bytes=INTENT["bytes"], now=clock.now())
    logs: list[str] = []
    return {"root": root, "cfg": cfg, "fake": fake, "clock": clock, "client": client, "priced": priced,
            "state": isolated_state, "ledger": Ledger(isolated_state, now=clock.now), "logs": logs}


def _lease(env, offers=None, **kw):
    kw.setdefault("watchdog", False)
    return OcrInstanceLease(
        env["client"], config_root=env["root"], offers=offers or env["priced"].offers, image=env["cfg"].image,
        disk_gb=env["cfg"].disk_gb, state_dir=env["state"], ledger=env["ledger"], intent=INTENT,
        log=env["logs"].append, clock=env["clock"], sleep=env["clock"].sleep, **kw,
    )


def test_a_lease_creates_labels_records_and_destroys(env):
    fake, clock = env["fake"], env["clock"]
    with _lease(env) as lease:
        assert lease.instance_id in fake.instances and lease.offer["id"] == 11
        assert (lease.machine_id, lease.host_id) == (9011, 7011)
        inst = fake.instances[lease.instance_id]
        assert inst["onstart"] == lease.onstart_script() and "kill -TERM 1" in inst["onstart"]
        assert inst["image"] == env["cfg"].image and inst["disk"] == 32
        rec = read_state_run_records(env["state"])[0]
        assert rec["status"] == "running" and rec["instance_id"] == lease.instance_id
        assert rec["program_fp"] == program_fingerprint(env["root"]) and rec["machine_id"] == 9011
        clock.advance(600)
    assert fake.instances == {} and lease.destroyed
    assert read_state_run_records(env["state"])[0]["status"] == "destroyed"
    rows = env["ledger"].read().rows
    assert [r["kind"] for r in rows] == ["intent"]
    intent = rows[0]
    assert intent["lease_id"] == lease.lease_id and intent["offer_id"] == 11
    assert (intent["host_id"], intent["machine_id"], intent["datacenter"], intent["verified"]) == (7011, 9011, True, True)
    assert intent["worst_usd"] == pytest.approx(env["priced"].offers[0].worst_usd, rel=1e-6)
    assert intent["sha256"] == SHA and intent["approval_nonce"] == "N1" and intent["deadline"].endswith("Z")
    outcome = env["ledger"].append("outcome", **lease.outcome_fields(), result="published")
    assert outcome["estimated_cost_usd"] == pytest.approx(lease.dph * 600 / 3600, abs=1e-6)
    assert lease_spend(env["ledger"].read().rows)[lease.lease_id].settled


def test_the_label_is_byte_compatible_with_the_public_reaper(env):
    with _lease(env) as lease:
        label = env["fake"].instances[lease.instance_id]["label"]
    assert len(label.split("|")) == 4 and re.fullmatch(r"trialerror\|[0-9a-f]{12}\|VOCR-[0-9a-f]{16}\|\d+", label)
    public = _public_parse_label(label)
    assert public == parse_label(label) and not public.get("malformed")
    assert public["program"] == program_fingerprint(env["root"])[:12]
    assert public["deadline_epoch"] == int(env["clock"].t + env["priced"].offers[0].ttl_s)


def test_nothing_sent_to_vastai_names_the_job_or_the_document(env):
    with _lease(env):
        pass
    sent = json.dumps([b for _m, _p, b in env["fake"].bodies])
    for secret in (JOB_ID, DOC_ID, SHA, "title"):
        assert secret not in sent


def test_an_exception_in_the_body_still_destroys(env):
    with pytest.raises(RuntimeError, match="marker exploded"):
        with _lease(env):
            raise RuntimeError("marker exploded")
    assert env["fake"].instances == {}


def test_a_taken_offer_falls_through_to_the_next_one_and_costs_nothing(env):
    env["fake"].gone = {11}
    with _lease(env) as lease:
        assert lease.offer["id"] == 12 and lease.offers_taken == [11]
    rows = env["ledger"].read().rows
    assert [(r["kind"], r.get("offer_id"), r.get("result")) for r in rows] == [
        ("intent", 11, None), ("outcome", None, "not_created"), ("intent", 12, None)]
    spend = lease_spend(rows)
    assert spend[rows[0]["lease_id"]].usd == 0.0 and spend[rows[0]["lease_id"]].settled
    statuses = sorted(r["status"] for r in read_state_run_records(env["state"]))
    assert statuses == ["destroyed", "offer_taken"]
    assert any("offer 11 was taken" in m for m in env["logs"])


def test_every_offer_taken_rents_nothing(env):
    env["fake"].gone = {11, 12}
    with pytest.raises(VastPlanRefused) as info:
        with _lease(env):
            pytest.fail("the body must not run")
    assert info.value.reason_code == "no-offer" and info.value.details["offers_taken"] == [11, 12]
    assert env["fake"].instances == {}
    assert {r["status"] for r in read_state_run_records(env["state"])} == {"offer_taken"}


def test_at_most_five_offers_are_tried(env):
    fake = env["fake"]
    fake.offers = [make_offer(i, dph_base=0.20 + i / 1000) for i in range(21, 28)]
    fake.gone = {o["id"] for o in fake.offers}
    priced = price_job(env["client"], env["cfg"], plan_document([A4_PT] * 100, env["cfg"]), document_bytes=1)
    assert len(priced.offers) == 7
    with pytest.raises(VastPlanRefused):
        with _lease(env, offers=priced.offers):
            pass
    assert len(fake.verbs("PUT")) == 5


def test_a_failed_create_is_a_plan_refusal_that_leaves_a_record_for_the_reaper(env):
    env["fake"].create_status = 500
    with pytest.raises(VastPlanRefused) as info:
        with _lease(env):
            pass
    assert info.value.reason_code == "create-failed"
    rec = read_state_run_records(env["state"])[0]
    assert rec["status"] == "create_failed" and rec["instance_id"] is None and rec["label"].startswith("trialerror|")


def test_the_watchdog_destroys_at_the_deadline_and_the_expiry_is_class_x(env):
    clock = env["clock"]
    with pytest.raises(LeaseExpired):
        with _lease(env) as lease:
            assert lease.poll_watchdog() is False
            clock.advance(lease.ttl_s + 1)
            assert lease.poll_watchdog() is True
            assert lease.destroyed and env["fake"].instances == {}
            raise EOFError("ssh channel closed under the range")  # what the body sees mid-range
    assert lease.expired
    assert read_state_run_records(env["state"])[0]["status"] == "destroyed"
    assert any("TTL of" in m for m in env["logs"])


def test_check_raises_lease_expired_past_the_deadline(env):
    with pytest.raises(LeaseExpired):
        with _lease(env) as lease:
            env["clock"].advance(lease.ttl_s)
            lease.check()
    assert env["fake"].instances == {}


def test_the_watchdog_thread_fires_on_the_injected_clock(env):
    fired = threading.Event()
    lease = _lease(env, watchdog=True, watchdog_interval_s=0.01)
    with pytest.raises(LeaseExpired):
        with lease:
            lease.on_expire = fired.set
            env["clock"].advance(lease.ttl_s + 5)
            assert fired.wait(5.0), "the watchdog thread did not fire"
            deadline = time.monotonic() + 5.0
            while not lease.destroyed and time.monotonic() < deadline:
                time.sleep(0.01)
            assert lease.destroyed
            lease.check()
    assert env["fake"].instances == {}


def test_destroy_is_retried_with_backoff_and_confirmed_by_listing(env):
    env["fake"].delete_failures = 2
    with _lease(env) as lease:
        env["clock"].sleeps.clear()
    assert lease.destroyed and env["fake"].instances == {}
    assert env["clock"].sleeps == [1.0, 2.0]


def test_a_destroy_that_cannot_be_confirmed_is_loud_recorded_and_ledgered(env):
    with _lease(env) as lease:
        env["fake"].sticky = {lease.instance_id}
    assert not lease.destroyed and lease.destroy_error == "still listed after DELETE"
    assert len(env["fake"].verbs("DELETE")) == 5
    assert read_state_run_records(env["state"])[0]["status"] == "destroy_failed"
    rows = env["ledger"].read().rows
    assert rows[-1]["kind"] == "destroy_failed" and rows[-1]["instance_id"] == lease.instance_id
    assert any("could NOT be confirmed destroyed" in m for m in env["logs"])


def test_wait_ready_polls_until_running_and_fails_over_on_a_dead_host(env):
    fake = env["fake"]
    fake.boot_polls = 2
    with _lease(env) as lease:
        inst = lease.wait_ready(poll_interval_s=10)
        assert inst["actual_status"] == "running" and env["clock"].sleeps[-2:] == [10, 10]
    fake.exit_on_boot = True
    with pytest.raises(HostFailure) as info:
        with _lease(env) as lease:
            lease.wait_ready(poll_interval_s=10)
    assert info.value.machine_id == 9011 and fake.instances == {}
    fake.exit_on_boot, fake.boot_polls = False, 10_000
    with pytest.raises(HostFailure, match="not running after 60 s"):
        with _lease(env) as lease:
            lease.wait_ready(poll_interval_s=10, timeout_s=60)


def test_run_records_default_to_the_worker_state_dir(env):
    from trialerror.offload.lock import worker_state_dir

    assert state_runs_dir() == worker_state_dir() / "vastai" / "runs"
    with _lease(env):
        pass
    assert list(state_runs_dir(env["state"]).glob("VOCR-*.json"))
