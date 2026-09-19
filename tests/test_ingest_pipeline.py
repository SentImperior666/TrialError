"""Tests for ``trialerror.ingest.pipeline``: register_source (+ dedup, license
route refusal), add_document (+ path-out-of-tree refusal, cost gate),
requeue_stage's kind mapping."""

from __future__ import annotations

import pytest

from trialerror.ingest import pipeline
from trialerror.ingest.errors import (
    LicenseRouteRefusedError,
    PathOutOfTreeError,
    SourceNotFoundError,
)
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture, write_pdf_text_fixture


def test_register_source_creates_row(store):
    launch_id = bootstrap_launch(store)
    row = pipeline.register_source(
        store, kind="web", title="A Source", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    assert row["source_id"].startswith("SRC-")
    assert row["request_state"] == "delivered"
    assert row.get("dedup_of") is None


def test_register_source_duplicate_sha_returns_existing_with_dedup_of(store):
    """M7 acceptance criterion (design Section 12): "duplicate sha ->
    dedup_of"."""
    launch_id = bootstrap_launch(store)
    first = pipeline.register_source(
        store, kind="paper", title="Paper One", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id, content_sha256="deadbeef" * 8,
    )
    second = pipeline.register_source(
        store, kind="paper", title="Paper One Again", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id, content_sha256="deadbeef" * 8,
    )
    assert second["source_id"] == first["source_id"]
    assert second["dedup_of"] == first["source_id"]

    count = store.knowledge.execute(
        "SELECT COUNT(*) FROM source WHERE content_sha256 = ?", ("deadbeef" * 8,)
    ).fetchone()[0]
    assert count == 1  # no second row was ever inserted


def test_register_source_different_sha_creates_distinct_rows(store):
    launch_id = bootstrap_launch(store)
    a = pipeline.register_source(
        store, kind="paper", title="A", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id, content_sha256="a" * 64,
    )
    b = pipeline.register_source(
        store, kind="paper", title="B", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id, content_sha256="b" * 64,
    )
    assert a["source_id"] != b["source_id"]


def test_register_source_refuses_route_outside_license_posture_allowlist(store):
    launch_id = bootstrap_launch(store)
    config = {"license": {"allowed_acquisition_routes": ["user_scan", "user_delivered"]}}
    with pytest.raises(LicenseRouteRefusedError):
        pipeline.register_source(
            store, kind="rulebook", title="Restricted", license_tier="commercial_restricted",
            acquisition_route="web", registered_by_launch=launch_id, config=config,
        )


def test_register_source_permissive_when_no_license_posture_configured(store):
    launch_id = bootstrap_launch(store)
    row = pipeline.register_source(
        store, kind="rulebook", title="Anything", license_tier="unknown", acquisition_route="web",
        registered_by_launch=launch_id, config=None,
    )
    assert row["source_id"]


def test_add_document_refuses_path_outside_ingest_roots(store, program_root):
    """M7 acceptance criterion (design Section 12): "manifest-glob wart
    test (out-of-tree path refused)" -- design Section 6: "register
    refuses paths outside raw/inbox globs (the ops-manifest-as-source
    wart)"."""
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    outside_dir = program_root.parent / "definitely_not_raw_or_inbox"
    outside_dir.mkdir(parents=True, exist_ok=True)
    stray = write_html_fixture(outside_dir / "manifest.html")

    with pytest.raises(PathOutOfTreeError):
        pipeline.add_document(
            store, program_root=program_root, source_id=source["source_id"], raw_path=stray,
            created_by_launch=launch_id,
        )


def test_add_document_accepts_path_under_configured_raw_root(store, program_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(raw_dir / "doc.html")

    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=doc_path,
        created_by_launch=launch_id,
    )
    assert result["document"]["doc_id"].startswith("DOC-")
    # schema-v2: normalize is a first-class job.kind (no more kind='custom'
    # wrapping) -- see pipeline.stage_job_kind_and_payload.
    assert result["job"]["kind"] == "normalize"


def test_add_document_accepts_path_under_configured_inbox_root(store, program_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    inbox_dir = program_root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(inbox_dir / "doc.html")

    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=doc_path,
        created_by_launch=launch_id,
    )
    assert result["document"]["doc_id"].startswith("DOC-")


# ---- [paths].archive_dir knob (the import-design notes (internal, not in this export) Sec 5 knob #5) --------


def test_add_document_default_rel_path_matches_unconfigured_behavior(store, program_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(raw_dir / "doc.html")

    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=doc_path,
        created_by_launch=launch_id, config={},
    )
    doc_id = result["document"]["doc_id"]
    assert result["document"]["rel_path"] == f"archive/{doc_id}.txt"


def test_add_document_respects_configured_relative_archive_dir(store, program_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(raw_dir / "doc.html")

    config = {"paths": {"archive_dir": "stash/archive"}}
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=doc_path,
        created_by_launch=launch_id, config=config,
    )
    doc_id = result["document"]["doc_id"]
    assert result["document"]["rel_path"] == f"stash/archive/{doc_id}.txt"
    assert store.program_root / result["document"]["rel_path"] == program_root / "stash" / "archive" / f"{doc_id}.txt"


def test_add_document_respects_configured_absolute_archive_dir(store, program_root, tmp_path):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(raw_dir / "doc.html")

    external_archive = tmp_path / "external-archive"
    config = {"paths": {"archive_dir": str(external_archive)}}
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=doc_path,
        created_by_launch=launch_id, config=config,
    )
    doc_id = result["document"]["doc_id"]
    # pathlib's own join-override behavior: program_root / <absolute
    # rel_path> resolves to the absolute path, not a nested subdirectory --
    # this is what trialerror.ingest.handlers._finish_normalize_stage relies on
    # to archive the normalized text at the CONFIGURED location.
    joined = program_root / result["document"]["rel_path"]
    assert joined == external_archive / f"{doc_id}.txt"


def test_add_document_unknown_source_raises(store, program_root):
    launch_id = bootstrap_launch(store)
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(raw_dir / "doc.html")
    with pytest.raises(SourceNotFoundError):
        pipeline.add_document(
            store, program_root=program_root, source_id="SRC-does-not-exist", raw_path=doc_path,
            created_by_launch=launch_id,
        )


def test_add_document_cost_gate_refuses_large_doc_without_yes(store, program_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="Big", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    big_pdf = write_pdf_text_fixture(raw_dir / "big.pdf", ["page text"] * 60)  # over the 50-page default threshold

    with pytest.raises(ValueError):
        pipeline.add_document(
            store, program_root=program_root, source_id=source["source_id"], raw_path=big_pdf,
            created_by_launch=launch_id, yes=False,
        )

    # --yes proceeds past the gate
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=big_pdf,
        created_by_launch=launch_id, yes=True,
    )
    assert result["document"]["doc_id"].startswith("DOC-")
    assert result["cost_estimate"]["pages"] == 60


def test_estimate_cost_reports_pages_chunks_tokens(program_root, tmp_path):
    path = write_pdf_text_fixture(tmp_path / "x.pdf", ["a page"] * 3)
    est = pipeline.estimate_cost(path, "pdf-text")
    assert est["pages"] == 3
    assert est["est_chunks"] >= 1
    assert est["est_embed_tokens"] > 0


def test_requeue_stage_chunk_uses_first_class_kind(store, program_root):
    """schema-v2: chunk is a first-class job.kind (docs/the migration-plan notes (internal, not in this export)
    Section 4 item 2; docs/INTEGRATION_NOTES.md item 8) -- no more
    kind='custom' + payload['handler'] wrapping."""
    launch_id = bootstrap_launch(store)
    job = pipeline.requeue_stage(store, doc_id="DOC-fake", kind="chunk", created_by_launch=launch_id)
    assert job["kind"] == "chunk"
    import json

    assert "handler" not in json.loads(job["payload"])


def test_requeue_stage_embed_uses_first_class_kind(store, program_root):
    launch_id = bootstrap_launch(store)
    job = pipeline.requeue_stage(store, doc_id="DOC-fake", kind="embed", created_by_launch=launch_id)
    assert job["kind"] == "embed"


def test_requeue_stage_normalize_uses_first_class_kind(store, program_root):
    """schema-v2: normalize is a first-class job.kind too (same migration as
    chunk, above)."""
    launch_id = bootstrap_launch(store)
    job = pipeline.requeue_stage(store, doc_id="DOC-fake", kind="normalize", created_by_launch=launch_id)
    assert job["kind"] == "normalize"


def test_worker_dispatch_handles_both_spellings_of_normalize_and_chunk(store, program_root):
    """Backward compat named in the schema-v2 mission: a pre-migration
    ``kind='custom'``/``payload={'handler': 'normalize'|'chunk'}`` job row
    (the OLD spelling ``stage_job_kind_and_payload`` used to enqueue) must
    still execute exactly like a post-migration first-class
    ``kind='normalize'``/``'chunk'`` row (the NEW spelling) -- both reach
    the SAME registered handler code in ``trialerror.ingest.handlers``, proven
    end-to-end via ``trialerror.jobs.worker.run_one`` (not just registry lookup):
    each variant is run against a nonexistent ``doc_id``, so both must fail
    for the identical reason (the handler body's own ``RuntimeError``), not
    for a dispatch-layer reason (``UnknownHandlerError``/"missing handler
    key"). If either spelling failed to resolve, ``last_error`` would show a
    dispatch-layer message instead of the handler's own."""
    from trialerror.jobs import ledger
    from trialerror.jobs.worker import run_one
    from trialerror.util.ids import new_id

    for stage in ("normalize", "chunk"):
        payload_body = {"doc_id": "DOC-nonexistent", "created_by_launch": "LNCH-fake"}

        # OLD spelling: kind='custom' + payload['handler'] (pre-schema-v2).
        old_job_id = f"JOB-oldstyle-{stage}-{new_id('X')}"
        old_payload = {**payload_body, "handler": stage}
        ledger.enqueue(store, kind="custom", payload=old_payload, job_id=old_job_id)
        old_result = run_one(store, job_id=old_job_id, kind="custom", payload=old_payload)
        assert old_result["status"] == "failed"
        old_row = ledger.get_job(store, old_job_id)
        assert f"no such document 'DOC-nonexistent'" in old_row["last_error"]

        # NEW spelling: kind=stage directly (post-schema-v2, via
        # stage_job_kind_and_payload).
        new_kind, new_payload = pipeline.stage_job_kind_and_payload(stage, dict(payload_body))
        assert new_kind == stage  # no more 'custom' wrapping
        new_job_id = f"JOB-newstyle-{stage}-{new_id('X')}"
        ledger.enqueue(store, kind=new_kind, payload=new_payload, job_id=new_job_id)
        new_result = run_one(store, job_id=new_job_id, kind=new_kind, payload=new_payload)
        assert new_result["status"] == "failed"
        new_row = ledger.get_job(store, new_job_id)
        assert f"no such document 'DOC-nonexistent'" in new_row["last_error"]

        # both spellings reached the identical handler failure.
        assert old_row["last_error"] == new_row["last_error"]
