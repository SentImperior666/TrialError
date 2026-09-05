"""``trialerror.containment`` — lane L0-F, the disaster-containment proof
obligations behind operator decision O11 (design section 14 of
``docs/reviews/lane0/LANE0_SANDBOX_RELOCATION_DESIGN.md``: "no permission
checks inside the container, conditional on the containment drill in
section 14 passing").

The operator's ruling (C-0077) this subsystem exists to satisfy: "rock
solid, absolute proof that the sandbox will prevent any possible disaster
like deleting all of the research files." The actual proof is the
REACHABILITY ARGUMENT in design section 14 — the git hubs are not mounted
into the container, hourly hard-link snapshots and a nightly off-machine
copy live outside the mount, and none of that is code this package ships
(it is host-side cron + shell, see ``deploy/sandbox/containment/``). This
package is the one piece of the proof that DOES run inside the program:
the detection half.

- :mod:`trialerror.containment.checks` — the ``mass_deletion`` doctor check,
  auto-discovered by :func:`trialerror.util.doctor.discover_and_register_checks`
  exactly like every other subsystem's ``checks.py``. Compares live file
  counts under the three watched paths against the last snapshot manifest
  ``deploy/sandbox/containment/te-snapshot.sh`` writes, and — its one
  deliberate side effect among this codebase's doctor checks — writes or
  clears the ``MASS_DELETION_DETECTED`` flag file
  ``deploy/sandbox/containment/te-mirror.sh`` refuses to mirror past.
- :mod:`trialerror.containment.dashboard_items` — a determinations-panel
  item source in the same ``_*_items(rostore) -> list[dict]`` shape
  ``trialerror.dashboard.data`` already uses (see that module's
  ``_gate_edit_items``/``_kg_merge_items``/... functions), deliberately
  NOT wired into ``build_determinations_panel`` by this lane — that
  function lives in a shared file this lane does not own. See
  ``deploy/sandbox/containment/INTEGRATION.md`` for the two-line edit
  that wires it in.

Everything else in the design section 14 proof (``te-snapshot.sh``,
``te-mirror.sh``, ``te-drive-sync.sh``, ``te-drill.sh``, the crontab, the
managed-settings deny list) is host-side shell under
``deploy/sandbox/containment/`` — deliberately outside this Python
package, for the same reason it is outside the container's reach at all:
none of it should be something the in-container agent process can read,
edit, or run.
"""

from __future__ import annotations
