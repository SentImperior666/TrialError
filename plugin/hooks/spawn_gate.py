#!/usr/bin/env python3
"""Compatibility shim: the spawn_gate hook now lives in
:mod:`trialerror.hooks.spawn_gate` so it can be reached through the
``trialerror`` console script (``trialerror hook spawn-gate``) instead of a
bare ``python <path>`` invocation that is not portable to Linux -- see
:mod:`trialerror.hooks` for the full account.

``plugin/hooks/hooks.json`` no longer points here. This file stays so that
anything invoking the script by path -- an older hooks.json, a hand-rolled
settings.json, or this repo's own subprocess tests -- keeps working.
``_evaluate`` and ``main`` are re-exported unchanged.
"""

from __future__ import annotations

from trialerror.hooks.spawn_gate import _evaluate, main  # noqa: F401  (re-exported for callers/tests)

if __name__ == "__main__":
    raise SystemExit(main())
