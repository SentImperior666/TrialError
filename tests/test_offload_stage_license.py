"""vast.ai round 2, lane L3: the queue side of the OCR executor.

* **The licence stamp (design 2.2).** ``offload_ocr_result`` stamps
  ``expect.license_tier`` and ``expect.source_id`` into the OCR marker, so a
  worker that may send documents off-site can decide on the manifest alone.
  ``expect`` is not part of ``config_hash``: a stamped marker and a marker
  queued before the stamp both fold, and nothing is re-queued.
* **O9.** When the queue side folds a published OCR result whose
  ``result.json`` carries a ``vastai`` block, it appends ONE event through the
  store's own event table (no migration). ``[ingest.ocr]
  record_offsite_ocr_events = false`` turns that off, and the key is kept out
  of ``config_hash`` so setting it re-queues nothing.

Everything runs in process through ``run_one`` and the local queue, as
``tests/test_offload_stage.py`` does. No network, no GPU.
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest import pipeline
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.offload import protocol, stage
from trialerror.offload.marker import config_hash
from tests._ingest_fixtures import bootstrap_launch, write_scanned_pdf_fixture
from tests._offload_fixtures import STUB_OCR_NAME, publish_stub_result, write_offload_toml

#: The sandbox's ``[ingest.ocr]`` table as ``write_offload_toml`` writes it.
_OCR_TABLE = {"backend": "offload", "expect_backend": STUB_OCR_NAME}

_VASTAI_BLOCK = {
    "lease_id": "VOCR-test-1",
    "instance_id": 5001,
    "host_id": 77,
    "datacenter": True,
    "verified": True,
    "geolocation": "Norway, NO",
    "gpu_name": "RTX 4090",
    "estimated_cost_usd": 0.42,
    "remote_versions": {"marker": "1.10.2", "torch": "2.13.0+cu130"},
}


@pytest.fixture()
def raw_dir(program_root):
    d = program_root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def offload_program(store, program_root):
    write_offload_toml(program_root)
    return program_root


def _add_scan(store, program_root, raw_dir, *, license_tier: str = "open"):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store,
        kind="paper",
        title="Licence stamp fixture",
        license_tier=license_tier,
        acquisition_route="web",
        registered_by_launch=launch_id,
    )
    path = write_scanned_pdf_fixture(raw_dir / "scan.pdf", None)
    result = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=source["source_id"],
        raw_path=path,
        created_by_launch=launch_id,
        media_type="pdf-scan",
    )
    return result["document"]["doc_id"], source["source_id"], path


def _pending_manifest(root, job_id):
    return protocol.read_json(protocol.pending_dir(root) / f"{job_id}.json")


def _offsite_events(store) -> list[dict]:
    rows = store.ops.execute(
        "SELECT payload FROM event WHERE type = ? ORDER BY rowid", (stage.OFFSITE_OCR_EVENT_TYPE,)
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _set_record_switch(program_root, value: bool) -> None:
    path = program_root / "trialerror.toml"
    text = path.read_text(encoding="utf-8")
    anchor = f'expect_backend = "{STUB_OCR_NAME}"'
    assert anchor in text
    path.write_text(
        text.replace(anchor, f"{anchor}\nrecord_offsite_ocr_events = {'true' if value else 'false'}"),
        encoding="utf-8",
    )


def _publish_and_fold(store, root, job_id, **publish_kwargs):
    publish_stub_result(root, job_id, **publish_kwargs)
    ledger.kick(store, job_id)
    return run_one(store, worker_id="w-fold")


# ---------------------------------------------------------------------------
# the stamp
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["open", "commercial_restricted", "unknown"])
def test_the_ocr_marker_carries_the_sources_licence_tier_and_id(store, offload_program, raw_dir, tier):
    doc_id, source_id, _ = _add_scan(store, offload_program, raw_dir, license_tier=tier)
    job_id = f"JOB-ingest-{doc_id}"
    assert run_one(store, worker_id="w0")["status"] == "deferred"

    manifest = _pending_manifest(protocol.offload_root(offload_program), job_id)
    assert manifest["expect"]["license_tier"] == tier
    assert manifest["expect"]["source_id"] == source_id
    # the rest of the block is what it always was
    assert manifest["expect"]["backend"] == STUB_OCR_NAME
    assert manifest["expect"]["stage"] == "ocr"


def test_the_stamp_is_not_part_of_the_config_hash(store, offload_program, raw_dir):
    """``config_hash`` covers the ``[ingest.ocr]`` table only: the marker's
    hash is the hash of the table, whatever ``expect`` carries."""
    doc_id, _, _ = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    run_one(store, worker_id="w0")
    manifest = _pending_manifest(protocol.offload_root(offload_program), job_id)
    assert manifest["config_hash"] == config_hash(_OCR_TABLE)
    assert "license_tier" not in _OCR_TABLE and "source_id" not in _OCR_TABLE


def test_a_stamped_marker_folds_and_nothing_is_requeued(store, offload_program, raw_dir):
    doc_id, _, _ = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    run_one(store, worker_id="w0")

    assert _publish_and_fold(store, root, job_id)["status"] == "complete"
    assert protocol.list_pending(root) == []
    assert protocol.find_manifest(root, job_id)[0] == "done"


def test_a_marker_queued_before_the_stamp_still_folds(store, offload_program, raw_dir):
    """A marker written by a harness without the stamp: same ``config_hash``
    (the table did not change), no tier. It is neither re-queued nor
    re-stamped, and its published result folds."""
    doc_id, _, path = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    old_expect = {
        "stage": "ocr",
        "backend": STUB_OCR_NAME,
        "version": None,
        "outputs": [stage.OCR_OUTPUT_NAME],
        "input_name": stage.ocr_input_name(path),
        "page_count": None,
    }
    protocol.queue_marker(
        root,
        job_id=job_id,
        stage="ocr",
        doc_id=doc_id,
        expect=old_expect,
        config_hash=config_hash(_OCR_TABLE),
        inputs=[(stage.ocr_input_name(path), path.read_bytes())],
    )
    assert run_one(store, worker_id="w0")["status"] == "deferred"
    manifest = _pending_manifest(root, job_id)
    assert "license_tier" not in manifest["expect"], "an already-queued marker is not rewritten"

    assert _publish_and_fold(store, root, job_id)["status"] == "complete"
    assert protocol.list_pending(root) == []


# ---------------------------------------------------------------------------
# O9: one event per folded off-site result
# ---------------------------------------------------------------------------
def test_an_offsite_result_appends_one_event_naming_the_document(store, offload_program, raw_dir):
    doc_id, source_id, _ = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    run_one(store, worker_id="w0")
    queued = _pending_manifest(root, job_id)

    folded = _publish_and_fold(store, root, job_id, result_overrides={"vastai": dict(_VASTAI_BLOCK)})
    assert folded["status"] == "complete"

    events = _offsite_events(store)
    assert len(events) == 1
    event = events[0]
    assert event["job_id"] == job_id
    assert event["doc_id"] == doc_id
    assert event["source_id"] == source_id
    assert event["license_tier"] == "open"
    assert event["input_sha256"] == queued["inputs"][0]["sha256"]
    assert event["input_bytes"] == queued["inputs"][0]["bytes"]
    assert event["vastai"] == _VASTAI_BLOCK
    assert len(event["vastai_sha256"]) == 64


@pytest.mark.parametrize("overrides", [{}, {"vastai": None}, {"vastai": {}}, {"vastai": "yes"}])
def test_a_result_without_a_vastai_block_appends_no_event(store, offload_program, raw_dir, overrides):
    doc_id, _, _ = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    run_one(store, worker_id="w0")

    assert _publish_and_fold(store, root, job_id, result_overrides=overrides)["status"] == "complete"
    assert _offsite_events(store) == []


def test_a_retried_fold_of_the_same_result_appends_nothing(store):
    manifest = {
        "doc_id": "DOC-x",
        "expect": {"license_tier": "open", "source_id": "SRC-x"},
        "inputs": [{"name": "input.pdf", "sha256": "a" * 64, "bytes": 10}],
    }
    result = {"worker_id": "dev", "finished_ts": "2026-09-19T00:00:00Z", "vastai": dict(_VASTAI_BLOCK)}
    first = stage._record_offsite_ocr(store, job_id="JOB-x", manifest=manifest, result=result)
    again = stage._record_offsite_ocr(store, job_id="JOB-x", manifest=manifest, result=result)
    assert first is not None and again is None
    assert len(_offsite_events(store)) == 1

    # the same job run off-site again (a new lease) is a new departure
    second_lease = {**result, "vastai": {**_VASTAI_BLOCK, "lease_id": "VOCR-test-2"}}
    assert stage._record_offsite_ocr(store, job_id="JOB-x", manifest=manifest, result=second_lease) is not None
    assert len(_offsite_events(store)) == 2


def test_an_oversized_block_is_recorded_by_its_keys_and_digest(store):
    big = {"lease_id": "VOCR-big", "log": "x" * 40_000}
    row = stage._record_offsite_ocr(
        store, job_id="JOB-big", manifest={"doc_id": "DOC-big"}, result={"vastai": big}
    )
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert payload["vastai"] == {"truncated": True, "keys": ["lease_id", "log"]}
    assert len(payload["vastai_sha256"]) == 64


def test_the_switch_turns_the_event_off_and_is_not_hashed(store, offload_program, raw_dir):
    """``record_offsite_ocr_events = false``, set AFTER the marker was queued:
    the result still folds (the key is not in ``config_hash``, so nothing is
    discarded or re-queued), and no event is appended."""
    doc_id, _, _ = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    run_one(store, worker_id="w0")
    _set_record_switch(offload_program, False)

    folded = _publish_and_fold(store, root, job_id, result_overrides={"vastai": dict(_VASTAI_BLOCK)})
    assert folded["status"] == "complete"
    assert protocol.list_pending(root) == []
    assert _offsite_events(store) == []


def test_the_switch_set_to_true_is_the_default(store, offload_program, raw_dir):
    doc_id, _, _ = _add_scan(store, offload_program, raw_dir)
    job_id = f"JOB-ingest-{doc_id}"
    root = protocol.offload_root(offload_program)
    _set_record_switch(offload_program, True)
    run_one(store, worker_id="w0")
    assert _pending_manifest(root, job_id)["config_hash"] == config_hash(_OCR_TABLE)

    folded = _publish_and_fold(store, root, job_id, result_overrides={"vastai": dict(_VASTAI_BLOCK)})
    assert folded["status"] == "complete"
    assert len(_offsite_events(store)) == 1
