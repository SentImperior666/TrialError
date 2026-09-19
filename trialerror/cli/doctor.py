"""``trialerror doctor`` — the top-level health-check command.

Design Section 5.2 doctor row: "(top-level) store integrity, XID
referential scan, stale heartbeats, index freshness, anchors_dangling,
``--license-audit`` | framework + license-audit in M0; each module
registers its own checks."

M0 wires the CLI surface and runs whatever checks are registered at call
time (``trialerror.util.doctor.discover_and_register_checks``) — in M0 that is
exactly one check, ``license_audit``. Later modules add checks purely by
shipping a ``checks.py`` in their own package; this file does not change.
"""

from __future__ import annotations

import argparse

from trialerror.util.config import find_program_root
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "doctor"
HELP = "Run store-integrity, license-audit, and other registered health checks."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    parser.add_argument(
        "--license-audit",
        action="store_true",
        help="run only the license_audit check (vendored/ header + manifest scan)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="CHECK_NAME",
        help="run only the named check (repeatable)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="override the repo root the checks resolve paths against (default: CWD)",
    )
    parser.add_argument(
        "--program-root",
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so
        # an unset value here never overwrites the global --program-root
        # the top-level parser resolved.
        default=argparse.SUPPRESS,
        help="program scaffold root, so program-scoped checks (store schema version, "
        "XID-dangling scan, anchors_dangling, ...) can resolve the program's ops/knowledge/"
        "jobs DBs instead of skipping (design Section 12 M15 row / INTEGRATION_NOTES.md "
        "item 5: 'program-scoped checks silently skip through the shared CLI' without this). "
        "Default when omitted everywhere: discovered by walking up from CWD looking for "
        "trialerror.toml (find_program_root), same convention every other CLI group uses "
        "(e.g. trialerror/cli/session.py's _resolve_program_root) -- LANE0_SANDBOX_RELOCATION_"
        "DESIGN.md Sec 6 item 2 / INTEGRATION_NOTES.md item 5 follow-up.",
    )
    parser.add_argument(
        "--platform-root",
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so
        # an unset value here never overwrites the global --platform-root
        # the top-level parser resolved.
        default=argparse.SUPPRESS,
        help="platform root, so platform-scoped checks (M1's xid_dangling, the "
        "store_schema_version 'platform' DB kind, budget's dangling-launches/pool-overspend, "
        "events' feed_author_integrity, ...) resolve the caller's platform.db instead of always "
        "falling back to TRIALERROR_PLATFORM_ROOT/~/.trialerror (fix-accept, C-0064: without this, "
        "`trialerror accept` false-positived xid_dangling against a real machine's ~/.trialerror/"
        "platform.db even though it resolved its own scratch platform_root elsewhere)",
    )
    parser.add_argument(
        "--vendored-root",
        default=None,
        help="override the vendored/ root the license_audit check scans (mainly for tests)",
    )
    parser.set_defaults(handler=run)
    return parser


def run(args: argparse.Namespace) -> dict:
    from pathlib import Path

    discover_and_register_checks()

    only = list(args.only) if args.only else None
    if args.license_audit:
        only = ["license_audit"]

    # LANE0_SANDBOX_RELOCATION_DESIGN.md Sec 6 item 2 (INTEGRATION_NOTES.md item 5
    # follow-up): item 5 gave `trialerror doctor` a --program-root FLAG, but never
    # gave it a DEFAULT -- run from inside a real program root with no flag at all,
    # DoctorContext.program_root stayed None and every program-scoped check kept
    # silently skipping. Every other CLI group falls back to find_program_root()
    # (walk up from CWD for trialerror.toml) when the flag is omitted at every
    # placement (e.g. trialerror/cli/session.py's _resolve_program_root) -- doctor
    # now does the same.
    program_root = Path(args.program_root) if args.program_root else find_program_root()

    ctx = DoctorContext(
        repo_root=Path(args.repo_root) if args.repo_root else Path.cwd(),
        program_root=program_root,
        platform_root=Path(args.platform_root) if args.platform_root else None,
        vendored_root=Path(args.vendored_root) if args.vendored_root else None,
    )

    try:
        results = run_checks(ctx, only=only)
    except Exception as exc:  # defensive: doctor itself must not crash uncaught
        return error_envelope("doctor", "doctor_crashed", str(exc))

    failed = [r for r in results if r.status == "fail"]
    warned = [r for r in results if r.status == "warn"]
    passed = [r for r in results if r.status == "pass"]

    summary = {
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "warned": len(warned),
    }
    result = {"checks": [r.to_dict() for r in results], "summary": summary}

    if failed:
        return error_envelope(
            "doctor",
            "doctor_checks_failed",
            f"{len(failed)} of {len(results)} doctor check(s) failed",
            details=result,
            next_actions=[
                next_action(
                    ["trialerror", "doctor", "--only", failed[0].name],
                    "re-run the failing check(s) after fixing the reported issue",
                )
            ],
        )

    return ok_envelope("doctor", result=result)
