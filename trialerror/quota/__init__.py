"""L4 (F6 first slice): the exchange rate between work and quota, the
monthly spend cap, and the new-account guard (``design/L4_quota-policy.md``).

Every table this package touches (``quota_capture``, ``quota_rate``,
``quota_notice``) lives in ``platform.db`` -- a fact about the plan/account,
not about any one program -- exactly like the pre-existing
``quota_snapshot`` and L3's ``unit``/``probe_run`` (see
:mod:`trialerror.stores.schema.platform`).

Submodules:

- :mod:`trialerror.quota.capture_import` -- ``rate_limits.jsonl`` -> ``quota_capture``.
- :mod:`trialerror.quota.rate` -- the ratio-estimator rate fit (``quota rate fit``).
- :mod:`trialerror.quota.forecast` -- USD/token -> window points (``quota forecast``).
- :mod:`trialerror.quota.monthly` -- the monthly spend cap and its notices.
- :mod:`trialerror.quota.accounts` -- the new-account guard (pure function; the
  machine-wide meter script this feeds lives in a separate repository, so
  this lane lands the guard's logic without editing that script itself).
- :mod:`trialerror.quota.notify` -- flag files -> ``quota_notice`` rows, pushed
  once through ``[packet] notify_cmd``.
"""

from __future__ import annotations
