"""``trialerror.budget`` - the budget subsystem (design Section 12, M3 row):
"pools, book/reconcile/status/calibrate, snapshot ingest; PreToolUse hook
w/ atomic booking consumption ... open-session requirement + account
binding; model-policy check; DEFER."

This is the design's central original contribution (Section 1 commitment
1, "Enforcement over convention"): budget-at-spawn is not a convention an
agent is asked to follow, it is a hook (``plugin/hooks/spawn_gate.py``,
backed by :mod:`trialerror.budget.gate`) that makes an unbooked ``Task`` call
physically impossible while hooks are armed.

Public surface:

- :mod:`trialerror.budget.policy` - model-class ranking + purpose policy checks
  (design Section 1.11 / 5.4).
- :mod:`trialerror.budget.pools` - ``book_launch``/``reconcile_launch``/
  ``budget_status``/``list_pools``/``create_pool``/``tree_rollup``/
  ``snapshot_ingest``/``calibrate`` (design Section 5.2 CLI table's
  ``budget`` group + Section 5.1 ``trialerror-ops`` tools 2-4).
- :mod:`trialerror.budget.gate` - the atomic PROVISIONAL->RUNNING booking
  claim (design Section 5.4 PreToolUse row; review finding F2) plus the
  model-policy/law-pin re-checks the same gate performs.
- :mod:`trialerror.budget.checks` - doctor checks for this subsystem.
"""

from __future__ import annotations

from trialerror.budget.errors import (
    BudgetError,
    NoOpenSessionError,
    ModelPolicyViolationError,
    UnknownOverrideRulingError,
)
from trialerror.budget.gate import GateResult, evaluate_spawn, evaluate_spawn_for_open_session, extract_launch_id_token
from trialerror.budget.pools import (
    BookResult,
    book_launch,
    budget_status,
    calibrate,
    create_pool,
    list_pools,
    reconcile_launch,
    snapshot_ingest,
    tree_rollup,
)

__all__ = [
    "BudgetError",
    "NoOpenSessionError",
    "ModelPolicyViolationError",
    "UnknownOverrideRulingError",
    "GateResult",
    "evaluate_spawn",
    "evaluate_spawn_for_open_session",
    "extract_launch_id_token",
    "BookResult",
    "book_launch",
    "budget_status",
    "calibrate",
    "create_pool",
    "list_pools",
    "reconcile_launch",
    "snapshot_ingest",
    "tree_rollup",
]
