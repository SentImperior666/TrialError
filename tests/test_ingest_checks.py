"""Tests for ``trialerror.ingest.checks``: chunker_missing/outdated,
embedding_missing/stale, anchor_spot_resolve -- design Section 4.1's
``trialerror ingest doctor`` counts."""

from __future__ import annotations

from trialerror.ingest import pipeline
from trialerror.ingest.checks import (
    check_anchor_spot_resolve,
    check_chunker_missing,
    check_chunker_outdated,
    check_embedding_missing,
    check_embedding_stale,
)
from trialerror.jobs.worker import run_one
from trialerror.stores.vecindex import ensure_vec_table
from trialerror.util.doctor import DoctorContext
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture


def _drain(store, max_steps=10):
    for i in range(max_steps):
        r = run_one(store, worker_id=f"w{i}")
        if r["status"] == "idle":
            break


def _ingest_one_doc(store, program_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = write_html_fixture(raw_dir / "doc.html")
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=path,
        created_by_launch=launch_id,
    )
    doc_id = result["document"]["doc_id"]
    _drain(store)
    return doc_id, launch_id


def test_chunker_missing_passes_on_clean_store(store, program_root):
    ctx = DoctorContext(program_root=program_root)
    result = check_chunker_missing(ctx)
    assert result.status in ("pass", "skip")


def test_chunker_missing_flags_document_with_elements_but_no_chunks(store, program_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    # delete this doc's chunks (and their anchors, FK-first) to simulate the gap
    store.knowledge.execute("DELETE FROM quote_anchor WHERE doc_id=?", (doc_id,))
    store.knowledge.execute("DELETE FROM chunk WHERE doc_id=?", (doc_id,))
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    result = check_chunker_missing(ctx)
    assert result.status == "warn"
    assert doc_id in result.details["doc_ids"]


def test_chunker_outdated_flags_chunk_with_old_chunker_version(store, program_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    store.knowledge.execute("UPDATE chunk SET chunker_version = '0-ancient' WHERE doc_id=?", (doc_id,))
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    result = check_chunker_outdated(ctx)
    assert result.status == "warn"
    assert result.details["count"] >= 1


def test_embedding_missing_flags_chunk_with_no_emb_row(store, program_root):
    doc_id, _launch = _ingest_one_doc(store, program_root)
    store.knowledge.execute("DELETE FROM emb")
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    result = check_embedding_missing(ctx)
    assert result.status == "warn"
    assert result.details["count"] >= 1


def test_embedding_stale_flags_planted_stale_chunk(store, program_root):
    """M7 acceptance criterion (design Section 12): "doctor flags planted
    stale chunk ... as anchors_dangling" -- the embedding-index half: a
    chunk's vec index entry exists but no longer matches its current
    sha256 (simulating a rechunk that changed the text without a
    re-embed/re-index)."""
    doc_id, _launch = _ingest_one_doc(store, program_root)
    chunk = dict(store.knowledge.execute("SELECT * FROM chunk WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone())

    # plant staleness: change the chunk's sha256 in place (as if its text
    # changed) WITHOUT re-embedding/re-indexing -- the vec_chunks entry for
    # this chunk_id now points at a vector for the OLD (no-longer-current) text.
    store.knowledge.execute("UPDATE chunk SET sha256 = ? WHERE chunk_id = ?", ("f" * 64, chunk["chunk_id"]))
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    result = check_embedding_stale(ctx)
    assert result.status == "warn"
    assert chunk["chunk_id"] in result.details["chunk_ids"]


def test_embedding_stale_passes_when_no_vec_table_yet(store, program_root):
    ctx = DoctorContext(program_root=program_root)
    result = check_embedding_stale(ctx)
    assert result.status in ("pass", "skip")


def test_anchor_spot_resolve_flags_planted_stale_chunk_text(store, program_root):
    """The quote_sha256-spot-resolve half of anchors_dangling: an anchor
    whose underlying element text changed since it was anchored (a
    "stale chunk" in the acceptance criterion's sense) is flagged even
    though document.sha256 itself wasn't touched."""
    doc_id, _launch = _ingest_one_doc(store, program_root)
    anchor = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone())
    element = dict(
        store.knowledge.execute(
            "SELECT * FROM element WHERE doc_id=? ORDER BY seq LIMIT 1", (doc_id,)
        ).fetchone()
    )
    store.knowledge.execute(
        "UPDATE element SET text = ? WHERE element_id = ?", ("MUTATED TEXT NOT MATCHING ANCHOR", element["element_id"])
    )
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    result = check_anchor_spot_resolve(ctx)
    assert result.status == "warn"
    assert anchor["anchor_id"] in result.details["anchor_ids"]


def test_anchor_spot_resolve_passes_on_untouched_store(store, program_root):
    _ingest_one_doc(store, program_root)
    ctx = DoctorContext(program_root=program_root)
    result = check_anchor_spot_resolve(ctx)
    assert result.status == "pass"


def test_anchors_dangling_doc_sha_mismatch_flags_planted_renormalized_doc(store, program_root):
    """M7 acceptance criterion: "doctor flags ... planted re-normalized
    doc as anchors_dangling" -- the M1-owned half (``trialerror.stores.checks
    .check_anchors_dangling``), exercised here to prove the FULL
    "anchors_dangling" concept (both halves) is satisfied without this
    build touching that out-of-lane file."""
    from trialerror.stores.checks import check_anchors_dangling

    doc_id, _launch = _ingest_one_doc(store, program_root)
    # simulate a re-normalization: document.sha256 moves, anchors don't.
    store.knowledge.execute("UPDATE document SET sha256 = ? WHERE doc_id = ?", ("9" * 64, doc_id))
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    result = check_anchors_dangling(ctx)
    assert result.status == "warn"
    assert result.details["doc_sha256_mismatches"] >= 1
