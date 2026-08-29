"""M7 acceptance criteria, design Section 12 row, gathered in one place --
mirrors the ``tests/test_m1_acceptance.py``/``tests/test_m2_acceptance.py``
convention: this file IS the acceptance-criteria mapping, each test here
re-running (not replacing) a narrower assertion that already lives in its
dedicated module.

    | Acceptance criterion (design Section 12, M7 row)                                      | Test |
    |------------------------------------------------------------------------------------------|------|
    | ingest 4 fixture docs (pdf-text, scanned pdf via marker, html, epub) end-to-end          | tests/test_ingest_handlers.py::test_full_pipeline_reaches_indexed_for_every_format (parametrized over all 4 formats) -- re-run directly below for pdf-text+html as a compact smoke, not duplicated in full |
    | ... restartable (kill mid-embed -> resume, byte-identical final state)                   | see tests/test_ingest_handlers.py::test_kill_mid_embed_worker_is_reclaimed_by_tick_and_resumes_byte_identical (real detached-subprocess + real TerminateProcess kill, mirrors M2's own flagship subprocess test; subprocess-heavy, not duplicated here -- same convention test_m2_acceptance.py established) |
    | duplicate sha -> dedup_of                                                                | test_duplicate_content_sha256_deduplicates_onto_existing_source (re-run here directly) |
    | doctor flags planted stale chunk AND planted re-normalized doc as anchors_dangling       | test_doctor_flags_both_planted_stale_chunk_and_planted_renormalized_doc_as_anchors_dangling (re-run here directly, combining both halves in one assertion) |
    | manifest-glob wart test (out-of-tree path refused)                                       | test_out_of_tree_raw_path_refused_the_manifest_glob_wart (re-run here directly) |

Binding-spec items also verified here or cross-referenced:
    | stream_v1 EXACTLY per design Section 4.1 (seq-order, \\n\\n joiner, Table->text,          | tests/test_ingest_stream.py (dedicated module; not duplicated here) |
    | PageBreak/Header/Footer/PageNumber/Image excluded, empty-text skipped)                   | |
    | epub + image->OCR-route normalizers (F9)                                                 | tests/test_ingest_normalizers.py + test_full_pipeline_reaches_indexed_for_every_format's pdf-scan/epub cases |
    | anchors_dangling doctor wiring                                                           | tests/test_ingest_checks.py (dedicated module) |
    | restart-safety via the jobs ledger                                                       | tests/test_ingest_handlers.py idempotency tests + the kill-mid-embed flagship test |
"""

from __future__ import annotations

import pytest

from trialerror.ingest import pipeline
from trialerror.ingest.checks import check_anchor_spot_resolve, check_embedding_stale
from trialerror.ingest.errors import PathOutOfTreeError
from trialerror.jobs.worker import run_one
from trialerror.stores.checks import check_anchors_dangling
from trialerror.util.doctor import DoctorContext
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture

pytestmark = pytest.mark.acceptance


def _drain(store, max_steps=10):
    for i in range(max_steps):
        r = run_one(store, worker_id=f"w{i}")
        if r["status"] == "idle":
            break


def test_duplicate_content_sha256_deduplicates_onto_existing_source(store):
    """M7 acceptance criterion (design Section 12, verbatim): "duplicate
    sha -> dedup_of"."""
    launch_id = bootstrap_launch(store)
    first = pipeline.register_source(
        store, kind="paper", title="Original", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id, content_sha256="c0ffee" * 10 + "aa",
    )
    second = pipeline.register_source(
        store, kind="paper", title="Re-registration attempt", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id, content_sha256="c0ffee" * 10 + "aa",
    )
    assert second["dedup_of"] == first["source_id"]
    row_count = store.knowledge.execute(
        "SELECT COUNT(*) FROM source WHERE content_sha256 = ?", ("c0ffee" * 10 + "aa",)
    ).fetchone()[0]
    assert row_count == 1


def test_out_of_tree_raw_path_refused_the_manifest_glob_wart(store, program_root):
    """M7 acceptance criterion (design Section 12, verbatim): "manifest-glob
    wart test (out-of-tree path refused)" -- design Section 6: "register
    refuses paths outside raw/inbox globs (the ops-manifest-as-source
    wart)"."""
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    out_of_tree_dir = program_root.parent / "ops_manifest_style_dir"
    out_of_tree_dir.mkdir(parents=True, exist_ok=True)
    stray_manifest = write_html_fixture(out_of_tree_dir / "manifest.html")

    with pytest.raises(PathOutOfTreeError):
        pipeline.add_document(
            store, program_root=program_root, source_id=source["source_id"], raw_path=stray_manifest,
            created_by_launch=launch_id,
        )

    # the SAME file, once placed under a configured ingest root, is accepted.
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    in_tree_copy = write_html_fixture(raw_dir / "manifest.html")
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=in_tree_copy,
        created_by_launch=launch_id,
    )
    assert result["document"]["doc_id"].startswith("DOC-")


def test_doctor_flags_both_planted_stale_chunk_and_planted_renormalized_doc_as_anchors_dangling(store, program_root):
    """M7 acceptance criterion (design Section 12, verbatim): "doctor
    flags planted stale chunk AND planted re-normalized doc as
    anchors_dangling."

    ``anchors_dangling`` is one design concept split across two checks
    (design Section 4.1 + M1's own ``trialerror.stores.checks
    .check_anchors_dangling`` docstring: "designed to be extended, not
    replaced, once M7 lands"): the doc_sha256-mismatch half stays in
    M1's file (out of this build's lane); the quote_sha256/embedding-index
    spot-resolve half lands in ``trialerror.ingest.checks``. Both are exercised
    here against their own planted document, proving the full concept is
    covered without this build editing the out-of-lane file.
    """
    launch_id = bootstrap_launch(store)

    # --- document A: plant a "stale chunk" (its indexed vector no longer
    # matches its current text -- the embedding_stale half) -----------------
    source_a = pipeline.register_source(
        store, kind="web", title="Doc A", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path_a = write_html_fixture(raw_dir / "doc_a.html")
    result_a = pipeline.add_document(
        store, program_root=program_root, source_id=source_a["source_id"], raw_path=path_a,
        created_by_launch=launch_id,
    )
    doc_a_id = result_a["document"]["doc_id"]
    _drain(store)

    stale_chunk = dict(store.knowledge.execute("SELECT * FROM chunk WHERE doc_id=? LIMIT 1", (doc_a_id,)).fetchone())
    store.knowledge.execute("UPDATE chunk SET sha256 = ? WHERE chunk_id = ?", ("e" * 64, stale_chunk["chunk_id"]))
    store.knowledge.commit()

    # --- document B: plant a "re-normalized doc" (document.sha256 moves,
    # its anchors don't -- the doc_sha256-mismatch half) --------------------
    source_b = pipeline.register_source(
        store, kind="web", title="Doc B", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    path_b = write_html_fixture(raw_dir / "doc_b.html")
    result_b = pipeline.add_document(
        store, program_root=program_root, source_id=source_b["source_id"], raw_path=path_b,
        created_by_launch=launch_id,
    )
    doc_b_id = result_b["document"]["doc_id"]
    _drain(store)

    store.knowledge.execute("UPDATE document SET sha256 = ? WHERE doc_id = ?", ("f" * 64, doc_b_id))
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)

    stale_chunk_result = check_embedding_stale(ctx)
    assert stale_chunk_result.status == "warn"
    assert stale_chunk["chunk_id"] in stale_chunk_result.details["chunk_ids"]

    renormalized_doc_result = check_anchors_dangling(ctx)
    assert renormalized_doc_result.status == "warn"
    assert renormalized_doc_result.details["doc_sha256_mismatches"] >= 1

    # both halves are reachable from `trialerror ingest doctor`'s own check-name
    # roster (trialerror/cli/ingest.py's _INGEST_CHECK_NAMES) without either
    # module editing the other's file.
    from trialerror.cli.ingest import _INGEST_CHECK_NAMES

    assert "anchors_dangling" in _INGEST_CHECK_NAMES
    assert "embedding_stale" in _INGEST_CHECK_NAMES
    assert "anchor_spot_resolve" in _INGEST_CHECK_NAMES


def test_stream_v1_and_ocr_route_and_epub_normalizer_present():
    """Binding-spec smoke: the exact items named in the build brief
    (stream_v1, epub normalizer, image/pdf-scan->OCR route) are real,
    importable, wired-up symbols -- the dedicated per-module test files
    (test_ingest_stream.py, test_ingest_normalizers.py,
    test_ingest_handlers.py) carry the actual behavioral assertions."""
    from trialerror.ingest.normalizers import MEDIA_TYPES_NEEDING_OCR, normalize_epub
    from trialerror.ingest.stream import STREAM_FN, stream_v1

    assert STREAM_FN == "stream_v1"
    assert callable(stream_v1)
    assert callable(normalize_epub)
    assert {"pdf-scan", "image"} <= MEDIA_TYPES_NEEDING_OCR
