"""Lane L0-C: the handler-level seam, end to end.

Design acceptance C-enq (a ``pdf-scan`` ``add_document`` produces
``pending/<job>.json``) and the in-process half of C-e2e (one scanned PDF
and one whole-document embed batch round trip to ``complete``), plus
``fake_backend_refused`` and the ``sha_mismatch_to_failed`` / C-fail
ledger halves.

Everything runs through :func:`trialerror.jobs.worker.run_one` -- the same
claim-run-settle loop a real detached worker uses -- and
:class:`trialerror.offload.transport.LocalTransport`, so the whole seam is
exercised with no SSH and no GPU.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from trialerror.ingest import pipeline
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.offload import protocol
from trialerror.offload.transport import LocalTransport
from trialerror.offload.worker import ConfigDevBackends, WorkerConfigError, run_worker
from trialerror.util.timeutil import now, parse
from tests._ingest_fixtures import bootstrap_launch, write_scanned_pdf_fixture
from tests._offload_fixtures import (
    STUB_DIMS,
    STUB_MODEL_KEY,
    STUB_OCR_NAME,
    StubDevBackends,
    StubOcrBackend,
    write_offload_toml,
)


@pytest.fixture()
def raw_dir(program_root):
    d = program_root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def offload_program(store, program_root):
    write_offload_toml(program_root)
    return program_root


def _add_scan(store, program_root, raw_dir, *, text: list[str] | None = None):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store,
        kind="paper",
        title="Offload fixture",
        license_tier="open",
        acquisition_route="web",
        registered_by_launch=launch_id,
    )
    path = write_scanned_pdf_fixture(raw_dir / "scan.pdf", text)
    result = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=source["source_id"],
        raw_path=path,
        created_by_launch=launch_id,
        media_type="pdf-scan",
    )
    return result["document"]["doc_id"]


def _drain(store, max_steps=12):
    results = []
    for i in range(max_steps):
        r = run_one(store, worker_id=f"w{i}")
        results.append(r)
        if r["status"] == "idle":
            break
    return results


def _kick(program_root, platform_root):
    """Run the real ``trialerror offload kick`` through the registered CLI
    group -- the sandbox's jobs window calls exactly this every cycle, and
    it is what un-delays a parked job once its result lands."""
    from trialerror.cli import build_parser

    args = build_parser().parse_args(
        [
            "offload",
            "kick",
            "--program-root",
            str(program_root),
            "--platform-root",
            str(platform_root),
        ]
    )
    return args.handler(args)


def _run_dev_worker(program_root, tmp_path, backends=None, **kwargs):
    root = protocol.offload_root(program_root)
    transport = LocalTransport(root, worker_id="dev")
    return run_worker(
        transport=transport,
        backends=backends or StubDevBackends(),
        work_root=tmp_path / "devwork",
        worker_id="dev",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# C-enq: enqueue and park
# ---------------------------------------------------------------------------
def test_ocr_stage_queues_a_marker_and_parks_the_job(store, offload_program, raw_dir):
    """C-enq. The stage writes inputs + manifest and defers -- and the
    deferral is ENVIRONMENTAL, so the ledger row's attempt budget is
    untouched no matter how long DEV stays off."""
    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"

    result = run_one(store, worker_id="w0")
    assert result == {"status": "deferred", "job_id": job_id, "worker_id": "w0"}

    root = protocol.offload_root(offload_program)
    assert protocol.list_pending(root) == [job_id]
    manifest = protocol.read_json(protocol.pending_dir(root) / f"{job_id}.json")
    assert manifest["stage"] == "ocr"
    assert manifest["doc_id"] == doc_id
    assert manifest["offload_attempts"] == 0
    assert manifest["expect"]["backend"] == STUB_OCR_NAME
    assert (protocol.pending_dir(root) / job_id / "input.pdf").is_file()

    job = ledger.get_job(store, job_id)
    assert job["state"] == "pending"
    assert job["attempts"] == 0, "an absent GPU must never consume a retry attempt"
    assert job["next_attempt_ts"] is not None


def test_a_second_claim_while_parked_writes_nothing_new(store, offload_program, raw_dir):
    """Design step 2. Two ledger attempts against a queued marker must not
    produce two markers, two input copies, or a reset attempt counter."""
    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)

    run_one(store, worker_id="w0")
    before = (protocol.pending_dir(root) / f"{job_id}.json").read_bytes()

    ledger.kick(store, job_id)
    result = run_one(store, worker_id="w1")
    assert result["status"] == "deferred"
    assert (protocol.pending_dir(root) / f"{job_id}.json").read_bytes() == before


def test_a_claimed_marker_also_parks_without_requeueing(store, offload_program, raw_dir):
    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    run_one(store, worker_id="w0")
    protocol.server_claim(root, job_id, worker_id="dev")

    ledger.kick(store, job_id)
    assert run_one(store, worker_id="w1")["status"] == "deferred"
    assert protocol.find_manifest(root, job_id)[0] == "claimed"
    assert protocol.list_pending(root) == []


# ---------------------------------------------------------------------------
# the full round trip
# ---------------------------------------------------------------------------
def test_full_round_trip_pending_to_indexed(store, offload_program, raw_dir, tmp_path, platform_root):
    """C-e2e, in process: pending -> claimed -> done -> published -> the
    stage completes, for BOTH GPU stages, and the document reaches
    ``indexed`` with real chunk/emb rows."""
    doc_id = _add_scan(store, offload_program, raw_dir)
    ocr_job = f"JOB-ingest-{doc_id}"
    embed_job = f"JOB-ingest-{doc_id}-embed"
    root = protocol.offload_root(offload_program)

    # 1. the sandbox parks the OCR stage
    assert run_one(store, worker_id="w0")["status"] == "deferred"
    assert protocol.list_pending(root) == [ocr_job]

    # 2. DEV drains the queue
    summary = _run_dev_worker(offload_program, tmp_path)
    assert summary["published"] == [ocr_job]
    assert summary["message"].startswith("Queue empty")
    assert protocol.list_done(root) == [ocr_job]
    assert protocol.list_claims(root) == []

    # 3. the sandbox un-delays the parked job and finishes the stage
    kicked = _kick(offload_program, platform_root)
    assert kicked["ok"] and kicked["result"]["kicked"] == [ocr_job]
    assert run_one(store, worker_id="w1")["status"] == "complete"

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "normalized"
    assert doc["ocr_backend"] == STUB_OCR_NAME, "the REAL backend name travels back with the result"
    elements = store.knowledge.execute("SELECT * FROM element WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
    assert [e["detection_origin"] for e in elements] == [f"ocr:{STUB_OCR_NAME}"] * len(elements)
    assert len(elements) == 2

    # 4. chunk runs locally (no GPU), embed parks
    assert run_one(store, worker_id="w2")["status"] == "complete"  # chunk
    assert run_one(store, worker_id="w3")["status"] == "deferred"  # embed -> offload
    assert protocol.list_pending(root) == [embed_job]
    manifest = protocol.read_json(protocol.pending_dir(root) / f"{embed_job}.json")
    chunk_ids = [
        r["chunk_id"]
        for r in store.knowledge.execute("SELECT chunk_id FROM chunk WHERE doc_id=? ORDER BY seq", (doc_id,))
    ]
    assert manifest["expect"]["chunk_ids"] == chunk_ids
    assert manifest["expect"]["model_key"] == STUB_MODEL_KEY
    payload = (protocol.pending_dir(root) / embed_job / "chunks.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line)["chunk_id"] for line in payload.splitlines()] == chunk_ids

    # 5. DEV embeds the whole document in one claim
    summary = _run_dev_worker(offload_program, tmp_path)
    assert summary["published"] == [embed_job]

    second_kick = _kick(offload_program, platform_root)
    assert second_kick["result"]["kicked"] == [embed_job]
    assert second_kick["result"]["swept"] == [ocr_job], "the finished OCR job's done/ is swept"
    assert run_one(store, worker_id="w4")["status"] == "complete"  # embed
    assert run_one(store, worker_id="w5")["status"] == "complete"  # index

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "indexed"
    emb = store.knowledge.execute("SELECT * FROM emb WHERE model_key=?", (STUB_MODEL_KEY,)).fetchall()
    assert len(emb) == len(chunk_ids) > 0
    assert {r["dims"] for r in emb} == {STUB_DIMS}

    # 6. kick sweeps the last published directory once its row is complete
    swept = _kick(offload_program, platform_root)
    assert swept["result"]["swept"] == [embed_job]
    assert protocol.list_done(root) == []


def test_the_worker_reports_an_empty_queue_without_touching_anything(offload_program, tmp_path):
    summary = _run_dev_worker(offload_program, tmp_path)
    assert summary == {
        "claimed": [],
        "published": [],
        "failed": [],
        "lost": [],
        "polls": 1,
        "message": "Queue empty - safe to switch DEV off",
    }


def test_the_worker_never_recomputes_after_a_failed_publish(store, offload_program, raw_dir, tmp_path):
    """A dropped connection between "the GPU finished" and "the sandbox has
    it" must cost zero GPU minutes: the local result is kept and the next
    poll retries the hand-over."""
    _add_scan(store, offload_program, raw_dir)
    run_one(store, worker_id="w0")
    root = protocol.offload_root(offload_program)

    class FlakyTransport(LocalTransport):
        publish_calls = 0

        def publish(self, job_id):
            FlakyTransport.publish_calls += 1
            if FlakyTransport.publish_calls == 1:
                from trialerror.offload.transport import TransportError

                raise TransportError("simulated connection drop")
            return super().publish(job_id)

    backends = StubDevBackends()
    transport = FlakyTransport(root, worker_id="dev")
    summary = run_worker(
        transport=transport, backends=backends, work_root=tmp_path / "devwork", max_polls=1, stay=True
    )
    assert summary["claimed"] and not summary["published"]
    assert protocol.list_done(root) == []
    assert backends.ocr().calls == 1

    summary = run_worker(
        transport=transport, backends=backends, work_root=tmp_path / "devwork", max_polls=1, stay=True
    )
    assert summary["published"]
    assert backends.ocr().calls == 1, "the model must not have run a second time"
    assert protocol.list_done(root)


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
def test_fake_backend_refused_on_the_dev_side(tmp_path):
    """``fake_backend_refused`` (C-unit). A DEV worker whose program root
    still says ``backend = "fake"`` refuses to start at all -- before it
    can claim anything, let alone publish hash-derived vectors into the
    record."""
    backends = ConfigDevBackends({"ingest": {"ocr": {"backend": "fake"}, "embed": {"backend": "fake"}}})
    with pytest.raises(WorkerConfigError, match="fake"):
        backends.validate()

    offloading = ConfigDevBackends(
        {"ingest": {"ocr": {"backend": "offload"}, "embed": {"backend": "offload", "model_key": "x"}}}
    )
    with pytest.raises(WorkerConfigError, match="offload"):
        offloading.validate()


def test_require_real_backends_refuses_a_fake_or_absent_table(store, program_root, raw_dir):
    """D13's config gate, at the handler's single config-load point."""
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "p"\n\n[ingest]\nrequire_real_backends = true\n\n'
        '[ingest.ocr]\nbackend = "fake"\n\n[ingest.embed]\nbackend = "offload"\nmodel_key = "m"\n',
        encoding="utf-8",
    )
    doc_id = _add_scan(store, program_root, raw_dir)
    result = run_one(store, worker_id="w0")
    assert result["status"] == "failed"
    job = ledger.get_job(store, f"JOB-ingest-{doc_id}")
    assert "require_real_backends" in job["last_error"]
    assert job["attempts"] == 1, "a config refusal is a logic failure, not an environmental one"


def test_an_unparseable_toml_stops_the_handler_instead_of_falling_back_to_fake(store, program_root, raw_dir):
    """The fail-closed half of D13: before this, one stray character
    silently reverted a GPU-configured program to the fake backends."""
    doc_id = _add_scan(store, program_root, raw_dir)
    (program_root / "trialerror.toml").write_text("[program\nbroken", encoding="utf-8")
    result = run_one(store, worker_id="w0")
    assert result["status"] == "failed"
    assert "invalid TOML" in ledger.get_job(store, f"JOB-ingest-{doc_id}")["last_error"]


def test_offload_marker_refuses_to_be_run_directly(store, offload_program):
    """Nothing may compute through the marker: if a caller ever resolves it
    and calls it, that is a loud logic failure, not a silent fake."""
    from trialerror.ingest.backends import load_embed_backend, load_ocr_backend
    from trialerror.offload.marker import OffloadNotRunnable

    ocr = load_ocr_backend({"backend": "offload"})
    with pytest.raises(OffloadNotRunnable):
        ocr.run(input_path=offload_program / "x", work_dir=offload_program / "w")

    embed = load_embed_backend({"backend": "offload", "model_key": "m", "dims": 4})
    assert embed.model_key == "m" and embed.dims == 4
    with pytest.raises(OffloadNotRunnable):
        embed.embed_batch(["a"])

    with pytest.raises(ValueError, match="model_key"):
        load_embed_backend({"backend": "offload"})


def test_sha_mismatch_moves_the_result_to_failed_and_burns_a_ledger_attempt(
    store, offload_program, raw_dir, tmp_path, platform_root
):
    """``sha_mismatch_to_failed``, ledger half. A tampered payload is a
    LOGIC failure (visible, attempt consumed) and the result is quarantined
    in ``failed/`` rather than folded into the record."""
    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    run_one(store, worker_id="w0")
    _run_dev_worker(offload_program, tmp_path)

    (protocol.done_dir(root) / job_id / "pages.json").write_text("tampered", encoding="utf-8")
    _kick(offload_program, platform_root)
    assert run_one(store, worker_id="w1")["status"] == "failed"

    job = ledger.get_job(store, job_id)
    assert job["attempts"] == 1 and job["failure_class"] == "logic"
    assert "sha256 mismatch" in job["last_error"]
    assert protocol.list_failed(root) == [job_id]
    assert protocol.list_done(root) == []
    assert store.knowledge.execute("SELECT COUNT(*) c FROM element WHERE doc_id=?", (doc_id,)).fetchone()["c"] == 0


def test_c_fail_a_deterministic_gpu_failure_ends_abandoned(
    store, offload_program, raw_dir, tmp_path, platform_root
):
    """Design acceptance C-fail: a corrupt document reaches ``failed/``
    after 3 offload attempts and the ledger row ends ``abandoned``.

    The two counters are deliberately separate. ``offload_attempts``
    measures GPU failures and stops DEV from grinding on a document that
    will never work; the ledger's own ``attempts`` then runs out over the
    following claims, which is what actually settles the row."""
    doc_id = _add_scan(store, offload_program, raw_dir, text=["CORRUPT PAGE"])
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    backends = StubDevBackends(ocr=StubOcrBackend(fail_on="CORRUPT"))

    run_one(store, worker_id="w0")
    for attempt in (1, 2):
        summary = _run_dev_worker(offload_program, tmp_path / f"a{attempt}", backends=backends)
        assert summary["failed"] == [job_id]
        _kick(offload_program, platform_root)
        assert run_one(store, worker_id=f"wr{attempt}")["status"] == "deferred"
        manifest = protocol.read_json(protocol.pending_dir(root) / f"{job_id}.json")
        assert manifest["offload_attempts"] == attempt
        assert ledger.get_job(store, job_id)["attempts"] == 0

    # third DEV failure exhausts the offload budget -> terminal marker
    _run_dev_worker(offload_program, tmp_path / "a3", backends=backends)
    _kick(offload_program, platform_root)
    assert run_one(store, worker_id="wr3")["status"] == "failed"
    assert protocol.list_failed(root) == [job_id]
    assert ledger.get_job(store, job_id)["attempts"] == 1

    # the terminal marker keeps failing the stage until the row abandons
    ledger.kick(store, job_id)
    assert run_one(store, worker_id="wr4")["status"] == "failed"
    ledger.kick(store, job_id)
    assert run_one(store, worker_id="wr5")["status"] == "abandoned"

    job = ledger.get_job(store, job_id)
    assert job["state"] == "abandoned" and job["attempts"] == 3
    assert "corrupt" in job["last_error"]
    err = protocol.read_json(protocol.failed_dir(root) / job_id / protocol.ERROR_FILENAME)
    assert err["offload_attempts"] == 3


def test_reclaim_returns_a_suspended_workers_claim_and_the_next_run_finishes_it(
    store, offload_program, raw_dir, tmp_path, platform_root
):
    """The lid-close path: no hook runs on DEV, so the sandbox's 60-minute
    reclaim is the only way the job comes back."""
    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    run_one(store, worker_id="w0")

    protocol.server_claim(root, job_id, worker_id="dev")
    hb = protocol.claimed_dir(root) / "dev" / f"{job_id}{protocol.HEARTBEAT_SUFFIX}"
    hb.write_text(
        (parse(now()) - timedelta(seconds=protocol.DEFAULT_CLAIM_EXPIRY_S * 2)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        ),
        encoding="utf-8",
    )

    from trialerror.cli import build_parser

    args = build_parser().parse_args(
        ["offload", "reclaim", "--program-root", str(offload_program), "--platform-root", str(platform_root)]
    )
    env = args.handler(args)
    assert env["ok"] and env["result"]["count"] == 1
    assert protocol.list_pending(root) == [job_id]

    _run_dev_worker(offload_program, tmp_path)
    _kick(offload_program, platform_root)
    assert run_one(store, worker_id="w1")["status"] == "complete"


# ---------------------------------------------------------------------------
# SEC-2: the config-hash check the sandbox actually owns
# ---------------------------------------------------------------------------
def test_a_result_for_a_changed_config_is_requeued_not_folded_in(
    store, offload_program, raw_dir, tmp_path
):
    """SEC-2. The marker names the configuration the work was queued for.
    If the stage's ``[ingest.ocr]`` table has changed since, the published
    result answers a question the program is no longer asking -- and the
    old check could not see that, because it compared the manifest against
    a ``config_hash`` the worker had copied out of that same manifest.

    The design says the SANDBOX owns the truth, and the truth is the config
    the stage has NOW, so the stale result is discarded and the work
    re-queued under the current hash. No offload attempt is burned: the
    previous run did not fail, it was superseded."""
    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)

    run_one(store, worker_id="w0")                      # queue + park
    _run_dev_worker(offload_program, tmp_path)          # DEV publishes
    assert protocol.list_done(root) == [job_id]
    before = protocol.read_json(protocol.done_dir(root) / job_id / protocol.MANIFEST_FILENAME)

    # the operator pins a different OCR backend between the queue and the
    # result landing
    write_offload_toml(offload_program, expect_backend="someothermarker")

    ledger.kick(store, job_id)
    assert run_one(store, worker_id="w1")["status"] == "deferred"

    # discarded, re-queued, and now carrying the CURRENT config hash
    assert protocol.list_done(root) == []
    assert protocol.list_pending(root) == [job_id]
    requeued = protocol.read_json(protocol.pending_dir(root) / f"{job_id}.json")
    assert requeued["config_hash"] != before["config_hash"]
    assert requeued["expect"]["backend"] == "someothermarker"
    assert requeued["offload_attempts"] == 0, "a superseded result is not a GPU failure"
    job = ledger.get_job(store, job_id)
    assert job["attempts"] == 0, "nor a stage failure"
    # the sandbox's own record moved with it
    assert protocol.queued_manifest(root, job_id)["config_hash"] == requeued["config_hash"]


def test_an_unchanged_config_still_completes_the_stage(store, offload_program, raw_dir, tmp_path, platform_root):
    """The other half of SEC-2: the check must not make the ordinary path
    any harder. Same round trip, config untouched, stage completes."""
    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    run_one(store, worker_id="w0")
    _run_dev_worker(offload_program, tmp_path)
    _kick(offload_program, platform_root)
    assert run_one(store, worker_id="w1")["status"] == "complete"
    assert ledger.get_job(store, job_id)["state"] == "complete"


# ---------------------------------------------------------------------------
# SEC-8: what may leave the machine
# ---------------------------------------------------------------------------
def test_a_raw_path_outside_the_program_root_is_never_queued(
    store, offload_program, raw_dir, tmp_path
):
    """SEC-8. ``document.raw_path`` is data -- whatever registered the
    document put there, honoured verbatim when absolute. Locally that only
    ever names a file this process could already read; offloading turns it
    into "copy these bytes into the queue and hand them to another machine
    over SSH". The queue carries the program's own files and nothing else."""
    from trialerror.stores.writer import update

    outsider = tmp_path / "not-my-program" / "secrets.pdf"
    outsider.parent.mkdir(parents=True, exist_ok=True)
    outsider.write_bytes(b"%PDF-1.4 not yours\n")

    doc_id = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"raw_path": str(outsider)})

    result = run_one(store, worker_id="w0")
    assert result["status"] == "failed", result
    root = protocol.offload_root(offload_program)
    assert protocol.list_pending(root) == [], "nothing left the program root"
    job = ledger.get_job(store, job_id)
    assert job["attempts"] == 1, "a refusal is a logic failure, not an absence"
    assert "program root" in (job["last_error"] or "")
