"""The vast.ai OCR executor through the DEV worker, end to end on fakes (the
vast.ai OCR design, section 14): egress, spend, the lease lifecycle, the
guards on remote output, control, ``--stages`` (O5), ``when_refused``
(O7), and the no-identifier assertion over everything that left DEV.

``run_worker`` drives a real ``VastaiMarkerOcrBackend`` over a
``LocalTransport`` queue; vast.ai is ``FakeVast`` (L1's, subclassed), the
instance is ``FakeInstanceShell``. No network, no ssh, no GPU: the network
tripwire and the ssh tripwire fail any test that tries.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import trialerror.ingest.backends as backends_mod
from tests._offload_fixtures import ControlTransport, StubDevBackends, StubOcrBackend, queue_chunks
from tests._vastai_fakes import isolated_state, network_tripwire  # noqa: F401 - fixtures
from tests._vastai_shell_fakes import (  # noqa: F401 - ssh_tripwire is a fixture
    DOC,
    JOB,
    TITLE_NAME,
    RecordingTransport,
    ledger_rows,
    make_env,
    run_env,
    ssh_tripwire,
)
from trialerror.offload import protocol
from trialerror.offload.worker import IDLE_MESSAGE, OCR_RANGE_CACHE_DIRNAME, STOPPED_MESSAGE
from trialerror.vastai.lease import LeaseExpired
from trialerror.vastai.ocr import PEAK_RSS_SOURCE_REMOTE


@pytest.fixture(autouse=True)
def _no_network_no_ssh(network_tripwire, isolated_state, ssh_tripwire):  # noqa: F811
    yield


@pytest.fixture
def state(isolated_state):  # noqa: F811
    return isolated_state


def _kinds(env) -> list[str]:
    return [r["kind"] for r in ledger_rows(env.state_dir)]


def _rows(env, kind: str) -> list[dict]:
    return [r for r in ledger_rows(env.state_dir) if r["kind"] == kind]


def _published(env, job: str = JOB) -> tuple[dict, list[dict]]:
    base = protocol.done_dir(env.queue) / job
    result = json.loads((base / protocol.RESULT_FILENAME).read_text(encoding="utf-8"))
    pages = json.loads((base / "pages.json").read_text(encoding="utf-8"))["pages"]
    return result, pages


def _error(env, job: str = JOB) -> str:
    for d in (protocol.done_dir(env.queue), protocol.failed_dir(env.queue)):
        path = d / job / protocol.ERROR_FILENAME
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))["error"]
    raise AssertionError("no error.json was published")


def _pending(env) -> list[str]:
    return RecordingTransport(env.queue).list_jobs()


def _cache(env, job: str = JOB) -> Path:
    return env.tmp / "work" / OCR_RANGE_CACHE_DIRNAME / job


def _document_uploads(env) -> list:
    return [s for s in env.world.stdin_sent if s[2] == env.data]


def _assert_nothing_identifying_left(env) -> None:
    sent = env.world.everything_sent(document=env.data)
    for needle in (JOB, DOC, TITLE_NAME, "Confidential", str(env.tmp), str(env.root), env.tmp.name):
        assert needle not in sent, f"{needle!r} reached vast.ai or the instance"


# ---------------------------------------------------------------------------
# egress through the worker
# ---------------------------------------------------------------------------
def test_without_an_approval_the_job_is_refused_before_the_pull(tmp_path, state):
    env = make_env(tmp_path, state, approve=False)
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)

    assert [e["reason_code"] for e in summary["refused"]] == ["approval-missing"]
    assert summary["refused"][0]["job_id"] == JOB and "Nothing was sent" in summary["refused"][0]["message"]
    assert ("pull", JOB) not in transport.verbs and ("return", JOB) in transport.verbs
    assert _pending(env) == [JOB], "the claim went back unrun"
    assert summary["message"].startswith("Queue not empty - 1 job(s) refused") and "approval-missing" in summary["message"]
    row = _rows(env, "refused")[0]
    assert (row["reason_code"], row["sha256"], row["bytes"]) == ("approval-missing", env.sha, len(env.data))
    assert env.world.vast.calls == [] and env.world.commands == [], "no vast.ai call, no shell"


def test_a_tier_the_policy_does_not_name_is_refused(tmp_path, state):
    env = make_env(tmp_path, state, egress={"allow_license_tiers": ["open"]}, tier="commercial_restricted")
    summary = run_env(env)
    assert [e["reason_code"] for e in summary["refused"]] == ["tier-not-allowed"]
    assert env.world.vast.calls == []


def test_a_marker_without_a_tier_is_refused_unless_its_sha256_is_named(tmp_path, state):
    env = make_env(tmp_path, state, egress={"allow_license_tiers": ["open"]}, tier=None)
    summary = run_env(env)
    assert [e["reason_code"] for e in summary["refused"]] == ["tier-missing"]
    assert env.world.vast.calls == []


def test_a_worst_case_over_the_job_cap_creates_nothing(tmp_path, state):
    env = make_env(tmp_path, state, vastai={"max_job_usd": 0.01})
    summary = run_env(env)
    assert [e["reason_code"] for e in summary["refused"]] == ["cap-job"]
    assert env.world.vast.verbs("PUT") == [], "zero create calls"
    assert _document_uploads(env) == [] and _rows(env, "intent") == []
    assert _rows(env, "refused")[0]["reason_code"] == "cap-job"


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def test_a_sha256_named_unknown_tier_document_runs_publishes_and_leaves_nothing(tmp_path, state):
    env = make_env(tmp_path, state, tier="unknown")
    summary = run_env(env)

    assert summary["published"] == [JOB] and "refused" not in summary
    assert summary["message"] == IDLE_MESSAGE
    result, pages = _published(env)
    assert [p["page_number"] for p in pages] == list(range(10))
    assert [p["text"] for p in pages] == [f"body of page {n}" for n in range(10)]
    assert (result["backend"], result["version"], result["page_count"]) == ("marker", "1.10.2", 10)
    assert [(r["first_page"], r["last_page"]) for r in result["ranges"]] == [(0, 2), (3, 5), (6, 8), (9, 9)]
    assert all(r["peak_rss_source"] == PEAK_RSS_SOURCE_REMOTE and r["peak_rss_bytes"] == 21_000_000_000
               for r in result["ranges"])
    block = result["vastai"]
    assert block["executor"] == "vastai" and len(block["leases"]) == 1
    assert block["lease_id"].startswith("VOCR-") and block["instance_id"] and block["host_id"]
    assert (block["datacenter"], block["verified"]) == (True, True) and block["geolocation"]
    assert block["remote_versions"]["marker"] == "1.10.2" and block["canary_similarity"] >= 0.97
    assert block["estimated_cost_usd"] > 0 and block["scratch_kind"] == "shm"
    # Ledger: intent -> shipped -> outcome, with the charter's fields.
    kinds = _kinds(env)
    assert kinds.index("intent") < kinds.index("shipped") < kinds.index("outcome")
    intent, shipped, outcome = _rows(env, "intent")[0], _rows(env, "shipped")[0], _rows(env, "outcome")[0]
    assert (intent["sha256"], intent["bytes"], intent["datacenter"], intent["verified"]) == (env.sha, len(env.data), True, True)
    assert shipped["instance_id"] == outcome["instance_id"] and shipped["start"] and outcome["end"]
    assert outcome["result"] == "published" and outcome["estimated_cost_usd"] > 0 and outcome["destroyed"] is True
    assert outcome["ranges_run"] == ["0-2", "3-5", "6-8", "9-9"]
    # One upload of the document, as input.pdf in RAM scratch; DEV's argv shape per range.
    (upload,) = _document_uploads(env)
    assert upload[1].startswith("/dev/shm/te/VOCR-") and upload[1].endswith("/input.pdf")
    ranges = [a[a.index("--page_range") + 1] for a in env.world.marker_argv if "--page_range" in a]
    assert ranges == ["0-2", "3-5", "6-8", "9-9"]
    assert env.world.vast.instances == {}, "destroyed"
    _assert_nothing_identifying_left(env)


def test_a_marker_failure_mid_range_is_class_e_and_destroys_the_lease(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.marker_rc["3-5"] = (1, "Traceback (most recent call last):\nMemoryError")
    summary = run_env(env)
    assert summary["failed"] == [JOB] and "refused" not in summary
    error = _error(env)
    assert "marker_single exited 1 on the rented host (pages 3-5)" in error and "MemoryError" in error
    assert "remote peak RSS 21.0 GB" in error
    assert env.world.vast.instances == {}
    assert [r["result"] for r in _rows(env, "outcome")] == ["failed"]


def test_an_oom_kill_is_class_e_too(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.marker_rc["0-2"] = (137, "Killed")
    summary = run_env(env)
    assert summary["failed"] == [JOB] and "exited 137" in _error(env)
    assert env.world.vast.instances == {}


def test_a_range_timeout_is_class_e(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.hang_on.add("6-8")
    summary = run_env(env)
    assert summary["failed"] == [JOB] and "EnvironmentalFailure" in _error(env)
    assert env.world.vast.instances == {}


def test_a_ttl_expiry_mid_range_destroys_returns_the_claim_keeps_the_cache_and_ends_the_run(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.expire_on = "3-5"
    transport = RecordingTransport(env.queue)
    with pytest.raises(LeaseExpired):
        run_env(env, transport=transport)
    assert ("return", JOB) in transport.verbs and _pending(env) == [JOB]
    assert (_cache(env) / "range-000000-000002.md").is_file(), "the finished range is kept"
    assert env.world.vast.instances == {}
    assert [r["result"] for r in _rows(env, "outcome")] == ["expired"]


def test_an_instance_dying_mid_range_fails_over_and_reruns_only_the_missing_ranges(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.die_on = ["3-5"]
    summary = run_env(env)
    assert summary["published"] == [JOB]
    by_instance: dict[int, list[str]] = {}
    for iid, flag in env.world.ranges_run:
        by_instance.setdefault(iid, []).append(flag)
    first, second = sorted(by_instance)
    assert by_instance[first] == ["0-2", "3-5"] and by_instance[second] == ["3-5", "6-8", "9-9"]
    intents = _rows(env, "intent")
    assert len(intents) == 2 and intents[0]["machine_id"] != intents[1]["machine_id"]
    assert intents[1]["excluded_machine_ids"] == [intents[0]["machine_id"]]
    assert [r["result"] for r in _rows(env, "outcome")] == ["failed", "published"]
    result, pages = _published(env)
    assert [p["page_number"] for p in pages] == list(range(10)) and len(result["vastai"]["leases"]) == 2
    assert env.world.vast.instances == {}


def test_a_second_death_returns_the_claim_as_hosts_exhausted(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.die_on = ["3-5", "3-5"]
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)
    assert [e["reason_code"] for e in summary["refused"]] == ["hosts-exhausted"]
    assert ("return", JOB) in transport.verbs and _pending(env) == [JOB]
    assert (_cache(env) / "range-000000-000002.md").is_file()
    assert env.world.vast.instances == {}
    assert _rows(env, "refused")[0]["reason_code"] == "hosts-exhausted"


def test_a_destroy_failure_is_loud(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.vast.sticky = {5001}  # the first instance stays listed after every DELETE
    run_env(env)
    assert _rows(env, "destroy_failed")[0]["instance_id"] == 5001
    assert any(line.startswith("!!! vast.ai instance 5001 could NOT be confirmed destroyed") for line in env.world.log)
    outcome = _rows(env, "outcome")[0]
    assert outcome["destroyed"] is False and outcome["destroy_error"]


# ---------------------------------------------------------------------------
# guards on remote output (the inherited DEV guards, running on DEV)
# ---------------------------------------------------------------------------
def test_relative_numbering_is_read_under_auto(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.numbering = "relative"
    run_env(env)
    result, pages = _published(env)
    assert [p["page_number"] for p in pages] == list(range(10)) and result["page_range_numbering"] == "relative"


@pytest.mark.parametrize("knob", ["mixed", "overlap"])
def test_mixed_or_overlapping_numbering_is_refused(tmp_path, state, knob):
    env = make_env(tmp_path, state)
    if knob == "mixed":
        env.world.numbering_by_range = {"3-5": "relative"}
    else:
        env.world.overlap_on = "3-5"
    summary = run_env(env)
    if knob == "mixed":
        assert summary["failed"] == [JOB] and "PageRangeNumberingError" in _error(env)
    else:
        # Round 3 (F01): a range with an extra page is the host's failure (class F), before any numbering check.
        assert JOB not in summary["published"] and _refused_codes(summary) == ["hosts-exhausted"]
    assert env.world.vast.instances == {}


def test_a_page_count_mismatch_is_refused(tmp_path, state):
    env = make_env(tmp_path, state, page_count=11)
    summary = run_env(env)
    assert summary["failed"] == [JOB] and "PageCountMismatchError" in _error(env)


def test_garbage_without_markers_is_a_host_failure_and_never_published(tmp_path, state):
    # Round 3 (F01): a rented host's range without one {N} marker per page is that host's failure (class F),
    # never cached or published (DEV's own loop would publish an unpaginated range as one page).
    env = make_env(tmp_path, state)
    env.world.garbage = True
    summary = run_env(env)
    assert JOB not in summary["published"] and _refused_codes(summary) == ["hosts-exhausted"]
    assert env.world.vast.instances == {}
    assert not any(_cache(env).rglob("*.md")) if _cache(env).is_dir() else True


def _refused_codes(summary) -> list:
    return [e["reason_code"] for e in summary["refused"]]


@pytest.mark.parametrize("flips", [1, 2])
def test_upload_bit_flips_are_retried_then_failed_over(tmp_path, state, flips):
    env = make_env(tmp_path, state)
    env.world.upload_flips = flips
    summary = run_env(env)
    assert summary["published"] == [JOB]
    leases = _rows(env, "intent")
    assert len(leases) == (1 if flips == 1 else 2)
    assert len(_rows(env, "shipped")) == 1, "shipped only once the hash matched"


@pytest.mark.parametrize("flips", [1, 2])
def test_download_bit_flips_are_refetched_then_failed_over(tmp_path, state, flips):
    env = make_env(tmp_path, state)
    env.world.download_flips = flips
    summary = run_env(env)
    assert summary["published"] == [JOB]
    assert len(_rows(env, "intent")) == (1 if flips == 1 else 2)
    assert [p["page_number"] for p in _published(env)[1]] == list(range(10))


@pytest.mark.parametrize("fault", ["version", "model", "canary"])
def test_a_wrong_stack_is_refused_before_upload_and_stops_vastai_for_the_run(tmp_path, state, fault):
    env = make_env(tmp_path, state, jobs=(JOB, "JOB-vocr-2"))
    if fault == "version":
        env.world.marker_version = "1.10.1"
    elif fault == "model":
        env.world.model_overrides = {"layout/2025_09_23/model.safetensors": "0" * 64}
    else:
        env.world.canary_ok = False
    summary = run_env(env)
    codes = {e["job_id"]: e["reason_code"] for e in summary["refused"]}
    assert codes == {JOB: "stack-mismatch", "JOB-vocr-2": "vastai-disabled-for-run"}
    assert _document_uploads(env) == [] and _rows(env, "shipped") == []
    assert len(env.world.vast.verbs("PUT")) == 1, "the second job made no vast.ai call"
    assert env.world.vast.instances == {}
    assert sorted(_pending(env)) == sorted([JOB, "JOB-vocr-2"])


def test_a_missing_range_flag_fails_the_job_before_upload(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.help_has_flag = False
    summary = run_env(env)
    assert summary["failed"] == [JOB] and "PageRangeFlagMissingError" in _error(env)
    assert _document_uploads(env) == [] and env.world.vast.instances == {}


def test_shm_too_small_fails_over_then_refuses_by_name(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.shm_avail = 64_000_000  # Docker's default /dev/shm, on every host
    summary = run_env(env)
    assert [e["reason_code"] for e in summary["refused"]] == ["shm-too-small"]
    assert _document_uploads(env) == [] and len(_rows(env, "intent")) == 2


# ---------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------
def test_a_stop_at_a_range_boundary_destroys_the_lease_and_keeps_the_cache(tmp_path, state):
    env = make_env(tmp_path, state)
    transport = ControlTransport(env.queue, word_for=lambda beat, job, payload: "stop" if job == JOB else "none")
    env.world.on_range = lambda first, last: transport.wait_for_beats(JOB, 2) if first == 0 else None
    summary = run_env(env, transport=transport)
    assert summary["stopped"] == [JOB] and summary["message"] == STOPPED_MESSAGE
    assert [f for _i, f in env.world.ranges_run] == ["0-2"]
    assert (_cache(env) / "range-000000-000002.md").is_file()
    assert env.world.vast.instances == {}
    assert [(r["result"], r["ended_by"]) for r in _rows(env, "outcome")] == [("returned", "stop")]


def test_a_pause_destroys_the_lease_and_resume_leases_again_for_the_remaining_ranges(tmp_path, state):
    env = make_env(tmp_path, state)
    asked: list[int] = []

    def word_for(beat, job, payload):
        if job == JOB and not asked:
            asked.append(beat)
            return "pause"
        return "none"

    transport = ControlTransport(env.queue, word_for=word_for)
    env.world.on_range = lambda first, last: transport.wait_for_beats(JOB, 2) if first == 0 else None
    summary = run_env(env, transport=transport, max_pause_s=0.2)
    assert summary["published"] == [JOB]
    by_instance: dict[int, list[str]] = {}
    for iid, flag in env.world.ranges_run:
        by_instance.setdefault(iid, []).append(flag)
    first, second = sorted(by_instance)
    assert by_instance[first] == ["0-2"] and by_instance[second] == ["3-5", "6-8", "9-9"]
    assert [(r["result"], r["ended_by"]) for r in _rows(env, "outcome")] == [("returned", "pause"), ("published", "done")]
    assert [p["page_number"] for p in _published(env)[1]] == list(range(10))
    assert env.world.vast.instances == {}


# ---------------------------------------------------------------------------
# O5: --stages ocr;  O7: when_refused = "local"
# ---------------------------------------------------------------------------
def test_stages_ocr_returns_an_embed_job_unrun_and_says_so(tmp_path):
    root = protocol.ensure_layout(tmp_path / "offload")
    queue_chunks(root, "JOB-embed-1", count=3)
    protocol.queue_marker(
        root, job_id="JOB-ocr-1", stage="ocr", doc_id="DOC-1",
        expect={"stage": "ocr", "backend": "stub-ocr", "outputs": ["pages.json"], "input_name": "input.txt"},
        config_hash="c", inputs=[("input.txt", b"hello\n")],
    )
    transport = RecordingTransport(root)
    from trialerror.offload.worker import run_worker

    summary = run_worker(transport=transport, backends=StubDevBackends(ocr=StubOcrBackend()), work_root=tmp_path / "w",
                         heartbeat_interval_s=0.02, stages="ocr")
    assert summary["published"] == ["JOB-ocr-1"]
    assert [(e["job_id"], e["reason_code"]) for e in summary["refused"]] == [("JOB-embed-1", "stage-not-served")]
    assert ("pull", "JOB-embed-1") not in transport.verbs and ("return", "JOB-embed-1") in transport.verbs
    assert transport.list_jobs() == ["JOB-embed-1"]
    assert "1 stage-not-served" in summary["message"]


def test_when_refused_local_runs_a_refused_document_on_devs_own_marker(tmp_path, state, monkeypatch):
    env = make_env(tmp_path, state, approve=False,
                   egress={"allow_documents": [], "when_refused": "local"},
                   ingest_ocr={"marker_single_exe": "o7-local-marker", "max_range_pixels": 0})
    real = subprocess.run
    calls: list[list[str]] = []

    def local_marker(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "o7-local-marker":
            calls.append(list(cmd))
            out = Path(cmd[cmd.index("--output_dir") + 1]) / Path(cmd[1]).stem
            out.mkdir(parents=True, exist_ok=True)
            body = "".join(f"{{{n}}}------------------------------------------------\n\nbody of page {n}\n\n" for n in range(10))
            (out / f"{Path(cmd[1]).stem}.md").write_text(body, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(backends_mod.subprocess, "run", local_marker)
    summary = run_env(env)
    assert summary["published"] == [JOB] and "refused" not in summary
    result, pages = _published(env)
    assert "vastai" not in result and [p["page_number"] for p in pages] == list(range(10))
    assert len(calls) == 1 and "--page_range" not in calls[0], "DEV's own budget (max_range_pixels = 0)"
    assert env.world.vast.calls == [] and env.world.commands == []
    row = _rows(env, "refused")[0]
    assert (row["reason_code"], row["routed"]) == ("approval-missing", "local")


# ---------------------------------------------------------------------------
# the CLI surface: --stages, the root, the refused bucket in the envelope
# ---------------------------------------------------------------------------
def _cli_worker(program_root, platform_root, tmp_path, monkeypatch, extra, summary):
    from tests._offload_fixtures import write_offload_toml
    from trialerror.cli import build_parser

    write_offload_toml(program_root)
    protocol.ensure_layout(tmp_path / "q")
    seen: dict = {}

    def fake_run_worker(**kwargs):
        seen.update(kwargs)
        return dict(summary)

    monkeypatch.setattr("trialerror.offload.worker.run_worker", fake_run_worker)
    argv = ["offload", "worker", "--program-root", str(program_root), "--platform-root", str(platform_root),
            "--queue-root", str(tmp_path / "q"), "--work-root", str(tmp_path / "work"),
            "--lock-path", str(tmp_path / "l.lock"), *extra]
    args = build_parser().parse_args(argv)
    return args.handler(args), seen


def test_the_worker_verb_passes_stages_and_the_root_and_prints_the_refused_bucket(
    store, program_root, platform_root, tmp_path, monkeypatch
):
    refused = {"job_id": "JOB-e", "reason_code": "stage-not-served", "message": "served ocr only",
               "next_actions": ["run a worker whose --stages includes embed"]}
    env, seen = _cli_worker(program_root, platform_root, tmp_path, monkeypatch, ["--stages", "ocr"],
                            {"message": "m", "published": [], "refused": [refused]})
    assert env["ok"] is True and seen["stages"] == ("ocr",)
    assert Path(seen["backends"].root) == Path(program_root)
    assert env["result"]["refused"] == [refused]
    warning = [w for w in env["warnings"] if w["code"] == "offload_job_refused"][0]
    assert warning["message"] == "JOB-e: [stage-not-served] served ocr only"


def test_the_worker_verb_defaults_to_both_stages_and_adds_nothing_without_refusals(
    store, program_root, platform_root, tmp_path, monkeypatch
):
    env, seen = _cli_worker(program_root, platform_root, tmp_path, monkeypatch, [], {"message": "m", "published": []})
    assert env["ok"] is True and seen["stages"] == ("ocr", "embed")
    assert not [w for w in env.get("warnings") or [] if w.get("code") == "offload_job_refused"]
    assert "refused" not in env["result"]


def test_an_unknown_stage_is_refused_by_name(store, program_root, platform_root, tmp_path, monkeypatch):
    env, seen = _cli_worker(program_root, platform_root, tmp_path, monkeypatch, ["--stages", "ocr,gpu"], {})
    assert env["ok"] is False and env["error"]["code"] == "bad_arguments" and "gpu" in env["error"]["message"]
    assert seen == {}
