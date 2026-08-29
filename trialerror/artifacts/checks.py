"""M10's doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every other
subsystem's ``checks.py`` (design Section 5.2 doctor row: "framework +
license-audit in M0; each module registers its own checks") — dropping
this file is the entire registration step, no shared file touched.

Three checks, all scoped to ``DoctorContext.program_root`` (ops.db is
per-program):

- ``gated_type_without_gate``: a ``registered`` artifact of a ``gated=1``
  type whose gate is missing or not itself ``registered`` — the write-API
  invariant ``trialerror.artifacts.registry.register_artifact`` enforces at
  write time, re-checked here the same way ``xid_dangling``/
  ``law_digest_lockstep`` re-check THEIR write-time invariants: a direct
  SQL write bypassing the validated API is the only way this can happen,
  and it should be loud, not silent.
- ``orphan_gate_transition``: ``gate_transition`` rows whose ``gate_id``
  has no matching ``gate`` row — same-file FK + ``PRAGMA foreign_keys=ON``
  (``trialerror.stores.connection``) makes this impossible through the normal
  write API; this is the adversarial/direct-write detector, mirroring
  ``trialerror.stores.checks.check_xid_dangling``'s role for cross-file
  references.
- ``gate_illegal_transition_history``: replays every gate's own
  ``gate_transition`` history against
  ``trialerror.artifacts.state_machine.LEGAL_TRANSITIONS`` — catches a gate
  whose CURRENT ``gate.state`` doesn't match where its own transition log
  says it should be, or a logged transition that was never legal in the
  first place (again: only reachable via a direct write bypassing
  ``trialerror.artifacts.gates``, since that module is "the ONLY mutation
  path").

Any DB file that doesn't exist yet, or a program with no artifacts/gates
at all, is reported ``skip`` — not a doctor failure.
"""

from __future__ import annotations

import sqlite3

from trialerror.artifacts.state_machine import is_legal_transition
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_gated_type_without_gate",
    "check_orphan_gate_transition",
    "check_gate_illegal_transition_history",
]


def _ops_conn_or_none(ctx: DoctorContext) -> sqlite3.Connection | None:
    if ctx.program_root is None:
        return None
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


@register_check("gated_type_without_gate", category="artifacts")
def check_gated_type_without_gate(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="gated_type_without_gate",
            category="artifacts",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = conn.execute(
            """
            SELECT a.artifact_id, a.type, a.gate_id, g.state AS gate_state
            FROM artifact a
            JOIN template t ON a.type = t.type_key
            LEFT JOIN gate g ON a.gate_id = g.gate_id
            WHERE a.status = 'registered' AND t.gated = 1
              AND (a.gate_id IS NULL OR g.state IS NULL OR g.state != 'registered')
            """
        ).fetchall()
        offenders = [
            {
                "artifact_id": r["artifact_id"],
                "type": r["type"],
                "gate_id": r["gate_id"],
                "gate_state": r["gate_state"],
            }
            for r in rows
        ]
        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} registered artifact(s) of a gated type without a 'registered' gate"
            if offenders
            else "every registered gated-type artifact has a 'registered' gate"
        )
        return CheckResult(
            name="gated_type_without_gate", category="artifacts", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


@register_check("orphan_gate_transition", category="artifacts")
def check_orphan_gate_transition(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="orphan_gate_transition",
            category="artifacts",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = conn.execute(
            """
            SELECT gt.id, gt.gate_id, gt.from_state, gt.to_state
            FROM gate_transition gt
            LEFT JOIN gate g ON gt.gate_id = g.gate_id
            WHERE g.gate_id IS NULL
            """
        ).fetchall()
        offenders = [dict(r) for r in rows]
        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} gate_transition row(s) reference a gate_id with no matching gate row"
            if offenders
            else "no orphan gate_transition rows"
        )
        return CheckResult(
            name="orphan_gate_transition", category="artifacts", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


@register_check("gate_illegal_transition_history", category="artifacts")
def check_gate_illegal_transition_history(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="gate_illegal_transition_history",
            category="artifacts",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        illegal_edges: list[dict] = []
        transitions = conn.execute(
            "SELECT id, gate_id, from_state, to_state FROM gate_transition ORDER BY gate_id, id"
        ).fetchall()
        for row in transitions:
            if not is_legal_transition(row["from_state"], row["to_state"]):
                illegal_edges.append(
                    {"id": row["id"], "gate_id": row["gate_id"], "from_state": row["from_state"], "to_state": row["to_state"]}
                )

        # Current-state consistency: a gate's live ``state`` must equal the
        # ``to_state`` of its own most-recent transition (a gate with zero
        # transitions is expected to still be at 'draft', its opening state).
        state_mismatches: list[dict] = []
        gates = conn.execute("SELECT gate_id, state FROM gate").fetchall()
        for g in gates:
            last = conn.execute(
                "SELECT to_state FROM gate_transition WHERE gate_id = ? ORDER BY id DESC LIMIT 1",
                (g["gate_id"],),
            ).fetchone()
            expected = last["to_state"] if last is not None else "draft"
            if g["state"] != expected:
                state_mismatches.append({"gate_id": g["gate_id"], "state": g["state"], "expected_from_history": expected})

        total = len(illegal_edges) + len(state_mismatches)
        status = "fail" if total else "pass"
        message = (
            f"{len(illegal_edges)} illegal transition(s) logged, {len(state_mismatches)} gate(s) whose "
            "state disagrees with its own transition history"
            if total
            else "every logged gate transition is legal and every gate.state matches its history"
        )
        return CheckResult(
            name="gate_illegal_transition_history", category="artifacts", status=status, message=message,
            details={"illegal_edges": illegal_edges, "state_mismatches": state_mismatches},
        )
    finally:
        conn.close()
