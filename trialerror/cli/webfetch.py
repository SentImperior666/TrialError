"""``trialerror webfetch`` — the web-ingestion CLI group.

Auto-discovered by :func:`trialerror.cli.discover_groups`, so adding it edits
no shared file (the group contract in ``trialerror/cli/__init__.py``).

**This group is the one door.** Design §1 P3: URLs enter the harness here and
nowhere else. There is deliberately no MCP tool, no config key naming a URL,
and no path from a link found in a fetched page to a job — ``links`` prints
what a page named, and a human or an agent has to bring a chosen one back
through ``add``, which re-runs every check from the start. Anything that
widened that would be a channel out of the sandbox with a queue attached.

The verbs, in the order a session uses them::

    trialerror webfetch add   --url U --launch-id L [--kind K] [--license-tier T]
    trialerror webfetch batch --list <file> --launch-id L
    trialerror webfetch status  [--list <file>]     what each URL is doing now
    trialerror webfetch report  [--list <file>]     what each URL became
    trialerror webfetch links   <fetch_id>          what one page pointed at
    trialerror webfetch refresh --all --older-than 30d --launch-id L
    trialerror webfetch proposals                   hosts awaiting approval
    trialerror webfetch sidecar --foreground        the fetch loop itself

Everything they return is **ids, counts and closed-vocabulary reasons**
(C-0007, design §4 T3). No verb here prints a line of a fetched page: the
text lives under ``raw/web/`` and reaches an agent only through retrieval,
where the existing fence applies. That is not a formatting preference — it
is the property that keeps a hostile page from writing into the context of
the agent that fetched it.

``sidecar`` runs the loop of :mod:`trialerror.webfetch.sidecar`. In the
sandbox it is what the container's entrypoint execs after the firewall is up
and privileges are dropped; on a workstation it is the development mode of
design §5 — the same loop, against local directories, so the whole pipeline
is exercised before an image is ever rebuilt::

    trialerror webfetch sidecar --foreground --queue ./queue --policy ./policy

Note what that verb does **not** take: no URL, no host, no header, no
timeout. Everything it is allowed to do comes from the policy directory,
which on the real deployment is mounted read-only from the host and is not
visible in the research container at all (ruling L-A2). ``proposals`` is the
in-container view of the hosts an agent would like added; approving one is a
command on the operator's host machine and cannot be done from here.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from trialerror.stores.store import open_store
from trialerror.util.config import CONFIG_FILENAME, ConfigError, find_program_root, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.webfetch import SIDECAR_VERSION, WebFetchHandlerError
from trialerror.webfetch.config import (
    WebFetchConfigError,
    WebFetchDisabledError,
    load_webfetch_config,
)
from trialerror.webfetch.links import ListParseError, list_ref_for, read_link_list
from trialerror.webfetch.policy import PolicyError
from trialerror.webfetch.protocol import Queue
from trialerror.webfetch.sidecar import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_RECLAIM_AFTER_S,
    Sidecar,
    SidecarPaths,
)

GROUP_NAME = "webfetch"
HELP = "Web-page → corpus ingestion: enqueue URLs, read what they became, run the sidecar."

#: ``--older-than 30d``. Accepting a bare number of seconds as well, because
#: a script that computed one should not have to format it back into a suffix.
_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhdw]?)$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 1}

_LICENSE_TIERS = ("open", "academic_oa", "user_owned_scan", "commercial_restricted", "unknown")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="webfetch_cmd", metavar="<command>", required=True)

    def _roots(p: argparse.ArgumentParser) -> None:
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): SUPPRESS so
        # an unset value here never overwrites the global flags.
        p.add_argument(
            "--program-root",
            default=argparse.SUPPRESS,
            help="program scaffold root (default: discovered from CWD via trialerror.toml)",
        )
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    # -- add -------------------------------------------------------------
    p_add = sub.add_parser("add", help="enqueue one URL for fetching")
    _roots(p_add)
    p_add.add_argument("--url", required=True, help="the URL to fetch (https, or http on a host the operator flagged)")
    p_add.add_argument("--launch-id", required=True, dest="launch_id", help="the launch this fetch is attributed to (design §4 T7)")
    p_add.add_argument("--kind", default=None, choices=["page", "pdf", "git"], help="override the shape inferred from the URL")
    p_add.add_argument(
        "--license-tier",
        default=None,
        dest="license_tier",
        choices=_LICENSE_TIERS,
        help="the operator's own judgment; outranks anything the page declares about itself",
    )
    p_add.add_argument(
        "--origin",
        default="agent",
        choices=["operator_list", "agent"],
        help="who asked for this URL. The default is 'agent': its query is stripped unless "
        "the host carries the keep-query flag, and it counts against the agent daily cap. "
        "'operator_list' is the privileged origin and needs --list-ref naming the delivered "
        "list the URL came off",
    )
    p_add.add_argument(
        "--list-ref",
        default=None,
        dest="list_ref",
        help="the delivered list this URL came off; required by --origin operator_list "
        "(`batch` derives it from the list file's sha256)",
    )
    p_add.add_argument(
        "--retry",
        action="store_true",
        help="supersede a SETTLED row for this URL with a fresh attempt (history is kept)",
    )
    p_add.set_defaults(handler=_cmd_add)

    # -- batch -----------------------------------------------------------
    p_batch = sub.add_parser("batch", help="enqueue every link in a delivered markdown list")
    _roots(p_batch)
    p_batch.add_argument("--list", required=True, dest="list_path", help="the markdown file of links")
    p_batch.add_argument("--launch-id", required=True, dest="launch_id")
    p_batch.add_argument(
        "--license-tier", default=None, dest="license_tier", choices=_LICENSE_TIERS,
        help="applied to every line that does not carry its own tier= tag",
    )
    p_batch.add_argument("--origin", default="operator_list", choices=["operator_list", "agent"])
    p_batch.add_argument("--retry", action="store_true", help="supersede settled rows rather than reporting them as dedup")
    p_batch.add_argument(
        "--dry-run",
        action="store_true",
        help="parse the list and print what WOULD be enqueued, touching nothing",
    )
    p_batch.set_defaults(handler=_cmd_batch)

    # -- refresh ---------------------------------------------------------
    p_refresh = sub.add_parser(
        "refresh",
        help="re-fetch conditionally (never automatic; a 304 or an unchanged extract costs "
        "one request and produces no new document)",
    )
    _roots(p_refresh)
    p_refresh.add_argument("--launch-id", required=True, dest="launch_id")
    p_refresh.add_argument("--fetch-id", action="append", default=[], dest="fetch_ids")
    p_refresh.add_argument("--url", action="append", default=[], dest="urls")
    p_refresh.add_argument("--all", action="store_true", dest="all_rows", help="every live row")
    p_refresh.add_argument(
        "--older-than",
        default=None,
        dest="older_than",
        help="only rows last fetched longer ago than this (e.g. 30d, 12h, 900s)",
    )
    p_refresh.set_defaults(handler=_cmd_refresh)

    # -- status ----------------------------------------------------------
    p_status = sub.add_parser("status", help="what each URL is doing now")
    _roots(p_status)
    p_status.add_argument("--list", default=None, dest="list_path", help="restrict to the URLs of this list file")
    p_status.add_argument("--state", default=None, help="restrict to one state")
    p_status.add_argument("--include-superseded", action="store_true", dest="include_superseded")
    p_status.set_defaults(handler=_cmd_status)

    # -- report ----------------------------------------------------------
    p_report = sub.add_parser("report", help="what each URL became: verdict, source, document, chunks, anchors")
    _roots(p_report)
    p_report.add_argument("--list", default=None, dest="list_path")
    p_report.set_defaults(handler=_cmd_report)

    # -- links -----------------------------------------------------------
    p_links = sub.add_parser(
        "links",
        help="the links one fetched page named — data only; bring a chosen one back through `add`",
    )
    _roots(p_links)
    p_links.add_argument("fetch_id", help="the fetch whose recorded links to print")
    p_links.set_defaults(handler=_cmd_links)

    # -- proposals -------------------------------------------------------
    p_proposals = sub.add_parser(
        "proposals",
        help="hosts an agent asked for and a human has not approved (approval is a host command)",
    )
    _roots(p_proposals)
    p_proposals.set_defaults(handler=_cmd_proposals)

    p_sidecar = sub.add_parser(
        "sidecar",
        help="run the fetch loop against a queue directory (the container's entrypoint, "
        "and the local development mode)",
    )
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): SUPPRESS so an
    # unset value here never overwrites the global flags. Neither root is
    # used by this verb today — the sidecar has no database — but the group
    # accepts them so every group parses the same way.
    p_sidecar.add_argument("--program-root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_sidecar.add_argument("--platform-root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_sidecar.add_argument(
        "--foreground",
        action="store_true",
        help="run in the foreground (the only supported mode; accepted so the container "
        "entrypoint and the docs can say what they mean)",
    )
    p_sidecar.add_argument(
        "--queue", default=None, help="queue directory (default: $TE_WEBFETCH_QUEUE, /queue)"
    )
    p_sidecar.add_argument(
        "--policy", default=None, help="policy directory (default: $TE_WEBFETCH_POLICY, /policy)"
    )
    p_sidecar.add_argument(
        "--audit", default=None, help="audit directory (default: $TE_WEBFETCH_AUDIT, /audit)"
    )
    p_sidecar.add_argument(
        "--state", default=None, help="state directory (default: $TE_WEBFETCH_STATE, /state)"
    )
    p_sidecar.add_argument(
        "--work", default=None, help="clone scratch directory (default: $TE_WEBFETCH_WORK, /work)"
    )
    p_sidecar.add_argument(
        "--once",
        action="store_true",
        help="process at most one job and exit (drains nothing if the queue is empty)",
    )
    p_sidecar.add_argument(
        "--max-jobs", type=int, default=None, help="stop after this many jobs"
    )
    p_sidecar.add_argument(
        "--max-idle-polls",
        type=int,
        default=None,
        help="stop after this many consecutive empty polls (default: never)",
    )
    p_sidecar.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help=f"seconds between polls of an empty queue (default: {DEFAULT_POLL_INTERVAL_S})",
    )
    p_sidecar.add_argument(
        "--reclaim-after",
        type=float,
        default=DEFAULT_RECLAIM_AFTER_S,
        help="return claims whose heartbeat is older than this many seconds "
        f"(default: {DEFAULT_RECLAIM_AFTER_S})",
    )
    p_sidecar.add_argument(
        "--worker-id", default=None, help="override the worker id (default: generated)"
    )
    p_sidecar.add_argument(
        "--allow-denylist-mode",
        action="store_true",
        help="permit a policy.toml with mode='denylist' (workstation development only; the "
        "sandbox deployment must never pass this)",
    )
    p_sidecar.set_defaults(handler=_cmd_sidecar)
    return parser


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    explicit = getattr(args, "program_root", None)
    if explicit:
        return Path(explicit)
    return find_program_root()


def _open(args: argparse.Namespace, cmd: str):
    """``(store, program_root, config, error_envelope_or_None)``.

    The config is loaded here rather than inside each verb so a broken
    ``trialerror.toml`` produces one clear error envelope naming the file,
    rather than a different failure per verb.
    """
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, None, None, error_envelope(
            cmd,
            "no_program_root",
            "no --program-root given and no trialerror.toml found walking up from CWD",
        )
    config_path = Path(program_root) / CONFIG_FILENAME
    config: dict = {}
    if config_path.is_file():
        try:
            config = load_config(config_path).raw
        except ConfigError as exc:
            return None, None, None, error_envelope(
                cmd, "config_invalid", str(exc), details={"path": str(config_path)}
            )
    store = open_store(program_root, platform_root=getattr(args, "platform_root", None))
    return store, Path(program_root), config, None


def _disabled_envelope(cmd: str, exc: WebFetchDisabledError) -> dict:
    """The gate saying no is not a crash — it is the config working.

    So it comes back as a structured error naming the exact line to add,
    rather than as a traceback the operator has to interpret."""
    return error_envelope(
        cmd,
        "webfetch_disabled",
        str(exc),
        next_actions=[
            next_action(
                ["sh", "-c", "printf '\\n[webfetch]\\nenabled = true\\n' >> trialerror.toml"],
                "turn web fetching on for this program (C-0069: off by default on purpose)",
            )
        ],
    )


def _parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    match = _DURATION_RE.match(value.strip())
    if match is None:
        raise ValueError(
            f"{value!r} is not a duration; use a number with an optional s/m/h/d/w suffix (e.g. 30d)"
        )
    return float(match.group("value")) * _DURATION_UNITS[match.group("unit").lower()]


def _run_next_action() -> object:
    return next_action(
        ["trialerror", "jobs", "start-worker", "--mode", "once"],
        "run the jobs worker so the queued fetches are handed to the sidecar",
    )


# ---------------------------------------------------------------------------
# the verbs
# ---------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> dict:
    from trialerror.webfetch.handlers import enqueue_fetch

    store, program_root, config, err = _open(args, "webfetch add")
    if err is not None:
        return err

    # `add` is the verb design §1's modelled attacker runs unprompted, so the
    # privileged origin is not something its caller can simply name. It has
    # to point at a list file that exists and is readable, and the recorded
    # `list_ref` is DERIVED from that file's bytes (the same sha256 `batch`
    # computes) rather than taken from the argument. This does not make the
    # origin unforgeable — something that can run this command can also write
    # a file — but it stops being the default, and it stops being free.
    list_ref = args.list_ref
    if args.origin == "operator_list":
        if not list_ref:
            return error_envelope(
                "webfetch add",
                "list_ref_required",
                "--origin operator_list means 'this URL came off a list a human "
                "delivered': pass --list-ref <that file>, or leave the default "
                "--origin agent (its query is stripped and it counts against the "
                "agent daily cap)",
            )
        try:
            list_ref = list_ref_for(list_ref, program_root)
        except OSError as exc:
            return error_envelope(
                "webfetch add",
                "list_unreadable",
                f"--list-ref {list_ref} could not be read: {exc}",
            )
    elif list_ref:
        try:
            list_ref = list_ref_for(list_ref, program_root)
        except OSError:
            pass  # agent origin gains nothing from it; keep it as free provenance

    try:
        result = enqueue_fetch(
            store,
            url=args.url,
            launch_id=args.launch_id,
            config=config,
            kind=args.kind,
            origin=args.origin,
            list_ref=list_ref,
            license_tier=args.license_tier,
            retry=args.retry,
        )
    except WebFetchDisabledError as exc:
        return _disabled_envelope("webfetch add", exc)
    except (WebFetchConfigError, WebFetchHandlerError) as exc:
        return error_envelope("webfetch add", "webfetch_refused", str(exc))

    if result.action == "refused":
        # A shape refusal reached nothing: no row, no job, and above all no
        # DNS lookup. It is an error envelope because the operator asked for
        # something that cannot happen, not because anything went wrong.
        return error_envelope(
            "webfetch add",
            result.reason or "refused",
            result.detail or f"{args.url} was refused before any lookup",
            details=result.as_dict(),
        )
    return ok_envelope(
        "webfetch add",
        result=result.as_dict(),
        next_actions=[_run_next_action()],
    )


def _cmd_batch(args: argparse.Namespace) -> dict:
    from trialerror.webfetch.handlers import enqueue_batch

    store, program_root, config, err = _open(args, "webfetch batch")
    if err is not None:
        return err
    try:
        entries, list_ref = read_link_list(args.list_path)
    except ListParseError as exc:
        return error_envelope("webfetch batch", "list_unreadable", str(exc))

    if args.dry_run:
        # Reads the file, touches nothing else. What an operator runs before
        # a wave: see what the parser made of the list, and which hosts will
        # need approving, without creating a single row.
        from trialerror.webfetch.links import distinct_hosts

        return ok_envelope(
            "webfetch batch",
            result={
                "dryRun": True,
                "listRef": list_ref,
                "links": [
                    {"line": e.line_no, "url": e.url, "kind": e.resolved_kind, "tier": e.license_tier}
                    for e in entries
                ],
                "hosts": distinct_hosts(entries),
            },
            next_actions=[
                next_action(
                    ["te-webfetch.sh", "import-list", str(args.list_path)],
                    "approve these hosts on the host before fetching (ruling L-A2)",
                )
            ],
        )

    try:
        results = enqueue_batch(
            store,
            entries,
            launch_id=args.launch_id,
            list_ref=list_ref,
            config=config,
            license_tier=args.license_tier,
            origin=args.origin,
            retry=args.retry,
        )
    except WebFetchDisabledError as exc:
        return _disabled_envelope("webfetch batch", exc)
    except (WebFetchConfigError, WebFetchHandlerError) as exc:
        return error_envelope("webfetch batch", "webfetch_refused", str(exc))

    counts: dict[str, int] = {}
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1
    return ok_envelope(
        "webfetch batch",
        result={
            "listRef": list_ref,
            "counts": counts,
            "links": [r.as_dict() for r in results],
        },
        next_actions=[_run_next_action()],
    )


def _cmd_refresh(args: argparse.Namespace) -> dict:
    from trialerror.webfetch.handlers import refresh_fetches

    store, program_root, config, err = _open(args, "webfetch refresh")
    if err is not None:
        return err
    if not (args.fetch_ids or args.urls or args.all_rows):
        return error_envelope(
            "webfetch refresh",
            "nothing_selected",
            "name what to refresh: --fetch-id, --url, or --all (a re-fetch is never automatic)",
        )
    try:
        older_than_s = _parse_duration(args.older_than)
    except ValueError as exc:
        return error_envelope("webfetch refresh", "bad_duration", str(exc))

    try:
        results = refresh_fetches(
            store,
            launch_id=args.launch_id,
            fetch_ids=args.fetch_ids,
            urls=args.urls,
            all_rows=args.all_rows,
            older_than_s=older_than_s,
            config=config,
        )
    except WebFetchDisabledError as exc:
        return _disabled_envelope("webfetch refresh", exc)
    except (WebFetchConfigError, WebFetchHandlerError) as exc:
        return error_envelope("webfetch refresh", "webfetch_refused", str(exc))

    return ok_envelope(
        "webfetch refresh",
        result={
            "refreshed": sum(1 for r in results if r.action == "superseded"),
            "skipped": sum(1 for r in results if r.action != "superseded"),
            "links": [r.as_dict() for r in results],
        },
        next_actions=[_run_next_action()],
    )


def _cmd_status(args: argparse.Namespace) -> dict:
    from trialerror.webfetch.handlers import fetch_rows

    store, program_root, config, err = _open(args, "webfetch status")
    if err is not None:
        return err
    list_ref = None
    if args.list_path:
        try:
            _entries, list_ref = read_link_list(args.list_path)
        except ListParseError as exc:
            return error_envelope("webfetch status", "list_unreadable", str(exc))

    rows = fetch_rows(
        store, list_ref=list_ref, state=args.state, include_superseded=args.include_superseded
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1

    cfg = load_webfetch_config(config)
    queue_dir = cfg.queue_path(program_root)
    queue = Queue(queue_dir)
    return ok_envelope(
        "webfetch status",
        result={
            "listRef": list_ref,
            "counts": counts,
            "total": len(rows),
            "queue": {
                "dir": str(queue_dir),
                "pending": len(queue.pending_job_ids()) if queue_dir.is_dir() else 0,
                "sidecarHeartbeatAgeS": queue.sidecar_heartbeat_age_s() if queue_dir.is_dir() else None,
            },
            # ids, states and reasons — deliberately not titles or text
            "fetches": [
                {
                    "fetchId": row["fetch_id"],
                    "url": row["url_norm"],
                    "state": row["state"],
                    "kind": row["kind"],
                    "reason": row["reason"],
                    "supersededBy": row["superseded_by"],
                }
                for row in rows
            ],
        },
    )


def _cmd_report(args: argparse.Namespace) -> dict:
    from trialerror.webfetch.handlers import fetch_report

    store, program_root, config, err = _open(args, "webfetch report")
    if err is not None:
        return err
    list_ref = None
    if args.list_path:
        try:
            _entries, list_ref = read_link_list(args.list_path)
        except ListParseError as exc:
            return error_envelope("webfetch report", "list_unreadable", str(exc))

    lines = fetch_report(store, list_ref=list_ref)
    unaccounted = [line for line in lines if not line["verdict"]]
    return ok_envelope(
        "webfetch report",
        result={
            "listRef": list_ref,
            "total": len(lines),
            # Design §6's A-wave0 criterion: "accounted for with a reason",
            # not "N documents". This number is the one to read.
            "unaccountedFor": len(unaccounted),
            "indexed": sum(1 for line in lines if line["documentStatus"] == "indexed"),
            "lines": lines,
        },
    )


def _cmd_links(args: argparse.Namespace) -> dict:
    from trialerror.webfetch.handlers import recorded_links

    store, program_root, config, err = _open(args, "webfetch links")
    if err is not None:
        return err
    try:
        links = recorded_links(store, args.fetch_id)
    except WebFetchHandlerError as exc:
        return error_envelope("webfetch links", "no_such_fetch", str(exc))
    return ok_envelope(
        "webfetch links",
        result={"fetchId": args.fetch_id, "count": len(links), "links": links},
        next_actions=[
            next_action(
                ["trialerror", "webfetch", "add", "--url", "<one of these>", "--launch-id", "<L>"],
                "links are recorded, never followed — a chosen one comes back through the one door",
            )
        ],
    )


def _cmd_proposals(args: argparse.Namespace) -> dict:
    """Hosts an agent asked for. Reading them here is all this side can do.

    Approval is ``te-webfetch.sh review`` on the operator's host, against a file
    this container cannot see — which is the whole of ruling L-A2. A verb
    here that could approve one would put the allowlist back inside the
    blast radius it was moved out of.
    """
    store, program_root, config, err = _open(args, "webfetch proposals")
    if err is not None:
        return err
    cfg = load_webfetch_config(config)
    path = Queue(cfg.queue_path(program_root)).proposals_path
    proposals: list[dict] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                proposals.append(json.loads(line))
            except ValueError:
                continue

    by_host: dict[str, dict] = {}
    for entry in proposals:
        host = str(entry.get("host") or "")
        if not host:
            continue
        seen = by_host.setdefault(
            host, {"host": host, "count": 0, "launchIds": [], "exampleUrl": entry.get("example_url")}
        )
        seen["count"] += 1
        launch_id = entry.get("launch_id")
        if launch_id and launch_id not in seen["launchIds"]:
            seen["launchIds"].append(launch_id)

    return ok_envelope(
        "webfetch proposals",
        result={"hosts": sorted(by_host.values(), key=lambda h: h["host"]), "total": len(proposals)},
        next_actions=[
            next_action(
                ["te-webfetch.sh", "review"],
                "approve or refuse these on the host — the allowlist is not reachable from here",
            )
        ],
    )


def _cmd_sidecar(args: argparse.Namespace) -> dict:
    paths = SidecarPaths.from_env(
        queue=args.queue,
        policy=args.policy,
        audit=args.audit,
        state=args.state,
        work=args.work,
    )
    if not Path(paths.policy).is_dir():
        return error_envelope(
            "webfetch sidecar",
            "policy_missing",
            f"policy directory {paths.policy} does not exist",
            details={"policy_dir": str(paths.policy)},
            next_actions=[
                next_action(
                    ["te-webfetch.sh", "status"],
                    "check the host-side policy mount (allowed-hosts.conf, policy.toml)",
                )
            ],
        )

    max_jobs = args.max_jobs
    max_idle_polls = args.max_idle_polls
    if args.once:
        max_jobs = 1 if max_jobs is None else min(1, max_jobs)
        max_idle_polls = 1 if max_idle_polls is None else min(1, max_idle_polls)

    sidecar = Sidecar(
        paths,
        worker_id=args.worker_id,
        require_allowlist=not args.allow_denylist_mode,
        poll_interval_s=args.poll_interval,
        reclaim_after_s=args.reclaim_after,
    )
    try:
        stats = sidecar.run(max_jobs=max_jobs, max_idle_polls=max_idle_polls)
    except PolicyError as exc:
        # Fail-closed and say so: a policy the sidecar cannot read is not a
        # reason to fetch with defaults.
        return error_envelope(
            "webfetch sidecar",
            "policy_invalid",
            str(exc),
            details={"policy_dir": str(paths.policy)},
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return ok_envelope(
            "webfetch sidecar",
            result={"stopped": "interrupt", "sidecarVersion": SIDECAR_VERSION},
        )

    return ok_envelope(
        "webfetch sidecar",
        result={
            "sidecarVersion": SIDECAR_VERSION,
            "workerId": sidecar.worker_id,
            "queueDir": str(paths.queue),
            "policyDir": str(paths.policy),
            **stats.as_dict(),
        },
        next_actions=[
            next_action(
                ["trialerror", "doctor", "--category", "webfetch"],
                "check the sidecar's health lines",
            )
        ],
    )
