"""The e2e handover-gate journeys, exercised on a development machine:
tmp program roots, no sandbox, no GPU, no network, no live Claude Code.

``docs/reviews/E2E_TEST_PLAN.md`` Section 7.4 is this file's spec. What it
asks for, and what each part of it is guarding:

- Every automated journey runs for real here, against throwaway program and
  platform roots, so a broken journey is caught on the machine that wrote it
  rather than on the sandbox in the middle of a handover.
- **Every journey also has a broken-program-root twin** that proves the
  journey FAILS -- not skips, not warns -- when the thing it checks is wrong.
  That is the whole difference between an acceptance harness and a green
  light: a check that cannot fail is not a check, and the plan's own
  "vacuous" column names exactly the hollow pass each criterion has to
  exclude. The twins here drive the real failure (a corpus with the wrong
  document count, a dashboard serving a program that is not the one under
  test, a translator that answers instead of refusing, an offload stage that
  ran locally instead of parking, emb rows carrying a fake model key), not a
  monkeypatched assertion.
- The enumeration guard: every ``E-nn`` the catalogue marks automated is
  claimed by exactly one journey, every operator item has a skip-marked
  stand-in in this file, and the catalogue's operator keys and the plan's
  human steps cannot drift apart silently.

The skip-marked operator stand-ins live here too, so ``pytest -m acceptance``
shows the whole e2e surface at once: what ran on this machine, and what is
still waiting for the sandbox, the phone and the GPU.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trialerror.accept import e2e as e2e_mod
from trialerror.accept.e2e import (
    E2E_CHECK_CATALOGUE,
    E2E_CHECK_EVENT_TYPE,
    E2E_OPERATOR_ITEMS,
    RUN_ID_RE,
    Capabilities,
    automated_check_ids,
    e2e_check_sequence,
    e2e_operator_enumeration,
    journey_check_ids,
    probe_capabilities,
    read_recorded_checks,
    run_e2e_corpus,
    run_e2e_dashboard,
    run_e2e_offload_enqueue,
    run_e2e_ops,
    verify_e2e_offload_roundtrip,
)

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "e2e-20260101T000000Z"


# ---------------------------------------------------------------------------
# shared fixtures: the three sequential journeys run ONCE for the whole module
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def caps() -> Capabilities:
    return probe_capabilities(repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def corpus_run(tmp_path_factory, caps):
    """The corpus journey, once, against fresh roots. Leaves the session OPEN
    (E-24 in the ops journey is what closes it), so the dashboard and ops
    fixtures below stack on top of this exactly as the phases do in the
    container."""
    base = tmp_path_factory.mktemp("e2e-chain")
    program_root = base / "program"
    platform_root = base / "platform"
    result = run_e2e_corpus(
        program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps
    )
    return {"result": result, "program_root": program_root, "platform_root": platform_root, "base": base}


@pytest.fixture(scope="module")
def dashboard_run(corpus_run, caps):
    assert corpus_run["result"].status in ("pass", "warn"), corpus_run["result"].message
    return run_e2e_dashboard(
        corpus_run["program_root"], corpus_run["platform_root"],
        run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps,
    )


@pytest.fixture(scope="module")
def ops_run(corpus_run, dashboard_run, caps):
    assert dashboard_run.status in ("pass", "warn"), dashboard_run.message
    return run_e2e_ops(
        corpus_run["program_root"], corpus_run["platform_root"],
        run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps,
    )


def _steps(result) -> dict[str, dict]:
    return {s["name"]: s for s in result.details["steps"]}


def _failing_step(result) -> str | None:
    for step in result.details["steps"]:
        if not step["ok"]:
            return step["name"]
    return None


def _open_store(program_root: Path, platform_root: Path):
    from trialerror.stores.store import open_store

    return open_store(program_root, platform_root=platform_root)


def _init_bare_program(base: Path, *, program_id: str, translator: str | None = None) -> tuple[Path, Path]:
    """A scaffolded program with one open session and NO corpus -- the cheapest
    starting point a broken-case twin can fail from for the right reason."""
    program_root = base / "program"
    platform_root = base / "platform"
    platform_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    for argv in (
        [sys.executable, "-m", "trialerror.cli", "program", "init", program_id,
         "--dir", str(program_root), "--platform-root", str(platform_root)],
        [sys.executable, "-m", "trialerror.cli", "--program-root", str(program_root),
         "--platform-root", str(platform_root), "session", "boot", "--create-account", "e2e-broken"],
    ):
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=180)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    toml = program_root / "trialerror.toml"
    extra = '\n[paths]\ningest_roots = ["raw"]\n\n[ingest.ocr]\nbackend = "fake"\n\n[ingest.embed]\nbackend = "fake"\n'
    if translator is not None:
        extra += f'\n[feed.translator]\nbackend = "{translator}"\n'
    toml.write_text(toml.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return program_root, platform_root


# ---------------------------------------------------------------------------
# the corpus journey (E-10..E-16)
# ---------------------------------------------------------------------------
def test_e2e_corpus_journey_passes_on_fake_backends(corpus_run, caps):
    result = corpus_run["result"]
    assert result.status in ("pass", "warn"), result.message
    steps = _steps(result)
    assert [s["name"] for s in result.details["steps"]] == [
        "program_init_cli", "write_scratch_toml", "session_boot_cli_then_hook", "hold_journey_launch",
        "ingest_fixture_slice", "reindex_fulltext", "backend_parity", "retrieve_auto_citation_fence",
        "corpus_stats", "doctor_green", "reconcile_journey_launch",
    ]
    ingest = steps["ingest_fixture_slice"]["detail"]
    assert len(ingest["doc_ids"]) == 4
    assert set(ingest["statuses"].values()) == {"indexed"}
    assert ingest["emb_rows"] == ingest["chunks"] > 0
    assert ingest["chunk_fts_rows"] == ingest["chunks"]
    doctor = steps["doctor_green"]["detail"]
    assert doctor["failed"] == 0
    assert doctor["total"] >= 40
    assert doctor["spawns_vs_bookings"] == "pass"
    assert steps["corpus_stats"]["detail"]["documents"] == 4
    assert steps["program_init_cli"]["detail"]["second_init_code"] == "already_scaffolded"
    assert steps["session_boot_cli_then_hook"]["detail"]["boot_pin_version"] is None
    assert steps["session_boot_cli_then_hook"]["detail"]["hook_reused_open"] is True


def test_e2e_corpus_records_one_e2e_check_row_per_check_id(corpus_run):
    store = _open_store(corpus_run["program_root"], corpus_run["platform_root"])
    try:
        rows = read_recorded_checks(store, run_id=RUN_ID)
    finally:
        store.close()
    for check_id in journey_check_ids("e2e_corpus"):
        assert check_id in rows, f"the corpus journey recorded no row for {check_id}"
        assert rows[check_id]["run_id"] == RUN_ID
        assert rows[check_id]["status"] in ("pass", "blocked")
        assert rows[check_id]["by"] == "journey"


def test_backend_parity_step_toggles_both_backends(corpus_run, caps):
    if not caps.tantivy:
        pytest.skip("this deployment has no tantivy lexical tier; E-14 records `blocked`, never `pass`")
    parity = _steps(corpus_run["result"])["backend_parity"]["detail"]
    assert len(parity["queries"]) == 3
    for entry in parity["queries"]:
        assert set(entry["fts5_ids"]) == set(entry["tantivy_ids"])
        assert entry["top1_equal"] is True


def test_backend_parity_is_blocked_not_passed_without_tantivy(tmp_path, caps, monkeypatch):
    """With the lexical tier reported absent, E-13/E-14 must record `blocked`
    with an owner and drag the journey to `warn` -- never quietly pass on the
    FTS5 fallback, which would read as parity between one backend and itself."""
    degraded = Capabilities(
        tantivy_pkg=False, tantivy_cli=False, translator=caps.translator,
        offload=caps.offload, plan_blob=caps.plan_blob,
    )
    result = run_e2e_corpus(
        tmp_path / "program", tmp_path / "platform", run_id=RUN_ID, repo_root=REPO_ROOT, caps=degraded
    )
    assert result.status == "warn", result.message
    steps = _steps(result)
    for name, owner in (("reindex_fulltext", "lane-d"), ("backend_parity", "lane-d")):
        assert steps[name]["detail"]["blocked"] is True
        assert steps[name]["detail"]["owner"] == owner
    store = _open_store(tmp_path / "program", tmp_path / "platform")
    try:
        rows = read_recorded_checks(store, run_id=RUN_ID)
    finally:
        store.close()
    assert rows["E-13"]["status"] == "blocked"
    assert rows["E-14"]["status"] == "blocked"
    assert rows["E-14"]["owner"] == "lane-d"


def test_e2e_corpus_fails_when_the_fixture_slice_is_wrong(tmp_path, caps, monkeypatch):
    """BROKEN TWIN. Give the journey a corpus that is not the four documents
    the plan names; E-12 must FAIL, not shrug."""
    from trialerror.demo import content

    monkeypatch.setattr(content, "DOCUMENTS", content.DOCUMENTS[:1], raising=True)
    result = run_e2e_corpus(
        tmp_path / "program", tmp_path / "platform", run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps
    )
    assert result.status == "fail", result.message
    assert _failing_step(result) == "ingest_fixture_slice"
    assert "expected 4 fixture documents" in result.message


# ---------------------------------------------------------------------------
# the dashboard journey (E-17, E-70)
# ---------------------------------------------------------------------------
def test_e2e_dashboard_journey_serves_the_scratch_root(dashboard_run, corpus_run):
    result = dashboard_run
    assert result.status == "pass", result.message
    steps = _steps(result)
    served = Path(steps["api_all_meta_program_root_is_scratch"]["detail"]["meta_program_root"])
    assert served.resolve() == corpus_run["program_root"].resolve()
    assert steps["api_all_corpus_panel"]["detail"]["corpus_counts"]["documents"] == 4
    assert steps["api_search_evidence_anchor"]["detail"]["matched_corpus_journey"] is True
    corpus_anchor = _steps(corpus_run["result"])["retrieve_auto_citation_fence"]["detail"]["open_anchor_id"]
    assert steps["api_search_evidence_anchor"]["detail"]["anchor_id"] == corpus_anchor
    assert steps["shutdown_clean"]["detail"]["shutdown_s"] <= 10


def test_e2e_dashboard_write_path_is_refused_twice_then_accepted(dashboard_run):
    steps = _steps(dashboard_run)
    assert steps["write_refused_without_token"]["detail"]["status"] == 403
    assert steps["write_refused_with_wrong_token"]["detail"]["status"] == 403
    accepted = steps["write_accepted_with_token"]["detail"]
    assert accepted["status"] == 200
    assert accepted["author"].startswith("orchestrator:")
    assert steps["feed_post_row_visible"]["detail"]["post_id"] == accepted["post_id"]


def test_e2e_dashboard_fails_when_it_is_serving_the_wrong_program(tmp_path, caps):
    """BROKEN TWIN. A scaffolded program with an OPEN session and no corpus:
    the serve subprocess comes up fine and answers every route, and the
    journey must still FAIL, because a dashboard whose corpus is not this
    run's corpus is the exact hollow pass E-17 exists to exclude."""
    program_root, platform_root = _init_bare_program(tmp_path, program_id="e2e-dash-broken")
    result = run_e2e_dashboard(
        program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps
    )
    assert result.status == "fail", result.message
    assert _failing_step(result) == "api_all_corpus_panel"
    assert "documents" in result.message


# ---------------------------------------------------------------------------
# the ops journey (E-19..E-24, and the copied E-02 row)
# ---------------------------------------------------------------------------
def test_e2e_ops_journey_runs_every_step(ops_run, caps):
    assert ops_run.status in ("pass", "warn"), ops_run.message
    names = [s["name"] for s in ops_run.details["steps"]]
    assert names[:4] == [
        "open_scratch_session", "hold_journey_launch", "feed_thread_post",
        "budget_book_spawn_return_reconcile",
    ], "E-20 must read spawns_vs_bookings BEFORE the translator half consumes a booking no spawn returns"
    assert names[-1] == "record_prior_checks"


def test_e2e_ops_post_task_hook_writes_subagent_return_with_launch_id(ops_run):
    detail = _steps(ops_run)["budget_book_spawn_return_reconcile"]["detail"]
    assert detail["states_seen"] == ["PROVISIONAL", "RUNNING", "RECONCILED"]
    assert detail["subagent_return_event_id"]
    assert detail["dangling_before"] == {"session": 1, "platform": 1}
    assert detail["dangling_after"] == {"session": 0, "platform": 0}
    assert detail["spawns_vs_bookings"] == "pass"
    consumed, returned = detail["consumed_vs_returned"]
    assert consumed == returned == 3, "three journey launches, three subagent_return rows"


def test_e2e_ops_translator_fails_closed_with_the_named_error(ops_run, caps):
    if not caps.translator:
        pytest.skip("this deployment has no feed translator; E-19's translator half records `blocked`")
    detail = _steps(ops_run)["feed_translate_fail_closed"]["detail"]
    assert "created_by_launch is empty" in detail["gate_error_head"]
    assert "has no generation driver" not in detail["gate_error_head"]
    assert detail["failure_class"] == "logic"
    assert "has no generation driver" in detail["last_error_head"]
    assert detail["launch_states_seen"] == ["PROVISIONAL", "RUNNING", "RECONCILED"]
    assert len(detail["paused"]) == 2
    assert detail["feed_panel_translation"] is None


@pytest.fixture(scope="module")
def no_translator_chain(tmp_path_factory, caps):
    """A SECOND full chain -- corpus then ops -- with the translator reported
    ABSENT.

    This is the branch ``_doctor_after_close`` predicts for a deployment
    without lane-b, and on a tree that HAS the translator it is unreachable
    unless the capability is forced off. It needs the REAL corpus: the ops
    journey's E-22 half re-embeds a document, so a corpus-less program dies
    at E-22 and never reaches the shape under test. The dashboard journey is
    skipped deliberately -- nothing in ops depends on it, and the launch
    arithmetic E-20 asserts (exactly one dangling launch, its own) holds at two
    journeys just as it does at three.
    """
    base = tmp_path_factory.mktemp("e2e-no-translator")
    program_root = base / "program"
    platform_root = base / "platform"
    degraded = Capabilities(
        tantivy_pkg=caps.tantivy_pkg, tantivy_cli=caps.tantivy_cli, translator=False,
        offload=caps.offload, plan_blob=caps.plan_blob,
    )
    corpus = run_e2e_corpus(
        program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, caps=degraded
    )
    assert corpus.status in ("pass", "warn"), corpus.message
    ops = run_e2e_ops(program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, caps=degraded)
    return {"ops": ops, "program_root": program_root, "platform_root": platform_root}


def test_e2e_ops_translator_step_is_blocked_without_lane_b(no_translator_chain):
    """The translator half records `blocked` with owner lane-b when the verb is
    absent, and the ROW says so -- a blocked gating check that reads MISSING
    would lose both its status and its owner."""
    result = no_translator_chain["ops"]
    assert result.status == "warn", result.message
    steps = _steps(result)
    assert steps["feed_translate_fail_closed"]["detail"]["blocked"] is True
    assert steps["feed_translate_fail_closed"]["detail"]["owner"] == "lane-b"
    store = _open_store(no_translator_chain["program_root"], no_translator_chain["platform_root"])
    try:
        rows = read_recorded_checks(store, run_id=RUN_ID)
    finally:
        store.close()
    assert rows["E-19"]["status"] == "blocked"
    assert rows["E-19"]["owner"] == "lane-b"


def test_e2e_ops_close_predicts_the_no_translator_reconciliation_shape(no_translator_chain):
    """The OTHER ``spawns_vs_bookings`` branch, driven to the close on a real
    corpus: with no handler-consumed booking there is no one-off mismatch, so
    the criterion is `pass` with an empty mismatch list -- not the `warn` the
    translator branch predicts. A `warn|fail` disjunction here would let either
    branch satisfy the other's criterion."""
    result = no_translator_chain["ops"]
    steps = _steps(result)
    assert [s["name"] for s in result.details["steps"]][-1] == "record_prior_checks"
    after = steps["doctor_green_after_close"]["detail"]
    assert after["translator_ran"] is False
    assert after["doctor_failed"] == 0
    assert after["spawns_vs_bookings"]["status"] == "pass"
    assert after["spawns_vs_bookings"]["mismatched_sessions"] == []
    assert after["spawns_vs_bookings"]["bad_launch_id_events"] == []
    store = _open_store(no_translator_chain["program_root"], no_translator_chain["platform_root"])
    try:
        rows = read_recorded_checks(store, run_id=RUN_ID)
    finally:
        store.close()
    assert rows["E-24"]["status"] == "pass"


def test_e2e_ops_booking_gate_refusal_leaves_every_launch_state_alone(ops_run, caps):
    """E-19(a) says "no launch row changed state", which a row COUNT cannot
    see: the snapshot is the whole {launch_id: state} mapping, compared across
    the unbooked translate job."""
    if not caps.translator:
        pytest.skip("this deployment has no feed translator; E-19's translator half records `blocked`")
    detail = _steps(ops_run)["feed_translate_fail_closed"]["detail"]
    assert detail["gate_launch_ledger_unchanged"] is True
    assert detail["gate_launch_rows_seen"] >= 1


def test_e2e_phase_fails_cleanly_when_there_is_no_open_session(tmp_path, caps):
    """A phase pointed at a program whose session is already closed must come
    back as a doctor-shaped `fail` with a recorded row -- never as an uncaught
    RuntimeError that leaves the CLI with no envelope to print at all."""
    program_root, platform_root = _init_bare_program(tmp_path, program_id="e2e-no-session")
    store = _open_store(program_root, platform_root)
    try:
        store.ops.execute("UPDATE session SET status='closed' WHERE status='open'")
        store.ops.commit()
    finally:
        store.close()

    ops = run_e2e_ops(program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps)
    assert ops.status == "fail", ops.message
    assert _failing_step(ops) == "open_scratch_session"
    assert "no OPEN session" in ops.message

    dashboard = run_e2e_dashboard(
        program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps
    )
    assert dashboard.status == "fail", dashboard.message
    assert _failing_step(dashboard) == "open_scratch_session"

    # and the failure is IN the record, against each journey's own fallback id
    store = _open_store(program_root, platform_root)
    try:
        rows = read_recorded_checks(store, run_id=RUN_ID)
    finally:
        store.close()
    assert rows["E-19"]["status"] == "fail"
    assert rows["E-17"]["status"] == "fail"


def test_e2e_phase_refuses_a_root_that_is_not_a_program(tmp_path, caps):
    """A mistyped ``--program-root`` fails BEFORE ``open_store`` gets to create
    and migrate a whole scaffold there."""
    mistyped = tmp_path / "porgram"
    result = run_e2e_ops(
        mistyped, tmp_path / "platform", run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps
    )
    assert result.status == "fail", result.message
    assert _failing_step(result) == "open_scratch_session"
    assert "not a program scaffold" in result.message
    assert not mistyped.exists(), "a mistyped root gained a scaffold"


@pytest.mark.parametrize("journey", [run_e2e_ops, run_e2e_dashboard])
def test_e2e_journeys_resolve_relative_roots(journey, tmp_path, caps, monkeypatch):
    """Every journey resolves its roots up front.

    The dashboard's `dashboard serve` subprocess is the one process this module
    starts with a cwd of its OWN (the package parent, so the console script
    cannot resolve a different checkout), while ``--program-root`` is passed
    through verbatim -- a relative root would make the SERVER open a different
    program than every other step in the same journey, and the failure would
    misdiagnose itself as an uninitialised corpus.
    """
    monkeypatch.chdir(tmp_path)
    result = journey(Path("porgram"), Path("platform"), run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps)
    assert result.status == "fail", result.message
    recorded_root = Path(result.details["program_root"])
    assert recorded_root.is_absolute()
    assert recorded_root == (tmp_path / "porgram").resolve()


def test_e2e_ops_fails_when_the_translator_answers_instead_of_refusing(tmp_path, caps):
    """BROKEN TWIN. Point the program at the ``pending`` translator backend --
    which parks an envelope and settles the job `complete` -- and the
    fail-closed criterion must FAIL. A translator that quietly succeeds is
    precisely what E-19 is there to catch."""
    if not caps.translator:
        pytest.skip("this deployment has no feed translator")
    program_root, platform_root = _init_bare_program(
        tmp_path, program_id="e2e-ops-broken", translator="pending"
    )
    result = run_e2e_ops(
        program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps
    )
    assert result.status == "fail", result.message
    assert _failing_step(result) == "feed_translate_fail_closed"
    assert "expected 'failed'" in result.message


def test_e2e_ops_law_stale_pin_negative_control(ops_run):
    detail = _steps(ops_run)["law_append_verify_with_stale_control"]["detail"]
    assert detail["pin_none_before"] is True
    assert detail["pin0"] != detail["pin1"]
    assert detail["verify_pin1"] == {"valid": True, "chain_ok": True, "pin_stale": False}
    assert detail["verify_pin0"]["valid"] is False
    assert detail["verify_pin0"]["pin_stale"] is True
    assert detail["verify_pin0"]["chain_ok"] is True


def test_e2e_ops_jobs_worker_runs_through_the_console_entry_point(ops_run):
    detail = _steps(ops_run)["jobs_worker_cli_once"]["detail"]
    assert "claimed" in detail["ledger_event_types"]
    assert "completed" in detail["ledger_event_types"]
    assert detail["index_job_state"] == "complete"
    assert ":" in detail["worker_id"]


def test_e2e_ops_events_export_is_byte_stable(ops_run):
    detail = _steps(ops_run)["events_tail_types"]["detail"]
    assert {"hook_alive", "subagent_return", E2E_CHECK_EVENT_TYPE} <= set(detail["types_seen"])
    assert set(detail["hooks_seen"]) >= {"session_start", "spawn_gate", "post_task"}
    assert len(detail["export_paths"]) == 2
    assert detail["export_sha256"]


def test_e2e_ops_close_needs_no_override_and_renders_handoff(ops_run, corpus_run, caps):
    close = _steps(ops_run)["session_close_and_handoff"]["detail"]
    handoff = Path(close["handoff_path"])
    resolved = handoff if handoff.is_absolute() else corpus_run["program_root"] / handoff
    assert resolved.is_file()
    after = _steps(ops_run)["doctor_green_after_close"]["detail"]
    assert after["doctor_failed"] == 0
    if caps.translator:
        assert after["spawns_vs_bookings"]["status"] == "warn"
        mismatched = after["spawns_vs_bookings"]["mismatched_sessions"]
        assert len(mismatched) == 1
        assert mismatched[0]["consumed_launch_count"] == mismatched[0]["subagent_return_count"] + 1
        assert after["spawns_vs_bookings"]["bad_launch_id_events"] == []
    else:
        assert after["spawns_vs_bookings"]["status"] == "pass"


def test_e2e_ops_copies_the_smoke_envelope_into_an_e2e_check_row(tmp_path, caps):
    """E-02 is the harness's own floor: the ops journey copies the smoke's
    envelope into the record rather than re-running it, and the row carries
    the 12 step names -- `ok:true` with fewer steps is the hollow pass that
    equality excludes."""
    from trialerror.accept.e2e import _smoke_evidence, _smoke_status

    envelope = {
        "ok": True,
        "result": {
            "summary": {"total": 1, "passed": 1, "failed": 0},
            "checks": [{
                "name": "clean_checkout_smoke", "status": "pass",
                "details": {"steps": [{"name": f"step{i}", "ok": True} for i in range(12)]},
            }],
        },
    }
    assert _smoke_status(envelope) == "pass"
    evidence = _smoke_evidence(envelope)
    assert evidence["step_count"] == 12
    assert evidence["all_steps_ok"] is True
    assert _smoke_status({"ok": False, "error": {"details": {"summary": {"failed": 1}}}}) == "fail"


# ---------------------------------------------------------------------------
# offload (E-50, E-52)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def offload_run(tmp_path_factory, caps):
    """The enqueue journey with the STUB worker's model identity and a small
    batch, so the whole round trip below stays inside a unit test's budget.
    The plan's own batch size (200) is what the sandbox runs; the mechanics
    under test here are identical at either size."""
    pytest.importorskip("trialerror.offload")
    if not caps.offload:
        pytest.skip("this deployment has no offload CLI group")
    from tests._offload_fixtures import STUB_DIMS, STUB_MODEL_KEY

    base = tmp_path_factory.mktemp("e2e-offload")
    program_root = base / "program-off"
    platform_root = base / "platform"
    result = run_e2e_offload_enqueue(
        program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, min_chunks=12,
        embed_model_key=STUB_MODEL_KEY, embed_dims=STUB_DIMS, caps=caps,
    )
    return {"result": result, "program_root": program_root, "platform_root": platform_root}


def test_e2e_offload_enqueue_produces_pending_manifest(offload_run):
    result = offload_run["result"]
    assert result.status == "pass", result.message
    steps = _steps(result)
    parked = steps["drain_until_embed_deferred"]["detail"]
    assert parked["job_state"] == "pending", "an absent GPU parks the job; it never fails it"
    assert parked["attempts"] == 0, "an absent GPU must cost the job no retry budget"
    assert parked["next_attempt_ts"]
    assert parked["deferred_events"] >= 1
    assert "retry_scheduled" not in parked["ledger_event_types"]
    assert "abandoned" not in parked["ledger_event_types"]
    manifest = steps["pending_manifest_present"]["detail"]
    assert Path(manifest["manifest_path"]).is_file()
    branch = manifest["branch_shape"]
    assert branch["offload_attempts"] == 0
    assert branch["has_expect_block"] is True
    assert branch["chunks_jsonl_matches_chunk_count"] is True


def test_e2e_offload_enqueue_fails_when_the_stage_ran_locally(tmp_path, caps, monkeypatch):
    """BROKEN TWIN. Write the scratch toml with LOCAL fake backends instead of
    the offload ones: the embed stage then completes in process, and E-50 must
    FAIL. A completed embed job is the one outcome an enqueue check must never
    read as success."""
    pytest.importorskip("trialerror.offload")
    if not caps.offload:
        pytest.skip("this deployment has no offload CLI group")
    real_write = e2e_mod._write_scratch_toml

    def _local_backends(program_root, **kwargs):
        kwargs.update({"ocr": "fake", "embed": "fake", "require_real_backends": False,
                       "embed_extra": None, "ocr_extra": None})
        return real_write(program_root, **kwargs)

    monkeypatch.setattr(e2e_mod, "_write_scratch_toml", _local_backends)
    result = run_e2e_offload_enqueue(
        tmp_path / "program-off", tmp_path / "platform", run_id=RUN_ID, repo_root=REPO_ROOT,
        min_chunks=8, caps=caps,
    )
    assert result.status == "fail", result.message
    assert _failing_step(result) == "drain_until_embed_deferred"
    assert "expected 'pending'" in result.message


def test_e2e_offload_roundtrip_passes_after_a_worker_publishes(offload_run, caps, tmp_path):
    """E-52's sandbox half, driven end to end with a LOCAL transport and the
    stub GPU backends the offload lane's own tests use: the worker claims,
    embeds, publishes; the sandbox reclaims, kicks, drains and verifies."""
    pytest.importorskip("trialerror.offload")
    from tests._offload_fixtures import StubDevBackends, StubEmbedBackend, STUB_DIMS, STUB_MODEL_KEY
    from trialerror.jobs.worker import run_loop
    from trialerror.offload import protocol
    from trialerror.offload.transport import LocalTransport
    from trialerror.offload.worker import run_worker

    assert offload_run["result"].status == "pass"
    program_root = offload_run["program_root"]
    platform_root = offload_run["platform_root"]
    job_id = offload_run["result"].details["job_id"]
    chunk_count = offload_run["result"].details["chunk_count"]

    summary = run_worker(
        transport=LocalTransport(protocol.offload_root(program_root)),
        backends=StubDevBackends(embed=StubEmbedBackend(model_key=STUB_MODEL_KEY, dims=STUB_DIMS)),
        work_root=tmp_path / "dev-work",
        worker_id="dev-under-test",
        max_polls=1,
        batch_size=8,
    )
    assert summary["published"] == [job_id], summary

    # the sandbox side, in the order the plan's E-52 block runs it: reclaim,
    # kick (which adopts the publish and clears the parked job's delay), then
    # the program's own worker loop -- the container's jobs window ticks the
    # LIVE program only, so an offload program is always drained explicitly.
    for verb in ("reclaim", "kick"):
        envelope = _cli([
            "--program-root", str(program_root), "--platform-root", str(platform_root), "offload", verb,
        ], platform_root=platform_root)
        assert envelope["ok"] is True, envelope
    store = _open_store(program_root, platform_root)
    try:
        run_loop(store, worker_id="e2e-verify-drain", poll_interval_s=0.01, max_idle_polls=3)
    finally:
        store.close()

    result = verify_e2e_offload_roundtrip(
        program_root, platform_root, run_id=RUN_ID, job_id=job_id, chunk_count=chunk_count,
        repo_root=REPO_ROOT, caps=caps,
    )
    assert result.status == "pass", result.message
    steps = _steps(result)
    assert steps["emb_rows_equal_chunks"]["detail"]["emb_rows"] == chunk_count
    assert steps["model_key_is_real"]["detail"]["model_keys"] == [STUB_MODEL_KEY]
    assert steps["dims_match"]["detail"]["dims"] == STUB_DIMS
    assert steps["document_indexed"]["detail"]["status"] == "indexed"
    assert set(steps["doctor_offload_checks"]["detail"].values()) == {"pass"}
    # E-52's "<= 30 min after the worker publishes" bound reports its OWN
    # status: it is measured from done/<job_id>/result.json, and the plan lists
    # a swept done/ as a legitimate branch shape. `elapsed_s: null` under an
    # unqualified pass would read as a bound that held rather than one that was
    # never evaluated.
    bound = steps["job_complete"]["detail"]["completion_bound"]
    assert bound["status"] in ("pass", "skip")
    assert bound["max_completion_s"] == 1800.0
    if steps["job_complete"]["detail"]["elapsed_s"] is None:
        assert bound["status"] == "skip"
        assert "swept" in bound["note"]
    else:
        assert bound["status"] == "pass"


def test_offload_verify_refuses_fake_model_key(tmp_path, caps, monkeypatch):
    """BROKEN TWIN for the verify journey, and the plan's own named case: emb
    rows written by a FAKE backend must fail ``model_key_is_real``. This is
    the assertion that stands between "the GPU ran" and "something wrote
    hash-derived numbers into a retrieval store"."""
    pytest.importorskip("trialerror.offload")
    if not caps.offload:
        pytest.skip("this deployment has no offload CLI group")
    real_write = e2e_mod._write_scratch_toml

    def _local_backends(program_root, **kwargs):
        kwargs.update({"ocr": "fake", "embed": "fake", "require_real_backends": False,
                       "embed_extra": None, "ocr_extra": None})
        return real_write(program_root, **kwargs)

    monkeypatch.setattr(e2e_mod, "_write_scratch_toml", _local_backends)
    program_root = tmp_path / "program-off"
    platform_root = tmp_path / "platform"
    broken = run_e2e_offload_enqueue(
        program_root, platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, min_chunks=8, caps=caps
    )
    # the enqueue journey failed (that is the previous test's point); what
    # matters here is that it left a COMPLETED embed job with fake vectors
    assert broken.status == "fail"
    store = _open_store(program_root, platform_root)
    try:
        row = store.jobs.execute("SELECT job_id, payload FROM job WHERE kind='embed'").fetchone()
        job_id = row["job_id"]
        doc_id = json.loads(row["payload"])["doc_id"]
        chunk_count = store.knowledge.execute(
            "SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        model_keys = [
            r["model_key"] for r in store.knowledge.execute("SELECT DISTINCT model_key FROM emb")
        ]
    finally:
        store.close()
    assert any(m.startswith("fake-") for m in model_keys), model_keys

    result = verify_e2e_offload_roundtrip(
        program_root, platform_root, run_id=RUN_ID, job_id=job_id, chunk_count=chunk_count,
        repo_root=REPO_ROOT, caps=caps,
    )
    assert result.status == "fail", result.message
    assert _failing_step(result) == "model_key_is_real"
    assert "FAKE model key" in result.message


def test_offload_journeys_are_blocked_not_passed_without_the_lane(tmp_path, caps):
    """Blocked is never a pass -- AND never a gap. Both journeys return early
    when the lane is absent, and both write their `blocked` row with its owner
    BEFORE they do: `--phase report` reads a missing row as `MISSING`, which
    would lose the status and the owner together."""
    degraded = Capabilities(
        tantivy_pkg=caps.tantivy_pkg, tantivy_cli=caps.tantivy_cli,
        translator=caps.translator, offload=False, plan_blob=caps.plan_blob,
    )
    record_root = tmp_path / "program"   # the FIRST scratch program: where the record lives
    platform_root = tmp_path / "platform"
    enqueue = run_e2e_offload_enqueue(
        tmp_path / "program-off", platform_root, run_id=RUN_ID, repo_root=REPO_ROOT,
        caps=degraded, record_program_root=record_root,
    )
    assert enqueue.status == "warn"
    assert _steps(enqueue)["offload_capability"]["detail"]["owner"] == "L0-C"
    assert _steps(enqueue)["record_blocked_row"]["detail"]["recorded"] is True

    verify = verify_e2e_offload_roundtrip(
        tmp_path / "program-off", platform_root, run_id=RUN_ID,
        job_id="JOB-nope", chunk_count=1, repo_root=REPO_ROOT, caps=degraded,
        record_program_root=record_root,
    )
    assert verify.status == "warn"
    assert _steps(verify)["record_blocked_row"]["detail"]["recorded"] is True

    store = _open_store(record_root, platform_root)
    try:
        rows = read_recorded_checks(store, run_id=RUN_ID)
    finally:
        store.close()
    for check_id in ("E-50", "E-52"):
        assert rows[check_id]["status"] == "blocked", check_id
        assert rows[check_id]["owner"] == "L0-C", check_id


def test_offload_evidence_rows_land_in_the_record_program(tmp_path, caps):
    """E-50/E-52 are recorded against the FIRST scratch program, not the
    offload one. `--phase report` and E-61's export read one program; a row
    left in the offload program reads as MISSING however well the phase went.
    """
    pytest.importorskip("trialerror.offload")
    if not caps.offload:
        pytest.skip("this deployment has no offload CLI group")
    from tests._offload_fixtures import STUB_DIMS, STUB_MODEL_KEY

    record_root = tmp_path / "program"
    platform_root = tmp_path / "platform"
    result = run_e2e_offload_enqueue(
        tmp_path / "program-off", platform_root, run_id=RUN_ID, repo_root=REPO_ROOT, min_chunks=8,
        embed_model_key=STUB_MODEL_KEY, embed_dims=STUB_DIMS, caps=caps,
        record_program_root=record_root,
    )
    assert result.status == "pass", result.message
    assert result.details["record_program_root"] == str(record_root.resolve())

    store = _open_store(record_root, platform_root)
    try:
        rows = read_recorded_checks(store, run_id=RUN_ID)
    finally:
        store.close()
    assert rows["E-50"]["status"] == "pass"
    # the offload session id is a LABEL, so it travels in the evidence rather
    # than on event.session_id (a foreign key into the OTHER program's session
    # table, which would refuse the row outright)
    session_step = _steps(result)["session_boot_cli_then_hook"]["detail"]
    assert rows["E-50"]["evidence"]["offload_session_id"] == session_step["session_id"]
    assert rows["E-50"]["evidence"]["offload_program_root"] == str((tmp_path / "program-off").resolve())

    # and nothing was recorded into the offload program itself
    off_store = _open_store(tmp_path / "program-off", platform_root)
    try:
        assert read_recorded_checks(off_store, run_id=RUN_ID) == {}
    finally:
        off_store.close()


# ---------------------------------------------------------------------------
# the CLI surface (Section 7.2)
# ---------------------------------------------------------------------------
def _cli(args: list[str], *, platform_root: Path | None = None) -> dict:
    env = dict(os.environ)
    if platform_root is not None:
        env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "trialerror.cli", *args],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=900,
    )
    return json.loads(proc.stdout)


def test_accept_cli_e2e_requires_explicit_roots():
    envelope = _cli(["accept", "--suite", "e2e", "--run-id", RUN_ID])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "e2e_requires_explicit_roots"


def test_accept_cli_e2e_run_id_must_be_shell_clock_shaped(tmp_path):
    envelope = _cli([
        "accept", "--suite", "e2e", "--run-id", "manual",
        "--program-root", str(tmp_path / "p"), "--platform-root", str(tmp_path / "pp"),
    ])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "e2e_run_id_malformed"
    assert RUN_ID_RE.match(RUN_ID)


def test_accept_cli_smoke_suite_is_unchanged_by_default(tmp_path):
    """The pre-existing front door keeps its behaviour: no --suite means the
    M15 clean-checkout smoke plus the GPU/live-CC enumeration.

    Not byte-for-byte -- ``result`` gains one ADDITIVE key, ``suite``, so the
    two suites name themselves the same way. Every key an existing caller
    already reads is unchanged, which is what this asserts: the shape is the
    old one PLUS that field, and nothing was renamed or removed.
    """
    envelope = _cli([
        "accept", "--program-root", str(tmp_path / "p"), "--platform-root", str(tmp_path / "pp"),
    ], platform_root=tmp_path / "pp")
    assert envelope["ok"] is True, envelope
    result = envelope["result"]
    assert set(result) == {"checks", "summary", "suite"}, "the smoke result grew or lost a key"
    assert result["suite"] == "smoke"
    assert set(result["summary"]) == {"total", "passed", "failed", "warned", "skipped"}
    names = [c["name"] for c in result["checks"]]
    assert names[0] == "clean_checkout_smoke"
    assert len(names) == 9  # the smoke + the 8 enumerated GPU/live-CC items


def test_accept_cli_e2e_phase_all_matches_direct_calls(tmp_path):
    # the smoke envelope a prior `trialerror accept` run produced: the ops
    # journey copies it into the E-02 row rather than re-running the smoke,
    # which is what lets the report's completeness rule ever be satisfied
    smoke_path = tmp_path / "smoke.json"
    smoke_path.write_text(json.dumps({
        "ok": True,
        "result": {
            "summary": {"total": 1, "passed": 1, "failed": 0},
            "checks": [{
                "name": "clean_checkout_smoke", "status": "pass",
                "details": {"steps": [{"name": f"step{i}", "ok": True} for i in range(12)]},
            }],
        },
    }), encoding="utf-8")

    envelope = _cli([
        "accept", "--suite", "e2e", "--phase", "all", "--run-id", RUN_ID,
        "--program-root", str(tmp_path / "program"), "--platform-root", str(tmp_path / "platform"),
        "--smoke-envelope", str(smoke_path),
    ])
    assert envelope["ok"] is True, envelope
    result = envelope["result"]
    journeys = [c["name"] for c in result["checks"] if c["category"] == "e2e"]
    assert journeys == ["e2e_corpus", "e2e_dashboard", "e2e_ops"]
    # the human steps ride along as skip entries, so one CLI run shows the
    # whole surface: what ran, and what is still waiting for a human
    operator = [c for c in result["checks"] if c["category"] == "e2e_operator"]
    assert {c["name"] for c in operator} == set(E2E_OPERATOR_ITEMS)
    assert {c["status"] for c in operator} == {"skip"}
    assert result["summary"]["failed"] == 0
    assert result["summary"]["skipped"] == len(E2E_OPERATOR_ITEMS)
    assert result["run_id"] == RUN_ID
    assert set(result["capabilities"]) == {"tantivy_pkg", "tantivy_cli", "translator", "offload", "plan_blob"}

    report = _cli([
        "accept", "--suite", "e2e", "--phase", "report", "--run-id", RUN_ID,
        "--program-root", str(tmp_path / "program"), "--platform-root", str(tmp_path / "platform"),
    ])
    assert report["ok"] is True, report
    rows = {r["check_id"]: r["status"] for r in report["result"]["rows"]}
    assert len(rows) == len(E2E_CHECK_CATALOGUE)
    for check_id in ("E-02", "E-10", "E-16", "E-17", "E-70", "E-24"):
        assert rows[check_id] in ("pass", "blocked"), (check_id, rows[check_id])
    # the human steps were not taken in this process, and the report says so
    assert rows["E-30"] == "MISSING"
    assert "E-30" in report["result"]["summary"]["missing"]


def test_accept_cli_refuses_an_unreadable_smoke_envelope(tmp_path):
    envelope = _cli([
        "accept", "--suite", "e2e", "--phase", "ops", "--run-id", RUN_ID,
        "--program-root", str(tmp_path / "p"), "--platform-root", str(tmp_path / "pp"),
        "--smoke-envelope", str(tmp_path / "does-not-exist.json"),
    ])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "smoke_envelope_unreadable"


def test_accept_cli_e2e_lists_operator_items_as_skip():
    """The enumeration the CLI appends to ``result.checks`` -- one skip entry
    per human step, carrying the instruction verbatim, so the two can never
    drift (the ``GPU_LIVE_CC_ITEMS`` pattern)."""
    enumerated = {r.name: r for r in e2e_operator_enumeration()}
    assert set(enumerated) == set(E2E_OPERATOR_ITEMS)
    for key, message in E2E_OPERATOR_ITEMS.items():
        assert enumerated[key].status == "skip"
        assert enumerated[key].category == "e2e_operator"
        assert enumerated[key].message == message


def test_accept_cli_offload_verify_needs_the_job_it_verifies(tmp_path):
    envelope = _cli([
        "accept", "--suite", "e2e", "--phase", "offload-verify", "--run-id", RUN_ID,
        "--program-root", str(tmp_path / "p"), "--platform-root", str(tmp_path / "pp"),
    ])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "e2e_offload_verify_needs_job"


# ---------------------------------------------------------------------------
# the evidence row and the enumeration guards (Section 7.3 / Section 2)
# ---------------------------------------------------------------------------
def test_e2e_check_event_payload_shape(tmp_path):
    from trialerror.accept.e2e import E2ERecorder
    from trialerror.stores.store import open_store

    store = open_store(tmp_path / "program", platform_root=tmp_path / "platform")
    try:
        recorder = E2ERecorder(run_id=RUN_ID, plan_blob="abc123")
        row = recorder.record(
            store, "E-10", "pass",
            evidence={"program_id": "e2e-x", "api_key": "sk-should-be-redacted"},
        )
        payload = json.loads(row["payload"])
        assert set(payload) == {"run_id", "check_id", "status", "by", "evidence", "plan_blob"}
        assert payload["run_id"] == RUN_ID
        assert payload["plan_blob"] == "abc123"
        assert payload["by"] == "journey"

        blocked = recorder.record(store, "E-13", "blocked", owner="lane-d", evidence={})
        assert json.loads(blocked["payload"])["owner"] == "lane-d"

        with pytest.raises(ValueError):
            recorder.record(store, "E-13", "green", evidence={})

        rows = read_recorded_checks(store, run_id=RUN_ID)
        assert set(rows) == {"E-10", "E-13"}
        assert read_recorded_checks(store, run_id="e2e-20990101T000000Z") == {}
    finally:
        store.close()


def test_every_automated_check_id_is_claimed_by_exactly_one_journey():
    """The sequence enumerator has to list every automated ``E-nn`` in the
    plan, and each has to have an owner: an id no journey records is an id the
    report phase will report MISSING for ever."""
    automated = automated_check_ids()
    assert len(automated) == len(set(automated))
    journeys = ("e2e_corpus", "e2e_dashboard", "e2e_ops", "e2e_offload_enqueue", "e2e_offload_roundtrip")
    claimed: list[str] = []
    for journey in journeys:
        claimed.extend(journey_check_ids(journey))
    assert sorted(claimed) == sorted(automated)
    assert len(claimed) == len(set(claimed)), "two journeys claim the same check id"
    # the plan's own automated set, spelled out so a silent renumbering fails here
    assert set(automated) == {
        "E-02", "E-10", "E-11", "E-12", "E-13", "E-14", "E-15", "E-16", "E-17", "E-70",
        "E-19", "E-20", "E-21", "E-22", "E-23", "E-24", "E-50", "E-52",
    }


def test_the_catalogue_covers_every_plan_id_in_phase_order():
    sequence = e2e_check_sequence()
    ids = [row["check_id"] for row in sequence]
    assert ids == [
        "E-00", "E-01", "E-02",
        "E-10", "E-11", "E-12", "E-13", "E-14", "E-15", "E-16",
        "E-17", "E-70", "E-18",
        "E-19", "E-20", "E-21", "E-22", "E-23", "E-24",
        "E-30", "E-31", "E-32", "E-33", "E-34", "E-35",
        "E-40", "E-41", "E-42",
        "E-50", "E-51", "E-52", "E-53",
        "E-60", "E-61",
    ]
    phases = [row["phase"] for row in sequence]
    assert phases == sorted(phases), "the catalogue is not in phase order"
    advisory = {row["check_id"] for row in sequence if not row["gating"]}
    assert advisory == {"E-34", "E-42", "E-53"}, "the plan's advisory set changed without a changelog line"


def test_every_operator_item_has_a_skip_marked_test():
    """The 1:1 guard: a new operator item that forgets its stand-in fails here
    instead of silently under-enumerating what ``trialerror accept`` reports."""
    import sys as _sys

    module = _sys.modules[__name__]
    for key in E2E_OPERATOR_ITEMS:
        fn = getattr(module, f"test_{key}", None)
        assert fn is not None, f"no test_{key} function defined in this module"
        marks = getattr(fn, "pytestmark", [])
        skip_marks = [m for m in marks if m.name == "skip"]
        assert skip_marks, f"test_{key} is not @pytest.mark.skip-marked"
        assert skip_marks[0].kwargs.get("reason") == E2E_OPERATOR_ITEMS[key]


def test_operator_items_speak_only_in_placeholders():
    """The human instructions this package ships are written against the
    plan's placeholder vocabulary, not against any real deployment: a
    ``<sandbox-host>``, a ``<container>``, a ``<deploy-root>`` and the shell
    variables the runbook defines. Deployment-specific NAMES are caught by
    the public-export gate, which is the tool that actually scans the exported
    tree -- spelling one out here to assert its absence would put it back in.
    """
    blob = "\n".join(E2E_OPERATOR_ITEMS.values())
    assert "<sandbox-host>" in blob
    assert "<container>" in blob
    assert "<deploy-root>" in blob
    for key, message in E2E_OPERATOR_ITEMS.items():
        # every instruction names its check id and phase up front, and says
        # how the step is judged -- an instruction with no criterion is a
        # human step nobody can score
        assert message.startswith("["), key
        assert "PASS" in message or "ADVISORY" in message, key


def test_e2e_journeys_never_touch_the_default_platform_root(tmp_path, monkeypatch, caps):
    """With ``TRIALERROR_PLATFORM_ROOT`` pointed at a sentinel directory and
    explicit roots passed, nothing may be created in the sentinel. Several CLI
    groups open their store off the environment rather than the flag, so this
    is the assertion that keeps a scratch run out of a real platform."""
    sentinel = tmp_path / "sentinel-platform"
    sentinel.mkdir()
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(sentinel))
    result = run_e2e_corpus(
        tmp_path / "program", tmp_path / "platform", run_id=RUN_ID, repo_root=REPO_ROOT, caps=caps
    )
    assert result.status in ("pass", "warn"), result.message
    assert list(sentinel.iterdir()) == [], f"the sentinel platform root was written to: {list(sentinel.iterdir())}"


# ---------------------------------------------------------------------------
# the operator / orchestrator stand-ins -- never attempted here, always listed
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e00_capability_probes_and_baseline"])
def test_e2e_e00_capability_probes_and_baseline():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e01_scratch_roots"])
def test_e2e_e01_scratch_roots():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e18_live_dashboard_untouched"])
def test_e2e_e18_live_dashboard_untouched():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e30_start_live_session"])
def test_e2e_e30_start_live_session():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e31_live_session_start_hook"])
def test_e2e_e31_live_session_start_hook():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e32_live_spawn_gate_refusal"])
def test_e2e_e32_live_spawn_gate_refusal():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e33_live_booked_spawn_and_return"])
def test_e2e_e33_live_booked_spawn_and_return():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e34_live_stop_hook"])
def test_e2e_e34_live_stop_hook():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e35_close_live_session_and_leak_check"])
def test_e2e_e35_close_live_session_and_leak_check():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e40_phone_sees_the_session"])
def test_e2e_e40_phone_sees_the_session():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e41_phone_command_lands"])
def test_e2e_e41_phone_command_lands():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e42_simultaneous_viewers"])
def test_e2e_e42_simultaneous_viewers():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e51_dev_off_window"])
def test_e2e_e51_dev_off_window():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e52_dev_worker_publishes_then_verify"])
def test_e2e_e52_dev_worker_publishes_then_verify():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e53_ocr_round_trip"])
def test_e2e_e53_ocr_round_trip():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e60_cleanup_and_leak_check"])
def test_e2e_e60_cleanup_and_leak_check():
    ...


@pytest.mark.skip(reason=E2E_OPERATOR_ITEMS["e2e_e61_run_record"])
def test_e2e_e61_run_record():
    ...
