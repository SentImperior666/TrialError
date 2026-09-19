"""Lane FB-6 item 8: full-text availability does not wait for embeddings.

The pipeline runs chunk -> embed -> index, so a program whose embeddings are
parked for a GPU run had NO full-text search over anything ingested since:
the documents were chunked, the text was in the store, and the one stage that
would put it in ``chunk_fts`` was queued behind a stage that needs hardware. A
live programme repaired that by hand with ``ingest reindex-fulltext`` after
every batch.

The ``chunk`` handler now enqueues a FULL-TEXT-ONLY ``index`` job beside the
``embed`` one. What these tests hold is both halves of that: the text is
searchable with the embed job still pending, and the vector side is untouched
until ``embed`` completes -- including the document's own status, which must
not read ``indexed`` while its vectors are missing.
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest import handlers, pipeline
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.stores.vecindex import vec_table_name

from tests._ingest_fixtures import bootstrap_launch, write_html_fixture


@pytest.fixture()
def raw_dir(program_root):
    d = program_root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _added(store, program_root, path, config=None):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    return pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=path,
        created_by_launch=launch_id, config=config,
    )


def _run(store, job_id):
    job = ledger.get_job(store, job_id)
    assert job is not None, f"no job {job_id!r}"
    return run_one(
        store, worker_id="w", job_id=job_id, kind=job["kind"], payload=json.loads(job["payload"])
    )


def _through_chunk(store, program_root, raw_dir, config=None):
    path = write_html_fixture(raw_dir / "doc.html")
    result = _added(store, program_root, path, config=config)
    doc_id = result["document"]["doc_id"]
    _run(store, result["job"]["job_id"])            # normalize
    _run(store, f"JOB-ingest-{doc_id}-chunk")
    return doc_id


def _fts_hits(store, doc_id):
    return store.knowledge.execute(
        "SELECT COUNT(*) AS n FROM chunk_fts f JOIN chunk c ON c.chunk_id = f.chunk_id "
        "WHERE c.doc_id = ?",
        (doc_id,),
    ).fetchone()["n"]


# ---------------------------------------------------------------------------
# the knob
# ---------------------------------------------------------------------------


def test_the_knob_defaults_on_and_reads_the_config():
    assert handlers.FULLTEXT_BEFORE_EMBED_DEFAULT is True
    assert handlers.fulltext_before_embed(None) is True
    assert handlers.fulltext_before_embed({}) is True
    assert handlers.fulltext_before_embed({"ingest": {}}) is True
    assert handlers.fulltext_before_embed({"ingest": {"fulltext_before_embed": False}}) is False


def test_the_fulltext_job_id_is_not_the_post_embed_one():
    """``ledger.enqueue`` is create-only: a shared id would swallow the
    hand-off that fills the vector table."""
    assert handlers.fulltext_index_job_id("DOC-1") != handlers.index_job_id("DOC-1", "some-model")


# ---------------------------------------------------------------------------
# a chunked document with a parked embed
# ---------------------------------------------------------------------------


def test_a_chunked_document_with_a_parked_embed_is_fts_searchable(store, program_root, raw_dir):
    doc_id = _through_chunk(store, program_root, raw_dir)
    assert _fts_hits(store, doc_id) == 0, "nothing is indexed until the job runs"

    _run(store, handlers.fulltext_index_job_id(doc_id))

    assert _fts_hits(store, doc_id) > 0
    # ...and the embed job is still sitting there, untouched.
    embed = ledger.get_job(store, f"JOB-ingest-{doc_id}-embed")
    assert embed["state"] == "pending"
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM emb").fetchone()["n"] == 0


def test_the_vector_index_is_untouched_until_embed_completes(store, program_root, raw_dir):
    doc_id = _through_chunk(store, program_root, raw_dir)
    _run(store, handlers.fulltext_index_job_id(doc_id))

    tables = {
        r["name"]
        for r in store.knowledge.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'vec_chunks__%'"
        ).fetchall()
    }
    assert tables == set(), "the full-text pass must not open a vector table"
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM emb").fetchone()["n"] == 0

    # the document is NOT 'indexed' -- that would be a row claiming vectors
    # it does not have
    status = store.knowledge.execute(
        "SELECT status FROM document WHERE doc_id = ?", (doc_id,)
    ).fetchone()["status"]
    assert status == "chunked"


def test_the_vector_side_still_lands_after_embed(store, program_root, raw_dir):
    doc_id = _through_chunk(store, program_root, raw_dir)
    _run(store, handlers.fulltext_index_job_id(doc_id))
    _run(store, f"JOB-ingest-{doc_id}-embed")

    pending = [
        j for j in ledger.list_jobs(store)
        if j["kind"] == "index" and j["job_id"] != handlers.fulltext_index_job_id(doc_id)
    ]
    assert len(pending) == 1, "the post-embed index hand-off must not be swallowed"
    _run(store, pending[0]["job_id"])

    model_key = json.loads(pending[0]["payload"])["model_key"]
    table = vec_table_name(model_key)
    n_vectors = store.knowledge.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    assert n_vectors > 0
    status = store.knowledge.execute(
        "SELECT status FROM document WHERE doc_id = ?", (doc_id,)
    ).fetchone()["status"]
    assert status == "indexed"


def test_the_fulltext_pass_is_idempotent_beside_the_real_one(store, program_root, raw_dir):
    doc_id = _through_chunk(store, program_root, raw_dir)
    _run(store, handlers.fulltext_index_job_id(doc_id))
    first = _fts_hits(store, doc_id)
    _run(store, f"JOB-ingest-{doc_id}-embed")
    index_job = next(
        j for j in ledger.list_jobs(store)
        if j["kind"] == "index" and j["job_id"] != handlers.fulltext_index_job_id(doc_id)
    )
    _run(store, index_job["job_id"])
    assert _fts_hits(store, doc_id) == first, "chunk_fts must not gain a second row per chunk"


def test_the_knob_off_restores_the_old_chain(store, program_root, raw_dir):
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "fixture"\n\n[ingest]\nfulltext_before_embed = false\n', encoding="utf-8"
    )
    doc_id = _through_chunk(store, program_root, raw_dir)
    assert ledger.get_job(store, handlers.fulltext_index_job_id(doc_id)) is None
    assert ledger.get_job(store, f"JOB-ingest-{doc_id}-embed") is not None


def test_a_full_drain_still_ends_indexed_and_searchable(store, program_root, raw_dir):
    path = write_html_fixture(raw_dir / "doc.html")
    result = _added(store, program_root, path)
    doc_id = result["document"]["doc_id"]
    for i in range(8):
        r = run_one(store, worker_id=f"w{i}")
        if r["status"] == "idle":
            break
        assert r["status"] == "complete", r
    status = store.knowledge.execute(
        "SELECT status FROM document WHERE doc_id = ?", (doc_id,)
    ).fetchone()["status"]
    assert status == "indexed"
    assert _fts_hits(store, doc_id) > 0
