"""The research side: enqueue, fetch hand-off, extract, settle.

Everything in this module runs in the research container — the one with the
corpus, the databases and **no egress**. It never opens a socket. What it
does instead is write a manifest into a directory the sidecar reads, wait a
little, and then make sense of whatever came back (design §2.2).

Two job kinds, deliberately (see the ``jobs_v3`` migration's own comment):

``web_fetch``
    Writes ``pending/<job>.json`` if it is not already there, polls the queue
    for up to ``wait_s``, and either settles the fetch or parks the job with
    :class:`~trialerror.jobs.worker.EnvironmentalFailure` — which costs the
    job no attempt, so a sidecar that is down for a day costs nothing but
    time. A *refusal* is a settlement, never a retry: the settlement table of
    design §5 is implemented in :func:`_settle_refusal`, and nothing in it
    walks back into the same wall.

``web_extract``
    Pure local CPU over bytes already on disk. Turns the fetched body into
    corpus rows through the EXISTING seams — ``register_source`` /
    ``add_document`` / ``normalize_html`` / the sanitizer / ``stream_v1`` /
    the chunker — with no new pipeline stage and no new normalizer. That is
    the point of §2.2's choice to feed ``normalize_html`` the cleaned HTML
    rather than the markdown: quote anchors keep resolving because nothing
    downstream of ``add_document`` has changed at all.

**Three rules the whole module rests on.**

*Ids and stats out, never page text* (C-0007, design §4 T3). Every dict a CLI
verb gets back from here carries identifiers, counts and closed-vocabulary
reasons. The page's words live in ``raw/web/`` and reach an agent only
through retrieval, fenced by the existing engine.

*A refusal is a result.* ``paywalled``, ``robots_disallow``,
``host_not_allowed`` and their siblings complete the job and, where a human
could lawfully fix them, leave a ``wanted`` source row and a line in
``REQUESTS.md``. No bypass is attempted for any of them, ever.

*Every byte is re-checked on the way in.* The queue is writable from both
sides (design §8), so a published result is verified against its own
``result.json`` — strict schema, exact size, exact sha256 — before one byte
of it is copied into the corpus, and the fixed payload filenames are the only
ones read.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trialerror.jobs import ledger
from trialerror.jobs.registry import register_handler
from trialerror.jobs.worker import EnvironmentalFailure
from trialerror.stores.store import Store
from trialerror.stores.writer import get, insert, update
from trialerror.util.atomic import atomic_write_text
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now, now_dt, parse as parse_ts
from trialerror.webfetch import (
    HUMAN_FIXABLE_REASONS,
    WebFetchError,
    WebFetchHandlerError,
    WebFetchRefused,
)
from trialerror.webfetch import protocol
from trialerror.webfetch.config import (
    WebFetchConfig,
    WebFetchConfigError,
    load_webfetch_config,
)
from trialerror.webfetch.links import ListEntry, infer_kind
from trialerror.webfetch.protocol import Manifest, Queue
from trialerror.webfetch.urlcheck import normalize, peek_host

__all__ = [
    "FETCH_JOB_PREFIX",
    "EXTRACT_JOB_PREFIX",
    "PARK_RETRY_DELAY_S",
    "TERMINAL_STATES",
    "EnqueueResult",
    "WebFetchHandlerError",
    "run_web_fetch",
    "run_web_extract",
    "enqueue_fetch",
    "enqueue_batch",
    "refresh_fetches",
    "fetch_rows",
    "fetch_report",
    "recorded_links",
    "live_row_for_url",
]

FETCH_JOB_PREFIX = "JOB-webfetch-"
EXTRACT_JOB_PREFIX = "JOB-webextract-"

#: Design §2.2 step 2. One minute, not the offload lane's half hour: the
#: sidecar is a container beside this one rather than a laptop that might be
#: switched off, so "not yet" here means seconds, not hours.
PARK_RETRY_DELAY_S = 60

#: Design §5: how often the handler checkpoints while waiting. A checkpoint
#: renews the lease, so this is also the liveness cadence.
_CHECKPOINT_EVERY_S = 15.0

#: States a ``web_fetch`` row never leaves on its own.
TERMINAL_STATES: frozenset[str] = frozenset({"extracted", "refused", "unchanged", "failed"})

#: File extension per content class, for ``raw/web/<host>/<fetch_id><ext>``.
_EXTENSIONS = {"html": ".html", "pdf": ".pdf", "text": ".txt", "git": ".tar"}

#: What a repo's documents are ingested as (design §2.2, the git branch).
#: Code files are not ingested — deferred, and named as deferred.
_REPO_TEXT_SUFFIXES = (".md", ".markdown", ".rst", ".txt")
_REPO_MAX_FILES = 200
_REPO_MAX_TEXT_BYTES = 50 * 1024 * 1024
_REPO_MAX_PDF_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------


def _config(store: Store) -> dict[str, Any]:
    """The program's ``trialerror.toml``, fail-closed.

    Same rule as ``trialerror.ingest.handlers._load_config``: an ABSENT
    config is a scratch program using defaults; a PRESENT but unreadable one
    raises, because the operator's stated intent exists and could not be
    read."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    path = store.program_root / CONFIG_FILENAME
    if not path.is_file():
        return {}
    return load_config(path).raw


def _event(store: Store, event_type: str, payload: Mapping[str, Any], *, launch_id: str | None) -> None:
    """Append one ``web_fetch_*`` event.

    Payloads here are ids, counts and reasons only — never a title, never a
    URL's query string, never a byte of the page. The event log is read by
    agents and rendered into dashboards; it is not a second copy of the
    corpus."""
    from trialerror.events.api import append_event

    append_event(store, event_type=event_type, payload=dict(payload), launch_id=launch_id)


def _enqueue_job(store: Store, *, kind: str, payload: dict[str, Any], job_id: str) -> None:
    """``ledger.enqueue``, skipped when the job already exists.

    The same idempotence guard ``trialerror.ingest.handlers._enqueue_next_stage``
    documents: a handler that crashes AFTER handing off and BEFORE settling
    re-runs this call on resume, and must not fail on the duplicate id."""
    if ledger.get_job(store, job_id) is None:
        ledger.enqueue(store, kind=kind, payload=payload, job_id=job_id)


def _row(store: Store, fetch_id: str) -> dict[str, Any] | None:
    return get(store, "web_fetch", pk_column="fetch_id", pk_value=fetch_id)


def _touch(store: Store, fetch_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
    update(
        store,
        "web_fetch",
        pk_column="fetch_id",
        pk_value=fetch_id,
        changes={**dict(changes), "updated_ts": now()},
    )
    row = _row(store, fetch_id)
    assert row is not None
    return row


def live_row_for_url(store: Store, url_norm: str) -> dict[str, Any] | None:
    """The one live ``web_fetch`` row for a canonical URL, if there is one."""
    found = store.knowledge.execute(
        "SELECT * FROM web_fetch WHERE url_norm = ? AND superseded_by IS NULL", (url_norm,)
    ).fetchone()
    return dict(found) if found is not None else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rel(program_root: Path, path: Path) -> str:
    """A program-root-relative POSIX path, or the absolute one if it is
    outside. Stored in the DB, so it must not pin the corpus to one host."""
    try:
        return path.resolve().relative_to(Path(program_root).resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnqueueResult:
    """What one ``add``/``batch`` line did. Ids only (design §4 T3)."""

    url_norm: str | None
    action: str  # enqueued | dedup | superseded | refused
    fetch_id: str | None = None
    job_id: str | None = None
    reason: str | None = None
    detail: str | None = None
    line_no: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action, "urlNorm": self.url_norm}
        if self.fetch_id:
            out["fetchId"] = self.fetch_id
        if self.job_id:
            out["jobId"] = self.job_id
        if self.reason:
            out["reason"] = self.reason
        if self.detail:
            out["detail"] = self.detail
        if self.line_no is not None:
            out["line"] = self.line_no
        return out


def _assert_launch_exists(store: Store, launch_id: str) -> None:
    """Design §4 T7. The write API would refuse the row anyway (``web_fetch.
    launch_id`` is a registered XID), but refusing here gives the operator
    the error at the CLI rather than as an integrity violation three frames
    down."""
    found = store.platform.execute(
        "SELECT 1 FROM launch WHERE launch_id = ? LIMIT 1", (launch_id,)
    ).fetchone()
    if found is None:
        raise WebFetchHandlerError(
            f"launch {launch_id!r} is not booked in platform.launch — every fetch is "
            "attributable to a real launch (design §4 T7); book it first"
        )


def enqueue_fetch(
    store: Store,
    *,
    url: str,
    launch_id: str,
    config: Mapping[str, Any] | None = None,
    webfetch: WebFetchConfig | None = None,
    kind: str | None = None,
    origin: str = "agent",
    list_ref: str | None = None,
    license_tier: str | None = None,
    program_id: str | None = None,
    retry: bool = False,
    line_no: int | None = None,
) -> EnqueueResult:
    """Enqueue one URL (design §2.2 step 1). One door in, and this is it.

    ``allow_http=True`` here is not a relaxation. Whether plain HTTP is
    acceptable is a *per-host* flag, and per-host policy lives on the operator's
    host, read-only in the sidecar and absent from ``/workspace`` entirely
    (ruling L-A2) — this side cannot see it and must not pretend to. An
    ``http://`` URL on a host without the flag is refused by the sidecar with
    ``scheme_not_allowed``, which is where the decision belongs. The same
    reasoning is already written down in ``urlcheck.peek_host``.

    ``origin`` defaults to ``agent`` — the *unprivileged* value. The modelled
    attacker (design §1) is something running in this container that calls
    exactly this function, and ``operator_list`` is what keeps a URL's query
    string and exempts it from the ``agent_daily`` cap; a privileged default
    would hand both to the attacker they were written for. Nor is the label
    free to assert: ``operator_list`` means "off a list a human delivered",
    and a delivered list has a ``list_ref``. Without one it is normalized
    away here, so the row, the manifest and the sidecar all record the same
    origin (``Manifest.effective_origin`` re-derives it at the trust
    boundary, for manifests that never came through this function).
    """
    raw_config = dict(config) if config is not None else _config(store)
    cfg = webfetch or load_webfetch_config(raw_config, require_enabled=True)
    cfg.require_enabled()
    _assert_launch_exists(store, launch_id)
    if origin not in protocol.ORIGINS:
        raise WebFetchHandlerError(f"origin must be one of {sorted(protocol.ORIGINS)}")
    if origin == "operator_list" and not list_ref:
        origin = "agent"

    try:
        nurl = normalize(url, allow_http=True)
    except WebFetchRefused as exc:
        # A shape refusal costs nothing and reaches nothing: no row, no job,
        # no manifest, and above all no DNS lookup. Design §4 T1's "zero
        # sockets for pre-DNS refusals" starts here.
        return EnqueueResult(
            url_norm=None, action="refused", reason=exc.reason, detail=exc.detail, line_no=line_no
        )

    resolved_kind = kind or infer_kind(nurl.url_norm)
    if resolved_kind not in protocol.KINDS:
        raise WebFetchHandlerError(f"kind must be one of {sorted(protocol.KINDS)}")

    existing = live_row_for_url(store, nurl.url_norm)
    if existing is not None and not retry:
        return EnqueueResult(
            url_norm=nurl.url_norm,
            action="dedup",
            fetch_id=existing["fetch_id"],
            reason=existing["reason"],
            detail=f"already {existing['state']}",
            line_no=line_no,
        )

    superseded: str | None = None
    if existing is not None:
        if existing["state"] not in TERMINAL_STATES:
            return EnqueueResult(
                url_norm=nurl.url_norm,
                action="dedup",
                fetch_id=existing["fetch_id"],
                detail=f"in flight ({existing['state']}) — --retry supersedes a settled row, "
                "not one still working",
                line_no=line_no,
            )
        superseded = existing["fetch_id"]

    fetch_id = new_id("WF")
    if superseded is not None:
        # Order matters and is fixed by the partial unique index: the old row
        # stops being live BEFORE the new one is inserted. See the v4
        # migration's comment for why ``superseded_by`` is not an FK.
        _touch(store, superseded, {"superseded_by": fetch_id})

    row = {
        "fetch_id": fetch_id,
        "job_id": f"{FETCH_JOB_PREFIX}{fetch_id}",
        "launch_id": launch_id,
        "program_id": program_id,
        "url": nurl.url,
        "url_norm": nurl.url_norm,
        "kind": resolved_kind,
        "origin": origin,
        "list_ref": list_ref,
        "state": "queued",
        "license_detected": None,
        "created_ts": now(),
    }
    if license_tier:
        # Recorded as the operator's declaration, distinct from anything the
        # page says about itself; the extract stage prefers this over its own
        # detection (design §4 T4: the tag outranks the markup).
        row["license_detected"] = f"operator:{license_tier}"
    insert(store, "web_fetch", row)

    job_id = f"{FETCH_JOB_PREFIX}{fetch_id}"
    _enqueue_job(
        store,
        kind="web_fetch",
        payload={"fetch_id": fetch_id, "created_by_launch": launch_id},
        job_id=job_id,
    )
    _event(
        store,
        "web_fetch_enqueued",
        {
            "fetch_id": fetch_id,
            "job_id": job_id,
            "kind": resolved_kind,
            "origin": origin,
            "supersedes": superseded,
            "list_ref": list_ref,
        },
        launch_id=launch_id,
    )
    return EnqueueResult(
        url_norm=nurl.url_norm,
        action="superseded" if superseded else "enqueued",
        fetch_id=fetch_id,
        job_id=job_id,
        line_no=line_no,
    )


def enqueue_batch(
    store: Store,
    entries: Sequence[ListEntry],
    *,
    launch_id: str,
    list_ref: str | None,
    config: Mapping[str, Any] | None = None,
    license_tier: str | None = None,
    origin: str = "operator_list",
    retry: bool = False,
) -> list[EnqueueResult]:
    """Enqueue a parsed list. Idempotent: a second run of the same list
    enqueues nothing and reports ``dedup`` for every line, which is what
    makes it safe to re-run after fixing one bad URL."""
    raw_config = dict(config) if config is not None else _config(store)
    cfg = load_webfetch_config(raw_config, require_enabled=True)
    results: list[EnqueueResult] = []
    seen: set[str] = set()
    for entry in entries:
        result = enqueue_fetch(
            store,
            url=entry.url,
            launch_id=launch_id,
            config=raw_config,
            webfetch=cfg,
            kind=entry.resolved_kind,
            origin=origin,
            list_ref=list_ref,
            license_tier=entry.license_tier or license_tier,
            retry=retry,
            line_no=entry.line_no,
        )
        if result.url_norm and result.url_norm in seen and result.action == "dedup":
            result = EnqueueResult(
                url_norm=result.url_norm,
                action="dedup",
                fetch_id=result.fetch_id,
                detail="the list names this URL more than once",
                line_no=entry.line_no,
            )
        if result.url_norm:
            seen.add(result.url_norm)
        results.append(result)
    return results


def refresh_fetches(
    store: Store,
    *,
    launch_id: str,
    fetch_ids: Iterable[str] = (),
    urls: Iterable[str] = (),
    all_rows: bool = False,
    older_than_s: float | None = None,
    config: Mapping[str, Any] | None = None,
) -> list[EnqueueResult]:
    """Re-fetch, conditionally (design §5, "Re-fetch: never automatic").

    Only ever explicit. ``--older-than`` narrows an ``--all`` sweep to rows
    that have not been looked at in a while; a row still in flight is left
    alone. Each selected row is superseded by a fresh one carrying its stored
    ``etag``/``last-modified``, so a 304 or an unchanged extracted hash costs
    one conditional request and produces no new document.
    """
    raw_config = dict(config) if config is not None else _config(store)
    cfg = load_webfetch_config(raw_config, require_enabled=True)

    selected: list[dict[str, Any]] = []
    for fetch_id in fetch_ids:
        row = _row(store, fetch_id)
        if row is None:
            raise WebFetchHandlerError(f"no such fetch: {fetch_id!r}")
        selected.append(row)
    for url in urls:
        try:
            nurl = normalize(url, allow_http=True)
        except WebFetchRefused as exc:
            raise WebFetchHandlerError(f"{url}: {exc.reason} ({exc.detail})") from exc
        row = live_row_for_url(store, nurl.url_norm)
        if row is None:
            raise WebFetchHandlerError(f"no live fetch for {nurl.url_norm}")
        selected.append(row)
    if all_rows:
        selected.extend(
            dict(r)
            for r in store.knowledge.execute(
                "SELECT * FROM web_fetch WHERE superseded_by IS NULL ORDER BY created_ts"
            ).fetchall()
        )

    cutoff_dt = None
    if older_than_s is not None:
        from datetime import timedelta

        cutoff_dt = now_dt() - timedelta(seconds=float(older_than_s))

    results: list[EnqueueResult] = []
    done: set[str] = set()
    for row in selected:
        if row["fetch_id"] in done or row["superseded_by"] is not None:
            continue
        done.add(row["fetch_id"])
        if row["state"] not in TERMINAL_STATES:
            results.append(
                EnqueueResult(
                    url_norm=row["url_norm"],
                    action="dedup",
                    fetch_id=row["fetch_id"],
                    detail=f"still {row['state']}",
                )
            )
            continue
        if cutoff_dt is not None:
            stamp = row["fetched_ts"] or row["created_ts"]
            try:
                if parse_ts(str(stamp)) >= cutoff_dt:
                    continue
            except ValueError:
                pass
        results.append(
            enqueue_fetch(
                store,
                url=row["url"],
                launch_id=launch_id,
                config=raw_config,
                webfetch=cfg,
                kind=row["kind"],
                origin=row["origin"],
                list_ref=row["list_ref"],
                program_id=row["program_id"],
                retry=True,
            )
        )
    return results


# ---------------------------------------------------------------------------
# the fetch handler
# ---------------------------------------------------------------------------


def _queue(store: Store, cfg: WebFetchConfig) -> Queue:
    return Queue(cfg.queue_path(store.program_root))


def _conditional_headers(store: Store, row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(etag, last_modified)`` from the row this one supersedes.

    Only from the SAME host: an ETag is a value that host chose, and sending
    it anywhere else would leak it (design §4 T2, and ``urlcheck.same_host``
    says the same thing one layer down). The sidecar re-checks this too; the
    cheapest place to not make the mistake is here, by not putting the value
    in the manifest at all.
    """
    previous = store.knowledge.execute(
        "SELECT headers_subset, url_norm FROM web_fetch "
        "WHERE superseded_by = ? AND headers_subset IS NOT NULL",
        (row["fetch_id"],),
    ).fetchone()
    if previous is None:
        return None, None
    try:
        if peek_host(previous["url_norm"]) != peek_host(row["url_norm"]):
            return None, None
        headers = json.loads(previous["headers_subset"])
    except (WebFetchError, ValueError, TypeError):
        return None, None
    etag = headers.get("etag")
    last_modified = headers.get("last-modified")
    return (etag if isinstance(etag, str) else None), (
        last_modified if isinstance(last_modified, str) else None
    )


def _manifest_for(store: Store, row: Mapping[str, Any]) -> Manifest:
    etag, last_modified = _conditional_headers(store, row)
    return Manifest.build(
        job_id=row["job_id"] or f"{FETCH_JOB_PREFIX}{row['fetch_id']}",
        fetch_id=row["fetch_id"],
        launch_id=row["launch_id"],
        url=row["url"],
        kind=row["kind"],
        origin=row["origin"],
        program_id=row["program_id"],
        list_ref=None if row["list_ref"] is None else str(row["list_ref"])[:250],
        etag=etag,
        last_modified=last_modified,
    )


@register_handler("web_fetch")
def run_web_fetch(ctx) -> None:
    """Hand one URL to the sidecar and settle whatever comes back.

    The five-way branch of design §2.2 step 2, in the only order it can be
    evaluated once (a job cannot be both published and pending): published
    result, published refusal, already queued, not queued yet, expired.
    """
    store: Store = ctx.store
    fetch_id = ctx.payload["fetch_id"]
    row = _row(store, fetch_id)
    if row is None:
        raise RuntimeError(f"web_fetch: no such fetch row {fetch_id!r}")

    raw_config = _config(store)
    try:
        cfg = load_webfetch_config(raw_config)
    except WebFetchConfigError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"web_fetch {fetch_id}: {exc}") from exc
    if not cfg.enabled:
        # The job was booked while fetching was on and someone turned it off.
        # That is an environment change, not a logic error: park it with no
        # attempt burned so it resumes when the operator turns it back on.
        raise EnvironmentalFailure(
            f"web_fetch {fetch_id}: [webfetch] enabled = false; the job waits until it is on again",
            retry_delay_s=PARK_RETRY_DELAY_S,
        )

    queue = _queue(store, cfg).ensure_layout()
    job_id = row["job_id"] or ctx.job_id

    settled = _try_settle(ctx, store, cfg, queue, row, job_id)
    if settled:
        return

    manifest = _manifest_for(store, row)
    submitted = queue.submit(manifest)
    if submitted.wrote:
        row = _touch(store, fetch_id, {"state": "pending"})
        ctx.set_checkpoint({"webfetch": "submitted", "job_id": job_id})

    deadline = time.monotonic() + max(0.0, cfg.wait_s)
    last_checkpoint = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(min(cfg.poll_interval_s, max(0.0, deadline - time.monotonic())))
        if _try_settle(ctx, store, cfg, queue, row, job_id):
            return
        if time.monotonic() - last_checkpoint >= _CHECKPOINT_EVERY_S:
            # A checkpoint renews the lease. Without it a 45-second wait
            # inside a handler would look like a dead worker to the ledger.
            ctx.set_checkpoint({"webfetch": "waiting", "job_id": job_id})
            last_checkpoint = time.monotonic()

    if job_id in queue.expired_pending(cfg.manifest_expires_after_s):
        # Visible, not silent: a day-old manifest means the sidecar is not
        # running at all, and a job that parks forever tells nobody that.
        raise RuntimeError(
            f"web_fetch {fetch_id}: manifest for {job_id} has been pending for over "
            f"{cfg.manifest_expires_after_s:.0f}s — the sidecar is not running "
            f"(reason: sidecar_unavailable). Start it, then `trialerror webfetch refresh "
            f"--fetch-id {fetch_id}`."
        )
    ctx.set_checkpoint({"webfetch": "awaiting_sidecar", "job_id": job_id})
    raise EnvironmentalFailure(
        f"awaiting webfetch sidecar: {job_id} is {queue.state_of(job_id)}",
        retry_delay_s=PARK_RETRY_DELAY_S,
    )


def _try_settle(ctx, store: Store, cfg: WebFetchConfig, queue: Queue, row, job_id: str) -> bool:
    """Settle from a published result if there is one. ``True`` when done."""
    state = queue.state_of(job_id)
    if state == protocol.STATE_DONE:
        _settle_done(ctx, store, cfg, queue, row, job_id)
        return True
    if state == protocol.STATE_FAILED:
        _settle_failed(ctx, store, cfg, queue, row, job_id)
        return True
    return False


def _settle_done(ctx, store: Store, cfg: WebFetchConfig, queue: Queue, row, job_id: str) -> None:
    fetch_id = row["fetch_id"]
    _state, result = queue.read_result(job_id)

    if result["fetch_id"] != fetch_id:
        raise RuntimeError(
            f"web_fetch {fetch_id}: published result names fetch {result['fetch_id']!r}"
        )
    if result["outcome"] == "refused":
        # A refusal published into done/ rather than failed/. Same settlement
        # either way — the reason is what matters, not which directory the
        # sidecar happened to put it in.
        _record_refusal(store, cfg, row, result)
        queue.sweep(job_id)
        return

    if result["outcome"] == "unchanged":
        # Checked BEFORE the payload verification below: a 304 has no body to
        # verify, and asking for one would turn "nothing changed" into a
        # logic failure.
        _touch(
            store,
            fetch_id,
            {
                "state": "unchanged",
                "outcome": "unchanged",
                "http_status": result["http_status"],
                "fetched_ts": result["fetched_ts"],
                "elapsed_ms": result["elapsed_ms"],
                "final_url": result["final_url"],
                "headers_subset": json.dumps(result["headers_subset"], sort_keys=True),
                "sidecar_version": result["sidecar_version"],
                **_robots_policy_columns(result),
            },
        )
        _event(
            store,
            "web_fetch_unchanged",
            {"fetch_id": fetch_id, "job_id": job_id, "http_status": result["http_status"]},
            launch_id=row["launch_id"],
        )
        queue.sweep(job_id)
        return

    try:
        payload_path = queue.verify_payload(job_id, result)
    except protocol.ProtocolError as exc:
        # The two sides disagree about what was fetched. Conservative
        # reading: neither number can be trusted, so nothing is ingested and
        # the attempt is burned visibly (design §5, last settlement row).
        raise RuntimeError(f"web_fetch {fetch_id}: {exc}") from exc

    host = _host_of(result, row)
    directory = cfg.raw_dir(store.program_root, host)
    directory.mkdir(parents=True, exist_ok=True)
    extension = _EXTENSIONS.get(str(result["content_class"]), ".bin")
    raw_path = directory / f"{fetch_id}{extension}"
    assert payload_path is not None
    shutil.copyfile(payload_path, raw_path)
    provenance_path = directory / f"{fetch_id}.fetch.json"
    atomic_write_text(provenance_path, json.dumps(result, sort_keys=True, indent=2) + "\n")

    _touch(
        store,
        fetch_id,
        {
            "state": "fetched",
            "outcome": "fetched",
            "final_url": result["final_url"],
            "redirect_chain": json.dumps(result["redirect_chain"]),
            "http_status": result["http_status"],
            "content_type": result["content_type"],
            "content_class": result["content_class"],
            "bytes": result["bytes"],
            "content_sha256": result["content_sha256"],
            "resolved_ips": json.dumps(result["resolved_ips"]),
            "bytes_out": result["bytes_out"],
            "headers_subset": json.dumps(result["headers_subset"], sort_keys=True),
            "fetched_ts": result["fetched_ts"],
            "elapsed_ms": result["elapsed_ms"],
            "sidecar_version": result["sidecar_version"],
            "git_head": (result["git"] or {}).get("head"),
            "git_ref": (result["git"] or {}).get("ref"),
            "git_path": (result["git"] or {}).get("path"),
            "raw_path": _rel(store.program_root, raw_path),
            "provenance_path": _rel(store.program_root, provenance_path),
            **_robots_policy_columns(result),
        },
    )
    _event(
        store,
        "web_fetch_fetched",
        {
            "fetch_id": fetch_id,
            "job_id": job_id,
            "http_status": result["http_status"],
            "content_class": result["content_class"],
            "bytes": result["bytes"],
            "elapsed_ms": result["elapsed_ms"],
            "redirects": len(result["redirect_chain"]),
        },
        launch_id=row["launch_id"],
    )
    _enqueue_job(
        store,
        kind="web_extract",
        payload={"fetch_id": fetch_id, "created_by_launch": row["launch_id"]},
        job_id=f"{EXTRACT_JOB_PREFIX}{fetch_id}",
    )
    queue.sweep(job_id)
    ctx.set_checkpoint({"webfetch": "fetched", "fetch_id": fetch_id})


def _settle_failed(ctx, store: Store, cfg: WebFetchConfig, queue: Queue, row, job_id: str) -> None:
    _state, result = queue.read_result(job_id)
    _record_refusal(store, cfg, row, result)
    shutil.rmtree(queue.failed_path(job_id), ignore_errors=True)
    ctx.set_checkpoint({"webfetch": "refused", "reason": result.get("reason")})


def _robots_policy_columns(result: Mapping[str, Any]) -> dict[str, Any]:
    robots = result.get("robots") or {}
    policy = result.get("policy") or {}
    return {
        "robots_verdict": robots.get("verdict"),
        "robots_crawl_delay_s": robots.get("crawl_delay_s"),
        "policy_host_rule": policy.get("host_rule"),
        "query_stripped": 1 if policy.get("query_stripped") else 0,
    }


def _host_of(result: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    for candidate in (result.get("final_url"), result.get("url_norm"), row["url_norm"]):
        if not candidate:
            continue
        try:
            return peek_host(str(candidate))
        except WebFetchError:
            continue
    return "unknown-host"


def _record_refusal(store: Store, cfg: WebFetchConfig, row, result: Mapping[str, Any]) -> None:
    """Design §5's settlement table for every refusal.

    A refusal COMPLETES the job. It is never retried into the same wall: a
    paywall does not open on the second request, and a robots disallow is not
    a transient error. What varies is only whether a human could lawfully fix
    it, and that decides whether a ``wanted`` row appears.
    """
    fetch_id = row["fetch_id"]
    reason = result.get("reason")
    _touch(
        store,
        fetch_id,
        {
            "state": "refused",
            "outcome": "refused",
            "reason": reason,
            "final_url": result.get("final_url"),
            "redirect_chain": json.dumps(result.get("redirect_chain") or []),
            "http_status": result.get("http_status"),
            "content_type": result.get("content_type"),
            "resolved_ips": json.dumps(result.get("resolved_ips") or []),
            "bytes_out": result.get("bytes_out") or 0,
            "fetched_ts": result.get("fetched_ts"),
            "elapsed_ms": result.get("elapsed_ms") or 0,
            "sidecar_version": result.get("sidecar_version"),
            **_robots_policy_columns(result),
        },
    )
    source_id = None
    if reason in HUMAN_FIXABLE_REASONS:
        source_id = _file_wanted_source(store, cfg, row, reason)
    _event(
        store,
        "web_fetch_refused",
        {
            "fetch_id": fetch_id,
            "job_id": row["job_id"],
            "reason": reason,
            "http_status": result.get("http_status"),
            "wanted_source_id": source_id,
        },
        launch_id=row["launch_id"],
    )


def _file_wanted_source(store: Store, cfg: WebFetchConfig, row, reason: str | None) -> str | None:
    """A ``wanted`` request row the operator can fulfil by hand.

    The lawful outcome of design §1 P7: where a human *could* legitimately
    obtain the page — a paywall they subscribe to, a bot wall a real browser
    passes, a robots rule that does not bind a person reading one article —
    the request queue says so and the existing ``user_delivered`` path takes
    it from there. The ``rights_notes`` line says in words that no bypass was
    attempted, because that is the thing a reader of this row six months from
    now will want to know.
    """
    from trialerror.ingest.errors import LicenseRouteRefusedError
    from trialerror.ingest.pipeline import register_source
    from trialerror.ingest.requests import write_requests_md

    existing = store.knowledge.execute(
        "SELECT source_id FROM source WHERE url = ? AND request_state = 'wanted'",
        (row["url_norm"],),
    ).fetchone()
    if existing is not None:
        _touch(store, row["fetch_id"], {"source_id": existing["source_id"]})
        return str(existing["source_id"])

    raw_config = _config(store)
    try:
        source = register_source(
            store,
            kind="web",
            title=(row["title"] or row["url_norm"])[:200],
            license_tier="unknown",
            acquisition_route="user_delivered",
            registered_by_launch=row["launch_id"],
            url=row["url_norm"],
            rights_notes=(
                f"webfetch refused: {reason}. Fetch {row['fetch_id']}. The operator may deliver "
                "a saved copy via the existing delivery path; no bypass was attempted."
            ),
            request_state="wanted",
            config=raw_config,
        )
    except LicenseRouteRefusedError as exc:
        raise RuntimeError(
            f"web_fetch {row['fetch_id']}: cannot file a 'wanted' row — {exc}. Add "
            "'user_delivered' (and 'web') to [license].allowed_acquisition_routes."
        ) from exc
    _touch(store, row["fetch_id"], {"source_id": source["source_id"]})
    write_requests_md(store, store.program_root, raw_config)
    return str(source["source_id"])


# ---------------------------------------------------------------------------
# the extract handler
# ---------------------------------------------------------------------------


@register_handler("web_extract")
def run_web_extract(ctx) -> None:
    """Turn fetched bytes into corpus rows (design §2.2 step 3).

    No network, by construction: everything it reads is already on this
    machine's disk. Whatever the class, the tail is the harness's existing
    one — ``add_document`` enqueues ``normalize``/``ocr``, the sanitizer runs
    at element insert, ``stream_v1`` and ``build_chunk_anchor`` are untouched.
    That is why anchors written before this lane existed still resolve.
    """
    store: Store = ctx.store
    fetch_id = ctx.payload["fetch_id"]
    row = _row(store, fetch_id)
    if row is None:
        raise RuntimeError(f"web_extract: no such fetch row {fetch_id!r}")
    if row["state"] == "extracted":
        ctx.set_checkpoint({"webfetch": "already_extracted", "fetch_id": fetch_id})
        return
    if row["state"] != "fetched":
        raise RuntimeError(
            f"web_extract {fetch_id}: row is {row['state']!r}, expected 'fetched'"
        )

    raw_config = _config(store)
    cfg = load_webfetch_config(raw_config)
    raw_path = store.program_root / str(row["raw_path"])
    if not raw_path.is_file():
        raise RuntimeError(f"web_extract {fetch_id}: {raw_path} is missing")

    content_class = row["content_class"]
    if content_class == "html":
        _extract_html_document(ctx, store, cfg, raw_config, row, raw_path)
    elif content_class == "git":
        _extract_repo(ctx, store, cfg, raw_config, row, raw_path)
    elif content_class in ("pdf", "text"):
        _extract_single_file(ctx, store, cfg, raw_config, row, raw_path)
    else:
        raise RuntimeError(
            f"web_extract {fetch_id}: content_class {content_class!r} has no extraction route"
        )


def _license_tier_for_row(row, detected_tier: str | None, cfg: WebFetchConfig) -> str:
    """The operator's tag, then the page's own declaration, then the default.

    Order is the point. A ``--license-tier`` on the command line is a human
    saying what this is; a ``rel="license"`` is a page saying what it is; the
    configured default is what we say when nobody said anything. Only the
    first can produce ``commercial_restricted``, and only deliberately
    (design §4 T4)."""
    declared = row["license_detected"]
    if isinstance(declared, str) and declared.startswith("operator:"):
        return declared.split(":", 1)[1]
    return detected_tier or cfg.default_license_tier


def _rights_note(row, extra: str = "") -> str:
    parts = [
        f"fetched via webfetch {row['fetch_id']}",
        f"robots={row['robots_verdict'] or 'n/a'}",
        f"source url {row['url_norm']}",
    ]
    if extra:
        parts.append(extra)
    return "; ".join(parts)[:1000]


def _extract_html_document(ctx, store: Store, cfg: WebFetchConfig, raw_config, row, raw_path: Path) -> None:
    from trialerror.webfetch.extract import extract_html

    fetch_id = row["fetch_id"]
    headers = {}
    if row["headers_subset"]:
        try:
            headers = json.loads(row["headers_subset"])
        except ValueError:  # pragma: no cover - column is written by us
            headers = {}

    extraction = extract_html(
        raw_path.read_bytes(),
        final_url=row["final_url"] or row["url_norm"],
        content_type=row["content_type"],
        headers=headers,
        fetched_ts=row["fetched_ts"],
        fetch_id=fetch_id,
    )

    clean_path = raw_path.with_suffix(".clean.html")
    markdown_path = raw_path.with_suffix(".md")
    atomic_write_text(clean_path, extraction.clean_html)
    atomic_write_text(markdown_path, extraction.markdown)

    signals = extraction.signals()
    signals["js_markers"] = json.dumps(list(extraction.js_markers))
    extracted_sha = hashlib.sha256(extraction.clean_html.encode("utf-8")).hexdigest()
    changes: dict[str, Any] = {
        **signals,
        "extracted_sha256": extracted_sha,
        "clean_path": _rel(store.program_root, clean_path),
        "markdown_path": _rel(store.program_root, markdown_path),
    }
    if isinstance(row["license_detected"], str) and row["license_detected"].startswith("operator:"):
        # Keep the operator's tag; it outranks what the page says about
        # itself, and overwriting it here would lose the human's judgment.
        changes.pop("license_detected", None)

    if cfg.honor_tdm_optout and extraction.tdm_signals.get("optout"):
        # Ruling L-A3 leaves this off, and it is off in the shipped default.
        # When an operator turns it on, the page is not ingested and the
        # signal that caused it is on the row for anyone who asks why.
        _touch(store, fetch_id, {**changes, "state": "refused", "reason": "tdm_optout"})
        _record_settlement_refusal(store, cfg, _row(store, fetch_id), "tdm_optout")
        return

    if extraction.needs_render:
        # Thin AND JS-marked: the article never existed in the served HTML.
        # Design §5 sends this to the operator rather than ingesting a shell.
        _touch(store, fetch_id, {**changes, "state": "refused", "reason": "needs_render"})
        _record_settlement_refusal(store, cfg, _row(store, fetch_id), "needs_render")
        return

    row = _touch(store, fetch_id, changes)
    _ingest_one(
        ctx,
        store,
        cfg,
        raw_config,
        row,
        path=clean_path,
        media_type="html",
        content_sha256=extracted_sha,
        title=extraction.title,
        detected_tier=extraction.license_tier,
        extra_rights=f"license signal {extraction.license_detected or 'none'}",
    )


def _record_settlement_refusal(store: Store, cfg: WebFetchConfig, row, reason: str) -> None:
    """A refusal decided on THIS side of the boundary (thin/JS, TDM opt-out).

    Shares the ``wanted``-row and event path with a sidecar refusal, because
    from the operator's point of view they are the same event: a page that
    was asked for and did not arrive, with a reason and a way to fix it."""
    source_id = _file_wanted_source(store, cfg, row, reason) if reason in HUMAN_FIXABLE_REASONS else None
    _event(
        store,
        "web_fetch_refused",
        {
            "fetch_id": row["fetch_id"],
            "job_id": row["job_id"],
            "reason": reason,
            "decided_by": "web_extract",
            "wanted_source_id": source_id,
        },
        launch_id=row["launch_id"],
    )


def _extract_single_file(ctx, store: Store, cfg: WebFetchConfig, raw_config, row, raw_path: Path) -> None:
    """A PDF or a plain-text/markdown body: straight into ``add_document``.

    A scanned PDF takes the existing OCR route unchanged — including the
    offload branch when one is configured — because ``add_document`` chooses
    the stage from the media type and this lane did not touch that.
    """
    from trialerror.ingest.normalizers import detect_media_type

    media_type = "md" if row["content_class"] == "text" else detect_media_type(raw_path)
    _ingest_one(
        ctx,
        store,
        cfg,
        raw_config,
        row,
        path=raw_path,
        media_type=media_type,
        content_sha256=row["content_sha256"] or _sha256_file(raw_path),
        title=row["title"] or row["url_norm"],
        detected_tier=None,
    )


def _ingest_one(
    ctx,
    store: Store,
    cfg: WebFetchConfig,
    raw_config,
    row,
    *,
    path: Path,
    media_type: str,
    content_sha256: str,
    title: str | None,
    detected_tier: str | None,
    extra_rights: str = "",
) -> None:
    """Register one source + one document and hand off to the pipeline."""
    from trialerror.ingest.pipeline import add_document, register_source

    fetch_id = row["fetch_id"]
    source = register_source(
        store,
        kind="web",
        title=(title or row["url_norm"])[:200],
        license_tier=_license_tier_for_row(row, detected_tier, cfg),
        acquisition_route="web",
        registered_by_launch=row["launch_id"],
        url=row["final_url"] or row["url_norm"],
        content_sha256=content_sha256,
        rights_notes=_rights_note(row, extra_rights),
        request_state="delivered",
        config=raw_config,
    )
    source_id = source["source_id"]

    if source.get("dedup_of"):
        # Design §5: content dedup on the EXTRACTED hash. The same article
        # under two URLs is one document, and re-ingesting it would double
        # every chunk it owns.
        existing_doc = store.knowledge.execute(
            "SELECT doc_id FROM document WHERE source_id = ? ORDER BY doc_id LIMIT 1", (source_id,)
        ).fetchone()
        _touch(
            store,
            fetch_id,
            {
                "state": "extracted",
                "source_id": source_id,
                "doc_id": existing_doc["doc_id"] if existing_doc else None,
            },
        )
        _event(
            store,
            "web_fetch_dedup",
            {"fetch_id": fetch_id, "source_id": source_id, "content_sha256": content_sha256},
            launch_id=row["launch_id"],
        )
        ctx.set_checkpoint({"webfetch": "dedup", "source_id": source_id})
        return

    # ``yes=True`` is honest rather than convenient, and design §2.2 says so
    # out loud: ``estimate_cost``'s 3 KB-per-"page" proxy would refuse any
    # HTML document over ~150 KB, and the real gate for a web body is the
    # sidecar's byte caps, which have already been applied before this row
    # existed. Passing yes=False here would mean the caps ran and then a
    # second, wronger gate refused the result anyway.
    added = add_document(
        store,
        program_root=store.program_root,
        source_id=source_id,
        raw_path=path,
        created_by_launch=row["launch_id"],
        media_type=media_type,
        config=raw_config,
        yes=True,
    )
    doc_id = added["document"]["doc_id"]
    _touch(store, fetch_id, {"state": "extracted", "source_id": source_id, "doc_id": doc_id})
    _event(
        store,
        "web_fetch_extracted",
        {
            "fetch_id": fetch_id,
            "source_id": source_id,
            "doc_id": doc_id,
            "media_type": media_type,
            "words": row["extracted_words"],
            "thin_content": row["thin_content"],
        },
        launch_id=row["launch_id"],
    )
    ctx.set_checkpoint({"webfetch": "extracted", "doc_id": doc_id})


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


def _extract_repo(ctx, store: Store, cfg: WebFetchConfig, raw_config, row, tar_path: Path) -> None:
    """Unpack ``repo.tar`` and ingest its prose under ONE source.

    ``filter="data"`` is the whole safety story and it is Python's, not ours:
    on 3.12 it refuses absolute paths, ``..`` escapes, symlinks and links
    pointing outside the destination — the classic tar traversals — by
    raising rather than by writing. Refused members are counted and reported;
    the rest of the archive still lands, because one hostile entry in a
    thousand-file repository is not a reason to lose the repository.

    Code files are NOT ingested (design §2.2, deferred and named as
    deferred): READMEs, markdown, ``docs/`` prose and PDFs only.
    """
    from trialerror.ingest.pipeline import add_document, register_source

    fetch_id = row["fetch_id"]
    slug = f"{row['git_head'] or 'head'}"[:12]
    host = _host_of({}, row)
    destination = cfg.raw_dir(store.program_root, host) / f"{fetch_id}@{slug}"
    destination.mkdir(parents=True, exist_ok=True)

    refused_members: list[str] = []
    extracted_members = 0
    with tarfile.open(tar_path, "r:*") as archive:
        for member in archive:
            if extracted_members >= _REPO_MAX_FILES * 4:
                break
            try:
                archive.extract(member, path=destination, filter="data")
            except (tarfile.TarError, OSError, ValueError) as exc:
                refused_members.append(f"{member.name}: {type(exc).__name__}")
                continue
            extracted_members += 1

    source = register_source(
        store,
        kind="web",
        title=(row["title"] or row["url_norm"])[:200],
        license_tier=_license_tier_for_row(row, _repo_license_tier(destination), cfg),
        acquisition_route="web",
        registered_by_launch=row["launch_id"],
        url=row["final_url"] or row["url_norm"],
        content_sha256=row["content_sha256"],
        rights_notes=_rights_note(row, f"git commit {row['git_head'] or 'unknown'}"),
        request_state="delivered",
        config=raw_config,
    )
    source_id = source["source_id"]

    doc_ids: list[str] = []
    text_bytes = 0
    for path in _repo_ingestable_files(destination):
        if len(doc_ids) >= _REPO_MAX_FILES:
            break
        size = path.stat().st_size
        is_pdf = path.suffix.lower() == ".pdf"
        if is_pdf and size > _REPO_MAX_PDF_BYTES:
            continue
        if not is_pdf:
            if text_bytes + size > _REPO_MAX_TEXT_BYTES:
                break
            text_bytes += size
        media_type = None if is_pdf else "md"
        added = add_document(
            store,
            program_root=store.program_root,
            source_id=source_id,
            raw_path=path,
            created_by_launch=row["launch_id"],
            media_type=media_type,
            config=raw_config,
            yes=True,
        )
        doc_ids.append(added["document"]["doc_id"])
        ctx.set_checkpoint({"webfetch": "repo", "documents": len(doc_ids)})

    _touch(
        store,
        fetch_id,
        {
            "state": "extracted",
            "source_id": source_id,
            "doc_id": doc_ids[0] if doc_ids else None,
            "extracted_words": None,
        },
    )
    _event(
        store,
        "web_fetch_extracted",
        {
            "fetch_id": fetch_id,
            "source_id": source_id,
            "documents": len(doc_ids),
            "git_head": row["git_head"],
            "refused_members": refused_members[:20],
            "refused_member_count": len(refused_members),
        },
        launch_id=row["launch_id"],
    )


def _repo_ingestable_files(root: Path) -> list[Path]:
    """READMEs first, then markdown/rst/txt and ``docs/``, then PDFs.

    Order is deliberate: the caps below cut the tail off a large repository,
    and the README is the file a researcher wanted when they listed the repo.
    """
    readmes: list[Path] = []
    prose: list[Path] = []
    pdfs: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        parts = {p.lower() for p in path.parts}
        if ".git" in parts:
            continue
        suffix = path.suffix.lower()
        if path.name.lower().startswith("readme") and suffix in _REPO_TEXT_SUFFIXES + ("",):
            readmes.append(path)
        elif suffix == ".pdf":
            pdfs.append(path)
        elif suffix in _REPO_TEXT_SUFFIXES:
            prose.append(path)
    return readmes + prose + pdfs


def _repo_license_tier(root: Path) -> str | None:
    from trialerror.webfetch.extract import license_tier_for
    from trialerror.webfetch.gitfetch import LICENSE_FILENAMES, detect_license_id

    for name in LICENSE_FILENAMES:
        candidate = root / name
        if not candidate.is_file():
            matches = [p for p in root.glob(f"*/{name}")]
            candidate = matches[0] if matches else candidate
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")[:64000]
            except OSError:  # pragma: no cover - defensive
                continue
            spdx = detect_license_id(text)
            if spdx in ("MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "BSD", "CC"):
                return "open"
            return license_tier_for(spdx)
    return None


# ---------------------------------------------------------------------------
# read-only views (status / report / links)
# ---------------------------------------------------------------------------


def fetch_rows(
    store: Store,
    *,
    list_ref: str | None = None,
    state: str | None = None,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_superseded:
        clauses.append("superseded_by IS NULL")
    if list_ref is not None:
        clauses.append("list_ref = ?")
        params.append(list_ref)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.knowledge.execute(
        f"SELECT * FROM web_fetch {where} ORDER BY created_ts, fetch_id", params
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_report(store: Store, *, list_ref: str | None = None) -> list[dict[str, Any]]:
    """``url | verdict | source_id | doc_id | status | chunks | anchors_ok``
    (design §5). Counts, not content."""
    out: list[dict[str, Any]] = []
    for row in fetch_rows(store, list_ref=list_ref):
        chunks = 0
        anchors_ok = 0
        status = None
        if row["doc_id"]:
            document = get(store, "document", pk_column="doc_id", pk_value=row["doc_id"])
            status = document["status"] if document else None
            chunks = store.knowledge.execute(
                "SELECT count(*) AS n FROM chunk WHERE doc_id = ?", (row["doc_id"],)
            ).fetchone()["n"]
            anchors_ok = store.knowledge.execute(
                "SELECT count(*) AS n FROM quote_anchor a JOIN document d ON d.doc_id = a.doc_id "
                "WHERE a.doc_id = ? AND a.doc_sha256 = d.sha256",
                (row["doc_id"],),
            ).fetchone()["n"]
        out.append(
            {
                "fetchId": row["fetch_id"],
                "url": row["url_norm"],
                "verdict": row["reason"] or row["outcome"] or row["state"],
                "state": row["state"],
                "sourceId": row["source_id"],
                "docId": row["doc_id"],
                "documentStatus": status,
                "chunks": chunks,
                "anchorsOk": anchors_ok,
                "thinContent": row["thin_content"],
                "words": row["extracted_words"],
            }
        )
    return out


def recorded_links(store: Store, fetch_id: str) -> list[str]:
    """The links one page named — data, and only data.

    Printing them is the whole point: a human or an agent reads the list,
    picks one, and passes it back through ``webfetch add``, which re-runs
    every check from the start. There is no path from this list to a job that
    does not go through that door (design §1 P3)."""
    row = _row(store, fetch_id)
    if row is None:
        raise WebFetchHandlerError(f"no such fetch: {fetch_id!r}")
    if not row["links_json"]:
        return []
    try:
        links = json.loads(row["links_json"])
    except ValueError:  # pragma: no cover - column is written by us
        return []
    return [str(link) for link in links if isinstance(link, str)]
