"""Lane L0-F: the ``mass_deletion`` determinations-panel item source.
Design section 14's offload-subsystem precedent for what "HOME's 'what
needs a human'" means in this codebase: "HOME's 'what needs a human':
'N documents wait for the DEV GPU -- run the GPU Worker'." This module is
the containment lane's equivalent line.

NOT auto-wired into ``trialerror.dashboard.data.build_determinations_panel``.
That function is a shared list in a shared file this lane does not own —
the implementation brief for this lane names the exact escape hatch for
this situation: "if it requires editing a shared list, describe the
one-line edit in INTEGRATION.md instead of doing it." This module supplies
the ready-to-wire function, in the SAME ``_*_items(rostore) -> list[dict]``
shape every other panel item source already uses (see
``trialerror/dashboard/data.py``'s ``_gate_edit_items``, ``_kg_merge_items``,
``_acquisition_items``, ``_prereg_reveal_items``, ``_room_escalation_items``,
``_memory_conflict_items``); ``deploy/sandbox/containment/INTEGRATION.md``
spells out the two-line edit (one import, one ``items.extend(...)`` call
inside ``build_determinations_panel``) that wires it in.

Deliberately reads the SAME flag file
:func:`trialerror.containment.checks.check_mass_deletion` writes/clears
(``<platform_root>/MASS_DELETION_DETECTED``) rather than re-running the
check itself: a dashboard render must never carry a doctor check's side
effects (the flag write/clear), and re-deriving the drop percentages here
would risk drifting from the check's own math. If the flag is stale
(the underlying problem was already fixed but nothing has re-run
``trialerror doctor`` since) that is a doctor-staleness question, not a
dashboard-render bug — the same "visible, not refused" spirit every other
determinations-panel item already has.
"""

from __future__ import annotations

from typing import Any

from trialerror.containment.checks import FLAG_FILENAME
from trialerror.dashboard.store_ro import RoStore

__all__ = ["mass_deletion_items"]


def mass_deletion_items(rostore: RoStore) -> list[dict[str, Any]]:
    """Zero or one item: the ``mass_deletion`` doctor check's flag file,
    if it is currently set. ``rostore.platform_root`` is always populated
    (see ``trialerror.dashboard.store_ro.RoStore`` / ``open_store_ro`` —
    it is not one of the four optional store connections), so this needs
    no ``is_available()`` guard the way the DB-backed item sources do."""
    flag_path = rostore.platform_root / FLAG_FILENAME
    if not flag_path.is_file():
        return []
    try:
        detail = flag_path.read_text(encoding="utf-8")
    except OSError:
        detail = "(flag file present but unreadable)"
    return [
        {
            "kind": "mass_deletion",
            "id": "mass_deletion",
            "flag_path": str(flag_path),
            "detail": detail,
            "blocking": True,
            "consequence": (
                "te-mirror.sh refuses to mirror the workspace into the git hub while this flag is "
                "set, so a destroyed working copy is never propagated into history. Run `trialerror "
                "doctor --only mass_deletion` after investigating; a clean result clears this flag "
                "and un-blocks the mirror on its next scheduled run."
            ),
        }
    ]
