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
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trialerror.memory.api import DEFAULT_TOKEN_BUDGET, estimate_tokens
from trialerror.memory.merge import group_id_from_item_id
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.config import CONFIG_FILENAME, load_config
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_memory_unresolved_conflict_groups", "check_memory_l0_index_budget"]


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
