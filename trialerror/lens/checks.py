"""M13's doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every other
subsystem's ``checks.py`` (design Section 5.2 doctor row) — dropping this
file is the entire registration step, no shared file touched.

Build brief: "doctor checks in ``trialerror/lens/checks.py`` (e.g.
assignment-coverage invariants: every cluster in >=N sets, far-arm floor
honored, no duplicate sets)." Three checks, all scoped to
``DoctorContext.program_root`` (ops.db is per-program), all reading
``lens_assignment`` joined back to ``lens_roster`` for ``round_id``:

- ``far_arm_floor_honored``: every lens's logged far-arm count must meet
  the ``far_floor`` value its own rows carry — the write-time invariant
  ``trialerror.lens.assign.run_assignment`` enforces via
  ``trialerror.lens.quota.draw_quota``, re-checked here the same way
  ``trialerror.artifacts.checks``'s trio re-check THEIR write-time invariants:
  only reachable via a direct write bypassing the validated assignment
  path.
- ``no_duplicate_slice``: no candidate (``slice_spec.candidate_id``) is
  assigned to more than one lens within the same round — the "no
  duplicate sets" coverage invariant; the seeded draw's shrinking-pool
  construction makes this structurally impossible through
  ``run_assignment`` itself, so a violation here is a direct-write
  adversarial signal.
- ``cluster_coverage``: for rounds whose assignments carry a
  ``cluster_id`` (i.e. the round used ``cluster_of``), every distinct
  cluster referenced by that round's assignment rows appears in at least
  ``min_sets`` (default 1) of them — a WARN (not a FAIL) by default, since
  "under-represented cluster" is a data-shape observation, not a
  structural violation the write API could have refused. Rounds with no
  cluster labels at all are skipped, not flagged.

Any DB file that doesn't exist yet, or a program with no lens_assignment
rows at all, is reported ``skip`` — not a doctor failure (same convention
``trialerror.artifacts.checks`` uses).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_far_arm_floor_honored", "check_no_duplicate_slice", "check_cluster_coverage"]

#: Default floor for the WARN-only cluster-coverage check (build brief:
#: "every cluster in >=N sets"; N is not pinned by the design, so this
#: module's default is 1 — "referenced at least once" — the loosest
#: reading that is still a real check, callers wanting AMENDMENT-3's
#: stricter bar pass a larger ``min_sets`` via a future CLI flag).
DEFAULT_MIN_SETS = 1


def _ops_conn_or_none(ctx: DoctorContext) -> sqlite3.Connection | None:
    if ctx.program_root is None:
        return None
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


def _assignment_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.assign_id, a.roster_id, a.slice_spec, a.arm, a.far_floor, r.round_id
        FROM lens_assignment a
        JOIN lens_roster r ON a.roster_id = r.roster_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


@register_check("far_arm_floor_honored", category="lens")
def check_far_arm_floor_honored(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="far_arm_floor_honored", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = _assignment_rows(conn)
        if not rows:
            return CheckResult(
                name="far_arm_floor_honored", category="lens", status="skip",
                message="no lens_assignment rows on file",
            )
        by_lens: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_lens[row["roster_id"]].append(row)

        offenders = []
        for roster_id, lens_rows in by_lens.items():
            far_count = sum(1 for r in lens_rows if r["arm"] == "far")
            # The far_floor a lens's rows carry is written uniformly by one
            # run_assignment call; take the most recently-seen value as the
            # applicable floor (see module docstring).
            required_floor = lens_rows[-1]["far_floor"]
            if far_count < required_floor:
                offenders.append(
                    {"roster_id": roster_id, "far_count": far_count, "far_floor": required_floor}
                )

        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} lens(es) with fewer far-arm assignments than their far_floor"
            if offenders
            else "every lens's far-arm assignment count meets its far_floor"
        )
        return CheckResult(
            name="far_arm_floor_honored", category="lens", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


@register_check("no_duplicate_slice", category="lens")
def check_no_duplicate_slice(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="no_duplicate_slice", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = _assignment_rows(conn)
        if not rows:
            return CheckResult(
                name="no_duplicate_slice", category="lens", status="skip",
                message="no lens_assignment rows on file",
            )
        seen: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in rows:
            spec = json.loads(row["slice_spec"])
            candidate_id = spec.get("candidate_id")
            if candidate_id is None:
                continue
            seen[(row["round_id"], candidate_id)].append(row["assign_id"])

        offenders = [
            {"round_id": round_id, "candidate_id": candidate_id, "assign_ids": assign_ids}
            for (round_id, candidate_id), assign_ids in seen.items()
            if len(assign_ids) > 1
        ]
        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} candidate(s) assigned to more than one lens within the same round"
            if offenders
            else "no candidate is assigned to more than one lens within any round"
        )
        return CheckResult(
            name="no_duplicate_slice", category="lens", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


@register_check("cluster_coverage", category="lens")
def check_cluster_coverage(ctx: DoctorContext, *, min_sets: int = DEFAULT_MIN_SETS) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="cluster_coverage", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = _assignment_rows(conn)
        if not rows:
            return CheckResult(
                name="cluster_coverage", category="lens", status="skip",
                message="no lens_assignment rows on file",
            )
        by_round_cluster: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        any_cluster_labeled = False
        for row in rows:
            spec = json.loads(row["slice_spec"])
            cluster_id = spec.get("cluster_id")
            if cluster_id is None:
                continue
            any_cluster_labeled = True
            by_round_cluster[row["round_id"]][cluster_id] += 1

        if not any_cluster_labeled:
            return CheckResult(
                name="cluster_coverage", category="lens", status="skip",
                message="no round's assignments carry cluster_id labels (cluster_of not used)",
            )

        under_covered = [
            {"round_id": round_id, "cluster_id": cluster_id, "set_count": count, "min_sets": min_sets}
            for round_id, clusters in by_round_cluster.items()
            for cluster_id, count in clusters.items()
            if count < min_sets
        ]
        status = "warn" if under_covered else "pass"
        message = (
            f"{len(under_covered)} cluster(s) referenced in fewer than {min_sets} assignment set(s)"
            if under_covered
            else f"every referenced cluster appears in >= {min_sets} assignment set(s)"
        )
        return CheckResult(
            name="cluster_coverage", category="lens", status=status, message=message,
            details={"under_covered": under_covered},
        )
    finally:
        conn.close()
