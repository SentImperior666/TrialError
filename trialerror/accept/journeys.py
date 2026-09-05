"""The M15 acceptance journeys themselves. Design Section 12 (M15 row) +
this build's brief: "clean-checkout smoke (fresh venv -> pip install -e ->
migrate stores -> boot session -> book launch -> spawn-gate refusal/
consumption round trip -> ingest a fixture doc (fake backends) -> search
with citation -> citecheck it -> close session w/ handoff render)" plus the
design row's own "-> gate -> close-refusal ->" steps folded in.

**Scope note on "fresh venv -> pip install -e":** M0's own acceptance
criterion already proves "pip install -e . on Win" against the CURRENT
environment (``tests/test_m0_acceptance.py::test_editable_install_is_importable``).
This module's :func:`run_clean_checkout_smoke` starts from "migrate
stores" (an already-installed ``trialerror``, exactly the CI-job shape: install
once, then run the acceptance suite) -- the literal fresh-venv-creation
step is its OWN separate, narrower proof
(``tests/acceptance/test_clean_checkout_smoke.py::test_fresh_venv_pip_install_dash_e_smoke``),
kept out of this function so a genuine venv-creation/pip-install failure
(a real environment issue) doesn't also mask every other step's own
signal, and so this function stays fast enough to run from ``trialerror
accept`` interactively.

**Real subprocess vs. direct API calls, deliberately:** every OTHER
module's own ``test_m*_acceptance.py`` proves cross-module correctness via
DIRECT calls into the landed Python API (never subprocess) -- that is
this codebase's established acceptance-test convention, and re-deriving
every CLI's argv shape here would both duplicate that coverage and add a
large, low-value error surface. This journey follows the SAME convention
for the business-logic steps, with two deliberate exceptions where the
thing actually being proven IS the subprocess boundary itself: the
``SessionStart``/``PreToolUse:Task`` HOOK SCRIPTS (``plugin/hooks/*.py``),
which are non-negotiably real subprocesses in production (Claude Code
invokes them that way) and have no direct-call equivalent worth trusting
instead -- mirrors ``tests/test_spawn_gate_hook.py``'s own "real subprocess,
real stdin JSON, real exit code" rationale, applied at harness scope
rather than unit scope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from trialerror.util.doctor import CheckResult, DoctorContext, discover_and_register_checks, run_checks
from trialerror.util.timeutil import now

__all__ = [
    "AcceptanceStepError",
    "GPU_LIVE_CC_ITEMS",
    "run_clean_checkout_smoke",
    "gpu_and_live_cc_enumeration",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SESSION_START_HOOK = _REPO_ROOT / "plugin" / "hooks" / "session_start.py"
_SPAWN_GATE_HOOK = _REPO_ROOT / "plugin" / "hooks" / "spawn_gate.py"

#: the acceptance-harness sentence a fixture pdf embeds, and later the
#: exact text a `[[cite:ANC-...]]` marker in the citecheck step binds to --
#: 9 words, well over the mechanical pass's 6-word-shingle floor, and
#: distinctive enough to never collide with another test's fixture text.
_CITECHECK_SENTENCE = "TrialError acceptance harness smoke test sentence for citation checking."


class AcceptanceStepError(RuntimeError):
    """Raised (and always caught at the top of :func:`run_clean_checkout_smoke`)
    when one named step fails -- carries the step name so the returned
    :class:`~trialerror.util.doctor.CheckResult` can say exactly which leg of
    the smoke broke, not just that something did."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


@contextmanager
def _step(steps: list[dict[str, Any]], name: str):
    """Run one named step; on ANY exception, record it in ``steps`` and
    re-raise as :class:`AcceptanceStepError` (uniform failure shape for
    every step, whether the underlying error was a landed-API refusal, an
    assertion this module makes about the result, or a subprocess/IO
    problem)."""
    try:
        yield
    except AcceptanceStepError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately broad: this IS the harness's catch-all
        steps.append({"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        raise AcceptanceStepError(name, str(exc)) from exc


def _run_hook_script(script: Path, payload: dict[str, Any], *, platform_root: Path, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Real subprocess invocation of a Claude Code hook script -- same
    shape ``tests/test_spawn_gate_hook.py::_run_hook``/
    ``tests/test_session_hooks.py`` use: stdin JSON, ``TRIALERROR_PLATFORM_ROOT``
    env-scoped so this never touches a real developer's ``~/.trialerror``."""
    env = dict(os.environ)
    env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def run_clean_checkout_smoke(program_root: Path, platform_root: Path, *, repo_root: Path | None = None) -> CheckResult:
    """The design's own M15-row smoke, end to end, against a caller-supplied
    (normally fresh/tmp) ``program_root``/``platform_root`` pair. Returns a
    single :class:`~trialerror.util.doctor.CheckResult` (``name="clean_checkout_smoke"``)
    whose ``details["steps"]`` names every leg attempted, in order, with a
    per-step ok/detail (or ok/error) record -- the FIRST failing step stops
    the run there (later steps depend on earlier state; there is no
    meaningful way to "keep going" past e.g. a failed boot)."""
    repo_root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    program_root = Path(program_root)
    platform_root = Path(platform_root)
    program_root.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, Any]] = []
    store = None

    try:
        # ------------------------------------------------------------
        # 1. migrate stores -- open_store() creates+migrates all four DBs;
        #    there is no separate "trialerror program init" (design Section 3.2
        #    names it, but no lane shipped that CLI group -- see this
        #    build's report for the discovered-gap note) so this step
        #    itself IS the program-scaffold bootstrap for a fresh root.
        # ------------------------------------------------------------
        with _step(steps, "migrate_stores"):
            from trialerror.stores.store import open_store

            store = open_store(program_root, platform_root=platform_root)
        steps.append({"name": "migrate_stores", "ok": True, "detail": {"program_root": str(program_root)}})

        # ------------------------------------------------------------
        # 2. boot session -- via the REAL SessionStart hook subprocess
        #    (plugin/hooks/session_start.py), which both boots (idempotent,
        #    reuse_open=True) AND records the hook_alive event close's own
        #    refusal ladder checks for later.
        #
        #    The hook script's own payload has no --create-account/
        #    --account equivalent (design F14: `boot_session` never
        #    guesses across >1 registered account, and account CRUD has NO
        #    CLI owner at all -- see trialerror.sessions.lifecycle.resolve_
        #    account_for_boot's own TRIALERROR-DEV-NOTE). A brand-new program
        #    scaffold genuinely has zero accounts, so this smoke pre-seeds
        #    exactly one (the same convention tests/test_session_hooks.py
        #    itself uses) -- with exactly one account registered, `boot_
        #    session(store, reuse_open=True)` auto-resolves to it with no
        #    ambiguity, matching how a real first-ever `trialerror session boot
        #    --create-account "<label>"` (the CLI's own bootstrap
        #    convenience, run by a human once) leaves the program for
        #    every SUBSEQUENT SessionStart hook fire to reuse.
        # ------------------------------------------------------------
        with _step(steps, "boot_session_via_session_start_hook"):
            from trialerror.stores import insert
            from trialerror.util.ids import new_id

            seed_account_id = new_id("ACC")
            insert(store, "account", {"account_id": seed_account_id, "label": "acceptance-harness", "created_ts": now()})

            boot_proc = _run_hook_script(
                _SESSION_START_HOOK,
                {"session_id": "accept-smoke", "cwd": str(program_root), "hook_event_name": "SessionStart", "source": "startup"},
                platform_root=platform_root,
            )
            if boot_proc.returncode != 0:
                raise RuntimeError(f"session_start.py exited {boot_proc.returncode}; stderr={boot_proc.stderr!r}")
            session_row = store.ops.execute("SELECT * FROM session WHERE status='open'").fetchone()
            if session_row is None:
                raise RuntimeError(f"session_start.py ran (stderr={boot_proc.stderr!r}) but no OPEN session exists")
            session_id = session_row["session_id"]
            account_id = session_row["account_id"]
            hook_alive_count = store.ops.execute(
                "SELECT COUNT(*) FROM event WHERE type='hook_alive' AND session_id=?", (session_id,)
            ).fetchone()[0]
            if hook_alive_count < 1:
                raise RuntimeError("session_start.py ran but recorded zero hook_alive events")
        steps.append({
            "name": "boot_session_via_session_start_hook", "ok": True,
            "detail": {"session_id": session_id, "account_id": account_id, "hook_stdout_nonempty": bool(boot_proc.stdout.strip())},
        })

        # ------------------------------------------------------------
        # 3. book launch -- needs a pool first (create_pool), then
        #    book_launch (design Section 5.2: "book returns launch_id
        #    token for the spawn gate").
        # ------------------------------------------------------------
        with _step(steps, "book_launch"):
            from trialerror.budget.pools import book_launch, create_pool

            create_pool(store, account_id=account_id, model_class="top", period="weekly", cap_tokens=1_000_000)
            book_result = book_launch(
                store, session_id=session_id, program_id="PROG-accept", agent_kind="acceptance-harness",
                model_class="top", model="sonnet", purpose="mechanical", est_tokens=500,
            )
            if not book_result.ok:
                raise RuntimeError(f"book_launch not PROVISIONAL (state={book_result.state}): {book_result.reason}")
            launch_id = book_result.launch_id
        steps.append({"name": "book_launch", "ok": True, "detail": {"launch_id": launch_id}})

        # ------------------------------------------------------------
        # 4. spawn-gate refusal/consumption round trip -- via the REAL
        #    PreToolUse hook subprocess (plugin/hooks/spawn_gate.py):
        #    (a) a Task prompt with no launch_id token -> exit 2 refused;
        #    (b) the SAME booked launch_id, spawned as the tool name Claude
        #        Code 2.1.x actually emits ("Agent" -- FU-11, 2026-09-05) ->
        #        exit 0, consumes the booking (PROVISIONAL -> RUNNING);
        #    (c) replaying the identical token again (as the legacy "Task"
        #        alias, still accepted) -> exit 2 refused (design Section
        #        12 M3 row: "same launch_id token on a second spawn
        #        refused"). Using both names across (a)/(b)/(c) means this
        #        shipped, run-in-the-field smoke journey exercises the live
        #        tool name Claude Code emits, not only the dead "Task"
        #        alias (FU-11 verification finding F2).
        # ------------------------------------------------------------
        with _step(steps, "spawn_gate_refusal_no_token"):
            no_token = _run_hook_script(
                _SPAWN_GATE_HOOK,
                {"hook_event_name": "PreToolUse", "tool_name": "Task",
                 "tool_input": {"prompt": "a subagent prompt with no launch_id token at all"},
                 "cwd": str(program_root)},
                platform_root=platform_root,
            )
            if no_token.returncode != 2:
                raise RuntimeError(f"expected exit 2 (refused), got {no_token.returncode}; stderr={no_token.stderr!r}")
        steps.append({"name": "spawn_gate_refusal_no_token", "ok": True, "detail": {"exit_code": no_token.returncode}})

        with _step(steps, "spawn_gate_consumption"):
            consumed = _run_hook_script(
                _SPAWN_GATE_HOOK,
                {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                 "tool_input": {"prompt": f"launch_id: {launch_id}\ndo the acceptance-smoke work"},
                 "cwd": str(program_root)},
                platform_root=platform_root,
            )
            if consumed.returncode != 0:
                raise RuntimeError(f"expected exit 0 (consumed), got {consumed.returncode}; stderr={consumed.stderr!r}")
            row = store.platform.execute("SELECT state FROM launch WHERE launch_id=?", (launch_id,)).fetchone()
            if row is None or row["state"] != "RUNNING":
                raise RuntimeError(f"launch {launch_id} did not move to RUNNING after a consumed spawn (row={dict(row) if row else None})")
        steps.append({"name": "spawn_gate_consumption", "ok": True, "detail": {"exit_code": consumed.returncode}})

        with _step(steps, "spawn_gate_replay_refused"):
            replay = _run_hook_script(
                _SPAWN_GATE_HOOK,
                {"hook_event_name": "PreToolUse", "tool_name": "Task",
                 "tool_input": {"prompt": f"launch_id: {launch_id}\ntry to reuse the same token"},
                 "cwd": str(program_root)},
                platform_root=platform_root,
            )
            if replay.returncode != 2:
                raise RuntimeError(f"expected exit 2 (already consumed), got {replay.returncode}; stderr={replay.stderr!r}")
        steps.append({"name": "spawn_gate_replay_refused", "ok": True, "detail": {"exit_code": replay.returncode}})

        # ------------------------------------------------------------
        # 5. ingest a fixture doc (fake backends -- the default when no
        #    trialerror.toml [ingest.ocr]/[ingest.embed] backend is configured)
        #    -- one open-license source (for the citation step) and one
        #    commercial_restricted source (for the serving-path fence
        #    step), each driven to 'indexed' via the SAME drain-loop
        #    pattern tests/test_m7_acceptance.py establishes.
        # ------------------------------------------------------------
        with _step(steps, "ingest_fixture_docs"):
            from trialerror.ingest import pipeline
            from trialerror.jobs.registry import discover_and_register_handlers
            from trialerror.jobs.worker import run_one

            discover_and_register_handlers()
            raw_dir = program_root / "raw"
            raw_dir.mkdir(exist_ok=True)

            open_source = pipeline.register_source(
                store, kind="paper", title="Acceptance Fixture (open)", license_tier="open",
                acquisition_route="user_delivered", registered_by_launch=launch_id, config={},
            )
            open_pdf = raw_dir / "accept_open.pdf"
            _write_pdf_text_fixture(open_pdf, [_CITECHECK_SENTENCE, "Second page filler text for the acceptance fixture document."])
            open_add = pipeline.add_document(
                store, program_root=program_root, source_id=open_source["source_id"], raw_path=open_pdf,
                created_by_launch=launch_id, config={}, yes=True,
            )
            open_doc_id = open_add["document"]["doc_id"]

            restricted_source = pipeline.register_source(
                store, kind="rulebook", title="Acceptance Fixture (commercial_restricted)", license_tier="commercial_restricted",
                acquisition_route="user_delivered", registered_by_launch=launch_id, config={},
            )
            restricted_text = "This commercial restricted rulebook fixture describes proprietary game mechanics. " + " ".join(
                f"secretword{i}" for i in range(60)
            )
            restricted_pdf = raw_dir / "accept_restricted.pdf"
            _write_pdf_text_fixture(restricted_pdf, [restricted_text])
            restricted_add = pipeline.add_document(
                store, program_root=program_root, source_id=restricted_source["source_id"], raw_path=restricted_pdf,
                created_by_launch=launch_id, config={}, yes=True,
            )
            restricted_doc_id = restricted_add["document"]["doc_id"]

            for _i in range(60):
                result = run_one(store, worker_id=f"accept-worker-{_i}")
                if result["status"] == "idle":
                    break

            def _doc_status(doc_id: str) -> str:
                row = store.knowledge.execute("SELECT status FROM document WHERE doc_id=?", (doc_id,)).fetchone()
                return row["status"] if row else "MISSING"

            open_status = _doc_status(open_doc_id)
            restricted_status = _doc_status(restricted_doc_id)
            if open_status != "indexed" or restricted_status != "indexed":
                raise RuntimeError(f"pipeline did not reach 'indexed' for both docs: open={open_status!r} restricted={restricted_status!r}")
        steps.append({
            "name": "ingest_fixture_docs", "ok": True,
            "detail": {"open_doc_id": open_doc_id, "restricted_doc_id": restricted_doc_id},
        })

        # ------------------------------------------------------------
        # 6. search with citation (fence incl.) -- the open-license doc's
        #    result must carry a non-null citation and fenced:false; the
        #    commercial_restricted doc's result must come back fenced:true
        #    with no verbatim run over 20 words (design Section 7).
        # ------------------------------------------------------------
        with _step(steps, "search_with_citation_and_fence"):
            from trialerror.retrieve import engine

            open_results = engine.search(store, query=_CITECHECK_SENTENCE, k=5, filters={"source_ids": [open_source["source_id"]]})
            if not open_results["results"]:
                raise RuntimeError("open-license search returned zero results")
            open_row = open_results["results"][0]
            if open_row.get("fenced") is not False:
                raise RuntimeError(f"open-license result unexpectedly fenced: {open_row!r}")
            citation = open_row.get("citation")
            if not citation or not citation.get("anchor") or not citation["anchor"].get("anchor_id"):
                raise RuntimeError(f"open-license result missing a citation/anchor block: {open_row!r}")
            anchor_id = citation["anchor"]["anchor_id"]

            restricted_results = engine.search(
                store, query="commercial restricted rulebook fixture", k=5, filters={"source_ids": [restricted_source["source_id"]]}
            )
            if not restricted_results["results"]:
                raise RuntimeError("commercial_restricted search returned zero results")
            restricted_row = restricted_results["results"][0]
            if restricted_row.get("fenced") is not True:
                raise RuntimeError(f"commercial_restricted result NOT fenced (F3 violation): {restricted_row!r}")
            quote = (restricted_row.get("citation") or {}).get("quote") or ""
            if len(quote.split()) > 20:
                raise RuntimeError(f"fenced quote exceeds 20 words ({len(quote.split())}): {quote!r}")
            if restricted_text in (restricted_row.get("text") or ""):
                raise RuntimeError("fenced result text contains the full raw restricted passage verbatim")
        steps.append({
            "name": "search_with_citation_and_fence", "ok": True,
            "detail": {"open_anchor_id": anchor_id, "restricted_fenced": True, "restricted_quote_words": len(quote.split())},
        })

        # ------------------------------------------------------------
        # 7. citecheck it -- a subject markdown string embedding the exact
        #    ingested sentence plus a [[cite:<anchor_id>]] marker bound to
        #    the anchor the search step just returned. The 6-word-shingle
        #    mechanical pass fires because the marker's sentence IS (a
        #    substring of) the anchor's own quote_text.
        # ------------------------------------------------------------
        with _step(steps, "citecheck"):
            from trialerror.verify.citecheck import run_citecheck

            subject_text = f"{_CITECHECK_SENTENCE} [[cite:{anchor_id}]]"
            cc_result = run_citecheck(store, subject_id="ACCEPT-citecheck", text=subject_text, issued_by_launch=launch_id)
            if cc_result["failures"]:
                raise RuntimeError(f"citecheck reported failures: {cc_result['failures']!r}")
            statuses = [p["status"] for p in cc_result["pairs"]]
            if statuses != ["mechanical_pass"]:
                raise RuntimeError(f"expected exactly one mechanical_pass pair, got statuses={statuses!r}")
        steps.append({"name": "citecheck", "ok": True, "detail": {"pair_statuses": statuses}})

        # ------------------------------------------------------------
        # 8. gate -- a gated-template artifact through the full state
        #    machine: draft -> submitted -> gated -> union_applied ->
        #    registered (register_artifact is the entry point that
        #    performs that last transition, design Section 4.2).
        # ------------------------------------------------------------
        with _step(steps, "gate_journey"):
            from trialerror.artifacts.gates import apply_union, open_gate, record_verdict, submit_gate
            from trialerror.artifacts.registry import create_artifact, register_artifact
            from trialerror.stores import get as store_get
            from trialerror.stores import insert

            insert(store, "template", {
                "type_key": "accept-keystone", "title": "Acceptance Keystone", "version": "1",
                "path": "templates/accept-keystone.md", "gated": 1,
            })
            artifact = create_artifact(
                store, type_key="accept-keystone", title="Acceptance smoke keystone",
                path="artifacts/accept-keystone.md", sha256="0" * 64, by_launch=launch_id,
            )
            gate = open_gate(store, artifact_id=artifact["artifact_id"])
            submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
            record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", critic_launch=launch_id)
            apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)
            register_artifact(store, artifact_id=artifact["artifact_id"], by_launch=launch_id)
            final_artifact = store_get(store, "artifact", pk_column="artifact_id", pk_value=artifact["artifact_id"])
            if final_artifact["status"] != "registered":
                raise RuntimeError(f"artifact did not reach 'registered' (status={final_artifact['status']!r})")
        steps.append({"name": "gate_journey", "ok": True, "detail": {"artifact_id": artifact["artifact_id"], "gate_id": gate["gate_id"]}})

        # ------------------------------------------------------------
        # 9. close-refusal -> reconcile -> close w/ handoff render --
        #    the RUNNING launch from step 4 is still dangling at this
        #    point, so the FIRST close attempt must refuse; only after
        #    reconciling does close succeed and render a handoff file.
        # ------------------------------------------------------------
        with _step(steps, "close_refusal_then_close"):
            from trialerror.budget.pools import reconcile_launch
            from trialerror.sessions.lifecycle import close_session

            course_check = {"rungs": "acceptance smoke", "build_vs_theory": "build", "drift_flag": False}
            refused = close_session(store, course_check=course_check, session_id=session_id, notes="pre-reconcile close attempt")
            if refused.ok or refused.code != "dangling_launches":
                raise RuntimeError(f"expected a dangling_launches refusal, got ok={refused.ok} code={refused.code!r}")

            reconcile_launch(store, launch_id=launch_id, actual_tokens=450)

            closed = close_session(store, course_check=course_check, session_id=session_id, notes="acceptance smoke close")
            if not closed.ok:
                raise RuntimeError(f"close refused after reconciling: code={closed.code!r} message={closed.message!r}")
            if not closed.handoff_path or not (program_root / closed.handoff_path).is_file():
                # handoff_path may already be absolute or program-relative -- accept either.
                handoff_file = Path(closed.handoff_path) if closed.handoff_path else None
                if handoff_file is None or not (handoff_file if handoff_file.is_absolute() else program_root / handoff_file).is_file():
                    raise RuntimeError(f"close succeeded but no handoff file found at {closed.handoff_path!r}")
        steps.append({"name": "close_refusal_then_close", "ok": True, "detail": {"handoff_path": closed.handoff_path}})

        # ------------------------------------------------------------
        # 10. doctor green -- every registered check, over this same
        #     program, must report no 'fail' (warn is tolerated -- e.g. a
        #     benign anchors_dangling warn is not a structural break).
        # ------------------------------------------------------------
        with _step(steps, "doctor_green"):
            discover_and_register_checks()
            # fix-accept (C-0064): thread this journey's own platform_root
            # through so platform-scoped checks (xid_dangling, the
            # store_schema_version "platform" DB kind, ...) resolve THIS
            # journey's platform.db, not whatever TRIALERROR_PLATFORM_ROOT/
            # ~/.trialerror happens to be in the caller's shell -- previously
            # DoctorContext had no platform_root at all here, so on a real
            # machine with an existing ~/.trialerror/platform.db, xid_dangling
            # false-positived even though every other step used THIS
            # platform_root correctly.
            ctx = DoctorContext(repo_root=repo_root, program_root=program_root, platform_root=platform_root)
            results = run_checks(ctx)
            failed = [r for r in results if r.status == "fail"]
            if failed:
                raise RuntimeError(f"{len(failed)} doctor check(s) failed: {[r.name for r in failed]}")
        steps.append({
            "name": "doctor_green", "ok": True,
            "detail": {"total": len(results), "warned": len([r for r in results if r.status == "warn"])},
        })

    except AcceptanceStepError as exc:
        return CheckResult(
            name="clean_checkout_smoke", category="acceptance", status="fail",
            message=f"step {exc.step!r} failed: {exc.detail}",
            details={"steps": steps, "program_root": str(program_root)},
        )
    finally:
        if store is not None:
            store.close()

    return CheckResult(
        name="clean_checkout_smoke", category="acceptance", status="pass",
        message=f"all {len(steps)} step(s) passed",
        details={"steps": steps, "program_root": str(program_root)},
    )


def _write_pdf_text_fixture(path: Path, pages_text: list[str]) -> Path:
    """A minimal, hand-built, spec-valid multi-page PDF with a real
    extractable text layer -- the exact construction
    ``tests/_ingest_fixtures.py::build_minimal_pdf`` uses, duplicated here
    (rather than imported from ``tests/``) so ``trialerror.accept`` -- shipped,
    non-test package code -- never imports from the test tree."""
    n_pages = len(pages_text)
    catalog_id = 1
    pages_id = 2
    page_ids = [3 + i for i in range(n_pages)]
    font_id = 3 + n_pages
    content_ids = [font_id + 1 + i for i in range(n_pages)]

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objs: list[tuple[int, str]] = []
    objs.append((catalog_id, f"<< /Type /Catalog /Pages {pages_id} 0 R >>"))
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs.append((pages_id, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>"))
    for i, pid in enumerate(page_ids):
        objs.append((
            pid,
            f"<< /Type /Page /Parent {pages_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/MediaBox [0 0 300 400] /Contents {content_ids[i]} 0 R >>",
        ))
    objs.append((font_id, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    for i, cid in enumerate(content_ids):
        stream = f"BT /F1 12 Tf 20 350 Td ({esc(pages_text[i])}) Tj ET"
        objs.append((cid, f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))

    out = ["%PDF-1.4\n"]
    offsets: dict[int, int] = {}
    pos = len(out[0].encode("latin-1"))
    for obj_id, body in sorted(objs):
        piece = f"{obj_id} 0 obj\n{body}\nendobj\n"
        offsets[obj_id] = pos
        out.append(piece)
        pos += len(piece.encode("latin-1"))

    xref_pos = pos
    max_id = max(offsets)
    xref_lines = [f"xref\n0 {max_id + 1}\n", "0000000000 65535 f \n"]
    for obj_id in range(1, max_id + 1):
        off = offsets.get(obj_id, 0)
        xref_lines.append(f"{off:010d} 00000 n \n")
    out.append("".join(xref_lines))
    out.append(f"trailer\n<< /Size {max_id + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF")

    path.write_bytes("".join(out).encode("latin-1"))
    return path


#: LIVE Claude Code / GPU-hardware journeys named by design Section 12 (M3,
#: M6, M7, M8, M14 rows) + Section 13 flag F18 ("live-CC hook tests are
#: orchestrator-executed integration items"). ENUMERATED here, once, as the
#: single source of truth both `trialerror accept`'s doctor-shaped summary and
#: tests/acceptance/test_gpu_and_live_cc_journeys.py's skip-marked pytest
#: stand-ins read from -- this build does NOT attempt to run any of these
#: (per this build's own binding instructions); every message below names
#: the exact command/step an orchestrator with the real hardware/session
#: runs to actually discharge it.
GPU_LIVE_CC_ITEMS: dict[str, str] = {
    "live_cc_session_start_round_trip": (
        "LIVE Claude Code SessionStart round trip (design Section 5.4 SessionStart row; "
        "M6/F18): install this plugin in a real Claude Code session on a TrialError-scaffolded "
        "program, start/resume/`\\clear`/`\\compact` the session, and confirm the boot bundle "
        "(pin status, dangling launches, inbox count, budget headroom, L0 memory index) "
        "actually appears as injected context, not just that `session_start.py` exits 0. "
        "Offline proxy already covered: tests/test_session_hooks.py + "
        "tests/test_m6_acceptance.py::test_session_start_injects_bundle_orchestrator_executed_note."
    ),
    "live_cc_spawn_gate_pretooluse_task": (
        "LIVE PreToolUse spawn-gate firing on a REAL subagent spawn (design Section 5.4 "
        "PreToolUse:Task row; M3/F18; tool name updated per FU-11, 2026-09-05): from a live "
        "Claude Code session with the plugin's hooks wired, get the agent to spawn a subagent "
        "-- Claude Code 2.1.x invokes this as the `Agent` tool (`Task` is only a legacy alias "
        "name that `hooks.json`'s matcher and `trialerror.hooks.SUBAGENT_TOOL_NAMES` still "
        "accept, not something current Claude Code emits) -- without a booked `launch_id:` "
        "token, and confirm Claude Code itself surfaces the exit-2 refusal message to the "
        "agent (not just that the script would exit 2 under a synthetic stdin payload). Then, "
        "in the SAME session, confirm a `hook_alive` row with `payload.hook == \"spawn_gate\"` "
        "was actually written for that call (`trialerror events tail`/a store query) -- the "
        "spawn's outcome alone does not distinguish the manifest matcher firing-and-allowing "
        "from the matcher never firing at all (FU-11 verification finding FU11-V4). Offline "
        "proxy already covered: tests/test_spawn_gate_hook.py (real subprocess, real stdin, "
        "real exit code, `Agent`-tool-name cases included) + this build's own "
        "run_clean_checkout_smoke (spawn_gate_refusal_no_token/spawn_gate_consumption/"
        "spawn_gate_replay_refused steps, the middle one now spawned as `Agent`)."
    ),
    "live_cc_stop_hook_close_check": (
        "LIVE Stop-hook close check (design Section 5.4 Stop row; M6/F18): in a real Claude "
        "Code session, leave a launch dangling or the digest stale, then invoke `/stop` (or "
        "let the session end) and confirm Claude Code's own Stop-hook protocol actually blocks "
        "once with the checklist message (and allows a second stop, never trapping the user) -- "
        "this is Claude Code UI behavior a synthetic stdin payload cannot exercise."
    ),
    "live_cc_task_or_agent_matcher_wiring": (
        "PreToolUse/PostToolUse matcher wiring verification in plugin/hooks/hooks.json (design "
        "Section 12 M6 row; corrected per FU-11, 2026-09-05 -- see FU11-V1): confirm, in a real "
        "Claude Code session, that the hook fires for `tool_name` in `{Task, Agent}` -- Claude "
        "Code 2.1.x's real subagent-spawn tool name is `Agent`; `Task` is a legacy alias -- and "
        "never for e.g. Bash/Read. This item used to say the matcher should fire ONLY for "
        "`tool_name==\"Task\"`; that criterion was itself wrong (a correctly-fixed matcher fires "
        "on `Agent`, which the old wording would have read as a wiring FAILURE) and is fixed "
        "here. i.e. confirm the plugin manifest's own `^(Task|Agent)$` matcher config is what "
        "Claude Code is actually applying, not merely that spawn_gate.py's/post_task.py's own "
        "internal `tool_name not in SUBAGENT_TOOL_NAMES` fast-path defends itself (which "
        "tests/test_spawn_gate_hook.py::test_hook_passes_through_non_task_tools and "
        "tests/test_cli_hook.py::test_hooks_json_pretooluse_posttooluse_matchers_cover_task_and_agent_only "
        "already prove at the script/manifest level, but cannot observe whether Claude Code's "
        "own regex engine fullmatches or searches)."
    ),
    "live_cc_mcp_smoke_knowledge_server": (
        "MCP smoke via Claude Code for trialerror-knowledge (design Section 12 M8 row: \"MCP smoke "
        "via Claude Code (integration session)\"): register `trialerror mcp knowledge` in a real "
        "Claude Code session's MCP config and confirm the 11 tools are actually offered to and "
        "callable by a live agent. Offline proxy already covered: "
        "tests/test_mcp_knowledge_protocol.py (real stdio JSON-RPC wire round trip + a real "
        "subprocess initialize/tools-list handshake)."
    ),
    "live_cc_mcp_smoke_ops_server_book_spawn_reconcile": (
        "MCP smoke via Claude Code for trialerror-ops, specifically the book->spawn->reconcile round "
        "trip (design Section 12 M14 row): register `trialerror mcp ops` in a real Claude Code "
        "session, call `book_launch` via the MCP tool, spawn a REAL subagent with the returned "
        "launch_id -- Claude Code 2.1.x does this as the `Agent` tool, not the legacy `Task` "
        "alias (FU-11, 2026-09-05); exercises the live PreToolUse hook above -- then "
        "`reconcile_launch`. While here, capture the full `tool_input` object of that real "
        "`Agent` spawn from the session transcript and confirm it still carries a `prompt` (or "
        "`description`) field: spawn_gate.py/post_task.py's launch_id extraction assumed that "
        "shape, unverified, the same way the tool NAME was assumed to stay `Task` -- if the "
        "field ever moves, the gate now falls back to scanning the whole serialized "
        "`tool_input` for the `launch_id:` token (FU-11 verification finding FU11-V5) rather "
        "than failing closed on every spawn, but that fallback itself is unverified live. "
        "Offline proxy already covered: tests/test_mcp_ops_protocol.py::"
        "test_full_book_spawn_reconcile_round_trip_over_the_wire + "
        "test_stdio_smoke_real_subprocess_initialize_and_tools_list."
    ),
    "gpu_real_marker_ocr_backend": (
        "LIVE GPU backend verification: RealMarkerOcrBackend against real scanned pages (design "
        "Section 13 flag F18/M7): on a machine with `marker_single` on PATH and a real GPU, set "
        "`trialerror.toml [ingest.ocr] backend=\"marker\"` (+ `marker_single_exe`), ingest an actual "
        "scanned-image PDF (not the FakeOcrBackend's form-feed-delimited text stand-in this "
        "harness uses), and confirm OCR output + page anchors are correct. Offline proxy already "
        "covered: tests/test_ingest_backends.py::test_real_marker_ocr_backend_smoke "
        "(skipif-gated on `shutil.which(\"marker_single\")`, so it already self-skips on this "
        "non-GPU build host)."
    ),
    "gpu_real_qwen_embed_backend": (
        "LIVE GPU backend verification: RealQwenEmbedBackend against the origin-project embeddings_local "
        "venv (design Section 13 flag F18/M7): on a machine with the origin-project `embeddings_local` "
        "Python environment + GPU, set `trialerror.toml [ingest.embed] backend=\"qwen3-4b\"` (+ "
        "`python_exe`/`module_dir` config-pathed, never hardcoded), ingest a real document, and "
        "confirm embeddings are produced and indexed correctly (matryoshka 2048, "
        "instruction-aware). No offline proxy exists for this one beyond the config-building "
        "unit tests (RealQwenEmbedBackend is constructed but never `.embed_batch()`-called "
        "outside a GPU host) -- this is the one true zero-coverage GPU gap this build leaves "
        "explicitly named rather than silently untested."
    ),
}


def gpu_and_live_cc_enumeration() -> list[CheckResult]:
    """The 8 items above, shaped as ``skip`` :class:`CheckResult`\\ s for
    ``trialerror accept``'s doctor-shaped summary -- never attempted, always
    listed, so a human/agent reading ``trialerror accept``'s output sees exactly
    what remains for a live Claude Code + GPU session to discharge."""
    return [
        CheckResult(name=key, category="live_cc_or_gpu", status="skip", message=message)
        for key, message in GPU_LIVE_CC_ITEMS.items()
    ]
