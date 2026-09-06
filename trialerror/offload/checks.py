"""Lane L0-C's doctor checks (design section 4's doctor row):
``offload_backlog`` (warn past 24 h), ``offload_stale_claims``,
``offload_failed``, ``fake_backend_rows``.

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
    visible until the directory is dealt with."""
    root = _root(ctx)
    if root is None or not root.is_dir():
        return _skip("offload_failed")
    failed = protocol.list_failed(root)
    reasons: dict[str, Any] = {}
    for job_id in failed:
        path = protocol.failed_dir(root) / job_id / protocol.ERROR_FILENAME
        if path.is_file():
            try:
                reasons[job_id] = protocol.read_json(path).get("error")
            except Exception:  # noqa: BLE001 - an unreadable error file is itself the detail
                reasons[job_id] = "(error.json unreadable)"
    status = "warn" if failed else "pass"
    message = (
        f"{len(failed)} offload job(s) failed on the DEV GPU past their attempt budget "
        f"(inspect {protocol.failed_dir(root)})"
        if failed
        else "no offload job has exhausted its DEV attempts"
    )
    return CheckResult(
        name="offload_failed",
        category=_CATEGORY,
        status=status,
        message=message,
        details={"failed": failed, "reasons": reasons},
    )


@register_check("fake_backend_rows", category="ingest")
def check_fake_backend_rows(ctx: DoctorContext) -> CheckResult:
    """Design D13's last line of defence: rows in the RECORD that were
    produced by a fake backend (``emb.model_key LIKE 'fake-%'`` or
    ``document.ocr_backend = 'fake'``).

    A config typo must never silently route the record through fake OCR or
    fake embeddings. The loaders refuse it, the DEV worker refuses it, the
    published-result verification refuses it -- and this check is what
    notices if one ever got through anyway, or if a program that used to
    be permissive was later declared real (``[ingest]
    require_real_backends = true``) while old fake rows were still sitting
    in it. ``fail`` in that program; ``warn`` elsewhere, because a scratch
    or test program running the fake backends on purpose is not broken."""
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
        doc_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM document WHERE ocr_backend = 'fake'"
        ).fetchone()
    finally:
        conn.close()

    fake_emb = {r["model_key"]: r["n"] for r in emb_rows}
    fake_docs = int(doc_rows["n"] if doc_rows is not None else 0)
    total = sum(fake_emb.values()) + fake_docs
    required = _require_real_backends(ctx)

    if total == 0:
        return CheckResult(
            name="fake_backend_rows",
            category="ingest",
            status="pass",
            message="no fake-backend rows in the record",
            details={"require_real_backends": required},
        )
    status = "fail" if required else "warn"
    return CheckResult(
        name="fake_backend_rows",
        category="ingest",
        status=status,
        message=(
            f"{sum(fake_emb.values())} emb row(s) with a fake model_key and {fake_docs} document(s) "
            f"with ocr_backend = 'fake'"
            + (
                " -- this program declares [ingest] require_real_backends = true, so these rows "
                "must be re-ingested or deleted"
                if required
                else " (permissive program: fake backends are configured on purpose)"
            )
        ),
        details={
            "fake_emb_model_keys": fake_emb,
            "fake_ocr_documents": fake_docs,
            "require_real_backends": required,
        },
    )


def _require_real_backends(ctx: DoctorContext) -> bool:
    from trialerror.util.config import CONFIG_FILENAME, load_config

    if ctx.program_root is None:
        return False
    cfg_path = ctx.program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return False
    try:
        raw = load_config(cfg_path).raw
    except Exception:  # noqa: BLE001 - an unparseable toml is its own check's problem
        return False
    return bool((raw.get("ingest") or {}).get("require_real_backends", False))
