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

**lane SI part B adds ``investigate run|verdict|render``** -- the CLI face of
:mod:`trialerror.litapi.investigate` (the source investigator: vet a list of
cited works before any reaches a human's request queue). ``run`` opens a
``Store`` and writes ``source_evidence``/``source_dossier`` rows plus one JSON
dossier per seed; ``verdict`` records a verdict on one dossier; ``render``
prints the operator lines for one list. The handlers here only wire the
module's functions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from trialerror.arxiv_index.query import MAX_BATCH_QUERIES
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


#: Where each provider's cross-invocation pacing stamp lives under a program
#: root (lane FB-acq item 2). One small JSON file per provider; ``data/`` is
#: the same gitignored, clearly-disposable directory the arXiv index already
#: uses. Without a program root there is nowhere to put it and the limiters
#: stay in-memory-only, exactly as before.
PACING_DIR_RELPATH = "data/litapi_pacing"


def _pacing_dir(program_root: Path | None) -> Path | None:
    return (program_root / PACING_DIR_RELPATH) if program_root is not None else None


def _rate_limited_next_action(po: dict) -> list:
    """The one action a rate-limited lookup earns. Names which providers were
    throttled AND which of them went out without a key, because those are two
    different fixes and the envelope should not make the caller guess which
    one this was."""
    limited = sorted(name for name, o in po.items() if o.get("outcome") == "rate_limited")
    keyless = sorted(name for name in limited if not po[name].get("keyed"))
    waits = [o.get("retry_after_s") for o in po.values() if o.get("retry_after_s") is not None]
    when = f"retry after {max(waits):g}s" if waits else "retry later"
    return [
        next_action(
            ["trialerror", "doctor", "--only", "litapi_providers_ready"],
            f"rate-limited ({', '.join(limited)}); keyless providers: "
            f"{', '.join(keyless) if keyless else 'none'} -- configure a key file or {when}",
        )
    ]


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
    transport-adjacent failure).

    Lane FB-acq item 2 reads ``details["provider_outcomes"]``, which carries an
    entry for EVERY provider asked rather than only the ones that raised, and
    so can tell three cases the old mapping collapsed into one generic code:
    every provider unreachable (today's code, unchanged), every provider
    genuinely without the record, and at least one provider rate-limited with
    the rest merely not having it. First match wins; anything else keeps the
    pre-existing generic exception-class-name code."""
    if isinstance(exc, AllProvidersFailedError):
        failures = exc.details.get("failures", [])
        po = exc.details.get("provider_outcomes") or {}
        details = {"failures": failures, "provider_outcomes": po}
        if _all_failures_transport_unreachable(failures):
            return "transport_unreachable", details if po else {"failures": failures}
        words = [o.get("outcome") for o in po.values()]
        if words and all(w == "transport_unreachable" for w in words):
            return "transport_unreachable", details
        if words and all(w == "not_found" for w in words):
            return "record_not_found", details
        if "rate_limited" in words and all(w in ("rate_limited", "not_found") for w in words):
            return "rate_limited", details
        if po:
            return type(exc).__name__, details
    return type(exc).__name__, getattr(exc, "details", None)


def _litapi_error_envelope(command: str, exc: LitApiError) -> dict:
    """Every ``lit`` subcommand's shared error path: the code/details mapping
    above, plus the one next action a ``rate_limited`` verdict earns."""
    code, details = _litapi_error_code_and_details(exc)
    actions = None
    if code == "rate_limited":
        actions = _rate_limited_next_action((details or {}).get("provider_outcomes") or {})
    return error_envelope(command, code, str(exc), details=details, next_actions=actions)


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
    query_group = p_arxiv_semantic.add_mutually_exclusive_group(required=True)
    query_group.add_argument(
        "--q", dest="query", default=None,
        help="one query. Note that vec0 has no approximate index, so EVERY call is a full scan of "
             "the index -- issue many queries with --q-file instead, which costs one scan for all of them",
    )
    query_group.add_argument(
        "--q-file", dest="q_file", default=None,
        help=f"a UTF-8 file of queries, one per line (blank lines skipped, at most "
             f"{MAX_BATCH_QUERIES}): all of them answered in ONE pass over the index",
    )
    p_arxiv_semantic.add_argument("--k", type=int, default=10)
    p_arxiv_semantic.set_defaults(handler=_cmd_arxiv_semantic)

    _register_investigate(sub, _common)

    return parser


def _register_investigate(sub: argparse._SubParsersAction, common) -> None:
    """``lit investigate run|verdict|render`` (lane SI part B). The logic is
    :mod:`trialerror.litapi.investigate`; these handlers only wire it."""
    from trialerror.litapi.investigate import VERDICTS

    p_investigate = sub.add_parser(
        "investigate",
        help="vet cited works before they reach a request queue: does each cited identifier resolve to the "
             "cited work, is it already held, who cites it, did a review consolidate it (dossiers + verdicts)",
    )
    common(p_investigate)
    inv_sub = p_investigate.add_subparsers(dest="investigate_cmd", metavar="<command>", required=True)

    def _platform(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)"
        )

    p_run = inv_sub.add_parser(
        "run",
        help="investigate every seed of a JSONL seeds file: resolve, match, held check, gather citing works, "
             "reviews, references and the authors' recent works; one JSON dossier per seed plus a source_dossier "
             "row. Provider answers are cached in source_evidence (rate limits and 5xx are always re-asked)",
    )
    common(p_run)
    _platform(p_run)
    p_run.add_argument(
        "--seeds-file", required=True, dest="seeds_file",
        help='JSONL, one object per seed: {"list_id", "row_id", "seed_raw", "question", "literature"} '
             '(or "seeds_raw" for a whole row, split on top-level ";")',
    )
    p_run.add_argument("--out-dir", required=True, dest="out_dir", help="dossiers go to <out-dir>/<list_id>/<row_id>/")
    p_run.add_argument("--launch-id", required=True, dest="launch_id")
    p_run.add_argument(
        "--max-calls-per-seed", type=int, default=None, dest="max_calls_per_seed",
        help="client-level lookups one seed may make, cache hits included "
             "(default: [litapi.investigate].max_calls_per_seed, 8)",
    )
    p_run.add_argument(
        "--resume", action="store_true", help="skip every seed whose dossier exists in a state other than retry"
    )
    p_run.add_argument(
        "--no-cache", action="store_true", dest="no_cache",
        help="ask every provider again (answers are still recorded, retiring the cached ones)",
    )
    p_run.add_argument(
        "--arxiv-neighbours", action="store_true", dest="arxiv_neighbours",
        help="after all seeds, one pass over the local arXiv index with every distinct question (k=10); "
             "skipped with a warning when the index or its query key is absent",
    )
    p_run.add_argument(
        "--delivered-manifest", default=None, dest="delivered_manifest",
        help="a TSV (header: path, title, doi, isbn) of delivered files the held check also consults",
    )
    p_run.set_defaults(handler=_cmd_investigate_run)

    p_verdict = inv_sub.add_parser(
        "verdict",
        help="record a verdict on one dossier (row + file); refused when the dossier does not support it",
    )
    common(p_verdict)
    _platform(p_verdict)
    p_verdict.add_argument("--dossier", required=True, dest="dossier", help="the dossier JSON file")
    p_verdict.add_argument("--verdict", required=True, dest="verdict", choices=list(VERDICTS))
    substitute_group = p_verdict.add_mutually_exclusive_group()
    substitute_group.add_argument("--substitute-doi", default=None, dest="substitute_doi")
    substitute_group.add_argument("--substitute-arxiv", default=None, dest="substitute_arxiv")
    substitute_group.add_argument("--substitute-isbn", default=None, dest="substitute_isbn")
    p_verdict.add_argument("--reason-code", default=None, dest="reason_code")
    p_verdict.add_argument(
        "--detail-json", default=None, dest="detail_json",
        help="a JSON object file: why, question, founds, consolidator, foundational_reason, ...",
    )
    p_verdict.add_argument(
        "--supersede", action="store_true", help="replace an existing verdict (kept in the detail's history)"
    )
    p_verdict.add_argument("--launch-id", required=True, dest="launch_id")
    p_verdict.set_defaults(handler=_cmd_investigate_verdict)

    p_render = inv_sub.add_parser(
        "render",
        help="the operator lines for one list: FETCH / FETCH-FOUNDATIONAL / ASK per judged seed; refused while any "
             "row holds a wrong-identifier seed or a seed nothing was tried for",
    )
    common(p_render)
    _platform(p_render)
    p_render.add_argument("--list-id", required=True, dest="list_id")
    # dest render_format: the top-level parser already owns ``format`` (json|text, the envelope's own form)
    p_render.add_argument("--format", default="lines", choices=["lines", "json"], dest="render_format")
    p_render.set_defaults(handler=_cmd_investigate_render)


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
    providers = build_default_providers(
        litapi_cfg, program_root=program_root, pacing_dir=_pacing_dir(program_root)
    )
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
        return _litapi_error_envelope("lit.lookup", exc)


def _cmd_citations(args: argparse.Namespace) -> dict:
    client = _build_client(args)
    try:
        page = client.get_citations(args.identifier, limit=args.limit, offset=args.offset)
        return ok_envelope("lit.citations", result=page.to_dict())
    except LitApiError as exc:
        return _litapi_error_envelope("lit.citations", exc)


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
        return _litapi_error_envelope("lit.search", exc)


def _oa_unresolved_envelope(args: argparse.Namespace, oa_legs: list[dict], metadata_failures: list[dict]) -> dict:
    """The ``oa_resolution_unresolved`` refusal (F18). Computed from the leg
    DICTS rather than the :class:`~trialerror.ingest.acquire.OAAttempt` object so
    a caller's own stub result (which carries plain dicts) reports identically.

    Two next actions, never both: the same command again when a retry can
    plausibly succeed (a 429, an unreachable host, a 5xx), and the readiness
    check when it cannot (a 4xx will be answered the same way next time).
    """
    from trialerror.ingest.acquire import OA_UNRESOLVED_OUTCOMES

    unresolved = [leg for leg in oa_legs if leg.get("outcome") in OA_UNRESOLVED_OUTCOMES]
    rendered = []
    for leg in unresolved:
        text = f"{leg.get('provider')}={leg.get('outcome')}"
        if leg.get("status_code") is not None:
            text += f" HTTP {leg['status_code']}"
        rendered.append(text)
    retryable = any(
        leg.get("outcome") in ("rate_limited", "transport_unreachable")
        or (leg.get("outcome") == "http_error" and (leg.get("status_code") or 0) >= 500)
        for leg in unresolved
    )
    waits = [leg["retry_after_s"] for leg in unresolved if leg.get("retry_after_s") is not None]
    retry_after_s = max(waits) if waits else None

    if retryable:
        reason = f"retry after {retry_after_s:g}s" if retry_after_s is not None else "retry later"
        actions = [
            next_action(
                ["trialerror", "lit", "acquire", "--doi" if args.doi else "--arxiv", args.doi or args.arxiv_id,
                 "--launch-id", args.launch_id],
                reason,
            )
        ]
    else:
        actions = [
            next_action(
                ["trialerror", "doctor", "--only", "litapi_providers_ready"],
                "the provider answered, and answered badly: check keys, email identification and base urls",
            )
        ]

    return error_envelope(
        "lit.acquire", "oa_resolution_unresolved",
        f"open-access resolution did not complete ({', '.join(rendered)}) -- no request-queue row was "
        "filed; this is a provider failure, not \"no open-access copy exists\"",
        details={"oa_legs": oa_legs, "retryable": retryable, "metadata_failures": metadata_failures},
        next_actions=actions,
    )


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
            pacing_dir=_pacing_dir(program_root),
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
        oa_legs = [dict(leg) for leg in (getattr(result, "oa_legs", None) or [])]
        nothing_reachable = not metadata_providers and _all_failures_transport_unreachable(metadata_failures)

        # F18: an open-access resolution that did not COMPLETE is its own
        # outcome -- trialerror.ingest.acquire wrote no row for it, and neither
        # the ok='queued' envelope (which would read as "no copy exists") nor a
        # traceback is an honest report of a 429.
        if result.outcome == "unresolved":
            if nothing_reachable:
                return error_envelope(
                    "lit.acquire", "transport_unreachable",
                    "metadata reconciliation reached no configured provider (transport-level failure on "
                    "every one) -- filed no request-queue row; this is very likely an egress/DNS problem, "
                    "not \"no open-access copy exists\"",
                    details={"failures": metadata_failures, "oa_legs": oa_legs},
                )
            return _oa_unresolved_envelope(args, oa_legs, metadata_failures)

        if result.outcome == "queued" and nothing_reachable:
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
            return _litapi_error_envelope("lit.acquire", exc)
        return error_envelope("lit.acquire", type(exc).__name__, str(exc), details=getattr(exc, "details", None))
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


def _read_query_file(path: Path) -> list[str]:
    """One query per line, surrounding whitespace stripped, blank lines skipped."""
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _encode_queries(encoder, queries: list[str]) -> list[list[float]]:
    """Use the encoder's batch method when it has one, else loop. The Protocol
    declares ``encode_queries`` and both shipped encoders implement it, but a
    caller's own injected encoder (a test fake, a future backend) may predate
    it, and a batch is not worth breaking one of those over."""
    batch = getattr(encoder, "encode_queries", None)
    if callable(batch):
        return [list(v) for v in batch(queries)]
    return [encoder.encode_query(q) for q in queries]


def _cmd_arxiv_semantic(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(
            "lit.arxiv-semantic", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD"
        )

    q_file = getattr(args, "q_file", None)
    if q_file:
        q_path = Path(q_file)
        if not q_path.is_file():
            return error_envelope("lit.arxiv-semantic", "q_file_not_found", f"no such file: {q_path}")
        queries = _read_query_file(q_path)
        if not queries:
            return error_envelope(
                "lit.arxiv-semantic", "q_file_empty", f"{q_path} holds no non-blank query lines"
            )
        if len(queries) > MAX_BATCH_QUERIES:
            return error_envelope(
                "lit.arxiv-semantic", "too_many_queries",
                f"{q_path} holds {len(queries)} queries; at most {MAX_BATCH_QUERIES} in one call "
                "(a batch is one pass over the index whatever its size, but the query matrix and the "
                "per-query candidate sets are linear in the batch and live in memory for that pass)",
            )
    else:
        queries = [args.query]

    raw_config = _load_program_config_raw(program_root)
    litapi_cfg = load_litapi_config(raw_config)
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
    from trialerror.arxiv_index.query import semantic_search_many
    from trialerror.arxiv_index.store import open_arxiv_index_db

    # The timing block is the operator's own before/after instrument: the real
    # index is tens of GB and disk-bound, and no synthetic fixture can predict
    # its wall clock. These three numbers can be read off a real run.
    t0 = time.perf_counter()
    conn = open_arxiv_index_db(db_path)
    t_open = time.perf_counter()
    try:
        vectors = _encode_queries(encoder, queries)
        t_encode = time.perf_counter()
        batch = semantic_search_many(conn, vectors, k=args.k, config=raw_config)
        t_search = time.perf_counter()
    except Exception as exc:  # noqa: BLE001 - deliberate: surface as a clean envelope, not a raw traceback
        return error_envelope("lit.arxiv-semantic", type(exc).__name__, str(exc))
    finally:
        conn.close()

    timing = {
        "open_s": round(t_open - t0, 3),
        "encode_s": round(t_encode - t_open, 3),
        "search_s": round(t_search - t_encode, 3),
        "total_s": round(t_search - t0, 3),
    }
    warnings = None
    if batch["scan_mode"] == "per_query" and len(queries) > 1:
        warnings = [
            "numpy is not available (or [retrieve] numpy_fastpath = \"off\"): "
            f"{len(queries)} queries ran as {len(queries)} full scans; install the 'fast' extra for one pass"
        ]

    if q_file:
        result = {
            "k": args.k,
            "n_queries": len(queries),
            "scan_mode": batch["scan_mode"],
            "passes": batch["passes"],
            "rows_scanned": batch["rows_scanned"],
            "backend": batch["backend"],
            "estimated_cost_usd": round(sum(estimate_query_cost_usd(q) for q in queries), 8),
            "timing": timing,
            "queries": [
                {"query": q, "results": [r.to_dict() for r in results]}
                for q, results in zip(queries, batch["results"])
            ],
        }
        return ok_envelope("lit.arxiv-semantic", result=result, warnings=warnings)

    # The single --q result is otherwise byte-identical to what it always was:
    # the same four keys in the same order, plus `timing`.
    return ok_envelope(
        "lit.arxiv-semantic",
        result={
            "query": args.query,
            "k": args.k,
            "estimated_cost_usd": round(estimate_query_cost_usd(args.query), 8),
            "results": [r.to_dict() for r in batch["results"][0]],
            "timing": timing,
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# investigate run / verdict / render (lane SI part B, trialerror.litapi.investigate)
# ---------------------------------------------------------------------------


def _investigate_providers(program_root: Path, litapi_cfg) -> list:
    """The providers ``investigate run`` asks -- the default pair, paced across
    invocations like every other ``lit`` verb. A module-level function so a
    test can hand the run providers over a ``FakeTransport`` instead."""
    return build_default_providers(litapi_cfg, program_root=program_root, pacing_dir=_pacing_dir(program_root))


def _investigate_neighbours(program_root: Path, litapi_cfg, raw_config: dict):
    """``(search, close, warning)`` for ``--arxiv-neighbours``: the search
    callable over the local arXiv index and the connection's closer, or
    ``(None, None, warning)`` when the index or its query key is absent -- the
    run then goes ahead without the pass and says why."""
    db_path = _resolve_arxiv_db_path(program_root, litapi_cfg)
    if not db_path.is_file():
        return None, None, f"--arxiv-neighbours skipped: no arXiv semantic index at {db_path}"
    try:
        encoder = _build_query_encoder(litapi_cfg, program_root)
    except ValueError as exc:
        return None, None, f"--arxiv-neighbours skipped: {exc}"
    from trialerror.arxiv_index.store import open_arxiv_index_db
    from trialerror.litapi.investigate import arxiv_neighbour_search

    conn = open_arxiv_index_db(db_path)
    return arxiv_neighbour_search(conn, encoder, config=raw_config), conn.close, None


def _investigate_error(command: str, exc) -> dict:
    return error_envelope(command, exc.code, str(exc), details=exc.details or None)


def _cmd_investigate_run(args: argparse.Namespace) -> dict:
    from trialerror.litapi.investigate import InvestigateError, load_delivered_manifest, load_seeds, run_investigation
    from trialerror.stores.store import open_store

    command = "lit.investigate.run"
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(command, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD")
    try:
        seeds, seed_warnings = load_seeds(args.seeds_file)
        manifest = load_delivered_manifest(args.delivered_manifest) if args.delivered_manifest else None
    except InvestigateError as exc:
        return _investigate_error(command, exc)
    raw_config = _load_program_config_raw(program_root)
    try:
        litapi_cfg = load_litapi_config(raw_config)
    except ValueError as exc:
        return error_envelope(command, "config_invalid", str(exc))

    warnings = list(seed_warnings)
    store = open_store(program_root, platform_root=getattr(args, "platform_root", None))
    close_index = None
    try:
        neighbours = None
        neighbours_skipped = None
        if args.arxiv_neighbours:
            neighbours, close_index, neighbours_skipped = _investigate_neighbours(program_root, litapi_cfg, raw_config)
            if neighbours_skipped:
                warnings.append(neighbours_skipped)
        result = run_investigation(
            store, _investigate_providers(program_root, litapi_cfg), seeds, config=litapi_cfg.investigate,
            out_dir=args.out_dir, launch_id=args.launch_id, resume=args.resume, use_cache=not args.no_cache,
            max_calls_per_seed=args.max_calls_per_seed, manifest=manifest, neighbours=neighbours,
        )
    except InvestigateError as exc:
        return _investigate_error(command, exc)
    except StoreError as exc:
        return error_envelope(command, type(exc).__name__, str(exc))
    finally:
        if close_index is not None:
            close_index()
        store.close()

    warnings.extend(result.pop("warnings", []))
    if args.arxiv_neighbours and neighbours is None:
        result["arxiv_neighbours"] = {"requested": True, "ran": False, "skipped": neighbours_skipped}
    next_actions = []
    if result["states"].get("retry"):
        argv = [
            "trialerror", "lit", "investigate", "run", "--program-root", str(program_root),
            "--seeds-file", str(Path(args.seeds_file).resolve()), "--out-dir", str(Path(args.out_dir).resolve()),
            "--launch-id", args.launch_id, "--resume",
        ]
        if args.max_calls_per_seed is not None:
            argv += ["--max-calls-per-seed", str(args.max_calls_per_seed)]
        if args.delivered_manifest:
            argv += ["--delivered-manifest", str(Path(args.delivered_manifest).resolve())]
        next_actions.append(
            next_action(argv, f"{result['states']['retry']} seed(s) could not be looked up (rate limit, unreachable "
                              "or 5xx): re-run them, and only them")
        )
    if result.get("evidence_incomplete"):
        warnings.append(
            f"{result['evidence_incomplete']} dossier(s) are missing gather evidence a provider could not give "
            "(rate limit, unreachable or 5xx); a later run without --resume re-asks only those calls"
        )
    return ok_envelope(command, result=result, warnings=warnings or None, next_actions=next_actions)


def _cmd_investigate_verdict(args: argparse.Namespace) -> dict:
    from trialerror.litapi.investigate import InvestigateError, record_verdict
    from trialerror.stores.store import open_store

    command = "lit.investigate.verdict"
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(command, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD")
    detail = None
    if args.detail_json:
        try:
            detail = json.loads(Path(args.detail_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return error_envelope(command, "detail_invalid", f"--detail-json {args.detail_json}: {exc}")
        if not isinstance(detail, dict):
            return error_envelope(command, "detail_invalid", f"--detail-json {args.detail_json}: not a JSON object")
    substitute = None
    for kind in ("doi", "arxiv", "isbn"):
        value = getattr(args, f"substitute_{kind}", None)
        if value:
            substitute = (kind, value)

    store = open_store(program_root, platform_root=getattr(args, "platform_root", None))
    try:
        result = record_verdict(
            store, args.dossier, verdict=args.verdict, launch_id=args.launch_id, substitute=substitute,
            reason_code=args.reason_code, detail=detail, supersede=args.supersede,
        )
    except InvestigateError as exc:
        return _investigate_error(command, exc)
    except StoreError as exc:
        return error_envelope(command, type(exc).__name__, str(exc))
    finally:
        store.close()
    return ok_envelope(command, result=result)


def _cmd_investigate_render(args: argparse.Namespace) -> dict:
    from trialerror.litapi.investigate import InvestigateError, render_list
    from trialerror.stores.store import open_store

    command = "lit.investigate.render"
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope(command, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD")
    store = open_store(program_root, platform_root=getattr(args, "platform_root", None))
    try:
        result = render_list(store, args.list_id, fmt=args.render_format)
    except InvestigateError as exc:
        return _investigate_error(command, exc)
    finally:
        store.close()
    return ok_envelope(command, result=result)
