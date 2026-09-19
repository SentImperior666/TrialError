"""Lane L0-C's doctor checks (design section 4's doctor row):
``offload_backlog`` (warn past 24 h), ``offload_stale_claims``,
``offload_failed``, ``worker_heartbeat_stale`` and
``offload_control_orphaned`` (C-0097's two), ``offload_backend_root_resolved``
(D-FB-6) and ``fake_backend_rows``.

Auto-discovered by ``trialerror.util.doctor.discover_and_register_checks``
-- dropping this file is the entire registration step, no shared file
touched (the same convention ``trialerror/jobs/checks.py`` documents).

Severity convention, following ``trialerror/jobs/checks.py``'s precedent:
an offload queue with work in it is the EXPECTED state of a design whose
GPU is a laptop that is off most of the time, so a backlog is never a
``fail`` -- it is a ``warn`` once it is old enough to mean "the operator
has not run the worker in a day", and the HOME banner
(:mod:`trialerror.offload.dashboard_items`) is what actually asks for a
human. ``fake_backend_rows`` is the one check that CAN fail, and only in a
program that declared ``[ingest] require_real_backends = true``: there,
fake rows in the record are a data-integrity violation, not an
operational state.
"""

from __future__ import annotations

from typing import Any

from trialerror.offload import protocol
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_offload_backlog",
    "check_offload_stale_claims",
    "check_offload_failed",
    "check_worker_heartbeat_stale",
    "check_offload_control_orphaned",
    "check_offload_backend_root_resolved",
    "check_fake_backend_rows",
]

_CATEGORY = "offload"


def _root(ctx: DoctorContext):
    if ctx.program_root is None:
        return None
    return protocol.offload_root(ctx.program_root)


def _skip(name: str) -> CheckResult:
    return CheckResult(
        name=name,
        category=_CATEGORY,
        status="skip",
        message="no offload/ queue (program_root not configured, or this program does not "
        "offload any stage to a GPU worker)",
    )


def _hours(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return f"{seconds / 3600.0:.1f}h"


@register_check("offload_backlog", category=_CATEGORY)
def check_offload_backlog(ctx: DoctorContext) -> CheckResult:
    """How much work is waiting for the DEV GPU, and for how long.

    ``warn`` past 24 h (design section 4). Below that the backlog is simply
    the system working as designed -- the laptop is off, the sandbox keeps
    accepting documents -- and saying anything louder would train the
    operator to ignore the check."""
    root = _root(ctx)
    if root is None or not root.is_dir():
        return _skip("offload_backlog")
    pending = protocol.list_pending(root)
    oldest = protocol.oldest_pending_age_s(root)
    stale = oldest is not None and oldest > protocol.BACKLOG_WARN_S
    status = "warn" if stale else "pass"
    if not pending:
        message = "no documents are waiting for the DEV GPU"
    elif stale:
        message = (
            f"{len(pending)} document(s) have waited {_hours(oldest)} for the DEV GPU -- "
            "run the GPU worker on DEV"
        )
    else:
        message = f"{len(pending)} document(s) waiting for the DEV GPU (oldest {_hours(oldest)})"
    return CheckResult(
        name="offload_backlog",
        category=_CATEGORY,
        status=status,
        message=message,
        details={
            "pending": len(pending),
            "job_ids": pending,
            "oldest_pending_age_s": oldest,
            "warn_after_s": protocol.BACKLOG_WARN_S,
        },
    )


@register_check("offload_stale_claims", category=_CATEGORY)
def check_offload_stale_claims(ctx: DoctorContext) -> CheckResult:
    """Claims whose heartbeat has stopped -- a DEV laptop that was closed
    or suspended mid-job. Self-healing: ``trialerror offload reclaim``
    (which the sandbox's jobs window runs every loop) returns them. Warn,
    never fail, for exactly the reason ``stale_lease`` warns: this is the
    expected state between a suspend and the next reclaim."""
    root = _root(ctx)
    if root is None or not root.is_dir():
        return _skip("offload_stale_claims")
    claims = protocol.list_claims(root)
    stale = [
        c for c in claims if c.get("age_s") is not None and c["age_s"] > protocol.DEFAULT_CLAIM_EXPIRY_S
    ]
    status = "warn" if stale else "pass"
    message = (
        f"{len(stale)} offload claim(s) past the {protocol.DEFAULT_CLAIM_EXPIRY_S // 60}-minute "
        "expiry (run `trialerror offload reclaim`)"
        if stale
        else f"{len(claims)} live offload claim(s), none expired"
    )
    return CheckResult(
        name="offload_stale_claims",
        category=_CATEGORY,
        status=status,
        message=message,
        details={"claims": claims, "stale": stale, "expiry_s": protocol.DEFAULT_CLAIM_EXPIRY_S},
    )


@register_check("offload_failed", category=_CATEGORY)
def check_offload_failed(ctx: DoctorContext) -> CheckResult:
    """Markers that exhausted their DEV attempts. Terminal by construction
    -- the ledger row abandons on its own -- but each one is a document
    that will never be ingested until a human looks at it, so it stays
    visible until the directory is dealt with.

    **A retried marker is not one of them** (lane FB-8a). ``trialerror jobs
    retry`` moves the terminal directory to ``failed/_retried/<job>.<stamp>/``
    and puts the work back in the queue, so the human this check asks for has
    already been. Those entries are COUNTED here, separately, and never
    contribute to the severity: a check that kept warning about work an
    operator had already dealt with would be a check that can never be
    cleared, and the next real failure would arrive as one more line in a
    list nobody reads."""
    root = _root(ctx)
    if root is None or not root.is_dir():
        return _skip("offload_failed")
    failed = protocol.list_failed(root)
    retried = protocol.list_retried(root)
    reasons: dict[str, Any] = {}
    for job_id in failed:
        path = protocol.failed_dir(root) / job_id / protocol.ERROR_FILENAME
        if path.is_file():
            try:
                reasons[job_id] = protocol.read_json(path).get("error")
            except Exception:  # noqa: BLE001 - an unreadable error file is itself the detail
                reasons[job_id] = "(error.json unreadable)"
    status = "warn" if failed else "pass"
    retried_note = f"; {len(retried)} retried (evidence kept)" if retried else ""
    message = (
        f"{len(failed)} offload job(s) failed on the DEV GPU past their attempt budget "
        f"(inspect {protocol.failed_dir(root)}){retried_note}"
        if failed
        else f"no offload job has exhausted its DEV attempts{retried_note}"
    )
    return CheckResult(
        name="offload_failed",
        category=_CATEGORY,
        status=status,
        message=message,
        details={
            "failed": failed,
            "reasons": reasons,
            "retried": retried,
            "retried_count": len(retried),
        },
    )


@register_check("worker_heartbeat_stale", category=_CATEGORY)
def check_worker_heartbeat_stale(ctx: DoctorContext) -> CheckResult:
    """Whether a worker that holds a claim is still talking, and whether one
    that is still talking is OBEYING (C-0097 D7).

    Two severities, and the split is the whole point of the check:

    * **warn** -- a worker's progress file is older than the lost window while
      its claim is still held. That is the ordinary closed-laptop state the
      60-minute ``offload reclaim`` already converges, so it warns for exactly
      the reason ``offload_stale_claims`` does.
    * **fail** -- a ``stop`` request is older than the control TTL and the
      worker is still heartbeating ``running``. Nothing here can kill a
      process (D8), so the only thing that makes the control law true is a
      worker that honours it; one that keeps running through an hour-old stop
      is a DEFECT in the worker, not a state of the queue, and a defect is
      what ``fail`` is for.
    * **warn** -- a worker whose payload calls itself something other than its
      own claim directory (FIX V-7). Every reading in this subsystem is keyed on
      the directory, the wrapper never checks the payload's ``worker_id``, and a
      mismatch means an operator reading the card is looking at one name while
      ``worker-control`` is addressing another."""
    from trialerror.offload import control as control_api

    root = _root(ctx)
    if root is None or not root.is_dir():
        return _skip("worker_heartbeat_stale")

    rows = control_api.worker_rows(root)
    claims = protocol.list_claims(root)
    claimed_workers = {c["worker_id"] for c in claims}

    stale_with_claim = [
        {
            "worker_id": r["worker_id"],
            "state": r["state"],
            "job_id": r["job_id"],
            "heartbeat_age_s": r["heartbeat_age_s"],
        }
        for r in rows
        if r["lost"] and r["worker_id"] in claimed_workers
    ]
    mismatched = [
        {
            "worker_id": r["worker_id"],
            "reported_worker_id": r.get("reported_worker_id"),
            "job_id": r["job_id"],
        }
        for r in rows
        if r.get("worker_id_mismatch")
    ]
    ignored_stops = []
    for record in control_api.list_controls(root):
        if record["request"] != "stop" or not record["stale"]:
            continue
        row = next((r for r in rows if r["worker_id"] == record["worker_id"]), None)
        if row is not None and not row["lost"] and row["state"] in ("running", "claiming"):
            ignored_stops.append(
                {
                    "worker_id": record["worker_id"],
                    "requested_ts": record["ts"],
                    "age_s": record["age_s"],
                    "state": row["state"],
                    "job_id": row["job_id"],
                }
            )

    if ignored_stops:
        status = "fail"
        names = ", ".join(sorted(r["worker_id"] for r in ignored_stops))
        message = (
            f"worker(s) {names} are still reporting work with a stop request older than "
            f"{control_api.DEFAULT_CONTROL_TTL_S / 60:.0f} minutes -- a worker that ignores control "
            "is a defect, not an operational state"
        )
    elif stale_with_claim:
        status = "warn"
        names = ", ".join(sorted(r["worker_id"] for r in stale_with_claim))
        message = (
            f"worker(s) {names} hold a claim but have not reported for over "
            f"{control_api.lost_after_s():.0f}s (run `trialerror offload reclaim` once the expiry "
            "passes, or restart the worker)"
        )
    elif mismatched:
        status = "warn"
        names = ", ".join(
            sorted(f"{r['worker_id']} reports {r['reported_worker_id']!r}" for r in mismatched)
        )
        message = (
            f"worker payload id(s) disagree with the claim directory they were written to "
            f"({names}) -- every control act and every count is keyed on the directory, so fix "
            "the worker's --worker-id"
        )
    elif not rows:
        status = "pass"
        message = "no worker has reported to this queue"
    else:
        status = "pass"
        message = f"{len(rows)} worker(s) reporting, none silent with a claim held"

    return CheckResult(
        name="worker_heartbeat_stale",
        category=_CATEGORY,
        status=status,
        message=message,
        details={
            "workers": rows,
            "stale_with_claim": stale_with_claim,
            "mismatched_ids": mismatched,
            "ignored_stops": ignored_stops,
            "lost_after_s": control_api.lost_after_s(),
            "control_ttl_s": control_api.DEFAULT_CONTROL_TTL_S,
        },
    )


@register_check("offload_control_orphaned", category=_CATEGORY)
def check_offload_control_orphaned(ctx: DoctorContext) -> CheckResult:
    """A control request with no worker to read it (C-0097 D7).

    Warn, never fail: leaving a request for a worker you are about to start is
    a legitimate thing to do (``worker-control --even-if-absent`` exists for
    it), and the request expires on its own at the TTL. What the check is
    actually for is the other reading of the same file -- an operator who
    paused a worker, watched nothing happen, and has no way to tell that the
    worker they paused was never running. The row names the worker and the age
    so that question answers itself."""
    from trialerror.offload import control as control_api

    root = _root(ctx)
    if root is None or not root.is_dir():
        return _skip("offload_control_orphaned")

    rows = {r["worker_id"]: r for r in control_api.worker_rows(root)}
    orphaned = []
    for record in control_api.list_controls(root):
        row = rows.get(record["worker_id"])
        if row is not None and not row["lost"]:
            continue
        orphaned.append(
            {
                "worker_id": record["worker_id"],
                "request": record["request"],
                "by_launch": record["by_launch"],
                "ts": record["ts"],
                "age_s": record["age_s"],
                "stale": record["stale"],
                "worker_seen": row is not None,
            }
        )
    status = "warn" if orphaned else "pass"
    message = (
        f"{len(orphaned)} control request(s) have no live worker to read them "
        f"({', '.join(sorted(str(o['worker_id']) for o in orphaned))})"
        if orphaned
        else "every control request in the queue has a live worker to read it"
    )
    return CheckResult(
        name="offload_control_orphaned",
        category=_CATEGORY,
        status=status,
        message=message,
        details={"orphaned": orphaned, "lost_after_s": control_api.lost_after_s()},
    )


@register_check("offload_backend_root_resolved", category=_CATEGORY)
def check_offload_backend_root_resolved(ctx: DoctorContext) -> CheckResult:
    """D-FB-6: does the backend config root this program root carries
    actually resolve to the backends it names?

    The question a worker answers in its first second and nothing else asked
    until now: which root was read, what each stage's ``[ingest.<stage>]
    backend`` says, and -- for a stage this machine is the one to run --
    whether the backend object can be CONSTRUCTED and whether the
    executable or module directory it carries exists here.

    Read through :meth:`trialerror.offload.worker.ConfigDevBackends.describe`,
    which makes the same ``ocr()``/``embed()`` calls
    :meth:`~trialerror.offload.worker.ConfigDevBackends.validate` makes and
    reports the same refusal text, so this check and
    ``trialerror offload worker`` cannot come to different conclusions about
    one root. That is the whole reason the reading lives in the worker's own
    class rather than in a second copy here.

    Severity:

    * ``skip`` -- no program root, or no ``trialerror.toml`` in it: nothing
      names a backend, which is not a finding.
    * ``fail`` -- the ``trialerror.toml`` that IS there cannot be loaded
      (unparseable, or missing ``[program].id``): the worker's own
      ``load_config`` call raises and it returns ``bad_config``, so reading
      the absent config as "nothing declared" here would report ``fake``
      stages for a file that says ``marker`` (V-1).
    * ``fail`` -- a stage this machine runs cannot be constructed (a missing
      ``marker_single_exe`` / ``python_exe`` / ``module_dir``, an unknown
      OCR backend name -- on the embed side an unrecognised name IS the
      model key, so that stage fails on the missing ``python_exe`` /
      ``module_dir`` rather than on the name, V-5): a worker launched
      against this root refuses to start.
    * ``fail`` -- a stage reads ``fake`` while this program declares that
      stage must be real: the config contradicts itself, and
      ``fake_backend_rows`` can only see it after the rows are written.
    * ``warn`` -- constructed, but a path it names does not exist on THIS
      machine. Warn rather than fail because the config of the machine that
      runs the model is legitimately readable from a machine that does not
      (the sandbox can see a DEV root over a mount), and because the
      construction -- the part a worker acts on -- succeeded.
    * ``pass`` -- every stage either resolves here or is honestly declared
      ``offload``/``fake`` (the SANDBOX's own normal state: the stage runs on
      the GPU worker's machine, and this check says which stub said so).

    C-0078: resolved paths appear in this result at RUNTIME only. Nothing
    here writes one into code, a test or a doc.
    """
    name = "offload_backend_root_resolved"
    if ctx.program_root is None:
        return CheckResult(
            name=name,
            category=_CATEGORY,
            status="skip",
            message="no backend config root (program_root not configured)",
        )
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = ctx.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return CheckResult(
            name=name,
            category=_CATEGORY,
            status="skip",
            message=f"no {CONFIG_FILENAME} at the backend config root -- no stage names a backend here",
            details={"backend_config_root": str(ctx.program_root)},
        )

    from trialerror.ingest.backends import stage_requires_real
    from trialerror.offload.worker import STAGES, ConfigDevBackends

    # V-1: the worker's OWN call, not the swallow-and-return-{} helper the
    # other checks use. ``_raw_config`` reads an unloadable toml as "nothing
    # declared", which here would default both stages to ``fake``, skip every
    # severity branch and print an affirmatively false sentence about a file
    # that names ``marker`` -- while ``offload worker`` on the same root
    # returns ``bad_config``. A check sold on "the doctor and the worker
    # cannot disagree about one root" has to fail where the worker refuses.
    try:
        raw_config = load_config(cfg_path).raw
    except Exception as exc:  # noqa: BLE001 - reported as a finding, never a traceback
        return CheckResult(
            name=name,
            category=_CATEGORY,
            status="fail",
            message=(
                f"backend config root read -- {CONFIG_FILENAME} cannot be loaded, so nothing here "
                "names a backend and a worker launched against this root refuses with "
                f"'bad_config': {exc}"
            ),
            details={
                "backend_config_root": str(ctx.program_root),
                "config_path": str(cfg_path),
                "config_error": f"{type(exc).__name__}: {exc}",
            },
        )
    described = ConfigDevBackends(raw_config).describe()
    stages: dict[str, Any] = described["stages"]

    failures: list[str] = []
    warnings: list[str] = []
    parts: list[str] = []
    for stage in STAGES:
        entry = stages[stage]
        backend = entry["backend"]
        requires_real = stage_requires_real(raw_config, stage)
        entry["requires_real"] = requires_real
        if not entry["runs_here"]:
            parts.append(f"{stage}: {backend!r} (not run on this machine)")
            if backend == "fake" and requires_real:
                failures.append(
                    f"[ingest.{stage}] backend = 'fake' while this program requires the {stage} "
                    "stage to be real"
                )
            continue
        if not entry["constructed"]:
            parts.append(f"{stage}: {backend!r} -- UNRESOLVED ({entry['error']})")
            failures.append(f"[ingest.{stage}] backend = {backend!r} does not resolve: {entry['error']}")
            continue
        missing = sorted(k for k, v in entry["paths"].items() if not v["exists"])
        if missing:
            parts.append(f"{stage}: {backend!r} constructed, {', '.join(missing)} not found here")
            warnings.append(
                f"[ingest.{stage}] backend = {backend!r} names {', '.join(missing)} that does not "
                "exist on this machine"
            )
        else:
            parts.append(f"{stage}: {backend!r} resolved")

    details = {
        "backend_config_root": str(ctx.program_root),
        "config_path": str(cfg_path),
        "stages": stages,
    }
    if failures:
        status, message = "fail", "; ".join(failures)
    elif warnings:
        status, message = "warn", "; ".join(warnings)
    else:
        status, message = "pass", f"backend config root read -- {'; '.join(parts)}"
    if status != "pass":
        message = f"backend config root read -- {'; '.join(parts)}: {message}"
    return CheckResult(name=name, category=_CATEGORY, status=status, message=message, details=details)


@register_check("fake_backend_rows", category="ingest")
def check_fake_backend_rows(ctx: DoctorContext) -> CheckResult:
    """Design D13's last line of defence: rows in the RECORD that were
    produced by a fake backend (``emb.model_key LIKE 'fake-%'`` or
    ``document.ocr_backend = 'fake'``).

    A config typo must never silently route the record through fake OCR or
    fake embeddings. The loaders refuse it, the DEV worker refuses it, the
    published-result verification refuses it -- and this check is what
    notices if one ever got through anyway, or if a program that used to
    be permissive was later declared real while old fake rows were still
    sitting in it. ``fail`` in that program; ``warn`` elsewhere, because a
    scratch or test program running the fake backends on purpose is not
    broken.

    Lane FB-1 item F5: "that program" is now resolved PER STAGE. Fake
    embeddings fail only where the embed stage is required to be real, fake
    OCR only where the OCR stage is -- each defaulting to the global
    ``[ingest] require_real_backends``, so a pre-F5 configuration reads
    exactly as before. A program that requires a real embedder while
    exempting an OCR stage it has no stack for gets a failure for the first
    and a warning for the second, in one message that names each stage
    beside the key that decided it: a finding an operator cannot act on
    because it names the wrong knob is a finding they learn to ignore."""
    if ctx.program_root is None:
        return CheckResult(
            name="fake_backend_rows",
            category="ingest",
            status="skip",
            message="knowledge.db not found (program_root not configured)",
        )
    db = paths.knowledge_db_path(ctx.program_root)
    if not db.exists():
        return CheckResult(
            name="fake_backend_rows",
            category="ingest",
            status="skip",
            message="knowledge.db not found (program not yet initialized)",
        )
    conn = connect(db, read_only=True)
    try:
        emb_rows = conn.execute(
            "SELECT model_key, COUNT(*) AS n FROM emb WHERE model_key LIKE 'fake-%' GROUP BY model_key"
        ).fetchall()
        # A RETRACTED document is precisely the case this check must stop
        # shouting about: its fake-backend rows were the reason it was
        # withdrawn, and they are gone. ``retract`` also clears
        # ``ocr_backend``, so this exclusion is belt-and-braces -- but it
        # keeps the "must not count a retracted document" contract in one
        # readable place instead of resting on a side effect of another
        # module's UPDATE.
        from trialerror.ingest.retract import retracted_doc_ids

        retracted = retracted_doc_ids(conn)
        fake_doc_ids = [
            r["doc_id"]
            for r in conn.execute("SELECT doc_id FROM document WHERE ocr_backend = 'fake'").fetchall()
        ]
    finally:
        conn.close()

    from trialerror.ingest.backends import require_real_key_for, stage_requires_real

    raw_config = _raw_config(ctx)
    fake_emb = {r["model_key"]: r["n"] for r in emb_rows}
    fake_emb_total = sum(fake_emb.values())
    fake_docs = len([d for d in fake_doc_ids if d not in retracted])
    embed_required = stage_requires_real(raw_config, "embed")
    ocr_required = stage_requires_real(raw_config, "ocr")
    requirement = {
        "embed": {"require_real": embed_required, "key": require_real_key_for(raw_config, "embed")},
        "ocr": {"require_real": ocr_required, "key": require_real_key_for(raw_config, "ocr")},
    }
    from trialerror.ingest.checks import configured_embed_model_key

    configured_key = configured_embed_model_key(raw_config)
    superseded = sorted(k for k in fake_emb if k != configured_key)

    if fake_emb_total + fake_docs == 0:
        return CheckResult(
            name="fake_backend_rows",
            category="ingest",
            status="pass",
            message="no fake-backend rows in the record",
            details={"require_real": requirement, "configured_embed_model_key": configured_key},
        )

    # A stage's rows fail only where that stage's own flag is on. The other
    # stage's rows are still reported -- they are simply not a violation of
    # anything this program declared.
    failing = (fake_emb_total and embed_required) or (fake_docs and ocr_required)
    status = "fail" if failing else "warn"
    # Backlog item (d): "these rows must be re-ingested or deleted" told an
    # operator nothing they could act on -- not which key their program is
    # actually embedding under now, not that the fake keys are superseded
    # rather than in use, and not that there is a verb whose whole job is
    # taking a superseded key's rows out. All three, named.
    parts: list[str] = []
    if fake_emb_total:
        if embed_required:
            if superseded:
                keys = ", ".join(repr(k) for k in superseded)
                is_are = "is" if len(superseded) == 1 else "are"
                purge_key = superseded[0] if len(superseded) == 1 else "<one of them>"
                detail = (
                    f" -- the embed stage is required to be real ({requirement['embed']['key']} = "
                    f"true). This program embeds under {configured_key!r}, so {keys} {is_are} "
                    f"SUPERSEDED, not in use: `trialerror ingest purge-embeddings --model-key "
                    f"{purge_key} --launch-id <a booked launch>` removes a superseded key's rows and "
                    "its vector-index entries, once the real key is known to be complete (its own "
                    "report's chunks_now_without_any_embedding must be 0 before you trust the corpus)"
                )
            else:
                detail = (
                    f" -- the embed stage is required to be real ({requirement['embed']['key']} = "
                    f"true), but the key this program embeds under is itself fake "
                    f"({configured_key!r}): configure a real backend and re-embed before any of "
                    "these rows can be superseded"
                )
        else:
            detail = (
                f" (the embed stage is not required to be real here; this program embeds under "
                f"{configured_key!r})"
            )
        parts.append(f"{fake_emb_total} emb row(s) with a fake model_key" + detail)
    if fake_docs:
        parts.append(
            f"{fake_docs} document(s) with ocr_backend = 'fake'"
            + (
                f" -- the OCR stage is required to be real ({requirement['ocr']['key']} = true), "
                "so these must be re-ingested or deleted"
                if ocr_required
                else " (the OCR stage is not required to be real here)"
            )
        )
    return CheckResult(
        name="fake_backend_rows",
        category="ingest",
        status=status,
        message="; ".join(parts),
        details={
            "fake_emb_model_keys": fake_emb,
            "fake_ocr_documents": fake_docs,
            "require_real": requirement,
            "configured_embed_model_key": configured_key,
            "superseded_fake_model_keys": superseded,
        },
    )


def _raw_config(ctx: DoctorContext) -> dict:
    """This program's ``trialerror.toml`` as a raw dict, or ``{}``.

    An unparseable or absent toml reads as "nothing declared" -- the same
    posture the former ``_require_real_backends`` helper took, kept because
    a doctor check that raised over another check's problem would hide both."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    if ctx.program_root is None:
        return {}
    cfg_path = ctx.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:  # noqa: BLE001 - an unparseable toml is its own check's problem
        return {}
