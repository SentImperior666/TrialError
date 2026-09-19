"""A ready-made demo program, so the dashboard can be seen doing something.

``trialerror demo seed`` scaffolds a program and fills it with a small,
coherent research narrative that touches every dashboard panel. See
:mod:`trialerror.demo.seed` for the mechanics and why it drives real APIs
rather than writing fixture rows, and :mod:`trialerror.demo.content` for the
(entirely synthetic, explicitly labelled) narrative itself.
"""

from __future__ import annotations

from trialerror.demo.seed import DemoSeedResult, SeedRefused, seed_demo_program

__all__ = ["DemoSeedResult", "SeedRefused", "seed_demo_program"]
