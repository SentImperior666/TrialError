"""``trialerror accept`` -- the acceptance harness's CLI surface. Design Section
12 (M15 row): "end-to-end smoke ... doctor green ... doubles as the CI
definition." This is the "or per-design entry" alternative front door onto
:mod:`trialerror.accept.journeys` (the other being ``pytest -m acceptance``,
under ``tests/acceptance/`` -- the design's own literally-named acceptance
bar): a human or agent can run the SAME journeys without pytest and get a
summary shaped exactly like ``trialerror doctor``'s own output
(``{"checks": [...], "summary": {...}}``, one entry per journey/enumerated
item) -- "doctor-integrated" in the sense of a shared vocabulary and shape,
not by registering these as passive ``trialerror doctor`` checks: the clean-
checkout smoke has real side effects (spawns real subprocesses, writes a
whole program scaffold) that do not belong in doctor's fast, read-only-in-
spirit per-module registry (see ``trialerror/util/doctor.py``'s own module
docstring) -- ``trialerror doctor`` stays cheap to run on every boot; `trialerror
accept` is the explicit, heavier sibling.

**Two suites, one front door (``--suite``).** ``smoke`` (the default, and
exactly the behaviour every existing caller already gets) runs the M15
clean-checkout journey plus the enumerated GPU/live-Claude-Code items.
``e2e`` runs the handover-gate journeys of ``docs/reviews/E2E_TEST_PLAN.md``
Section 7 -- one acceptance front door and one envelope shape rather than a
second CLI group, per that plan's own resolution of its open question OQ-7.
The e2e suite differs from the smoke in three ways that are deliberate, not
incidental:

- ``--program-root``/``--platform-root`` are REQUIRED. The smoke's temp-dir
  default is exactly wrong for a run whose later phases (a live session, a
  phone, a GPU worker on another machine, a cleanup step) inspect the scratch
  tree hours later, and whose offload root has to live inside a bind mount.
- ``--run-id`` is required and must be shell-clock shaped, so a run id can
  never be hand-typed into the record.
- the exit code is non-zero only on a real ``fail``. A ``blocked`` check (its
  lane is not present on this deployment) leaves the exit code at zero and
  says so in the statuses -- the handover gate reads the statuses, not only the
  exit code, and a blocked gating check is released by an operator ruling, not
  by a shell test.

The offload phases are the one place where the program under test and the
program the record lives in come apart: they run against a SECOND scratch
program, while ``--phase report`` and E-61's export read the first one.
``--record-program-root`` is what keeps E-50/E-52 in the same record as the
rest; without it their rows stay in the offload program and the report reads
them as ``MISSING``.

``--suite smoke`` keeps its pre-existing behaviour, with one additive field:
``result.suite`` now names the suite that ran (``"smoke"``), alongside the
``checks``/``summary`` keys every existing caller already reads.

Registration rule (design Section 5.2 / lane safety): this module lives at
``trialerror/cli/accept.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touches
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from trialerror.accept.e2e import (
    E2E_CHECK_CATALOGUE,
    RUN_ID_RE,
    e2e_check_sequence,
    e2e_operator_enumeration,
    probe_capabilities,
    read_recorded_checks,
    run_e2e_corpus,
    run_e2e_dashboard,
    run_e2e_offload_enqueue,
    run_e2e_ops,
    verify_e2e_offload_roundtrip,
)
from trialerror.accept.journeys import gpu_and_live_cc_enumeration, run_clean_checkout_smoke
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "accept"
HELP = (
    "Run the acceptance harness: --suite smoke (the M15 clean-checkout journey + the enumerated "
    "GPU/live-Claude-Code items) or --suite e2e (the sandbox handover-gate journeys)."
)

E2E_PHASES = ("all", "corpus", "dashboard", "ops", "offload-enqueue", "offload-verify", "report")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root/
    # --platform-root the top-level parser resolved.
    parser.add_argument(
        "--program-root", default=argparse.SUPPRESS,
        help="program scaffold root to run against (smoke: defaults to a fresh temp directory, discarded "
        "after the run; e2e: REQUIRED)",
    )
    parser.add_argument(
        "--platform-root", default=argparse.SUPPRESS,
        help="override the platform root (smoke: defaults to a fresh temp directory alongside "
        "--program-root; e2e: REQUIRED)",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="override the repo root the doctor steps resolve paths against (default: this checkout)",
    )
    parser.add_argument(
        "--skip-gpu-live-cc-enumeration", action="store_true",
        help="omit the enumerated live/GPU/operator items from the summary (the journeys only)",
    )
    parser.add_argument(
        "--suite", choices=("smoke", "e2e"), default="smoke",
        help="which acceptance suite to run (default: smoke -- exactly the pre-existing behaviour)",
    )
    parser.add_argument(
        "--phase", choices=E2E_PHASES, default="all",
        help="e2e only: which phase to run. 'all' = corpus -> dashboard -> ops in one process; "
        "'report' prints every catalogue id with its recorded e2e_check status or MISSING",
    )
    parser.add_argument(
        "--run-id", default=None, dest="run_id",
        help="e2e only, REQUIRED: the run identifier, always taken from the shell clock "
        "(e2e-$(date -u +%%Y%%m%%dT%%H%%M%%SZ))",
    )
    parser.add_argument(
        "--record-program-root", default=None, dest="record_program_root",
        help="e2e --phase offload-enqueue|offload-verify: the program whose ops.db the E-50/E-52 evidence rows "
        "are written to (default: --program-root). The offload phases run against a SECOND scratch program "
        "while --phase report and the run-record export read the FIRST one -- point this at that first program "
        "and the whole record stays in one place",
    )
    parser.add_argument("--job-id", default=None, dest="job_id", help="e2e --phase offload-verify: the parked job")
    parser.add_argument(
        "--chunk-count", type=int, default=None, dest="chunk_count",
        help="e2e --phase offload-verify: the chunk count the enqueue phase reported",
    )
    parser.add_argument(
        "--expected-dims", type=int, default=None, dest="expected_dims",
        help="e2e --phase offload-verify: override the dimensionality the vectors must carry "
        "(default: the program's own [ingest.embed] dims)",
    )
    parser.add_argument(
        "--min-chunks", type=int, default=200, dest="min_chunks",
        help="e2e --phase offload-enqueue: the batch size the synthetic fixture must reach (default: 200)",
    )
    parser.add_argument(
        "--smoke-envelope", default=None, dest="smoke_envelope",
        help="e2e --phase ops|all: path to the JSON envelope a prior `trialerror accept` (smoke suite) run "
        "wrote. The ops journey copies its summary and step names into the E-02 evidence row rather than "
        "re-running the smoke -- without it E-02 stays MISSING in the report",
    )
    parser.set_defaults(handler=run)
    return parser


def _summarize(checks: list[dict]) -> dict:
    return {
        "total": len(checks),
        "passed": len([c for c in checks if c["status"] == "pass"]),
        "failed": len([c for c in checks if c["status"] == "fail"]),
        "warned": len([c for c in checks if c["status"] == "warn"]),
        "skipped": len([c for c in checks if c["status"] == "skip"]),
    }


def run(args: argparse.Namespace) -> dict:
    if getattr(args, "suite", "smoke") == "e2e":
        return _run_e2e(args)
    return _run_smoke(args)


def _run_smoke(args: argparse.Namespace) -> dict:
    repo_root = Path(args.repo_root) if args.repo_root else None
    program_root_arg = getattr(args, "program_root", None)
    platform_root_arg = getattr(args, "platform_root", None)

    with tempfile.TemporaryDirectory(prefix="trialerror-accept-") as tmp:
        tmp_path = Path(tmp)
        program_root = Path(program_root_arg) if program_root_arg else tmp_path / "program"
        platform_root = Path(platform_root_arg) if platform_root_arg else tmp_path / "platform"

        smoke = run_clean_checkout_smoke(program_root, platform_root, repo_root=repo_root)
        checks = [smoke.to_dict()]
        if not args.skip_gpu_live_cc_enumeration:
            checks.extend(r.to_dict() for r in gpu_and_live_cc_enumeration())

    summary = _summarize(checks)
    result = {"suite": "smoke", "checks": checks, "summary": summary}

    failed = [c for c in checks if c["status"] == "fail"]
    if failed:
        return error_envelope(
            "accept", "acceptance_checks_failed",
            f"{len(failed)} of {len(checks)} acceptance check(s) failed: {[c['name'] for c in failed]}",
            details=result,
        )
    return ok_envelope("accept", result=result)


def _run_e2e(args: argparse.Namespace) -> dict:
    program_root_arg = getattr(args, "program_root", None)
    platform_root_arg = getattr(args, "platform_root", None)
    if not program_root_arg or not platform_root_arg:
        return error_envelope(
            "accept", "e2e_requires_explicit_roots",
            "--suite e2e requires BOTH --program-root and --platform-root: a scratch root that vanishes with "
            "the process cannot be inspected by the live-session, remote-control, offload and cleanup phases, "
            "and the offload root has to live inside the deployment's bind mount",
            next_actions=[
                next_action(
                    ["trialerror", "accept", "--suite", "e2e", "--run-id", "<run-id>",
                     "--program-root", "<scratch program>", "--platform-root", "<scratch platform>"],
                    "re-run with explicit scratch roots",
                )
            ],
        )

    run_id = args.run_id
    if not run_id or not RUN_ID_RE.match(run_id):
        return error_envelope(
            "accept", "e2e_run_id_malformed",
            f"--run-id must be shell-clock shaped ({RUN_ID_RE.pattern}), got {run_id!r} -- take it from the "
            'shell clock: RUN="e2e-$(date -u +%Y%m%dT%H%M%SZ)"',
        )

    program_root = Path(program_root_arg)
    platform_root = Path(platform_root_arg)
    repo_root = Path(args.repo_root) if args.repo_root else None
    record_program_root = (
        Path(args.record_program_root) if getattr(args, "record_program_root", None) else None
    )
    phase = args.phase

    if phase == "report":
        return _run_report(program_root, platform_root, run_id=run_id)

    smoke_envelope: dict | None = None
    if args.smoke_envelope:
        try:
            smoke_envelope = json.loads(Path(args.smoke_envelope).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return error_envelope(
                "accept", "smoke_envelope_unreadable",
                f"--smoke-envelope {args.smoke_envelope!r} is not readable JSON: {exc}",
            )

    caps = probe_capabilities(repo_root=repo_root)
    checks: list[dict] = []
    meta: dict[str, Any] = {}

    if phase in ("all", "corpus"):
        checks.append(
            run_e2e_corpus(program_root, platform_root, run_id=run_id, repo_root=repo_root, caps=caps).to_dict()
        )
    if phase in ("all", "dashboard") and not _stop(checks):
        checks.append(
            run_e2e_dashboard(program_root, platform_root, run_id=run_id, repo_root=repo_root, caps=caps).to_dict()
        )
    if phase in ("all", "ops") and not _stop(checks):
        checks.append(
            run_e2e_ops(
                program_root, platform_root, run_id=run_id, repo_root=repo_root, caps=caps,
                smoke_envelope=smoke_envelope,
            ).to_dict()
        )
    if phase == "offload-enqueue":
        result = run_e2e_offload_enqueue(
            program_root, platform_root, run_id=run_id, repo_root=repo_root,
            min_chunks=args.min_chunks, caps=caps, record_program_root=record_program_root,
        )
        checks.append(result.to_dict())
        job_id = result.details.get("job_id")
        chunk_count = result.details.get("chunk_count")
        meta["record_program_root"] = result.details.get("record_program_root")
        if job_id:
            meta["prompt_fragment"] = (
                f"offload job_id: {job_id}\nchunk_count: {chunk_count}\n"
                "pass both to `--phase offload-verify` once the GPU worker has published."
            )
            meta["job_id"] = job_id
            meta["chunk_count"] = chunk_count
    if phase == "offload-verify":
        if not args.job_id or args.chunk_count is None:
            return error_envelope(
                "accept", "e2e_offload_verify_needs_job",
                "--phase offload-verify requires --job-id and --chunk-count (both reported by "
                "--phase offload-enqueue on meta.prompt_fragment)",
            )
        verified = verify_e2e_offload_roundtrip(
            program_root, platform_root, run_id=run_id, job_id=args.job_id,
            chunk_count=args.chunk_count, repo_root=repo_root,
            expected_dims=args.expected_dims, caps=caps, record_program_root=record_program_root,
        )
        checks.append(verified.to_dict())
        meta["record_program_root"] = verified.details.get("record_program_root")

    if not args.skip_gpu_live_cc_enumeration:
        checks.extend(r.to_dict() for r in e2e_operator_enumeration())

    summary = _summarize(checks)
    result_payload = {
        "suite": "e2e",
        "phase": phase,
        "run_id": run_id,
        "checks": checks,
        "summary": summary,
        "capabilities": caps.to_dict(),
    }

    failed = [c for c in checks if c["status"] == "fail"]
    if failed:
        return error_envelope(
            "accept", "e2e_checks_failed",
            f"{len(failed)} of {len(checks)} e2e journey(s) failed: {[c['name'] for c in failed]}",
            details=result_payload,
            next_actions=[
                next_action(
                    ["trialerror", "accept", "--suite", "e2e", "--phase", "report", "--run-id", run_id,
                     "--program-root", str(program_root), "--platform-root", str(platform_root)],
                    "list every catalogue id with its recorded status",
                )
            ],
        )
    return ok_envelope("accept", result=result_payload, meta=meta or None)


def _stop(checks: list[dict]) -> bool:
    """``--phase all`` runs the three journeys in sequence and each depends on
    the previous one's state in the SAME scratch program, so a failure stops
    the chain rather than cascading into three unrelated failures."""
    return any(c["status"] == "fail" for c in checks)


def _run_report(program_root: Path, platform_root: Path, *, run_id: str) -> dict:
    """Every catalogue id with the status actually recorded for it, or
    ``MISSING``. This is what closes the completeness rule before the run
    record is exported: a check with no ``e2e_check`` row did not happen."""
    from trialerror.stores.store import open_store

    store = open_store(program_root, platform_root=platform_root)
    try:
        recorded = read_recorded_checks(store, run_id=run_id)
    finally:
        store.close()

    rows = []
    for spec in e2e_check_sequence():
        row = recorded.get(spec["check_id"])
        rows.append({
            **spec,
            "status": (row or {}).get("status", "MISSING"),
            "recorded_by": (row or {}).get("by"),
            "owner": (row or {}).get("owner"),
            "plan_blob": (row or {}).get("plan_blob"),
        })

    missing = [r["check_id"] for r in rows if r["status"] == "MISSING"]
    failed = [r["check_id"] for r in rows if r["status"] == "fail"]
    blocked = [r["check_id"] for r in rows if r["status"] == "blocked"]
    gating_incomplete = [
        r["check_id"] for r in rows if r["gating"] and r["status"] not in ("pass",)
    ]
    result = {
        "suite": "e2e",
        "phase": "report",
        "run_id": run_id,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "recorded": len(rows) - len(missing),
            "missing": missing,
            "failed": failed,
            "blocked": blocked,
            "gating_not_yet_pass": gating_incomplete,
        },
        "catalogue_size": len(E2E_CHECK_CATALOGUE),
    }
    if failed:
        return error_envelope(
            "accept", "e2e_checks_failed",
            f"{len(failed)} recorded e2e check(s) failed: {failed}",
            details=result,
        )
    return ok_envelope("accept", result=result)
