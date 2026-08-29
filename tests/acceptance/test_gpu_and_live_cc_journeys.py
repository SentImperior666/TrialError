"""The 8 GPU-hardware / live-Claude-Code journeys design Section 12 (M3,
M6, M7, M8, M14 rows) + Section 13 flag F18 name as
"orchestrator-executed integration items" -- genuinely live only on a
machine with the marker+Qwen3 GPU environment (design's own stated
hardware assumption for the M15 acceptance host) inside an ACTUAL Claude
Code session. This build's binding instructions: "implement as ENUMERATED,
SKIP-MARKED steps with exact instructions in their skip messages ... do
not attempt to run them" -- so every test below is unconditionally
``@pytest.mark.skip`` (never ``skipif``-gated on hardware detection: this
build session neither has the GPU environment nor a live Claude Code host
to probe for, and the instruction is not to attempt these, not to attempt
detecting whether they're attemptable).

Reason strings are pulled from :data:`trialerror.accept.journeys.GPU_LIVE_CC_ITEMS`
-- the SAME single source of truth ``trialerror accept``'s own doctor-shaped
summary reads from (see
``tests/acceptance/test_clean_checkout_smoke.py::test_trialerror_accept_includes_the_gpu_live_cc_enumeration_by_default``),
so the pytest skip reason and the CLI's listed message can never drift
apart.
"""

from __future__ import annotations

import pytest

from trialerror.accept.journeys import GPU_LIVE_CC_ITEMS

pytestmark = pytest.mark.acceptance


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_session_start_round_trip"])
def test_live_cc_session_start_round_trip():
    ...


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_spawn_gate_pretooluse_task"])
def test_live_cc_spawn_gate_pretooluse_task():
    ...


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_stop_hook_close_check"])
def test_live_cc_stop_hook_close_check():
    ...


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_task_matcher_wiring"])
def test_live_cc_task_matcher_wiring():
    ...


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_mcp_smoke_knowledge_server"])
def test_live_cc_mcp_smoke_knowledge_server():
    ...


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_mcp_smoke_ops_server_book_spawn_reconcile"])
def test_live_cc_mcp_smoke_ops_server_book_spawn_reconcile():
    ...


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["gpu_real_marker_ocr_backend"])
def test_gpu_real_marker_ocr_backend():
    ...


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["gpu_real_qwen_embed_backend"])
def test_gpu_real_qwen_embed_backend():
    ...


def test_every_gpu_live_cc_item_has_exactly_one_skip_marked_test():
    """Guards the enumeration itself: every key in
    :data:`GPU_LIVE_CC_ITEMS` has exactly one corresponding
    ``test_<key>`` function above -- a future addition to the dict that
    forgets its skip-marked test (or a typo'd test name) fails loudly here
    instead of silently under-enumerating what `trialerror accept` reports."""
    import sys

    module = sys.modules[__name__]
    for key in GPU_LIVE_CC_ITEMS:
        fn = getattr(module, f"test_{key}", None)
        assert fn is not None, f"no test_{key} function defined in this module"
        marks = getattr(fn, "pytestmark", [])
        skip_marks = [m for m in marks if m.name == "skip"]
        assert skip_marks, f"test_{key} is not @pytest.mark.skip-marked"
        assert skip_marks[0].kwargs.get("reason") == GPU_LIVE_CC_ITEMS[key]
