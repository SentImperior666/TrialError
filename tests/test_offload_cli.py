"""Lane L0-C: the ``trialerror offload {worker,reclaim,kick,status}`` group,
the new ``ledger.kick`` primitive it rides, and the transport gate.

The group is auto-discovered (design Section 5.2) -- these tests go through
``trialerror.cli.build_parser`` rather than calling handlers directly, so a
registration mistake shows up here and not on the sandbox.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
def test_the_group_is_auto_discovered_with_all_seven_verbs():
    """Four verbs until C-0097, six after it, seven after D-FB-6:
    `worker-control` and `worker-status` are the control law's CLI surface
    (D1/D4) and `doctor` is this subsystem's own checks in one place. The
    key's own seven-verb contract is untouched -- these run against the queue
    directory on whichever machine owns it, never over the restricted key."""
    from trialerror.cli import discover_groups

    group = next(m for m in discover_groups() if m.GROUP_NAME == "offload")
    parser = build_parser()
    extra = {"worker": ["--queue-root", "y"], "worker-control": ["--pause", "--by-launch", "LNCH-1"]}
    for verb in ("worker", "reclaim", "kick", "status", "worker-control", "worker-status", "doctor"):
        args = parser.parse_args(["offload", verb, "--program-root", "x"] + extra.get(verb, []))
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
    than publishing stand-ins into the record.

    ``--lock-path`` is not decoration. Without it the run takes the
    MACHINE-GLOBAL single-instance lock, and ``_cmd_worker`` takes that lock
    BEFORE ``run_worker`` reaches the backend validation -- so on a machine
    where a real worker happens to be running, this test measured
    ``already_running`` and reported the fail-closed gate as broken. Its
    sibling below already passed one."""
    (program_root / "trialerror.toml").write_text('[program]\nid = "dev"\n', encoding="utf-8")
    env = _run(
        _base(program_root, platform_root, "worker")
        + [
            "--queue-root", str(tmp_path / "q"),
            "--max-polls", "1",
            "--lock-path", str(tmp_path / "l.lock"),
        ]
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
    assert set(summary) == {
        "claimed", "published", "failed", "lost", "polls", "message",
        # C-0097: `stopped` is a fifth outcome bucket (a claim handed back
        # mid-job on request, which is neither a failure nor a loss), and
        # `control` is the transition log D2 asks every act to leave behind.
        "stopped", "control",
    }


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


# ---------------------------------------------------------------------------
# VERIFY V-4: building the parser must not import the worker
# ---------------------------------------------------------------------------


def test_building_the_parser_does_not_import_the_offload_worker():
    """``register()`` runs for EVERY ``trialerror`` invocation (the CLI's
    group auto-discovery), so anything it touches is paid by commands that
    never go near a GPU. ``trialerror.offload.worker`` drags in the jobs
    ledger/registry, the offload lock/protocol/shell/stage/transport stack
    and the whole stores schema package -- ~32 modules -- for a single
    integer. The helper this replaces documented exactly that hazard and
    then walked into it, because the PARSER called the helper.

    Measured in a subprocess: this process has almost certainly imported
    the worker already (other tests in this file run one)."""
    probe = (
        "import sys; from trialerror.cli import build_parser; build_parser(); "
        "print('trialerror.offload.worker' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout


def test_the_batch_size_help_default_is_pinned_to_the_worker_constant():
    """The parser cannot read the worker's constant without importing it,
    so the HELP text carries its own copy. This is what keeps the copy
    honest -- the value that actually takes effect is still the worker's."""
    from trialerror.cli.offload import _BATCH_SIZE_HELP_DEFAULT
    from trialerror.offload.worker import DEFAULT_EMBED_BATCH_SIZE

    assert _BATCH_SIZE_HELP_DEFAULT == DEFAULT_EMBED_BATCH_SIZE
    # C-0097 D7: and the value is the MEASURED one, not the old VRAM guess.
    assert DEFAULT_EMBED_BATCH_SIZE == 4


#: A program whose backends are REAL ones (fake paths, but not the
#: ``fake`` backend), so the worker's fail-closed gate lets the run start.
_REAL_BACKEND_TOML = """\
[program]
id = "dev"

[ingest.ocr]
backend = "marker"
marker_single_exe = "C:/fake/marker.exe"

[ingest.embed]
backend = "qwen3-4b"
python_exe = "C:/fake/python.exe"
module_dir = "C:/fake"
"""


def _run_worker_capturing_kwargs(program_root, platform_root, tmp_path, monkeypatch, extra):
    """Drive the real CLI path (parser -> _cmd_worker) with only
    ``run_worker`` itself stubbed, so argument plumbing is under test and
    nothing else is faked. ``--lock-path`` is isolated on purpose: the
    default is a machine-global lock a real worker may be holding."""
    (program_root / "trialerror.toml").write_text(_REAL_BACKEND_TOML, encoding="utf-8")
    queue_root = tmp_path / "q"
    protocol.ensure_layout(queue_root)
    seen: dict = {}

    def _fake_run_worker(**kwargs):
        seen.update(kwargs)
        return {"message": "stub", "claimed": 0, "published": 0}

    monkeypatch.setattr("trialerror.offload.worker.run_worker", _fake_run_worker)
    env = _run(
        _base(program_root, platform_root, "worker")
        + [
            "--queue-root", str(queue_root),
            "--work-root", str(tmp_path / "work"),
            "--lock-path", str(tmp_path / "l.lock"),
        ]
        + extra
    )
    assert env["ok"] is True, env
    return seen


def test_an_unset_batch_size_reaches_the_worker_as_the_worker_default(
    store, program_root, platform_root, tmp_path, monkeypatch
):
    """``default=None`` must not reach ``run_worker``: the point of the
    change is that the default is resolved LATER, not lost."""
    from trialerror.offload.worker import DEFAULT_EMBED_BATCH_SIZE

    seen = _run_worker_capturing_kwargs(program_root, platform_root, tmp_path, monkeypatch, [])
    assert seen["batch_size"] == DEFAULT_EMBED_BATCH_SIZE


def test_an_explicit_batch_size_still_wins(
    store, program_root, platform_root, tmp_path, monkeypatch
):
    seen = _run_worker_capturing_kwargs(
        program_root, platform_root, tmp_path, monkeypatch, ["--batch-size", "8"]
    )
    assert seen["batch_size"] == 8


def test_keep_resident_reaches_the_worker(
    store, program_root, platform_root, tmp_path, monkeypatch
):
    """C-0097 D9: the flag that preserves the pre-ruling behaviour of holding
    both stages' models for the whole run. Default off, because the measured
    alternative left 0.9 GB of RAM free on the machine this runs on."""
    assert _run_worker_capturing_kwargs(
        program_root, platform_root, tmp_path, monkeypatch, []
    )["keep_resident"] is False
    assert _run_worker_capturing_kwargs(
        program_root, platform_root, tmp_path, monkeypatch, ["--keep-resident"]
    )["keep_resident"] is True


# ---------------------------------------------------------------------------
# C-0097 D1/D4/D5 -- `offload worker-control` and `offload worker-status`
#
# Acceptance D of docs/reviews/WORKER_CONTROL_DESIGN.md: the envelopes, and the
# missing-launch refusal in the L-E4 wording. Both verbs go through
# build_parser() like every other test in this file, so a registration mistake
# shows up here and not on the sandbox.
# ---------------------------------------------------------------------------
def _seed_launch(store) -> str:
    from tests._store_fixtures import populate_one_of_everything

    return populate_one_of_everything(store)["launch"]


def _report_idle(root, worker_id: str = "dev") -> None:
    """One idle heartbeat, which is what makes a worker "known" to the control
    verb -- the same beat the real worker sends while it holds no claim."""
    from trialerror.offload import control as control_api
    from trialerror.offload.transport import LocalTransport

    LocalTransport(root, worker_id=worker_id).heartbeat(
        protocol.idle_job_id(worker_id),
        progress=control_api.encode_progress({"worker_id": worker_id, "state": "idle"}),
    )


def test_worker_control_without_a_launch_refuses_in_the_l_e4_wording(offload_program, platform_root):
    """"No launch, no control" -- and the refusal NAMES the missing launch
    rather than failing later with a schema error."""
    env = _run(_base(offload_program, platform_root, "worker-control", "--pause"))
    assert env["ok"] is False
    assert env["error"]["code"] == "launch_required"
    assert "L-E4" in env["error"]["message"]
    assert "--by-launch" in env["error"]["message"]
    root = protocol.offload_root(offload_program)
    assert not protocol.control_path(root, "dev").exists()


def test_worker_control_refuses_a_launch_with_no_row(offload_program, platform_root):
    """An id that names no ``platform.launch`` row is refused, never accepted
    with a fallback identity -- the XID posture every lane e decision has."""
    root = protocol.offload_root(offload_program)
    _report_idle(root)
    env = _run(
        _base(offload_program, platform_root, "worker-control", "--stop", "--by-launch", "LNCH-nope")
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "launch_missing"
    assert not protocol.control_path(root, "dev").exists()


def test_worker_control_writes_the_request_and_logs_one_event(store, offload_program, platform_root):
    from trialerror.offload import control as control_api

    launch = _seed_launch(store)
    root = protocol.offload_root(offload_program)
    _report_idle(root)

    env = _run(
        _base(offload_program, platform_root, "worker-control", "--pause", "--by-launch", launch)
    )
    assert env["ok"] is True, env
    assert env["result"]["request"] == "pause"
    assert env["result"]["worker_id"] == "dev"
    assert env["result"]["by_launch"] == launch
    assert any("worker-status" in str(a) for a in env["nextActions"])

    record = control_api.read_control(root, "dev")
    assert record["request"] == "pause" and record["stale"] is False

    rows = store.ops.execute(
        "SELECT payload, launch_id FROM event WHERE type = 'offload_worker_control'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["request"] == "pause"
    assert payload["worker_id"] == "dev"
    assert rows[0]["launch_id"] == launch


def test_worker_control_refuses_an_unknown_worker_unless_told_otherwise(
    store, offload_program, platform_root
):
    """A request nothing is listening for is a request that will surprise
    somebody -- so it refuses by default, and ``--even-if-absent`` is how an
    operator says "leave it waiting, I am about to start the worker"."""
    launch = _seed_launch(store)
    root = protocol.offload_root(offload_program)

    env = _run(
        _base(offload_program, platform_root, "worker-control", "--stop", "--by-launch", launch)
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "control_refused"
    assert "no worker" in env["error"]["message"]
    assert not protocol.control_path(root, "dev").exists()

    env = _run(
        _base(
            offload_program, platform_root, "worker-control", "--stop",
            "--by-launch", launch, "--even-if-absent",
        )
    )
    assert env["ok"] is True, env
    assert protocol.control_path(root, "dev").is_file()


def test_worker_control_refuses_a_second_pending_request_but_never_a_stop(
    store, offload_program, platform_root
):
    """Silently replacing an unread request would lose an instruction the
    operator believes they gave. A stop is the one exception: it is strictly
    stronger than either other word, and an operator escalating from pause to
    stop must not have to wait out a TTL."""
    from trialerror.offload import control as control_api

    launch = _seed_launch(store)
    root = protocol.offload_root(offload_program)
    _report_idle(root)
    args = ["worker-control", "--by-launch", launch]

    assert _run(_base(offload_program, platform_root, *args, "--pause"))["ok"] is True
    second = _run(_base(offload_program, platform_root, *args, "--pause"))
    assert second["ok"] is False
    assert second["error"]["code"] == "control_refused"
    assert "still pending" in second["error"]["message"]

    escalated = _run(_base(offload_program, platform_root, *args, "--stop"))
    assert escalated["ok"] is True, escalated
    assert control_api.read_control(root, "dev")["request"] == "stop"


def test_worker_control_requires_exactly_one_request_word(offload_program, platform_root):
    """argparse's mutually-exclusive group, asserted because it is the thing
    that stops `--pause --stop` from being a coin toss."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["offload", "worker-control", "--program-root", str(offload_program)]
        )
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["offload", "worker-control", "--pause", "--stop", "--program-root", str(offload_program)]
        )


def test_worker_control_accepts_a_resume_once_the_pause_has_been_read(
    store, offload_program, platform_root
):
    """FIX V-2 on the CLI surface: pause, let the worker report that it read it,
    resume. This used to be refused for the full one-hour TTL with a sentence
    that asserted the opposite of what the worker was reporting, which made the
    design's own acceptance step unperformable."""
    from trialerror.offload import control as control_api
    from trialerror.offload.transport import LocalTransport

    launch = _seed_launch(store)
    root = protocol.offload_root(offload_program)
    _report_idle(root)
    args = ["worker-control", "--by-launch", launch]
    assert _run(_base(offload_program, platform_root, *args, "--pause"))["ok"] is True

    # the worker picks the word up and says so on its next beat
    LocalTransport(root, worker_id="dev").heartbeat(
        protocol.idle_job_id("dev"),
        progress=control_api.encode_progress(
            {"worker_id": "dev", "state": "paused", "control_seen": "pause"}
        ),
    )
    resumed = _run(_base(offload_program, platform_root, *args, "--resume"))
    assert resumed["ok"] is True, resumed
    assert control_api.read_control(root, "dev")["request"] == "resume"


def test_worker_control_clears_a_request_nobody_read(store, offload_program, platform_root):
    """FIX V-3's operator escape hatch. A request a worker never read -- left for
    a worker that did not start, or one the operator changed their mind about --
    used to be unendable except by waiting out the TTL. ``--clear`` is an ACT, so
    it carries a launch and lands in the event log like the three words do."""
    from trialerror.offload import control as control_api

    launch = _seed_launch(store)
    root = protocol.offload_root(offload_program)
    args = ["worker-control", "--by-launch", launch]
    assert _run(
        _base(offload_program, platform_root, *args, "--pause", "--even-if-absent")
    )["ok"] is True
    assert protocol.control_path(root, "dev").is_file()

    cleared = _run(_base(offload_program, platform_root, *args, "--clear"))
    assert cleared["ok"] is True, cleared
    assert cleared["result"]["cleared"] is True
    assert cleared["result"]["previous_request"] == "pause"
    assert not protocol.control_path(root, "dev").exists()
    assert control_api.control_word(root, "dev") == "none"

    rows = store.ops.execute(
        "SELECT payload FROM event WHERE type = 'offload_worker_control' ORDER BY rowid"
    ).fetchall()
    assert [json.loads(r["payload"])["request"] for r in rows] == ["pause", "clear"]


def test_worker_control_clear_still_needs_a_launch(offload_program, platform_root):
    """A clear changes what the worker will be told, so L-E4 applies to it too."""
    env = _run(_base(offload_program, platform_root, "worker-control", "--clear"))
    assert env["ok"] is False
    assert env["error"]["code"] == "launch_required"


def test_the_guide_documents_every_worker_control_word(offload_program):
    """The guide is the only place an operator learns these verbs exist, so the
    parser and the prose are checked against each other. ``--clear`` exists
    because of FIX V-3 and has to be findable."""
    from pathlib import Path as _Path

    parser_help = build_parser().format_help()
    assert "offload" in parser_help
    guide = (_Path(__file__).resolve().parents[1] / "docs" / "OPERATOR_GUIDE.md").read_text(
        encoding="utf-8"
    )
    for flag in ("--pause", "--resume", "--stop", "--clear"):
        assert flag in guide, f"{flag} is not documented for an operator"
    assert "worker-status" in guide


def test_worker_status_reaps_a_request_its_worker_has_finished_with(
    offload_program, platform_root
):
    """FIX V-3: the status verb is the janitor with the highest poll rate, so it
    is one of the three sandbox-side places that end a spent request. A stop the
    worker acted on before exiting must not stop the next run."""
    from trialerror.offload import control as control_api
    from trialerror.offload.transport import LocalTransport

    root = protocol.offload_root(offload_program)
    control_api.request_control(
        root, worker_id="dev", request="stop", by_launch="LNCH-1", require_worker=False
    )
    LocalTransport(root, worker_id="dev").heartbeat(
        protocol.idle_job_id("dev"),
        progress=control_api.encode_progress(
            {"worker_id": "dev", "state": "exited", "control_seen": "stop"}
        ),
    )
    env = _run(_base(offload_program, platform_root, "worker-status"))
    assert env["ok"] is True, env
    assert [r["request"] for r in env["result"]["reaped"]] == ["stop"]
    assert env["result"]["controls"] == []
    assert not protocol.control_path(root, "dev").exists()


def test_kick_reaps_spent_control_requests_too(offload_program, platform_root):
    """The jobs loop runs ``kick`` every cycle, which is what makes a RESTART
    inside the TTL start clean with nobody having to ask."""
    from trialerror.offload import control as control_api
    from trialerror.offload.transport import LocalTransport

    root = protocol.offload_root(offload_program)
    protocol.ensure_layout(root)
    control_api.request_control(
        root, worker_id="dev", request="stop", by_launch="LNCH-1", require_worker=False
    )
    LocalTransport(root, worker_id="dev").heartbeat(
        protocol.idle_job_id("dev"),
        progress=control_api.encode_progress(
            {"worker_id": "dev", "state": "exited", "control_seen": "stop"}
        ),
    )
    env = _run(_base(offload_program, platform_root, "kick"))
    assert env["ok"] is True, env
    assert [r["request"] for r in env["result"]["reaped"]] == ["stop"]
    assert not protocol.control_path(root, "dev").exists()


def test_worker_status_reports_a_worker_and_its_progress(offload_program, platform_root):
    from trialerror.offload import control as control_api
    from trialerror.offload.transport import LocalTransport

    root = protocol.offload_root(offload_program)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    LocalTransport(root).heartbeat(
        "JOB-a",
        progress=control_api.encode_progress(
            {
                "worker_id": "dev",
                "state": "running",
                "kind": "embed",
                "job_id": "JOB-a",
                "units_done": 12,
                "units_total": 40,
                "unit": "chunk",
                "settings": {"batch_size": 4, "model_key": "stub-embed"},
            }
        ),
    )

    env = _run(_base(offload_program, platform_root, "worker-status"))
    assert env["ok"] is True, env
    assert env["result"]["count"] == 1
    row = env["result"]["workers"][0]
    assert row["worker_id"] == "dev"
    assert row["state"] == "running"
    assert (row["units_done"], row["units_total"], row["unit"]) == (12, 40, "chunk")
    assert row["settings"]["batch_size"] == 4
    assert row["lost"] is False
    assert row["heartbeat_age_s"] is not None
    assert env["result"]["lost_after_s"] == 2 * 300.0 + 60.0


def test_worker_status_on_a_queue_with_no_workers_is_still_ok(offload_program, platform_root):
    env = _run(_base(offload_program, platform_root, "worker-status"))
    assert env["ok"] is True
    assert env["result"]["workers"] == []
    assert env["result"]["count"] == 0


def test_worker_status_filters_by_worker_and_honours_the_interval(offload_program, platform_root):
    """``--heartbeat-interval-s`` is what makes the lost window assertable: the
    window is 2x the interval + 60 s, and a worker is only ``lost`` relative to
    the beat interval it was actually running at."""
    root = protocol.offload_root(offload_program)
    _report_idle(root, "dev")
    _report_idle(root, "other")

    env = _run(_base(offload_program, platform_root, "worker-status", "--worker-id", "other"))
    assert [r["worker_id"] for r in env["result"]["workers"]] == ["other"]

    env = _run(
        _base(offload_program, platform_root, "worker-status", "--heartbeat-interval-s", "1")
    )
    assert env["result"]["lost_after_s"] == 62.0
    assert all(r["lost"] is False for r in env["result"]["workers"])


def test_worker_status_reads_a_queue_root_on_this_machine(offload_program, platform_root, tmp_path):
    """The DEV side of the split: the same verb, pointed at a queue path rather
    than resolved from a program root."""
    root = protocol.ensure_layout(tmp_path / "elsewhere")
    _report_idle(root)
    env = _run(["offload", "worker-status", "--queue-root", str(root)])
    assert env["ok"] is True
    assert [r["worker_id"] for r in env["result"]["workers"]] == ["dev"]
    assert env["result"]["root"] == str(root)
