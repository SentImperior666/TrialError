"""Lane L0-C: the ``trialerror offload {worker,reclaim,kick,status}`` group,
the new ``ledger.kick`` primitive it rides, and the transport gate.

The group is auto-discovered (design Section 5.2) -- these tests go through
``trialerror.cli.build_parser`` rather than calling handlers directly, so a
registration mistake shows up here and not on the sandbox.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import build_parser
from trialerror.jobs import ledger
from trialerror.offload import protocol
from trialerror.offload.transport import LocalTransport, TransportError
from tests._offload_fixtures import StubDevBackends, publish_stub_result, queue_one, write_offload_toml


def _run(argv: list[str]) -> dict:
    args = build_parser().parse_args(argv)
    return args.handler(args)


@pytest.fixture()
def offload_program(store, program_root):
    write_offload_toml(program_root)
    protocol.ensure_layout(protocol.offload_root(program_root))
    return program_root


def _base(program_root, platform_root, *rest):
    return ["offload", *rest, "--program-root", str(program_root), "--platform-root", str(platform_root)]


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def test_the_group_is_auto_discovered_with_all_four_verbs():
    from trialerror.cli import discover_groups

    group = next(m for m in discover_groups() if m.GROUP_NAME == "offload")
    parser = build_parser()
    for verb in ("worker", "reclaim", "kick", "status"):
        args = parser.parse_args(["offload", verb, "--program-root", "x"] + (["--queue-root", "y"] if verb == "worker" else []))
        assert args.offload_cmd == verb
        assert callable(args.handler)
    assert group.HELP


def test_program_root_is_required_and_reported_as_an_envelope(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env = _run(["offload", "status"])
    assert env["ok"] is False
    assert env["error"]["code"] == "no_program_root"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_reports_the_counts_te_status_prints(offload_program, platform_root):
    root = protocol.offload_root(offload_program)
    queue_one(root, "JOB-a")
    queue_one(root, "JOB-b")
    protocol.server_claim(root, "JOB-b", worker_id="dev")

    env = _run(_base(offload_program, platform_root, "status"))
    assert env["ok"]
    assert env["result"]["pending"] == 1
    assert env["result"]["claimed"] == 1
    assert env["result"]["failed"] == 0
    assert env["result"]["pending_job_ids"] == ["JOB-a"]
    assert env["nextActions"], "a non-empty queue tells the operator what to run"

    # the shape te-status.sh's jq reads
    parsed = json.loads(json.dumps(env))
    assert set(["pending", "claimed", "failed", "oldest_pending_age_s"]) <= set(parsed["result"])


def test_status_on_an_untouched_program_is_still_ok(store, program_root, platform_root):
    env = _run(_base(program_root, platform_root, "status"))
    assert env["ok"] and env["result"]["pending"] == 0 and env["result"]["exists"] is False


# ---------------------------------------------------------------------------
# reclaim
# ---------------------------------------------------------------------------
def test_reclaim_honours_an_explicit_expiry(offload_program, platform_root):
    root = protocol.offload_root(offload_program)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")

    env = _run(_base(offload_program, platform_root, "reclaim"))
    assert env["ok"] and env["result"]["count"] == 0

    env = _run(_base(offload_program, platform_root, "reclaim") + ["--expiry-s", "0"])
    assert env["ok"] and env["result"]["count"] == 1
    assert protocol.list_pending(root) == ["JOB-a"]


# ---------------------------------------------------------------------------
# kick
# ---------------------------------------------------------------------------
def test_kick_adopts_an_interrupted_publish(offload_program, platform_root):
    root = protocol.offload_root(offload_program)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", protocol.pack_dir(protocol.pending_dir(root)), worker_id="dev")
    (protocol.claimed_dir(root) / "dev" / "JOB-a.json").replace(
        protocol.partial_dir(root) / "JOB-a" / protocol.MANIFEST_FILENAME
    )

    env = _run(_base(offload_program, platform_root, "kick"))
    assert env["ok"] and env["result"]["adopted"] == ["JOB-a"]
    assert protocol.list_done(root) == ["JOB-a"]


def test_kick_names_a_published_job_with_no_ledger_row(offload_program, platform_root):
    """A published result whose ledger row was deleted (or was never a
    ledger job at all) is reported rather than swept: deleting a result
    nobody asked for would throw away GPU work."""
    root = protocol.offload_root(offload_program)
    queue_one(root, "JOB-orphan")
    publish_stub_result(root, "JOB-orphan")
    env = _run(_base(offload_program, platform_root, "kick"))
    assert env["result"]["unknown_job_ids"] == ["JOB-orphan"]
    assert protocol.list_done(root) == ["JOB-orphan"]


# ---------------------------------------------------------------------------
# ledger.kick
# ---------------------------------------------------------------------------
def test_ledger_kick_clears_a_deferred_jobs_delay(store):
    job = ledger.enqueue(store, kind="ocr", payload={}, job_id="JOB-k")
    worker = "w"
    ledger.claim_specific(store, "JOB-k", worker_id=worker)
    ledger.fail(store, "JOB-k", worker, failure_class="environmental", error="awaiting DEV GPU",
                environmental_retry_delay_s=1800)
    assert ledger.get_job(store, "JOB-k")["next_attempt_ts"] is not None
    assert ledger.claim_specific(store, "JOB-k", worker_id=worker) is None

    row = ledger.kick(store, "JOB-k")
    assert row is not None and row["next_attempt_ts"] is None
    assert ledger.get_job(store, "JOB-k")["attempts"] == 0, "kick never touches the attempt budget"
    assert ledger.claim_specific(store, "JOB-k", worker_id=worker) is not None
    assert [e["type"] for e in ledger.list_events(store, "JOB-k")].count("kicked") == 1
    assert job["state"] == "pending"


def test_ledger_kick_is_a_no_op_when_there_is_nothing_to_clear(store):
    ledger.enqueue(store, kind="ocr", payload={}, job_id="JOB-k")
    assert ledger.kick(store, "JOB-k") is None  # already claimable
    assert ledger.kick(store, "JOB-missing") is None  # no such job

    ledger.claim_specific(store, "JOB-k", worker_id="w")
    ledger.pause(store, "JOB-k")
    assert ledger.kick(store, "JOB-k") is None, "un-delaying is not un-pausing"
    assert ledger.get_job(store, "JOB-k")["state"] == "paused"


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------
def test_worker_requires_exactly_one_of_remote_and_queue_root(offload_program, platform_root, tmp_path):
    env = _run(_base(offload_program, platform_root, "worker"))
    assert env["ok"] is False and env["error"]["code"] == "bad_arguments"

    env = _run(
        _base(offload_program, platform_root, "worker")
        + ["--remote", "te-offload", "--queue-root", str(tmp_path)]
    )
    assert env["ok"] is False and env["error"]["code"] == "bad_arguments"


def test_worker_refuses_a_program_configured_for_fake_backends(store, program_root, platform_root, tmp_path):
    """The DEV worker's own fail-closed gate, through the CLI: an operator
    who points the launcher at the wrong program root is told so, rather
    than publishing stand-ins into the record."""
    (program_root / "trialerror.toml").write_text('[program]\nid = "dev"\n', encoding="utf-8")
    env = _run(
        _base(program_root, platform_root, "worker") + ["--queue-root", str(tmp_path / "q"), "--max-polls", "1"]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "fake_backend_refused"


def test_worker_reports_an_unreadable_config(store, program_root, platform_root, tmp_path):
    (program_root / "trialerror.toml").write_text("[program\nbroken", encoding="utf-8")
    env = _run(_base(program_root, platform_root, "worker") + ["--queue-root", str(tmp_path / "q")])
    assert env["ok"] is False and env["error"]["code"] == "bad_config"


def test_worker_runs_against_a_local_queue_and_reports_an_empty_one(
    store, program_root, platform_root, tmp_path
):
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "dev"\n\n[ingest.ocr]\nbackend = "marker"\nmarker_single_exe = "C:/fake/marker.exe"\n\n'
        '[ingest.embed]\nbackend = "qwen3-4b"\npython_exe = "C:/fake/python.exe"\nmodule_dir = "C:/fake"\n',
        encoding="utf-8",
    )
    queue_root = tmp_path / "q"
    protocol.ensure_layout(queue_root)
    env = _run(
        _base(program_root, platform_root, "worker")
        + ["--queue-root", str(queue_root), "--work-root", str(tmp_path / "work"), "--lock-path", str(tmp_path / "l.lock")]
    )
    assert env["ok"]
    assert env["result"]["message"].startswith("Queue empty")


def test_a_second_worker_is_refused_by_the_single_instance_lock(tmp_path):
    """Two workers on one laptop would write into the same per-job work
    directory. The lock is an OS byte-range lock, so a worker killed with
    the power button releases it with nothing to clean up."""
    from trialerror.offload.lock import WorkerAlreadyRunning, single_instance_lock

    lock_path = tmp_path / "worker.lock"
    with single_instance_lock(lock_path):
        with pytest.raises(WorkerAlreadyRunning):
            with single_instance_lock(lock_path):
                pass
    # released on exit
    with single_instance_lock(lock_path):
        pass


# ---------------------------------------------------------------------------
# transport gate
# ---------------------------------------------------------------------------
def test_local_transport_applies_the_same_verb_gate_as_the_ssh_wrapper(tmp_path):
    """Even in-process, a job id the wrapper would refuse is refused --
    so a test can never accidentally exercise a path the real deployment
    would reject."""
    root = protocol.ensure_layout(tmp_path / "offload")
    transport = LocalTransport(root)
    for bad in ("..", "a/b", "a;id"):
        with pytest.raises(TransportError, match="refuse"):
            transport.claim(bad)


def test_local_transport_round_trips_the_seven_verbs(tmp_path):
    root = protocol.ensure_layout(tmp_path / "offload")
    queue_one(root, "JOB-a", payload=b"body")
    transport = LocalTransport(root, worker_id="dev")

    assert transport.list_jobs() == ["JOB-a"]
    manifest = transport.claim("JOB-a")
    assert manifest["job_id"] == "JOB-a"
    transport.heartbeat("JOB-a")
    data = transport.pull("JOB-a")
    assert b"body" in data
    transport.return_job("JOB-a")
    assert transport.list_jobs() == ["JOB-a"]

    with pytest.raises(TransportError):
        transport.pull("JOB-a")  # no longer claimed


def test_ssh_transport_refuses_a_bad_id_before_touching_the_wire():
    from trialerror.offload.transport import SshTransport

    transport = SshTransport("te-offload", ssh_exe="definitely-not-an-executable")
    with pytest.raises(TransportError, match="refused job id"):
        transport.claim("../escape")


def test_ssh_transport_surfaces_a_missing_ssh_binary_as_a_transport_error():
    from trialerror.offload.transport import SshTransport

    transport = SshTransport("te-offload", ssh_exe="definitely-not-an-executable-xyz")
    with pytest.raises(TransportError):
        transport.list_jobs()


def test_worker_summary_shape_is_stable(offload_program, platform_root, tmp_path):
    """``GPU Worker launcher`` prints this; the dashboard and te-status read
    the same fields. Keep the keys stable."""
    from trialerror.offload.worker import run_worker

    summary = run_worker(
        transport=LocalTransport(protocol.offload_root(offload_program)),
        backends=StubDevBackends(),
        work_root=tmp_path / "w",
    )
    assert set(summary) == {"claimed", "published", "failed", "lost", "polls", "message"}


# ---------------------------------------------------------------------------
# SEC-1 / V2 at the CLI surface
# ---------------------------------------------------------------------------
def test_kick_reports_a_quarantined_staging_directory(offload_program, platform_root):
    """SEC-1 through the verb the sandbox's jobs window actually runs. A
    staging directory for a job the sandbox never queued is not adopted --
    and it is REPORTED, because a manifest that arrived by `push` for an
    unknown job is not an operational state, it is somebody writing into
    the queue."""
    root = protocol.offload_root(offload_program)
    staging = protocol.partial_dir(root) / "JOB-forged"
    staging.mkdir(parents=True, exist_ok=True)
    protocol.write_json(
        staging / protocol.MANIFEST_FILENAME,
        {"job_id": "JOB-forged", "stage": "ocr", "config_hash": "forged", "expect": {}},
    )

    env = _run(_base(offload_program, platform_root, "kick"))
    assert env["ok"] is True
    assert env["result"]["adopted"] == []
    assert [r["job_id"] for r in env["result"]["rejected"]] == ["JOB-forged"]
    assert protocol.list_done(root) == []


def test_reclaim_and_kick_do_not_create_a_queue_that_is_not_there(store, program_root, platform_root):
    """V2: both verbs run every jobs-loop cycle in EVERY program. In one
    that offloads nothing they must answer the question, not create the
    thing the question was about."""
    root = protocol.offload_root(program_root)
    assert not root.exists()

    assert _run(_base(program_root, platform_root, "reclaim"))["ok"] is True
    assert _run(_base(program_root, platform_root, "kick"))["ok"] is True
    assert _run(_base(program_root, platform_root, "status"))["ok"] is True
    assert not root.exists(), "an inspection verb created the queue it was asked about"


def test_ssh_transport_refuses_an_over_large_push_before_the_wire(tmp_path):
    """SEC-4, DEV side. The wrapper would refuse it anyway; spending half
    an hour uploading first is the part worth avoiding."""
    from trialerror.offload.transport import SshTransport

    transport = SshTransport("te-offload", ssh_exe="definitely-not-an-executable", max_payload_bytes=16)
    with pytest.raises(TransportError, match="cap"):
        transport.push("JOB-a", b"x" * 64)


def test_local_transport_refuses_an_over_large_push(tmp_path):
    root = protocol.ensure_layout(tmp_path / "offload")
    queue_one(root, "JOB-a")
    transport = LocalTransport(root, worker_id="dev", max_payload_bytes=16)
    transport.claim("JOB-a")
    with pytest.raises(TransportError, match="cap"):
        transport.push("JOB-a", b"x" * 64)
