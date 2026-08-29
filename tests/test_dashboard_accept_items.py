"""The 1 real-browser-DOM item this build names as orchestrator/integration
territory (see ``trialerror.dashboard.accept_items``'s module docstring for why
it lives here rather than in ``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS``
itself). Same unconditional-skip discipline
``tests/acceptance/test_gpu_and_live_cc_journeys.py`` uses for its own
8 items (that file's own module docstring, read as this file's pattern):
this build session has no real browser to drive against a live-served
dashboard page, and the instruction is not to attempt it, not to attempt
detecting whether it's attemptable.
"""

from __future__ import annotations

import pytest

from trialerror.dashboard.accept_items import DASHBOARD_LIVE_ITEMS


@pytest.mark.skip(reason=DASHBOARD_LIVE_ITEMS["live_dom_dashboard_serve_real_browser"])
def test_live_dom_dashboard_serve_real_browser():
    ...


def test_every_dashboard_live_item_has_exactly_one_skip_marked_test():
    """Guards the enumeration itself -- same structural check
    ``test_gpu_and_live_cc_journeys.py::
    test_every_gpu_live_cc_item_has_exactly_one_skip_marked_test`` runs for
    its own dict: every key in :data:`DASHBOARD_LIVE_ITEMS` has exactly one
    corresponding ``test_<key>`` function above, skip-marked with that
    exact reason string."""
    import sys

    module = sys.modules[__name__]
    for key in DASHBOARD_LIVE_ITEMS:
        fn = getattr(module, f"test_{key}", None)
        assert fn is not None, f"no test_{key} function defined in this module"
        marks = getattr(fn, "pytestmark", [])
        skip_marks = [m for m in marks if m.name == "skip"]
        assert skip_marks, f"test_{key} is not @pytest.mark.skip-marked"
        assert skip_marks[0].kwargs.get("reason") == DASHBOARD_LIVE_ITEMS[key]
