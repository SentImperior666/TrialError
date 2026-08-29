"""``trialerror.dashboard`` -- the v1 LIVE DASHBOARD (design Section 11): a local,
read-only web view over one TrialError program's stores.

Ops cockpit, not a research-content viz (that distinction is deliberate --
see the module docstrings under this package for the panel-by-panel scope).
Modeled on the origin-project mechspace dashboard's proven serve+watch architecture
(``research/tools/research_dashboard/mechspace/serve_mechspace.py`` in the
sibling ``origin-project`` repo, read there as a READ-ONLY
reference -- this package is TrialError's own build, not a copy): stdlib
``http.server`` + a Server-Sent-Events endpoint fed by a polling mtime
watcher, a static-snapshot export fallback, and localhost-only binding.

Public surface, by submodule:

- :mod:`trialerror.dashboard.store_ro` -- read-only connections to a program's
  four stores (never opens ``sqlite3`` directly outside
  ``trialerror.stores.connection.connect``, exactly like every
  ``trialerror.<subsystem>.checks`` doctor module already does).
- :mod:`trialerror.dashboard.data` -- one data-builder function per panel,
  pure functions of an :class:`~trialerror.dashboard.store_ro.RoStore` (or
  ``None`` fields on it) to a JSON-serializable ``dict`` -- these are what
  both the live server and the static exporter call, so the two paths can
  never independently drift on what a panel means.
- :mod:`trialerror.dashboard.doctor_run` -- the on-demand doctor-run sidecar
  (the dashboard's doctor panel reports the LAST run's results; running
  doctor itself is an explicit action, never part of the passive
  watch/refresh loop).
- :mod:`trialerror.dashboard.serve` -- the live serve+watch+SSE layer.
- :mod:`trialerror.dashboard.export` -- the static self-contained snapshot
  writer (``trialerror dashboard export``).
- :mod:`trialerror.dashboard.accept_items` -- this lane's own
  ``GPU_LIVE_CC_ITEMS``-shaped enumeration for the one acceptance item that
  is genuinely orchestrator/integration territory (a real browser DOM
  check) -- see that module's docstring for why it is NOT added to
  ``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS`` itself (out of this lane's
  write scope).

CLI surface: ``trialerror/cli/dashboard.py`` (``trialerror dashboard serve``,
``trialerror dashboard export``).
"""

from __future__ import annotations
