"""Lane FB-8a: ``trialerror jobs retry`` -- the sanctioned way back for a
failed or abandoned job, and the offload marker that goes back with it.

Four bands, in the order the brief states them:

1. the STATE MATRIX -- every ledger state x ``retry``, effect or the named
   refusal (a verb whose refusals are not pinned is a verb that quietly
   grows a new legal state the first time somebody adds one);
2. the FIELDS -- attempts/lease/settled/failure_class after a retry, the
   kept-and-prefixed ``last_error``, the ``job_retried`` event's contents
   and ``--max-attempts``' bounds;
3. the OFFLOAD INVERSE -- ``failed/<id>/`` back to ``queued/`` + ``pending/``
   with the attempts reset and the stamp appended, the evidence kept under
   ``failed/_retried/``, a second retry appending a second stamp, the missing
   payload refused by name, the ``parked_largeformat`` twin named and
   untouched, and the crash between the two halves completed by a second
   call;
4. the PROBES -- five adversarial questions the lane asked of its own diff,
   each kept here as a regression test whether or not it found something.

Everything runs against fixtures. No SSH, no GPU: the DEV worker half goes
through :class:`trialerror.offload.transport.LocalTransport` and the stub
backends, exactly as ``tests/test_offload_stage.py`` drives it.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import main
from trialerror.ingest import pipeline
from trialerror.jobs import ledger
from trialerror.jobs.errors import InvalidTransitionError, JobNotFoundError
from trialerror.jobs.worker import run_one
from trialerror.offload import protocol
from trialerror.offload.retry import retry_offload_entry
from trialerror.offload.stage import MissingStageInputError
from trialerror.offload.transport import LocalTransport
from trialerror.offload.worker import run_worker
from tests._ingest_fixtures import bootstrap_launch, write_scanned_pdf_fixture
from tests._offload_fixtures import (
    StubDevBackends,
    StubOcrBackend,
    queue_one,
    write_offload_toml,
)


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def raw_dir(program_root):
    d = program_root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def offload_program(store, program_root):
    write_offload_toml(program_root)
    return program_root


def _run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def _common(program_root, platform_root):
    return ["--program-root", str(program_root), "--platform-root", str(platform_root)]


def _enqueue(store, job_id="JOB-x", **kwargs):
    return ledger.enqueue(store, kind="custom", payload={"handler": "noop"}, job_id=job_id, **kwargs)


def _make_failed(store, job_id="JOB-failed", *, error="boom"):
    """A row in ``failed`` -- one logic failure with budget still left."""
    _enqueue(store, job_id, max_attempts=3)
    ledger.claim_specific(store, job_id, worker_id="w")
    return ledger.fail(store, job_id, "w", failure_class="logic", error=error)


def _make_abandoned(store, job_id="JOB-abandoned", *, error="spent"):
    """A row in ``abandoned`` by the route the design intends: the logic
    failures ran out. (``ledger.abandon`` reaches the same state, and the
    matrix below covers that one through the CLI.)"""
    _enqueue(store, job_id, max_attempts=1)
    ledger.claim_specific(store, job_id, worker_id="w")
    return ledger.fail(store, job_id, "w", failure_class="logic", error=error)


def _add_scan(store, program_root, raw_dir, *, text=None, name="scan.pdf"):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store,
        kind="paper",
        title="Retry fixture",
        license_tier="open",
        acquisition_route="web",
        registered_by_launch=launch_id,
    )
    path = write_scanned_pdf_fixture(raw_dir / name, text)
    result = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=source["source_id"],
        raw_path=path,
        created_by_launch=launch_id,
        media_type="pdf-scan",
    )
    return result["document"]["doc_id"]


def _cli_offload(program_root, platform_root, verb):
    from trialerror.cli import build_parser

    args = build_parser().parse_args(
        ["offload", verb, "--program-root", str(program_root), "--platform-root", str(platform_root)]
    )
    return args.handler(args)


def _run_dev_worker(program_root, work_root, backends=None, **kwargs):
    root = protocol.offload_root(program_root)
    return run_worker(
        transport=LocalTransport(root, worker_id="dev"),
        backends=backends or StubDevBackends(),
        work_root=work_root,
        worker_id="dev",
        **kwargs,
    )


def _drive_to_terminal_marker(store, program_root, platform_root, raw_dir, tmp_path):
    """The live shape this lane exists for: an OCR job whose DEV attempts
    are spent, its marker terminal in ``failed/`` and its ledger row
    ``abandoned`` -- reached only through the real stage/worker path, never
    by hand-writing the directory."""
    doc_id = _add_scan(store, program_root, raw_dir, text=["CORRUPT PAGE"])
    job_id = f"JOB-ingest-{doc_id}"
    backends = StubDevBackends(ocr=StubOcrBackend(fail_on="CORRUPT"))

    run_one(store, worker_id="w0")
    for attempt in (1, 2, 3):
        _run_dev_worker(program_root, tmp_path / f"a{attempt}", backends=backends)
        _cli_offload(program_root, platform_root, "kick")
        run_one(store, worker_id=f"wr{attempt}")
    for i in range(4, 7):
        ledger.kick(store, job_id)
        run_one(store, worker_id=f"wr{i}")
    assert ledger.get_job(store, job_id)["state"] == "abandoned"
    assert protocol.list_failed(protocol.offload_root(program_root)) == [job_id]
    return doc_id, job_id


# ---------------------------------------------------------------------------
# 1. the state matrix
# ---------------------------------------------------------------------------
def test_retry_from_failed_returns_the_job_to_pending(store):
    _make_failed(store, "JOB-m1")
    row = ledger.retry(store, "JOB-m1", reason="the tool-side defect is fixed")
    assert row["state"] == "pending"


def test_retry_from_abandoned_returns_the_job_to_pending(store):
    _make_abandoned(store, "JOB-m2")
    row = ledger.retry(store, "JOB-m2", reason="the tool-side defect is fixed")
    assert row["state"] == "pending"


def test_retry_refuses_a_complete_job_by_name(store):
    _enqueue(store, "JOB-m3")
    ledger.claim_specific(store, "JOB-m3", worker_id="w")
    ledger.complete(store, "JOB-m3", "w")
    with pytest.raises(InvalidTransitionError) as exc:
        ledger.retry(store, "JOB-m3", reason="why not")
    assert "'complete'" in str(exc.value)
    assert "nothing to retry" in str(exc.value)
    assert ledger.get_job(store, "JOB-m3")["state"] == "complete"


def test_retry_refuses_a_pending_job_by_name(store):
    _enqueue(store, "JOB-m4")
    with pytest.raises(InvalidTransitionError) as exc:
        ledger.retry(store, "JOB-m4", reason="why not")
    assert "'pending'" in str(exc.value)
    assert "already claimable" in str(exc.value)


def test_retry_refuses_a_claimed_job_and_names_its_worker(store):
    _enqueue(store, "JOB-m5")
    ledger.claim_specific(store, "JOB-m5", worker_id="w-holder")
    with pytest.raises(InvalidTransitionError) as exc:
        ledger.retry(store, "JOB-m5", reason="why not")
    assert "'claimed'" in str(exc.value)
    assert "w-holder" in str(exc.value)
    assert ledger.get_job(store, "JOB-m5")["state"] == "claimed"


def test_retry_refuses_a_running_job(store):
    _enqueue(store, "JOB-m6")
    ledger.claim_specific(store, "JOB-m6", worker_id="w")
    ledger.heartbeat(store, "JOB-m6", "w")
    assert ledger.get_job(store, "JOB-m6")["state"] == "running"
    with pytest.raises(InvalidTransitionError) as exc:
        ledger.retry(store, "JOB-m6", reason="why not")
    assert "'running'" in str(exc.value)


def test_retry_refuses_a_paused_job_and_points_at_resume(store):
    _enqueue(store, "JOB-m7")
    ledger.pause(store, "JOB-m7")
    with pytest.raises(InvalidTransitionError) as exc:
        ledger.retry(store, "JOB-m7", reason="why not")
    assert "'paused'" in str(exc.value)
    assert "jobs resume" in str(exc.value)


def test_retry_refuses_an_unknown_job_id(store):
    with pytest.raises(JobNotFoundError):
        ledger.retry(store, "JOB-nope", reason="why not")


def test_retry_refuses_an_empty_reason(store):
    _make_failed(store, "JOB-m8")
    with pytest.raises(ValueError):
        ledger.retry(store, "JOB-m8", reason="   ")
    assert ledger.get_job(store, "JOB-m8")["state"] == "failed"


# ---------------------------------------------------------------------------
# 2. the fields, the error prefix, the event, the bounds
# ---------------------------------------------------------------------------
def test_retry_resets_every_settled_field(store):
    _make_failed(store, "JOB-f1")
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET claimed_by='ghost', lease_expires_ts='2999-01-01T00:00:00.000Z', "
            "settled_ts='2026-01-01T00:00:00.000Z' WHERE job_id='JOB-f1'"
        )
    row = ledger.retry(store, "JOB-f1", reason="fixed upstream")
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["next_attempt_ts"] is None
    assert row["claimed_by"] is None
    assert row["lease_expires_ts"] is None
    assert row["settled_ts"] is None
    assert row["failure_class"] is None


def test_retry_keeps_the_previous_error_behind_a_prefix(store):
    _make_failed(store, "JOB-f2", error="terminal after 3 DEV attempt(s) -- stub marker")
    row = ledger.retry(store, "JOB-f2", reason="worker updated")
    assert row["last_error"].startswith(f"{ledger.RETRY_ERROR_PREFIX} ")
    assert "terminal after 3 DEV attempt(s)" in row["last_error"], (
        "a retried row must still say on `jobs list` what it failed with"
    )


def test_retry_of_a_row_with_no_error_still_records_the_retry(store):
    _enqueue(store, "JOB-f3", max_attempts=1)
    ledger.claim_specific(store, "JOB-f3", worker_id="w")
    ledger.fail(store, "JOB-f3", "w", failure_class="logic", error="x")
    with store.jobs:
        store.jobs.execute("UPDATE job SET last_error = NULL WHERE job_id='JOB-f3'")
    row = ledger.retry(store, "JOB-f3", reason="r")
    assert ledger.RETRY_ERROR_PREFIX in row["last_error"]
    assert ledger.RETRY_NO_PREVIOUS_ERROR in row["last_error"]


def test_retry_keeps_the_checkpoint_unless_asked_to_clear_it(store):
    _make_failed(store, "JOB-f4")
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET checkpoint = ? WHERE job_id='JOB-f4'", (json.dumps({"cursor": 7}),)
        )
    kept = ledger.retry(store, "JOB-f4", reason="r")
    assert json.loads(kept["checkpoint"]) == {"cursor": 7}

    _make_failed(store, "JOB-f5")
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET checkpoint = ? WHERE job_id='JOB-f5'", (json.dumps({"cursor": 7}),)
        )
    cleared = ledger.retry(store, "JOB-f5", reason="r", clear_checkpoint=True)
    assert cleared["checkpoint"] is None


def test_retry_leaves_max_attempts_alone_unless_told(store):
    _make_failed(store, "JOB-f6")
    assert ledger.retry(store, "JOB-f6", reason="r")["max_attempts"] == 3
    _make_failed(store, "JOB-f7")
    assert ledger.retry(store, "JOB-f7", reason="r", max_attempts=5)["max_attempts"] == 5


@pytest.mark.parametrize("bad", [0, -1, 11, 100])
def test_max_attempts_is_bounded(store, bad):
    _make_failed(store, "JOB-f8")
    with pytest.raises(ValueError):
        ledger.retry(store, "JOB-f8", reason="r", max_attempts=bad)
    assert ledger.get_job(store, "JOB-f8")["state"] == "failed", "a refused bound must write nothing"


@pytest.mark.parametrize("good", [1, 10])
def test_max_attempts_accepts_its_own_bounds(store, good):
    _make_failed(store, f"JOB-f9-{good}")
    row = ledger.retry(store, f"JOB-f9-{good}", reason="r", max_attempts=good)
    assert row["max_attempts"] == good


def test_the_ledger_event_carries_the_whole_previous_state(store):
    _make_abandoned(store, "JOB-e1", error="terminal after 3 DEV attempt(s) -- corrupt")
    ledger.retry(store, "JOB-e1", reason="worker updated on DEV", by_launch="LNCH-1")
    events = ledger.list_events(store, "JOB-e1")
    assert [e["type"] for e in events][-1] == "job_retried"
    detail = json.loads(events[-1]["detail"])
    assert detail["previous_state"] == "abandoned"
    assert detail["previous_attempts"] == 1
    assert detail["previous_last_error"] == "terminal after 3 DEV attempt(s) -- corrupt"
    assert detail["reason"] == "worker updated on DEV"
    assert detail["by_launch"] == "LNCH-1"
    assert detail["ts"]


def test_a_retried_job_is_claimable_again(store):
    _make_abandoned(store, "JOB-e2")
    assert ledger.claim_specific(store, "JOB-e2", worker_id="w2") is None
    ledger.retry(store, "JOB-e2", reason="r")
    assert ledger.claim_specific(store, "JOB-e2", worker_id="w2") is not None


# ---------------------------------------------------------------------------
# 3. the offload inverse
# ---------------------------------------------------------------------------
def test_the_failed_marker_goes_back_to_queued_and_pending_with_the_stamp(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    error_bytes = (protocol.failed_dir(root) / job_id / protocol.ERROR_FILENAME).read_bytes()

    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "marker fixed on DEV", *_common(offload_program, platform_root)],
        capsys,
    )
    assert rc == 0 and env["ok"] is True

    record = protocol.queued_manifest(root, job_id)
    assert record is not None, "the sandbox's own record must come back -- fail_marker forgot it"
    assert record["offload_attempts"] == 0
    assert record["retried"][-1]["reason"] == "marker fixed on DEV"
    assert record["retried"][-1]["previous_attempts"] == 3
    assert record["retried"][-1]["previous_error_sha256"] == protocol.sha256_bytes(error_bytes)
    assert protocol.list_pending(root) == [job_id], "and the work queue entry with it"
    assert protocol.read_json(protocol.pending_dir(root) / f"{job_id}.json") == record
    assert env["result"]["offload"]["moved"] is True
    assert env["result"]["offload"]["attempts_reset"] == 3


def test_the_evidence_directory_is_kept_not_deleted(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    before = protocol.read_json(protocol.failed_dir(root) / job_id / protocol.ERROR_FILENAME)

    _run(["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)], capsys)

    assert not (protocol.failed_dir(root) / job_id).exists()
    kept = protocol.list_retried(root)
    assert [k["job_id"] for k in kept] == [job_id]
    entry = protocol.retried_dir(root) / kept[0]["entry"]
    assert protocol.read_json(entry / protocol.ERROR_FILENAME) == before, (
        "the archived pair is the evidence -- a retry that edited it would be worth less"
    )
    assert protocol.read_json(entry / protocol.MANIFEST_FILENAME)["offload_attempts"] == 3
    note = protocol.read_json(entry / protocol.RETRY_NOTE_FILENAME)
    assert note["reason"] == "r" and note["job_id"] == job_id
    assert kept[0]["reason"] == "r"


def test_the_evidence_name_carries_no_character_windows_refuses(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    """The directory name is a path component on both machines, and an ISO
    stamp carries ``:`` -- a drive separator and an ADS separator on
    Windows, the same character :func:`protocol.unpack_into` refuses."""
    _doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    _run(["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)], capsys)
    name = protocol.list_retried(protocol.offload_root(offload_program))[0]["entry"]
    assert ":" not in name and "\\" not in name and "/" not in name


def test_a_second_retry_after_a_second_failure_appends_a_second_stamp(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    _run(["jobs", "retry", job_id, "--reason", "first go", *_common(offload_program, platform_root)], capsys)

    # the GPU fails it all over again, three more times, and the row abandons
    backends = StubDevBackends(ocr=StubOcrBackend(fail_on="CORRUPT"))
    for attempt in (1, 2, 3):
        _run_dev_worker(offload_program, tmp_path / f"b{attempt}", backends=backends)
        _cli_offload(offload_program, platform_root, "kick")
        run_one(store, worker_id=f"br{attempt}")
    for i in range(4, 7):
        ledger.kick(store, job_id)
        run_one(store, worker_id=f"br{i}")
    assert protocol.list_failed(root) == [job_id]
    assert ledger.get_job(store, job_id)["state"] == "abandoned"

    _run(["jobs", "retry", job_id, "--reason", "second go", *_common(offload_program, platform_root)], capsys)
    record = protocol.queued_manifest(root, job_id)
    assert [s["reason"] for s in record["retried"]] == ["first go", "second go"]
    assert record["offload_attempts"] == 0
    assert len(protocol.list_retried(root)) == 2, "both terminal attempts are kept"


def test_a_requeue_after_a_retry_carries_the_retry_history_forward(store, program_root):
    """Found while writing the two-stamp test above, and worth its own
    unit: :func:`protocol.queue_marker` BUILDS a manifest rather than
    copying one, so the first DEV failure after a retry re-queued the job
    with the ``retried`` list dropped -- an evidence directory under
    ``failed/_retried/`` that the live manifest no longer pointed back at,
    and a second retry that looked like a first."""
    root = protocol.offload_root(program_root)
    queue_one(root, "JOB-hist")
    manifest = protocol.read_json(protocol.pending_dir(root) / "JOB-hist.json")
    manifest["retried"] = [{"ts": "2026-01-01T00:00:00.000Z", "reason": "earlier", "previous_attempts": 3}]
    requeued = protocol.requeue_marker(
        root, "JOB-hist", manifest={**manifest, "offload_attempts": 1}, inputs=[("input.txt", b"x")]
    )
    assert requeued["retried"] == manifest["retried"]
    assert protocol.queued_manifest(root, "JOB-hist")["retried"] == manifest["retried"]


def test_a_manifest_with_no_retry_history_gains_no_key(store, program_root):
    """The other direction of the same change: additive by construction, so
    every manifest this codebase already writes is unchanged."""
    root = protocol.offload_root(program_root)
    queue_one(root, "JOB-nohist")
    assert "retried" not in protocol.read_json(protocol.pending_dir(root) / "JOB-nohist.json")


def test_a_missing_input_payload_is_refused_by_name_and_nothing_moves(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    (raw_dir / "scan.pdf").unlink()

    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)], capsys
    )
    assert rc == 1 and env["ok"] is False
    assert env["error"]["code"] == "offload_input_missing"
    assert "raw file is missing" in env["error"]["message"]
    assert protocol.list_failed(root) == [job_id], "the terminal marker stays put"
    assert protocol.list_retried(root) == []
    assert protocol.list_pending(root) == []
    assert ledger.get_job(store, job_id)["state"] == "abandoned", (
        "a retry that cannot put the work back must not settle the row as though it had"
    )


def test_a_retracted_document_is_refused_by_name(store, offload_program, platform_root, raw_dir, tmp_path):
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    with store.knowledge:
        store.knowledge.execute("DELETE FROM document WHERE doc_id = ?", (doc_id,))
    with pytest.raises(MissingStageInputError) as exc:
        retry_offload_entry(store, root, job_id, reason="r")
    assert "no longer in this program's record" in str(exc.value)


def test_a_parked_largeformat_twin_is_named_in_warnings_and_untouched(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    parked = protocol.parked_largeformat_dir(root) / job_id
    parked.mkdir(parents=True)
    (parked / "note.txt").write_text("a human parked this", encoding="utf-8")

    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)], capsys
    )
    assert rc == 0
    codes = [w["code"] for w in env["warnings"]]
    assert "parked_largeformat_twin" in codes
    assert protocol.PARKED_LARGEFORMAT_DIRNAME in json.dumps(env["warnings"])
    assert (parked / "note.txt").read_text(encoding="utf-8") == "a human parked this"
    assert [p.name for p in parked.iterdir()] == ["note.txt"]


def test_a_crash_between_the_two_halves_is_completed_by_a_second_call(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    """The ordering acceptance. The queue half lands, the process dies
    before the store half, and running the same command again completes it
    -- without a second evidence directory and without a second stamp."""
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)

    # the queue half only -- exactly what the CLI does first
    retry_offload_entry(store, root, job_id, reason="crashed run")
    assert ledger.get_job(store, job_id)["state"] == "abandoned"
    assert protocol.queue_half_retried(root, job_id) is not None

    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "second call", *_common(offload_program, platform_root)],
        capsys,
    )
    assert rc == 0
    assert ledger.get_job(store, job_id)["state"] == "pending"
    assert env["result"]["offload"]["already_retried"] is True
    assert env["result"]["offload"]["moved"] is False
    assert "offload_half_already_done" in [w["code"] for w in env["warnings"]]
    assert len(protocol.list_retried(root)) == 1, "no second evidence directory"
    assert len(protocol.queued_manifest(root, job_id)["retried"]) == 1, "no second stamp"


def test_a_crash_after_the_evidence_move_rebuilds_from_the_archived_manifest(
    store, offload_program, platform_root, raw_dir, tmp_path
):
    """The other interruption point inside the queue half: the evidence is
    archived and the queue entry was never written. The archived manifest is
    then the only copy of what to re-queue, and a second call must not make
    a SECOND evidence directory out of nothing."""
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    retry_offload_entry(store, root, job_id, reason="first")
    # undo the two manifest writes, leaving the archived evidence behind
    (protocol.queued_dir(root) / f"{job_id}.json").unlink()
    (protocol.pending_dir(root) / f"{job_id}.json").unlink()

    result = retry_offload_entry(store, root, job_id, reason="second")
    assert result["moved"] is True
    assert "offload_evidence_already_archived" in [w["code"] for w in result["warnings"]]
    assert len(protocol.list_retried(root)) == 1
    assert protocol.queued_manifest(root, job_id)["offload_attempts"] == 0
    assert protocol.list_pending(root) == [job_id]


def test_a_job_with_no_offload_entry_retries_cleanly(store, program_root, platform_root, capsys):
    _make_abandoned(store, "JOB-plain")
    rc, env = _run(
        ["jobs", "retry", "JOB-plain", "--reason", "r", *_common(program_root, platform_root)], capsys
    )
    assert rc == 0
    assert env["result"]["offload"] == {
        "moved": False,
        "from": None,
        "to": None,
        "attempts_reset": None,
        "state": "none",
        "already_retried": False,
    }
    assert "warnings" not in env


def test_the_envelope_carries_the_shape_the_brief_names(store, program_root, platform_root, capsys):
    _make_abandoned(store, "JOB-env", error="terminal after 3 DEV attempt(s)")
    rc, env = _run(
        ["jobs", "retry", "JOB-env", "--reason", "r", *_common(program_root, platform_root)], capsys
    )
    assert rc == 0
    result = env["result"]
    for key in ("job_id", "kind", "previous_state", "previous_attempts", "state", "offload", "warnings"):
        assert key in result, key
    assert result["previous_state"] == "abandoned"
    assert result["previous_attempts"] == 1
    assert result["state"] == "pending"
    assert ["jobs", "kick"] == env["nextActions"][0]["argv"][1:3]


def test_the_offload_next_action_says_the_worker_claims_it(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    _doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)], capsys
    )
    assert rc == 0
    assert any("claims it on its next poll" in (a.get("description") or "") for a in env["nextActions"])


# ---------------------------------------------------------------------------
# the counts: status and doctor
# ---------------------------------------------------------------------------
def test_status_and_doctor_count_retried_separately_and_do_not_fire_on_it(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    _doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    before = _cli_offload(offload_program, platform_root, "status")
    assert before["result"]["failed"] == 1 and before["result"]["retried"] == 0

    _run(["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)], capsys)

    after = _cli_offload(offload_program, platform_root, "status")
    assert after["result"]["failed"] == 0, "a retried marker is not an outstanding failure"
    assert after["result"]["retried"] == 1
    assert after["result"]["retried_entries"][0]["job_id"] == job_id

    doctor = _cli_offload(offload_program, platform_root, "doctor")
    check = next(c for c in doctor["result"]["checks"] if c["name"] == "offload_failed")
    assert check["status"] == "pass", "the doctor must not keep warning about work already dealt with"
    assert check["details"]["retried_count"] == 1
    assert check["details"]["failed"] == []


# ---------------------------------------------------------------------------
# the end-to-end acceptance: a retried job really runs
# ---------------------------------------------------------------------------
def test_a_retried_offload_job_is_claimed_by_the_worker_and_completes(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    """The whole point of the lane: after the fix lands on the worker, the
    documents that met the defect first can be run again."""
    doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    # "the fix": the raw file no longer trips the stub backend
    write_scanned_pdf_fixture(raw_dir / "scan.pdf", ["Recovered page one.", "Recovered page two."])

    rc, _env = _run(
        ["jobs", "retry", job_id, "--reason", "worker updated", *_common(offload_program, platform_root)],
        capsys,
    )
    assert rc == 0

    summary = _run_dev_worker(offload_program, tmp_path / "fixed")
    assert summary["published"] == [job_id]
    _cli_offload(offload_program, platform_root, "kick")
    assert run_one(store, worker_id="after")["status"] == "complete"
    elements = store.knowledge.execute(
        "SELECT text FROM element WHERE doc_id = ? ORDER BY seq", (doc_id,)
    ).fetchall()
    assert [e["text"] for e in elements] == ["Recovered page one.", "Recovered page two."]


def test_the_cli_help_lists_the_verb():
    from trialerror.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["jobs", "retry"])  # job_id and --reason are required
    args = parser.parse_args(["jobs", "retry", "JOB-1", "--reason", "r"])
    assert args.jobs_cmd == "retry"
    assert args.reason == "r"


# ---------------------------------------------------------------------------
# 4. the probes -- kept as regression tests, findings and non-findings alike
# ---------------------------------------------------------------------------
def test_probe_a_a_job_a_worker_holds_keeps_its_offload_marker(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    """PROBE (a): retry a job a worker currently holds.

    FOUND: the state refusal has to come BEFORE the queue move, or a
    refused retry still takes the marker out of ``failed/`` on its way to
    saying no. The CLI checks ``ledger.retry_refusal`` first for exactly
    this reason; this test is what holds that order in place."""
    _doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)
    # put the ledger row back under a live lease without touching the queue
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET state='running', claimed_by='w-live', "
            "lease_expires_ts='2999-01-01T00:00:00.000Z' WHERE job_id = ?",
            (job_id,),
        )

    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)], capsys
    )
    assert rc == 1 and env["error"]["code"] == "InvalidTransitionError"
    assert protocol.list_failed(root) == [job_id], "a refusal must move nothing"
    assert protocol.list_retried(root) == []
    assert protocol.list_pending(root) == []
    assert ledger.get_job(store, job_id)["claimed_by"] == "w-live"


def test_probe_a2_a_ledger_job_id_the_queue_cannot_name_is_still_retryable(
    store, program_root, platform_root, capsys
):
    """PROBE (a), second finding: the OFFLOAD id rule was gating a job that
    has nothing to do with the offload queue.

    ``job.job_id`` is unconstrained in the ledger; the queue's
    ``validate_job_id`` is not, because the id becomes a path component on
    two machines. The ``parked_largeformat`` lookup validated the id on its
    own path rather than behind the "could this queue ever name it" guard,
    so the moment a program grew a ``parked_largeformat/`` directory, every
    ledger job whose id the queue dislikes became unretryable -- refused
    with an offload error for a job with no offload entry."""
    job_id = "JOB with spaces"
    _make_abandoned(store, job_id)
    protocol.parked_largeformat_dir(protocol.offload_root(program_root)).mkdir(parents=True)

    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "r", *_common(program_root, platform_root)], capsys
    )
    assert rc == 0, env
    assert env["result"]["offload"]["state"] == "none"
    assert ledger.get_job(store, job_id)["state"] == "pending"


def test_probe_a3_the_conditional_update_is_the_guard_not_the_read(store, monkeypatch):
    """PROBE (a), third angle: the state check and the write are two
    statements, so what happens if the row changes between them?

    NOTHING FOUND: the UPDATE is gated on ``state IN ('failed','abandoned')``
    itself, so the read is only there to produce a good message. With the
    read neutered, the second retry still matches zero rows and raises --
    it does not silently reset a row somebody else has since claimed."""
    _make_abandoned(store, "JOB-race")
    monkeypatch.setattr(ledger, "retry_refusal", lambda *a, **k: None)
    ledger.retry(store, "JOB-race", reason="first")
    with pytest.raises(InvalidTransitionError) as exc:
        ledger.retry(store, "JOB-race", reason="second")
    assert "concurrently" in str(exc.value)
    assert ledger.get_job(store, "JOB-race")["attempts"] == 0


def test_a_second_retry_prefixes_without_losing_the_original_error(store):
    """The prefix stacks, and that is the intended reading: two retries of
    one job are two facts about it, and the original failure is still the
    tail of the string."""
    _make_abandoned(store, "JOB-twice", error="terminal after 3 DEV attempt(s)")
    ledger.retry(store, "JOB-twice", reason="a")
    with store.jobs:
        store.jobs.execute("UPDATE job SET state='abandoned' WHERE job_id='JOB-twice'")
    second = ledger.retry(store, "JOB-twice", reason="b")
    assert second["last_error"].count(ledger.RETRY_ERROR_PREFIX) == 2
    assert second["last_error"].endswith("terminal after 3 DEV attempt(s)")


def test_jobs_logs_shows_the_retry_event(store, program_root, platform_root, capsys):
    """The brief names ``jobs logs`` as the ledger the event has to reach,
    so it is asserted through that verb and not only through the API."""
    _make_abandoned(store, "JOB-logs")
    ledger.retry(store, "JOB-logs", reason="r")
    rc, env = _run(["jobs", "logs", "JOB-logs", *_common(program_root, platform_root)], capsys)
    assert rc == 0
    assert [e["type"] for e in env["result"]["events"]] == [
        "enqueued", "claimed", "abandoned", "job_retried",
    ]


def test_retrying_a_job_whose_result_already_landed_destroys_nothing(
    store, program_root, platform_root, capsys
):
    """A marker in ``done/`` is a published result. Retrying the ledger row
    must warn and leave it alone -- deleting it would throw away GPU work
    that has already been paid for and verified."""
    from tests._offload_fixtures import publish_stub_result

    root = protocol.offload_root(program_root)
    queue_one(root, "JOB-done")
    publish_stub_result(root, "JOB-done")
    _make_abandoned(store, "JOB-done")

    rc, env = _run(
        ["jobs", "retry", "JOB-done", "--reason", "r", *_common(program_root, platform_root)], capsys
    )
    assert rc == 0
    assert env["result"]["offload"]["moved"] is False
    assert "offload_result_published" in [w["code"] for w in env["warnings"]]
    assert protocol.list_done(root) == ["JOB-done"]


def test_probe_b_a_job_named_retried_cannot_destroy_the_evidence_archive(store, program_root):
    """PROBE (b): lose the evidence directory.

    FOUND: ``failed/_retried/`` lives inside ``failed/``, and
    ``fail_marker`` starts by ``_rmtree``-ing ``failed/<job_id>/`` -- so a
    job whose id was literally ``_retried`` would delete every kept
    evidence directory in the program the first time it failed on the GPU.
    The id is now reserved, like ``CONTROL`` and ``_ranges`` before it."""
    root = protocol.offload_root(program_root)
    protocol.ensure_layout(root)
    keep = protocol.retried_dir(root) / "JOB-kept.20260101T000000Z"
    keep.mkdir(parents=True)
    (keep / protocol.ERROR_FILENAME).write_text("{}", encoding="utf-8")

    with pytest.raises(protocol.OffloadProtocolError) as exc:
        protocol.fail_marker(
            root, protocol.RETRIED_DIRNAME, manifest={"stage": "ocr"}, error="x"
        )
    assert "reserved" in str(exc.value)
    assert (keep / protocol.ERROR_FILENAME).is_file()
    assert protocol.list_retried(root) == [] or protocol.list_retried(root)[0]["job_id"] == "JOB-kept"


def test_probe_b2_list_failed_never_reports_the_archive_as_a_job(store, program_root):
    """PROBE (b), second half: the archive must not read as a failure, or
    ``offload doctor`` grows a finding nothing can ever clear."""
    root = protocol.offload_root(program_root)
    protocol.ensure_layout(root)
    (protocol.retried_dir(root) / "JOB-x.20260101T000000Z").mkdir(parents=True)
    assert protocol.list_failed(root) == []
    assert protocol.counts(root)["failed"] == 0
    assert protocol.counts(root)["retried"] == 1


def test_probe_c_calling_retry_twice_does_not_double_queue(
    store, offload_program, platform_root, raw_dir, tmp_path, capsys
):
    """PROBE (c): double-queue a job by calling retry twice.

    NOTHING FOUND, and the reason is structural: the second call is refused
    on the ledger row's state (``pending`` is not retryable) before it
    reaches the queue at all, and even the queue half on its own is
    idempotent -- :func:`protocol.queue_half_retried` detects the completed
    half and :func:`protocol.retry_marker` returns without writing. Tried
    both orders: CLI twice, and the queue half twice directly."""
    _doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)

    rc, _ = _run(["jobs", "retry", job_id, "--reason", "r1", *_common(offload_program, platform_root)], capsys)
    assert rc == 0
    rc, env = _run(["jobs", "retry", job_id, "--reason", "r2", *_common(offload_program, platform_root)], capsys)
    assert rc == 1 and env["error"]["code"] == "InvalidTransitionError"
    assert "already claimable" in env["error"]["message"]

    assert protocol.list_pending(root) == [job_id], "exactly one queue entry, not two"
    assert len(protocol.list_retried(root)) == 1
    assert len(protocol.queued_manifest(root, job_id)["retried"]) == 1

    # and the queue half alone, twice
    second = retry_offload_entry(store, root, job_id, reason="r3")
    assert second["already_retried"] is True and second["moved"] is False
    assert len(protocol.queued_manifest(root, job_id)["retried"]) == 1
    assert len(protocol.list_retried(root)) == 1


def test_probe_d_an_exception_between_the_halves_leaves_a_recoverable_state(
    store, offload_program, platform_root, raw_dir, tmp_path, monkeypatch, capsys
):
    """PROBE (d): leave the store and the queue disagreeing.

    NOTHING FOUND beyond what the ordering already promises. An exception
    injected between the two halves leaves the queue entry back and the row
    still terminal -- the recoverable direction -- and a second call
    completes it. The opposite ordering could not be recovered by repeating
    the command, which is why this order is the design."""
    _doc_id, job_id = _drive_to_terminal_marker(
        store, offload_program, platform_root, raw_dir, tmp_path
    )
    root = protocol.offload_root(offload_program)

    boom = RuntimeError("power cut between the halves")

    def _explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(ledger, "retry", _explode)
    with pytest.raises(RuntimeError):
        main(["jobs", "retry", job_id, "--reason", "r", *_common(offload_program, platform_root)])
    capsys.readouterr()
    monkeypatch.undo()

    # the recoverable disagreement: queue back, row still terminal
    assert protocol.list_pending(root) == [job_id]
    assert ledger.get_job(store, job_id)["state"] == "abandoned"

    rc, env = _run(
        ["jobs", "retry", job_id, "--reason", "after the crash", *_common(offload_program, platform_root)],
        capsys,
    )
    assert rc == 0
    assert ledger.get_job(store, job_id)["state"] == "pending"
    assert env["result"]["offload"]["already_retried"] is True
    assert len(protocol.list_retried(root)) == 1


def test_probe_e_the_doctor_does_not_fire_on_the_archive(store, program_root, platform_root):
    """PROBE (e): make ``offload doctor`` fire on ``_retried``.

    FOUND (before the fix): ``list_failed`` returned every directory under
    ``failed/``, so the archive itself counted as one more failed job --
    a permanent ``warn`` with a job id of ``_retried``. Tried three shapes:
    the archive empty, the archive holding one entry, and the archive
    holding an entry whose ``retried.json`` is unreadable."""
    from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

    root = protocol.offload_root(program_root)
    protocol.ensure_layout(root)
    protocol.retried_dir(root).mkdir(parents=True, exist_ok=True)
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root, platform_root=platform_root)

    def _offload_failed():
        return next(r for r in run_checks(ctx, only=["offload_failed"]))

    assert _offload_failed().status == "pass"

    entry = protocol.retried_dir(root) / "JOB-y.20260101T000000Z"
    entry.mkdir(parents=True)
    (entry / protocol.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
    result = _offload_failed()
    assert result.status == "pass"
    assert result.details["failed"] == []
    assert result.details["retried_count"] == 1

    (entry / protocol.RETRY_NOTE_FILENAME).write_text("{not json", encoding="utf-8")
    result = _offload_failed()
    assert result.status == "pass", "an unreadable note is still not a failure"
    assert result.details["retried"][0]["job_id"] == "JOB-y"


def test_probe_e2_a_real_failure_still_warns_alongside_the_archive(store, program_root, platform_root):
    """The other half of probe (e): silencing the archive must not silence
    an actual outstanding failure sitting beside it."""
    from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

    root = protocol.offload_root(program_root)
    queue_one(root, "JOB-still-bad")
    manifest = protocol.read_json(protocol.pending_dir(root) / "JOB-still-bad.json")
    protocol.fail_marker(root, "JOB-still-bad", manifest=manifest, error="really failed")
    (protocol.retried_dir(root) / "JOB-y.20260101T000000Z").mkdir(parents=True)

    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root, platform_root=platform_root)
    result = next(r for r in run_checks(ctx, only=["offload_failed"]))
    assert result.status == "warn"
    assert result.details["failed"] == ["JOB-still-bad"]
    assert result.details["retried_count"] == 1
    assert "1 retried (evidence kept)" in result.message
