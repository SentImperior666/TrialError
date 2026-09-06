"""Lane L0-C doctor checks: ``offload_backlog``, ``offload_stale_claims``,
``offload_failed``, ``fake_backend_rows`` -- plus the HOME "what needs a
human" item they pair with (design section 4's doctor row).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trialerror.offload import protocol
from trialerror.offload.checks import (
    check_fake_backend_rows,
    check_offload_backlog,
    check_offload_failed,
    check_offload_stale_claims,
)
from trialerror.offload.dashboard_items import offload_backlog_items
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, registered_checks
from trialerror.util.timeutil import now, parse
from tests._offload_fixtures import queue_one


@pytest.fixture()
def ctx(program_root, platform_root):
    return DoctorContext(program_root=program_root, platform_root=platform_root)


@pytest.fixture()
def root(program_root):
    return protocol.ensure_layout(protocol.offload_root(program_root))


def _age_manifest(root, job_id, seconds):
    path = protocol.pending_dir(root) / f"{job_id}.json"
    manifest = protocol.read_json(path)
    manifest["created_ts"] = (parse(now()) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    protocol.write_json(path, manifest)


def _age_heartbeat(root, job_id, worker_id, seconds):
    hb = protocol.claimed_dir(root) / worker_id / f"{job_id}{protocol.HEARTBEAT_SUFFIX}"
    hb.write_text(
        (parse(now()) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z") + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
def test_every_offload_check_is_auto_discovered():
    """Dropping ``trialerror/offload/checks.py`` IS the registration step --
    no shared file was edited to add these."""
    discover_and_register_checks()
    names = set(registered_checks())
    assert {"offload_backlog", "offload_stale_claims", "offload_failed", "fake_backend_rows"} <= names


def test_checks_skip_a_program_with_no_offload_queue(ctx):
    for check in (check_offload_backlog, check_offload_stale_claims, check_offload_failed):
        assert check(ctx).status == "skip"


def test_backlog_passes_on_an_empty_queue(ctx, root):
    result = check_offload_backlog(ctx)
    assert result.status == "pass"
    assert "no documents" in result.message


def test_backlog_passes_for_a_young_queue_and_warns_past_24h(ctx, root):
    """A queue with work in it is the DESIGNED state of a GPU that is off
    most of the time -- only an old one is worth a human's attention."""
    queue_one(root, "JOB-a")
    fresh = check_offload_backlog(ctx)
    assert fresh.status == "pass" and fresh.details["pending"] == 1

    _age_manifest(root, "JOB-a", protocol.BACKLOG_WARN_S + 3600)
    stale = check_offload_backlog(ctx)
    assert stale.status == "warn"
    assert "DEV GPU" in stale.message
    assert stale.details["oldest_pending_age_s"] > protocol.BACKLOG_WARN_S


def test_stale_claims_warns_only_past_the_expiry(ctx, root):
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    assert check_offload_stale_claims(ctx).status == "pass"

    _age_heartbeat(root, "JOB-a", "dev", protocol.DEFAULT_CLAIM_EXPIRY_S * 2)
    result = check_offload_stale_claims(ctx)
    assert result.status == "warn"
    assert "reclaim" in result.message
    assert [c["job_id"] for c in result.details["stale"]] == ["JOB-a"]


def test_offload_failed_reports_the_reason(ctx, root):
    manifest = queue_one(root, "JOB-a")
    assert check_offload_failed(ctx).status == "pass"
    protocol.fail_marker(root, "JOB-a", manifest=manifest, error="stub marker: this document is corrupt")
    result = check_offload_failed(ctx)
    assert result.status == "warn"
    assert result.details["reasons"]["JOB-a"].startswith("stub marker")


# ---------------------------------------------------------------------------
# fake_backend_rows
# ---------------------------------------------------------------------------
def _ingest_with_fake_backends(store, program_root):
    from trialerror.ingest import pipeline
    from trialerror.jobs.worker import run_one
    from tests._ingest_fixtures import bootstrap_launch, write_scanned_pdf_fixture

    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="t", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    pipeline.add_document(
        store,
        program_root=program_root,
        source_id=source["source_id"],
        raw_path=write_scanned_pdf_fixture(raw / "scan.pdf"),
        created_by_launch=launch_id,
        media_type="pdf-scan",
    )
    for i in range(8):
        if run_one(store, worker_id=f"w{i}")["status"] == "idle":
            break


def test_fake_backend_rows_warns_in_a_permissive_program(store, ctx, program_root):
    """A scratch or test program running the fake backends on purpose is
    not broken -- it is the documented default."""
    _ingest_with_fake_backends(store, program_root)
    result = check_fake_backend_rows(ctx)
    assert result.status == "warn"
    assert result.details["fake_ocr_documents"] == 1
    assert result.details["fake_emb_model_keys"]


def test_fake_backend_rows_fails_once_the_program_declares_real_backends(store, ctx, program_root):
    """D13: in a program that says it will not accept fake backends, fake
    rows already in the record are a data-integrity failure."""
    _ingest_with_fake_backends(store, program_root)
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "p"\n\n[ingest]\nrequire_real_backends = true\n\n'
        '[ingest.ocr]\nbackend = "offload"\n\n[ingest.embed]\nbackend = "offload"\nmodel_key = "m"\n',
        encoding="utf-8",
    )
    result = check_fake_backend_rows(ctx)
    assert result.status == "fail"
    assert "re-ingested or deleted" in result.message


def test_fake_backend_rows_passes_on_a_clean_record(store, ctx):
    assert check_fake_backend_rows(ctx).status == "pass"


# ---------------------------------------------------------------------------
# HOME item
# ---------------------------------------------------------------------------
def test_home_item_appears_only_when_something_is_waiting(store, program_root, platform_root, root):
    from trialerror.dashboard.store_ro import open_store_ro

    with open_store_ro(program_root, platform_root=platform_root) as rostore:
        assert offload_backlog_items(rostore) == []

        queue_one(root, "JOB-a")
        queue_one(root, "JOB-b")
        items = offload_backlog_items(rostore)
        assert len(items) == 1
        assert items[0]["kind"] == "offload_backlog"
        assert items[0]["pending"] == 2
        assert items[0]["blocking"] is False
        assert "GPU worker" in items[0]["summary"]


def test_home_item_is_wired_into_the_determinations_panel(store, program_root, platform_root, root):
    from trialerror.dashboard.data import build_determinations_panel
    from trialerror.dashboard.store_ro import open_store_ro

    queue_one(root, "JOB-a")
    with open_store_ro(program_root, platform_root=platform_root) as rostore:
        panel = build_determinations_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["counts_by_kind"].get("offload_backlog") == 1
