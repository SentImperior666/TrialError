"""Tests for ``trialerror.ingest.checks``: chunker_missing/outdated,
embedding_missing/stale, anchor_spot_resolve -- design Section 4.1's
``trialerror ingest doctor`` counts."""

from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------------------
# the configured embed model_key (sandbox-audit lane, 2026-09-06)
#
# ``_active_model_key`` used to return the BACKEND NAME for every non-fake
# backend, so ``backend = "offload"`` made both embedding checks look for
# ``model_key='offload'`` rows -- a key nothing writes, because the offload
# path stamps the CONFIGURED model_key (e.g. 'qwen3-4b'). Result: a fully
# embedded corpus reported as entirely unembedded.
# ---------------------------------------------------------------------------

OFFLOAD_MODEL_KEY = "qwen3-4b"


def _write_embed_config(program_root, table_body: str) -> None:
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n[ingest.embed]\n' + table_body, encoding="utf-8"
    )


def _rekey_emb_rows(store, model_key: str) -> None:
    """Re-stamp every ``emb`` row with ``model_key`` -- what the offload
    path's own writes look like once the DEV worker's vectors land."""
    store.knowledge.execute("UPDATE emb SET model_key = ?", (model_key,))
    store.knowledge.commit()


def test_embedding_missing_honours_configured_model_key_under_offload(store, program_root):
    _ingest_one_doc(store, program_root)
    _rekey_emb_rows(store, OFFLOAD_MODEL_KEY)
    _write_embed_config(
        program_root,
        f'backend = "offload"\nmodel_key = "{OFFLOAD_MODEL_KEY}"\ndims = 2048\n',
    )

    result = check_embedding_missing(DoctorContext(program_root=program_root))
    assert result.details["model_key"] == OFFLOAD_MODEL_KEY
    assert result.status == "pass", result.message
    assert result.details["count"] == 0


def test_embedding_missing_still_flags_a_real_gap_under_offload(store, program_root):
    """The fix must not turn the check into a no-op: with the model_key
    resolved correctly, a genuinely unembedded chunk is still reported."""
    _ingest_one_doc(store, program_root)
    _rekey_emb_rows(store, OFFLOAD_MODEL_KEY)
    store.knowledge.execute("DELETE FROM emb")
    store.knowledge.commit()
    _write_embed_config(
        program_root, f'backend = "offload"\nmodel_key = "{OFFLOAD_MODEL_KEY}"\n'
    )

    result = check_embedding_missing(DoctorContext(program_root=program_root))
    assert result.status == "warn"
    assert result.details["model_key"] == OFFLOAD_MODEL_KEY
    assert result.details["count"] >= 1


def test_embedding_stale_agrees_with_the_configured_model_key(store, program_root):
    """The stale check reads the same key: its vector table is
    ``vec_chunks__<model_key>``, so a mismatched key silently reported
    "no vector index table yet" (a pass) on an indexed corpus."""
    from trialerror.stores.vecindex import ensure_vec_table, serialize_vector_fallback, vec_table_name

    doc_id, _launch = _ingest_one_doc(store, program_root)
    _rekey_emb_rows(store, OFFLOAD_MODEL_KEY)
    _write_embed_config(
        program_root, f'backend = "offload"\nmodel_key = "{OFFLOAD_MODEL_KEY}"\ndims = 4\n'
    )

    conn = store.knowledge
    ensure_vec_table(conn, OFFLOAD_MODEL_KEY, 4)
    table = vec_table_name(OFFLOAD_MODEL_KEY)
    chunk_ids = [r["chunk_id"] for r in conn.execute("SELECT chunk_id FROM chunk").fetchall()]
    assert chunk_ids
    blob = serialize_vector_fallback([0.0, 0.0, 0.0, 0.0])
    for chunk_id in chunk_ids:
        conn.execute(
            f"INSERT OR REPLACE INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)",
            (chunk_id, OFFLOAD_MODEL_KEY, 4, blob),
        )
    conn.commit()

    clean = check_embedding_stale(DoctorContext(program_root=program_root))
    assert clean.details["model_key"] == OFFLOAD_MODEL_KEY
    assert clean.status == "pass", clean.message

    # plant staleness the same way the fake-backend test above does
    conn.execute("UPDATE chunk SET sha256 = ? WHERE chunk_id = ?", ("f" * 64, chunk_ids[0]))
    conn.commit()
    stale = check_embedding_stale(DoctorContext(program_root=program_root))
    assert stale.status == "warn"
    assert chunk_ids[0] in stale.details["chunk_ids"]


def test_fake_backend_model_key_naming_is_unchanged(store, program_root):
    """The pre-existing fake-backend behaviour, pinned: an explicit
    ``dims`` still names ``fake-<dims>``, and no config at all still
    names the fake default."""
    from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS
    from trialerror.ingest.checks import _active_model_key

    _write_embed_config(program_root, 'backend = "fake"\ndims = 32\n')
    assert _active_model_key(DoctorContext(program_root=program_root)) == "fake-32"

    (program_root / "trialerror.toml").unlink()
    assert (
        _active_model_key(DoctorContext(program_root=program_root))
        == f"fake-{DEFAULT_FAKE_EMBED_DIMS}"
    )
    assert _active_model_key(DoctorContext(program_root=None)) == f"fake-{DEFAULT_FAKE_EMBED_DIMS}"


def test_a_stale_model_key_line_does_not_outrank_the_backends_own_naming(store, program_root):
    """``model_key`` is honoured on the ONE branch where the loader reads it
    (``offload``) and ignored on the two where the loader ignores it.

    ``load_embed_backend`` pushes ``model_key=backend_name`` into a real local
    backend, and ``FakeEmbedBackend`` stamps ``fake-<dims>`` from ``dims``
    alone; neither ever looks at the config's ``model_key``. So a line left
    behind by an earlier backend must not steer the doctor at a key nothing on
    disk carries -- that is the original offload defect with its arrow
    reversed."""
    from trialerror.ingest.checks import _active_model_key

    _write_embed_config(program_root, 'backend = "qwen3-4b"\npython_exe = "python"\nmodule_dir = "."\n')
    assert _active_model_key(DoctorContext(program_root=program_root)) == "qwen3-4b"

    _write_embed_config(
        program_root, 'backend = "qwen3-4b"\nmodel_key = "pinned-key"\npython_exe = "python"\nmodule_dir = "."\n'
    )
    assert _active_model_key(DoctorContext(program_root=program_root)) == "qwen3-4b"

    # the reversed arrow: an operator moves back to the fake backend and leaves
    # the offload model_key line in the table.
    _write_embed_config(program_root, 'backend = "fake"\ndims = 32\nmodel_key = "qwen3-4b"\n')
    assert _active_model_key(DoctorContext(program_root=program_root)) == "fake-32"


def test_the_resolver_agrees_with_the_loader_on_every_reachable_shape(store, program_root):
    """The claim this piece rests on, asserted rather than described: for each
    reachable ``[ingest.embed]`` shape, the key the doctor resolves is the key
    the backend ``load_embed_backend`` builds would stamp on its rows."""
    from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS, load_embed_backend
    from trialerror.ingest.checks import _embed_model_key

    shapes = [
        {},
        {"backend": "fake"},
        {"backend": "fake", "dims": 32},
        {"backend": "fake", "dims": 32, "model_key": "qwen3-4b"},
        {"backend": "offload", "model_key": "qwen3-4b", "dims": 2048},
        {"backend": "qwen3-4b", "python_exe": "python", "module_dir": "."},
        {"backend": "qwen3-4b", "model_key": "pinned-key", "python_exe": "python", "module_dir": "."},
    ]
    for cfg in shapes:
        backend = load_embed_backend(dict(cfg))
        assert _embed_model_key(dict(cfg)) == backend.model_key, cfg

    # the one shape the loader cannot build at all: offload with no model_key
    # raises, so there is no row naming to mirror and the resolver degrades to
    # the fake default rather than inventing "offload" as a key.
    with pytest.raises(ValueError):
        load_embed_backend({"backend": "offload"})
    assert _embed_model_key({"backend": "offload"}) == f"fake-{DEFAULT_FAKE_EMBED_DIMS}"


def test_offload_without_a_model_key_falls_back_to_the_fake_default(store, program_root):
    """An ``offload`` table with no ``model_key`` cannot construct a
    backend at all (``OffloadMarker`` refuses), so there is no row naming
    to mirror -- the doctor reports imprecisely rather than inventing
    ``'offload'`` as a key."""
    from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS
    from trialerror.ingest.checks import _active_model_key

    _write_embed_config(program_root, 'backend = "offload"\n')
    assert (
        _active_model_key(DoctorContext(program_root=program_root))
        == f"fake-{DEFAULT_FAKE_EMBED_DIMS}"
    )
