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

from trialerror.ingest import backends, pipeline, pipeline_status, quality, requests as ingest_requests
from trialerror.ingest.errors import IngestError
from trialerror.jobs.errors import JobError
from trialerror.retrieve import tantivysearch
from trialerror.stores import paths as store_paths
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
    p_add_source.add_argument(
        "--kind", required=True, choices=list(pipeline.SOURCE_KINDS),
        help="source kind; 'inventory' marks a structured reference set -- chunked one chunk per row "
             "and excluded by default from every retrieval surface unless a caller asks for it by kind",
    )
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
                        help="override media-type detection (pdf-text|pdf-scan|html|epub|md|image|djvu)")
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

    p_purge = sub.add_parser(
        "purge-embeddings",
        help="remove a SUPERSEDED embed model key's rows and that key's vector-index entries -- "
        "the companion subtraction to re-embed, which by design only ever adds",
    )
    _common(p_purge)
    p_purge.add_argument(
        "--model-key",
        required=True,
        dest="model_key",
        help="the superseded embed model_key to remove. Refused for the key [ingest.embed] "
        "configures: that one is the live search surface",
    )
    p_purge.add_argument(
        "--launch-id",
        required=True,
        dest="launch_id",
        help="the booked launch this deletion is attributable to -- XID-validated against "
        "platform.launch before a single row is touched",
    )
    p_purge.add_argument(
        "--doc-id", default=None, dest="doc_id", help="scope the purge to one document"
    )
    p_purge.add_argument(
        "--dry-run", action="store_true", dest="dry_run", help="report the counts and touch nothing"
    )
    p_purge.set_defaults(handler=_cmd_purge_embeddings)

    p_retract = sub.add_parser(
        "retract",
        help="withdraw an ingested document: remove every derived row, file and index entry; the "
        "document row stays, with a retraction record carrying the reason",
    )
    _common(p_retract)
    p_retract.add_argument("--doc-id", required=True, dest="doc_id")
    p_retract.add_argument("--launch-id", required=True, dest="launch_id")
    p_retract.add_argument(
        "--reason",
        required=True,
        help="why this document is being withdrawn -- recorded on both the retraction record and the "
        "'document_retracted' event, because a corpus deletion with no stated cause is not auditable",
    )
    p_retract.set_defaults(handler=_cmd_retract)

    p_reindex_ft = sub.add_parser(
        "reindex-fulltext",
        help="rebuild the tantivy full-text index from knowledge.db's chunk table (derived state; always safe)",
    )
    _common(p_reindex_ft)
    p_reindex_ft.set_defaults(handler=_cmd_reindex_fulltext)

    p_reindex_vec = sub.add_parser(
        "reindex-vectors",
        help="rebuild one embed model key's vector index (vec_chunks__<key>) from its emb rows -- "
        "the repair for a vector_index_stale finding",
    )
    _common(p_reindex_vec)
    p_reindex_vec.add_argument(
        "--model-key",
        required=True,
        dest="model_key",
        help="the embed model_key whose vector index is rebuilt. Refused for a key this program "
        "has never embedded or indexed under",
    )
    p_reindex_vec.add_argument(
        "--launch-id",
        required=True,
        dest="launch_id",
        help="the booked launch this rebuild is attributable to -- XID-validated against "
        "platform.launch before anything is written. Required because this REPLACES the key's "
        "live semantic-search surface",
    )
    p_reindex_vec.add_argument(
        "--dry-run", action="store_true", dest="dry_run", help="report the counts and touch nothing"
    )
    p_reindex_vec.set_defaults(handler=_cmd_reindex_vectors)

    p_quality = sub.add_parser(
        "quality",
        help="measure extraction quality -- one document (--doc-id) or the corpus (--all); read-only",
    )
    _common(p_quality)
    p_quality.add_argument("--doc-id", default=None, dest="doc_id", help="measure exactly this document")
    p_quality.add_argument(
        "--all", action="store_true", dest="all_docs",
        help="measure the corpus (every document, or --sample of them)",
    )
    p_quality.add_argument(
        "--sample", type=int, default=None,
        help="with --all: measure a seeded random subset of this many documents instead of every one",
    )
    p_quality.add_argument(
        "--seed", type=int, default=None,
        help=f"with --all --sample: the sample's seed (default {quality.DEFAULT_SAMPLE_SEED}) -- the same seed "
             "over an unchanged corpus measures the same documents",
    )
    p_quality.add_argument(
        "--worst", type=int, default=None, dest="worst",
        help="with --all: how many worst-first rows to report (default [ingest.quality] worst_n); "
             "refused beside --doc-id, which reports exactly one",
    )
    p_quality.set_defaults(handler=_cmd_quality)

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
        # FB-1 item F4: `add` returns a registered document, not a
        # searchable one. Say both things -- what is pending, and the
        # one-document way to run it -- rather than leaving a caller to
        # discover the gap by searching for what it just added.
        result["searchable"] = False
        result["pending_stage"] = pipeline_status.job_stage(
            result["job"]["kind"], result["job"].get("payload")
        )
        # FB-1 item F10a: in the envelope, never on stderr -- the command
        # succeeded, and a caller parsing JSON must not have to read a second
        # stream to learn that the pages of what it just added will be read by
        # a stand-in.
        warning = backends.fake_stage_backend_warning(result.get("stage_backend"))
        return ok_envelope(
            "ingest.add",
            result=result,
            warnings=[warning] if warning else None,
            next_actions=pipeline_status.not_yet_searchable_next_actions(
                result["job"], next_action=next_action
            ),
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
    "vector_index_stale",  # the index side of the same pair as embedding_stale
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


def _cmd_purge_embeddings(args: argparse.Namespace) -> dict:
    """The second subtractive verb in this group, and the only one keyed on
    a MODEL rather than a document.

    ``--launch-id`` is required by the parser and XID-validated inside
    :func:`~trialerror.ingest.purge.purge_embeddings` (the L-E4 posture
    ``retract`` applies, for the same reason). Every refusal comes back as
    an error envelope with its own code -- ``purge_refused_active_model_key``
    for the configured key, which is the one an operator is most likely to
    hit and the one whose message has to explain itself.
    """
    from trialerror.ingest import purge as ingest_purge

    store, program_root, err = _open(args, "ingest.purge-embeddings")
    if err is not None:
        return err
    try:
        result = ingest_purge.purge_embeddings(
            store,
            model_key=args.model_key,
            launch_id=args.launch_id,
            doc_id=args.doc_id,
            dry_run=args.dry_run,
            config=_load_program_config(program_root),
        )
    except ingest_purge.ActiveEmbedKeyPurgeError as exc:
        return error_envelope("ingest.purge-embeddings", "purge_refused_active_model_key", str(exc))
    except ingest_purge.PurgeIndexUnreadableError as exc:
        return error_envelope("ingest.purge-embeddings", "purge_index_unreadable", str(exc))
    except IngestError as exc:
        return error_envelope("ingest.purge-embeddings", type(exc).__name__, str(exc))
    except StoreError as exc:
        return error_envelope("ingest.purge-embeddings", type(exc).__name__, str(exc))
    finally:
        store.close()

    next_actions = []
    if result["dry_run"]:
        argv = ["trialerror", "ingest", "purge-embeddings", "--model-key", args.model_key,
                "--launch-id", args.launch_id]
        if args.doc_id:
            argv += ["--doc-id", args.doc_id]
        next_actions.append(next_action(argv, "run the purge for real"))
    elif result["chunks_now_without_any_embedding"]:
        next_actions.append(
            next_action(
                ["trialerror", "ingest", "doctor"],
                f"{result['chunks_now_without_any_embedding']} chunk(s) now have no embedding "
                "under any model key -- re-embed them",
            )
        )
    return ok_envelope("ingest.purge-embeddings", result=result, next_actions=next_actions)


def _cmd_retract(args: argparse.Namespace) -> dict:
    """The one subtractive verb in this group. ``--launch-id`` is required
    (and XID-validated inside :func:`~trialerror.ingest.retract.retract_document`)
    for the same reason every other write verb requires it: a change to the
    record has to be attributable to a booked launch."""
    from trialerror.ingest import retract as ingest_retract

    store, program_root, err = _open(args, "ingest.retract")
    if err is not None:
        return err
    try:
        result = ingest_retract.retract_document(
            store,
            doc_id=args.doc_id,
            launch_id=args.launch_id,
            reason=args.reason,
            config=_load_program_config(program_root),
        )
    except ingest_retract.RetractBlockedError as exc:
        return error_envelope("ingest.retract", "retract_blocked", str(exc))
    except IngestError as exc:
        return error_envelope("ingest.retract", type(exc).__name__, str(exc))
    except StoreError as exc:
        return error_envelope("ingest.retract", type(exc).__name__, str(exc))
    finally:
        store.close()

    next_actions = []
    for held in result.get("jobs_held") or []:
        # Fix pass V-6: a stage a worker is still running against the
        # document that was just withdrawn is the one thing this verb cannot
        # settle itself, so it is the one thing it must say out loud.
        next_actions.append(
            next_action(
                ["trialerror", "jobs", "pause", held["job_id"]],
                f"a worker holds {held['job_id']} ({held['kind']}, {held['state']}) and it was NOT "
                "cancelled; pause it, then `trialerror jobs abandon <job_id> --reason ...`",
            )
        )
    if result["fulltext"].get("action") == "failed":
        next_actions.append(
            next_action(
                ["trialerror", "ingest", "reindex-fulltext"],
                "the full-text index still holds the retracted chunks -- rebuild it",
            )
        )
    return ok_envelope("ingest.retract", result=result, next_actions=next_actions)


def _cmd_reindex_fulltext(args: argparse.Namespace) -> dict:
    """Rebuild the tantivy lexical index from scratch (C-0080). No
    ``--launch-id``, no cost gate, no confirmation flag: this writes
    nothing but DERIVED state that ``knowledge.db`` can regenerate at will
    (:mod:`trialerror.retrieve.tantivysearch`), so there is nothing here an
    operator could destroy by running it twice, or at the wrong moment, or
    on the wrong program. It is the documented repair for every
    ``fulltext_index_stale`` doctor finding and the one migration step an
    existing program needs to move off the FTS5 tier."""
    store, program_root, err = _open(args, "ingest.reindex-fulltext")
    if err is not None:
        return err
    try:
        config = _load_program_config(program_root)
        index_dir = store_paths.fulltext_index_path(program_root, config)
        result = tantivysearch.reindex(store.knowledge, index_dir)
    except tantivysearch.TantivyUnavailableError as exc:
        return error_envelope("ingest.reindex-fulltext", "tantivy_unavailable", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "ingest.reindex-fulltext",
        result=result,
        next_actions=[
            next_action(["trialerror", "doctor", "--only", "fulltext_index_stale"], "confirm the index is current")
        ],
    )


def _cmd_reindex_vectors(args: argparse.Namespace) -> dict:
    """``reindex-fulltext``'s vector-index counterpart -- and the one place
    the two part company: this one takes a ``--launch-id``.

    Both rebuild derived state from ``chunk``/``emb``, so neither can destroy
    knowledge. But ``reindex-fulltext`` writes a file-backed index beside the
    database, while this writes rows INTO the record and replaces the key's
    live semantic-search surface in one transaction -- a program whose
    retrieval changed shape has to be able to name the launch that changed
    it. Every refusal comes back as its own error code, because each one
    means something different for what the operator should do next."""
    from trialerror.ingest import reindex as ingest_reindex

    store, program_root, err = _open(args, "ingest.reindex-vectors")
    if err is not None:
        return err
    try:
        result = ingest_reindex.reindex_vectors(
            store,
            model_key=args.model_key,
            launch_id=args.launch_id,
            dry_run=args.dry_run,
            config=_load_program_config(program_root),
        )
    except ingest_reindex.UnknownEmbedModelKeyError as exc:
        return error_envelope("ingest.reindex-vectors", "unknown_embed_model_key", str(exc))
    except ingest_reindex.VectorIndexDimsConflictError as exc:
        return error_envelope("ingest.reindex-vectors", "vector_index_dims_conflict", str(exc))
    except ingest_reindex.VectorIndexUnreadableError as exc:
        return error_envelope("ingest.reindex-vectors", "vector_index_unreadable", str(exc))
    except IngestError as exc:
        return error_envelope("ingest.reindex-vectors", type(exc).__name__, str(exc))
    except StoreError as exc:
        return error_envelope("ingest.reindex-vectors", type(exc).__name__, str(exc))
    finally:
        store.close()

    if result["dry_run"]:
        next_actions = [
            next_action(
                ["trialerror", "ingest", "reindex-vectors", "--model-key", args.model_key,
                 "--launch-id", args.launch_id],
                "run the rebuild for real",
            )
        ]
    else:
        next_actions = [
            next_action(
                ["trialerror", "doctor", "--only", "vector_index_stale"],
                "confirm the vector index is complete for every key",
            )
        ]
    return ok_envelope("ingest.reindex-vectors", result=result, next_actions=next_actions)


def _document_quality(store: Store, program_root: Path | None, doc_id: str) -> dict:
    """One document's four extraction-quality numbers plus the verdict
    against THIS program's thresholds -- the ``quality`` key of
    ``ingest status`` and the ``--doc-id`` body of ``ingest quality``.

    The thresholds travel with the numbers on purpose: a bare ``suspect:
    true`` invites the reader to guess what it was measured against, and
    the answer is a config file they may not have open.
    """
    thresholds = quality.thresholds_from_config(_load_program_config(program_root) if program_root else None)
    row = quality.measure_document(store, doc_id)
    annotated = quality.worst_first([row], thresholds=thresholds)[0]
    annotated["thresholds"] = thresholds
    return annotated


def _cmd_quality(args: argparse.Namespace) -> dict:
    """``trialerror ingest quality`` -- the four extraction-quality measures,
    read-only, for one document or for the corpus.

    This is the verb an operator (or an orchestrator at acceptance) runs on
    a live program without changing anything: no job, no write, no config.
    It is also the exhaustive counterpart to the ``extraction_quality_suspect``
    doctor check, which samples by design -- so ``--all`` with no ``--sample``
    really does measure every document, and says how many that was.
    """
    doc_id = getattr(args, "doc_id", None)
    all_docs = bool(getattr(args, "all_docs", False))
    if bool(doc_id) == all_docs:
        return error_envelope(
            "ingest.quality",
            "usage",
            "pass exactly one of --doc-id (one document) or --all (the corpus)",
        )
    # Every flag that describes a CORPUS pass is refused beside --doc-id
    # rather than silently ignored -- an ignored flag is an operator who
    # believes they asked for something (fix pass V-7 added --worst, which
    # was the one hole in this guard).
    corpus_flags = [
        name
        for name, value in (
            ("--sample", args.sample),
            ("--seed", args.seed),
            ("--worst", getattr(args, "worst", None)),
        )
        if value is not None
    ]
    if not all_docs and corpus_flags:
        many = len(corpus_flags) > 1
        return error_envelope(
            "ingest.quality",
            "usage",
            f"{'/'.join(corpus_flags)} {'describe' if many else 'describes'} a corpus pass; "
            f"{'they apply' if many else 'it applies'} to --all, not to --doc-id",
        )
    if args.sample is not None and args.sample < 1:
        return error_envelope(
            "ingest.quality",
            "usage",
            f"--sample {args.sample} measures nothing -- pass a positive count, or --all with no --sample "
            "to measure every document",
        )

    store, program_root, err = _open(args, "ingest.quality")
    if err is not None:
        return err
    try:
        thresholds = quality.thresholds_from_config(_load_program_config(program_root) if program_root else None)
        if doc_id:
            row = _document_quality(store, program_root, doc_id)
            next_actions = []
            if row["suspect"]:
                next_actions.append(
                    next_action(
                        ["trialerror", "ingest", "status", "--doc-id", doc_id],
                        "the document's pipeline chain and its provenance, to see where this text came from",
                    )
                )
            return ok_envelope("ingest.quality", result={"document": row}, next_actions=next_actions)

        corpus_size = len(quality.corpus_doc_ids(store))
        rows = quality.measure_corpus(store, sample=args.sample, seed=args.seed)
        worst_n = args.worst if args.worst is not None else thresholds["worst_n"]
        ranked = quality.worst_first(rows, thresholds=thresholds)
        suspects = [r for r in ranked if r["suspect"]]
        by_measure = {
            measure: sum(1 for r in suspects if any(reason.startswith(measure) for reason in r["reasons"]))
            for measure in quality.MEASURE_KEYS
        }
        result = {
            "corpus_documents": corpus_size,
            "measured": len(rows),
            "measurable": sum(1 for r in rows if r.get("measurable")),
            "sample": args.sample,
            "seed": args.seed if args.sample is not None else None,
            "suspect_count": len(suspects),
            # Measured but never judged: below `[ingest.quality] min_tokens`
            # the four denominators say nothing about extraction, so these
            # rows are reported under their own name rather than folded into
            # a suspect count of zero that looks like a clean corpus.
            "below_min_tokens": sum(1 for r in ranked if r.get("below_min_tokens")),
            "suspect_by_measure": by_measure,
            "thresholds": thresholds,
            "worst": ranked[: max(0, int(worst_n))],
        }
        next_actions = []
        if suspects:
            next_actions.append(
                next_action(
                    ["trialerror", "ingest", "status", "--doc-id", suspects[0]["doc_id"]],
                    "the worst document's pipeline chain -- read the text before deciding anything",
                )
            )
        return ok_envelope("ingest.quality", result=result, next_actions=next_actions)
    except (IngestError, StoreError) as exc:
        return error_envelope("ingest.quality", type(exc).__name__, str(exc))
    finally:
        store.close()


def _cmd_status(args: argparse.Namespace) -> dict:
    store, program_root, err = _open(args, "ingest.status")
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
        # A retracted document's row is deliberately still here, and its
        # zero counts would otherwise be indistinguishable from a document
        # whose pipeline never ran. The retraction record says which.
        from trialerror.ingest.retract import retraction_record

        retraction = retraction_record(store.knowledge, args.doc_id)

        # Two ADDITIVE keys (feedback dispositions D-FB-1/D-FB-8). Every
        # key above is unchanged and still present, because an envelope
        # shape is a contract with the MCP tools and every existing caller:
        # this answers two more questions, it does not re-answer any old
        # one. Both degrade rather than raise -- `status` is the command an
        # operator runs precisely when something is wrong, so it is the
        # last command in the system allowed to be the thing that breaks.
        result = {
            "document": doc,
            "counts": counts,
            "retracted": retraction is not None,
            "retraction": retraction,
        }
        try:
            result["quality"] = _document_quality(store, program_root, args.doc_id)
        except Exception as exc:  # noqa: BLE001 - see the comment above
            result["quality"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            result["pipeline"] = pipeline_status.document_pipeline(store, args.doc_id, document=doc)
        except Exception as exc:  # noqa: BLE001
            result["pipeline"] = {"error": f"{type(exc).__name__}: {exc}"}

        next_actions = []
        argv = (result.get("pipeline") or {}).get("next_argv")
        if argv:
            next_actions.append(next_action(argv, (result["pipeline"] or {}).get("reason")))
        # FB-1 item F10c: one more, and only for a state that earns it. A
        # document this program's own thresholds call suspect is one whose
        # numbers the operator should read before trusting anything derived
        # from its text -- and `ingest quality --doc-id` is the read-only verb
        # that prints them with the thresholds they were judged against.
        if (result.get("quality") or {}).get("suspect"):
            next_actions.append(
                next_action(
                    ["trialerror", "ingest", "quality", "--doc-id", args.doc_id],
                    "this document measures suspect: read the four numbers against the thresholds "
                    "they were judged by",
                )
            )
        return ok_envelope("ingest.status", result=result, next_actions=next_actions)
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
