"""``trialerror archive`` — a content-addressed archive of session transcripts,
so that a host's own clean-up never loses one. Verbs: ``run`` (copy what is new
or changed), ``restore`` (get exact bytes back), ``status`` (what the archive
holds), ``audit`` (eight plain-words checks, filed under ``<dest>/audits/``).

The verbs take their folders as flags, not from ``trialerror.toml``: the host
schedulers that run them carry the paths, and an archive belongs to a host, not
to a program.
"""

from __future__ import annotations

import argparse

from trialerror.archive import audit as audit_mod
from trialerror.archive import store
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "archive"
HELP = "Transcript archive: `archive run` copies new/changed files, `restore` gets bytes back, `status` and `audit` report."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_run = actions.add_parser("run", help="copy every new or changed file from --src into the archive at --dest")
    p_run.add_argument("--src", required=True, help="the folder to archive (a `projects` folder)")
    p_run.add_argument("--dest", required=True, help="the archive folder; must be outside every git working tree")
    p_run.add_argument("--host", required=True, help="a label for this host (kept in the index)")
    p_run.add_argument("--dry-run", action="store_true", help="report what would be copied; write nothing")
    p_run.add_argument(
        "--no-prune", action="store_true", help="do not delete old superseded object files after this run"
    )
    p_run.add_argument(
        "--include", action="append", default=None, metavar="GLOB",
        help="B6: scope this run to project folders (the first path component under --src) matching this glob "
        "(repeatable); without it, everything is archived, as before",
    )
    p_run.set_defaults(handler=run_run)

    p_restore = actions.add_parser("restore", help="write the exact original bytes of an archived file to --out")
    p_restore.add_argument("--dest", required=True)
    p_restore.add_argument("--sha", default=None, help="restore the object with this sha256")
    p_restore.add_argument("--host", default=None, help="with --path: the host the file came from")
    p_restore.add_argument("--path", default=None, help="with --host: the file's path relative to the archived folder")
    p_restore.add_argument("--as-of", default=None, help="with --path: the version the archive had at this ISO time")
    p_restore.add_argument("--out", required=True, help="where to write the file (never overwritten)")
    p_restore.set_defaults(handler=run_restore)

    p_status = actions.add_parser("status", help="per host: files, bytes stored, last run, gone and superseded counts")
    p_status.add_argument("--dest", required=True)
    p_status.set_defaults(handler=run_status)

    p_audit = actions.add_parser("audit", help="eight checks on the archive, filed as AUDIT_<host>_<date>.md and .json")
    p_audit.add_argument("--dest", required=True)
    p_audit.add_argument("--host", required=True, help="the host label the archive was run with")
    p_audit.add_argument("--src", default=None, help="the folder the archive copies from; enables the coverage check")
    p_audit.add_argument("--sample", type=int, default=50, help="objects to re-hash for the integrity check (default 50)")
    p_audit.add_argument("--gap-file", default=None, help="JSON list of known gaps (default: <dest>/gaps.json)")
    p_audit.add_argument(
        "--settings", default=None, help="the host's Claude Code settings.json (default: next to --src, else ~/.claude)"
    )
    p_audit.set_defaults(handler=run_audit)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(GROUP_NAME, "no_action", "specify one of: run, restore, status, audit")


def _refusal(command: str, exc: store.ArchiveError) -> dict:
    return error_envelope(command, exc.code, exc.message, details=exc.details)


def run_run(args: argparse.Namespace) -> dict:
    try:
        result = store.run_archive(
            args.src, args.dest, args.host, dry_run=args.dry_run, prune=not args.no_prune,
            include=getattr(args, "include", None),
        )
    except store.ArchiveError as exc:
        return _refusal("archive run", exc)
    warnings = []
    if result.get("errors"):
        warnings.append(
            {"code": "file_errors", "message": f"{len(result['errors'])} file(s) could not be archived; see result.errors"}
        )
    actions = []
    if not result.get("locked") and not args.dry_run:
        actions.append(next_action(["trialerror", "archive", "status", "--dest", str(args.dest)], "see what the archive holds"))
    return ok_envelope("archive run", result=result, next_actions=actions, warnings=warnings or None)


def run_restore(args: argparse.Namespace) -> dict:
    try:
        result = store.restore(
            args.dest, args.out, sha=args.sha, host=args.host, rel_path=args.path, as_of=args.as_of
        )
    except store.ArchiveError as exc:
        return _refusal("archive restore", exc)
    return ok_envelope("archive restore", result=result)


def run_audit(args: argparse.Namespace) -> dict:
    try:
        report = audit_mod.run_audit(
            args.dest, args.host, src=args.src, sample=args.sample, gap_file=args.gap_file, settings_file=args.settings
        )
    except store.ArchiveError as exc:
        return _refusal("archive audit", exc)
    result = {
        "host": report["host"],
        "clean": report["clean"],
        "headline": report["headline"],
        "cadence": report["cadence"],
        "checks": [
            {"n": c["n"], "name": c["name"], "status": c["status"], "sentence": c["sentence"]} for c in report["checks"]
        ],
        "report_md": report["report_md"],
        "report_json": report["report_json"],
    }
    warnings = [
        {"code": "audit_fail", "message": f"check {c['n']} ({c['name']}): {c['sentence']}"}
        for c in report["checks"]
        if c["status"] == "fail"
    ]
    return ok_envelope("archive audit", result=result, warnings=warnings or None)


def run_status(args: argparse.Namespace) -> dict:
    try:
        result = store.archive_status(args.dest)
    except store.ArchiveError as exc:
        return _refusal("archive status", exc)
    return ok_envelope("archive status", result=result)
