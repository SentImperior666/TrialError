"""``trialerror ingest`` -- the ingestion pipeline's CLI surface. Design Section
6 (stage graph) + Section 12 (M7 row).

Registration rule (design Section 5.2 / lane safety): this module lives at
``trialerror/cli/ingest.py`` and is auto-discovered by ``trialerror.cli.discover_groups``
-- adding it never touched ``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trialerror.ingest import pipeline, requests as ingest_requests
from trialerror.ingest.errors import IngestError
from trialerror.jobs.errors import JobError
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "ingest"
HELP = "Ingestion pipeline: register sources/documents, run doctor, rechunk/re-embed, request queue."


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="ingest_cmd", metavar="<command>", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so
        # an unset value here never overwrites the global --program-root/
        # --platform-root the top-level parser resolved.
        p.add_argument(
            "--program-root", default=argparse.SUPPRESS, help="program scaffold root (default: discovered from CWD via trialerror.toml)"
        )
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")

    p_add_source = sub.add_parser("add-source", help="register a source row (dedups on content_sha256)")
    _common(p_add_source)
    p_add_source.add_argument("--kind", required=True, choices=["paper", "book", "web", "rulebook", "dataset", "report", "other"])
    p_add_source.add_argument("--title", required=True)
    p_add_source.add_argument("--license-tier", required=True, dest="license_tier",
                               choices=["open", "academic_oa", "user_owned_scan", "commercial_restricted", "unknown"])
    p_add_source.add_argument("--acquisition-route", required=True, dest="acquisition_route",
                               choices=["author_posted", "institutional", "publisher_oa", "user_scan", "user_delivered", "api", "web"])
    p_add_source.add_argument("--launch-id", required=True, dest="launch_id")
    p_add_source.add_argument("--content-file", default=None, dest="content_file", help="hash this file for content_sha256/dedup")
    p_add_source.add_argument("--authors", default=None)
    p_add_source.add_argument("--year", type=int, default=None)
    p_add_source.add_argument("--url", default=None)
    p_add_source.add_argument("--rights-notes", default=None, dest="rights_notes")
    p_add_source.add_argument("--request-state", default="delivered", dest="request_state")
    p_add_source.set_defaults(handler=_cmd_add_source)

    p_add = sub.add_parser("add", help="acquire+register a document under a source and enqueue its pipeline")
    _common(p_add)
    p_add.add_argument("--source-id", required=True, dest="source_id")
    p_add.add_argument("--path", required=True, help="raw file path (must resolve under a configured ingest root)")
    p_add.add_argument("--media-type", default=None, dest="media_type",
                        help="override media-type detection (pdf-text|pdf-scan|html|epub|md|image)")
    p_add.add_argument("--launch-id", required=True, dest="launch_id")
    p_add.add_argument("--yes", action="store_true", help="proceed past the cost gate")
    p_add.set_defaults(handler=_cmd_add)

    p_doctor = sub.add_parser("doctor", help="ingest-specific health checks (chunker/embedding staleness, anchors_dangling)")
    _common(p_doctor)
    p_doctor.set_defaults(handler=_cmd_doctor)

    p_rechunk = sub.add_parser("rechunk", help="re-enqueue the chunk stage for a document")
    _common(p_rechunk)
    p_rechunk.add_argument("--doc-id", required=True, dest="doc_id")
    p_rechunk.add_argument("--launch-id", required=True, dest="launch_id")
    p_rechunk.set_defaults(handler=_cmd_rechunk)

    p_reembed = sub.add_parser("re-embed", help="re-enqueue the embed stage for a document")
    _common(p_reembed)
    p_reembed.add_argument("--doc-id", required=True, dest="doc_id")
    p_reembed.add_argument("--launch-id", required=True, dest="launch_id")
    p_reembed.set_defaults(handler=_cmd_reembed)

    p_status = sub.add_parser("status", help="show a document's pipeline status")
    _common(p_status)
    p_status.add_argument("--doc-id", required=True, dest="doc_id")
    p_status.set_defaults(handler=_cmd_status)

    p_request = sub.add_parser("request", help="request-queue transitions + REQUESTS.md render")
    _common(p_request)
    p_request.add_argument("--source-id", required=True, dest="source_id")
    p_request.add_argument("--to", required=True, dest="to_state")
    p_request.add_argument("--launch-id", default=None, dest="launch_id")
    p_request.add_argument("--note", default=None)
    p_request.set_defaults(handler=_cmd_request)

    p_requests_md = sub.add_parser("requests-md", help="render requests/REQUESTS.md from the source table")
    _common(p_requests_md)
    p_requests_md.set_defaults(handler=_cmd_requests_md)

    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if args.program_root:
        return Path(args.program_root)
    return find_program_root()


def _open(args: argparse.Namespace, cmd: str) -> tuple[Store | None, Path | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, None, error_envelope(
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )
    store = open_store(program_root, platform_root=args.platform_root)
    return store, program_root, None


def _load_program_config(program_root: Path) -> dict:
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


def _cmd_add_source(args: argparse.Namespace) -> dict:
    store, program_root, err = _open(args, "ingest.add-source")
    if err is not None:
        return err
    try:
        content_sha256 = None
        if args.content_file:
            content_sha256 = pipeline.sha256_file(Path(args.content_file))
        row = pipeline.register_source(
            store,
            kind=args.kind,
            title=args.title,
            license_tier=args.license_tier,
            acquisition_route=args.acquisition_route,
            registered_by_launch=args.launch_id,
            authors=args.authors,
            year=args.year,
            url=args.url,
            content_sha256=content_sha256,
            rights_notes=args.rights_notes,
            request_state=args.request_state,
            config=_load_program_config(program_root),
        )
        deduped = row.get("dedup_of") == row.get("source_id")
        return ok_envelope(
            "ingest.add-source",
            result={"source": row, "deduped": deduped},
            next_actions=[] if deduped else [next_action(["trialerror", "ingest", "add", "--source-id", row["source_id"], "--path", "<raw-file>", "--launch-id", args.launch_id], "acquire a document under this source")],
        )
    except (IngestError, StoreError) as exc:
        return error_envelope("ingest.add-source", type(exc).__name__, str(exc))
    finally:
        store.close()


def _cmd_add(args: argparse.Namespace) -> dict:
    store, program_root, err = _open(args, "ingest.add")
    if err is not None:
        return err
    try:
        result = pipeline.add_document(
            store,
            program_root=program_root,
            source_id=args.source_id,
            raw_path=Path(args.path),
            created_by_launch=args.launch_id,
            media_type=args.media_type,
            config=_load_program_config(program_root),
            yes=args.yes,
        )
        return ok_envelope(
            "ingest.add",
            result=result,
            next_actions=[next_action(["trialerror", "jobs", "start-worker", "--job-id", result["job"]["job_id"]], "run the enqueued pipeline job")],
        )
    except ValueError as exc:  # cost-gate refusal
        return error_envelope("ingest.add", "cost_gate_refused", str(exc), next_actions=[
            next_action(["trialerror", "ingest", "add", "--source-id", args.source_id, "--path", args.path, "--launch-id", args.launch_id, "--yes"], "proceed past the cost gate")
        ])
    except (IngestError, StoreError) as exc:
        return error_envelope("ingest.add", type(exc).__name__, str(exc))
    finally:
        store.close()


_INGEST_CHECK_NAMES = (
    "chunker_missing",
    "chunker_outdated",
    "embedding_missing",
    "embedding_stale",
    "anchors_dangling",  # M1's own check (doc_sha256 half)
    "anchor_spot_resolve",  # M7's check (quote_sha256 half)
)


def _cmd_doctor(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    results = run_checks(ctx, only=list(_INGEST_CHECK_NAMES))
    failed = [r for r in results if r.status == "fail"]
    warned = [r for r in results if r.status == "warn"]
    by_name = {r.name: r for r in results}
    doc_sha_mismatches = by_name["anchors_dangling"].details.get("doc_sha256_mismatches", 0) if "anchors_dangling" in by_name else 0
    quote_mismatches = len(by_name["anchor_spot_resolve"].details.get("anchor_ids", [])) if "anchor_spot_resolve" in by_name else 0
    anchors_total = doc_sha_mismatches + quote_mismatches
    result = {
        "checks": [r.to_dict() for r in results],
        "summary": {"total": len(results), "warned": len(warned), "failed": len(failed)},
        "anchors_dangling_total": anchors_total,
    }
    if failed:
        return error_envelope("ingest.doctor", "ingest_doctor_checks_failed", f"{len(failed)} check(s) failed", details=result)
    return ok_envelope("ingest.doctor", result=result)


def _cmd_rechunk(args: argparse.Namespace) -> dict:
    store, _program_root, err = _open(args, "ingest.rechunk")
    if err is not None:
        return err
    try:
        job = pipeline.requeue_stage(store, doc_id=args.doc_id, kind="chunk", created_by_launch=args.launch_id)
        return ok_envelope("ingest.rechunk", result={"job": job})
    except (IngestError, StoreError, JobError) as exc:
        return error_envelope("ingest.rechunk", type(exc).__name__, str(exc))
    finally:
        store.close()


def _cmd_reembed(args: argparse.Namespace) -> dict:
    store, _program_root, err = _open(args, "ingest.re-embed")
    if err is not None:
        return err
    try:
        job = pipeline.requeue_stage(store, doc_id=args.doc_id, kind="embed", created_by_launch=args.launch_id)
        return ok_envelope("ingest.re-embed", result={"job": job})
    except (IngestError, StoreError, JobError) as exc:
        return error_envelope("ingest.re-embed", type(exc).__name__, str(exc))
    finally:
        store.close()


def _cmd_status(args: argparse.Namespace) -> dict:
    store, _program_root, err = _open(args, "ingest.status")
    if err is not None:
        return err
    try:
        doc = store.knowledge.execute("SELECT * FROM document WHERE doc_id = ?", (args.doc_id,)).fetchone()
        if doc is None:
            return error_envelope("ingest.status", "document_not_found", f"no such document: {args.doc_id!r}")
        doc = dict(doc)
        counts = {
            "elements": store.knowledge.execute("SELECT COUNT(*) FROM element WHERE doc_id=?", (args.doc_id,)).fetchone()[0],
            "chunks": store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (args.doc_id,)).fetchone()[0],
            "anchors": store.knowledge.execute("SELECT COUNT(*) FROM quote_anchor WHERE doc_id=?", (args.doc_id,)).fetchone()[0],
        }
        return ok_envelope("ingest.status", result={"document": doc, "counts": counts})
    finally:
        store.close()


def _cmd_request(args: argparse.Namespace) -> dict:
    store, _program_root, err = _open(args, "ingest.request")
    if err is not None:
        return err
    try:
        row = ingest_requests.transition(store, args.source_id, args.to_state, launch_id=args.launch_id, note=args.note)
        return ok_envelope("ingest.request", result={"source": row})
    except IngestError as exc:
        return error_envelope("ingest.request", type(exc).__name__, str(exc))
    finally:
        store.close()


def _cmd_requests_md(args: argparse.Namespace) -> dict:
    store, program_root, err = _open(args, "ingest.requests-md")
    if err is not None:
        return err
    try:
        out_path = ingest_requests.write_requests_md(store, program_root, config=_load_program_config(program_root))
        return ok_envelope("ingest.requests-md", result={"path": str(out_path)})
    finally:
        store.close()
