"""The superset (round 3): the public TrialError copy's embedding backend and
this branch's OCR backend in one package. The public ``tests/test_vastai.py``
runs unchanged beside these; the tests here pin what the merge adds or must
keep: C5 (the offload config hash of a table without routing keys is the one
the tree had before the merge), the ``remote.py`` module beside the
``remote/`` data directory, one ``LeaseExpired``, one ``VastConfigError``,
the two reaper policies each in its own lane, and the CLI's two forms.
"""

from __future__ import annotations

import pytest

from tests._vastai_fakes import isolated_state, network_tripwire  # noqa: F401
from trialerror.offload.marker import ROUTING_KEYS, config_hash, gpu_executor


@pytest.fixture(autouse=True)
def _no_network(network_tripwire):  # noqa: F811
    yield


# ---------------------------------------------------------------------------
# C5: config_hash of a table without routing keys is the pre-merge value
# ---------------------------------------------------------------------------
#: Computed with the config_hash of the tree before the merge (the branch's
#: base), for tables that carry no routing key. The merge drops ROUTING_KEYS
#: before hashing, so these must never move.
_PRE_MERGE_HASHES = {
    "none": (None, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    "empty": ({}, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    "embed": (
        {"backend": "offload", "model_key": "qwen3-4b", "dims": 2048},
        "b282b3f766927a1626d5f77f68ba23d1f26d378c523ea89e8e9f6b0b56f84e5f",
    ),
    "ocr": (
        {"backend": "offload", "expect_backend": "marker", "executor": "vastai", "range_pages": 20},
        "d3da6af4628ff29e8bc31cf023ee9247fc85fa1c02d7e9170dc667145adf7851",
    ),
    "nested": (
        {"backend": "offload", "model_key": "fake-4", "dims": 4, "batch": {"size": 8, "names": ["a", "é"]}},
        "60e5c59fa04d985ea78663d4212df4565ccb28c6f30d3c29cb1edc367f7d4280",
    ),
}


def test_c5_a_table_without_routing_keys_hashes_as_before_the_merge():
    for name, (table, expected) in _PRE_MERGE_HASHES.items():
        assert config_hash(table) == expected, name


def test_c5_routing_keys_are_dropped_and_nothing_else():
    assert ROUTING_KEYS == ("gpu", "query")
    base = {"backend": "offload", "model_key": "qwen3-4b", "dims": 2048}
    expected = _PRE_MERGE_HASHES["embed"][1]
    assert config_hash({**base, "gpu": "vastai"}) == expected
    assert config_hash({**base, "gpu": "dev", "query": {"module_dir": "x"}}) == expected
    assert config_hash({**base, "executor": "vastai"}) != expected  # the OCR table's executor key is not routing


def test_gpu_executor_defaults_to_dev_and_refuses_a_typo():
    assert gpu_executor(None) == "dev" and gpu_executor({"gpu": "vastai"}) == "vastai"
    with pytest.raises(ValueError, match="gpu"):
        gpu_executor({"gpu": "vast"})


# ---------------------------------------------------------------------------
# C-1 / C-9: the remote.py module beside the remote/ data directory; one
# LeaseExpired, one VastConfigError
# ---------------------------------------------------------------------------
def test_c1_remote_module_and_remote_data_directory_both_work():
    from pathlib import Path

    import trialerror.vastai.remote as remote_mod
    from trialerror.vastai import ocr

    # the import resolves to the module, not to the data directory
    assert Path(remote_mod.__file__).name == "remote.py"
    assert remote_mod.REMOTE_DIR == "/root/te" and "load_backend" in remote_mod.SERVE_SOURCE
    assert callable(remote_mod.SshChannel) and callable(remote_mod.RemoteEmbedBackend)
    # the OCR lane still reads its instance-side tools from the directory, by path
    data_dir = Path(ocr.__file__).resolve().parent / "remote"
    assert data_dir.is_dir() and not (data_dir / "__init__.py").exists()
    for name in ocr.TOOL_FILES:
        assert (data_dir / name).is_file(), name
        assert ocr._tool_bytes(name) == (data_dir / name).read_bytes().replace(b"\r\n", b"\n")


def test_c9_one_lease_expired_raised_by_both_leases_and_caught_by_the_runner():
    import trialerror.vastai.lease as lease_mod
    import trialerror.vastai.remote as remote_mod
    import trialerror.vastai.runner as runner_mod

    assert remote_mod.LeaseExpired is lease_mod.LeaseExpired is runner_mod.LeaseExpired
    assert issubclass(lease_mod.LeaseExpired, KeyboardInterrupt)
    assert "LeaseExpired" not in vars(remote_mod) or remote_mod.LeaseExpired.__module__ == "trialerror.vastai.lease"


def test_one_vast_config_error_is_a_vast_error_and_a_value_error():
    from trialerror.vastai import errors, tiers

    assert tiers.VastConfigError is errors.VastConfigError
    assert issubclass(tiers.VastConfigError, errors.VastError) and issubclass(tiers.VastConfigError, ValueError)
    # PlanRefused is the public class, not an alias of the OCR lane's refusal
    assert tiers.PlanRefused is not errors.VastPlanRefused and not issubclass(tiers.PlanRefused, errors.VastPlanRefused)


def test_lease_public_names_and_the_ocr_lane_renames(tmp_path):
    from trialerror.vastai import lease

    assert lease.runs_dir(tmp_path) == tmp_path / "offload" / "vastai" / "runs"
    assert lease.state_runs_dir(tmp_path) == tmp_path / "runs"
    assert lease.read_run_records(tmp_path) == [] and lease.read_state_run_records(tmp_path) == []
    assert lease.InstanceLease is not lease.OcrInstanceLease


def test_search_offers_takes_the_public_keywords_or_the_ocr_query_but_not_both(tmp_path):
    import json

    from trialerror.vastai.api import VastClient

    key = tmp_path / "vastai.key"
    key.write_text("fake-key-for-tests\n", encoding="utf-8")
    bodies = []

    def http(method, url, headers, body, timeout):
        bodies.append(json.loads(body))
        return 200, {"offers": [{"id": 1}, "junk"]}

    client = VastClient(key, http=http)
    # the public keyword form: the public body, the public answer (as listed)
    out = client.search_offers(gpu_names=["RTX 3090"], min_vram_gb=16, max_dph=0.45, min_reliability=0.97)
    assert out == [{"id": 1}, "junk"]
    assert bodies[-1]["gpu_name"] == {"in": ["RTX_3090", "RTX 3090"]} and bodies[-1]["limit"] == 64
    # the OCR lane's query form: its own body, dict offers only
    assert client.search_offers({"q": 1}) == [{"id": 1}] and bodies[-1] == {"q": 1}
    with pytest.raises(TypeError, match="do not mix"):
        client.search_offers({"q": 1}, gpu_names=["RTX 3090"])
    with pytest.raises(TypeError, match="missing"):
        client.search_offers(gpu_names=["RTX 3090"])


# ---------------------------------------------------------------------------
# the two reaper policies, each for its own lane (V2-F01, C-6, C-7, V1-F07)
# ---------------------------------------------------------------------------
_NOW = 1_900_000_000.0


def _reaper_account(tmp_path):
    from tests._vastai_fakes import FakeVast, write_key
    from trialerror.vastai.api import VastClient

    root = tmp_path / "devroot"
    fake = FakeVast(offers=[], key="k" * 8)
    return root, fake, VastClient(write_key(root / "keys", "k" * 8), http=fake.http)


def _put(fake, iid, label):
    fake.instances[iid] = {"id": iid, "label": label, "actual_status": "running", "ssh_host": "10.0.0.1",
                           "ssh_port": 2222}


def _ocr_record(state, root, run_id, iid, *, pid, deadline=_NOW + 3600, status="running", host="devhost"):
    from trialerror.vastai.guard import program_fingerprint
    from trialerror.vastai.lease import make_label, write_run_record

    fp = program_fingerprint(root)
    write_run_record(state, {"run_id": run_id, "lease_id": run_id, "program_fp": fp, "pid": pid, "host": host,
                             "label": make_label(fp, run_id, deadline), "deadline_epoch": int(deadline),
                             "status": status, "instance_id": iid})


def test_v1_f07_a_live_embedding_lane_instance_under_the_same_root_is_not_destroyed(tmp_path, isolated_state):  # noqa: F811
    """Adapted from V1's claim C27 (round 3): one directory is both the
    embedding lane's program root and the OCR lane's backend-config-root. The
    OCR lane's reaper destroys only VOCR- instances with no live record."""
    import json

    from trialerror.vastai.guard import program_fingerprint
    from trialerror.vastai.lease import make_label
    from trialerror.vastai.ledger import Ledger
    from trialerror.vastai.reaper import reap_ocr

    root, fake, client = _reaper_account(tmp_path)
    fp = program_fingerprint(root)  # the embedding lane fingerprints its program root the same way
    _put(fake, 6301, make_label(fp, "VAST-embed-live", _NOW + 3600))
    _put(fake, 6302, make_label(fp, "VAST-embed-late", _NOW - 10))  # past deadline: still the other lane's
    _put(fake, 6303, make_label(fp, "VOCR-00000000000000b1", _NOW + 3600))  # this lane's, no record: destroyed
    runs = root / "offload" / "vastai" / "runs"  # where the embedding lane keeps its own run records
    runs.mkdir(parents=True)
    (runs / "VAST-embed-live.json").write_text(json.dumps({"run_id": "VAST-embed-live", "status": "running",
                                                           "pid": 333, "host": "devhost"}), encoding="utf-8")
    entries = reap_ocr(client, config_root=root, state_dir=isolated_state, ledger=Ledger(isolated_state),
                       clock=lambda: _NOW, alive=lambda pid: True, host="devhost", log=lambda m: None)
    assert 6301 in fake.instances and 6302 in fake.instances, f"an embedding-lane instance was destroyed: {entries}"
    assert 6303 not in fake.instances
    by_id = {e["instance_id"]: (e["reason"], e["action"]) for e in entries}
    assert by_id == {
        6301: ("other_lane_live", "reported"),
        6302: ("other_lane_past_deadline", "reported"),
        6303: ("no_run_record", "destroyed"),
    }


def test_c7_the_public_reap_never_destroys_a_live_ocr_lease(tmp_path, isolated_state):  # noqa: F811
    """The public verb keeps the public policy, and reads the OCR lane's run
    records (the worker's state directory, by default) too: a live OCR lease
    is never destroyed, whatever the public policy would say of its label."""
    from trialerror.vastai.guard import program_fingerprint
    from trialerror.vastai.lease import make_label
    from trialerror.vastai.reaper import reap

    root, fake, client = _reaper_account(tmp_path)
    fp = program_fingerprint(root)
    _put(fake, 7001, make_label(fp, "VOCR-00000000000000c1", _NOW + 3600))  # live OCR lease, no public record
    _put(fake, 7002, None)  # unlabelled, a live OCR record names it by id
    _put(fake, 7003, make_label(fp, "VOCR-00000000000000c3", _NOW + 3600))  # OCR record, owner dead
    _put(fake, 7004, make_label(fp, "VOCR-00000000000000c4", _NOW - 5))  # OCR record, past its deadline
    import socket

    here = socket.gethostname()  # the records' owner runs on this host, so its pid decides
    _ocr_record(isolated_state, root, "VOCR-00000000000000c1", 7001, pid=222, host=here)
    _ocr_record(isolated_state, root, "VOCR-00000000000000c2", 7002, pid=222, host=here)
    _ocr_record(isolated_state, root, "VOCR-00000000000000c3", 7003, pid=111, host=here)
    _ocr_record(isolated_state, root, "VOCR-00000000000000c4", 7004, pid=222, host=here, deadline=_NOW - 5)
    out = reap(client, root, clock=lambda: _NOW, alive=lambda pid: pid == 222)  # state_dir: the worker's default
    assert {e["instance_id"]: e["reason"] for e in out} == {7003: "no_run_record", 7004: "past_deadline"}
    assert set(fake.instances) == {7001, 7002}


# ---------------------------------------------------------------------------
# the CLI: the public verbs and flags beside the OCR lane's forms (C-5)
# ---------------------------------------------------------------------------
_EMBED_JOB = "JOB-superset-1"


def _embed_program(tmp_path, *, vast: str = ""):
    """A program root whose embed stage is offloaded to vast.ai, with one
    pending embed marker; the "key" is written by the test itself."""
    import json

    from trialerror.offload import protocol
    from trialerror.offload.stage import EMBED_INPUT_NAME, EMBED_OUTPUT_NAME, build_chunks_payload

    root = tmp_path / "program"
    (root / "keys").mkdir(parents=True)
    (root / "keys" / "vastai.key").write_text("fake-key-for-tests\n", encoding="utf-8")
    mod = tmp_path / "embeddings_local"
    mod.mkdir()
    (mod / "embed_backend.py").write_text("# stand-in module\n", encoding="utf-8")
    embed_cfg = {"backend": "offload", "model_key": "fake-4", "dims": 4, "gpu": "vastai"}
    toml = "\n".join([
        "[program]", 'id = "vast-superset"', "",
        "[ingest.embed]", 'backend = "offload"', 'model_key = "fake-4"', "dims = 4", 'gpu = "vastai"', "",
        "[ingest.embed.query]", f"module_dir = {json.dumps(str(mod))}", "",
        "[vastai]", 'api_key_path = "keys/vastai.key"', vast, "",
    ])
    (root / "trialerror.toml").write_text(toml, encoding="utf-8")
    chunks = [{"chunk_id": f"CHK-{i}", "seq": i, "text": f"chunk {i}"} for i in range(2)]
    protocol.queue_marker(
        protocol.offload_root(root), job_id=_EMBED_JOB, stage="embed", doc_id="DOC-1",
        expect={"stage": "embed", "model_key": "fake-4", "dims": 4, "chunk_count": 2,
                "chunk_ids": [c["chunk_id"] for c in chunks], "outputs": [EMBED_OUTPUT_NAME],
                "input_name": EMBED_INPUT_NAME},
        config_hash=config_hash(embed_cfg),
        inputs=[(EMBED_INPUT_NAME, build_chunks_payload(chunks))],
    )
    return root


def _cli(*argv):
    from trialerror.cli import build_parser

    args = build_parser().parse_args(["vastai", *argv])
    return args.handler(args)


def _cli_fake(monkeypatch, fake):
    import trialerror.cli.vastai as cli_vastai
    from trialerror.vastai.api import VastClient

    monkeypatch.setattr(cli_vastai, "_client_factory", lambda key_path: VastClient(key_path, http=fake.http))


def test_every_verb_takes_program_root_as_the_backend_config_root_and_the_help_is_a_superset():
    import argparse

    from trialerror.cli import build_parser
    from trialerror.cli import vastai as cli_vastai

    parser = build_parser()
    group = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0].choices["vastai"]
    verbs = [a for a in group._actions if isinstance(a, argparse._SubParsersAction)][0].choices
    assert {"plan", "run", "reap", "approve-high"} <= set(verbs)  # the public verbs
    for name, sub in verbs.items():
        by_flag = {o: a for a in sub._actions for o in a.option_strings}
        assert by_flag["--program-root"] is by_flag["--backend-config-root"], name
        assert by_flag["--program-root"].dest == "program_root" and "--platform-root" in by_flag, name
    public_help = "Rent a vast.ai GPU for pending embed jobs (plan/run/reap); create -> run -> destroy, never kept alive."
    assert cli_vastai.HELP.startswith(public_help)
    assert {"--max-jobs", "--input", "--json"} <= {o for a in verbs["plan"]._actions for o in a.option_strings}
    assert "--max-jobs" in {o for a in verbs["run"]._actions for o in a.option_strings}
    assert {"--dry-run", "--ocr"} <= {o for a in verbs["reap"]._actions for o in a.option_strings}
    assert {"--hours", "--max-job-usd"} <= {o for a in verbs["approve-high"]._actions for o in a.option_strings}


def test_plan_without_input_is_the_public_embed_plan_and_rents_nothing(tmp_path, monkeypatch, isolated_state):  # noqa: F811
    from tests._vastai_fakes import FakeVast, make_offer

    root = _embed_program(tmp_path)
    fake = FakeVast(offers=[make_offer(11, gpu_name="RTX 3090", dph_total=0.30)], key="fake-key-for-tests",
                    ignore_filters=True)
    _cli_fake(monkeypatch, fake)
    env = _cli("plan", "--program-root", str(root), "--max-jobs", "5")
    assert env["ok"] is True, env
    assert env["result"]["jobs"] == [_EMBED_JOB] and env["result"]["plan"]["gpu_name"] == "RTX 3090"
    assert fake.verbs("PUT") == [] and fake.instances == {}
    both = _cli("plan", "--program-root", str(root), "--input", str(tmp_path / "x.pdf"), "--max-jobs", "5")
    assert both["ok"] is False and both["error"]["code"] == "bad_arguments" and both["nextActions"]


def test_the_public_embed_verbs_refuse_by_name_with_next_actions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = _cli("run")
    assert env["ok"] is False and env["error"]["code"] == "no_program_root" and env["nextActions"]
    root = _embed_program(tmp_path, vast="keep_alive = true")
    for verb in ("plan", "run", "reap"):
        env = _cli(verb, "--program-root", str(root))
        assert env["ok"] is False and "keep-alive" in env["error"]["message"] and env["nextActions"], verb


def test_reap_public_form_and_ocr_form_answer_each_with_its_own_policy(tmp_path, monkeypatch, isolated_state):  # noqa: F811
    from tests._vastai_fakes import FakeVast
    from trialerror.vastai.lease import make_label

    root = _embed_program(tmp_path)
    fake = FakeVast(offers=[], key="fake-key-for-tests")
    fake.instances = {31: {"id": 31, "label": make_label("f" * 64, "VAST-other", 1_000), "actual_status": "running"}}
    _cli_fake(monkeypatch, fake)
    public = _cli("reap", "--program-root", str(root), "--dry-run")
    assert public["ok"] is True, public
    assert public["result"]["dry_run"] is True and public["result"]["count"] == 1
    assert [(e["instance_id"], e["reason"]) for e in public["result"]["reaped"]] == [(31, "past_deadline")]
    ocr = _cli("reap", "--ocr", "--program-root", str(root), "--dry-run")
    assert ocr["ok"] is True, ocr
    assert [(e["instance_id"], e["reason"], e["action"]) for e in ocr["result"]["entries"]] == [
        (31, "foreign_past_deadline", "reported")
    ]
    assert fake.verbs("DELETE") == [] and 31 in fake.instances


# ---------------------------------------------------------------------------
# the doctor checks: a union of both lanes' sources (C-8)
# ---------------------------------------------------------------------------
def test_the_checks_read_both_record_stores_and_both_high_tier_sources(
    store, program_root, isolated_state, monkeypatch  # noqa: F811
):
    import json

    from tests._vastai_fakes import FakeVast
    from trialerror.events.api import append_event
    from trialerror.util.doctor import DoctorContext
    from trialerror.vastai import checks as vchecks
    from trialerror.vastai.api import VastClient
    from trialerror.vastai.guard import program_fingerprint
    from trialerror.vastai.lease import make_label, runs_dir, write_run_record
    from trialerror.vastai.ledger import Ledger

    (program_root / "keys").mkdir(exist_ok=True)
    (program_root / "keys" / "vastai.key").write_text("fake-key-for-tests\n", encoding="utf-8")
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "vast-union"\n\n[vastai]\napi_key_path = "keys/vastai.key"\n', encoding="utf-8"
    )
    ctx = DoctorContext(program_root=program_root)
    assert vchecks.check_vastai_high_tier(ctx).status == "pass"
    append_event(store, event_type="vastai_high_tier_use", payload={"run_id": "VAST-h"})
    Ledger(isolated_state).append("intent", lease_id="VOCR-h", sha256="f" * 64, bytes=1, host_id=1,
                                  datacenter=True, verified=True, worst_usd=2.0, tier="high")
    result = vchecks.check_vastai_high_tier(ctx)
    assert result.status == "warn" and "high-tier run" in result.message and "high-tier lease" in result.message

    fp = program_fingerprint(program_root)
    fake = FakeVast(offers=[], key="fake-key-for-tests")
    monkeypatch.setattr(vchecks, "_client_factory", lambda kp: VastClient(kp, http=fake.http))
    assert vchecks.check_vastai_live_instances(ctx).status == "pass"
    # the embedding lane's record (under the program root): a destroy that failed
    d = runs_dir(program_root)
    d.mkdir(parents=True)
    (d / "VAST-e.json").write_text(json.dumps({"run_id": "VAST-e", "status": "destroy_failed", "instance_id": 41,
                                               "deadline_epoch": 4_000_000_000,
                                               "label": make_label(fp, "VAST-e", 4e9)}), encoding="utf-8")
    result = vchecks.check_vastai_live_instances(ctx)
    assert result.status == "fail" and [e["run_id"] for e in result.details["overdue_or_failed"]] == ["VAST-e"]
    (d / "VAST-e.json").unlink()
    # the OCR lane's record (the worker's state directory): a live lease
    write_run_record(isolated_state, {"run_id": "VOCR-00000000000000d1", "program_fp": fp, "status": "running",
                                      "instance_id": 42, "deadline_epoch": 4_000_000_000,
                                      "label": make_label(fp, "VOCR-00000000000000d1", 4e9)})
    result = vchecks.check_vastai_live_instances(ctx)
    assert result.status == "warn" and [e["run_id"] for e in result.details["live"]] == ["VOCR-00000000000000d1"]


# ---------------------------------------------------------------------------
# the embedding lane's events (row 87): a destroy that is not confirmed is
# recorded as vastai_destroy_failed, not vastai_run
# ---------------------------------------------------------------------------
def test_a_run_whose_destroy_is_not_confirmed_records_vastai_destroy_failed(store, tmp_path, isolated_state):  # noqa: F811
    import io
    import json

    from trialerror.ingest.backends import FakeEmbedBackend
    from trialerror.util.config import load_config
    from trialerror.vastai.api import VastClient
    from trialerror.vastai.lease import runs_dir
    from trialerror.vastai.runner import run_vastai

    root = _embed_program(tmp_path)  # the store fixture's program root
    raw = load_config(root / "trialerror.toml").raw
    offer = {"id": 11, "gpu_name": "RTX 3090", "gpu_ram": 24576, "dph_total": 0.30, "reliability": 0.99, "num_gpus": 1}
    instances: dict[int, dict] = {}
    deletes: list[str] = []

    def http(method, url, headers, body, timeout):
        if method == "POST" and "/bundles" in url:
            return 200, {"offers": [offer]}
        if method == "PUT" and "/asks/" in url:
            instances[901] = {"id": 901, "label": json.loads(body)["label"], "actual_status": "running",
                              "ssh_host": "10.0.0.1", "ssh_port": 2222}
            return 200, {"success": True, "new_contract": 901}
        if method == "GET" and "/api/v1/instances" in url:
            return 200, {"instances": list(instances.values()), "next_token": None}
        if method == "DELETE":
            deletes.append(url)
            return 200, {"success": True}  # accepted, yet the instance stays listed
        return 404, {"msg": "no route"}

    class Channel:
        def __init__(self):
            self.embed = FakeEmbedBackend(dims=4)

        def bootstrap(self, *, files, pip_packages):
            pass

        def start(self, model_key):
            return {"ready": True, "dims": 4}

        def request(self, payload):
            return {"vectors": self.embed.embed_batch(payload["texts"])}

        def close(self):
            pass

    channel = Channel()
    summary = run_vastai(root, raw, store=store, client=VastClient(root / "keys" / "vastai.key", http=http),
                         channel_factory=lambda inst: channel, sleep=lambda s: None, stderr=io.StringIO())
    assert summary["published"] == [_EMBED_JOB] and summary["destroyed"] is False and deletes
    assert summary["refused"] == []  # the worker's refused bucket, empty here
    types = [r[0] for r in store.ops.execute("SELECT type FROM event WHERE type LIKE 'vastai_%'").fetchall()]
    assert types == ["vastai_destroy_failed"]
    rec = json.loads(next(runs_dir(root).glob("*.json")).read_text(encoding="utf-8"))
    assert rec["status"] == "destroy_failed" and rec["instance_id"] == 901
