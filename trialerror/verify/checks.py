"""M9's doctor checks. Design Section 5.2 (doctor row): "framework +
license-audit in M0; each module registers its own checks."

Both checks stay read-only-connection-only (``trialerror.stores.connection.
connect(path, read_only=True)``), the same discipline every other module's
``checks.py`` follows (``trialerror.retrieve.checks``, ``trialerror.ingest.checks``,
...) — a doctor run must never itself mutate a program's stores.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_verdict_evidence_anchors", "check_prereg_escrow_integrity"]

#: How many verdict/prereg rows each check samples per run -- bounded so
#: doctor stays fast against a large program, same "regression sentinel,
#: not exhaustive audit" posture ``trialerror.retrieve.checks`` documents for
#: its own ``_FENCE_SAMPLE_LIMIT``.
_SAMPLE_LIMIT = 500


def _skip(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, category="verify", status="skip", message=message)


@register_check("verdict_evidence_anchors", category="verify")
def check_verdict_evidence_anchors(ctx: DoctorContext) -> CheckResult:
    """Every ``anchor_id`` cited in a ``verdict.evidence`` JSON array should
    resolve to a live ``quote_anchor`` row -- the verification-layer half of
    the ``anchors_dangling`` concern M7's own doctor check owns for
    ingestion (design Section 6: "doctor's staleness report (the chunk-fix
    wart)"; this check is the same idea applied to recorded verdicts: a
    verdict whose cited evidence no longer resolves is a silent
    correctness hole a re-normalization could introduce without anyone
    noticing)."""
    if ctx.program_root is None:
        return _skip("verdict_evidence_anchors", "program_root not configured")
    path = paths.knowledge_db_path(ctx.program_root)
    if not path.exists():
        return _skip("verdict_evidence_anchors", "knowledge.db not found (program not yet initialized)")

    conn = connect(path, read_only=True)
    try:
        rows = conn.execute("SELECT verdict_id, evidence FROM verdict ORDER BY ts DESC LIMIT ?", (_SAMPLE_LIMIT,)).fetchall()
        if not rows:
            return CheckResult(name="verdict_evidence_anchors", category="verify", status="skip", message="no verdict rows yet", details={"sampled": 0})

        offenders: list[dict[str, str]] = []
        anchors_checked = 0
        for row in rows:
            try:
                evidence = json.loads(row["evidence"]) if row["evidence"] else []
            except json.JSONDecodeError:
                offenders.append({"verdict_id": row["verdict_id"], "anchor_id": "<unparseable evidence JSON>"})
                continue
            for item in evidence:
                anchor_id = item.get("anchor_id") if isinstance(item, dict) else None
                if not anchor_id:
                    continue
                anchors_checked += 1
                found = conn.execute("SELECT 1 FROM quote_anchor WHERE anchor_id = ?", (anchor_id,)).fetchone()
                if found is None:
                    offenders.append({"verdict_id": row["verdict_id"], "anchor_id": anchor_id})
    finally:
        conn.close()

    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} verdict evidence anchor reference(s) do not resolve to a live quote_anchor row"
        if offenders
        else f"all {anchors_checked} cited evidence anchor(s) across {len(rows)} sampled verdict(s) resolve"
    )
    return CheckResult(
        name="verdict_evidence_anchors", category="verify", status=status, message=message,
        details={"verdicts_sampled": len(rows), "anchors_checked": anchors_checked, "offenders": offenders},
    )


@register_check("prereg_escrow_integrity", category="verify")
def check_prereg_escrow_integrity(ctx: DoctorContext) -> CheckResult:
    """Non-destructive tamper check over every non-``voided`` ``prereg``
    row: the escrow file at ``escrow_path`` must exist and still hash to
    the committed ``procedure_sha256``/``params_sha256`` -- the read-only
    counterpart to what ``trialerror.verify.prereg.reveal_prereg`` checks (and
    voids on failure) destructively; this check only REPORTS, never mutates
    a row, so it is safe to run at any time, repeatedly, without side
    effects on a prereg's own lifecycle."""
    if ctx.program_root is None:
        return _skip("prereg_escrow_integrity", "program_root not configured")
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return _skip("prereg_escrow_integrity", "ops.db not found (program not yet initialized)")

    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT prereg_id, procedure_sha256, params_sha256, escrow_path, status FROM prereg "
            "WHERE status != 'voided' ORDER BY committed_ts DESC LIMIT ?",
            (_SAMPLE_LIMIT,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return CheckResult(name="prereg_escrow_integrity", category="verify", status="skip", message="no non-voided prereg rows yet", details={"sampled": 0})

    offenders: list[dict[str, str]] = []
    for row in rows:
        escrow_path = Path(row["escrow_path"])
        if not escrow_path.is_file():
            offenders.append({"prereg_id": row["prereg_id"], "reason": f"escrow file missing: {escrow_path}"})
            continue
        try:
            content = json.loads(escrow_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            offenders.append({"prereg_id": row["prereg_id"], "reason": f"escrow file unreadable: {exc}"})
            continue
        procedure_sha = hashlib.sha256(content.get("procedure", "").encode("utf-8")).hexdigest()
        params_sha = hashlib.sha256(json.dumps(content.get("params", {}), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if procedure_sha != row["procedure_sha256"] or params_sha != row["params_sha256"]:
            offenders.append({"prereg_id": row["prereg_id"], "reason": "escrowed content no longer matches its committed hash"})

    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} prereg escrow file(s) missing or tampered"
        if offenders
        else f"all {len(rows)} sampled non-voided prereg escrow(s) are intact"
    )
    return CheckResult(
        name="prereg_escrow_integrity", category="verify", status=status, message=message,
        details={"sampled": len(rows), "offenders": offenders},
    )
