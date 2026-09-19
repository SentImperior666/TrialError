"""The sidecar loop — claim, decide, fetch, publish, audit. One job at a time.

Design §2.1/§2.2. This is the whole of what runs in the container that has
egress. It reads five directories and nothing else:

===========  ===========================================================
``/queue``   manifests in, results out — the only thing shared with
             the research container
``/policy``  the operator's allowlist and caps, mounted **read-only**
``/audit``   the authoritative audit trail, invisible to the research side
``/state``   robots cache and daily counters — the guard's own memory
``/work``    per-job scratch for clones, emptied after every job
===========  ===========================================================

No corpus, no database, no secrets, no ``/workspace``. The harness it runs is
the copy baked into the image (design §4 T8): an agent editing the checkout
in the research container changes nothing here.

**Single-threaded on purpose.** One job at a time is the simplest correct
implementation of "≥ 3 s between requests to one host", and pacing that
cannot be raced is worth more than throughput on a queue whose realistic
depth is a links list.

**Every attempt ends in a record.** Fetched, unchanged, refused, or a
manifest that did not even parse — each produces a ``result.json`` published
by atomic rename and one audit line carrying the job and launch ids. There
is no path through this loop that consumes a manifest and leaves nothing
behind; "the fetch just never happened" is the one outcome an operator
cannot investigate.

**A refusal is a result, never a retry.** Bot walls, paywalls, robots
disallow and policy refusals are published to ``failed/`` and settled there.
Nothing in this loop tries a refused fetch a second way — that is the whole
of the no-evasion rule (C-0069, C-0048/49), expressed as control flow.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from trialerror.util.atomic import atomic_write_text
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now_dt
from trialerror.webfetch import SIDECAR_VERSION, WebFetchRefused
from trialerror.webfetch.fetcher import Fetcher
from trialerror.webfetch.gitfetch import GitFetcher, parse_git_url
from trialerror.webfetch.netguard import NetGuard
from trialerror.webfetch.policy import Counters, HostPacer, Policy, PolicyError
from trialerror.webfetch.protocol import (
    BODY_FILENAME,
    REPO_FILENAME,
    Claim,
    Manifest,
    Queue,
    append_jsonl,
    new_result,
)
from trialerror.webfetch.robots import RobotsCache, RobotsVerdict
from trialerror.webfetch.urlcheck import NormalizedUrl

__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_RECLAIM_AFTER_S",
    "SidecarPaths",
    "SidecarStats",
    "Sidecar",
]

DEFAULT_POLL_INTERVAL_S = 5.0
#: A claim whose heartbeat is this old belonged to a process that is gone.
#: An hour is long enough that no live job is ever stolen (the longest
#: permitted single job is a 300 s clone) and short enough that a killed
#: container does not park a job until the 24 h manifest expiry.
DEFAULT_RECLAIM_AFTER_S = 3600.0

#: ``/state`` file remembering the last ``ETag``/``Last-Modified`` this
#: sidecar itself saw per ``url_norm`` (design §4 T2 — see
#: :meth:`Sidecar._conditional_for`).
CONDITIONAL_STATE_FILE = "conditional.json"
#: Bound on that file. A links-list-shaped queue never approaches it; a
#: sidecar that has run for years must still not hold an unbounded map.
MAX_CONDITIONAL_ENTRIES = 5000

_TDM_TOKENS = ("noai", "noimageai", "notrain", "tdm-reservation=1")


@dataclass(frozen=True)
class SidecarPaths:
    """The five mounts, resolved once."""

    queue: Path
    policy: Path
    audit: Path
    state: Path
    work: Path

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        queue: str | Path | None = None,
        policy: str | Path | None = None,
        audit: str | Path | None = None,
        state: str | Path | None = None,
        work: str | Path | None = None,
    ) -> "SidecarPaths":
        """Read the ``TE_WEBFETCH_*`` variables the compose service sets,
        with explicit arguments (the CLI flags) winning.

        The development mode of design §5 is exactly this: point the five
        paths at local directories and the same loop runs on a workstation
        against a local queue, with no container involved.
        """
        source = dict(os.environ if env is None else env)

        def pick(explicit: str | Path | None, key: str, default: str) -> Path:
            if explicit is not None:
                return Path(explicit)
            return Path(source.get(key, default))

        return cls(
            queue=pick(queue, "TE_WEBFETCH_QUEUE", "/queue"),
            policy=pick(policy, "TE_WEBFETCH_POLICY", "/policy"),
            audit=pick(audit, "TE_WEBFETCH_AUDIT", "/audit"),
            state=pick(state, "TE_WEBFETCH_STATE", "/state"),
            work=pick(work, "TE_WEBFETCH_WORK", "/work"),
        )


@dataclass
class SidecarStats:
    """What one run did. Ids and counts only — never page text (C-0007)."""

    claimed: int = 0
    fetched: int = 0
    unchanged: int = 0
    refused: int = 0
    reclaimed: int = 0
    polls: int = 0
    jobs: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "fetched": self.fetched,
            "unchanged": self.unchanged,
            "refused": self.refused,
            "reclaimed": self.reclaimed,
            "polls": self.polls,
            "jobs": list(self.jobs),
        }


class Sidecar:
    """The fetch loop.

    Every collaborator is injectable, and every injection point is a
    constructor argument rather than a configuration key — the fail-closed
    rule of design §4 is that nothing a deployment *mounts* can weaken the
    address policy, so the seams that could (``netguard``, the two
    factories) exist only in Python.
    """

    def __init__(
        self,
        paths: SidecarPaths,
        *,
        worker_id: str | None = None,
        require_allowlist: bool = True,
        netguard: NetGuard | None = None,
        fetcher_factory: Callable[[Policy, NetGuard, HostPacer], Fetcher] | None = None,
        gitfetcher_factory: Callable[[Policy, Path], GitFetcher] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        reclaim_after_s: float = DEFAULT_RECLAIM_AFTER_S,
        _time_fn: Callable[[], float] | None = None,
        _sleep_fn: Callable[[float], None] | None = None,
        _now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self.worker_id = worker_id or _default_worker_id()
        self.require_allowlist = bool(require_allowlist)
        self.poll_interval_s = float(poll_interval_s)
        self.reclaim_after_s = float(reclaim_after_s)
        self._time_fn = _time_fn or time.monotonic
        self._sleep_fn = _sleep_fn or time.sleep
        self.queue = Queue(paths.queue, _now_fn=_now_fn or now_dt)
        self.netguard = netguard or NetGuard()
        self._fetcher_factory = fetcher_factory or (
            lambda policy, guard, pacer: Fetcher(
                policy,
                guard,
                pacer=pacer,
                _time_fn=self._time_fn,
                _sleep_fn=self._sleep_fn,
            )
        )
        self._gitfetcher_factory = gitfetcher_factory or (
            lambda policy, work: GitFetcher(policy.caps, work_dir=work)
        )
        self.pacer = HostPacer(3.0, _time_fn=self._time_fn, _sleep_fn=self._sleep_fn)
        self._current_fetcher: Fetcher | None = None
        self.robots = RobotsCache(self._fetch_robots, _time_fn=self._time_fn)

    # -- loop ------------------------------------------------------------
    def run(
        self,
        *,
        max_jobs: int | None = None,
        max_idle_polls: int | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> SidecarStats:
        """Claim and process until told to stop.

        ``max_jobs``/``max_idle_polls`` bound a run for the CLI's ``--once``
        and for tests; the container runs with neither and is stopped by
        docker.
        """
        self.queue.ensure_layout()
        self.paths.audit.mkdir(parents=True, exist_ok=True)
        self.paths.state.mkdir(parents=True, exist_ok=True)
        self.paths.work.mkdir(parents=True, exist_ok=True)

        stats = SidecarStats()
        idle = 0
        while True:
            if stop is not None and stop():
                break
            stats.polls += 1
            self.queue.touch_sidecar_heartbeat(
                {"worker_id": self.worker_id, "version": SIDECAR_VERSION}
            )
            stats.reclaimed += len(self.queue.reclaim(self.reclaim_after_s))

            claim = self.queue.claim_next(self.worker_id)
            if claim is None:
                idle += 1
                if max_idle_polls is not None and idle >= max_idle_polls:
                    break
                self._sleep_fn(self.poll_interval_s)
                continue

            idle = 0
            record = self.process(claim)
            stats.claimed += 1
            # Bounded: this list is a run summary for the CLI envelope, not a
            # log. A container that runs for weeks must not accumulate one
            # dict per fetch in memory.
            stats.jobs.append(record)
            del stats.jobs[:-50]
            outcome = record.get("outcome")
            if outcome == "fetched":
                stats.fetched += 1
            elif outcome == "unchanged":
                stats.unchanged += 1
            else:
                stats.refused += 1
            if max_jobs is not None and stats.claimed >= max_jobs:
                break

        self.queue.touch_sidecar_heartbeat(
            {"worker_id": self.worker_id, "version": SIDECAR_VERSION}
        )
        return stats

    # -- one job ---------------------------------------------------------
    def process(self, claim: Claim) -> dict:
        """Settle one claim. Always publishes; never raises for a refusal."""
        self.queue.heartbeat(claim)

        if claim.manifest is None:
            assert claim.error is not None
            return self._settle_refusal(
                claim,
                manifest=_placeholder_manifest(claim.job_id),
                refusal=claim.error,
                url_norm="",
                host=None,
                robots=None,
                host_rule=None,
            )

        manifest = claim.manifest
        try:
            policy = Policy.load(self.paths.policy, require_allowlist=self.require_allowlist)
        except PolicyError:
            # The sidecar does not know what it may do. Put the job back and
            # let the failure be loud: a container that will not start is a
            # visible condition, and an unreadable policy must never degrade
            # into "fetch anything".
            self.queue.unclaim(claim)
            raise

        self.pacer.min_interval_s = policy.caps.min_host_interval_s
        self.robots.ttl_s = policy.caps.robots_ttl_s
        self.robots.crawl_delay_cap_s = policy.caps.crawl_delay_cap_s
        counters = Counters(self.paths.state, policy.caps)
        fetcher = self._fetcher_factory(policy, self.netguard, self.pacer)
        self._current_fetcher = fetcher

        try:
            self._check_disk(policy)
            if manifest.kind == "git":
                return self._process_git(claim, manifest, policy, counters)
            return self._process_http(claim, manifest, policy, counters, fetcher)
        except WebFetchRefused as exc:
            host = str(exc.context.get("host") or "") or None
            if host:
                counters.record(
                    host,
                    manifest.effective_origin,
                    bytes_in=0,
                    bytes_out=int(exc.context.get("bytes_out") or 0),
                )
            return self._settle_refusal(
                claim,
                manifest=manifest,
                refusal=exc,
                url_norm=str(exc.context.get("url_norm") or ""),
                host=host,
                robots=None,
                host_rule=str(exc.context.get("host_rule") or "") or None,
            )
        except Exception as exc:
            # "Every attempt ends in a record" is what the closed reason
            # vocabulary buys for every *modelled* outcome. An unmodelled
            # crash is a bug and the container must still die visibly — so
            # this re-raises — but the job may NOT go back to `pending/`.
            #
            # It used to. With `restart: unless-stopped` that made one
            # crashing manifest a permanent, self-replaying kill of the whole
            # fetch subsystem: claim, crash, restart, claim the same job,
            # crash, with no `failed/` entry and no audit line to say why.
            # A single hand-written manifest was enough. So the job is
            # settled first, as `manifest_invalid` (the queue/protocol reason
            # — this job's bytes are what the sidecar could not process),
            # which writes `failed/<job>/result.json` and one audit line;
            # only then does the exception continue on its way. The restart
            # comes back to an empty queue and the operator has a record.
            try:
                self._settle_refusal(
                    claim,
                    manifest=manifest,
                    refusal=WebFetchRefused(
                        "manifest_invalid",
                        f"unmodelled {type(exc).__name__} while processing "
                        f"{manifest.job_id}: {exc}",
                    ),
                    url_norm="",
                    host=None,
                    robots=None,
                    host_rule=None,
                )
            except Exception:
                # The settlement itself is what broke (an unwritable queue,
                # a full disk). Nothing can be recorded here; fall back to
                # the old behaviour so the manifest is at least not lost.
                self.queue.unclaim(claim)
            raise
        except BaseException:
            # KeyboardInterrupt / SystemExit: the operator stopping the loop,
            # not a defect in this job. Give the manifest back untouched —
            # burning a fetch because someone pressed ^C would be wrong.
            self.queue.unclaim(claim)
            raise
        finally:
            self._current_fetcher = None

    # -- http ------------------------------------------------------------
    def _process_http(
        self,
        claim: Claim,
        manifest: Manifest,
        policy: Policy,
        counters: Counters,
        fetcher: Fetcher,
    ) -> dict:
        nurl, rule = fetcher.resolve_target(manifest.url, origin=manifest.effective_origin)
        counters.check(nurl.host, manifest.effective_origin)

        verdict = self.robots.verdict_for(
            nurl,
            kind=manifest.kind,
            override_requested=manifest.robots_override_ruling,
            approved_ruling=policy.robots_ruling_for(nurl.url_norm),
        )
        try:
            verdict.raise_if_refused(nurl.host)
        except WebFetchRefused as exc:
            exc.context.setdefault("url_norm", nurl.url_norm)
            exc.context.setdefault("host_rule", rule.source)
            counters.record(nurl.host, manifest.effective_origin)
            return self._settle_refusal(
                claim,
                manifest=manifest,
                refusal=exc,
                url_norm=nurl.url_norm,
                host=nurl.host,
                robots=verdict,
                host_rule=rule.source,
            )
        self.pacer.set_crawl_delay(nurl.host, verdict.crawl_delay_s)
        self.queue.heartbeat(claim)

        etag, last_modified = self._conditional_for(manifest, nurl.url_norm)
        try:
            fetched = fetcher.fetch(
                manifest.url,
                kind=manifest.kind,
                origin=manifest.effective_origin,
                etag=etag,
                last_modified=last_modified,
            )
        except WebFetchRefused as exc:
            exc.context.setdefault("url_norm", nurl.url_norm)
            exc.context.setdefault("host_rule", rule.source)
            raise

        self._remember_conditional(nurl.url_norm, fetched.headers_subset)
        counters.record(
            nurl.host,
            manifest.effective_origin,
            bytes_in=len(fetched.body),
            bytes_out=fetched.bytes_out,
        )

        tdm = self._tdm_signal(fetched.headers_subset)
        if tdm and policy.honor_tdm_optout:
            refusal = WebFetchRefused(
                "tdm_optout",
                f"{nurl.host} signals {tdm}; honor_tdm_optout is on",
                host=nurl.host,
            )
            return self._settle_refusal(
                claim,
                manifest=manifest,
                refusal=refusal,
                url_norm=nurl.url_norm,
                host=nurl.host,
                robots=verdict,
                host_rule=rule.source,
                http_status=fetched.http_status,
                bytes_out=fetched.bytes_out,
                resolved_ips=fetched.resolved_ips,
                elapsed_ms=fetched.elapsed_ms,
            )

        result = new_result(
            manifest=manifest,
            outcome=fetched.outcome,
            url_norm=nurl.url_norm,
            final_url=fetched.final_url.url,
            elapsed_ms=fetched.elapsed_ms,
            http_status=fetched.http_status,
            content_type=fetched.content_type,
            content_class=fetched.content_class,
            payload_bytes=len(fetched.body),
            content_sha256=fetched.sha256,
            resolved_ips=fetched.resolved_ips,
            redirect_chain=fetched.redirect_chain,
            bytes_out=fetched.bytes_out,
            headers_subset=fetched.headers_subset,
            robots=verdict.as_result_block(),
            policy={
                "verdict": "allow",
                "host_rule": rule.source,
                "query_stripped": fetched.query_stripped,
            },
        )
        payloads = {BODY_FILENAME: fetched.body} if fetched.body else None
        self.queue.publish(claim, result, payloads)
        return self._audit(claim, result, host=nurl.host)

    # -- git -------------------------------------------------------------
    def _process_git(
        self, claim: Claim, manifest: Manifest, policy: Policy, counters: Counters
    ) -> dict:
        spec = parse_git_url(manifest.url)
        rule = policy.rule_for(spec.host)
        if not rule.allow_git:
            # The proposal is written once, by the settlement path, so a
            # refusal never produces two rows for one human decision.
            raise WebFetchRefused(
                "host_not_allowed",
                f"{spec.host} is approved for fetching but not for cloning (no 'git' flag)",
                host=spec.host,
                url_norm=spec.url_norm,
                host_rule=rule.source,
                propose_flags=["git"],
            )
        counters.check(spec.host, manifest.effective_origin)

        # Design §1 P5 promises TWO independent SSRF layers on every fetch,
        # and `git clone` brings neither: it does its own DNS and (with
        # git's default `http.followRedirects=initial`) its own redirect
        # following, so NetGuard's resolve-validate-pin never runs on this
        # path. The pin is not recoverable — the addresses go to a child
        # process, not to a socket this module holds — but the *validation*
        # is, and so is the audit: resolve the clone host here, refuse
        # `ip_private`/`dns_failed`/`ipv6_unsupported` before git is
        # invoked, and put the answers in `result.json` so the record can
        # say where the bytes came from. (`gitfetch` closes the redirect
        # half by pinning `http.followRedirects=false`.) In DEV mode —
        # `webfetch sidecar --foreground` on a workstation, no container and
        # therefore no kernel firewall — this is the only layer there is.
        resolution = self.netguard.resolve(spec.host, 443)

        started = self._time_fn()
        gitfetcher = self._gitfetcher_factory(policy, self.paths.work)
        try:
            archive = gitfetcher.fetch(spec, job_id=claim.job_id)
            self.queue.heartbeat(claim)
            counters.record(spec.host, manifest.effective_origin, bytes_in=archive.size_bytes)
            result = new_result(
                manifest=manifest,
                outcome="fetched",
                url_norm=spec.url_norm,
                final_url=spec.clone_url,
                elapsed_ms=int((self._time_fn() - started) * 1000),
                http_status=None,
                content_type="application/x-tar",
                content_class="git",
                payload_bytes=archive.size_bytes,
                content_sha256=archive.sha256,
                resolved_ips=list(resolution.addresses),
                robots={"fetched": False, "verdict": "n/a", "crawl_delay_s": 0},
                policy={
                    "verdict": "allow",
                    "host_rule": rule.source,
                    "query_stripped": False,
                },
                git=archive.as_result_git_block(),
            )
            self.queue.publish(claim, result, {REPO_FILENAME: archive.tar_path})
            return self._audit(claim, result, host=spec.host)
        except WebFetchRefused as exc:
            # Counted once, by the handler in `process`: an attempt that
            # reached the network costs cap whether it succeeded or not, but
            # it must not cost it twice.
            exc.context.setdefault("host", spec.host)
            exc.context.setdefault("url_norm", spec.url_norm)
            exc.context.setdefault("host_rule", rule.source)
            exc.context.setdefault("resolved_ips", list(resolution.addresses))
            raise
        finally:
            # `/work` is scratch, never an archive: a clone that failed
            # halfway must not leave a partial repository for the next job
            # to trip over.
            gitfetcher.cleanup(claim.job_id)

    # -- settlement ------------------------------------------------------
    def _settle_refusal(
        self,
        claim: Claim,
        *,
        manifest: Manifest,
        refusal: WebFetchRefused,
        url_norm: str,
        host: str | None,
        robots: RobotsVerdict | None,
        host_rule: str | None,
        http_status: int | None = None,
        bytes_out: int = 0,
        resolved_ips: tuple[str, ...] = (),
        elapsed_ms: int = 0,
    ) -> dict:
        context = refusal.context
        if refusal.reason in ("host_not_allowed", "redirect_off_allowlist") and host:
            flags = context.get("propose_flags")
            self.queue.append_proposal(
                {
                    "host": host,
                    "flags": list(flags) if isinstance(flags, (list, tuple)) else [],
                    "launch_id": manifest.launch_id,
                    "job_id": manifest.job_id,
                    "example_url": manifest.url,
                    "reason": refusal.reason,
                }
            )
        result = new_result(
            manifest=manifest,
            outcome="refused",
            url_norm=url_norm or str(context.get("url_norm") or ""),
            final_url=str(context.get("final_url") or "") or None,
            elapsed_ms=int(context.get("elapsed_ms") or elapsed_ms),
            http_status=_as_int(context.get("http_status"), http_status),
            content_type=None,
            content_class=None,
            payload_bytes=0,
            content_sha256=None,
            resolved_ips=list(context.get("resolved_ips") or resolved_ips),
            redirect_chain=list(context.get("redirect_chain") or ()),
            bytes_out=int(context.get("bytes_out") or bytes_out),
            robots=robots.as_result_block() if robots is not None else None,
            policy={
                "verdict": "refused",
                "host_rule": host_rule or str(context.get("host_rule") or "") or None,
                "query_stripped": bool(context.get("query_stripped") or False),
            },
            reason=refusal.reason,
        )
        self.queue.fail(claim, result)
        return self._audit(claim, result, host=host, detail=refusal.detail)

    def _audit(
        self,
        claim: Claim,
        result: Mapping[str, object],
        *,
        host: str | None,
        detail: str = "",
    ) -> dict:
        """One line per attempt, in two places.

        The copy under ``/audit`` is authoritative — the research side cannot
        see it, so nothing running there can edit its own trail. The copy in
        the queue exists only so the in-container doctor check has something
        to read (design §3.4, the same split as the two ``mass_deletion``
        flags).
        """
        record = {
            "job_id": result.get("job_id"),
            "fetch_id": result.get("fetch_id"),
            "launch_id": result.get("launch_id"),
            "worker_id": self.worker_id,
            "url_norm": result.get("url_norm"),
            "host": host,
            "outcome": result.get("outcome"),
            "reason": result.get("reason"),
            "http_status": result.get("http_status"),
            "bytes": result.get("bytes"),
            "bytes_out": result.get("bytes_out"),
            "elapsed_ms": result.get("elapsed_ms"),
            "sidecar_version": result.get("sidecar_version"),
        }
        if detail:
            record["detail"] = detail[:500]
        day = now_dt().strftime("%Y-%m-%d")
        append_jsonl(self.paths.audit / f"webfetch-{day}.jsonl", record)
        self.queue.append_audit(record)
        return record

    # -- conditional headers ---------------------------------------------
    #
    # Design §4 T2: "conditional headers only from the stored etag/
    # last-modified of the **same host**". The manifest carries a
    # ``conditional`` block, and the manifest is written by the research
    # side — so taking those two values on trust would put ~512 B of
    # attacker-chosen bytes on the wire per request (`If-None-Match:
    # <anything ASCII, 256 chars>`), which is the one request-shape claim
    # the design makes that the shape alone does not keep. The values are
    # not *forbidden*: they are checked against what this sidecar itself
    # recorded the last time it fetched the same ``url_norm``. A `refresh`
    # replaying the etag the research side stored matches and is sent; a
    # value the sidecar has never seen coming back from that URL is
    # dropped, and the request goes out unconditional.
    def _conditional_path(self) -> Path:
        return self.paths.state / CONDITIONAL_STATE_FILE

    def _conditional_seen(self) -> dict:
        try:
            data = json.loads(self._conditional_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _conditional_for(self, manifest: Manifest, url_norm: str) -> tuple[str | None, str | None]:
        """The conditional headers this request may carry.

        A manifest value is passed through only when it equals the one this
        sidecar recorded for the same ``url_norm``; anything else becomes
        ``None`` and the request is unconditional (one extra full body, and
        the design already says a re-fetch is never free).
        """
        want_etag = manifest.etag
        want_modified = manifest.last_modified
        if want_etag is None and want_modified is None:
            return None, None
        entry = self._conditional_seen().get(url_norm)
        if not isinstance(entry, dict):
            return None, None
        etag = want_etag if want_etag is not None and want_etag == entry.get("etag") else None
        modified = (
            want_modified
            if want_modified is not None and want_modified == entry.get("last_modified")
            else None
        )
        return etag, modified

    def _remember_conditional(
        self, url_norm: str, headers_subset: Mapping[str, str | None]
    ) -> None:
        """Record what the origin server just said about this URL's version."""
        etag = headers_subset.get("etag")
        last_modified = headers_subset.get("last-modified")
        if etag is None and last_modified is None:
            return
        data = self._conditional_seen()
        data[url_norm] = {"etag": etag, "last_modified": last_modified}
        if len(data) > MAX_CONDITIONAL_ENTRIES:
            # Bounded: `/state` is the guard's own memory, not an archive.
            # Python dicts keep insertion order, so this drops the URLs this
            # sidecar has not touched in longest — the cost of a miss is one
            # unconditional request.
            for key in list(data)[: len(data) - MAX_CONDITIONAL_ENTRIES]:
                del data[key]
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._conditional_path(), json.dumps(data, sort_keys=True) + "\n"
            )
        except OSError:
            # Same posture as `Counters`: a cache that cannot be written is a
            # cosmetic failure (the next refresh costs a full body), not a
            # reason to fail a fetch that already succeeded.
            pass

    # -- helpers ---------------------------------------------------------
    def _check_disk(self, policy: Policy) -> None:
        used = self.queue.disk_usage_bytes()
        if used > policy.caps.queue_disk_cap:
            raise WebFetchRefused(
                "disk_cap",
                f"the queue holds {used} bytes, cap {policy.caps.queue_disk_cap}; "
                "the research side has not swept its results",
            )

    def _fetch_robots(self, robots_url: NormalizedUrl):
        if self._current_fetcher is None:  # pragma: no cover - defensive
            raise WebFetchRefused(
                "robots_unavailable", "no fetcher is active", host=robots_url.host
            )
        return self._current_fetcher.fetch_robots(robots_url)

    @staticmethod
    def _tdm_signal(headers: Mapping[str, str | None]) -> str | None:
        """Report a text-and-data-mining opt-out signal in the headers.

        Recorded on every fetch regardless of the knob (ruling L-A3), so the
        provenance record says what the site asked for even when the
        internal-research posture proceeds anyway.
        """
        raw = (headers.get("x-robots-tag") or "").lower()
        for token in _TDM_TOKENS:
            if token in raw:
                return token
        return None


def _as_int(value: object, fallback: int | None) -> int | None:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    return fallback


def _placeholder_manifest(job_id: str) -> Manifest:
    """A stand-in for a manifest that did not parse.

    The ids are literally ``unknown`` and that is the point: a job whose
    manifest could not be read is by definition unattributed, and the
    doctor's ``webfetch_unattributed`` check should say so rather than be
    handed a plausible-looking id this module invented.
    """
    return Manifest.build(
        job_id=job_id,
        fetch_id="unknown",
        launch_id="unknown",
        url="about:unparseable-manifest",
        kind="page",
        origin="agent",
    )


def _default_worker_id() -> str:
    return f"sidecar-{new_id('WRK').split('-', 1)[-1][:12]}"
