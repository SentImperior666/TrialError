"""M9's doctor checks. Design Section 5.2 (doctor row): "framework +
license-audit in M0; each module registers its own checks."

Every check here stays read-only-connection-only (``trialerror.stores.
connection.connect(path, read_only=True)``), the same discipline every other
module's ``checks.py`` follows (``trialerror.retrieve.checks``,
``trialerror.ingest.checks``, ...) — a doctor run must never itself mutate a
program's stores.

A third check arrived with the ideation framework: ``gate_without_prereg``,
which refuses a ROUND gate that carries no pre-registration. It lives here
rather than in ``trialerror.artifacts.checks`` because what it is really
checking is the prereg end of the link, and this module already owns the
other prereg invariant (``prereg_escrow_integrity``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_verdict_evidence_anchors",
    "check_prereg_escrow_integrity",
    "check_gate_without_prereg",
]

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


#: The ``artifact.attrs`` key that marks an artifact as a ROUND artifact and
#: so brings it under the pre-registration rule. It is the same key
#: ``lens export`` puts on every bookable row and the screen puts on every
#: batch record, so a round artifact carries it without anyone adding a
#: convention for this check's benefit.
_ROUND_ATTR = "round_id"

#: Where a round artifact's pre-registration can be recorded: on the artifact
#: itself, or on a verdict row written about it. Both are accepted — the
#: screen links ``prereg_id`` onto its novelty verdicts, and a synthesis
#: artifact carries its own.
_PREREG_ATTR = "prereg_id"


@register_check("gate_without_prereg", category="verify")
def check_gate_without_prereg(ctx: DoctorContext) -> CheckResult:
    """A gate on a ROUND artifact must name a live pre-registration.

    Blind pre-registration is what makes a round's results reportable at all:
    the procedure and the parameters are escrowed before any spawn, and
    ``prereg_compliant`` is recomputed afterwards rather than asserted. A
    round artifact that reaches a gate with no prereg id is a round whose
    procedure can still be described to match its results, and the gate is
    the last place to notice.

    Scope is deliberately narrow: only artifacts whose ``attrs`` declare a
    ``round_id``. Every other gated artifact in the program — a methods note,
    a review verdict, an ordinary keystone — is not under this rule, and
    widening the check to "every gate" would make it noise an operator learns
    to skip.

    The prereg is looked for in every place one can be recorded: the
    artifact's own ``attrs.prereg_id``, a ``verdict`` row ABOUT that artifact
    (``subject_kind='artifact'``), and a verdict about one of the ROUND's own
    records (``subject_kind='claim'``, resolved through ``idea.round_id``) —
    which is the shape the novelty screen actually writes: it links
    ``prereg_id`` onto a verdict per idea per reference set, never onto the
    artifact. Reading only the artifact-shaped verdicts made the documented
    linkage unsatisfiable, because nothing in the tree produces one.

    A named prereg that does not resolve to a row, or resolves to a ``voided``
    one, is reported as its own offender kind — "named a prereg that is not
    usable" is a different finding from "named none", and the message says
    which."""
    name = "gate_without_prereg"
    if ctx.program_root is None:
        return _skip(name, "program_root not configured")
    ops_path = paths.ops_db_path(ctx.program_root)
    if not ops_path.exists():
        return _skip(name, "ops.db not found (program not yet initialized)")

    conn = connect(ops_path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT g.gate_id, g.state, a.artifact_id, a.type, a.title, a.attrs "
            "FROM gate g JOIN artifact a ON g.artifact_id = a.artifact_id"
        ).fetchall()
        round_gates: list[dict] = []
        for row in rows:
            try:
                attrs = json.loads(row["attrs"]) if row["attrs"] else {}
            except (TypeError, ValueError):
                attrs = {}
            if not isinstance(attrs, dict) or not attrs.get(_ROUND_ATTR):
                continue
            round_gates.append(
                {
                    "gate_id": row["gate_id"], "state": row["state"], "artifact_id": row["artifact_id"],
                    "type": row["type"], "round_id": attrs.get(_ROUND_ATTR), "prereg_id": attrs.get(_PREREG_ATTR),
                }
            )
        if not round_gates:
            return _skip(name, "no gate belongs to an artifact whose attrs declare a round_id")

        prereg_status = {
            r["prereg_id"]: r["status"] for r in conn.execute("SELECT prereg_id, status FROM prereg").fetchall()
        }
    finally:
        conn.close()

    knowledge_path = paths.knowledge_db_path(ctx.program_root)
    verdict_prereg: dict[str, set[str]] = {}
    round_prereg: dict[str, set[str]] = {}
    if knowledge_path.exists():
        knowledge = connect(knowledge_path, read_only=True)
        try:
            for row in knowledge.execute(
                "SELECT subject_id, prereg_id FROM verdict WHERE prereg_id IS NOT NULL AND subject_kind = 'artifact'"
            ).fetchall():
                verdict_prereg.setdefault(row["subject_id"], set()).add(row["prereg_id"])
            # The shape the screen writes: one verdict per idea per reference
            # set, subject_kind='claim', carrying the round's prereg_id. The
            # round is on the idea row, so that is where the join goes.
            for row in knowledge.execute(
                "SELECT i.round_id AS round_id, v.prereg_id AS prereg_id FROM verdict v "
                "JOIN idea i ON v.subject_id = i.idea_id "
                "WHERE v.prereg_id IS NOT NULL AND v.subject_kind = 'claim' AND i.round_id IS NOT NULL"
            ).fetchall():
                round_prereg.setdefault(str(row["round_id"]), set()).add(row["prereg_id"])
        finally:
            knowledge.close()

    missing: list[dict] = []
    unusable: list[dict] = []
    for gate in round_gates:
        candidates = {gate["prereg_id"]} if gate["prereg_id"] else set()
        candidates |= verdict_prereg.get(gate["artifact_id"], set())
        candidates |= round_prereg.get(str(gate["round_id"]), set())
        candidates = {c for c in candidates if c}
        if not candidates:
            missing.append(gate)
            continue
        if not any(prereg_status.get(c) in ("committed", "revealed") for c in candidates):
            unusable.append({**gate, "named": sorted(candidates), "statuses": sorted({str(prereg_status.get(c)) for c in candidates})})

    if missing or unusable:
        status = "fail"
        message = (
            f"{len(missing)} round gate(s) name no pre-registration"
            + (
                f"; {len(unusable)} name one that does not resolve or is voided"
                if unusable
                else ""
            )
            + " -- a round's procedure is escrowed before any spawn, and a gate is the last place to notice"
        )
    else:
        status = "pass"
        message = f"all {len(round_gates)} round gate(s) name a committed or revealed pre-registration"
    return CheckResult(
        name=name, category="verify", status=status, message=message,
        details={"missing": missing, "unusable": unusable, "round_gates": len(round_gates)},
    )
