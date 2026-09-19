"""Cancelling work: retraction takes its own pending jobs with it, the stage
runner refuses a retracted document at claim time, and an operator has a verb
for their own cleanups.

Lane FB-3 item 9. Observed 2026-09-15: a duplicate registration was retracted
while its normalize job was still ``pending``, and the job survived the
retraction -- an orphan the next worker would have claimed and run against a
document whose derived rows had just been removed. The jobs were paused by
hand. Until this lane the only route to ``abandoned`` was exhausting
``max_attempts``, so "cancel this" meant letting a job run and fail three
times, or leaving a paused row in the queue forever.
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest import pipeline
from trialerror.ingest.retract import retract_document
from trialerror.jobs import ledger
from trialerror.jobs.errors import InvalidTransitionError, JobNotFoundError
from trialerror.jobs.worker import run_one

from tests._ingest_fixtures import bootstrap_launch, write_html_fixture


def _enqueue(store, job_id, *, doc_id=None, kind="custom", state=None):
    payload = {"handler": "noop"}
    if doc_id:
        payload["doc_id"] = doc_id
    job = ledger.enqueue(store, kind=kind, payload=payload, job_id=job_id)
    if state and state != job["state"]:
        store.jobs.execute("UPDATE job SET state = ? WHERE job_id = ?", (state, job_id))
        store.jobs.commit()
    return ledger.get_job(store, job_id)


def _registered_document(store, program_root, launch_id):
    """A document registered with its first stage job PENDING -- the shape the
    live incident had: added, not yet drained."""
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = write_html_fixture(raw_dir / "dupe.html")
    source_id = pipeline.register_source(
        store, kind="paper", title="Fixture", license_tier="open",
        acquisition_route="web", registered_by_launch=launch_id,
    )["source_id"]
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source_id, raw_path=path,
        created_by_launch=launch_id,
    )
    return result["document"]["doc_id"], result["job"]["job_id"]


# ---------------------------------------------------------------------------
# ledger.abandon
# ---------------------------------------------------------------------------


def test_abandon_settles_a_pending_job_with_its_reason(store):
    _enqueue(store, "JOB-a")
    row = ledger.abandon(store, "JOB-a", reason="superseded by a re-ingest")
    assert row["state"] == "abandoned"
    assert row["last_error"] == "superseded by a re-ingest"
    assert row["settled_ts"]
    events = [e["type"] for e in ledger.list_events(store, "JOB-a")]
    assert "abandoned" in events


def test_abandon_settles_a_paused_job(store):
    _enqueue(store, "JOB-b")
    ledger.pause(store, "JOB-b")
    assert ledger.abandon(store, "JOB-b", reason="cleanup")["state"] == "abandoned"


def test_abandon_settles_a_failed_job_with_attempts_left(store):
    _enqueue(store, "JOB-c", state="failed")
    assert ledger.abandon(store, "JOB-c", reason="cleanup")["state"] == "abandoned"


@pytest.mark.parametrize("held", ["running", "claimed"])
def test_abandon_is_refused_while_a_worker_holds_the_job(store, held):
    """Settling a held job terminally would let its worker complete a row the
    ledger has already closed."""
    _enqueue(store, "JOB-d", state=held)
    with pytest.raises(InvalidTransitionError) as exc:
        ledger.abandon(store, "JOB-d", reason="cleanup")
    assert held in str(exc.value)
    assert "pause it first" in str(exc.value)
    assert ledger.get_job(store, "JOB-d")["state"] == held


def test_abandon_is_refused_on_a_completed_job(store):
    _enqueue(store, "JOB-e", state="complete")
    with pytest.raises(InvalidTransitionError):
        ledger.abandon(store, "JOB-e", reason="cleanup")


def test_abandon_is_idempotent(store):
    _enqueue(store, "JOB-f")
    ledger.abandon(store, "JOB-f", reason="first")
    again = ledger.abandon(store, "JOB-f", reason="second")
    assert again["state"] == "abandoned"
    assert again["last_error"] == "first"  # the first reason stands


def test_abandon_refuses_an_unknown_job(store):
    with pytest.raises(JobNotFoundError):
        ledger.abandon(store, "JOB-nope", reason="cleanup")


# ---------------------------------------------------------------------------
# retraction cancels the document's pending work
# ---------------------------------------------------------------------------


def test_retraction_abandons_the_documents_pending_job(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, job_id = _registered_document(store, program_root, launch_id)
    assert ledger.get_job(store, job_id)["state"] == "pending"

    result = retract_document(
        store, doc_id=doc_id, launch_id=launch_id, reason="duplicate registration"
    )

    assert [entry["job_id"] for entry in result["jobs_cancelled"]] == [job_id]
    settled = ledger.get_job(store, job_id)
    assert settled["state"] == "abandoned"
    assert "document retracted: duplicate registration" in settled["last_error"]


def test_the_retraction_event_records_which_jobs_were_cancelled(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, job_id = _registered_document(store, program_root, launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")

    row = store.ops.execute(
        "SELECT payload FROM event WHERE type='document_retracted' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert [entry["job_id"] for entry in payload["jobs_cancelled"]] == [job_id]


def test_retraction_leaves_another_documents_jobs_alone(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, job_id = _registered_document(store, program_root, launch_id)
    _enqueue(store, "JOB-other", doc_id="DOC-somebody-else")

    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")

    assert ledger.get_job(store, "JOB-other")["state"] == "pending"


def test_a_second_retraction_cancels_nothing(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _job_id = _registered_document(store, program_root, launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")
    again = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")
    assert again["already_retracted"] is True
    assert again["jobs_cancelled"] == []


# ---------------------------------------------------------------------------
# the run-time guard
# ---------------------------------------------------------------------------


def test_a_pending_job_on_a_retracted_document_is_refused_at_claim(store, program_root):
    """The job that escaped cancellation -- enqueued after the retraction, or
    held by a worker at the time -- is stopped where every stage passes."""
    launch_id = bootstrap_launch(store)
    doc_id, _job_id = _registered_document(store, program_root, launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")

    # a stage job that appears AFTER the retraction (the shape a held worker's
    # own hand-off to the next stage would produce)
    _enqueue(store, f"JOB-ingest-{doc_id}-chunk-late", doc_id=doc_id)

    result = run_one(store, worker_id="w1")
    assert result["status"] == "refused"
    assert result["reason"] == "document_retracted"
    assert doc_id in result["message"]
    assert ledger.get_job(store, f"JOB-ingest-{doc_id}-chunk-late")["state"] == "abandoned"


def test_a_job_on_a_live_document_still_runs(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _job_id = _registered_document(store, program_root, launch_id)
    _enqueue(store, "JOB-live", doc_id=doc_id)

    seen = []
    for i in range(6):
        result = run_one(store, worker_id=f"w{i}")
        seen.append(result["status"])
        if result["status"] == "idle":
            break
    assert "refused" not in seen


def test_a_job_with_no_doc_id_is_never_refused_by_the_guard(store):
    _enqueue(store, "JOB-nodoc")
    assert run_one(store, worker_id="w1")["status"] == "complete"


# ---------------------------------------------------------------------------
# the CLI verb
# ---------------------------------------------------------------------------


def _cli(argv, program_root, platform_root):
    from trialerror.cli import main

    return main(["--program-root", str(program_root), "--platform-root", str(platform_root), "jobs", *argv])


def test_jobs_abandon_settles_the_job(store, capsys):
    _enqueue(store, "JOB-cli")
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(["abandon", "JOB-cli", "--reason", "operator cleanup"], program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["result"]["state"] == "abandoned"
    assert out["result"]["last_error"] == "operator cleanup"


def test_jobs_abandon_refuses_a_running_job_and_names_the_way_out(store, capsys):
    _enqueue(store, "JOB-cli-running", state="running")
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(["abandon", "JOB-cli-running", "--reason", "cleanup"], program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code != 0
    assert out["error"]["code"] == "InvalidTransitionError"
    assert any(a["argv"][-2] == "pause" for a in out["nextActions"])


def test_jobs_abandon_requires_a_reason(store, capsys):
    program_root, platform_root = store.program_root, store.platform_root
    with pytest.raises(SystemExit):
        _cli(["abandon", "JOB-x"], program_root, platform_root)
    assert "--reason" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Fix pass V-6: a job a worker is holding is reported, not skipped in silence
# ---------------------------------------------------------------------------


def test_retraction_reports_the_jobs_a_worker_is_holding(store, program_root):
    """The near neighbour of the 2026-09-15 incident: the duplicate is
    retracted while its normalize stage is RUNNING. That job cannot be
    settled here (its worker would complete a row the ledger had closed) and
    the run-time guard fires at CLAIM, so it is never caught -- which makes
    saying so the whole of what this surface can do."""
    launch_id = bootstrap_launch(store)
    doc_id, job_id = _registered_document(store, program_root, launch_id)
    store.jobs.execute(
        "UPDATE job SET state='running', claimed_by='w-1' WHERE job_id = ?", (job_id,)
    )
    store.jobs.commit()
    _enqueue(store, "JOB-next", doc_id=doc_id)

    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")

    assert [entry["job_id"] for entry in result["jobs_cancelled"]] == ["JOB-next"]
    held = result["jobs_held"]
    assert [entry["job_id"] for entry in held] == [job_id]
    assert held[0]["state"] == "running"
    assert held[0]["claimed_by"] == "w-1"
    assert job_id in result["jobs_note"] and "w-1" in result["jobs_note"]
    assert "jobs abandon" in result["jobs_note"]
    # Untouched: the ledger did not close a row out from under its worker.
    assert ledger.get_job(store, job_id)["state"] == "running"


def test_the_retraction_event_records_the_held_jobs_too(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, job_id = _registered_document(store, program_root, launch_id)
    store.jobs.execute(
        "UPDATE job SET state='claimed', claimed_by='w-2' WHERE job_id = ?", (job_id,)
    )
    store.jobs.commit()

    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")

    row = store.ops.execute(
        "SELECT payload FROM event WHERE type='document_retracted' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert [entry["job_id"] for entry in payload["jobs_held"]] == [job_id]
    assert payload["jobs_cancelled"] == []


def test_abandon_pending_for_doc_returns_both_lists(store):
    _enqueue(store, "JOB-pending", doc_id="DOC-x")
    _enqueue(store, "JOB-paused", doc_id="DOC-x")
    ledger.pause(store, "JOB-paused")
    _enqueue(store, "JOB-claimed", doc_id="DOC-x", state="claimed")
    _enqueue(store, "JOB-running", doc_id="DOC-x", state="running")

    outcome = ledger.abandon_pending_for_doc(store, "DOC-x", reason="retracted")

    assert {e["job_id"] for e in outcome["cancelled"]} == {"JOB-pending", "JOB-paused"}
    assert {e["job_id"] for e in outcome["held"]} == {"JOB-claimed", "JOB-running"}


def test_the_retract_cli_names_the_held_job_in_its_next_actions(store, program_root, capsys):
    from trialerror.cli import main

    launch_id = bootstrap_launch(store)
    doc_id, job_id = _registered_document(store, program_root, launch_id)
    store.jobs.execute(
        "UPDATE job SET state='running', claimed_by='w-1' WHERE job_id = ?", (job_id,)
    )
    store.jobs.commit()
    platform_root = store.platform_root
    store.close()

    code = main(
        [
            "--program-root", str(program_root), "--platform-root", str(platform_root),
            "ingest", "retract", "--doc-id", doc_id, "--launch-id", launch_id,
            "--reason", "duplicate registration",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    actions = out["nextActions"]
    assert any(job_id in action["argv"] for action in actions)
    assert any("pause" in action["argv"] for action in actions)


# ---------------------------------------------------------------------------
# Fix pass V-7: the guard's own settle cannot take the worker down
# ---------------------------------------------------------------------------


def test_a_lease_stolen_under_the_guard_refuses_the_job_without_killing_the_worker(
    store, program_root, monkeypatch
):
    """`run_loop` does not catch JobError, so an InvalidTransitionError out of
    the guard's `abandon` would end the worker PROCESS instead of settling one
    job. The race is narrow -- a retracted document and a lease swept between
    the claim and the settle -- and the surrounding code is careful to keep
    'one broken job never takes the worker down' true."""
    from trialerror.jobs import worker as worker_mod

    launch_id = bootstrap_launch(store)
    doc_id, job_id = _registered_document(store, program_root, launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")
    # Re-enqueue the stage: the retraction settled the original one.
    _enqueue(store, "JOB-stolen", doc_id=doc_id)

    real = worker_mod._retracted_document_refusal

    def steal_then_refuse(st, job):
        refusal = real(st, job)
        if refusal is not None:
            st.jobs.execute(
                "UPDATE job SET claimed_by = 'w-somebody-else' WHERE job_id = ?", (job["job_id"],)
            )
            st.jobs.commit()
        return refusal

    monkeypatch.setattr(worker_mod, "_retracted_document_refusal", steal_then_refuse)

    result = run_one(store, worker_id="w-mine", job_id="JOB-stolen", kind="custom",
                     payload={"handler": "noop", "doc_id": doc_id})
    assert result["status"] == "refused"
    assert result["reason"] == "document_retracted"
    assert result["settled"] is False  # the other holder's row was left alone
    # The refusal stands either way: this worker did not run the stage.
    assert ledger.get_job(store, "JOB-stolen")["state"] != "complete"


def test_the_guard_reports_the_settle_it_did_make(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _job_id = _registered_document(store, program_root, launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="dupe")
    _enqueue(store, "JOB-late", doc_id=doc_id)

    result = run_one(store, worker_id="w-mine", job_id="JOB-late", kind="custom",
                     payload={"handler": "noop", "doc_id": doc_id})
    assert result["status"] == "refused"
    assert result["settled"] is True
    assert ledger.get_job(store, "JOB-late")["state"] == "abandoned"
