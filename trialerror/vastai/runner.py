"""``trialerror vastai run``: serve the embed offload queue from a rented GPU.

Order of operations (design sections 2.2, 4, 5):

1. refuse unless ``[ingest.embed] backend = "offload"`` and ``gpu = "vastai"``;
2. select pending EMBED markers (never OCR);
3. high tier -> verify the operator approval (refuse without it);
4. one read-only offer search; rank by estimated tokens per dollar;
   size the batch so the TTL stays under the cap; refuse if the worst-case
   cost exceeds the per-job cap -- all BEFORE anything is created;
5. high tier -> stderr banner + ``vastai_high_tier_use`` event, before create;
6. :class:`~trialerror.vastai.lease.InstanceLease`: create, wait, bootstrap,
   then for each job the DEV worker's own ``_process_one`` with a
   :class:`~trialerror.vastai.remote.RemoteEmbedBackend`, publishing into
   ``done/`` where ``stage.py`` verifies it exactly as it verifies DEV;
7. destroy in ``finally``; a ``vastai_run`` event records what happened.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

from trialerror.events.api import append_event
from trialerror.offload import protocol
from trialerror.offload.marker import OFFLOAD_BACKEND_NAME, OffloadMarker, gpu_executor
from trialerror.offload.stage import EMBED_INPUT_NAME
from trialerror.offload.transport import LocalTransport
from trialerror.offload.worker import _HEARTBEAT_INTERVAL_S, _process_one
from trialerror.util.ids import new_id
from trialerror.vastai import guard
from trialerror.vastai.api import OfferUnavailable, VastClient
from trialerror.vastai.lease import InstanceLease, runs_dir
from trialerror.vastai.remote import SERVE_SOURCE, LeaseExpired, RemoteEmbedBackend, SshChannel, module_bytes_and_sha
from trialerror.vastai.tiers import (
    TOKENS_PER_BYTE,
    PlanRefused,
    VastConfig,
    load_vast_config,
    plan_run,
    rank_offers,
    ttl_for,
)

__all__ = ["VastRunRefused", "select_embed_jobs", "prepare_run", "run_vastai"]

#: Offers tried when each is taken between search and create (no rental
#: happens on a ``no_such_ask``), before the run gives up.
_MAX_OFFER_ATTEMPTS = 5


class VastRunRefused(RuntimeError):
    pass


class _VastBackends:
    """The ``DevBackends`` shape ``_process_one`` expects; embed only."""

    def __init__(self, embed_backend: RemoteEmbedBackend):
        self._embed = embed_backend

    def validate(self) -> None:
        return None

    def ocr(self) -> Any:
        raise RuntimeError("the vast.ai executor serves embed markers only")

    def embed(self) -> Any:
        return self._embed


def _job_text_bytes(root: Path, job_id: str) -> int:
    path = protocol.pending_dir(root) / job_id / EMBED_INPUT_NAME
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            total += len(str(json.loads(line).get("text", "")).encode("utf-8"))
    return total


def select_embed_jobs(root: Path) -> list[tuple[str, int]]:
    """Pending markers whose stage is ``embed``: ``[(job_id, text_bytes)]``, oldest first."""
    out = []
    for job_id in protocol.list_pending(root):
        try:
            manifest = protocol.read_json(protocol.pending_dir(root) / f"{job_id}.json")
            if manifest.get("stage") != "embed":
                continue
            out.append((job_id, _job_text_bytes(root, job_id)))
        except (OSError, ValueError):
            continue  # raced with a claim, or unreadable: not ours to judge here
    return out


def _embed_table(raw: dict[str, Any]) -> dict[str, Any]:
    return dict((raw.get("ingest") or {}).get("embed") or {})


def prepare_run(
    program_root: Path,
    raw: dict[str, Any],
    *,
    client: VastClient | None = None,
    max_jobs: int | None = None,
    exclude_offer_ids: Iterable[Any] = (),
) -> dict[str, Any]:
    """Steps 1-4: everything up to (not including) spending money. Returns
    ``{"cfg", "marker", "plan", "jobs", "approval", "client", "module_dir"}``
    or raises :class:`VastRunRefused` / :class:`PlanRefused` /
    :class:`~trialerror.vastai.guard.HighTierRefused`."""
    embed_cfg = _embed_table(raw)
    if embed_cfg.get("backend") != OFFLOAD_BACKEND_NAME or gpu_executor(embed_cfg) != "vastai":
        raise VastRunRefused(
            "vast.ai is not this program's embed executor: set [ingest.embed] backend = \"offload\" and "
            "gpu = \"vastai\" in trialerror.toml (the one-line switch; docs/VASTAI_EMBED_DESIGN.md 2.3)"
        )
    marker = OffloadMarker("embed", embed_cfg)
    cfg = load_vast_config(raw, program_root)
    module_dir = (embed_cfg.get("query") or {}).get("module_dir") or (raw.get("vastai") or {}).get("module_dir")
    if not module_dir or not (Path(module_dir) / "embed_backend.py").is_file():
        raise VastRunRefused(
            "the instance must run the SAME embed_backend.py as the local query path: set "
            "[ingest.embed.query] module_dir (or [vastai] module_dir) to the embeddings_local directory"
        )

    root = protocol.offload_root(program_root)
    jobs = select_embed_jobs(root)
    limit = min(cfg.max_jobs_per_run, max_jobs) if max_jobs else cfg.max_jobs_per_run
    jobs = jobs[: max(0, limit)]
    if not jobs:
        raise VastRunRefused("no pending embed markers in the offload queue -- nothing to rent a GPU for")

    approval = None
    if cfg.tier == "high":
        approval = guard.verify_high_tier_approval(program_root, cfg.api_key_path)

    client = client or VastClient(cfg.api_key_path)
    tier = cfg.tiers[cfg.tier]
    offers = client.search_offers(
        gpu_names=list(tier.gpus), min_vram_gb=tier.min_vram_gb, max_dph=tier.max_dph, min_reliability=tier.min_reliability
    )
    excluded = {str(o) for o in exclude_offer_ids}
    offers = [o for o in offers if str(o.get("id")) not in excluded]
    ranked = rank_offers(offers, tier, cfg.gpu_factors)
    if ranked:
        # Size the batch: add jobs while the lease still fits under the TTL cap.
        tps = ranked[0][1]
        chosen, total = [], 0
        for job_id, nbytes in jobs:
            _ttl, uncapped = ttl_for((total + nbytes) * TOKENS_PER_BYTE / tps, cfg)
            if chosen and uncapped > cfg.ttl_cap_s:
                break
            chosen.append((job_id, nbytes))
            total += nbytes
        jobs = chosen
    total_bytes = sum(n for _, n in jobs)
    plan = plan_run(
        offers, total_bytes, cfg, max_job_usd=float(approval["max_job_usd"]) if approval else None
    )
    return {
        "cfg": cfg,
        "marker": marker,
        "plan": plan,
        "jobs": jobs,
        "approval": approval,
        "client": client,
        "module_dir": Path(module_dir),
    }


def _default_channel_factory(cfg: VastConfig, run_id: str) -> Callable[[dict[str, Any]], Any]:
    def make(inst: dict[str, Any]) -> SshChannel:
        return SshChannel(
            host=str(inst["ssh_host"]),
            port=int(inst["ssh_port"]),
            identity_path=cfg.ssh_identity_path,
            known_hosts=runs_dir(cfg.program_root).parent / "known_hosts",
        )

    return make


def run_vastai(
    program_root: Path | str,
    raw: dict[str, Any],
    *,
    store: Any,
    client: VastClient | None = None,
    channel_factory: Callable[[dict[str, Any]], Any] | None = None,
    max_jobs: int | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat_interval_s: float = _HEARTBEAT_INTERVAL_S,
    max_offer_attempts: int = _MAX_OFFER_ATTEMPTS,
) -> dict[str, Any]:
    """Rent, embed, destroy. An offer taken between search and create
    (``no_such_ask``) rents nothing, so the next ranked offer -- re-planned
    under the same tier, price ceiling and TTL rules -- is tried, up to
    ``max_offer_attempts`` offers in all."""
    program_root = Path(program_root)
    stderr = stderr or sys.stderr
    log = log or (lambda m: print(m, file=stderr))
    excluded: list[Any] = []
    while True:
        prep = prepare_run(program_root, raw, client=client, max_jobs=max_jobs, exclude_offer_ids=excluded)
        try:
            return _run_prepared(
                program_root, prep, store=store, channel_factory=channel_factory, dry_run=dry_run, log=log,
                stderr=stderr, clock=clock, sleep=sleep, heartbeat_interval_s=heartbeat_interval_s,
            )
        except OfferUnavailable as exc:
            excluded.append(exc.offer_id)
            if len(excluded) >= max_offer_attempts:
                raise
            log(f"! vast.ai offer {exc.offer_id} was taken before create (nothing rented); trying the next offer")


def _run_prepared(
    program_root: Path,
    prep: dict[str, Any],
    *,
    store: Any,
    channel_factory: Callable[[dict[str, Any]], Any] | None,
    dry_run: bool,
    log: Callable[[str], None],
    stderr: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    heartbeat_interval_s: float,
) -> dict[str, Any]:
    cfg: VastConfig = prep["cfg"]
    plan = prep["plan"]
    plan_d = plan.as_dict()
    jobs = [j for j, _ in prep["jobs"]]
    summary: dict[str, Any] = {
        "plan": plan_d,
        "jobs": jobs,
        "published": [],
        "failed": [],
        "lost": [],
        "claimed": [],
        "expired": False,
        "destroyed": None,
        "notes": list(cfg.notes),
    }
    if dry_run:
        summary["message"] = "dry run: nothing created"
        return summary

    run_id = new_id("VAST")
    summary["run_id"] = run_id
    if cfg.tier == "high":
        guard.print_high_tier_banner(plan_d, prep["approval"], stream=stderr)
        append_event(
            store,
            event_type="vastai_high_tier_use",
            payload={"run_id": run_id, "plan": plan_d, "approval_expires": prep["approval"].get("expires")},
        )

    module_bytes, module_sha = module_bytes_and_sha(prep["module_dir"])
    marker = prep["marker"]
    root = protocol.offload_root(program_root)
    transport = LocalTransport(root, worker_id=f"vastai-{run_id}")
    work_root = runs_dir(program_root).parent / "work" / run_id
    factory = channel_factory or _default_channel_factory(cfg, run_id)
    lease = InstanceLease(
        prep["client"],
        program_root=program_root,
        program_fp=guard.program_fingerprint(program_root),
        run_id=run_id,
        offer=plan.offer,
        ttl_s=plan.ttl_s,
        image=cfg.image,
        disk_gb=cfg.disk_gb,
        extra_record={"tier": cfg.tier, "jobs": jobs, "worst_case_usd": plan.worst_case_usd},
        log=log,
        clock=clock,
        sleep=sleep,
    )
    backend: RemoteEmbedBackend | None = None
    started = clock()
    try:
        with lease:
            inst = lease.wait_ready(cfg.poll_interval_s)
            channel = factory(inst)
            lease.on_expire = channel.close
            try:
                channel.bootstrap(
                    files={"embed_backend.py": module_bytes, "serve.py": SERVE_SOURCE.encode("utf-8")},
                    pip_packages=cfg.pip_packages,
                )
                lease.check()
                hello = channel.start(marker.model_key)
                if int(hello.get("dims") or 0) != marker.dims:
                    raise RuntimeError(
                        f"remote embed_backend reports {hello.get('dims')} dims; [ingest.embed] dims = {marker.dims}"
                    )
                backend = RemoteEmbedBackend(
                    channel, lease, model_key=marker.model_key, dims=marker.dims, module_sha256=module_sha
                )
                backends = _VastBackends(backend)
                for job_id in jobs:
                    lease.check()
                    bucket, _detail = _process_one(
                        transport,
                        backends,
                        work_root,
                        job_id,
                        worker_id=transport.worker_id,
                        batch_size=cfg.batch_size,
                        heartbeat_interval_s=heartbeat_interval_s,
                        log=log,
                    )
                    summary[bucket].append(job_id)
            finally:
                channel.close()
    except LeaseExpired as exc:
        summary["expired"] = True
        summary["message"] = str(exc)
        log(
            f"!!! vast.ai run {run_id}: TTL expired ({plan.ttl_s:.0f} s); instance destroyed; unfinished jobs "
            "returned to the queue unrun. If this repeats, raise [vastai] startup_s or run fewer jobs."
        )
    finally:
        summary["destroyed"] = lease.destroyed or lease.instance_id is None
        summary["instance_id"] = lease.instance_id
        summary["elapsed_s"] = round(clock() - started, 1)
        if backend is not None and backend.seconds > 0:
            summary["measured_tokens_s"] = round(backend.bytes * TOKENS_PER_BYTE / backend.seconds, 1)
        append_event(
            store,
            event_type="vastai_run" if lease.destroyed or lease.instance_id is None else "vastai_destroy_failed",
            payload={**{k: v for k, v in summary.items() if k != "notes"}, "tier": cfg.tier, "embed_module_sha256": module_sha},
        )
    summary.setdefault("message", f"published {len(summary['published'])} job(s); instance destroyed")
    return summary
