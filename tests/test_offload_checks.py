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


# ===========================================================================
# C-0097 D7 -- the two control checks
#
# Acceptance E: each demonstrates pass / warn / fail on fixtures. The split
# between them is the point: `worker_heartbeat_stale` is about a worker that
# went quiet or is IGNORING control; `offload_control_orphaned` is about a
# request with nobody to read it. Only one of the five outcomes is a `fail`,
# and it is the one that says a worker is defective rather than absent.
# ===========================================================================
def _report(root, worker_id="dev", *, job_id=None, state="running", age_s=0.0, **fields):
    """One heartbeat's worth of progress, written the way the wrapper writes
    it, with the stamp aged by ``age_s`` so the lost window is reachable
    without waiting."""
    from trialerror.offload import control as control_api
    from trialerror.offload.transport import LocalTransport

    target = job_id or protocol.idle_job_id(worker_id)
    payload = {"worker_id": worker_id, "state": state}
    if job_id:
        payload["job_id"] = job_id
    payload.update(fields)
    LocalTransport(root, worker_id=worker_id).heartbeat(
        target, progress=control_api.encode_progress(payload)
    )
    if age_s:
        _age_heartbeat(root, target, worker_id, age_s)


def _request(root, request, *, worker_id="dev", age_s=0.0):
    from trialerror.offload import control as control_api

    ts = (parse(now()) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return control_api.request_control(
        root, worker_id=worker_id, request=request, by_launch="LNCH-test", ts=ts,
        require_worker=False,
    )


def test_worker_heartbeat_stale_skips_without_a_queue(program_root, platform_root):
    from trialerror.offload.checks import check_offload_control_orphaned, check_worker_heartbeat_stale

    ctx = DoctorContext(program_root=program_root, platform_root=platform_root)
    assert check_worker_heartbeat_stale(ctx).status == "skip"
    assert check_offload_control_orphaned(ctx).status == "skip"


def test_worker_heartbeat_stale_passes_on_a_quiet_queue_and_on_a_live_worker(ctx, root):
    from trialerror.offload.checks import check_worker_heartbeat_stale

    empty = check_worker_heartbeat_stale(ctx)
    assert empty.status == "pass"
    assert "no worker has reported" in empty.message

    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _report(root, job_id="JOB-a", units_done=3, units_total=9, unit="chunk")
    live = check_worker_heartbeat_stale(ctx)
    assert live.status == "pass"
    assert "1 worker(s) reporting" in live.message
    assert live.details["stale_with_claim"] == []


def test_worker_heartbeat_stale_warns_when_a_claim_holder_goes_quiet(ctx, root):
    """The ordinary closed-laptop state: warn, never fail, for exactly the
    reason ``offload_stale_claims`` warns -- ``offload reclaim`` converges it
    on its own once the 60-minute expiry passes."""
    from trialerror.offload.checks import check_worker_heartbeat_stale

    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _report(root, job_id="JOB-a", age_s=5_000)
    result = check_worker_heartbeat_stale(ctx)
    assert result.status == "warn"
    assert "dev" in result.message
    assert "offload reclaim" in result.message
    assert [r["worker_id"] for r in result.details["stale_with_claim"]] == ["dev"]
    assert result.details["lost_after_s"] == 660.0


def test_worker_heartbeat_stale_does_not_warn_for_a_quiet_worker_with_no_claim(ctx, root):
    """A worker that finished and went home is not a problem. The warn is
    specifically "it is still holding something and has stopped talking"."""
    from trialerror.offload.checks import check_worker_heartbeat_stale

    _report(root, state="idle", age_s=5_000)
    assert check_worker_heartbeat_stale(ctx).status == "pass"


def test_worker_heartbeat_stale_fails_on_a_stop_the_worker_is_ignoring(ctx, root):
    """The one FAIL in this pair, and the only place in the subsystem where a
    worker's own behaviour is called a defect. Nothing here can kill a process
    (D8), so a worker still reporting work an hour after a stop is the single
    failure mode that would make the control law untrue -- and it has to be
    loud, because the remedy is a human at the keyboard."""
    from trialerror.offload.checks import check_worker_heartbeat_stale

    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _report(root, job_id="JOB-a", units_done=1)
    _request(root, "stop", age_s=7_200)

    result = check_worker_heartbeat_stale(ctx)
    assert result.status == "fail"
    assert "dev" in result.message
    assert "ignores control" in result.message
    assert [r["worker_id"] for r in result.details["ignored_stops"]] == ["dev"]

    # A FRESH stop is not a failure: the worker gets to reach its next
    # checkpoint, which on an OCR job can be a long way off (D6). Re-requesting
    # overwrites the same CONTROL.json with a current timestamp.
    _request(root, "stop")
    assert check_worker_heartbeat_stale(ctx).status == "pass"


def test_worker_heartbeat_stale_does_not_fail_for_a_stopped_worker_that_went_away(ctx, root):
    """A stop that was obeyed ends with the worker gone -- so a stale stop
    beside a silent worker is the system working, not a defect. The FAIL is
    reserved for a worker that is demonstrably still running."""
    from trialerror.offload.checks import check_worker_heartbeat_stale

    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _report(root, job_id="JOB-a", age_s=5_000)
    _request(root, "stop", age_s=7_200)
    result = check_worker_heartbeat_stale(ctx)
    assert result.status == "warn", result.message
    assert result.details["ignored_stops"] == []


def test_a_worker_cannot_rename_itself_out_of_its_own_warning(ctx, root):
    """FIX V-7. The row's id used to come from the PAYLOAD -- which the wrapper
    never checks -- while this check matches row ids against the claim
    DIRECTORIES ``list_claims`` returns. So a worker reporting a different id
    silenced its own stale-claim warning: same 5,000-second-old stamp, same held
    claim, only the payload's ``worker_id`` changed, and the check went from warn
    to pass. A defect-detector the watched thing can switch off is not one."""
    from trialerror.offload import control as control_api
    from trialerror.offload.checks import check_worker_heartbeat_stale

    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _report(root, job_id="JOB-a", worker_id="dev", age_s=5_000)
    # the payload claims another identity, written into dev's own claim directory
    _report(root, job_id="JOB-a", worker_id="dev", age_s=5_000)
    path = protocol.progress_path(root, "dev", "JOB-a")
    path.write_text(
        path.read_text(encoding="utf-8").replace('"worker_id":"dev"', '"worker_id":"ghost"'),
        encoding="utf-8",
    )

    row = control_api.worker_row(root, "dev")
    assert row["worker_id"] == "dev", "the row is keyed on the claim directory"
    assert row["reported_worker_id"] == "ghost"
    assert row["worker_id_mismatch"] is True

    result = check_worker_heartbeat_stale(ctx)
    assert result.status == "warn", result.message
    assert [r["worker_id"] for r in result.details["stale_with_claim"]] == ["dev"]


def test_a_live_worker_with_a_mismatched_payload_id_is_a_warning_of_its_own(ctx, root):
    """A worker writing under the wrong name is neither silent nor stale, so
    nothing used to say anything -- while every control act and every count
    addressed a name the card was not showing."""
    from trialerror.offload.checks import check_worker_heartbeat_stale

    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _report(root, job_id="JOB-a", worker_id="dev")
    path = protocol.progress_path(root, "dev", "JOB-a")
    path.write_text(
        path.read_text(encoding="utf-8").replace('"worker_id":"dev"', '"worker_id":"gpu-2"'),
        encoding="utf-8",
    )

    result = check_worker_heartbeat_stale(ctx)
    assert result.status == "warn", result.message
    assert "disagree with the claim directory" in result.message
    assert result.details["mismatched_ids"] == [
        {"worker_id": "dev", "reported_worker_id": "gpu-2", "job_id": "JOB-a"}
    ]


def test_a_control_request_for_a_worker_reporting_another_name_is_not_orphaned(ctx, root):
    """The other half of the same defect: the orphan check looks the row up by
    id, so a live worker whose payload renamed it turned its own pending request
    into "nobody is listening"."""
    from trialerror.offload.checks import check_offload_control_orphaned

    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _report(root, job_id="JOB-a", worker_id="dev")
    path = protocol.progress_path(root, "dev", "JOB-a")
    path.write_text(
        path.read_text(encoding="utf-8").replace('"worker_id":"dev"', '"worker_id":"gpu-2"'),
        encoding="utf-8",
    )
    _request(root, "pause")

    result = check_offload_control_orphaned(ctx)
    assert result.status == "pass", result.message
    assert result.details["orphaned"] == []


def test_offload_control_orphaned_passes_warns_and_names_the_worker(ctx, root):
    from trialerror.offload.checks import check_offload_control_orphaned

    assert check_offload_control_orphaned(ctx).status == "pass"

    # a live worker reading it: nothing to report
    _report(root, state="idle")
    _request(root, "pause")
    live = check_offload_control_orphaned(ctx)
    assert live.status == "pass"
    assert "live worker" in live.message

    # the same request, for a worker that never reported
    _request(root, "pause", worker_id="ghost")
    orphan = check_offload_control_orphaned(ctx)
    assert orphan.status == "warn"
    assert "ghost" in orphan.message
    names = {o["worker_id"] for o in orphan.details["orphaned"]}
    assert names == {"ghost"}
    assert orphan.details["orphaned"][0]["worker_seen"] is False


def test_offload_control_orphaned_counts_a_lost_worker_as_no_worker(ctx, root):
    """"No worker to read it" is a reading about the heartbeat age, not about
    whether a directory exists -- otherwise a request left for a laptop that
    was shut last week would look like it was being read."""
    from trialerror.offload.checks import check_offload_control_orphaned

    _report(root, state="idle", age_s=5_000)
    _request(root, "stop")
    result = check_offload_control_orphaned(ctx)
    assert result.status == "warn"
    assert result.details["orphaned"][0]["worker_seen"] is True


def test_both_control_checks_are_auto_discovered_in_the_offload_category():
    """The registration rule this package documents: dropping the file IS the
    registration step, and the category decides where the row prints."""
    discover_and_register_checks()
    registry = registered_checks()
    for name in ("worker_heartbeat_stale", "offload_control_orphaned"):
        assert name in registry, name
        assert registry[name][0] == "offload"
