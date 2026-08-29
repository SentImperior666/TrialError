"""``trialerror accept`` -- M15's acceptance-harness CLI surface. Design Section
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

Registration rule (design Section 5.2 / lane safety): this module lives at
``trialerror/cli/accept.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touches
``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from trialerror.accept.journeys import gpu_and_live_cc_enumeration, run_clean_checkout_smoke
from trialerror.util.envelope import error_envelope, ok_envelope

GROUP_NAME = "accept"
HELP = "Run the M15 acceptance harness (clean-checkout smoke + the enumerated GPU/live-Claude-Code journeys)."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root/
    # --platform-root the top-level parser resolved.
    parser.add_argument(
        "--program-root", default=argparse.SUPPRESS,
        help="program scaffold root to run the smoke against (default: a fresh temp directory, discarded after the run)",
    )
    parser.add_argument(
        "--platform-root", default=argparse.SUPPRESS,
        help="override the platform root (default: a fresh temp directory alongside --program-root)",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="override the repo root the doctor_green step resolves paths against (default: this checkout)",
    )
    parser.add_argument(
        "--skip-gpu-live-cc-enumeration", action="store_true",
        help="omit the 8 always-skip GPU/live-Claude-Code entries from the summary (clean_checkout_smoke only)",
    )
    parser.set_defaults(handler=run)
    return parser


def run(args: argparse.Namespace) -> dict:
    repo_root = Path(args.repo_root) if args.repo_root else None

    with tempfile.TemporaryDirectory(prefix="trialerror-accept-") as tmp:
        tmp_path = Path(tmp)
        program_root = Path(args.program_root) if args.program_root else tmp_path / "program"
        platform_root = Path(args.platform_root) if args.platform_root else tmp_path / "platform"

        smoke = run_clean_checkout_smoke(program_root, platform_root, repo_root=repo_root)
        checks = [smoke.to_dict()]
        if not args.skip_gpu_live_cc_enumeration:
            checks.extend(r.to_dict() for r in gpu_and_live_cc_enumeration())

    passed = [c for c in checks if c["status"] == "pass"]
    failed = [c for c in checks if c["status"] == "fail"]
    skipped = [c for c in checks if c["status"] == "skip"]
    warned = [c for c in checks if c["status"] == "warn"]

    summary = {"total": len(checks), "passed": len(passed), "failed": len(failed), "warned": len(warned), "skipped": len(skipped)}
    result = {"checks": checks, "summary": summary}

    if failed:
        return error_envelope(
            "accept", "acceptance_checks_failed",
            f"{len(failed)} of {len(checks)} acceptance check(s) failed: {[c['name'] for c in failed]}",
            details=result,
        )
    return ok_envelope("accept", result=result)
