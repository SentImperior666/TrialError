"""M11's doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every other
subsystem's ``checks.py`` (mirrors ``trialerror/law/checks.py``'s
``_ops_conn_or_none`` convention: a raw read-only connection, tolerant of
a not-yet-initialized program — "any DB file that doesn't exist yet ...
is reported ``skip``, not a doctor failure").

- ``memory_unresolved_conflict_groups`` — the "unresolved-conflict-group
  count" named in the M11 build brief's integration-contracts note
  (``trialerror/memory/checks.py``). Reports ``warn`` (never ``fail``): an open
  conflict is a legitimate WAITING state (design Section 9.7: "never
  auto-resolved"), not corruption — it becomes a doctor-visible signal an
  operator can act on, without blocking anything else that depends on a
  clean doctor run.
- ``memory_l0_index_budget`` — advisory: is the L0 tier ALONE already
  bigger than the configured boot-bundle token budget (``[memory]
  token_budget`` in ``trialerror.toml``, falling back to
  ``trialerror.memory.api.DEFAULT_TOKEN_BUDGET``)? ``trialerror.memory.api.
  boot_bundle`` already GUARANTEES its own output never exceeds budget
  (by construction — whole-item truncation), so this check is not
  re-verifying that guarantee; it surfaces the operator-relevant fact that
  truncation is silently happening on every boot because the L0 tier
  itself has outgrown its budget.
- ``memory_stale_items`` — MINING ADOPTION engram-F5 (``docs/reviews/
  MINING_2026-09_OPERATOR_LINKS.md`` section 3: "adopt-now:memory as a
  DOCTOR CHECK (needs_review surfacing by type-keyed age); never mutates a
  pin or a ruling"). Every ACTIVE item past its per-kind half-life
  (:mod:`trialerror.memory.staleness`) — nobody has edited or reviewed it
  in longer than its kind's clock allows. ``warn``, never ``fail``, and
  read-only in the strongest sense available: it holds a
  ``read_only=True`` connection, so a future edit that tried to make decay
  WRITE something would fail at the driver rather than quietly expire a
  law on a timer (review section 5.7).
- ``memory_pending_conflict_candidates`` — MINING ADOPTION engram-F4. How
  many save-time conflict candidates sit unjudged. ``warn`` for the same
  reason the unresolved-conflict-group check is: an unjudged candidate is
  a legitimate waiting state, not corruption.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trialerror.memory.api import DEFAULT_TOKEN_BUDGET, estimate_tokens
from trialerror.memory.merge import group_id_from_item_id
from trialerror.memory.staleness import REVIEW_THRESHOLD, stale_items, summarize
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.config import CONFIG_FILENAME, load_config
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_memory_unresolved_conflict_groups",
    "check_memory_l0_index_budget",
    "check_memory_stale_items",
    "check_memory_pending_conflict_candidates",
]

#: How many stale items the check names inline. The count is the signal;
#: an unbounded list in a doctor envelope is noise.
_STALE_SAMPLE = 10


def _ops_conn_or_none(ctx: DoctorContext) -> sqlite3.Connection | None:
    if ctx.program_root is None:
        return None
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


def _configured_token_budget(ctx: DoctorContext) -> int:
    if ctx.program_root is None:
        return DEFAULT_TOKEN_BUDGET
    config_path = Path(ctx.program_root) / CONFIG_FILENAME
    if not config_path.is_file():
        return DEFAULT_TOKEN_BUDGET
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001 - a malformed trialerror.toml is not this check's concern
        return DEFAULT_TOKEN_BUDGET
    memory_cfg = config.raw.get("memory", {})
    if not isinstance(memory_cfg, dict):
        return DEFAULT_TOKEN_BUDGET
    budget = memory_cfg.get("token_budget", DEFAULT_TOKEN_BUDGET)
    try:
        return int(budget)
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_BUDGET


@register_check("memory_unresolved_conflict_groups", category="memory")
def check_memory_unresolved_conflict_groups(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="memory_unresolved_conflict_groups",
            category="memory",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = conn.execute(
            "SELECT memory_item_id, key FROM memory_item WHERE status = 'needs_merge'"
        ).fetchall()
        groups: dict[str, str] = {}
        for r in rows:
            groups[group_id_from_item_id(r["memory_item_id"])] = r["key"]

        status = "warn" if groups else "pass"
        message = (
            f"{len(groups)} unresolved memory conflict group(s) awaiting `trialerror memory merge --group ... --keep ...`"
            if groups
            else "no unresolved memory conflict groups"
        )
        return CheckResult(
            name="memory_unresolved_conflict_groups",
            category="memory",
            status=status,
            message=message,
            details={"count": len(groups), "groups": groups},
        )
    finally:
        conn.close()


@register_check("memory_l0_index_budget", category="memory")
def check_memory_l0_index_budget(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="memory_l0_index_budget",
            category="memory",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        budget = _configured_token_budget(ctx)
        rows = conn.execute(
            "SELECT key, l0_abstract FROM memory_item WHERE status = 'active' AND tier = 'L0'"
        ).fetchall()
        total = sum(estimate_tokens(r["l0_abstract"] or r["key"]) for r in rows)
        status = "warn" if total > budget else "pass"
        message = (
            f"L0 tier alone is ~{total} estimated tokens, over the {budget}-token boot budget "
            "(boot_bundle() still truncates safely, but every boot silently drops L0 items)"
            if status == "warn"
            else f"L0 tier ~{total} estimated tokens, within the {budget}-token boot budget"
        )
        return CheckResult(
            name="memory_l0_index_budget",
            category="memory",
            status=status,
            message=message,
            details={"l0_item_count": len(rows), "estimated_tokens": total, "token_budget": budget},
        )
    finally:
        conn.close()


@register_check("memory_stale_items", category="memory")
def check_memory_stale_items(ctx: DoctorContext) -> CheckResult:
    """MINING ADOPTION engram-F5. Surfaces every ACTIVE memory item whose
    kind-specific half-life has elapsed since it was last edited or
    explicitly reviewed.

    ``warn``, never ``fail``: an item nobody has looked at in a year is a
    prompt, not a broken store, and a hard failure here would block every
    other gate that wants a clean doctor run for a reason that is
    inherently a judgement call. Nothing about the item changes as a
    result of appearing here -- resetting the clock takes an explicit
    ``trialerror memory reviewed <id>``.

    Tolerant of a store predating the ops v7 migration, exactly as its
    engram-F4 sibling below is: ``memory_item.reviewed_ts`` simply is not
    there yet, reported ``skip`` the same way a missing DB file is. Letting
    that ``OperationalError`` escape would have
    ``trialerror.util.doctor.run_checks`` convert it into a ``fail`` --
    precisely the hard failure the paragraph above forbids.
    """
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="memory_stale_items",
            category="memory",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        try:
            stale = stale_items(conn)
        except sqlite3.OperationalError:
            return CheckResult(
                name="memory_stale_items",
                category="memory",
                status="skip",
                message="memory_item.reviewed_ts absent (ops schema predates the v7 migration)",
            )
        rollup = summarize(stale)
        status = "warn" if stale else "pass"
        if stale:
            worst = stale[0]
            message = (
                f"{rollup['count']} active memory item(s) past their type-keyed review half-life "
                f"(worst: {worst['key']!r}, {worst['overdue_days']:.0f}d overdue on a "
                f"{worst['half_life_days']}d clock) -- review with `trialerror memory reviewed <id>`"
            )
        else:
            message = "no active memory items past their type-keyed review half-life"
        return CheckResult(
            name="memory_stale_items",
            category="memory",
            status=status,
            message=message,
            details={
                "count": rollup["count"],
                "by_kind": rollup["by_kind"],
                "threshold": REVIEW_THRESHOLD,
                "items": stale[:_STALE_SAMPLE],
                "truncated": max(0, rollup["count"] - _STALE_SAMPLE),
            },
        )
    finally:
        conn.close()


@register_check("memory_pending_conflict_candidates", category="memory")
def check_memory_pending_conflict_candidates(ctx: DoctorContext) -> CheckResult:
    """MINING ADOPTION engram-F4. Counts save-time conflict candidates
    nobody has judged yet. Tolerant of a store predating the ops v7
    migration (the table simply is not there yet) -- reported as ``skip``,
    the same way a missing DB file is, rather than as a failure."""
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="memory_pending_conflict_candidates",
            category="memory",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        try:
            rows = conn.execute(
                "SELECT r.relation_id, s.key AS source_key, t.key AS target_key "
                "FROM memory_relation r "
                "JOIN memory_item s ON s.memory_item_id = r.source_id "
                "JOIN memory_item t ON t.memory_item_id = r.target_id "
                "WHERE r.judgment_status = 'pending' ORDER BY r.created_ts DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            return CheckResult(
                name="memory_pending_conflict_candidates",
                category="memory",
                status="skip",
                message="memory_relation table absent (ops schema predates the v7 migration)",
            )
        pairs = [{"relation_id": r["relation_id"], "source": r["source_key"], "target": r["target_key"]} for r in rows]
        status = "warn" if pairs else "pass"
        message = (
            f"{len(pairs)} unjudged memory conflict candidate(s) awaiting "
            "`trialerror memory judge --relation <id> --verb <verb> --actor <name>`"
            if pairs
            else "no unjudged memory conflict candidates"
        )
        return CheckResult(
            name="memory_pending_conflict_candidates",
            category="memory",
            status=status,
            message=message,
            details={"count": len(pairs), "candidates": pairs[:_STALE_SAMPLE]},
        )
    finally:
        conn.close()
