"""``trialerror lit`` -- the ``trialerror.litapi`` CLI surface (C-0064 litapi-preview
build). Design brief: "its own CLI group (``trialerror lit {lookup,citations,
search}``) returning ``trialerror.util.envelope`` results."

Registration rule (repo-wide convention, ``trialerror/cli/__init__.py``'s own
docstring): this module lives at ``trialerror/cli/lit.py`` and is
auto-discovered by ``trialerror.cli.discover_groups`` -- adding it never
touched ``trialerror/cli/__init__.py``.

No ``Store``/program-scaffold write path here for ``lookup``/``citations``/
``search`` (unlike ``trialerror/cli/ingest.py``): those three do no persistence
(see ``trialerror/litapi/__init__.py``'s "v1 wiring seams" note) --
``--program-root`` is used ONLY to locate an optional ``trialerror.toml``
``[litapi]`` config section, exactly like ``trialerror/cli/ingest.py``'s own
``_load_program_config`` helper, reimplemented here rather than imported
(that helper is a private, underscore-prefixed function local to
``ingest.py``, not a shared utility -- this module's own copy is the
lane-isolation-respecting choice over reaching into another CLI group
module).

**v3-acquisition build (C-0064 flags F1/F2 RESOLVED) adds ``acquire``** --
the one command in this group that DOES open a ``Store`` and write:
``trialerror lit acquire --doi <doi>|--arxiv <id> --launch-id <launch>`` is the
CLI face of the litapi-preview module's own documented M7 wiring seam
(``trialerror/litapi/__init__.py``), made real via the new
``trialerror.ingest.acquire`` module -- this file calls that module, it does
not reimplement the acquisition logic itself.

**build-arxiv-kaggle-index session adds ``arxiv-index build``/
``arxiv-semantic``** -- the CLI face of :mod:`trialerror.arxiv_index` (the
standalone all-arXiv semantic search index; see that package's own
docstring for the full architecture). ``arxiv-index build`` DOES open a
``Store`` (the jobs ledger a build's resumability rides -- see
``trialerror/arxiv_index/handlers.py``'s own TRIALERROR-DEV-NOTE on why the job
rides ``kind='custom'``); ``arxiv-semantic`` opens the standalone index db
directly (:mod:`trialerror.arxiv_index.store`), never the program's
``knowledge.db``, since this index is deliberately not one of the four
Section-3.2 program stores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from trialerror.ingest.errors import IngestError
from trialerror.litapi.client import LitApiClient, build_default_providers
from trialerror.litapi.config import load_litapi_config, resolve_api_key
from trialerror.litapi.errors import AllProvidersFailedError, LitApiError
from trialerror.stores.errors import StoreError
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

GROUP_NAME = "lit"
HELP = "Literature metadata lookup + acquisition: redundant OpenAlex + Semantic Scholar + arXiv + Unpaywall client."


def _all_failures_transport_unreachable(failures: list[dict]) -> bool:
    """True when every recorded per-provider failure was a genuine
    transport-level unreachability -- ``trialerror.litapi.providers.base.get_with_retry``
    wraps a raw ``URLError``/socket timeout/``ConnectionError`` into
    ``ProviderTransportError(host=..., scheme=...)`` and
    ``trialerror.litapi.client``'s own ``_failure_entry`` helper records that
    (and ONLY that) case with ``code == "transport_unreachable"`` -- never a
    bad HTTP status or a legitimate not-found. An empty ``failures`` list is
    NOT all-transport-unreachable (there is nothing to be unreachable)."""
    return bool(failures) and all(f.get("code") == "transport_unreachable" for f in failures)


def _litapi_error_code_and_details(exc: LitApiError) -> tuple[str, dict | None]:
    """Shared ``lookup``/``citations``/``search``/``acquire`` mapping: an
    :class:`~trialerror.litapi.errors.AllProvidersFailedError` caused ENTIRELY
    by transport-unreachable failures (see
    :func:`_all_failures_transport_unreachable`) is reported with the more
    actionable ``transport_unreachable`` code instead of the generic
    exception-class-name one (design brief Section 5.1: \"errors returned as
    structured content ... never exceptions\" -- following the same
    ``except OSError as exc: return error_envelope(cmd, \"<code>\", ...)``
    pattern ``trialerror/cli/jobs.py``'s ``_cmd_kick`` uses for its own
    transport-adjacent failure). Every other ``LitApiError`` keeps the
    pre-existing generic mapping unchanged."""
    if isinstance(exc, AllProvidersFailedError):
        failures = exc.details.get("failures", [])
        if _all_failures_transport_unreachable(failures):
            return "transport_unreachable", {"failures": failures}
    return type(exc).__name__, getattr(exc, "details", None)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="lit_cmd", metavar="<command>", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE, landed after this
        # group but applying uniformly): default=SUPPRESS so an unset
        # value here never overwrites the global --program-root the
        # top-level parser resolved.
        p.add_argument(
            "--program-root", default=argparse.SUPPRESS,
            help="program scaffold root to read trialerror.toml's [litapi] section from "
                 "(default: discovered from CWD; a missing trialerror.toml is not an error -- "
                 "conservative built-in defaults apply)",
        )

    p_lookup = sub.add_parser("lookup", help="reconciled metadata lookup by DOI or arXiv id")
    _common(p_lookup)
    id_group = p_lookup.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--doi", default=None)
    id_group.add_argument("--arxiv", default=None, dest="arxiv_id")
    p_lookup.set_defaults(handler=_cmd_lookup)

    p_citations = sub.add_parser("citations", help="papers citing a given DOI/arXiv id/provider paper id")
    _common(p_citations)
    p_citations.add_argument("--id", required=True, dest="identifier", help="DOI, arXiv id, or provider-native paper id")
    p_citations.add_argument("--limit", type=int, default=20)
    p_citations.add_argument("--offset", type=int, default=0)
    p_citations.set_defaults(handler=_cmd_citations)

    p_search = sub.add_parser(
        "search",
        help="reconciled search across providers -- each provider searches its OWN way: title only on "
             "OpenAlex (filter=title.search), all fields on arXiv (search_query=all:), relevance on "
             "Semantic Scholar. The query is passed through verbatim to each; the result reports the "
             "scope per provider beside providers_succeeded",
    )
    _common(p_search)
    p_search.add_argument(
        "--query", required=True,
        help="passed to every provider verbatim -- read the per-provider scope above before comparing "
             "their hit counts (a title-only provider returning nothing is not evidence the paper is absent)",
    )
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(handler=_cmd_search)

    p_acquire = sub.add_parser(
        "acquire",
        help="resolve metadata + a legal OA pdf (Unpaywall/arXiv only), download, and register+ingest "
             "-- or file a `wanted` request-queue row when no legal OA copy exists",
    )
    _common(p_acquire)
    p_acquire.add_argument(
        "--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)"
    )
    acquire_id_group = p_acquire.add_mutually_exclusive_group(required=True)
    acquire_id_group.add_argument("--doi", default=None)
    acquire_id_group.add_argument("--arxiv", default=None, dest="arxiv_id")
    p_acquire.add_argument("--launch-id", required=True, dest="launch_id")
    p_acquire.add_argument("--yes", action="store_true", help="proceed past the ingest cost gate")
    p_acquire.set_defaults(handler=_cmd_acquire)

    p_arxiv_index = sub.add_parser(
        "arxiv-index", help="build/inspect the standalone all-arXiv semantic search index (trialerror.arxiv_index)"
    )
    _common(p_arxiv_index)
    arxiv_index_sub = p_arxiv_index.add_subparsers(dest="arxiv_index_cmd", metavar="<command>", required=True)

    p_ai_build = arxiv_index_sub.add_parser(
        "build", help="stream-ingest the Kaggle openai-arxiv-embeddings zip into the standalone index (resumable)"
    )
    _common(p_ai_build)
    p_ai_build.add_argument(
        "--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)"
    )
    p_ai_build.add_argument("--zip", required=True, dest="zip_path", help="path to the downloaded Kaggle zip")
    p_ai_build.add_argument("--db-path", default=None, dest="db_path", help="override [litapi.arxiv_index].db_path")
    p_ai_build.add_argument("--dims", type=int, default=None)
    p_ai_build.add_argument("--batch-size", type=int, default=None, dest="batch_size")
    p_ai_build.add_argument("--member-glob", default=None, dest="member_glob")
    p_ai_build.add_argument("--min-free-gb", type=float, default=None, dest="min_free_gb")
    p_ai_build.add_argument(
        "--job-id", default=None, dest="job_id",
        help="target a specific job id (default: deterministic from --zip's path, so re-running the "
        "same command resumes the same job after a kill/crash)",
    )
    p_ai_build.add_argument("--launch-id", default=None, dest="launch_id")
    p_ai_build.add_argument(
        "--detach", action="store_true",
        help="spawn a detached background worker instead of running in this process (real ~34.9GB "
        "builds can run for hours; default runs in-process, Ctrl+C-able, resumable by re-running)",
    )
    p_ai_build.set_defaults(handler=_cmd_arxiv_index_build)

    p_arxiv_semantic = sub.add_parser(
        "arxiv-semantic", help="semantic search the standalone all-arXiv index (native sqlite-vec MATCH)"
    )
    _common(p_arxiv_semantic)
    p_arxiv_semantic.add_argument("--q", required=True, dest="query")
    p_arxiv_semantic.add_argument("--k", type=int, default=10)
    p_arxiv_semantic.set_defaults(handler=_cmd_arxiv_semantic)

    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if args.program_root:
        return Path(args.program_root)
    return find_program_root()


def _load_program_config_raw(program_root: Path | None) -> dict:
    if program_root is None:
        return {}
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


def _build_client(args: argparse.Namespace) -> LitApiClient:
    program_root = _resolve_program_root(args)
    litapi_cfg = load_litapi_config(_load_program_config_raw(program_root))
    providers = build_default_providers(litapi_cfg, program_root=program_root)
    return LitApiClient(providers)


def _cmd_lookup(args: argparse.Namespace) -> dict:
    client = _build_client(args)
    try:
        if args.doi:
            result = client.lookup_doi(args.doi)
        else:
            result = client.lookup_arxiv(args.arxiv_id)
        return ok_envelope("lit.lookup", result=result.to_dict())
    except LitApiError as exc:
        code, details = _litapi_error_code_and_details(exc)
        return error_envelope("lit.lookup", code, str(exc), details=details)


def _cmd_citations(args: argparse.Namespace) -> dict:
    client = _build_client(args)
    try:
        page = client.get_citations(args.identifier, limit=args.limit, offset=args.offset)
        return ok_envelope("lit.citations", result=page.to_dict())
    except LitApiError as exc:
        code, details = _litapi_error_code_and_details(exc)
        return error_envelope("lit.citations", code, str(exc), details=details)


def _search_next_actions(args: argparse.Namespace, result) -> list:
    """Two states, two actions, nothing else (FB-1 item F10c).

    A provider that FAILED is a configuration or reachability question, and
    the check that answers it is the one the setup guide already points at.
    Zero records across every provider that DID answer is a different
    question -- the providers searched their own narrow ways (see
    ``provider_query_scope``), and the local all-arXiv index is the one
    surface here that does neither a title filter nor a keyword match. A
    search that returned records earns no action."""
    actions = []
    if result.providers_failed:
        failed = ", ".join(sorted({str(f.get("provider")) for f in result.providers_failed}))
        actions.append(
            next_action(
                ["trialerror", "doctor", "--only", "litapi_providers_ready"],
                f"provider(s) failed ({failed}): check keys, email identification and reachability",
            )
        )
    elif not result.records:
        actions.append(
            next_action(
                ["trialerror", "lit", "arxiv-semantic", "--q", args.query],
                "no provider returned a record: try the local all-arXiv semantic index, which "
                "matches meaning rather than a title filter",
            )
        )
    return actions


def _cmd_search(args: argparse.Namespace) -> dict:
    client = _build_client(args)
    try:
        result = client.search(args.query, limit=args.limit)
        return ok_envelope(
            "lit.search", result=result.to_dict(), next_actions=_search_next_actions(args, result)
        )
    except LitApiError as exc:
        code, details = _litapi_error_code_and_details(exc)
        return error_envelope("lit.search", code, str(exc), details=details)


def _cmd_acquire(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "lit.acquire", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )

    from trialerror.stores.store import open_store

    store = open_store(program_root, platform_root=getattr(args, "platform_root", None))
    try:
        raw_config = _load_program_config_raw(program_root)
        litapi_cfg = load_litapi_config(raw_config)

        from trialerror.ingest.acquire import acquire as run_acquire

        result = run_acquire(
            store, program_root=program_root, doi=args.doi, arxiv_id=args.arxiv_id,
            created_by_launch=args.launch_id, litapi_config=litapi_cfg, config=raw_config, yes=args.yes,
        )

        # trialerror.ingest.acquire.acquire tolerates a total metadata-lookup
        # failure silently (module docstring: "a total metadata-lookup
        # failure does NOT abort acquisition") and files a `wanted`
        # request-queue row instead -- correct when providers genuinely have
        # no record, but misleading when EVERY provider failed because none
        # could be REACHED at all (getattr, not a direct attribute access:
        # AcquireResult always carries these two fields, but a caller's own
        # test stub may not -- see tests/test_litapi_cli.py's _FakeAcquireResult).
        # Surface that distinctly rather than silently queuing a request no
        # human asked for over what is very likely an egress/DNS problem.
        metadata_providers = getattr(result, "metadata_providers", None)
        metadata_failures = getattr(result, "metadata_failures", None) or []
        if (
            result.outcome == "queued"
            and not metadata_providers
            and _all_failures_transport_unreachable(metadata_failures)
        ):
            return error_envelope(
                "lit.acquire", "transport_unreachable",
                "metadata reconciliation reached no configured provider (transport-level failure on "
                "every one) -- filed no request-queue row; this is very likely an egress/DNS problem, "
                "not \"no open-access copy exists\"",
                details={"failures": metadata_failures},
            )

        next_actions = []
        if result.outcome == "acquired" and result.job:
            # FB-1 item F4: "acquired" is not "searchable". The result says so
            # in two keys and these two actions say what to do about it -- the
            # second is the small-batch answer (one document, one job, run it
            # here), mirroring `ingest add` word for word from the one module
            # that owns the sentence.
            from trialerror.ingest import pipeline_status

            next_actions.extend(
                pipeline_status.not_yet_searchable_next_actions(result.job, next_action=next_action)
            )
        elif result.outcome == "queued":
            next_actions.append(
                next_action(["trialerror", "ingest", "requests-md", "--program-root", str(program_root)],
                            "re-render requests/REQUESTS.md for the human-fulfillment queue")
            )
        # FB-1 item F10a, mirrored word for word from `ingest add` (same
        # helper): an acquisition that routes to OCR is about to have its
        # pages read by whatever [ingest.ocr] names, and an absent table
        # names the stand-in. In the envelope, never on stderr.
        from trialerror.ingest.backends import fake_stage_backend_warning

        warning = fake_stage_backend_warning(getattr(result, "stage_backend", None))
        return ok_envelope(
            "lit.acquire",
            result=result.to_dict(),
            warnings=[warning] if warning else None,
            next_actions=next_actions,
        )
    except ValueError as exc:  # cost-gate refusal (mirrors trialerror/cli/ingest.py's own _cmd_add handling)
        return error_envelope("lit.acquire", "cost_gate_refused", str(exc), next_actions=[
            next_action(
                ["trialerror", "lit", "acquire", "--doi" if args.doi else "--arxiv", args.doi or args.arxiv_id,
                 "--launch-id", args.launch_id, "--yes"],
                "proceed past the cost gate",
            )
        ])
    except (LitApiError, IngestError, StoreError) as exc:
        if isinstance(exc, LitApiError):
            code, details = _litapi_error_code_and_details(exc)
        else:
            code, details = type(exc).__name__, getattr(exc, "details", None)
        return error_envelope("lit.acquire", code, str(exc), details=details)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# arxiv-index build / arxiv-semantic (build-arxiv-kaggle-index session,
# trialerror.arxiv_index -- the standalone all-arXiv semantic search index)
# ---------------------------------------------------------------------------


def _resolve_arxiv_db_path(program_root: Path, litapi_cfg) -> Path:
    p = Path(litapi_cfg.arxiv_index.db_path)
    return p if p.is_absolute() else program_root / p


def _cmd_arxiv_index_build(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "lit.arxiv-index.build", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )

    zip_path = Path(args.zip_path)
    if not zip_path.is_file():
        return error_envelope("lit.arxiv-index.build", "zip_not_found", f"no such file: {zip_path}")

    litapi_cfg = load_litapi_config(_load_program_config_raw(program_root))
    db_path = Path(args.db_path) if args.db_path else _resolve_arxiv_db_path(program_root, litapi_cfg)
    dims = args.dims if args.dims is not None else litapi_cfg.arxiv_index.dims
    batch_size = args.batch_size if args.batch_size is not None else litapi_cfg.arxiv_index.batch_size
    member_glob = args.member_glob if args.member_glob is not None else litapi_cfg.arxiv_index.member_glob
    min_free_gb = args.min_free_gb if args.min_free_gb is not None else litapi_cfg.arxiv_index.min_free_gb

    # Deterministic default job_id (from the zip's resolved path) so
    # re-running the SAME command after a kill/crash resumes the SAME job
    # row (trialerror.jobs.worker.run_one's job_id path is claim-OR-create --
    # an existing row's own persisted checkpoint/payload wins, a fresh
    # payload here is only used the first time this job_id is seen).
    job_id = args.job_id or f"JOB-arxiv-index-build-{hashlib.sha256(str(zip_path.resolve()).encode('utf-8')).hexdigest()[:16]}"
    payload = {
        "handler": "arxiv_index_build",
        "zip_path": str(zip_path),
        "db_path": str(db_path),
        "dims": dims,
        "batch_size": batch_size,
        "member_glob": member_glob,
        "min_free_gb": min_free_gb,
        "created_by_launch": args.launch_id,
    }

    from trialerror.stores.store import open_store

    platform_root = getattr(args, "platform_root", None)
    store = open_store(program_root, platform_root=platform_root)
    try:
        if args.detach:
            from trialerror.jobs.worker import spawn_worker

            handle = spawn_worker(
                program_root=program_root, platform_root=platform_root, job_id=job_id, kind="custom",
                payload=payload, mode="once",
            )
            return ok_envelope(
                "lit.arxiv-index.build",
                result={"status": "spawned", "job_id": job_id, "pid": handle.pid, "log_path": str(handle.log_path)},
                next_actions=[next_action(["trialerror", "jobs", "logs", job_id], "tail the build's ledger event history")],
            )

        from trialerror.jobs import ledger
        from trialerror.jobs.errors import NotClaimableError
        from trialerror.jobs.registry import discover_and_register_handlers
        from trialerror.jobs.worker import make_worker_id, run_one

        discover_and_register_handlers()
        try:
            result = run_one(store, worker_id=make_worker_id(), job_id=job_id, kind="custom", payload=payload)
        except NotClaimableError:
            # Re-running the SAME command (deterministic job_id) after the
            # build already finished (or was abandoned) is a normal, honest
            # outcome, not a crash -- claim_or_create refuses to reclaim a
            # terminal job (trialerror.jobs.ledger's state machine: 'complete'/
            # 'abandoned' are both terminal). Report the existing job's own
            # settled state instead of a raw error.
            existing = ledger.get_job(store, job_id)
            status = existing["state"] if existing else "unknown"
            result = {"status": f"already-{status}", "job_id": job_id}

        job_row = ledger.get_job(store, job_id)
        checkpoint = json.loads(job_row["checkpoint"]) if job_row and job_row.get("checkpoint") else None

        next_actions = []
        if result["status"] in ("failed", "deferred", "paused"):
            next_actions.append(
                next_action(
                    ["trialerror", "lit", "arxiv-index", "build", "--zip", str(zip_path), "--job-id", job_id],
                    "resume the build (same job id -> same checkpoint, no rework of already-committed rows)",
                )
            )
        return ok_envelope("lit.arxiv-index.build", result={**result, "job_id": job_id, "checkpoint": checkpoint}, next_actions=next_actions)
    finally:
        store.close()


def _build_query_encoder(litapi_cfg, program_root: Path | None):
    """Factory for the real query-time encoder. A module-level function
    (not inlined into :func:`_cmd_arxiv_semantic`) specifically so tests
    can monkeypatch ``trialerror.cli.lit._build_query_encoder`` to inject a
    :class:`~trialerror.arxiv_index.encoder.FakeQueryEncoder` instead of making
    a live OpenAI call (build brief item 4: "tests use a fake encoder")."""
    from trialerror.arxiv_index.encoder import OpenAIQueryEncoder

    key = resolve_api_key(litapi_cfg.arxiv_index, program_root=program_root)
    if not key:
        raise ValueError(
            "no OpenAI API key configured -- set [litapi.arxiv_index].api_key_path in trialerror.toml to a "
            "file holding your OpenAI API key (query-time embedding only; the corpus vectors in the "
            "downloaded dataset are already precomputed, this package never re-embeds them)"
        )
    return OpenAIQueryEncoder(api_key=key)


def _cmd_arxiv_semantic(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "lit.arxiv-semantic", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )

    litapi_cfg = load_litapi_config(_load_program_config_raw(program_root))
    db_path = _resolve_arxiv_db_path(program_root, litapi_cfg)
    if not db_path.is_file():
        return error_envelope(
            "lit.arxiv-semantic", "index_not_built", f"no arxiv semantic index db at {db_path}",
            next_actions=[
                next_action(["trialerror", "lit", "arxiv-index", "build", "--zip", "<path to Kaggle zip>"], "build the index first")
            ],
        )

    try:
        encoder = _build_query_encoder(litapi_cfg, program_root)
    except ValueError as exc:
        return error_envelope("lit.arxiv-semantic", "no_api_key", str(exc))

    from trialerror.arxiv_index.encoder import estimate_query_cost_usd
    from trialerror.arxiv_index.query import semantic_search
    from trialerror.arxiv_index.store import open_arxiv_index_db

    conn = open_arxiv_index_db(db_path)
    try:
        query_vector = encoder.encode_query(args.query)
        results = semantic_search(conn, query_vector, k=args.k)
    except Exception as exc:  # noqa: BLE001 - deliberate: surface as a clean envelope, not a raw traceback
        return error_envelope("lit.arxiv-semantic", type(exc).__name__, str(exc))
    finally:
        conn.close()

    return ok_envelope(
        "lit.arxiv-semantic",
        result={
            "query": args.query,
            "k": args.k,
            "estimated_cost_usd": round(estimate_query_cost_usd(args.query), 8),
            "results": [r.to_dict() for r in results],
        },
    )
