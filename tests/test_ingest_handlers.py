"""Tests for ``trialerror.ingest.handlers``: the normalize/ocr/chunk/embed/index
job bodies, driven end to end via ``trialerror.jobs.worker.run_one`` (the same
claim-run-settle loop a real detached worker uses) over the fake OCR/embed
backends -- no GPU required (design Section 13 flag F18/M15)."""

from __future__ import annotations

import json
import os

import pytest

from trialerror.ingest import pipeline
from trialerror.jobs.worker import run_one
from trialerror.stores.vecindex import vec_table_name
from tests._ingest_fixtures import (
    bootstrap_launch,
    write_epub_fixture,
    write_html_fixture,
    write_markdown_fixture,
    write_pdf_text_fixture,
    write_scanned_pdf_fixture,
)


def _drain(store, max_steps=10):
    results = []
    for i in range(max_steps):
        r = run_one(store, worker_id=f"w{i}")
        results.append(r)
        if r["status"] == "idle":
            break
    return results


def _register_and_add(store, program_root, path, *, media_type=None):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=path,
        created_by_launch=launch_id, media_type=media_type,
    )
    return launch_id, source, result


@pytest.fixture()
def raw_dir(program_root):
    d = program_root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


FORMAT_FIXTURES = {
    "pdf-text": lambda d: write_pdf_text_fixture(d / "doc.pdf", ["Fixture page one.", "Fixture page two."]),
    "pdf-scan": lambda d: write_scanned_pdf_fixture(d / "scan.pdf"),
    "html": lambda d: write_html_fixture(d / "doc.html"),
    "epub": lambda d: write_epub_fixture(d / "book.epub"),
}


@pytest.mark.parametrize("media_type", sorted(FORMAT_FIXTURES))
def test_full_pipeline_reaches_indexed_for_every_format(store, program_root, raw_dir, media_type):
    """M7 acceptance criterion (design Section 12): "ingest 4 fixture docs
    (pdf-text, scanned pdf via marker, html, epub) end-to-end" -- the
    format-coverage half (the restartable/byte-identical half lives in
    ``tests/test_m7_acceptance.py``, subprocess-heavy like M2's own
    kill-mid-job test)."""
    fixture_fn = FORMAT_FIXTURES[media_type]
    path = fixture_fn(raw_dir)
    _launch_id, _source, result = _register_and_add(store, program_root, path, media_type=media_type)
    doc_id = result["document"]["doc_id"]

    results = _drain(store)
    assert all(r["status"] in ("complete", "idle") for r in results), results

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "indexed"
    assert doc["sha256"] != "pending"

    elements = store.knowledge.execute("SELECT COUNT(*) FROM element WHERE doc_id=?", (doc_id,)).fetchone()[0]
    chunks = store.knowledge.execute("SELECT * FROM chunk WHERE doc_id=?", (doc_id,)).fetchall()
    anchors = store.knowledge.execute("SELECT COUNT(*) FROM quote_anchor WHERE doc_id=?", (doc_id,)).fetchone()[0]
    assert elements > 0
    assert len(chunks) > 0
    assert anchors == len(chunks)  # one anchor per chunk

    for c in chunks:
        emb = store.knowledge.execute(
            "SELECT COUNT(*) FROM emb WHERE chunk_sha256=?", (c["sha256"],)
        ).fetchone()[0]
        assert emb == 1
        fts = store.knowledge.execute(
            "SELECT COUNT(*) FROM chunk_fts WHERE chunk_id=?", (c["chunk_id"],)
        ).fetchone()[0]
        assert fts == 1

    # archived normalized text actually landed on disk (design: rel_path)
    archive_path = program_root / doc["rel_path"]
    assert archive_path.is_file()
    assert archive_path.read_text(encoding="utf-8").strip() != ""


def test_normalize_pdf_text_produces_narrativetext_per_page(store, program_root, raw_dir):
    path = write_pdf_text_fixture(raw_dir / "doc.pdf", ["Alpha page.", "Beta page."])
    _launch_id, _source, result = _register_and_add(store, program_root, path, media_type="pdf-text")
    doc_id = result["document"]["doc_id"]

    r = run_one(store, worker_id="w0")
    assert r["status"] == "complete"

    elements = [dict(e) for e in store.knowledge.execute("SELECT * FROM element WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()]
    assert len(elements) == 2
    assert "Alpha page." in elements[0]["text"]
    assert "Beta page." in elements[1]["text"]

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "normalized"
    assert doc["normalizer_id"] == "trialerror-normalize"


def test_ocr_route_produces_elements_from_fake_backend(store, program_root, raw_dir):
    path = write_scanned_pdf_fixture(raw_dir / "scan.pdf", ["Recognized page A.", "Recognized page B."])
    _launch_id, _source, result = _register_and_add(store, program_root, path, media_type="pdf-scan")
    doc_id = result["document"]["doc_id"]
    assert result["job"]["kind"] == "ocr"

    r = run_one(store, worker_id="w0")
    assert r["status"] == "complete"

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == "normalized"
    assert doc["ocr_backend"] == "fake"

    elements = [dict(e) for e in store.knowledge.execute("SELECT * FROM element WHERE doc_id=?", (doc_id,)).fetchall()]
    assert len(elements) == 2
    assert any("Recognized page A." in e["text"] for e in elements)


def test_chunk_handler_is_idempotent_on_rerun(store, program_root, raw_dir):
    path = write_html_fixture(raw_dir / "doc.html")
    _launch_id, _source, result = _register_and_add(store, program_root, path)
    doc_id = result["document"]["doc_id"]

    run_one(store, worker_id="w0")  # normalize
    run_one(store, worker_id="w1")  # chunk

    chunk_count_1 = store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0]

    # re-run the SAME chunk stage manually (simulating a resumed retry) --
    # must not duplicate any chunk row.
    from trialerror.jobs import ledger

    job_id = f"JOB-ingest-{doc_id}-chunk"
    job = ledger.get_job(store, job_id)
    assert job is not None
    # the job already completed; force it back to pending to re-run its body
    store.jobs.execute("UPDATE job SET state='pending', claimed_by=NULL WHERE job_id=?", (job_id,))
    store.jobs.commit()
    r = run_one(store, worker_id="w2", job_id=job_id, kind="custom", payload=json.loads(job["payload"]))
    assert r["status"] == "complete"

    chunk_count_2 = store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0]
    assert chunk_count_2 == chunk_count_1


def test_embed_handler_skips_already_cached_chunk_sha(store, program_root, raw_dir):
    path = write_html_fixture(raw_dir / "doc.html")
    _launch_id, _source, result = _register_and_add(store, program_root, path)
    doc_id = result["document"]["doc_id"]

    run_one(store, worker_id="w0")  # normalize
    run_one(store, worker_id="w1")  # chunk
    run_one(store, worker_id="w2")  # embed

    emb_count_1 = store.knowledge.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
    assert emb_count_1 > 0

    # re-run embed for the same doc -- every chunk_sha256 is already cached,
    # so no new emb rows should appear (byte-identical resume behavior).
    from trialerror.jobs import ledger

    job_id = f"JOB-ingest-{doc_id}-embed"
    job = ledger.get_job(store, job_id)
    store.jobs.execute("UPDATE job SET state='pending', claimed_by=NULL WHERE job_id=?", (job_id,))
    store.jobs.commit()
    r = run_one(store, worker_id="w3", job_id=job_id, kind=job["kind"], payload=json.loads(job["payload"]))
    assert r["status"] == "complete"

    emb_count_2 = store.knowledge.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
    assert emb_count_2 == emb_count_1


def test_index_handler_populates_active_model_vec_table(store, program_root, raw_dir):
    path = write_html_fixture(raw_dir / "doc.html")
    _launch_id, _source, result = _register_and_add(store, program_root, path)
    doc_id = result["document"]["doc_id"]

    _drain(store)

    from trialerror.ingest.backends import FakeEmbedBackend

    model_key = FakeEmbedBackend().model_key
    table = vec_table_name(model_key)
    rows = store.knowledge.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    chunks = store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0]
    assert rows == chunks


def test_extract_handler_is_registered_stub_not_auto_chained(store, program_root, raw_dir):
    """design Section 11: "v0 ships the schema + a minimal claim
    extractor" -- the handler exists and settles cleanly, but ``index``
    does not enqueue it automatically."""
    path = write_html_fixture(raw_dir / "doc.html")
    _launch_id, _source, result = _register_and_add(store, program_root, path)
    doc_id = result["document"]["doc_id"]
    _drain(store)

    extract_job = store.jobs.execute("SELECT * FROM job WHERE kind='extract'").fetchone()
    assert extract_job is None  # never auto-enqueued

    r = run_one(store, worker_id="wx", job_id="JOB-manual-extract", kind="extract", payload={"doc_id": doc_id})
    assert r["status"] == "complete"


# ---------------------------------------------------------------------------
# The flagship acceptance test: a real detached OS process running the
# ``embed`` stage, killed abruptly mid-batch, reclaimed by a tick, and
# resumed to a byte-identical final state. Mirrors
# tests/test_jobs_worker.py::test_kill_mid_job_worker_is_reclaimed_by_tick_and_resumes_from_checkpoint
# (M2's own flagship subprocess test) -- same shape, M7's stage.
# ---------------------------------------------------------------------------


def _multi_section_html(path, n_sections=8):
    """Many small Title-opened sections -> many small, separately-packed
    chunks (design Section 6 chunker: a Title always opens a new section,
    never recombined across the boundary) -- lets a small ``batch_size``
    produce several distinct embed batches to kill between."""
    body = "".join(f"<h1>Section {i}</h1><p>Body text for section number {i}.</p>" for i in range(n_sections))
    path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    return path


def test_kill_mid_embed_worker_is_reclaimed_by_tick_and_resumes_byte_identical(store, program_root, platform_root):
    """M7 acceptance criterion (design Section 12, verbatim): "ingest 4
    fixture docs ... end-to-end restartable (kill mid-embed -> resume,
    byte-identical final state)". Uses ``FakeEmbedBackend``'s ``delay_s``
    test-only seam (mirrors ``trialerror.util.atomic``'s own kill-mid-write
    seam) to make partial per-batch progress observable deterministically
    -- no GPU needed."""
    import time

    from trialerror.jobs import ledger
    from trialerror.jobs.worker import make_worker_id, spawn_worker

    dims = 8
    delay_s = 0.35
    (program_root / "trialerror.toml").write_text(
        "[program]\nid = \"test-program\"\n\n"
        "[ingest.embed]\nbackend = \"fake\"\nbatch_size = 1\n"
        f"dims = {dims}\ndelay_s = {delay_s}\n",
        encoding="utf-8",
    )

    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = _multi_section_html(raw_dir / "doc.html", n_sections=8)

    launch_id, _source, result = _register_and_add(store, program_root, path)
    doc_id = result["document"]["doc_id"]

    # drive normalize + chunk in-process (fast, deterministic) up to the
    # point where the ``embed`` job is pending -- only the embed stage
    # itself needs the real-subprocess-kill treatment.
    r1 = run_one(store, worker_id="w-normalize")
    assert r1["status"] == "complete"
    r2 = run_one(store, worker_id="w-chunk")
    assert r2["status"] == "complete"

    total_chunks = store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0]
    assert total_chunks >= 4, "fixture must produce enough chunks to observe partial embed progress"

    embed_job_id = f"JOB-ingest-{doc_id}-embed"
    assert ledger.get_job(store, embed_job_id) is not None

    env = os.environ.copy()  # inherits TRIALERROR_PLATFORM_ROOT set by the platform_root fixture
    handle = spawn_worker(
        program_root=program_root,
        platform_root=platform_root,
        job_id=embed_job_id,
        mode="once",
        lease_s=2,
        env=env,
    )

    deadline = time.time() + 15.0
    completed_before_kill = 0
    while time.time() < deadline:
        row = ledger.get_job(store, embed_job_id)
        if row["checkpoint"]:
            completed_before_kill = json.loads(row["checkpoint"]).get("embedded", 0)
        if completed_before_kill > 0:
            break
        time.sleep(0.05)
    assert 0 < completed_before_kill < total_chunks, (
        f"expected PARTIAL embed progress before killing (got {completed_before_kill} of {total_chunks})"
    )

    handle.process.kill()
    handle.process.wait(timeout=5)

    crashed = ledger.get_job(store, embed_job_id)
    assert crashed["state"] in ("claimed", "running")
    assert json.loads(crashed["checkpoint"])["embedded"] >= completed_before_kill

    time.sleep(2.2)  # past the 2s lease
    reclaimed = ledger.sweep_expired_leases(store)
    assert embed_job_id in [r["job_id"] for r in reclaimed]
    after_reclaim = ledger.get_job(store, embed_job_id)
    assert after_reclaim["state"] == "pending"

    result2 = run_one(store, job_id=embed_job_id, worker_id=make_worker_id(), lease_s=ledger.LEASE_DURATION_S)
    assert result2["status"] == "complete"

    emb_rows = [
        dict(r) for r in store.knowledge.execute(
            "SELECT e.* FROM emb e JOIN chunk c ON c.sha256 = e.chunk_sha256 WHERE c.doc_id = ?", (doc_id,)
        ).fetchall()
    ]
    assert len(emb_rows) == total_chunks  # every chunk embedded exactly once, none lost, none duplicated

    # byte-identical final state: an UNINTERRUPTED fake-embed of the same
    # texts must produce the exact same serialized vector bytes the
    # kill-then-resume run actually persisted.
    from trialerror.ingest.backends import FakeEmbedBackend
    from trialerror.stores.vecindex import serialize_vector_fallback

    fresh_backend = FakeEmbedBackend(dims=dims)
    chunks = [
        dict(r) for r in store.knowledge.execute("SELECT sha256, text FROM chunk WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
    ]
    for c in chunks:
        expected_vector = fresh_backend.embed_batch([c["text"]])[0]
        expected_bytes = serialize_vector_fallback(expected_vector)
        actual = store.knowledge.execute(
            "SELECT vector FROM emb WHERE chunk_sha256 = ? AND model_key = ?", (c["sha256"], fresh_backend.model_key)
        ).fetchone()
        assert actual is not None
        assert actual["vector"] == expected_bytes
