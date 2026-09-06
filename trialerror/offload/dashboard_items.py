"""HOME's "what needs a human" line for the GPU offload queue.

Design section 4, verbatim: HOME's "what needs a human": "N documents wait
for the DEV GPU -- run the GPU worker".

Shape matches every other determinations-panel item source in
``trialerror/dashboard/data.py`` (``_gate_edit_items``, ``_kg_merge_items``,
``_acquisition_items``, ``_prereg_reveal_items``, ``_room_escalation_items``,
``_memory_conflict_items``, and lane L0-F's ``mass_deletion_items``): a
``_*_items(rostore) -> list[dict]`` with ``kind``/``id``/``blocking``/
``consequence`` keys.

Reads the queue directory, not the jobs ledger: the ledger's own rows for
these jobs are ``pending`` with a ``next_attempt_ts`` in the future, which
is indistinguishable from any other deferred job. The offload manifests are
the only place that says "this one is waiting for a machine that is
currently switched off", which is precisely the thing a human can act on.

Never ``blocking``: nothing is stuck or unsafe, the operator simply has to
walk over to the laptop. Blocking items are for states that stop the
system from proceeding at all.
"""

from __future__ import annotations

from typing import Any

from trialerror.offload import protocol
from trialerror.dashboard.store_ro import RoStore

__all__ = ["offload_backlog_items"]


def offload_backlog_items(rostore: RoStore) -> list[dict[str, Any]]:
    """Zero or one item: the count of documents waiting for the DEV GPU."""
    program_root = getattr(rostore, "program_root", None)
    if program_root is None:
        return []
    root = protocol.offload_root(program_root)
    if not root.is_dir():
        return []
    pending = protocol.list_pending(root)
    claimed = protocol.list_claims(root)
    if not pending and not claimed:
        return []
    oldest = protocol.oldest_pending_age_s(root)
    waiting = len(pending) + len(claimed)
    return [
        {
            "kind": "offload_backlog",
            "id": "offload_backlog",
            "pending": len(pending),
            "claimed": len(claimed),
            "oldest_pending_age_s": oldest,
            "blocking": False,
            "summary": f"{waiting} document(s) wait for the DEV GPU - run the GPU worker",
            "consequence": (
                f"{waiting} document(s) are parked mid-ingest until the DEV GPU worker runs. "
                "Run the GPU worker on DEV (`trialerror offload worker --remote te-offload`, or the "
                "GPU Worker launcher that wraps it); it claims the queue, runs marker/Qwen3 "
                "locally, publishes the results, and exits when the queue is empty. Nothing is lost "
                "while it waits - the parked jobs keep their full retry budget."
            ),
        }
    ]
