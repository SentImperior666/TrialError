"""vast.ai executor guardrails (docs/VASTAI_EMBED_DESIGN.md sections 4-5).

The vast.ai HTTP API is replaced by :class:`FakeVast` (an in-memory server
behind the client's injectable ``http``), and the SSH channel by
:class:`FakeChannel`. No network, no GPU, no real key: the "key" file is
written by the test itself.
"""

from __future__ import annotations

import io
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from trialerror.ingest.backends import FakeEmbedBackend
from trialerror.offload import protocol
from trialerror.offload.marker import config_hash
from trialerror.offload.stage import EMBED_INPUT_NAME, EMBED_OUTPUT_NAME, build_chunks_payload, offload_embed_vectors
from trialerror.util.doctor import DoctorContext
from trialerror.vastai import checks as vchecks
from trialerror.vastai import guard
from trialerror.vastai.api import OfferUnavailable, VastClient, VastKeyMissing, read_api_key
from trialerror.vastai.lease import make_label, runs_dir
from trialerror.vastai.reaper import reap
from trialerror.vastai.runner import run_vastai
from trialerror.vastai.tiers import DEFAULT_TIER, PlanRefused, VastConfigError, load_vast_config

FAKE_KEY = "test-key-not-a-real-secret"
DIMS = 4
MODEL = "fake-4"
JOB = "JOB-vast-1"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeVast:
    def __init__(self, offers=None):
        self.offers = offers if offers is not None else [
            {"id": 11, "gpu_name": "RTX 3090", "gpu_ram": 24576, "dph_total": 0.30, "reliability": 0.99, "num_gpus": 1},
            {"id": 12, "gpu_name": "RTX 4090", "gpu_ram": 24576, "dph_total": 0.60, "reliability": 0.99, "num_gpus": 1},
            {"id": 13, "gpu_name": "RTX 4090", "gpu_ram": 24576, "dph_total": 1.10, "reliability": 0.99, "num_gpus": 1},
        ]
        self.instances: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.next_id = 5000
        self.gone: set[int] = set()  # offers taken between search and create
        self.v0_delete_gone = False  # vast.ai may retire v0 DELETE as it did v0 GET

    def http(self, method, url, headers, body, timeout):
        assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
        version = "v1" if "/api/v1" in url else "v0"
        path = url.split(f"/api/{version}", 1)[1]
        self.calls.append((method, path))
        # the live API, 2026-09-18: v0 instance listing is retired
        if method == "GET" and path.startswith("/instances") and version == "v0":
            return 410, {"error": "/api/v0/instances/ is deprecated. Use /api/v1/instances/ instead."}
        if method == "DELETE" and version == "v0" and self.v0_delete_gone:
            return 410, {"error": "deprecated"}
        if method == "POST" and path.startswith("/bundles"):
            return 200, {"offers": list(self.offers)}
        if method == "PUT" and path.startswith("/asks/"):
            if int(path.strip("/").split("/")[1]) in self.gone:
                return 400, {"error": "error 404/3603: no_such_ask  Instance type is not available."}
            self.next_id += 1
            req = json.loads(body)
            self.instances[self.next_id] = {
                "id": self.next_id, "label": req["label"], "actual_status": "running",
                "ssh_host": "10.0.0.1", "ssh_port": 2222, "onstart": req["onstart"],
            }
            return 200, {"success": True, "new_contract": self.next_id}
        if method == "GET" and path.startswith("/instances"):
            return 200, {"instances": list(self.instances.values()), "next_token": None}
        if method == "DELETE" and path.startswith("/instances/"):
            self.instances.pop(int(path.strip("/").split("/")[1]), None)
            return 200, {"success": True}
        return 404, {"msg": "no route"}

    def verbs(self, method):
        return [p for m, p in self.calls if m == method]


class FakeChannel:
    def __init__(self, *, block=False, fail_start=False):
        self.block = block
        self.fail_start = fail_start
        self.closed = threading.Event()
        self.files: dict[str, bytes] = {}
        self.embed = FakeEmbedBackend(dims=DIMS)

    def bootstrap(self, *, files, pip_packages):
        self.files = dict(files)

    def start(self, model_key):
        if self.fail_start:
            raise RuntimeError("remote CUDA out of memory")
        return {"ready": True, "dims": DIMS}

    def request(self, payload):
        if self.block:
            self.closed.wait(10)
            raise EOFError("channel closed")
        return {"vectors": self.embed.embed_batch(payload["texts"])}

    def close(self):
        self.closed.set()


# ---------------------------------------------------------------------------
# program + queue setup
# ---------------------------------------------------------------------------
def _setup(program_root: Path, tmp_path: Path, *, vast: str = "") -> dict:
    mod = tmp_path / "embeddings_local"
    mod.mkdir(exist_ok=True)
    (mod / "embed_backend.py").write_text("# stand-in module; shipped byte-for-byte to the instance\n")
    (program_root / "keys").mkdir(exist_ok=True)
    (program_root / "keys" / "vastai.key").write_text(FAKE_KEY + "\n")
    toml = "\n".join([
        "[program]", 'id = "vast-test"', "",
        "[ingest.embed]", 'backend = "offload"', f'model_key = "{MODEL}"', f"dims = {DIMS}", 'gpu = "vastai"', "",
        "[ingest.embed.query]", 'python_exe = "unused"', f"module_dir = {json.dumps(str(mod))}", "",
        "[vastai]", 'api_key_path = "keys/vastai.key"', "poll_interval_s = 0", vast, "",
    ])
    (program_root / "trialerror.toml").write_text(toml, encoding="utf-8")
    from trialerror.util.config import load_config

    raw = load_config(program_root / "trialerror.toml").raw
    embed_cfg = raw["ingest"]["embed"]
    chunks = [{"chunk_id": f"CHK-{i}", "seq": i, "text": f"chunk number {i} about retry budgets"} for i in range(3)]
    root = protocol.offload_root(program_root)
    protocol.queue_marker(
        root, job_id=JOB, stage="embed", doc_id="DOC-1",
        expect={"stage": "embed", "model_key": MODEL, "dims": DIMS, "chunk_count": 3,
                "chunk_ids": [c["chunk_id"] for c in chunks], "outputs": [EMBED_OUTPUT_NAME], "input_name": EMBED_INPUT_NAME},
        config_hash=config_hash(embed_cfg),
        inputs=[(EMBED_INPUT_NAME, build_chunks_payload(chunks))],
    )
    return {"raw": raw, "embed_cfg": embed_cfg, "chunks": chunks, "root": root}


def _run(program_root, env, store, fake, channel, **kw):
    client = VastClient(program_root / "keys" / "vastai.key", http=fake.http)
    return run_vastai(
        program_root, env["raw"], store=store, client=client, channel_factory=lambda inst: channel,
        sleep=lambda s: None, stderr=kw.pop("stderr", io.StringIO()), **kw,
    )


def _events(store, event_type):
    return store.ops.execute("SELECT payload FROM event WHERE type = ?", (event_type,)).fetchall()


# ---------------------------------------------------------------------------
# lifecycle: destroy on success / exception / TTL expiry
# ---------------------------------------------------------------------------
def test_success_publishes_verified_vectors_and_destroys(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path)
    fake, chan = FakeVast(), FakeChannel()
    summary = _run(program_root, env, store, fake, chan)
    assert summary["published"] == [JOB] and summary["destroyed"] is True
    assert fake.instances == {} and len(fake.verbs("DELETE")) >= 1
    assert summary["plan"]["gpu_name"] == "RTX 3090"  # mid tier by default; 4090 is not mid
    # the SAME sandbox-side verification a DEV result goes through
    ctx = SimpleNamespace(store=SimpleNamespace(program_root=program_root), job_id=JOB, set_checkpoint=lambda c: None)
    vecs = offload_embed_vectors(ctx, doc_id="DOC-1", chunks=env["chunks"], embed_cfg=env["embed_cfg"], model_key=MODEL, dims=DIMS)
    assert set(vecs) == {"CHK-0", "CHK-1", "CHK-2"}
    assert chan.files["embed_backend.py"].startswith(b"# stand-in module")
    rec = json.loads(next(runs_dir(program_root).glob("*.json")).read_text())
    assert rec["status"] == "destroyed"
    assert _events(store, "vastai_run")


def test_exception_still_destroys(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path)
    fake = FakeVast()
    with pytest.raises(RuntimeError, match="out of memory"):
        _run(program_root, env, store, fake, FakeChannel(fail_start=True))
    assert fake.instances == {} and fake.verbs("DELETE")
    assert protocol.list_pending(env["root"]) == [JOB]  # never claimed, still queued


def test_ttl_expiry_destroys_mid_job_and_returns_the_claim(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path, vast="startup_s = 0.3\nsafety = 1.0\ngrace_s = 0")
    fake = FakeVast()
    err = io.StringIO()
    summary = _run(program_root, env, store, fake, FakeChannel(block=True), stderr=err)
    assert summary["expired"] is True and summary["destroyed"] is True
    assert fake.instances == {}
    assert protocol.list_pending(env["root"]) == [JOB]  # handed back unrun, no attempt burned
    assert "TTL" in err.getvalue()


def test_label_and_onstart_carry_the_deadline(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path)
    fake = FakeVast()
    seen = {}
    orig = fake.http

    def spy(method, url, headers, body, timeout):
        if method == "PUT":
            seen.update(json.loads(body))
        return orig(method, url, headers, body, timeout)

    fake.http = spy
    _run(program_root, env, store, fake, FakeChannel())
    assert seen["label"].startswith("trialerror|") and re.search(r"\|\d+$", seen["label"])
    assert "kill -TERM 1" in seen["onstart"]


# ---------------------------------------------------------------------------
# dollar cap / TTL cap / config
# ---------------------------------------------------------------------------
def test_dollar_cap_refuses_before_any_instance_is_created(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path, vast="max_job_usd = 0.01")
    fake = FakeVast()
    with pytest.raises(PlanRefused, match="per-job cap"):
        _run(program_root, env, store, fake, FakeChannel())
    assert fake.verbs("PUT") == [] and fake.instances == {}


def test_keep_alive_is_refused_by_name(program_root):
    for key in ("keep_alive", "reuse_instance", "ttl_extend"):
        with pytest.raises(VastConfigError, match="no keep-alive"):
            load_vast_config({"vastai": {key: True}}, program_root)


def test_default_tier_is_mid_and_ttl_cap_cannot_be_raised(program_root):
    cfg = load_vast_config({"vastai": {"ttl_cap_s": 99999}}, program_root)
    assert DEFAULT_TIER == "mid" and cfg.tier == "mid"
    assert cfg.ttl_cap_s == 4 * 3600 and cfg.notes


def test_gpu_switch_is_outside_the_config_hash():
    base = {"backend": "offload", "model_key": "qwen3-4b", "dims": 2048}
    assert config_hash(base) == config_hash({**base, "gpu": "vastai", "query": {"module_dir": "x"}})
    assert config_hash(base) != config_hash({**base, "dims": 1024})


# ---------------------------------------------------------------------------
# high tier
# ---------------------------------------------------------------------------
def test_high_tier_refused_without_approval(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path, vast='tier = "high"')
    fake = FakeVast()
    with pytest.raises(guard.HighTierRefused, match="no approval"):
        _run(program_root, env, store, fake, FakeChannel())
    assert fake.calls == []  # not even an offer search


def test_approval_cannot_be_minted_without_a_tty(program_root, tmp_path):
    _setup(program_root, tmp_path)
    with pytest.raises(guard.HighTierRefused, match="interactive terminal"):
        guard.mint_high_tier_approval(program_root, program_root / "keys" / "vastai.key", hours=1, max_job_usd=5)
    assert not guard.approval_path(program_root).exists()


def _mint(program_root, monkeypatch, *, max_job_usd=5.0):
    monkeypatch.setattr(guard, "_is_interactive", lambda: True)
    out = io.StringIO()

    def answer(_prompt):
        return re.search(r"Type (\w+) to confirm", out.getvalue()).group(1)

    return guard.mint_high_tier_approval(
        program_root, program_root / "keys" / "vastai.key", hours=2, max_job_usd=max_job_usd, input_fn=answer, out=out
    )


def test_hand_written_or_edited_approval_is_refused(program_root, tmp_path, monkeypatch):
    _setup(program_root, tmp_path)
    path = guard.approval_path(program_root)
    path.write_text(json.dumps({"tier": "high", "expires": "2099-01-01T00:00:00+00:00", "mac": "0" * 64}))
    with pytest.raises(guard.HighTierRefused, match="invalid signature"):
        guard.verify_high_tier_approval(program_root, program_root / "keys" / "vastai.key")
    _mint(program_root, monkeypatch)
    body = json.loads(path.read_text())
    body["max_job_usd"] = 500.0  # an agent raising the approved spend
    path.write_text(json.dumps(body))
    with pytest.raises(guard.HighTierRefused, match="invalid signature"):
        guard.verify_high_tier_approval(program_root, program_root / "keys" / "vastai.key")


def test_approved_high_tier_prints_banner_and_records_event(store, program_root, tmp_path, monkeypatch):
    env = _setup(program_root, tmp_path, vast='tier = "high"')
    _mint(program_root, monkeypatch)
    fake, err = FakeVast(), io.StringIO()
    summary = _run(program_root, env, store, fake, FakeChannel(), stderr=err)
    assert "HIGH-TIER vast.ai GPU" in err.getvalue()
    assert summary["plan"]["gpu_name"] == "RTX 4090" and summary["destroyed"] is True
    assert len(_events(store, "vastai_high_tier_use")) == 1
    result = vchecks.check_vastai_high_tier(DoctorContext(program_root=program_root))
    assert result.status == "warn" and "configured" in result.message and "high-tier run" in result.message


# ---------------------------------------------------------------------------
# reaper + doctor
# ---------------------------------------------------------------------------
def test_reaper_destroys_orphans_and_past_deadline_only(program_root, tmp_path):
    _setup(program_root, tmp_path)
    fp = guard.program_fingerprint(program_root)
    now = 1_900_000_000
    fake = FakeVast()
    fake.instances = {
        1: {"id": 1, "label": make_label("f" * 64, "VAST-other", now - 10)},  # other program, past deadline
        2: {"id": 2, "label": make_label(fp, "VAST-dead", now + 3600)},       # ours, owner process died
        3: {"id": 3, "label": make_label(fp, "VAST-live", now + 3600)},       # ours, owner alive -> keep
        4: {"id": 4, "label": "someone-else's instance"},                      # not ours -> never touch
        5: {"id": 5, "label": "trialerror|garbled"},                            # our prefix, malformed
        6: {"id": 6, "label": make_label(fp, "VAST-norecord", now + 3600)},   # ours, no local record
    }
    d = runs_dir(program_root)
    d.mkdir(parents=True)
    import socket

    for run_id, pid in (("VAST-dead", 111), ("VAST-live", 222)):
        (d / f"{run_id}.json").write_text(json.dumps(
            {"run_id": run_id, "pid": pid, "host": socket.gethostname(), "status": "running", "deadline_epoch": now + 3600}
        ))
    client = VastClient(program_root / "keys" / "vastai.key", http=fake.http)
    out = reap(client, program_root, clock=lambda: now, alive=lambda pid: pid == 222)
    reasons = {e["instance_id"]: e["reason"] for e in out}
    assert reasons == {1: "past_deadline", 2: "owner_dead", 5: "malformed_label", 6: "no_run_record"}
    assert set(fake.instances) == {3, 4}
    assert json.loads((d / "VAST-dead.json").read_text())["status"] == "reaped"


def test_doctor_reports_live_and_overdue_instances(program_root, tmp_path, monkeypatch):
    _setup(program_root, tmp_path)
    ctx = DoctorContext(program_root=program_root)
    fake = FakeVast()
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=fake.http))
    assert vchecks.check_vastai_live_instances(ctx).status == "pass"
    fp = guard.program_fingerprint(program_root)
    fake.instances = {7: {"id": 7, "label": make_label(fp, "VAST-x", 4_000_000_000)}}
    assert vchecks.check_vastai_live_instances(ctx).status == "warn"
    fake.instances = {7: {"id": 7, "label": make_label(fp, "VAST-x", 1)}}
    assert vchecks.check_vastai_live_instances(ctx).status == "fail"
    assert vchecks.check_vastai_high_tier(ctx).status == "pass"


# ---------------------------------------------------------------------------
# live-API findings, 2026-09-18 (first live use)
# ---------------------------------------------------------------------------
def test_instance_listing_uses_v1_because_v0_is_retired(program_root, tmp_path):
    _setup(program_root, tmp_path)
    fake = FakeVast()
    client = VastClient(program_root / "keys" / "vastai.key", http=fake.http)
    assert client.list_instances() == []
    assert ("GET", "/instances/?owner=me") in fake.calls


def test_destroy_falls_back_to_v1_when_v0_is_retired(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path)
    fake = FakeVast()
    fake.v0_delete_gone = True
    summary = _run(program_root, env, store, fake, FakeChannel())
    assert summary["destroyed"] is True and fake.instances == {}


def test_a_taken_offer_falls_through_to_the_next_one(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path)
    fake = FakeVast(offers=[
        {"id": 11, "gpu_name": "RTX 3090", "gpu_ram": 24576, "dph_total": 0.30, "reliability": 0.99, "num_gpus": 1},
        {"id": 14, "gpu_name": "RTX 3090", "gpu_ram": 24576, "dph_total": 0.35, "reliability": 0.99, "num_gpus": 1},
    ])
    fake.gone = {11}
    err = io.StringIO()
    summary = _run(program_root, env, store, fake, FakeChannel(), stderr=err)
    assert summary["published"] == [JOB] and summary["plan"]["offer_id"] == 14
    assert fake.instances == {} and "offer 11 was taken" in err.getvalue()


def test_all_offers_taken_rents_nothing_and_gives_up(store, program_root, tmp_path):
    env = _setup(program_root, tmp_path)
    fake = FakeVast()
    fake.gone = {11, 12, 13}
    with pytest.raises((OfferUnavailable, PlanRefused)):
        _run(program_root, env, store, fake, FakeChannel())
    assert fake.instances == {}
    assert protocol.list_pending(env["root"]) == [JOB]


def test_a_key_passed_where_the_path_belongs_is_never_echoed():
    key_like = "0123456789abcdef" * 4
    with pytest.raises(VastKeyMissing) as info:
        read_api_key(key_like)
    assert key_like not in str(info.value)
    with pytest.raises(VastKeyMissing) as info:
        VastClient(key_like, http=lambda *a: (200, {})).list_instances()
    assert key_like not in str(info.value)


def test_reaper_finds_an_unlabelled_instance_through_its_run_record(program_root, tmp_path):
    # Whether vast.ai echoes the create-time label back was not verified before
    # first live use; the instance id in our own run record must still find it.
    _setup(program_root, tmp_path)
    now = 1_900_000_000
    fake = FakeVast()
    fake.instances = {
        8: {"id": 8, "label": None},   # ours per record, record says finished -> destroy
        9: {"id": 9, "label": ""},     # ours per record, run live, owner alive -> keep
        10: {"id": 10, "label": None},  # no record names it -> never touch
    }
    d = runs_dir(program_root)
    d.mkdir(parents=True)
    import socket

    for run_id, iid, status in (("VAST-done", 8, "destroyed"), ("VAST-on", 9, "running")):
        (d / f"{run_id}.json").write_text(json.dumps({
            "run_id": run_id, "instance_id": iid, "pid": 222, "host": socket.gethostname(),
            "status": status, "deadline_epoch": now + 3600,
        }))
    client = VastClient(program_root / "keys" / "vastai.key", http=fake.http)
    out = reap(client, program_root, clock=lambda: now, alive=lambda pid: pid == 222)
    assert {e["instance_id"]: e["reason"] for e in out} == {8: "run_finished"}
    assert set(fake.instances) == {9, 10}


def test_doctor_warns_when_blind_and_on_foreign_instances(program_root, tmp_path, monkeypatch):
    _setup(program_root, tmp_path)
    ctx = DoctorContext(program_root=program_root)

    def blind_http(method, url, headers, body, timeout):
        return 410, {"error": "/api/v1/instances/ is deprecated"}

    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=blind_http))
    result = vchecks.check_vastai_live_instances(ctx)
    assert result.status == "warn" and "cannot be ruled out" in result.message
    fake = FakeVast()
    fake.instances = {4: {"id": 4, "label": "someone-else's instance"}}
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=fake.http))
    result = vchecks.check_vastai_live_instances(ctx)
    assert result.status == "warn" and "not TrialError's" in result.message
    assert fake.instances == {4: {"id": 4, "label": "someone-else's instance"}}  # reported, never touched


def test_a_taken_offer_leaves_a_clean_record_not_a_doctor_failure(store, program_root, tmp_path, monkeypatch):
    env = _setup(program_root, tmp_path)
    fake = FakeVast()
    fake.gone = {11, 12, 13}
    with pytest.raises((OfferUnavailable, PlanRefused)):
        _run(program_root, env, store, fake, FakeChannel())
    recs = [json.loads(f.read_text()) for f in runs_dir(program_root).glob("*.json")]
    assert recs and {r["status"] for r in recs} == {"offer_taken"}
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=fake.http))
    assert vchecks.check_vastai_live_instances(DoctorContext(program_root=program_root)).status == "pass"


def test_an_old_create_failure_is_settled_only_by_a_successful_listing(program_root, tmp_path, monkeypatch):
    _setup(program_root, tmp_path)
    fp = guard.program_fingerprint(program_root)
    now = 1_900_000_000
    d = runs_dir(program_root)
    d.mkdir(parents=True)
    past = 1_000_000_000  # before both the doctor's real clock and the reaper's injected one
    label = make_label(fp, "VAST-cf", past)
    (d / "VAST-cf.json").write_text(json.dumps({
        "run_id": "VAST-cf", "label": label, "status": "create_failed", "instance_id": None,
        "pid": 1, "host": "elsewhere", "deadline_epoch": past,
    }))
    ctx = DoctorContext(program_root=program_root)

    def blind_http(method, url, headers, body, timeout):
        return 503, {"error": "unavailable"}

    # blind: cannot rule the instance out -> still a failure
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=blind_http))
    assert vchecks.check_vastai_live_instances(ctx).status == "fail"
    # the create DID land after all: the labelled instance is listed -> failure, and reap destroys it
    fake = FakeVast()
    fake.instances = {21: {"id": 21, "label": label}}
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=fake.http))
    assert vchecks.check_vastai_live_instances(ctx).status == "fail"
    # listed and absent -> the doctor passes, and a real reap settles the record
    fake.instances = {}
    assert vchecks.check_vastai_live_instances(ctx).status == "pass"
    client = VastClient(program_root / "keys" / "vastai.key", http=fake.http)
    reap(client, program_root, dry_run=True, clock=lambda: now)
    assert json.loads((d / "VAST-cf.json").read_text())["status"] == "create_failed"  # dry run writes nothing
    reap(client, program_root, clock=lambda: now)
    assert json.loads((d / "VAST-cf.json").read_text())["status"] == "absent"
